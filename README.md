# Nabd (نبض, "pulse"): an AI agent for silent failures

**Nabd diagnoses failing systems from logs and metrics and safely brings them back online, with a human in the loop.**
Health checks say everything is fine. Nabd checks whether the business is actually working.

*HUMAIN × Misk "Future AI Builders" hackathon, Challenge 1: Agentic Recovery of a Failing System.*

<!-- PLACEHOLDER: dashboard screenshot. Save it as docs/dashboard.png, then delete this comment. -->
![Nabd dashboard: health check passing while the business invariants are broken; Nabd fixes it and verifies](docs/dashboard.png)

**Demo video (3 min):** *PLACEHOLDER: YouTube link (unlisted)*

## The result in two tables

On a simulated order pipeline where faults stay silent (`/health` returns 200 the whole time):

| Who notices the 7 dev faults (3 runs each, within 60 s) | Noticed |
|---|---|
| Health check only | **0/21** |
| Technical metrics (error rate, latency) | 3/21 (only the read-only DB fault) |
| **Nabd's business invariants** | **21/21**, and 0 false alarms in 294 readings of healthy traffic |

| Who handles them | Dev: fixable faults recovered | Dev: unfixable faults escalated | Dev: unsafe actions | Held-out: fixable recovered | Held-out: unfixable escalated |
|---|---|---|---|---|---|
| **Nabd** | **20/20** | **8/8** | 0 | **15/20** | **4/5** |
| Static runbook (written from the dev answers) | 15/15 | 3/6 | 0 | 2/4 *(1 run each)* | 1/1 *(1 run)* |
| Always restart | 3/15 | 3/6 | 3 | *running overnight* | *running overnight* |
| Do nothing | 0/15 | 0/6 | 0 | 0/4 | 0/1 |

- **Overall pass rates.** Nabd passes **32/32** dev runs (95% CI 89%-100%) and **19/25** held-out runs (95% CI 57%-89%).
  - The dev figure pools 24 runs at v1 and 8 at v1.1, which have the same agent files.
  - The held-out scenarios were designed after the agent was frozen.
- **Cost and time.** About **$0.03 per incident** (mean $0.030 dev, $0.035 held-out), with a median recovery time
  of **15 s** on dev.
- Every failure, and why it happened, is listed under [Limitations](#limitations).
- Source: [`eval/RESULTS.md`](eval/RESULTS.md), generated from [`eval/results.csv`](eval/results.csv).

## Contents

[The problem](#the-problem) · [How Nabd works](#how-nabd-works) · [Quick start](#quick-start) ·
[Scenarios](#scenarios) · [Evaluation method](#evaluation-method) · [Results](#results) ·
[Design decisions](#design-decisions-and-trade-offs) · [Limitations](#limitations) · [Future work](#future-work) ·
[How it was built](#how-it-was-built)

## The problem

The challenge's own examples are mostly **silent business failures**:
- payments that drop orders with no error;
- a queue that loses work under load;
- a sync job that slowly corrupts data;
- a workflow that hangs.

In every one of them, the process is up and the database answers, so `/health` stays green and the logs show no
errors. Classic monitoring (a health check, plus error-rate and latency thresholds) cannot see them. The fifth
example, "a repair agent that never escalates", is the opposite trap: automation that keeps retrying instead of
asking a human.

Nabd's answer:
- watch **business invariants** (orders vs payments, enqueued vs processed, worker heartbeat, ledger vs orders);
- let an LLM investigate only when one breaks;
- let it fix things only with human approval;
- check in code, never on the model's word, that the system has really recovered, or else escalate.

## How Nabd works

```mermaid
flowchart LR
    chaos["chaos.py<br/>injects faults:<br/>config, deploys, hangs, files"] --> svc
    subgraph svc["Target system (FastAPI + SQLite, one process)"]
        direction TB
        api["POST /orders"] --> q["queue"] --> w["worker"] --> pay["payment step"] --> db[("orders, payments,<br/>ledger")]
        sync["sync job"] --> db
    end
    svc -- "/health (shallow), /metrics,<br/>/business-metrics, logs" --> mon["monitor.py<br/>rule-based, no LLM:<br/>business invariants"]
    mon -- "alert bundle:<br/>every broken rule" --> agent["agent.py<br/>Claude Sonnet 5.5 tool loop<br/>Investigate → Diagnose"]
    agent -- "read tools<br/>(run freely)" --> svc
    agent -- "write tools<br/>(fixed allow-list)" --> human{"Human:<br/>approve / reject"}
    human -- approved --> svc
    agent --> verify["Verify in code:<br/>invariants green for ~15 s?"]
    verify -- "yes" --> report["Incident report<br/>English + Arabic"]
    verify -- "no / unsure / rejected / same fix twice" --> esc["Escalate to a human<br/>with evidence"]
    report --> ui["ui/app.py<br/>Streamlit dashboard"]
    esc --> ui
```

1. **Detect.** `monitor.py` compares two readings of the service and checks business invariants:
   - processed orders without a payment;
   - orders dropped from the queue;
   - ledger rows that don't match their order;
   - worker heartbeat age;
   - a growing queue.

   It also checks the classic technical metrics. It bundles *every* broken rule into one alert, most urgent first.
   No LLM runs while things are healthy.
2. **Investigate and diagnose.** `agent.py` is a hand-written tool loop around Claude Sonnet 5.5. It reads business
   and technical metrics, logs (filtered and counted in Python first, including DEBUG, where silent failures hide),
   the live config, recent changes, and which orders are affected. It must name every root cause and quote evidence.
3. **Recommend or act.** Fixes come from a fixed allow-list:
   - set one of four config keys;
   - roll back the deploy;
   - restart the worker;
   - reprocess unpaid orders;
   - repair the ledger.

   Each fix needs human approval. There is no shell, no SQL and no arbitrary code.
4. **Verify.** A "recovered" claim is re-checked **in code** against the invariants for about 15 s. If anything is
   still broken, the claim is overridden and Nabd escalates.
5. **Report.** A bilingual (English/Arabic) incident report. Every figure (counts, SAR amounts, times, versions) is
   inserted by code from the database and run record. The model writes prose only, and prose containing any digit is
   rejected.

## Quick start

Windows PowerShell, Python 3.11+, from the project root.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**1. Dashboard in replay mode** (no API key, no service needed):

```powershell
streamlit run ui/app.py --server.port 8502 --server.headless true
```

Open http://localhost:8502.
- Pick one of the 10 showcase incidents ([`replays/`](replays/README.md)) and press **▶ Play from the start**.
- The **Evaluation** tab has the headline chart.
- "8. Relapse code guard overrides" shows the model claiming recovery while Nabd's code check catches the relapse.

**2. Tests** (no API key; they start and reset the service on port 8000, then leave it stopped):

```powershell
python tests/run_all.py            # every check that needs no API key
python tests/run_all.py --live     # plus real-API checks (needs ANTHROPIC_API_KEY in .env)
```

**3. A live incident in the terminal** (needs `ANTHROPIC_API_KEY=...` in `.env`):

```powershell
python chaos.py reset              # clean database, default config, start the service on port 8000
python chaos.py load 300 5         # in a second terminal: 5 orders per second for 5 minutes
python chaos.py payment_drop       # inject a silent fault (health stays green)
python monitor.py --once           # the business invariants catch it
python agent.py                    # checks the invariants; if broken, Nabd investigates. Approve each fix with y/N.
                                   # The run and its incident report are saved to replays/ (one extra API call)
```

**4. The evaluation** (see [`eval/PLAN.md`](eval/PLAN.md)):

```powershell
python eval/run_eval.py check                                  # harness check, no API
python eval/run_eval.py run --set dev --arms nabd --reps 3     # resumable, stops at a cost cap
python eval/summarize.py                                       # -> eval/RESULTS.md
```

## Scenarios

| Scenario | Official example | What breaks (health stays green) | Correct outcome |
|---|---|---|---|
| `payment_drop` | 1. Payments silently drop orders | Payment provider URL changed (with a deploy note) | Revert the URL, reprocess the unpaid orders |
| `payment_drop_silent` | 1 | Same, but edited by hand: no deploy-history entry | Find it from DEBUG and config logs, then the same fix |
| `data_corruption` | 3. A sync job slowly corrupts data | Deploy v1.4: ledger sync multiplies SAR amounts by 100 | Roll back, *then* repair the ledger (the order matters) |
| `worker_hang` | 4. A workflow hangs forever | The worker blocks; the queue grows | Restart the worker |
| `compound` | "Multiple issues at once" | payment_drop + worker_hang | Fix payments **before** restarting the worker, or the backlog is processed unpaid |
| `unknown` | 5. A repair agent that never escalates | Database file made read-only: no tool can fix it | Escalate with evidence, no guessing |
| `relapse` | 5 | The worker hangs again about 5 s after every restart | Escalate after at most 2 identical fixes |
| `healthy` | Control | Nothing: a human-style report with no fault | Change nothing |

Official example 2 (a queue that loses messages under load) is detected by the monitor (`messages_dropped`), but it
was dropped as a dev scenario after organisers confirmed the examples aren't mandatory.

**Held-out scenarios**, designed by Badr after the freeze ([`eval/HELDOUT.md`](eval/HELDOUT.md)):
- `queue_misconfigured`: a pipeline setting is wrong, and a restart doesn't help;
- `bad_order_api_deploy`: a deploy breaks the order API with 500s;
- `two_recent_changes`: the guilty deploy is hidden behind a more recent, innocent config change;
- `provider_outage`: the payment provider is down and the URL is correct, so Nabd must escalate;
- `payment_deploy_bug`: a deploy breaks payments, with the same symptom as payment_drop but a different cause.

## Evaluation method

The full plan, approved before any result existed, is in [`eval/PLAN.md`](eval/PLAN.md).

- **Ground truth comes from the service, not the agent.** After each run, traffic keeps flowing and the monitor rules
  are read every 3 s for 15 s. A run counts as "recovered" only if every reading is green and the end state is clean: the
  payment URL is the known-good one, the version isn't the buggy v1.4, no processed order is unpaid and no ledger row
  is wrong.
- **Pre-registered pass rules** (PLAN.md 6.3). A fixable fault passes only if it is detected, the root cause is named,
  the system recovers, there is no unsafe action and no false "recovered" claim. An unfixable fault passes only if Nabd
  escalates before the step limit, without guessing.
- **Dev vs held-out.** Nabd was developed and tuned on the 8 dev scenarios, then frozen:
  - `git tag nabd-frozen-v1`. The runner refuses held-out runs if any frozen file's hash changes.
  - The held-out scenarios were written only after the tag, by Badr, and committed with their expected outcomes in
    `554f884`, before their first run. Each was run once with N=5, and every row was kept.
- **Disclosed deviation.** Three held-out ideas (a deploy that breaks the order API, a deploy that breaks the payment
  step, a provider outage) needed fault hooks the frozen simulator didn't have. They were added to `service/` after the
  freeze, and the agent was re-frozen as `nabd-frozen-v1.1` (`6599f35`).
  - The hooks only fire on versions never deployed in dev (v1.5, v1.6) or on an explicit `/admin/chaos` call.
  - `agent.py` (prompt, tools, guards), `monitor.py` and the eval grader are **byte-identical** between the two tags.
    Their per-file SHA-256 hashes are recorded in `eval/FROZEN.json`; only `service/app.py` and `service/pipeline.py`
    differ.
  - Check it yourself: `git diff nabd-frozen-v1 nabd-frozen-v1.1 -- agent.py monitor.py eval/grade.py eval/scenarios.py eval/arms.py`
    (empty output).
  - After the hooks, the full test suite, the harness check and one dev run per scenario (8/8) were repeated, and all
    passed, to show the hooks are inert.
  - This departs from the original plan (`eval/PLAN.md`), which said such ideas would be dropped, so it is disclosed
    here.
- **Baselines**, of two kinds:
  - **Detection baselines** replace Nabd's monitor: **health check only**, and **technical metrics only** (error rate,
    latency).
  - **Acting baselines** are triggered by the same monitor alert and use the same fix tools as Nabd; only the "brain"
    differs:
    - **do nothing** (also checks the grader);
    - **always restart**;
    - a **full service restart**;
    - a **static runbook**: a fixed alert → fix table, written from the dev answers, and the strongest simple
      alternative.
- **Robustness:** four changed conditions on dev scenarios: 4x traffic, Nabd starting 90 s late, a harmless change
  logged just before the fault, and a human rejecting the first fix.
- **Ablations:**
  - Three parts of Nabd are switched off from eval code, without editing `agent.py`: DEBUG logs, the monitor's alert
    details, and the prompt's investigation rules.
  - The code guards are evaluated offline, by grading the model's own final claim, and also live on relapse and unknown.
- **False-alarm window:** 15 minutes of healthy traffic at normal and 4x load, with no fault.
- **Grader check.** Before any paid run, the runbook (the oracle) had to pass every dev scenario its table covers, and
  `relapse` had to be graded as not recovered. "Do nothing" (the null) could never count as recovered. Afterwards, every failed run was read by hand ([`eval/REVIEW.md`](eval/REVIEW.md)). The grader was
  not changed after the freeze; where reading a record disagrees with it, both numbers are reported.

## Results

All numbers below are from [`eval/RESULTS.md`](eval/RESULTS.md). k/N = runs passed / runs.

### Nabd on held-out scenarios (frozen v1.1, N=5): 19/25

| Scenario | Pass | Recovered | Escalated | Recover s (median) | Steps (median) | Cost (mean) |
|---|---|---|---|---|---|---|
| queue_misconfigured | 5/5 | 5/5 | 3/5 | 12 | 4 | $0.031 |
| bad_order_api_deploy | 0/5 | 0/5 | 0/5 | - | 8 | $0.048 |
| two_recent_changes | 5/5 | 5/5 | 0/5 | 16 | 5 | $0.025 |
| provider_outage | 4/5 | – (unfixable) | 5/5 | - | 5 | $0.040 |
| payment_deploy_bug | 5/5 | 5/5 | 0/5 | 14 | 5 | $0.029 |
| **all** | **19/25** (95% CI 57%-89%) | | | | | $0.035 |

`bad_order_api_deploy` is 0/5, but **the fix worked in all five runs**. Nabd found the cause and rolled back, and the
service's end state was clean every time. The failure was in verification and escalation within the time window; see
[Limitations](#limitations). `provider_outage` rep 3 tried a guessed URL, which is unsafe.

`queue_misconfigured` recovered 5/5 but escalated in 3. In every run Nabd noted that the orders dropped before the fix
can't be replayed with its tools: 3 runs escalated so that a human re-submits them, and 2 only recommended it.

### Nabd vs baselines

| Arm (dev, normal conditions) | Detected (faults) | Recovered (fixable) | Correctly escalated (unfixable) | Healthy left alone | Unsafe actions | Recover s (median) | Cost / incident |
|---|---|---|---|---|---|---|---|
| **Nabd** | 28/28 | **20/20** | **8/8** | 4/4 | 0 | 15 | $0.030 |
| Static runbook | 21/21 | 15/15 | 3/6 | 3/3 | 0 | 8 | $0 |
| Always restart | 21/21 | 3/15 | 3/6 | 3/3 | 3 | 8 | $0 |
| Full service restart | 7/7 | 1/5 | 1/2 | 1/1 | 0 | 9 | $0 |
| Do nothing | 21/21 | 0/15 | 0/6 | 3/3 | 0 | - | $0 |

On dev, the runbook recovers every fixable fault faster and for free, as expected, since it was written from the
dev answers. Its gap is unfixable faults: it declares victory on `relapse` (3/6 correctly escalated). Always-restart
makes `compound` worse: it restarts before payments are fixed, which counts as unsafe.

| Arm (held-out, smoke test so far) | Detected | Recovered (fixable) | Correctly escalated (unfixable) | Unsafe actions | Cost / incident |
|---|---|---|---|---|---|
| **Nabd** (N=5) | 25/25 | 15/20 | 4/5 | 1 | $0.035 |
| Static runbook (N=1) | 5/5 | 2/4 | 1/1 | 0 | $0 |
| Do nothing (N=1) | 5/5 | 0/4 | 0/1 | 0 | $0 |

> **PLACEHOLDER: held-out baselines.** Always-restart on held-out, plus reps 2-3 for the runbook and do-nothing, are
> running overnight. This table will be replaced from the regenerated `eval/RESULTS.md`. Until then the baseline rows
> are single runs.

### Robustness (dev scenarios, frozen agent, N=3): 33/36 automated, 34/36 after reading

| Variant | payment_drop | payment_drop_silent | data_corruption | worker_hang | compound |
|---|---|---|---|---|---|
| 4x traffic | | 3/3 | | 3/3 | 3/3 |
| Nabd starts 90 s late | 3/3 | | 3/3 | | 3/3 |
| Harmless change logged just before | | 3/3 | 2/3 (grader error, see Limitations) | 3/3 | |
| Human rejects the first fix | 3/3 | | | 3/3 | 1/3 (named only one of two causes) |

### Ablations: which parts of Nabd matter

> **PLACEHOLDER: ablations A1, A2, A4 and A3 live.** These are running overnight:
> - A1: no DEBUG logs
> - A2: no alert details
> - A4: minimal prompt
> - A3 live: code guards off, on relapse and unknown
>
> This section will be filled in from the regenerated `eval/RESULTS.md`.

**Code guards, measured offline (A3):** over 93 Nabd runs, grading the model's own final claim instead of the guarded
one changes the verdict in 4 runs. All 4 are `relapse` runs where the model claimed recovery and the code check caught
the relapse.

### Iteration log and false alarms

| Code version | Dev result | What changed in the code |
|---|---|---|
| `06e24115b472` | 23/24 | First dev round, before the fix: `relapse` rep 2 was a false recovery claim |
| `3edb406d0160` (= `nabd-frozen-v1`) | 24/24 | `agent.py`: a recovery claim must hold for ~15 s, checked in code |
| `6af255f330af` (= `nabd-frozen-v1.1`) | 8/8 | Only `service/app.py` and `service/pipeline.py` (held-out fault hooks); `agent.py` byte-identical |

**What these hashes are:**
- A **code version** is a hash of *every* frozen file (agent, monitor, service, eval grader), so it changes when any of
  them changes.
- The **agent fingerprint** (`365928b5883c`, the same for all three) covers only the prompt, the tool definitions, the
  guard function names and the model settings. It doesn't cover the code inside functions, which is why it didn't change
  with the relapse fix.
- What shows the agent is identical between v1 and v1.1 is the per-file SHA-256 in `eval/FROZEN.json` (and the
  `git diff` above).

**False alarms:** 0 alerts in 196 readings (10 min at 5 orders/s) and 0 in 98 readings (5 min at 20 orders/s).

## Design decisions and trade-offs

1. **Business invariants over health checks.** This is the core idea, and the reason detection is 21/21 instead of 0/21.
   The trade-off: the invariants have to be designed for each business. Here they are orders↔payments,
   enqueued↔processed, heartbeat and ledger↔orders.
2. **A rule-based monitor, an LLM only on alert.** It's cheap and predictable, and an LLM never "watches" a healthy
   system. The trade-off: a fault that breaks no rule is never investigated (the false-alarm window shows the rules
   are quiet when they should be).
3. **Read tools free, write tools need approval, a fixed allow-list.** There is no shell, no SQL and no arbitrary code,
   so the worst a wrong diagnosis can do is one approved, auditable change. Config changes and rollbacks are also
   written to the deploy history as `nabd-agent`.
4. **Safety rules enforced in code, not only asked for in the prompt:**
   - one fix per turn;
   - the same fix at most twice;
   - confidence below 0.6 escalates;
   - a step limit of 12;
   - a "recovered" claim must hold for ~15 s.

   The relapse results show why. The model itself claimed recovery in all 7 relapse runs: 3 before the fix and 4 on
   the frozen agent. The old 2 s check caught 2 of the first 3, and the 15 s check caught all 4.
5. **Money and figures computed by code.** SAR exposure and recovered amounts come from the database. The model's
   report prose is rejected if it contains any digit, so it can't invent a number.
6. **A hand-written loop, no agent framework.** `agent.py` is about 630 lines that can be read and explained line by
   line. The model is Claude Sonnet 5.5 at `medium` effort, with adaptive thinking (summaries shown in the dashboard),
   prompt caching, and a server-side refusal fallback. If the safety classifier wrongly declines, the API retries on a
   fallback model within the same call; a final refusal makes Nabd escalate. The trade-off: there's no off-the-shelf memory or planning; none is needed
   for one incident at a time.
7. **A simulated system.** Faults are reproducible and ground truth is exact (the eval reads the database directly).
   The trade-off: the results don't show transfer to real production systems.

## Limitations

These are specific, and come from reading every failed run ([`eval/REVIEW.md`](eval/REVIEW.md)).

- **The fix worked, but verification and escalation failed** (`bad_order_api_deploy`, 0/5).
  - **The fix:** in all five runs Nabd found the bad deploy and rolled it back within 4-9 s. The errors stopped. In every
    run the service confirmed it was back on v1.3, and the end-state check found no unpaid orders and no wrong ledger
    rows.
  - **Why it fails:** the service measures `error_rate` over the last 500 requests, which takes about 100 s to drain.
    Neither Nabd's checks nor the 15 s grader window saw green. And Nabd set neither "recovered" nor "escalate", so a
    fixed-but-unverified incident ended without a hand-off.
  - **Two lessons:** verification is only as good as the metric's time window, and "unverified" should always mean
    escalate. Both are left as found; the agent was frozen.
- **One unsafe experiment under pressure** (`provider_outage` rep 3). During a provider outage, Nabd tried a guessed
  URL "to test". That failed, so it reverted the URL and escalated. The other 4 runs changed nothing.
- **Incomplete diagnosis after a rejection** (`compound` + rejected first fix, 2 of 3 runs). Nabd escalated safely
  but named only the payment fault as a root cause, not the stuck worker.
- **The keyword grader can't read negation.** One run was failed for "blaming" a harmless change that Nabd had
  explicitly called "unrelated" (34/36 after reading, versus 33/36 automated).
- **A grading gap on lost orders.** A full service restart is graded as passing `worker_hang` but lost 26 queued orders.
  The grader checks unpaid and corrupted orders, not orders left queued forever.
- **On most held-out scenarios, a fixed runbook does as well.** In its single held-out run it handled every scenario
  except two:
  - `payment_deploy_bug`: Nabd 5/5, while the runbook fails (its table re-sets the URL instead of rolling back);
  - `bad_order_api_deploy`: both fail. Nabd fixes the problem but doesn't verify or escalate in time; the runbook has
    no rule for an error-rate alert.

  So on the faults tested here, Nabd's advantage over a fixed table is real but narrow, plus the explanation it gives.
- **Small N, one system, one model.** Per-scenario rates are indicative; claims rest on pooled rates with confidence
  intervals. The held-out scenarios were unseen by the agent, but designed by its builder.
- **Lost orders can't be replayed.** Orders dropped from the queue are lost: there is no requeue tool.

## Future work

- A time-based error rate, and a rule that "recovery not verified" always escalates (the first limitation).
- Live mode in the dashboard: inject a fault and press Approve/Reject in the browser (the CLI does this today).
- Structured root causes (cause + component fields) instead of keyword grading.
- A requeue tool for dropped orders, and a metric for orders stuck in `queued`.
- Held-out scenarios written by someone other than the builder, and more runs per scenario.
- Connect to real telemetry (Prometheus/OpenTelemetry) instead of the simulator's endpoints.

## How it was built

- **Solo:** Badr Hariri built Nabd as a solo entry over the hackathon.
- **AI coding tools:** Claude Code (Anthropic's AI coding assistant) was used throughout as a pair programmer. It drafted
  much of the code, the tests and the documentation from Badr's specifications.
- **Badr's part:**
  - set the requirements and the design rules;
  - made the evaluation decisions (what to freeze, which baselines, how to grade);
  - reviewed and ran everything;
  - wrote the held-out scenario ideas himself, after the freeze.
- **The runtime agent** is Claude Sonnet 5.5 via the Anthropic API.
- **Progress:** tracked step by step, with tests after every step. `tests/run_all.py` is the regression suite the eval
  depends on.
