"""Live check (real Claude API, ~$0.03-0.04 per run): Nabd in ACT mode fixes each scenario.

Every write is auto-approved (standing in for the human pressing "y"). Afterwards an independent
monitor check decides whether the system REALLY recovered. A final run rejects every fix.
Skips (exit 0) if there is no ANTHROPIC_API_KEY.
Run from the project root:  python tests/live/live_act.py [scenario ...]
Starts and resets the service on port 8000, and leaves it stopped.
"""

import time

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
    if scenario == "payment_drop":
        results.check("payment_drop: reverted the URL and reprocessed", {"set_config", "reprocess_orders"} <= set(writes), str(writes))
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
