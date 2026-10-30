"""Checks for the money impact figures: what /business-metrics and /data-integrity report must
match the database. The expected values are recomputed here in plain Python from the raw rows
(not with the service's SQL), so a wrong query in db.money_impact() is caught.
Starts and resets the service on port 8000, leaves it stopped. Run from the project root:
    python tests/check_money.py
"""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]  # the project root (this file is in tests/)
sys.path.insert(0, str(ROOT))
import chaos  # noqa: E402
from service import db  # noqa: E402

PY = sys.executable
URL = "http://127.0.0.1:8000"
USD_TO_SAR = 3.75
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def run(*args):
    subprocess.run([PY, *args], cwd=ROOT, capture_output=True)


def load(seconds):
    run("chaos.py", "load", str(seconds), "5")  # blocks until done, then the traffic has stopped


def expected_from_db() -> dict:
    """The three money figures, recomputed from raw rows in Python."""
    conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    try:
        orders = {r[0]: r for r in conn.execute("SELECT id, amount, currency, status FROM orders")}
        paid = {r[0] for r in conn.execute("SELECT order_id FROM payments")}
        ledger = list(conn.execute("SELECT order_id, amount FROM ledger"))
    finally:
        conn.close()  # an open handle would block `chaos.py reset` on Windows
    unpaid = [(o[1], o[2]) for o in orders.values() if o[3] == "processed" and o[0] not in paid]
    queued = [(o[1], o[2]) for o in orders.values() if o[3] == "queued"]
    overstated = [(amount - orders[oid][1], orders[oid][2]) for oid, amount in ledger
                  if abs(amount - orders[oid][1]) > 0.001]
    return {"unpaid_orders": totals(unpaid), "queued_orders": totals(queued), "ledger_overstatement": totals(overstated)}


def totals(pairs) -> dict:
    sar = sum(a for a, cur in pairs if cur == "SAR")
    usd = sum(a for a, cur in pairs if cur == "USD")
    return {"count": len(pairs), "SAR": sar, "USD": usd, "total_sar": sar + usd * USD_TO_SAR}


def matches(reported: dict, expected: dict) -> bool:
    """Same count, same amount per currency and same SAR total (to the cent)."""
    by_cur = reported["by_currency"]
    return (reported["count"] == expected["count"]
            and abs(by_cur.get("SAR", 0) - expected["SAR"]) < 0.01
            and abs(by_cur.get("USD", 0) - expected["USD"]) < 0.01
            and abs(reported["total_sar"] - expected["total_sar"]) < 0.01)


def money() -> dict:
    return requests.get(f"{URL}/business-metrics", timeout=5).json()["money_impact"]


def check_all_match(label: str) -> dict:
    """Compare all three figures with the DB. Returns the reported money impact."""
    reported, expected = money(), expected_from_db()
    for key in ("unpaid_orders", "queued_orders", "ledger_overstatement"):
        check(f"{label}: {key} matches the DB", matches(reported[key], expected[key]),
              f"reported {reported[key]['count']} / {reported[key]['total_sar']} SAR, "
              f"DB {expected[key]['count']} / {expected[key]['total_sar']:.2f} SAR")
    return reported


print("\n--- sum_money (pure)")
check("empty -> zero, no currencies", db.sum_money([]) == {"count": 0, "by_currency": {}, "total_sar": 0.0})
check("USD converted at the 3.75 peg", db.sum_money([{"amount": 10, "currency": "SAR"}, {"amount": 10, "currency": "USD"}])
      == {"count": 2, "by_currency": {"SAR": 10.0, "USD": 10.0}, "total_sar": 47.5})
check("rounded to the cent", db.sum_money([{"amount": 0.1, "currency": "SAR"}] * 3)["by_currency"]["SAR"] == 0.3)

print("\n--- healthy")
run("chaos.py", "reset")
load(4)
time.sleep(2)  # let the worker drain the last orders
m = check_all_match("healthy")
check("healthy: nothing unpaid, queued or overstated",
      all(m[k]["count"] == 0 for k in ("unpaid_orders", "queued_orders", "ledger_overstatement")))

print("\n--- payment_drop")
chaos.payment_drop()
time.sleep(3)  # hot reload
load(6)
time.sleep(2)
m = check_all_match("payment_drop")
check("payment_drop: unpaid value > 0, in both currencies",
      m["unpaid_orders"]["total_sar"] > 0 and set(m["unpaid_orders"]["by_currency"]) == {"SAR", "USD"},
      str(m["unpaid_orders"]))
di = requests.get(f"{URL}/data-integrity", timeout=5).json()
check("/data-integrity reports the same money as /business-metrics", di["money_impact"] == m)
unpaid_in_db = expected_from_db()["unpaid_orders"]
chaos.set_config("payment_provider_url", "internal://payments-v1")
time.sleep(3)  # hot reload
r = requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10).json()
check("reprocess money_recovered = DB value of the orders that were unpaid", matches(r["money_recovered"], unpaid_in_db)
      and r["paid"] == unpaid_in_db["count"] > 0,
      f"recovered {r['money_recovered']['count']} / {r['money_recovered']['total_sar']} SAR, "
      f"DB had {unpaid_in_db['count']} / {unpaid_in_db['total_sar']:.2f} SAR")
r = requests.post(f"{URL}/admin/reprocess", json={"order_ids": "all_unpaid"}, timeout=10).json()
check("second reprocess recovers 0 (nothing double-counted)", r["money_recovered"]["count"] == 0, str(r))

print("\n--- worker_hang")
run("chaos.py", "reset")
load(2)
time.sleep(1)
chaos.worker_hang()
load(6)
m = check_all_match("worker_hang")
check("worker_hang: queued (delayed) value > 0, nothing unpaid",
      m["queued_orders"]["total_sar"] > 0 and m["unpaid_orders"]["count"] == 0, str(m["queued_orders"]))

print("\n--- data_corruption")
run("chaos.py", "reset")
load(3)
chaos.data_corruption()
load(10)
time.sleep(7)  # one more sync run, then the ledger is stable
m = check_all_match("data_corruption")
over = m["ledger_overstatement"]
mismatched = requests.get(f"{URL}/data-integrity", timeout=5).json()["ledger_mismatches"]
conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
ids = db.mismatched_ledger_order_ids()
sar_orders = sum(r[0] for r in conn.execute(
    f"SELECT amount FROM orders WHERE id IN ({','.join('?' * len(ids))})", ids)) if ids else 0
conn.close()
check("data_corruption: overstatement = 99 x the real SAR amounts (ledger shows 100x), SAR only",
      over["count"] == mismatched["count"] > 0 and set(over["by_currency"]) == {"SAR"}
      and abs(over["by_currency"]["SAR"] - 99 * sar_orders) < 0.05,
      f"{over['by_currency']} vs 99 x {sar_orders:.2f}")
chaos.set_config("version", "v1.3")
time.sleep(3)
overstated_in_db = expected_from_db()["ledger_overstatement"]
r = requests.post(f"{URL}/admin/repair-ledger", json={"order_ids": "all_flagged"}, timeout=10).json()
check("repair money_recovered = DB overstatement of the repaired rows (measured before the fix)",
      matches(r["money_recovered"], overstated_in_db) and r["fixed"] == overstated_in_db["count"] > 0,
      f"recovered {r['money_recovered']['count']} / {r['money_recovered']['total_sar']} SAR, "
      f"DB had {overstated_in_db['count']} / {overstated_in_db['total_sar']:.2f} SAR")
check("after rollback + repair: overstatement back to 0", money()["ledger_overstatement"]["count"] == 0)
r = requests.post(f"{URL}/admin/repair-ledger", json={"order_ids": "all_flagged"}, timeout=10).json()
check("second repair recovers 0", r["money_recovered"]["count"] == 0 and r["fixed"] == 0, str(r))

run("chaos.py", "reset")
run("supervisor.py", "stop")
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
