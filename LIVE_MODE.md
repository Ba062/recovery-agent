# Live mode (dashboard)

Built on branch `live-mode`; merged into `main` on Sat 31 Oct (`cfb83cd`).

Live mode runs a **real incident** from the dashboard. You approve or reject every fix in the browser:

    reset the service → start traffic → inject the fault you pick → monitor → Nabd investigates
    → you approve / reject each fix → final check → bilingual incident report

Replay mode is unchanged and is still the default.

## How to use it

1. `.env` must contain `ANTHROPIC_API_KEY`. Without it, Start is disabled and the page says why.
2. Nothing else should be using port 8000, so no tests and no eval running. Live mode resets the service there and
   **wipes its database**.
3. Start the dashboard: `streamlit run ui/app.py --server.port 8502 --server.headless true`. Then open
   http://localhost:8502.
4. At the top, switch **Mode** to **Live**.
5. Pick a fault and press **▶ Start**. The faults on offer are the dev scenarios: payment_drop, payment_drop_silent,
   data_corruption, worker_hang, compound, unknown, relapse.
6. Watch the stage strip and the live event stream. Whenever Nabd proposes a fix, the panel shows
   "Nabd wants to run `tool(args)`", Nabd's latest reasoning, and **✅ Approve** and **❌ Reject**. Nothing changes
   until you click. The model often skips reasoning on simple steps, so if the reasoning was written before other tool
   calls, the panel says so: it may be about an earlier step.
7. At the end you see:
   - "At alert" next to "After Nabd" (one final monitor check);
   - the money figures;
   - the diagnosis;
   - the English and Arabic incident report.
8. **■ Stop** (during a run) rejects the fix being asked about, rejects any later one, and skips the remaining steps.
   **Stop the service** (after a run) stops the service on port 8000, which live mode leaves running so you can
   inspect it.

**Cost:** one live run of payment_drop cost **$0.051** in total ($0.032 for the agent and $0.019 for the report), and
took about 60 s.

**Where runs go:** each run is saved to `live_runs/` (git-ignored) as `<time>_<fault>.json` plus `.report.md`. They
don't appear in Replay mode, whose sources are unchanged.

## How it works

- **`ui/live.py` (new, no Streamlit code).** A `LiveRun` runs the whole incident in a background thread.
  - The thread is needed because Streamlit reruns the page script on every click, so the incident can't run inside
    the page.
  - The agent is `agent.run_agent`, **unchanged**. Live mode only passes two callbacks:
    - `on_event`, which appends every event to the run's event list;
    - `approve`, which posts the fix as a pending request and **waits on a queue** until the page answers.
  - Each request has its own queue (`queue.Queue(maxsize=1)`), so a late or repeated click can never leak into the
    next fix.
  - A shared lock guards the fields both threads touch, and `view()` gives the page a consistent copy.
  - **Safety:**
    - no answer within 10 minutes counts as a rejection;
    - after Stop, every approval is rejected immediately;
    - an exception is shown on the page, never lost in the thread;
    - traffic and the relapse re-injector are always stopped, in `finally`.
  - **One run at a time per dashboard process** (`live.start` refuses a second one), because a run owns port 8000.
    Reloading the page reconnects to the run in progress.
- **`ui/app.py`:**
  - a `Mode: Replay | Live` switch at the top of the main area (the replay sidebar is untouched);
  - `live_page()` (picker, Start, Stop, Stop the service);
  - an `@st.fragment(run_every=1s)` panel that redraws only itself every second while the run is active;
  - when the run ends, one full rerun shows the final view.
  - The event stream reuses replay mode's `show_event`, so it looks the same.
- **Not changed:** no frozen file (`agent.py`, `monitor.py`, `requirements.txt`, `eval/grade.py`, `eval/scenarios.py`,
  `eval/arms.py`). Nor the other files in the freeze manifest (`chaos.py`, `supervisor.py`, `service/`). Live mode only
  *imports* them.
- **Other changes:** `.gitignore` gets `live_runs/`, and `tests/check_ui.py` gets the new checks.

## What was tested

- **`tests/check_ui.py`, 32/32:** the 12 replay checks (unchanged, all passing) plus 20 new live-mode checks. These use
  a **mocked agent and fake steps**: no service, no port, no API key.
  - **`LiveRun` on its own:**
    - a fix waits for the human;
    - approve answers it, and a repeated or stale click is ignored;
    - the run finishes with the record, a green final check, a saved report, and traffic stopped;
    - the event stream has all 7 steps and both approvals;
    - rejecting both fixes means nothing is applied, and the final check is still broken;
    - Stop rejects the waiting fix and the rest, and stops traffic;
    - no answer in time counts as rejected;
    - no alert means the agent is never called;
    - an error in a step is shown, and traffic is still stopped;
    - a second Start is refused while a run is active.
  - **The page, driven by Streamlit's AppTest:**
    - the switch, picker and Start are shown;
    - Approve fix 1, then fix 2, in the page, then the final view: before/after, money, diagnosis and the Arabic
      report;
    - Reject both, then "still broken" and both rejections in the stream;
    - Stop while a fix waits ends the run as stopped, with nothing applied;
    - without an API key, Start is disabled and says why.
- **A real end-to-end run of payment_drop** (Fri 30 Oct, 21:26). The real page was driven through AppTest, with the
  real service on port 8000 and the real API, and nothing mocked:
  - the monitor alerted at 7.6 s (`payment_gap`, with /health passing);
  - Nabd read the config, the changes and the DEBUG payment logs;
  - it proposed `set_config payment_provider_url = internal://payments-v1` and then `reprocess_orders all_unpaid`;
    both were approved by clicking Approve on the page;
  - it recovered, with 53 orders paid; the final check was all green; and the report used model prose;
  - the final page showed SAR at risk 21,178, SAR recovered 26,341 and still at risk 0, plus the diagnosis and both
    reports, with no exceptions;
  - 60 s and $0.051 in total. The service was stopped afterwards.
- **The real server:** `streamlit run ui/app.py --server.headless true` started and its health endpoint answered `ok`.
- **`tests/run_all.py`: ALL PASSED**, 365/365 across 10 scripts (`check_ui.py` 33/33). That covers 20 replay and live
  checks plus 1 added after the real run (below).
- **No frozen file changed:** `git diff main` on the freeze manifest is empty, and `run_eval.py`'s freeze check
  reports no changes.
- **Changed after the real end-to-end run, covered by the mocked tests only:**
  - **A race between Stop and a new fix request.** If Stop came in the instant before a fix was posted, that fix waited
    out the 10-minute timeout. Now the check and the post happen under the lock that Stop takes. New check: "a fix asked
    for after Stop is rejected at once".
  - **The final view** now says "After Nabd: not checked" when a run was stopped or failed before the final check.
  - Neither change touches the approve-path logic the real run used, but `approve()` was edited. Badr's browser runs
    (next point) went through the edited `approve()`.
- **Clicked through in a real browser by Badr** (Sat 31 Oct, 04:13-04:18, after the merge; records in `live_runs/`):
  - data_corruption: rollback approved, `repair_ledger` **rejected**, so Nabd escalated instead of retrying;
  - data_corruption again: both fixes approved, recovered;
  - payment_drop_silent: both fixes approved, recovered.
- **Sat 31 Oct, the approval prompt shows Nabd's latest reasoning** (code review L2). Checked with the mocked runs:
  `check_ui.py` +4 checks (42/42), all 4 fail on the previous `ui/`. Not yet seen in a real live run.

## Known issues and limits

- **Don't run live mode while tests or the eval use port 8000.** Live mode's reset stops the service there and wipes its
  database, so it would break a running eval (and the reverse). Nothing prevents this beyond the warning on the page.
- **Stop can't interrupt an API call already in flight.** It rejects every further fix, and the agent finishes its
  current step (at most 12 steps). The page shows the run as stopped once the agent returns.
- **One run per dashboard process, shared by all browser tabs.** Two tabs see the same run, and either can click
  Approve; the first click wins, and the second is ignored.
- **The service is left running after a run** (on purpose, so you can inspect it). After the `unknown` fault the database
  stays read-only until the next reset (a new live run, `python chaos.py reset`, or any test run). "Stop the service"
  stops the service but doesn't undo that.
- **If the Streamlit server stops in the middle of a run,** the background thread stops with it. The service is left
  in the fault state until the next reset.
- **Each run costs a few cents,** including one extra API call for the report.
- **No `healthy` option.** A run with no fault would just end at "no alert".
