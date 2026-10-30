"""Agent loop checks with scripted fake model replies and fake tools: no API key, no service needed.
Run from the project root:  python tests/check_agent_mock.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]  # the project root (this file is in tests/)
sys.path.insert(0, str(ROOT))
import agent  # noqa: E402

calls = []
fakes = {
    "read_logs": lambda **a: "1x [DEBUG] payment: provider call failed",
    "check_invariants": lambda: "All business invariants green",
    "restart_worker": lambda: calls.append("restart_worker") or "HTTP 200",
    "rollback_deploy": lambda: calls.append("rollback_deploy") or "rolled back",
    "set_config": lambda key, value: calls.append(f"set_config {value}") or "ok",
    "reprocess_orders": lambda order_ids: (_ for _ in ()).throw(RuntimeError("boom")),
}
agent.TOOL_FUNCS.update(fakes)
broken_now = []  # what the (fake) monitor reports when the agent claims recovery
agent.monitor.check_once = lambda gap=3: [NS(rule=r) for r in broken_now]
agent.fetch_money_impact = lambda: None  # no service in these checks; the money checks below replace this
agent.fetch_config = lambda: None

USAGE = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=None)


def tool(name, inp, i):
    return NS(type="tool_use", name=name, input=inp, id=f"t{i}")


def reply(stop, *blocks, model="claude-sonnet-5-5"):
    return NS(stop_reason=stop, content=list(blocks), usage=USAGE, model=model)


good = dict(root_causes=["bad provider url"], evidence=["DEBUG provider call failed"], confidence=0.4,
            impact="25 orders unpaid", actions_taken=[], recommended_actions=[], recovered=True,
            escalate=False, summary="s")
bad = dict(good, confidence="high")


def run(script, approve=lambda n, a: True, recommend=False):
    sent = []

    def fake_create(**kw):
        sent.append({**kw, "messages": list(kw["messages"])})  # snapshot: the real list keeps growing
        return script.pop(0)
    agent.client = NS(beta=NS(messages=NS(create=fake_create)))
    d, rec = agent.run_agent("test alert", approve=approve, on_event=lambda *e: None, recommend_only=recommend)
    return d, rec, sent


def last_user(sent, i):
    return sent[i]["messages"][-1]["content"]


checks = {}
d, rec, sent = run([
    reply("end_turn", NS(type="thinking", thinking="looking at metrics"), NS(type="text", text="hmm")),
    reply("max_tokens", tool("read_logs", {"min_level": "DEBUG"}, 1)),
    reply("tool_use", tool("restart_worker", {}, 2), tool("rollback_deploy", {}, 3)),
    reply("tool_use", tool("set_config", {"key": "payment_provider_url", "value": "x"}, 4)),
    reply("tool_use", tool("set_config", {"key": "payment_provider_url", "value": "x"}, 5)),
    reply("tool_use", tool("set_config", {"key": "payment_provider_url", "value": "x"}, 6)),
    reply("tool_use", tool("reprocess_orders", {"order_ids": "all_unpaid"}, 7)),
    reply("tool_use", tool("submit_diagnosis", bad, 8), model="claude-opus-5-5"),
    reply("tool_use", tool("submit_diagnosis", good, 9)),
])
kw = sent[0]
checks["request: no temperature, effort medium, summarized thinking, cache, fallback"] = (
    "temperature" not in kw and kw["output_config"] == {"effort": "medium"}
    and kw["thinking"]["display"] == "summarized" and kw["cache_control"] == {"type": "ephemeral"}
    and kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"])
checks["act mode offers write tools"] = "restart_worker" in [t["name"] for t in kw["tools"]]
checks["nudge after text-only reply"] = "submit_diagnosis" in str(last_user(sent, 1))
checks["max_tokens tool_use gets a tool_result"] = last_user(sent, 2)[0]["type"] == "tool_result"
checks["second write in same turn skipped"] = "rollback_deploy" not in calls and "SKIPPED" in last_user(sent, 3)[1]["content"]
checks["same fix runs twice, third time BLOCKED"] = (calls.count("set_config x") == 2
                                                     and "BLOCKED" in last_user(sent, 6)[0]["content"])
checks["failing tool -> is_error true"] = last_user(sent, 7)[0]["is_error"] is True
checks["invalid diagnosis sent back as error"] = ("Invalid diagnosis" in last_user(sent, 8)[0]["content"]
                                                 and last_user(sent, 8)[0]["is_error"] is True)
checks["low confidence forces escalate"] = d.escalate is True
checks["fallback model recorded"] = any(e["kind"] == "fallback" for e in rec["events"])
checks["thinking + thought events recorded"] = {"thinking", "thought"} <= {e["kind"] for e in rec["events"]}
s = rec["stats"]
checks["stats: tokens, cache, cost, writes"] = (s["steps"] == 9 and s["input_tokens"] == 90 and s["cache_read_tokens"] == 900
                                                and s["est_cost_usd"] > 0 and len(s["writes_applied"]) == 3
                                                and len(s["writes_blocked"]) == 1)

d, rec, sent = run([reply("refusal")])
checks["refusal -> escalate, no crash"] = d.escalate and "declined" in d.root_causes[0].lower()

d, rec, sent = run([tool_reply := reply("tool_use", tool("restart_worker", {}, 1)),
                    reply("tool_use", tool("submit_diagnosis", dict(good, confidence=0.9), 2))],
                   approve=lambda n, a: False)
checks["rejected write not executed, recorded"] = (calls.count("restart_worker") == 1  # only the earlier run
                                                   and len(rec["stats"]["writes_rejected"]) == 1
                                                   and "REJECTED" in last_user(sent, 1)[0]["content"])

d, rec, sent = run([reply("tool_use", tool("submit_diagnosis", dict(good, confidence=0.9), 1))], recommend=True)
names = [t["name"] for t in sent[0]["tools"]]
checks["recommend mode: no write tools at all"] = not (set(names) & agent.WRITE_TOOLS) and "submit_diagnosis" in names
checks["recommend mode: prompt says so"] = "RECOMMEND-ONLY" in sent[0]["system"]

honest = dict(good, confidence=0.9)
d, rec, sent = run([reply("tool_use", tool("submit_diagnosis", honest, 1))])
checks["true recovery claim kept"] = d.recovered and not d.escalate and rec["stats"]["recovery_claim_overridden"] is False
broken_now[:] = ["payment_gap"]
d, rec, sent = run([reply("tool_use", tool("submit_diagnosis", honest, 1))])
checks["FALSE recovery claim overridden in code -> not recovered, escalate"] = (
    not d.recovered and d.escalate and "payment_gap" in d.summary and rec["stats"]["recovery_claim_overridden"]
    and any(e["kind"] == "verification" for e in rec["events"]))
broken_now[:] = []

# A relapse: green at first, broken a few readings later. One quick check would miss it.
sequence = [[], [], ["queue_backlog"]]
fake_check_once = agent.monitor.check_once
agent.monitor.check_once = lambda gap=3: [NS(rule=r) for r in (sequence.pop(0) if sequence else [])]
d, rec, sent = run([reply("tool_use", tool("submit_diagnosis", honest, 1))])
agent.monitor.check_once = fake_check_once
checks["recovery claim that relapses on the 3rd reading -> overridden, escalate"] = (
    not d.recovered and d.escalate and "queue_backlog" in d.summary and not sequence)
calls_made = []
agent.monitor.check_once = lambda gap=3: calls_made.append(gap) or []
d, rec, sent = run([reply("tool_use", tool("submit_diagnosis", honest, 1))])
agent.monitor.check_once = fake_check_once
checks["true recovery: watched for 5 readings of 3s (~15s) before it is accepted"] = (
    d.recovered and calls_made == [3] * agent.VERIFY_READINGS)

# ---- same fix fails twice -> escalate (design rule 7 / official example 5: never give up silently)
URL_X = {"key": "payment_provider_url", "value": "x"}
URL_Y = {"key": "payment_provider_url", "value": "y"}


def same_fix_three_times(final: dict, then=None):
    """Apply the same fix twice, try it a third time, optionally do something else, then submit."""
    steps = [reply("tool_use", tool("set_config", URL_X, i)) for i in range(3)]
    if then:
        steps.append(reply("tool_use", tool(*then, 3)))
    steps.append(reply("tool_use", tool("submit_diagnosis", final, 9)))
    return run(steps)


before = calls.count("set_config x")
gives_up = dict(good, confidence=0.9, recovered=False, escalate=False)  # high confidence, so it can't mask the rule
d, rec, sent = same_fix_three_times(gives_up)
checks["same fix: executed only twice, third attempt BLOCKED and recorded"] = (
    calls.count("set_config x") - before == 2 and len(rec["stats"]["writes_blocked"]) == 1
    and "BLOCKED" in last_user(sent, 3)[0]["content"])
checks["same fix failed twice, model gives up silently -> code FORCES escalate"] = (
    d.escalate and not d.recovered and "same fix failed twice" in d.summary)

broken_now[:] = ["payment_gap"]
d, rec, sent = same_fix_three_times(dict(good, confidence=0.9, recovered=True, escalate=False))
checks["same fix failed twice, model falsely claims recovered -> still escalates"] = d.escalate and not d.recovered
broken_now[:] = []

d, rec, sent = same_fix_three_times(dict(good, confidence=0.9, recovered=True, escalate=False),
                                    then=("restart_worker", {}))
checks["blocked fix, but a DIFFERENT fix really recovered (verified) -> no forced escalation"] = (
    d.recovered and not d.escalate and len(rec["stats"]["writes_blocked"]) == 1)

before_x, before_y = calls.count("set_config x"), calls.count("set_config y")
d, rec, sent = run([reply("tool_use", tool("set_config", URL_X, 1)), reply("tool_use", tool("set_config", URL_X, 2)),
                    reply("tool_use", tool("set_config", URL_Y, 3)),
                    reply("tool_use", tool("submit_diagnosis", dict(good, confidence=0.9), 4))])
checks["same tool with DIFFERENT arguments is a different fix -> not blocked"] = (
    calls.count("set_config x") - before_x == 2 and calls.count("set_config y") - before_y == 1
    and not rec["stats"]["writes_blocked"])

d, rec, sent = run([reply("tool_use", tool("reprocess_orders", {"order_ids": "all_unpaid"}, i)) for i in range(3)]
                   + [reply("tool_use", tool("submit_diagnosis", gives_up, 9))])
checks["a fix that ERRORS twice also counts as failed twice -> blocked + escalate"] = (
    last_user(sent, 1)[0]["is_error"] and last_user(sent, 2)[0]["is_error"]
    and "BLOCKED" in last_user(sent, 3)[0]["content"] and d.escalate)

script = [reply("tool_use", tool("read_logs", {}, i)) for i in range(agent.MAX_STEPS)]
d, rec, sent = run(script)
checks["step limit -> escalate"] = d.escalate and rec["stats"]["steps"] == agent.MAX_STEPS

# ---- money impact comes from code (the DB), never from the model
def money(unpaid_count, sar, usd):
    zero = {"count": 0, "by_currency": {}, "total_sar": 0.0}
    unpaid = {"count": unpaid_count, "by_currency": {"SAR": sar, "USD": usd}, "total_sar": round(sar + usd * 3.75, 2)}
    return {"unpaid_orders": unpaid, "queued_orders": zero, "ledger_overstatement": zero, "rates_to_sar": {}}


readings = [money(25, 3000.0, 403.28), money(0, 0.0, 0.0)]  # what the "service" reports: before, then after
agent.fetch_money_impact = lambda: readings.pop(0)
schema = agent.SUBMIT_TOOL["input_schema"]
checks["submit_diagnosis schema has NO money fields (the model can't fill them)"] = (
    not (agent.CODE_FIELDS & set(schema["properties"])) and not (agent.CODE_FIELDS & set(schema["required"]))
    and "impact" in schema["required"])
liar = dict(good, confidence=0.9, money_impact_before={"unpaid_orders": {"total_sar": 1}}, money_impact_after=None)
d, rec, sent = run([reply("tool_use", tool("submit_diagnosis", liar, 1))])
checks["model-supplied money is overwritten with the code's before/after figures"] = (
    d.money_impact_before["unpaid_orders"]["total_sar"] == 4512.3 and d.money_impact_after["unpaid_orders"]["count"] == 0
    and rec["diagnosis"]["money_impact_before"] == d.money_impact_before)
checks["impact text gets the computed line (SAR total + per currency)"] = (
    d.impact.startswith("25 orders unpaid") and "unpaid orders: 25 = 4,512.30 SAR (SAR 3,000.00, USD 403.28)" in d.impact
    and "At end of run: unpaid orders: 0 = 0.00 SAR" in d.impact)
checks["no fix applied -> peak None, impact says 'no fix applied', nothing recovered"] = (
    d.money_impact_peak is None and "(peak): no fix applied" in d.impact
    and d.money_recovered["orders_paid"]["count"] == 0)


# ---- peak (just before the first applied fix) and money recovered (from the fix responses)
def fix_response(count, sar, usd, **extra):
    m = {"count": count, "by_currency": {"SAR": sar, "USD": usd}, "total_sar": round(sar + usd * 3.75, 2)}
    return "HTTP 200: " + agent.json.dumps({**extra, "money_recovered": m})


agent.TOOL_FUNCS["reprocess_orders"] = lambda order_ids: fix_response(2, 100.0, 10.0, paid=2)
agent.TOOL_FUNCS["repair_ledger"] = lambda order_ids: fix_response(3, 990.0, 0.0, fixed=3)
readings = [money(25, 3000.0, 403.28), money(40, 5000.0, 600.0), money(0, 0.0, 0.0)]  # alert, peak, end
asked, fetched_after_asks = [], []  # approval requests so far, at the moment of each money fetch
agent.fetch_money_impact = lambda: fetched_after_asks.append(len(asked)) or readings.pop(0)
fake_recovered = {"orders_paid": {"count": 999, "by_currency": {}, "total_sar": 1e9}}
d, rec, sent = run([
    reply("tool_use", tool("restart_worker", {}, 1)),                                   # rejected: no peak yet
    reply("tool_use", tool("reprocess_orders", {"order_ids": [1, 2]}, 2)),              # first applied fix
    reply("tool_use", tool("reprocess_orders", {"order_ids": "all_unpaid"}, 3)),
    reply("tool_use", tool("repair_ledger", {"order_ids": "all_flagged"}, 4)),
    reply("tool_use", tool("submit_diagnosis", dict(good, confidence=0.9, money_recovered=fake_recovered), 5)),
], approve=lambda name, args: asked.append(name) or name != "restart_worker")
# Fetches: at start (0 asks), at the first APPROVED fix (2nd ask: restart was rejected), at the end (4 asks).
checks["peak taken just before the FIRST APPLIED fix (not at a rejected one, not again later)"] = (
    fetched_after_asks == [0, 2, 4] and d.money_impact_peak["unpaid_orders"]["count"] == 40
    and d.money_impact_after["unpaid_orders"]["count"] == 0)
paid, ledger = d.money_recovered["orders_paid"], d.money_recovered["ledger_corrected"]
checks["recovered = sum of the fix responses, model's figure ignored"] = (
    paid == {"count": 4, "by_currency": {"SAR": 200.0, "USD": 20.0}, "total_sar": 275.0}
    and ledger == {"count": 3, "by_currency": {"SAR": 990.0, "USD": 0.0}, "total_sar": 990.0})
checks["impact line has peak + recovered"] = (
    "(peak): unpaid orders: 40 = 7,250.00 SAR" in d.impact
    and "orders paid by reprocessing: 4 = 275.00 SAR (SAR 200.00, USD 20.00)" in d.impact
    and "ledger overstatement corrected: 3 = 990.00 SAR" in d.impact)
agent.TOOL_FUNCS["reprocess_orders"] = fakes["reprocess_orders"]  # back to the one that raises
agent.fetch_money_impact = lambda: readings.pop(0)
readings =[money(5, 50.0, 0.0), money(5, 50.0, 0.0), money(5, 50.0, 0.0)]
d, rec, sent = run([reply("tool_use", tool("reprocess_orders", {"order_ids": "all_unpaid"}, 1)),
                    reply("tool_use", tool("submit_diagnosis", dict(good, confidence=0.9), 2))])
checks["a fix that ERRORS recovers nothing (peak still taken: a fix was attempted)"] = (
    d.money_recovered["orders_paid"]["count"] == 0 and d.money_impact_peak is not None)

readings[:] = [None, None]
d, rec, sent = run([reply("refusal")])
checks["service unreachable -> money marked unavailable, no crash (also on give-up path)"] = (
    d.money_impact_before is None and "unavailable" in d.impact)

for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
print(f"{sum(checks.values())}/{len(checks)} passed")
sys.exit(0 if all(checks.values()) else 1)
