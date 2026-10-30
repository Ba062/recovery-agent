"""Checks for build step 3 (monitor.py): pure rule tests, then live scenario tests.
Starts and resets the service on port 8000. Run from the project root:  python tests/check_monitor.py
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]  # the project root (this file is in tests/)
sys.path.insert(0, str(ROOT))
import chaos  # noqa: E402
import monitor  # noqa: E402

PY = sys.executable
URL = "http://127.0.0.1:8000"
results = []


def wait_applied(key, value, timeout=10):
    """Wait until the service confirms it uses the new config value (no fixed sleeps)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(f"{URL}/config", timeout=3).json().get(key) == value:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"config {key}={value} not applied")


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def rules(anoms):
    return [a.rule for a in anoms]


# ---------------------------------------------------------------- pure rule tests (no service)
print("\n--- rules on fake snapshots")
HEALTHY_B = {"orders_created": 100, "payments_recorded": 100, "orders_without_payment": 0,
             "integrity_failures": 0, "last_sync_ts": 0, "enqueued": 100, "processed": 100,
             "dropped": 0, "queue_depth": 0, "worker_heartbeat_age_s": 0.5}
HEALTHY_M = {"error_rate": 0.0, "p95_latency_ms": 15, "memory_mb": 55, "request_count": 100}


def snap(t=0.0, health=True, **b):
    return {"ts": t, "health_ok": health, "metrics": dict(HEALTHY_M), "business": {**HEALTHY_B, **b}}


check("healthy -> nothing", monitor.evaluate(snap(), snap(3)) == [])
check("unpaid orders -> payment_gap (business)",
      [(a.rule, a.kind) for a in monitor.evaluate(snap(), snap(3, orders_without_payment=7))] == [("payment_gap", "business")])
check("dropped counter rising -> messages_dropped", rules(monitor.evaluate(snap(dropped=2), snap(3, dropped=9))) == ["messages_dropped"])
check("dropped counter flat (old drops) -> no alarm", monitor.evaluate(snap(dropped=9), snap(3, dropped=9)) == [])
check("queue deep but shrinking -> no backlog alarm", monitor.evaluate(snap(queue_depth=80), snap(3, queue_depth=40)) == [])
check("queue deep and growing -> queue_backlog", rules(monitor.evaluate(snap(queue_depth=30), snap(3, queue_depth=60))) == ["queue_backlog"])
check("ledger mismatch -> ledger_mismatch", rules(monitor.evaluate(snap(), snap(3, integrity_failures=4))) == ["ledger_mismatch"])
cur = snap(3)
cur["metrics"]["error_rate"] = 0.4
check("high error rate -> technical rule", [(a.rule, a.kind) for a in monitor.evaluate(snap(), cur)] == [("high_error_rate", "technical")])
check("health failing -> service_down only", rules(monitor.evaluate(snap(), snap(3, health=False))) == ["service_down"])
down = {"ts": 3, "health_ok": False, "metrics": None, "business": None}
check("unreachable -> service_down", rules(monitor.evaluate(snap(), down)) == ["service_down"])
prev_failed = {"ts": 0, "health_ok": False, "metrics": None, "business": None}
check("previous reading failed -> no crash", monitor.evaluate(prev_failed, snap(3, orders_without_payment=1)) != [])
many = monitor.evaluate(snap(queue_depth=30), snap(3, queue_depth=90, worker_heartbeat_age_s=30, orders_without_payment=5))
check("several at once -> sorted, data loss first", rules(many) == ["payment_gap", "worker_stalled", "queue_backlog"], str(rules(many)))
text = monitor.format_alert(monitor.build_alert(many, snap(3)))
check("alert text lists every anomaly", all(r in text for r in rules(many)) and "Health check: passing" in text)

print("\n--- edge cases (bug fixes)")
broken = {"ts": 3, "health_ok": True, "metrics": dict(HEALTHY_M), "business": None}
found = monitor.evaluate(snap(), broken)
check("BUG 1: business-metrics failing while health passes -> service_down, no crash",
      rules(found) == ["service_down"] and "business-metrics" in found[0].message, found[0].message)
broken = {"ts": 3, "health_ok": True, "metrics": None, "business": dict(HEALTHY_B)}
check("metrics failing while health passes -> service_down, no crash", rules(monitor.evaluate(snap(), broken)) == ["service_down"])
check("BUG 2: counters reset by restart (dropped 9 -> 3) -> 3 new drops detected",
      [(a.rule, a.value) for a in monitor.evaluate(snap(dropped=9, enqueued=100), snap(3, dropped=3, enqueued=20))]
      == [("messages_dropped", 3)])
check("restart with no new drops -> no alarm", monitor.evaluate(snap(dropped=9, enqueued=100), snap(3, dropped=0, enqueued=20)) == [])
check("restart with deep queue -> counts as growing",
      "queue_backlog" in rules(monitor.evaluate(snap(queue_depth=90, enqueued=100), snap(3, queue_depth=50, enqueued=50))))
at = snap(3, worker_heartbeat_age_s=15, queue_depth=20, orders_without_payment=0)
at["metrics"].update(error_rate=0.05, p95_latency_ms=500)
check("exactly AT every threshold -> no alarm", monitor.evaluate(snap(queue_depth=5), at) == [])
above = snap(3, worker_heartbeat_age_s=15.1, queue_depth=21)
above["metrics"].update(error_rate=0.051, p95_latency_ms=501)
check("just ABOVE every threshold -> all four fire",
      set(rules(monitor.evaluate(snap(queue_depth=5), above))) == {"worker_stalled", "queue_backlog", "high_error_rate", "high_latency"},
      str(rules(monitor.evaluate(snap(queue_depth=5), above))))
hl = snap(3)
hl["metrics"]["p95_latency_ms"] = 900
check("high latency -> high_latency (technical)", [(a.rule, a.kind) for a in monitor.evaluate(snap(), hl)] == [("high_latency", "technical")])

# polling loop survives an exception inside evaluate()
real_evaluate, real_snapshot, calls = monitor.evaluate, monitor.snapshot, {"n": 0}
def flaky_evaluate(prev, cur):
    calls["n"] += 1
    if calls["n"] == 1:
        raise RuntimeError("boom")
    return [monitor.Anomaly("payment_gap", "business", 1, 0, "x")]
monitor.evaluate, monitor.snapshot = flaky_evaluate, lambda: snap(time.time())
loop_alerts = []
threading.Thread(target=monitor.run, kwargs={"on_alert": loop_alerts.append, "poll_seconds": 0.2}, daemon=True).start()
time.sleep(1.5)
monitor.evaluate, monitor.snapshot = real_evaluate, real_snapshot
check("polling loop survives an error in one reading and keeps alerting", calls["n"] >= 2 and len(loop_alerts) == 1,
      f"{calls['n']} evaluations, {len(loop_alerts)} alert")
time.sleep(0.5)  # let that thread see the real functions; it keeps running harmlessly as a daemon

# ---------------------------------------------------------------- live
def start_load(seconds=300):
    return subprocess.Popen([PY, "chaos.py", "load", str(seconds), "5"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fix_url():
    chaos.set_config("payment_provider_url", "internal://payments-v1")
    wait_applied("payment_provider_url", "internal://payments-v1")


def fresh():
    subprocess.run([PY, "chaos.py", "reset"], cwd=ROOT, capture_output=True)
    return start_load()


print("\n--- live: healthy")
traffic = fresh()
time.sleep(4)
found = monitor.check_once()
check("healthy with traffic -> all green", found == [], str(rules(found)))

print("\n--- live: payment_drop")
chaos.payment_drop()
time.sleep(6)
found = monitor.check_once()
check("payment_drop -> payment_gap", "payment_gap" in rules(found), str(rules(found)))
check("...and NO technical rule fires (silent failure)", all(a.kind == "business" for a in found))
fix_url()
requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10)
found = monitor.check_once()
check("after fix + reprocess -> green again", found == [], str(rules(found)))
traffic.kill()

print("\n--- live: worker_hang")
traffic = fresh()
time.sleep(2)
chaos.worker_hang()
time.sleep(17)
found = monitor.check_once()
check("worker_hang -> worker_stalled + queue_backlog", {"worker_stalled", "queue_backlog"} <= set(rules(found)), str(rules(found)))
check("...no payment_gap false alarm", "payment_gap" not in rules(found))
requests.post(f"{URL}/admin/restart-worker", timeout=3)
time.sleep(3)
found = monitor.check_once()
check("after restart-worker -> green again", found == [], str(rules(found)))
traffic.kill()

print("\n--- live: compound")
traffic = fresh()
time.sleep(2)
chaos.compound()
time.sleep(17)
found = monitor.check_once()
check("compound -> payment_gap AND worker_stalled", {"payment_gap", "worker_stalled"} <= set(rules(found)), str(rules(found)))
check("...payment_gap ranked first (data loss)", rules(found)[0] == "payment_gap")
fix_url()
requests.post(f"{URL}/admin/restart-worker", timeout=3)
time.sleep(3)
requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10)
found = monitor.check_once()
check("after both fixes -> green", found == [], str(rules(found)))
traffic.kill()

print("\n--- live: unknown")
traffic = fresh()
time.sleep(2)
chaos.unknown()
time.sleep(4)
found = monitor.check_once()
check("unknown -> high_error_rate (technical)", "high_error_rate" in rules(found), str(rules(found)))
traffic.kill()

print("\n--- live: idle (no traffic at all)")
subprocess.run([PY, "chaos.py", "reset"], cwd=ROOT, capture_output=True)
time.sleep(5)
found = monitor.check_once()
check("idle healthy service -> all green (worker heartbeats while idle)", found == [], str(rules(found)))

print("\n--- live: /business-metrics returns 500 while /health passes")
import sqlite3  # noqa: E402
conn = sqlite3.connect(ROOT / "service" / "data" / "nabd.db")
conn.execute("DROP TABLE ledger")  # /business-metrics now fails; /health (SELECT 1) still passes
conn.commit()
conn.close()  # `with sqlite3.connect()` does NOT close; an open handle blocks reset on Windows
r = requests.get(f"{URL}/business-metrics", timeout=3)
found = monitor.check_once(1)
check("health 200 + business-metrics 500 -> service_down, no crash",
      requests.get(f"{URL}/health").status_code == 200 and r.status_code == 500 and rules(found) == ["service_down"],
      f"business-metrics HTTP {r.status_code}, rules {rules(found)}")

print("\n--- live: messages_dropped, including across a service restart")
traffic = fresh()
time.sleep(2)
chaos.set_config("queue_max", 0)  # every new order is dropped
time.sleep(3)
found = monitor.check_once()
check("queue_max 0 -> messages_dropped", "messages_dropped" in rules(found), str(rules(found)))
before = monitor.snapshot()
subprocess.run([PY, "supervisor.py", "restart"], cwd=ROOT, capture_output=True)
time.sleep(3)
after = monitor.snapshot()
found = monitor.evaluate(before, after)
check("drops still detected right after restart (counters reset)",
      "messages_dropped" in rules(found) and after["business"]["dropped"] < before["business"]["dropped"],
      f"dropped {before['business']['dropped']} -> {after['business']['dropped']}, rules {rules(found)}")
traffic.kill()

print("\n--- live: data_corruption")
traffic = fresh()
time.sleep(2)
chaos.data_corruption()
time.sleep(12)  # hot reload + at least one sync run
found = monitor.check_once()
check("data_corruption (sync v1.4) -> ledger_mismatch", "ledger_mismatch" in rules(found), str(rules(found)))
check("...health green and no technical rule fires (silent failure)",
      requests.get(f"{URL}/health").status_code == 200 and all(a.kind == "business" for a in found), str(rules(found)))
chaos.set_config("version", "v1.3")
wait_applied("version", "v1.3")
requests.post(f"{URL}/admin/repair-ledger", json={"order_ids": "all_flagged"}, timeout=10)
time.sleep(7)  # at least one more sync run under v1.3
found = monitor.check_once()
check("after rollback + repair -> green, stays green on new syncs", found == [], str(rules(found)))
traffic.kill()

print("\n--- live: service down")
subprocess.run([PY, "supervisor.py", "stop"], cwd=ROOT, capture_output=True)
found = monitor.check_once(1)
check("service stopped -> service_down", rules(found) == ["service_down"], str(rules(found)))

print("\n--- live: polling loop fires ONE alert per incident")
traffic = fresh()
alerts = []
threading.Thread(target=monitor.run, kwargs={"on_alert": alerts.append, "poll_seconds": 1}, daemon=True).start()
time.sleep(4)
check("no alert while healthy", alerts == [])
chaos.payment_drop()
time.sleep(10)
check("exactly one alert during the incident", len(alerts) == 1, f"{len(alerts)} alerts")
check("alert bundle has anomalies + both metric sets",
      bool(alerts) and alerts[0]["anomalies"][0]["rule"] == "payment_gap" and alerts[0]["business_metrics"] is not None)
fix_url()
requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10)
time.sleep(4)
chaos.payment_drop()
time.sleep(8)
check("a NEW incident after recovery fires a second alert", len(alerts) == 2, f"{len(alerts)} alerts")
traffic.kill()

subprocess.run([PY, "chaos.py", "reset"], cwd=ROOT, capture_output=True)
subprocess.run([PY, "supervisor.py", "stop"], cwd=ROOT, capture_output=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
