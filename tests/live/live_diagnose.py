"""Live check (real Claude API, ~$0.03 per scenario): Nabd in RECOMMEND mode diagnoses each scenario.

Recommend mode has no write tools, so nothing is changed: this checks the root cause and the
recommended fixes only. Skips (exit 0) if there is no ANTHROPIC_API_KEY.
Run from the project root:  python tests/live/live_diagnose.py [scenario ...]
Starts and resets the service on port 8000, and leaves it stopped.
"""

from live_common import (Results, action_index, inject_and_wait, mentions, pick_scenarios, reset, save,
                         show_progress, skip_without_api_key, start_traffic, stop)

skip_without_api_key()
import agent  # noqa: E402  (imported after the key check)

results = Results()

for scenario in pick_scenarios():
    print(f"\n--- {scenario} (recommend mode)")
    reset()
    traffic = start_traffic()
    try:
        alert = inject_and_wait(scenario)
        diagnosis, record = agent.run_agent(alert, on_event=show_progress, recommend_only=True)
    finally:
        traffic.kill()
    save(f"diagnose_{scenario}", record)
    causes = " ".join(diagnosis.root_causes)
    recs = diagnosis.recommended_actions
    print(f"  root causes: {causes[:300]}")
    print(f"  recommended: {[r[:90] for r in recs]}")

    results.check(f"{scenario}: changed nothing (recommend mode)",
                  not record["stats"]["writes_applied"] and not diagnosis.actions_taken and not diagnosis.recovered)
    if scenario == "payment_drop":
        results.check("payment_drop: blames the payment provider URL change", mentions(causes, "payment_provider_url", "payments-v2"))
        url, reprocess = action_index(recs, "url_fix"), action_index(recs, "reprocess")
        results.check("payment_drop: recommends reverting the URL, then reprocessing",
                      url is not None and reprocess is not None and url < reprocess, f"url {url}, reprocess {reprocess}")
    elif scenario == "worker_hang":
        results.check("worker_hang: identifies the stuck worker", mentions(causes, "worker"))
        restart = action_index(recs, "restart")
        results.check("worker_hang: restart is the first or second recommendation",
                      restart is not None and restart <= 1, f"index {restart}")
    elif scenario == "compound":
        results.check("compound: finds BOTH causes",
                      mentions(causes, "payment_provider_url", "payments-v2") and mentions(causes, "worker"))
        url, restart = action_index(recs, "url_fix"), action_index(recs, "restart")
        results.check("compound: fix the URL BEFORE restarting the worker",
                      url is not None and restart is not None and url < restart, f"url {url}, restart {restart}")
    elif scenario == "unknown":
        results.check("unknown: names the read-only database", mentions(causes, "read-only", "readonly"))
        results.check("unknown: escalates", diagnosis.escalate)

reset()
stop()
results.finish()
