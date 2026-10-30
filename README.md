# recovery-agent

*(Full README coming: summary, architecture, setup, scenarios, results, design decisions.)*

## Evaluation integrity: what changed after the freeze

The agent was frozen (`git tag nabd-frozen-v1`) before any held-out scenario existed. All five held-out scenarios
were then designed by Badr, after the freeze (see `eval/PLAN.md` and `eval/HELDOUT.md`).

Three of those ideas (a deploy that breaks the order API, a deploy that breaks the payment step, and a payment provider
outage) could not be injected into the frozen simulator. We therefore **added three opt-in fault hooks to the target
service after the freeze** and re-froze as `nabd-frozen-v1.1`. This departs from our own plan, which said such ideas
would be dropped, so we state it here:

- The hooks only fire on versions never deployed in dev (v1.5, v1.6) or on an explicit `/admin/chaos` call.
- **The agent must be byte-identical between the two tags**: `agent.py` (prompt, tools, guards), `monitor.py` and the eval
  grader. Verify with `git diff nabd-frozen-v1 nabd-frozen-v1.1 -- agent.py monitor.py eval/grade.py eval/scenarios.py eval/arms.py`
  (empty output). `eval/RESULTS.md` records the file hashes and agent fingerprints of both freezes.
- The held-out definitions and their expected outcomes were committed in `554f884` (tagged `nabd-frozen-v1.1` via
  `6599f35`), before any held-out run.
- Checks that the hooks are inert in normal operation, run after adding them (Fri 30 Oct), **all passed**:
  - the full no-API test suite `tests/run_all.py`: 336/336 checks, including `tests/check_hooks.py` (16/16), which shows
    each hook works when triggered and is off after a reset;
  - the harness check `python eval/run_eval.py check`: 23/23 (the runbook still passes the dev scenarios; doing nothing
    is still never graded as recovered);
  - one dev run per scenario with the agent: 8/8 passed, with the same agent fingerprint (`365928b5883c`) as before
    the hooks.
