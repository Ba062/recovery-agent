"""Checks of the live-mode email notifications (notify.py + ui/live.py). The HTTP call to Resend is mocked:
no email is sent, no API key or service is needed.
Run from the project root:  python tests/check_notify.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT))
import notify  # noqa: E402
import live    # noqa: E402

REAL_SEND = notify.send

passed = total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    passed += bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


# ---------------------------------------------------------------- notify.send with a mocked HTTP call
posts = []


def fake_post(url, **kw):
    posts.append({"url": url, **kw})
    return NS(status_code=200, text='{"id": "abc"}')


notify.requests = NS(post=fake_post)
os.environ.pop("RESEND_API_KEY", None)
os.environ.pop("NOTIFY_TO", None)
result = notify.send("subject", "text")
check("not configured: nothing is sent, status says so", result["status"] == notify.NOT_CONFIGURED and not posts)

os.environ["RESEND_API_KEY"], os.environ["NOTIFY_TO"] = "re_test", "owner@example.com"
result = notify.send("[Nabd] test", "hello")
sent = posts[-1] if posts else {}
check("configured: one POST to Resend, from onboarding@resend.dev to NOTIFY_TO, with the key and a short timeout",
      result["status"] == notify.SENT and sent.get("url") == "https://api.resend.com/emails"
      and sent["json"]["from"] == "Nabd <onboarding@resend.dev>" and sent["json"]["to"] == ["owner@example.com"]
      and sent["json"]["subject"] == "[Nabd] test" and sent["headers"]["Authorization"] == "Bearer re_test"
      and sent["timeout"] <= 10, str(sent))

notify.requests = NS(post=lambda url, **kw: NS(status_code=403, text="domain not verified"))
result = notify.send("s", "t")
check("Resend answers an error: status 'send failed' with the reason, no exception",
      result["status"] == notify.FAILED and "403" in result["detail"], str(result))


def raise_timeout(url, **kw):
    raise TimeoutError("timed out")


notify.requests = NS(post=raise_timeout)
result = notify.send("s", "t")
check("network error or timeout: status 'send failed', no exception", result["status"] == notify.FAILED, str(result))

# ---------------------------------------------------------------- live mode: when notifications go out
tmp = Path(tempfile.mkdtemp())
live.NOTIFY_LOG = tmp / "notifications.jsonl"
outbox = []


def recording_send(subject, text):
    outbox.append((subject, text))
    return {"status": notify.SENT, "detail": "to owner@example.com"}


live.notify.send = recording_send
MONEY = {"count": 3, "by_currency": {"SAR": 300.0}, "total_sar": 300.0}
NONE = {"count": 0, "by_currency": {}, "total_sar": 0}
IMPACT = {"unpaid_orders": MONEY, "queued_orders": NONE, "ledger_overstatement": NONE}
RECORD = {"diagnosis": {"recovered": True, "escalate": False, "summary": "Reverted the provider URL.",
                        "money_impact_before": IMPACT, "money_impact_peak": IMPACT,
                        "money_recovered": {"orders_paid": MONEY, "ledger_corrected": NONE},
                        "money_impact_after": {**IMPACT, "unpaid_orders": NONE}}}


def fake_agent(alert, approve, on_event):
    on_event("thinking", {"text": "The provider URL changed to payments-v2."})
    on_event("tool_call", {"tool": "set_config", "args": {"key": "payment_provider_url", "value": "internal://payments-v1"}})
    approve("set_config", {"key": "payment_provider_url", "value": "internal://payments-v1"})
    return NS(), RECORD


live.reset_service = lambda: "started (fake)"
live.start_traffic = lambda: NS(kill=lambda: None)
live.inject = lambda fault: None
live.run_agent = fake_agent
live.save_run = lambda record, fault: tmp / "run.json"
live.monitor = NS(snapshot=lambda: {"health_ok": True}, evaluate=lambda prev, cur: [NS(rule="payment_gap")],
                  build_alert=lambda found, cur: found, format_alert=lambda found: "Health check: passing\nALERT",
                  check_once=lambda gap=3: [])
live.WARMUP_S, live.POLL_S = 0, 0.01


def wait(condition, timeout: float = 10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def finish(run) -> None:
    """Approve every fix as it comes, then wait for the run and its notification threads."""
    while not run.view()["done"]:
        pending = run.view()["pending"]
        if pending:
            run.answer(pending["id"], True)
        time.sleep(0.02)
    for thread in list(run.notify_threads):
        thread.join(5)


run = live.LiveRun("payment_drop")
run.thread.start()
check("approval needed: a notification goes out while the fix waits",
      wait(lambda: run.view()["pending"] is not None and len(outbox) == 1))
subject, text = outbox[0] if outbox else ("", "")
check("approval email: names the tool + args and quotes Nabd's reasoning",
      "set_config" in subject and "key=payment_provider_url, value=internal://payments-v1" in text
      and "The provider URL changed to payments-v2." in text, subject + " / " + text)
finish(run)
subject, text = outbox[-1] if len(outbox) == 2 else ("", "")
check("incident ended: a second notification with outcome, summary and the money figures from the record",
      len(outbox) == 2 and "Recovered" in subject and "Reverted the provider URL." in text
      and "SAR at risk (just before the first fix): 300.00 SAR" in text and "SAR recovered: 300.00 SAR" in text
      and "Still at risk at the end: 0.00 SAR" in text, subject + " / " + text)
notes = [e for e in run.view()["events"] if e["kind"] == "notification"]
check("both notifications appear in the event stream with their status",
      [e["what"] for e in notes] == ["approval needed", "incident ended"] and all(e["status"] == notify.SENT for e in notes))
logged = [json.loads(line) for line in live.NOTIFY_LOG.read_text(encoding="utf-8").splitlines()]
check("both are logged to live_runs/notifications.jsonl", [r["what"] for r in logged] == ["approval needed", "incident ended"])

# not configured: shown + logged, labelled, run unaffected (the path a judge without a Resend key takes)
live.notify.send = REAL_SEND
os.environ.pop("RESEND_API_KEY", None)
live.NOTIFY_LOG = tmp / "not_configured.jsonl"
run = live.LiveRun("payment_drop")
run.thread.start()
finish(run)
notes = [e for e in run.view()["events"] if e["kind"] == "notification"]
logged = [json.loads(line) for line in live.NOTIFY_LOG.read_text(encoding="utf-8").splitlines()]
check("not configured: both notifications still shown and logged, labelled 'email not configured'",
      len(notes) == 2 and all(e["status"] == "email not configured" for e in notes)
      and len(logged) == 2 and all(r["status"] == "email not configured" for r in logged))
check("not configured: the run finishes normally", run.view()["record"] is RECORD and not run.view()["error"])

# a send that hangs or crashes must never break or block the run
def slow_crashing_send(subject, text):
    time.sleep(2)
    raise RuntimeError("Resend is down")


live.notify.send = slow_crashing_send
run = live.LiveRun("payment_drop")
started = time.time()
run.thread.start()
check("a slow send doesn't hold up the approval prompt", wait(lambda: run.view()["pending"] is not None, timeout=1.5))
while not run.view()["done"]:
    pending = run.view()["pending"]
    if pending:
        run.answer(pending["id"], True)
    time.sleep(0.02)
check("a send that hangs then crashes: the run still finishes, quickly and without an error",
      time.time() - started < 2 and run.view()["record"] is RECORD and not run.view()["error"],
      f"{time.time() - started:.1f}s, error={run.view()['error']}")


def broken_record_email(*args):
    raise KeyError("diagnosis")


live.notify.send = recording_send
real_incident_email, live.incident_email = live.incident_email, broken_record_email
run = live.LiveRun("payment_drop")
run.thread.start()
finish(run)
live.incident_email = real_incident_email
check("a bug while building the email text doesn't break the run either",
      run.view()["record"] is RECORD and not run.view()["error"] and run.view()["saved"] is not None)

print(f"\n{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
