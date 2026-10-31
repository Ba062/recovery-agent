"""Regression checks for the medium/minor step 1-2 fixes (B, C, G, H, I).
Starts and resets the service on port 8000. Run from the project root:  python tests/check_minor_fixes.py
"""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil
import requests

ROOT = Path(__file__).resolve().parents[1]  # the project root (this file is in tests/)
sys.path.insert(0, str(ROOT))
import chaos  # noqa: E402
from service.pipeline import validate_config  # noqa: E402

PY = sys.executable
URL = "http://127.0.0.1:8000"
DB = ROOT / "service" / "data" / "nabd.db"
CFG = ROOT / "service" / "config.yaml"
LOGS = ROOT / "service" / "logs" / "app.jsonl"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def sh(*args):
    r = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, timeout=90)
    return r.returncode, (r.stdout + r.stderr).strip()


def bm():
    return requests.get(f"{URL}/business-metrics", timeout=3).json()


def post(path, body):
    return requests.post(f"{URL}{path}", json=body, timeout=10)


GOOD = {"version": "v1.3", "payment_provider_url": "internal://payments-v1", "queue_max": 1000,
        "worker_count": 1, "sync_interval_s": 5}

print("\n--- C. validate_config (pure)")
check("good config -> valid", validate_config(GOOD) == [])
for key, val in [("queue_max", "abc"), ("queue_max", -1), ("queue_max", True), ("queue_max", 2.5),
                 ("worker_count", 0), ("worker_count", 99), ("worker_count", "2"),
                 ("sync_interval_s", -1), ("sync_interval_s", 0), ("sync_interval_s", "5"),
                 ("version", 1.4), ("payment_provider_url", None)]:
    check(f"{key}={val!r} -> rejected", validate_config({**GOOD, key: val}) != [], str(validate_config({**GOOD, key: val})))
for key, val in [("queue_max", 0), ("queue_max", 5), ("payment_provider_url", "internal://payments-v2"),
                 ("version", "v1.4"), ("sync_interval_s", 0.5), ("worker_count", 8)]:
    check(f"fault-like but meaningful {key}={val!r} -> still allowed", validate_config({**GOOD, key: val}) == [])
check("missing key -> rejected (D still covered)", validate_config({k: v for k, v in GOOD.items() if k != "version"}) != [])
check("empty -> rejected (D still covered)", validate_config({}) != [])

print("\n--- C. live: invalid values never reach the running service")
sh("chaos.py", "reset")
try:
    chaos.set_config("queue_max", "abc")
    rejected = False
except ValueError as e:
    rejected, msg = True, str(e)
check("set_config refuses an invalid value before writing", rejected and CFG.read_text().count("abc") == 0, msg if rejected else "")
for key, val in [("queue_max", "abc"), ("worker_count", 0), ("sync_interval_s", -1)]:
    text = CFG.read_text(encoding="utf-8")
    CFG.write_text(text.replace(f"{key}: {GOOD[key]}", f"{key}: {val}"), encoding="utf-8")  # hand-edit
    time.sleep(3)
    codes = [post("/orders", {"amount": 10}).status_code for _ in range(3)]
    requests.post(f"{URL}/admin/restart-worker")
    post("/orders", {"amount": 10})
    time.sleep(2)
    b = bm()
    check(f"hand-edited {key}: {val} ignored -> orders OK, worker alive",
          codes == [200, 200, 200] and b["queue_depth"] == 0 and b["worker_heartbeat_age_s"] < 3,
          f"codes {codes}, queue {b['queue_depth']}, heartbeat {b['worker_heartbeat_age_s']}s")
    CFG.write_text(text, encoding="utf-8")
    time.sleep(3)
logs = LOGS.read_text(encoding="utf-8")
# >= 3: a hand edit is a non-atomic write, so the reloader may also (correctly) catch it half-written
check("each invalid edit logged as ERROR with the reason",
      logs.count("invalid config ignored") >= 3 and all(s in logs for s in ["queue_max must be", "worker_count must be", "sync_interval_s must be"]))
time.sleep(7)
check("sync thread still alive (ledger keeps syncing)", bm()["sync_last_run_age_s"] < 8)

print("\n--- B. each admin endpoint accepts only its own keyword")
chaos.payment_drop()
time.sleep(3)
for _ in range(3):
    post("/orders", {"amount": 10})
time.sleep(1)
chaos.set_config("payment_provider_url", "internal://payments-v1")
time.sleep(3)
r = post("/admin/reprocess", {"order_ids": "all_flagged"})
check("/admin/reprocess 'all_flagged' -> 422", r.status_code == 422, f"HTTP {r.status_code}")
r = post("/admin/repair-ledger", {"order_ids": "all_unpaid"})
check("/admin/repair-ledger 'all_unpaid' -> 422", r.status_code == 422, f"HTTP {r.status_code}")
r = post("/admin/reprocess", {"order_ids": "all_unpaid"})
check("/admin/reprocess 'all_unpaid' still works", r.status_code == 200 and r.json()["paid"] == 3, r.text)
r = post("/admin/repair-ledger", {"order_ids": "all_flagged"})
check("/admin/repair-ledger 'all_flagged' still works", r.status_code == 200, r.text)
r = post("/admin/reprocess", {"order_ids": [1, 2]})
check("explicit id lists still work", r.status_code == 200, r.text)

print("\n--- I. order amounts")
for amount, want in [("1e309", 422), (float("1e7"), 422), (1_000_000, 200), (0.01, 200)]:
    r = post("/orders", {"amount": amount, "currency": "SAR"})
    check(f"amount {amount!r} -> {want}", r.status_code == want, f"HTTP {r.status_code}")
r = requests.post(f"{URL}/orders", data='{"amount": NaN}', headers={"content-type": "application/json"})
check("amount NaN -> 422", r.status_code == 422, f"HTTP {r.status_code}")

print("\n--- H. clear errors, no tracebacks")
code, out = sh("chaos.py", "load", "1", "0")
check("load rate 0 -> clear message", code != 0 and "greater than 0" in out and "Traceback" not in out, out)
code, out = sh("chaos.py", "load", "abc")
check("load with non-number -> usage", code != 0 and "usage" in out and "Traceback" not in out, out)
sh("supervisor.py", "stop")
code, out = sh("chaos.py", "worker_hang")
check("worker_hang with service down -> clear message", code != 0 and "not running" in out and "Traceback" not in out, out)
before = CFG.read_text(encoding="utf-8")
code, out = sh("chaos.py", "compound")
check("compound with service down -> refuses, injects NOTHING",
      code != 0 and CFG.read_text(encoding="utf-8") == before and "Traceback" not in out, out)
chaos.delete_file(DB)
code, out = sh("chaos.py", "unknown")
check("unknown with no DB -> clear message", code != 0 and "no database yet" in out and "Traceback" not in out, out)

print("\n--- G. reset when the DB is open in another program")
sh("chaos.py", "reset")
holder = sqlite3.connect(DB)
holder.execute("SELECT 1").fetchone()
code, out = sh("chaos.py", "reset")
if os.name == "nt":  # Windows refuses to delete an open file, so reset must say so
    check("reset reports the locked DB clearly (exit 1, no traceback)",
          code != 0 and "open in another program" in out and "Traceback" not in out, out.splitlines()[-1])
else:  # Linux/macOS delete an open file without complaint, so there is nothing to report
    check("reset works even with the DB open (not Windows: open files can be deleted)",
          code == 0 and "DB and logs wiped" in out and "Traceback" not in out, out.splitlines()[-1])
check("...and the service is NOT left stopped", sh("supervisor.py", "status")[1].startswith("running"))
holder.close()
code, out = sh("chaos.py", "reset")
check("after closing it, reset works again", code == 0 and "DB and logs wiped" in out, out)

print("\n--- G + F together: unmanaged server holds port AND DB")
sh("supervisor.py", "stop")
squatter = subprocess.Popen([PY, "-m", "uvicorn", "service.app:app", "--port", "8000"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(4)
requests.get(f"{URL}/business-metrics")  # make it touch the DB
code, out = sh("chaos.py", "reset")
check("reset fails loudly and explains (no traceback)",
      code != 0 and "Traceback" not in out and ("NOT started" in out or "open in another program" in out), out.splitlines()[-1])
for p in psutil.Process(squatter.pid).children(recursive=True) + [psutil.Process(squatter.pid)]:
    p.kill()
time.sleep(1)
code, out = sh("chaos.py", "reset")
check("after the squatter is gone, reset works", code == 0, out)

sh("chaos.py", "reset")
sh("supervisor.py", "stop")
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
