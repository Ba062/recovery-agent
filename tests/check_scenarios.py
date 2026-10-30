"""Live checks for build step 2: chaos.py scenarios, reset, /admin/chaos, default config templates.
Starts and resets the service on port 8000. Run from the project root:  python tests/check_scenarios.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]  # the project root (this file is in tests/)
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


def run(*args, timeout=120):
    r = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def bm():
    return requests.get(f"{URL}/business-metrics", timeout=3).json()


def health_ok():
    try:
        return requests.get(f"{URL}/health", timeout=3).json() == {"status": "ok"}
    except requests.RequestException:
        return False


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def load(seconds, rate=5):
    return run("chaos.py", "load", str(seconds), str(rate))[1]


cfg = ROOT / "service" / "config.yaml"
deploys = ROOT / "service" / "deploys.jsonl"
default_cfg = (ROOT / "service" / "config.default.yaml").read_text(encoding="utf-8")
default_deploys = (ROOT / "service" / "deploys.default.jsonl").read_text(encoding="utf-8")

print("\n--- templates + fresh clone")
run("supervisor.py", "stop")
cfg.unlink(missing_ok=True)
deploys.unlink(missing_ok=True)
code, out = run("supervisor.py", "start")
check("start with no live config files (fresh clone) creates them and is healthy",
      cfg.exists() and deploys.exists() and health_ok(), out)
check("live config == template", cfg.read_text(encoding="utf-8") == default_cfg)
ignored = subprocess.run(["git", "check-ignore", "service/config.yaml", "service/deploys.jsonl"],
                         cwd=ROOT, capture_output=True, text=True).stdout.split()
check("live config + deploys are git-ignored", len(ignored) == 2, ", ".join(ignored))
tracked = subprocess.run(["git", "ls-files", "service"], cwd=ROOT, capture_output=True, text=True).stdout
check("templates are not ignored", subprocess.run(
    ["git", "check-ignore", "-q", "service/config.default.yaml"], cwd=ROOT).returncode == 1)

print("\n--- reset")
code, out = run("chaos.py", "reset")
check("reset while running -> stops, wipes, starts healthy", health_ok() and bm()["orders_created"] == 0, out)
run("supervisor.py", "stop")
code, out = run("chaos.py", "reset")
check("reset while stopped -> starts healthy", health_ok() and "not running" in out, out)
code, out = run("chaos.py", "reset")
check("reset twice in a row is fine", code == 0 and health_ok())

print("\n--- payment_drop")
load(3)
run("chaos.py", "payment_drop")
last = json.loads(deploys.read_text(encoding="utf-8").splitlines()[-1])
time.sleep(3)
load(6)
m = bm()
check("health stays green", health_ok())
check("error_rate stays 0", requests.get(f"{URL}/metrics").json()["error_rate"] == 0.0)
check("orders go unpaid", m["orders_without_payment"] > 10, f"{m['orders_without_payment']} of {m['orders_created']}")
check("deploy history records the change", "payment_provider_url" in last["change"] and last["by"] == "ops", last["change"])
code, out = run("chaos.py", "payment_drop")
check("injecting payment_drop twice doesn't crash", code == 0, out)

print("\n--- worker_hang")
run("chaos.py", "reset")
load(2)
run("chaos.py", "worker_hang")
load(17)
m = bm()
check("health stays green", health_ok())
check("queue grows", m["queue_depth"] > 50, f"queue_depth {m['queue_depth']}")
check("heartbeat goes stale (>15s)", m["worker_heartbeat_age_s"] > 15, f"{m['worker_heartbeat_age_s']}s")
check("no unpaid orders (it's a hang, not a payment fault)", m["orders_without_payment"] == 0)
requests.post(f"{URL}/admin/restart-worker", timeout=3)
time.sleep(4)
m = bm()
check("restart-worker drains queue and pays everything",
      m["queue_depth"] == 0 and m["payments_recorded"] == m["orders_created"], f"{m['payments_recorded']}/{m['orders_created']}")
check("heartbeat fresh again", m["worker_heartbeat_age_s"] < 3)

print("\n--- compound, right order (stop data loss first)")
run("chaos.py", "reset")
load(2)
run("chaos.py", "compound")
time.sleep(3)
load(15)
m = bm()
check("both signals visible, health green",
      health_ok() and m["queue_depth"] > 40 and m["worker_heartbeat_age_s"] > 10, f"queue {m['queue_depth']}")
subprocess.run([PY, "-c", "import chaos; chaos.set_config('payment_provider_url','internal://payments-v1')"], cwd=ROOT)
wait_applied('payment_provider_url', 'internal://payments-v1')
requests.post(f"{URL}/admin/restart-worker", timeout=3)
time.sleep(4)
m = bm()
check("URL first, then restart -> everything paid, no reprocess needed",
      m["orders_without_payment"] == 0 and m["payments_recorded"] == m["orders_created"], f"{m['payments_recorded']}/{m['orders_created']}")

print("\n--- compound, wrong order (restart first)")
run("chaos.py", "reset")
load(2)
run("chaos.py", "compound")
time.sleep(3)
load(10)
queued = bm()["queue_depth"]
requests.post(f"{URL}/admin/restart-worker", timeout=3)
time.sleep(4)
m = bm()
check("restart first -> queued orders processed WITHOUT payment", m["orders_without_payment"] >= queued,
      f"{m['orders_without_payment']} unpaid, {queued} were queued")
subprocess.run([PY, "-c", "import chaos; chaos.set_config('payment_provider_url','internal://payments-v1')"], cwd=ROOT)
wait_applied('payment_provider_url', 'internal://payments-v1')
r = requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10).json()
check("...and then need reprocessing to recover", bm()["orders_without_payment"] == 0 and r["paid"] > 0, str(r))

print("\n--- unknown")
run("chaos.py", "reset")
load(2)
run("chaos.py", "unknown")
load(3)
check("health still green", health_ok())
er = requests.get(f"{URL}/metrics").json()["error_rate"]
check("error rate rises", er > 0.3, f"error_rate {er}")
logs = (ROOT / "service" / "logs" / "app.jsonl").read_text(encoding="utf-8")
check("ERROR log names the real cause", "attempt to write a readonly database" in logs)
requests.post(f"{URL}/admin/restart-worker", timeout=3)
out = load(2)
check("restart-worker does not fix it", out.startswith("load done: 0 orders sent"), out)
run("chaos.py", "reset")
out = load(2)
check("reset recovers from read-only DB", " 0 failed" in out, out)

print("\n--- bad input / edge cases")
r = requests.post(f"{URL}/admin/chaos", json={"fault": "rm_rf"}, timeout=3)
check("/admin/chaos rejects unknown fault names", r.status_code == 422, f"HTTP {r.status_code}")
code, out = run("chaos.py", "nonsense")
check("chaos.py with a bad command prints usage", code != 0 and "usage" in out)
run("supervisor.py", "stop")
code, out = run("chaos.py", "worker_hang")
check("worker_hang while service is down fails (doesn't silently pretend)", code != 0)

print("\n--- leave clean")
run("chaos.py", "reset")
run("supervisor.py", "stop")
check("config restored to template", cfg.read_text(encoding="utf-8") == default_cfg)
check("deploy history restored to template", deploys.read_text(encoding="utf-8") == default_deploys)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
