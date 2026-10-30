"""Regression checks for the 4 serious step 1-2 bugs (E, F, A, D).
Starts and resets the service on port 8000. Run from the project root:  python tests/check_serious_fixes.py
"""
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil
import requests

ROOT = Path(__file__).resolve().parents[1]  # the project root (this file is in tests/)
sys.path.insert(0, str(ROOT))
import chaos  # noqa: E402

PY = sys.executable
URL = "http://127.0.0.1:8000"
DB = ROOT / "service" / "data" / "nabd.db"
PID = ROOT / "service" / "data" / "service.pid"
LOGS = ROOT / "service" / "logs" / "app.jsonl"
CFG = ROOT / "service" / "config.yaml"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def sh(*args):
    r = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def bm():
    return requests.get(f"{URL}/business-metrics", timeout=3).json()


def logs():
    return LOGS.read_text(encoding="utf-8") if LOGS.exists() else ""


print("\n--- E. stale PID file must never kill an unrelated process")
sh("supervisor.py", "stop")
victim = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
PID.write_text(str(victim.pid))
_, status = sh("supervisor.py", "status")
_, stop = sh("supervisor.py", "stop")
time.sleep(1)
check("status ignores a PID that isn't our server", status == "not running", status)
check("stop does NOT kill the unrelated process", victim.poll() is None, stop)
victim.kill()
PID.write_text("not-a-number")
check("garbage PID file -> not running, no crash", sh("supervisor.py", "status")[1] == "not running")
code, out = sh("supervisor.py", "start")
check("normal start still works", code == 0 and out.startswith("started"), out)
check("...and status recognises our own server", sh("supervisor.py", "status")[1].startswith("running"))
_, out = sh("supervisor.py", "stop")
check("...and stop still stops it", out.startswith("stopped") and sh("supervisor.py", "status")[1] == "not running", out)

print("\n--- F. port already taken by a server supervisor didn't start")
squatter = subprocess.Popen([PY, "-m", "uvicorn", "service.app:app", "--port", "8000"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(4)
code, out = sh("supervisor.py", "start")
check("start refuses instead of claiming success", out.startswith("NOT started") and not PID.exists(), out)
_, out = sh("supervisor.py", "stop")
check("stop explains the port is held by someone else", "used by another process" in out, out)
for p in psutil.Process(squatter.pid).children(recursive=True) + [psutil.Process(squatter.pid)]:
    p.kill()
time.sleep(1)
code, out = sh("supervisor.py", "start")
check("once the port is free, start works again", out.startswith("started"), out)

print("\n--- A. worker must survive a DB write error and log it honestly")
sh("chaos.py", "reset")
requests.post(f"{URL}/admin/chaos", json={"fault": "worker_hang"})
for _ in range(5):
    requests.post(f"{URL}/orders", json={"amount": 10, "currency": "SAR"})
os.chmod(DB, stat.S_IREAD)
requests.post(f"{URL}/admin/restart-worker")  # worker wakes up into a read-only DB
time.sleep(6)
age = bm()["worker_heartbeat_age_s"]
check("worker thread still alive after DB errors (heartbeat fresh)", age < 3, f"heartbeat {age}s")
check("DB error logged at ERROR with the real cause",
      '"level": "ERROR", "component": "worker", "msg": "job failed: attempt to write a readonly database"' in logs())
check("no misleading 'provider call failed' for a DB error",
      "provider call failed: attempt to write a readonly database" not in logs())
os.chmod(DB, stat.S_IREAD | stat.S_IWRITE)
for _ in range(3):
    requests.post(f"{URL}/orders", json={"amount": 10, "currency": "SAR"})
time.sleep(2)
b = bm()
check("same worker processes new orders once the DB is writable again",
      b["queue_depth"] == 0 and b["orders_without_payment"] == 0 and b["payments_recorded"] >= 3,
      f"payments {b['payments_recorded']}, queue {b['queue_depth']}")
chaos.payment_drop()
time.sleep(3)
requests.post(f"{URL}/orders", json={"amount": 10, "currency": "SAR"})
time.sleep(1)
check("...payment_drop still logs only at DEBUG and leaves the order unpaid",
      bm()["orders_without_payment"] == 1 and '"level": "DEBUG", "component": "payment", "msg": "provider call failed' in logs())

print("\n--- D. empty / half-written config must not be applied")
sh("chaos.py", "reset")
good = CFG.read_text(encoding="utf-8")
CFG.write_text("", encoding="utf-8")
time.sleep(3)
requests.post(f"{URL}/orders", json={"amount": 10, "currency": "SAR"})
time.sleep(1)
check("empty config ignored: orders still get paid", bm()["orders_without_payment"] == 0)
CFG.write_text("version: v1.3\npayment_provider_url: internal://", encoding="utf-8")  # cut off mid-write
time.sleep(3)
requests.post(f"{URL}/orders", json={"amount": 10, "currency": "SAR"})
time.sleep(1)
check("half-written config ignored: orders still get paid", bm()["orders_without_payment"] == 0)
n_err = logs().count("invalid config ignored")
time.sleep(5)
check("invalid config logged once per bad version, not every 2s",
      n_err == 2 and logs().count("invalid config ignored") == 2, f"{logs().count('invalid config ignored')} ERROR lines")
CFG.write_text(good, encoding="utf-8")
time.sleep(3)
chaos.payment_drop()
time.sleep(3)
requests.post(f"{URL}/orders", json={"amount": 10, "currency": "SAR"})
time.sleep(1)
check("a valid config change after a bad one is still applied", bm()["orders_without_payment"] == 1)

sh("chaos.py", "reset")
load = subprocess.Popen([PY, "chaos.py", "load", "30", "10"], cwd=ROOT, stdout=subprocess.DEVNULL)
errors = 0
for i in range(150):  # hammer: rewrite config while the reloader polls and traffic flows
    try:
        chaos.set_config("sync_interval_s", 5 + (i % 2))
    except Exception:
        errors += 1
    time.sleep(0.02)
time.sleep(4)
load.kill()
check("150 rapid set_config writes: none failed", errors == 0, f"{errors} write errors")
check("...no invalid config ever seen by the service", "invalid config ignored" not in logs())
check("...no order lost to a bad read", bm()["orders_without_payment"] == 0)
check("no leftover temp file", not (ROOT / "service" / "config.yaml.tmp").exists())

sh("chaos.py", "reset")
sh("supervisor.py", "stop")
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
