"""Dashboard check: ui/app.py renders every saved run in replay mode, with no service and no API key.
Uses Streamlit's AppTest (no browser, no server, no port). Run from the project root:  python tests/check_ui.py
On a fresh clone without saved runs it checks the empty state instead.
"""

import json
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
RAW_RULES = ["payment_gap", "queue_backlog", "worker_stalled", "ledger_mismatch", "high_error_rate", "messages_dropped"]
passed = total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    passed += bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


def texts(at, kind: str) -> list[str]:
    return [e.value for e in getattr(at, kind)]


def charts(at) -> list:
    """Altair charts on the page (AppTest has no wrapper for them yet: they show up as VegaLiteChart protos)."""
    return [e for e in at.main if type(e).__name__ == "UnknownElement" and e.proto.DESCRIPTOR.name == "VegaLiteChart"]


def blocks(node) -> list:
    """Every layout block under a node (containers are nested inside tabs and columns)."""
    found = []
    for child in getattr(node, "children", {}).values():
        if type(child).__name__ == "Block":
            found.append(child)
        found += blocks(child)
    return found


at = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=120).run()
check("dashboard loads without an exception", not at.exception, at.exception[0].message if at.exception else "")

# ---- 8. evaluation headline chart (from eval/results.csv)
if (ROOT / "eval" / "results.csv").exists():
    found = charts(at)
    spec = json.loads(found[0].proto.spec) if found else {}
    check("evaluation: headline chart shown", bool(found))
    check("evaluation: bars faceted dev vs held-out, labelled with k/N",
          spec.get("facet", {}).get("column", {}).get("field") == "Set"
          and any(layer.get("encoding", {}).get("text", {}).get("field") == "Label"
                  for layer in spec.get("spec", {}).get("layer", [])), str(spec)[:200])

saved = any((ROOT / folder).glob("*.json") for folder in ("replays", "eval/runs") if (ROOT / folder).exists())
if not saved:
    check("no saved runs: says so instead of crashing", any("No saved runs" in i for i in texts(at, "info")))
else:
    runs = at.sidebar.selectbox[-1]
    names = list(runs.options)
    if at.sidebar.radio[0].value == "Showcase replays":
        # ---- 6. readable run names from the replay filenames
        check("replays: default source with readable names", names[0].startswith("1. ") and "_" not in " ".join(names),
              str(names[:3]))
    failures, raw_json, metrics_tables = [], [], 0
    for i in range(len(names)):
        at.sidebar.selectbox[-1].select_index(i)
        at.run()
        if at.exception:
            failures.append(f"{names[i]}: {at.exception[0].message}")
        raw_json += [names[i] for t in texts(at, "code") if t.startswith("HTTP 200: {")]
        metrics_tables += any("| Orders created |" in t for t in texts(at, "markdown"))
    check(f"every saved run renders ({len(names)} runs)", not failures, "; ".join(failures[:3]))
    # ---- 5. readable tool results (incl. business metrics cut off at 1500 characters in the record)
    check("tool results formatted: no raw JSON on any run, business metrics shown as a table",
          not raw_json and metrics_tables > 0, f"raw JSON in {sorted(set(raw_json))[:3]}, metrics tables {metrics_tables}")

    at.sidebar.selectbox[-1].select_index(0)  # first run: a fault Nabd fixed
    at.run()
    alert_box = [t for t in texts(at, "error") if t.startswith("### At alert")]
    after_box = [t for t in texts(at, "success") + texts(at, "warning") if t.startswith("### After Nabd")]
    # ---- 1. before and after
    check("before/after: 'At alert: N broken' and 'After Nabd: ...' shown", bool(alert_box) and bool(after_box),
          f"{alert_box[:1]} {after_box[:1]}")
    # ---- 3. friendly labels, not rule names
    check("status uses friendly labels, not rule names",
          alert_box and not any(rule in alert_box[0] for rule in RAW_RULES), alert_box[0][:120] if alert_box else "")
    # ---- 2. money on top
    labels = [m.label for m in at.metric]
    check("money banner: SAR at risk, SAR recovered, delayed orders shown first",
          labels[:3] == ["SAR at risk", "SAR recovered", "Orders delayed (queued)"], str(labels[:4]))
    check("tagline shown", any("business is actually working" in c for c in texts(at, "caption")))
    check("Arabic report keeps its right-to-left markup", any('dir="rtl"' in m for m in texts(at, "markdown")))

    # ---- 4. Play from the start: animates into an auto-scrolling container with a progress bar
    shortest = min(range(len(names)), key=lambda i: len(names[i]) if "ealthy" in names[i] else 99)
    at.sidebar.selectbox[-1].select_index(shortest)
    at.run()
    at.button[0].click()
    at.run(timeout=180)
    scrolling = [b for b in blocks(at.main) if b.proto.autoscroll]
    check("Play: runs without an exception, progress bar and auto-scrolling timeline shown",
          not at.exception and len(at.get("progress")) == 1 and bool(scrolling),
          at.exception[0].message if at.exception else f"progress {len(at.get('progress'))}, autoscroll {len(scrolling)}")

# ================================================================ live mode, with every step faked
# (no service, no port, no API key: the fakes stand in for chaos, monitor, the agent and the report)
import tempfile  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402

sys.path.insert(0, str(ROOT / "ui"))
import live  # noqa: E402  (the same module object the dashboard imports)

ALERT = ("Health check: passing. 1 anomaly(ies), most urgent first:\n"
         "- [business] payment_gap: 11 of 27 orders were processed but have no payment.")
FIXES = [("set_config", {"key": "payment_provider_url", "value": "internal://payments-v1"}),
         ("reprocess_orders", {"order_ids": "all_unpaid"})]
NO_MONEY = {"count": 0, "by_currency": {}, "total_sar": 0.0}
SOME_MONEY = {"count": 11, "by_currency": {"SAR": 4200.0}, "total_sar": 4200.0}
fake = {"applied": [], "agent_calls": 0, "found": ["payment_gap"]}


class FakeTraffic:
    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def anomaly(rule: str) -> NS:
    return NS(rule=rule, kind="business", message="11 of 27 orders were processed but have no payment.")


def fake_agent(alert: str, approve, on_event) -> tuple:
    """Behaves like agent.run_agent from the outside: events, one approval per fix, a record at the end."""
    fake["agent_calls"] += 1
    fake["applied"] = []
    on_event("alert", {"text": alert})
    for tool, args in FIXES:
        on_event("tool_call", {"tool": tool, "args": args})
        ok = approve(tool, args)
        text = "HTTP 200: {\"paid\": 11}" if ok else "REJECTED by human operator. Pick another safe action or escalate."
        on_event("tool_result", {"tool": tool, "text": text, "is_error": False})
        if ok:
            fake["applied"].append(f"{tool} {json.dumps(args)}")
    recovered = len(fake["applied"]) == len(FIXES)
    money = {"unpaid_orders": SOME_MONEY, "queued_orders": NO_MONEY, "ledger_overstatement": NO_MONEY}
    diagnosis = {"root_causes": ["payment_provider_url changed to payments-v2"], "evidence": ["DEBUG provider call failed"],
                 "confidence": 0.9, "impact": "11 orders unpaid", "actions_taken": fake["applied"],
                 "recommended_actions": [] if recovered else ["Revert payment_provider_url"], "recovered": recovered,
                 "escalate": not recovered, "summary": "Fake run.", "money_impact_before": money,
                 "money_impact_peak": money if fake["applied"] else None,
                 "money_recovered": {"orders_paid": SOME_MONEY if recovered else NO_MONEY, "ledger_corrected": NO_MONEY},
                 "money_impact_after": {**money, "unpaid_orders": NO_MONEY if recovered else SOME_MONEY}}
    record = {"alert": alert, "events": [], "diagnosis": diagnosis,
              "stats": {"steps": 4, "seconds": 3.0, "est_cost_usd": 0.0, "writes_applied": fake["applied"]}}
    return NS(), record


saved_dir = Path(tempfile.mkdtemp())


def fake_save(record: dict, fault: str) -> Path:
    path = saved_dir / f"live_{fault}_{len(list(saved_dir.glob('*.json')))}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    path.with_suffix(".report.md").write_text("# Incident report\n\nFake report.\n\n---\n\n"
                                              '<div dir="rtl">\n\nتقرير\n\n</div>\n', encoding="utf-8")
    return path


traffic_handles: list[FakeTraffic] = []
live.reset_service = lambda: "started (fake)"
live.start_traffic = lambda: traffic_handles.append(FakeTraffic()) or traffic_handles[-1]
live.inject = lambda fault: None
live.run_agent = fake_agent
live.save_run = fake_save
live.has_api_key = lambda: True
live.monitor = NS(snapshot=lambda: {"health_ok": True},
                  evaluate=lambda prev, cur: [anomaly(r) for r in fake["found"]],
                  build_alert=lambda found, cur: found,
                  format_alert=lambda found: ALERT,
                  check_once=lambda gap=3: [] if len(fake["applied"]) == len(FIXES) else [anomaly("payment_gap")])
live.WARMUP_S, live.POLL_S, live.DETECT_TIMEOUT_S = 0, 0.01, 0.3


def wait(condition, timeout: float = 10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def pending_id(run) -> int | None:
    view = run.view()
    return view["pending"]["id"] if view["pending"] else None


# ---- the run object on its own
run = live.LiveRun("payment_drop")
run.thread.start()
check("live run: a fix waits for the human", wait(lambda: pending_id(run) == 1) and run.view()["pending"]["tool"] == "set_config")
check("live run: approving answers it; a repeated or stale click is ignored",
      run.answer(1, True) and not run.answer(1, False) and not run.answer(99, True))
check("live run: next fix waits, approve it", wait(lambda: pending_id(run) == 2) and run.answer(2, True))
check("live run: finishes with the record, final check green, report saved, traffic stopped",
      wait(lambda: run.view()["done"]) and run.view()["record"]["diagnosis"]["recovered"] and run.view()["after"] == []
      and run.view()["saved"] is not None and traffic_handles[-1].killed and not run.view()["error"])
kinds = [e["kind"] for e in run.view()["events"]]
check("live run: event stream has the pipeline steps, the agent's events and both approvals",
      kinds.count("stage") == len(live.STAGES) and kinds.count("approval") == 2 and "tool_call" in kinds)

run = live.LiveRun("payment_drop")
run.thread.start()
wait(lambda: pending_id(run) == 1)
run.answer(1, False)
wait(lambda: pending_id(run) == 2)
run.answer(2, False)
check("live run: rejecting both fixes -> nothing applied, not recovered, final check still broken",
      wait(lambda: run.view()["done"]) and fake["applied"] == [] and not run.view()["record"]["diagnosis"]["recovered"]
      and [rule for rule, _ in run.view()["after"]] == ["payment_gap"])

run = live.LiveRun("payment_drop")
run.thread.start()
wait(lambda: pending_id(run) == 1)
run.cancel()
check("live run: Stop rejects the waiting fix and every later one, skips the rest, stops traffic",
      wait(lambda: run.view()["done"]) and fake["applied"] == [] and run.view()["cancelled"]
      and run.view()["saved"] is None and run.view()["stage"] == "Nabd investigates" and traffic_handles[-1].killed)

run = live.LiveRun("payment_drop")  # not started: call approve() directly, as the agent thread would
run.cancel()
started = time.time()
check("live run: a fix asked for after Stop is rejected at once (no 10-minute wait)",
      run.approve("restart_worker", {}) is False and time.time() - started < 1 and run.view()["pending"] is None)

live.APPROVAL_TIMEOUT_S = 0.2
run = live.LiveRun("payment_drop")
run.thread.start()
check("live run: no answer in time = rejected (never waits forever)",
      wait(lambda: run.view()["done"]) and fake["applied"] == []
      and any(e["kind"] == "approval" and "no answer" in e["how"] for e in run.view()["events"]))
live.APPROVAL_TIMEOUT_S = 600

fake["found"], calls = [], fake["agent_calls"]
run = live.LiveRun("payment_drop")
run.thread.start()
check("live run: no alert -> the agent is never called",
      wait(lambda: run.view()["done"]) and fake["agent_calls"] == calls and run.view()["record"] is None
      and not run.view()["error"])
fake["found"] = ["payment_gap"]

real_inject = live.inject
live.inject = lambda fault: (_ for _ in ()).throw(RuntimeError("injection failed"))
run = live.LiveRun("payment_drop")
run.thread.start()
check("live run: an error is shown, not lost in the thread, and traffic is still stopped",
      wait(lambda: run.view()["done"]) and "injection failed" in run.view()["error"] and traffic_handles[-1].killed)
live.inject = real_inject

live._current = None
first = live.start("payment_drop")
wait(lambda: pending_id(first) == 1)
try:
    live.start("worker_hang")
    second_refused = False
except RuntimeError:
    second_refused = True
first.cancel()
wait(lambda: first.view()["done"])
check("live mode: one run at a time (a second Start is refused while one runs)", second_refused)

# ---- the page, driven like a person: Live mode, Start, Approve / Reject / Stop in the browser
APP = str(ROOT / "ui" / "app.py")


def live_app() -> AppTest:
    page = AppTest.from_file(APP, default_timeout=60).run()
    page.radio(key="mode").set_value("Live").run()
    return page


def click_when_shown(page: AppTest, key: str, timeout: float = 15) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if any(b.key == key for b in page.button):
            page.button(key=key).click().run()
            return True
        time.sleep(0.1)
        page.run()
    return False


def wait_page(page: AppTest, condition, timeout: float = 15) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        page.run()
        if condition(page):
            return True
        time.sleep(0.1)
    return False


def final_shown(page: AppTest) -> bool:
    return any(s.value == "Diagnosis" for s in page.subheader)


page = live_app()
check("live page: Replay/Live switch, fault picker and Start shown, no exception",
      not page.exception and any(b.key == "live_start" and not b.disabled for b in page.button)
      and page.selectbox(key="live_fault").value == "payment_drop")
page.button(key="live_start").click().run()
check("live page: Approve fix 1 in the browser", click_when_shown(page, "approve_1"))
check("live page: Approve fix 2 in the browser", click_when_shown(page, "approve_2"))
check("live page: final view with before/after, money, diagnosis and the bilingual report",
      wait_page(page, final_shown) and not page.exception
      and any(t.value.startswith("### After Nabd: all green") for t in page.success)
      and [m.label for m in page.metric][:2] == ["SAR at risk", "SAR recovered"]
      and any('dir="rtl"' in m.value for m in page.markdown), page.exception[0].message if page.exception else "")
check("live page: the event stream shows both approvals",
      sum("✅ approved (by you)" in m.value for m in page.markdown) == 2)

page = live_app()
page.button(key="live_start").click().run()
check("live page: Reject fix 1 and fix 2 in the browser",
      click_when_shown(page, "reject_1") and click_when_shown(page, "reject_2"))
check("live page: after rejecting, the final view says still broken, escalated",
      wait_page(page, final_shown) and any(t.value.startswith("### After Nabd: still broken") for t in page.warning)
      and sum("❌ rejected (by you)" in m.value for m in page.markdown) == 2)

page = live_app()
page.button(key="live_start").click().run()
wait_page(page, lambda p: any(b.key == "approve_1" for b in p.button))
page.button(key="live_stop").click().run()
check("live page: Stop while a fix waits -> run ends as stopped, nothing applied",
      wait(lambda: live.current().view()["done"]) and wait_page(page, lambda p: any("Stopped by you" in c.value for c in p.caption))
      and fake["applied"] == [] and not page.exception
      and any(i.value.startswith("### After Nabd: not checked") for i in page.info))

live.has_api_key = lambda: False
page = live_app()
check("live page: without an API key, Start is disabled and says why",
      any(b.key == "live_start" and b.disabled for b in page.button)
      and any("ANTHROPIC_API_KEY" in e.value for e in page.error))
live.has_api_key = lambda: True

print(f"\n{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
