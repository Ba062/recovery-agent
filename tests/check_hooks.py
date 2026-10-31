"""Checks of the fault hooks added for the held-out eval (eval/HELDOUT.md, "Disclosure"):
each one does what it should when triggered, and is gone after a reset. No agent, no API key.
Starts and resets the service on port 8000, and leaves it stopped.
Run from the project root:  python tests/check_hooks.py
"""

import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import chaos       # noqa: E402
import supervisor  # noqa: E402
from service.jsonlog import LOG_FILE  # noqa: E402
from service import app as svc  # noqa: E402  (only its metrics code; the real service runs separately)

URL = chaos.SERVICE_URL
passed = total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    passed += bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


def order() -> requests.Response:
    return requests.post(f"{URL}/orders", json={"amount": 100, "currency": "SAR"}, timeout=3)


def business() -> dict:
    return requests.get(f"{URL}/business-metrics", timeout=3).json()


def logs(text: str) -> list[dict]:
    lines = [json.loads(line) for line in LOG_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in lines if text in r["msg"]]


def wait_reload(key: str, value) -> None:
    for _ in range(20):
        if requests.get(f"{URL}/config", timeout=3).json().get(key) == value:
            return
        time.sleep(0.5)
    raise RuntimeError(f"config {key}={value} not applied")


chaos.reset()

# v1.5: POST /orders fails with 500, logged at ERROR; rollback to v1.3 fixes it
chaos.set_config("version", "v1.5")
wait_reload("version", "v1.5")
codes = [order().status_code for _ in range(3)]
check("v1.5: POST /orders returns 500", codes == [500, 500, 500], str(codes))
errors = logs("order validation failed")
check("v1.5: exception logged at ERROR", errors and all(r["level"] == "ERROR" for r in errors), str(errors[:1]))
check("v1.5: no order was created", business()["orders_created"] == 0)
for _ in range(30):  # 33 failed requests: a 500-request window would need ~630 good ones to drop under 5%
    order()
chaos.set_config("version", "v1.3")
wait_reload("version", "v1.3")
check("v1.5 -> v1.3: orders work again", order().status_code == 200)

# v2, review F2: the error rate covers the last 30 s, not the last 500 requests, so it clears after a fix
def error_rate() -> float:
    return requests.get(f"{URL}/metrics", timeout=3).json()["error_rate"]


check("v2 F2: right after the rollback the old 500s still count", error_rate() > 0.05, str(error_rate()))
deadline = time.time() + svc.METRICS_WINDOW_S + 5
while time.time() < deadline and error_rate() > 0.05:
    order()
    time.sleep(0.5)
check(f"v2 F2: error rate back under 5% within {svc.METRICS_WINDOW_S}+5 s (a 500-request window needs ~100 s)",
      error_rate() <= 0.05, str(error_rate()))

# v2, review F2, without the service: requests older than the window don't count
now = time.monotonic()
svc.recent_requests.clear()
svc.recent_requests.extend([(now - 40, 5.0, 500)] * 450 + [(now - 5, 5.0, 200)] * 10)
check("v2 F2: 450 old 500s + 10 recent 200s -> error rate 0", svc.metrics()["error_rate"] == 0.0,
      str(svc.metrics()))
svc.recent_requests.extend([(now - 1, 5.0, 500)] * 10)
check("v2 F2: 10 recent 500s of 20 recent requests -> error rate 0.5", svc.metrics()["error_rate"] == 0.5,
      str(svc.metrics()))
svc.recent_requests.clear()
check("v2 F2: no requests -> error rate 0", svc.metrics()["error_rate"] == 0.0)

# v1.7 (v2 scenario partial_api_errors): 3 in 10 POST /orders return 500, logged at ERROR; rollback fixes it
chaos.reset()
chaos.set_config("version", "v1.7")
wait_reload("version", "v1.7")
codes = [order().status_code for _ in range(20)]
check("v1.7: 6 of 20 POST /orders return 500, the rest 200",
      codes.count(500) == 6 and codes.count(200) == 14, str(codes))
check("v1.7: the failures are logged at ERROR", any(r["level"] == "ERROR" for r in logs("rate limiter misread")))
check("v1.7: error rate about 30% (monitor threshold 5%)", 0.25 <= error_rate() <= 0.35, str(error_rate()))
b = business()
check("v1.7: health still green, accepted orders still paid", requests.get(f"{URL}/health", timeout=3).status_code == 200
      and b["orders_created"] == 14, str(b))
chaos.set_config("version", "v1.3")
wait_reload("version", "v1.3")
check("v1.7 -> v1.3: every order works again", all(order().status_code == 200 for _ in range(10)))

# v1.6: the payment step fails silently (DEBUG), the order is processed unpaid; reprocess fails until rollback
chaos.reset()
chaos.set_config("version", "v1.6")
wait_reload("version", "v1.6")
check("v1.6: POST /orders still accepted", order().status_code == 200)
time.sleep(2)
b = business()
check("v1.6: order processed without payment (silent)", b["orders_without_payment"] == 1, str(b))
debug = logs("provider call failed: Object of type Decimal")
check("v1.6: error only at DEBUG", debug and all(r["level"] == "DEBUG" for r in debug), str(debug[:1]))
r = requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10).json()
check("v1.6: reprocess fails while v1.6 runs", r["paid"] == 0 and r["failed"] == 1, str(r))
chaos.set_config("version", "v1.3")
wait_reload("version", "v1.3")
r = requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10).json()
check("v1.6 -> v1.3: reprocess pays the order", r["paid"] == 1 and business()["orders_without_payment"] == 0, str(r))

# provider_outage: payments time out with the correct URL; only a restart (reset) clears it
chaos.reset()
r = requests.post(f"{URL}/admin/chaos", json={"fault": "provider_outage"}, timeout=3)
check("provider_outage: accepted by /admin/chaos", r.status_code == 200, str(r.status_code))
order()
time.sleep(2)
b = business()
cfg = requests.get(f"{URL}/config", timeout=3).json()
check("provider_outage: order unpaid while the URL is correct",
      b["orders_without_payment"] == 1 and cfg["payment_provider_url"] == "internal://payments-v1", str(b))
timeouts = logs("payment provider timed out at internal://payments-v1")
check("provider_outage: only a DEBUG trace", timeouts and all(r["level"] == "DEBUG" for r in timeouts))
check("provider_outage: health still green", requests.get(f"{URL}/health", timeout=3).status_code == 200)
r = requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10).json()
check("provider_outage: reprocess fails (nothing the agent can fix)", r["paid"] == 0, str(r))

# after a reset every hook is off again
chaos.reset()
order()
time.sleep(2)
b = business()
check("after reset: orders paid normally, nothing unpaid",
      b["orders_created"] == 1 and b["payments_recorded"] == 1 and b["orders_without_payment"] == 0, str(b))
check("unknown fault names still rejected", requests.post(f"{URL}/admin/chaos", json={"fault": "rm_rf"},
                                                          timeout=3).status_code == 422)

chaos.reset()
supervisor.stop()
print(f"\n{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
