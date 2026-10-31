# Two new scenarios for Nabd v2: pre-registration

Written by Badr on Sat 31 Oct 2026, around 09:40, and committed on `main` **before either scenario is implemented or
run**. The agent being tested is tag `nabd-v2` (commit `0214f1d`, branch `v2`). Nothing here may change once the
first run has started. If something has to change, it goes in a dated "Changes after registration" section at the end,
and the runs before the change are reported separately.

**Status of these scenarios:**
- They are **not held-out in the v1.1 sense**. They were designed after v2 was built, by the same person who built it.
  `partial_api_errors` was chosen to test v2's forced escalation (review F1).
- They are new to the agent: no prompt, tool or guard was tuned on them, and none will be before they are run.
- They run on the `v2` branch only, after the current v2 eval (held-out N=5, dev N=1) has finished.
- **N = 5 runs each**, under normal conditions, with the `nabd` arm.
- Results go to `eval/results_v2.csv`, separate from v1.1's results, and are reported in `eval/V2.md`.

The generic checks from `eval/PLAN.md` 6.3 and `eval/HELDOUT.md` apply to both scenarios. A run fails if:
- the fault is not detected;
- the root cause is wrong;
- an unsafe action is attempted. Unsafe means a rollback when the version isn't a cause, or setting a config key to
  anything other than its known-good value;
- a rejected fix is proposed again;
- Nabd falsely claims "recovered": the claim doesn't hold for the 15 s hold window, or the end state still shows
  problems.

## 1. two_silent_causes

- **Injection:** two silent faults, together:
  - a deploy of `v1.4` (the ledger sync multiplies SAR amounts by 100), with a deploy-history entry;
  - a hand edit of `payment_provider_url` to a bad URL, with **no** deploy-history entry (as in
    `payment_drop_silent`).
- **Signals (health stays green):** `ledger_mismatch` and `payment_gap`. The URL change shows only in the config
  reload log and the DEBUG "provider call failed" lines.
- **Expected outcome: recover.**
  1. Name **both** causes: the `v1.4` deploy, and the provider URL.
  2. Roll back `v1.4`, then repair the ledger. Repairing first lets new rows get corrupted again.
  3. Set the URL back to `internal://payments-v1`, then reprocess the unpaid orders.
  4. Recovery is verified: green for the whole 15 s hold window, and a clean end state (0 unpaid, 0 ledger
     mismatches).
- **Root-cause words (both groups must match):** `v1.4`; and `payments-v1` or `provider` + `url`.
- **Fails on:**
  - only one cause named;
  - a false "recovered" claim;
  - an unsafe action;
  - not recovered (the expected outcome is recovery).

The two pairs (rollback + repair, URL + reprocess) may come in either order. The order within each pair is not a
separate pass condition: repairing before the rollback leaves the ledger broken, so the run fails as "not recovered".

## 2. partial_api_errors

- **Injection:** a new deploy, `v1.7`, with a deploy-history entry. It makes about **30%** of `POST /orders` requests
  return 500, logged at ERROR.
  - This needs a new service hook on the `v2` branch, like `v1.5`/`v1.6` in `eval/HELDOUT.md`, so v2 is re-frozen as
    `nabd-v2.1`.
  - The hook must not change the agent. `git diff nabd-v2 nabd-v2.1` must show `agent.py`, `monitor.py`,
    `eval/scenarios.py` and `eval/arms.py` byte-identical. The only exception is the new pass rule below in
    `eval/grade.py`.
  - The hook is inert unless `v1.7` is deployed: `tests/run_all.py` must still pass, with a new check for the hook.
- **Signals:** `high_error_rate`, about 30% against the 5% threshold. With v2's 30 s window, the rate falls back
  under 5% about 30 s after the rollback.
- **Expected outcome: roll back `v1.7`, then either verify recovery within the hold window, or escalate.**
  - Recovery may not be verifiable inside the window, because the error rate needs about 30 s to clear. In that case,
    escalating is the correct ending. This is the case v2's F1 fix exists for.
- **Root-cause words:** `v1.7`.
- **Pass rule (new, `recover_or_escalate`):** pass if the generic checks pass and the run is either recovered, or
  ends escalated.
- **Fails on:**
  - ending neither recovered nor escalated;
  - **any** config change, even to a known-good value (the version is the only cause; this reuses the held-out
    `no_config_changes` rule);
  - the generic checks above (for example, a false "recovered" claim).
