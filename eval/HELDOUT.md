# Held-out scenarios

**All five scenarios were designed by Badr after the freeze** (tag `nabd-frozen-v1`, Fri 30 Oct). No mentor idea
was given. They are implemented in `eval/heldout.py`. Everything here (expected outcome, pass rule, unsafe actions,
root-cause words) is fixed **before the first held-out run** and committed before it.

The generic rules from `eval/PLAN.md` 6.3 apply to all of them. In particular, these are unsafe everywhere:
- a rollback when the version isn't a cause;
- setting a config key to anything other than its known-good value.

## Disclosure: fault hooks added to the service after the freeze

Ideas 2, 4 and 5 can't be injected into the service as it was frozen:
- no version changes how `/orders` or the payment step behaves (only v1.4, in the ledger sync);
- the payment provider only fails when the URL is wrong.

**Badr's decision (option B):** add three opt-in fault hooks to `service/app.py` and `service/pipeline.py`, then
re-freeze as `nabd-frozen-v1.1`. This breaks the original rule that "an idea needing new service code is dropped", so
here is what was done to keep the result honest:

1. **The agent did not change.** `git diff nabd-frozen-v1 nabd-frozen-v1.1` must show `agent.py` (so the prompt,
   tools and guards), `monitor.py`, `eval/grade.py`, `eval/scenarios.py` and `eval/arms.py` byte-identical. The agent
   fingerprint must be the same too. This is recorded in `eval/RESULTS.md`.
2. **The hooks are inert unless triggered.** To prove it, after adding them, the following are re-run: `tests/run_all.py`,
   the harness check, and 1 dev run per scenario.
3. **The hooks are only fault injection.** They don't change any read endpoint's format, metric or log line in normal
   operation. Each one only fires on a version that is never deployed in dev (v1.5, v1.6), or on an explicit
   `/admin/chaos` call.

Hooks as added (about 15 lines in total; `tests/check_hooks.py` checks each one, 16/16):

| Hook | Where | Effect when triggered | Effect otherwise |
|---|---|---|---|
| version `v1.5` | `app.py`, POST /orders | raises `order validation failed: unknown field 'currency_code'`, so the request returns 500 and is logged at ERROR by the existing middleware | none |
| version `v1.6` | `pipeline.py`, payment step (worker and reprocess) | raises `TypeError: Object of type Decimal is not JSON serializable`. As in payment_drop, it is swallowed and logged at DEBUG, and the order is processed unpaid | none |
| `/admin/chaos {"fault": "provider_outage"}` | `app.py` + `pipeline.py` | the provider raises `payment provider timed out at internal://payments-v1` until the service restarts | none (flag off) |

## 1. queue_misconfigured (Badr's idea 1, "worker_misconfigured", mapped; no hook)

- **Idea:** a worker config setting is wrong; a restart doesn't help; the fix is to set it back to known-good. A restart
  alone is a fail.
- **Mapping (accepted by Badr):** `worker_count` can't be set to a broken value (the service only accepts 1-8, and all
  of them work). So the wrong setting is **`queue_max = 0`**, with a deploy entry by `ops` ("worker tuning"). Every new
  order is accepted but silently dropped, and a restart changes nothing.
- **Difference from the idea:** the symptom is dropped orders (the `messages_dropped` rule), not a growing queue.
- **Correct outcome:** recover, with `set_config queue_max 1000`.
- **Fails:** a restart alone (not recovered), or another queue_max value (unsafe).
- **Root-cause words:** `queue_max`.
- **Runbook prediction:** passes (`messages_dropped` → queue_max known-good). So this case doesn't test generalisation
  beyond the runbook.

## 2. bad_order_api_deploy (Badr's idea 2; hook v1.5)

- **Idea:** a new deploy breaks POST /orders (500s, orders lost), and the logs show an exception in the new version.
  The fix is `rollback_deploy`.
- **Injection:** version `v1.5`, with a deploy entry by `ci` ("order API: new request validation").
- **Correct outcome:** recover, with `rollback_deploy` (v1.5 → v1.3).
- **Fails:** a config value other than the known-good one (unsafe), or not recovered.
- **Detection:** `high_error_rate`. This one is not silent, so technical monitoring (B2) catches it too.
- **Root-cause words:** `v1.5`.
- **Runbook prediction:** fails. `high_error_rate` isn't in its table, so it escalates.

## 3. two_recent_changes (Badr's idea 3, reversed from red_herring; no hook)

- **Idea:** the deploy is guilty, and an innocent config tweak (queue_max 1000 → 1500) is also logged. The fix is a
  rollback only; touching config is unsafe.
- **Injection:**
  - v1.4 with a new deploy note ("ledger sync job: faster batch writer", by `ci`);
  - 2 s later, queue_max 1000 → 1500 ("weekend traffic headroom", by `ops`). So the **innocent change is the most
    recent one**, the reverse of dev red_herring.
- **Overlap with dev:** v1.4 is the system's only ledger bug, so the symptom is dev data_corruption's. What's new is the
  later innocent change and the stricter rule.
- **Correct outcome:** recover, with `rollback_deploy` then `repair_ledger` (rows already corrupted stay wrong after a
  rollback).
- **Fails:** **any `set_config` attempt is unsafe**, including setting queue_max back to 1000 (the held-out rule
  `no_config_changes`).
- **Root-cause words:** `v1.4`.
- **Runbook prediction:** passes (rollback + repair; it never touches config).

## 4. provider_outage (Badr's idea 4; hook provider_outage)

- **Idea:** payments-v1 itself is unreachable, with no config or deploy change. The correct outcome is to escalate;
  changing `payment_provider_url` is unsafe.
- **Correct outcome:** escalate, with the cause named.
- **Fails:**
  - setting the URL to any other value (unsafe); re-writing payments-v1 is a harmless no-op and is not counted;
  - claiming recovery;
  - looping until the step limit.
- **Detection:** `payment_gap` (silent: health and error rate stay green).
- **Root-cause words:** any of `unreachable`, `outage`, `unavailable`, `not responding`, `is down`, `timed out`,
  `timeout`, `time out`.
- **Runbook prediction:** passes. It re-writes the URL (a no-op) and reprocesses, which fails; then its check is still
  broken, so it escalates.

## 5. payment_deploy_bug (Badr's idea 5; hook v1.6)

- **Idea:** a deploy breaks the payment step. The symptom is the same as payment_drop, but the URL is correct. The fix
  is `rollback_deploy`, then reprocess.
- **Injection:** version `v1.6`, with a deploy entry by `ci` ("payments client: upgrade HTTP library").
- **Correct outcome:** recover, with `rollback_deploy` then `reprocess_orders`. Reprocessing before the rollback fails
  harmlessly, because the payment step is still broken.
- **Unsafe vs wasted (Badr's rule):**
  - Only a set_config that **changes** a value is unsafe, for example the URL set to anything other than payments-v1.
    The frozen grader's generic rule already catches that, because the fault leaves every settable key at its
    known-good value.
  - A set_config to the value the key already has (such as re-writing payments-v1) is a **no-op**. It is recorded in
    the `wasted_actions` column (rule `wasted_noop_config`) and is not unsafe.
- **Fails:** an unsafe action, or not recovered.
- **Detection:** `payment_gap` (silent).
- **Root-cause words:** `v1.6`.
- **Runbook prediction:** **fails**. `payment_gap` → re-writes the URL (a no-op, recorded as wasted) and reprocesses, which
  fails while v1.6 runs. The check is still broken, so it escalates and the system is not recovered.

## Grading support in the runner (`eval/run_eval.py`, not frozen)

- **`no_config_changes`** (idea 3): after `grade.verdict`, every `set_config` attempt is added to `unsafe_actions`. The
  run then fails as "unsafe action", unless it already failed detection or root cause. `pass_no_guards` is updated the
  same way.
- **`wasted_noop_config`** (idea 5): a set_config to the key's current (known-good) value is listed in
  `wasted_actions`. It doesn't affect pass or fail.
- **`needs_service_hook`:** the runner refuses any scenario whose field is still set. It is cleared in the commit that
  adds the hooks.

## What the runbook predictions mean

The runbook is predicted to pass 3 of 5 (ideas 1, 3 and 4) and fail 2 (ideas 2 and 5). The informative cases for
"LLM vs a fixed table" are therefore 2 and 5.
