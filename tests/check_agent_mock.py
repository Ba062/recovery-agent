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

script = [reply("tool_use", tool("read_logs", {}, i)) for i in range(agent.MAX_STEPS)]
d, rec, sent = run(script)
checks["step limit -> escalate"] = d.escalate and rec["stats"]["steps"] == agent.MAX_STEPS

for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
print(f"{sum(checks.values())}/{len(checks)} passed")
sys.exit(0 if all(checks.values()) else 1)
