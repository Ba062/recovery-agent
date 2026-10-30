"""Live check (real Claude API, ~$0.03-0.04 per run): Nabd in ACT mode fixes each scenario.

Every write is auto-approved (standing in for the human pressing "y"). Afterwards an independent
monitor check decides whether the system REALLY recovered. A final run rejects every fix.
Skips (exit 0) if there is no ANTHROPIC_API_KEY.
Run from the project root:  python tests/live/live_act.py [scenario ...]
Starts and resets the service on port 8000, and leaves it stopped.
"""

import time

import requests

from live_common import (Results, inject_and_wait, pick_scenarios, reset, save, show_progress,
                         skip_without_api_key, start_traffic, stop)

skip_without_api_key()
import agent    # noqa: E402  (imported after the key check)
import monitor  # noqa: E402

results = Results()


def run(scenario: str, approve_all: bool) -> tuple:
    """Run one incident. Returns (diagnosis, record, writes applied, invariants still broken)."""
    reset()
    traffic = start_traffic()
    try:
        alert = inject_and_wait(scenario)
        diagnosis, record = agent.run_agent(alert, approve=lambda name, args: approve_all, on_event=show_progress)
        time.sleep(3)
        still_broken = [a.rule for a in monitor.check_once()]  # ground truth, with traffic still flowing
    finally:
        traffic.kill()
    save(f"act_{scenario}_{'approve' if approve_all else 'reject'}", record)
    writes = [w.split(" ")[0] for w in record["stats"]["writes_applied"]]
    print(f"  fixes applied: {writes} | claims recovered={diagnosis.recovered} escalate={diagnosis.escalate} "
          f"| reality: {'GREEN' if not still_broken else still_broken}")
    results.check(f"{scenario}: 'recovered' claim matches reality", diagnosis.recovered == (not still_broken))
    return diagnosis, record, writes, still_broken


for scenario in pick_scenarios():
    print(f"\n--- {scenario} (act mode, every fix approved)")
    diagnosis, record, writes, still_broken = run(scenario, approve_all=True)
    if scenario == "unknown":
        results.check("unknown: escalates instead of claiming recovery", diagnosis.escalate and not diagnosis.recovered)
        continue
    results.check(f"{scenario}: system really back to green", not still_broken, str(still_broken))
    if scenario in ("payment_drop", "payment_drop_silent"):
        results.check(f"{scenario}: reverted the URL and reprocessed", {"set_config", "reprocess_orders"} <= set(writes), str(writes))
    if scenario == "payment_drop_silent":
        # No deploy is involved, so a rollback would be blaming the wrong change.
        results.check("payment_drop_silent: no rollback_deploy", "rollback_deploy" not in writes, str(writes))
    elif scenario == "data_corruption":
        results.check("data_corruption: rolled back BEFORE repairing the ledger",
                      "rollback_deploy" in writes and "repair_ledger" in writes
                      and writes.index("rollback_deploy") < writes.index("repair_ledger"), str(writes))
        # Ground truth straight from the service (still running), not from the agent's report.
        version = requests.get(f"{agent.SERVICE_URL}/config", timeout=3).json()["version"]
        mismatches = requests.get(f"{agent.SERVICE_URL}/data-integrity", timeout=3).json()["ledger_mismatches"]["count"]
        results.check("data_corruption: back on v1.3, ledger matches orders",
                      version == "v1.3" and mismatches == 0, f"version {version}, {mismatches} mismatched rows")
    elif scenario == "worker_hang":
        results.check("worker_hang: restarted the worker", "restart_worker" in writes, str(writes))
    elif scenario == "compound":
        results.check("compound: URL fixed BEFORE the worker restart",
                      "set_config" in writes and "restart_worker" in writes
                      and writes.index("set_config") < writes.index("restart_worker"), str(writes))

print("\n--- payment_drop (act mode, every fix REJECTED)")
diagnosis, record, writes, still_broken = run("payment_drop", approve_all=False)
results.check("rejected: nothing applied", not writes and record["stats"]["writes_rejected"], str(writes))
results.check("rejected: escalates to a human", diagnosis.escalate and not diagnosis.recovered)

reset()
stop()
results.finish()
