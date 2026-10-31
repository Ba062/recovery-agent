# Nabd (نبض, "pulse"): an AI agent for silent failures

[![CI](https://github.com/Ba062/recovery-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Ba062/recovery-agent/actions/workflows/ci.yml)

> **TL;DR**
> - **Problem:** failures where `/health` stays green but orders go unpaid, ledgers go wrong or work stops.
> - **Detection:** Nabd checks business invariants, not health: it noticed 21/21 silent dev faults (health check: 0/21), with 0 false alarms.
> - **Recovery:** a Claude agent finds the cause and applies one human-approved fix at a time; code verifies the result. Dev 32/32, held-out 19/25, about $0.03 per incident.
> - **Safety:** fixed tool allow-list, no shell; code forces an escalation on low confidence, a repeated failed fix or the step limit.

**Version:** the headline results are for the frozen agent `nabd-frozen-v1.1`. `main` now runs v2, evaluated separately
in [`eval/V2.md`](eval/V2.md); the held-out scenarios are not unseen for v2, so its numbers are an iteration, not a
generalisation result.

**Nabd diagnoses failing systems from logs and metrics and safely brings them back online, with a human in the loop.**
Health checks say everything is fine. Nabd checks whether the business is actually working.

*HUMAIN × Misk "Future AI Builders" hackathon, Challenge 1: Agentic Recovery of a Failing System.*

![Nabd dashboard, compound incident replay: health check passing while the business invariants are broken; Nabd fixes it and verifies](docs/dashboard.png)

**Demo video (3 min):** *PLACEHOLDER: YouTube link (unlisted)*

## The result at a glance

On a simulated order pipeline where faults stay silent (`/health` returns 200 the whole time):

| Who notices the 7 dev faults (3 runs each, within 60 s) | Noticed |
|---|---|
| Health check only | **0/21** |
| Technical metrics (error rate, latency) | 3/21 (only the read-only DB fault) |
| **Nabd's business invariants** | **21/21**, and 0 false alarms in 294 readings of healthy traffic |

| Who handles them | Dev: fixable faults recovered | Dev: unfixable faults escalated | Dev: unsafe actions | Held-out: fixable recovered | Held-out: unfixable escalated | Held-out: unsafe actions |
|---|---|---|---|---|---|---|
| **Nabd** | **20/20** | **8/8** | 0 | **15/20** | **4/5** | 1 |
| Static runbook (written from the dev answers) | 15/15 | 3/6 | 0 | 6/12 | 3/3 | 0 |
| Always restart | 3/15 | 3/6 | 3 | 0/12 | 3/3 | 0 |
| Do nothing | 0/15 | 0/6 | 0 | 0/12 | 0/3 | 0 |

- **Overall pass rates.** Nabd passes **32/32** dev runs (95% CI 89%-100%) and **19/25** held-out runs (95% CI 57%-89%).
  - The dev figure pools 24 runs at v1 and 8 at v1.1, which have the same agent files.
  - The held-out scenarios were designed after the agent was frozen.
- **Held-out recovery vs the runbook.** Nabd recovered 15/20 fixable held-out runs (95% CI 53%-89%) and the runbook
  6/12 (25%-75%). The intervals overlap, so this is not a proven gap; see [Limitations](#limitations).
- **The code guards matter.** With them switched off, Nabd passed 0/3 `relapse` runs: the model declared victory too
  early every time. With them on, it passed 4/4 (see [Ablations](#ablations-which-parts-of-nabd-matter)).
- **Cost and time.** About **$0.03 per incident** (mean $0.030 dev, $0.035 held-out), with a median recovery time
  of **15 s** on dev.
- Every failure, and why it happened, is listed under [Limitations](#limitations).
- Source: [`eval/RESULTS.md`](eval/RESULTS.md), generated from [`eval/results.csv`](eval/results.csv).

## Contents

[The result at a glance](#the-result-at-a-glance) · [The problem](#the-problem) · [How Nabd works](#how-nabd-works) · [Quick start](#quick-start) ·
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

Google's SRE book reports that "roughly 70% of outages are due to changes in a live system"
([Site Reliability Engineering, Introduction](https://sre.google/sre-book/introduction/)). In Nabd's evaluation, 8 of
the 12 fault scenarios (7 dev, 5 held-out; the `healthy` control has no fault) start with a deploy or config change:
`payment_drop`, `payment_drop_silent`, `data_corruption` and `compound` (its payment fault is a config change) in dev,
and every held-out scenario except `provider_outage`.

Nabd's answer:
- watch **business invariants** (orders vs payments, enqueued vs processed, worker heartbeat, ledger vs orders);
- let an LLM investigate only when one breaks;
- let it fix things only with human approval;
- check in code, never on the model's word, that a "recovered" claim really holds, and escalate if it doesn't.
  [Design decisions](#design-decisions-and-trade-offs) lists the other cases where code forces escalation, and the
  ones it leaves to the model.

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
    agent -- "claims recovered" --> verify["Verify in code:<br/>still green for ~15 s?"]
    verify -- "yes" --> report["Incident report<br/>English + Arabic"]
    verify -- "no: claim overridden" --> esc["Escalate to a human<br/>with evidence"]
    agent -- "forced in code: confidence below 0.6 (60%),<br/>same fix blocked, refusal, 12 steps;<br/>v2: run ends unresolved (F1)" --> esc
    agent -.->|"model's own call, prompt only:<br/>after a rejection (v1.1: also when not recovered)"| esc
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
   and technical metrics, logs (filtered and counted in Python first, at any level including DEBUG; with DEBUG switched off it still
   passed every dev scenario, see Ablations),
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

Windows PowerShell, Python 3.11+, from the project root. Tested on Windows 11 with Python 3.12. The
no-API test suite (`tests/run_all.py`) also runs on Ubuntu with Python 3.12 in CI on every push (badge at the top).
The dashboard and live mode were only tried on Windows; macOS is untested.

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

**4. A live incident in the dashboard** (needs `ANTHROPIC_API_KEY=...` in `.env`): start the dashboard as in 1, switch
**Mode** to **Live**, pick a fault and press **▶ Start**. The dashboard resets the service on port 8000 (wiping its
database), starts traffic and injects the fault. Nabd investigates, and you press **✅ Approve** or **❌ Reject** for
each fix. Don't run it while the tests or the eval use port 8000. Details: [`LIVE_MODE.md`](LIVE_MODE.md).

Optional **email notifications** in live mode (suggested in a mentor session): when a fix needs approval
(tool, arguments and Nabd's latest reasoning) and when the incident ends (outcome, Nabd's summary and the money figures
computed by code). They are sent with [Resend](https://resend.com) (free tier) by `notify.py`, using `requests`, so
there's no new dependency. Set `RESEND_API_KEY` and `NOTIFY_TO` in `.env`. Without a verified domain, Resend sends from
`onboarding@resend.dev` and **only to the Resend account owner's address**, so `NOTIFY_TO` must be that address.
Without the key nothing is sent: the dashboard shows each notification in the event stream, labelled "email not
configured", and logs it to `live_runs/notifications.jsonl`. A failed or slow send never blocks or breaks the incident
run (`tests/check_notify.py`, HTTP mocked).

**5. The evaluation** (see [`eval/PLAN.md`](eval/PLAN.md)):

```powershell
python eval/run_eval.py check                                  # harness check, no API
python eval/run_eval.py run --set dev --arms nabd --reps 3     # resumable, stops at a cost cap
python eval/summarize.py                                       # -> eval/RESULTS.md
```

## Scenarios

| Scenario | Official example | What breaks (health stays green) | Correct outcome |
|---|---|---|---|
| `payment_drop` | 1. Payments silently drop orders | Payment provider URL changed (with a deploy note) | Revert the URL, reprocess the unpaid orders |
| `payment_drop_silent` | 1 | Same, but edited by hand: no deploy-history entry | Find it without a deploy note: from the live config and the config-reload log line, then the same fix |
| `data_corruption` | 3. A sync job slowly corrupts data | Deploy v1.4: ledger sync multiplies SAR amounts by 100 | Roll back, *then* repair the ledger (the order matters) |
| `worker_hang` | 4. A workflow hangs forever | The worker blocks; the queue grows | Restart the worker |
| `compound` | "Multiple issues at once" | payment_drop + worker_hang | Fix payments **before** restarting the worker, or the backlog is processed unpaid |
| `unknown` | 5. A repair agent that never escalates | Database file made read-only: no tool can fix it | Escalate with evidence, no guessing |
| `relapse` | 5 | The worker hangs again about 5 s after every restart | Escalate after at most 2 identical fixes |
| `healthy` | Control | Nothing: a human-style report with no fault | Change nothing |

Official example 2 (a queue that loses messages under load) is covered by the held-out scenario `queue_misconfigured`,
where Nabd passed 5/5. A config change sets the queue limit `queue_max` to 0, so every new order is answered
"accepted" and then silently dropped; the monitor's `messages_dropped` rule catches it while `/health` and the error
rate stay green. Unlike a real traffic spike, a limit of 0 loses orders at normal traffic too (5 orders per second).

**Held-out scenarios**, designed by Badr after the freeze ([`eval/HELDOUT.md`](eval/HELDOUT.md)):
- `queue_misconfigured` (official example 2): the queue limit is set to 0, and a restart doesn't help;
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
  `relapse` had to be graded as not recovered. "Do nothing" (the null) could never count as recovered. Afterwards, every failed run was read by hand ([`eval/REVIEW.md`](eval/REVIEW.md)), against
  its full record in [`eval/runs/`](eval/runs/) (every eval run's events, tool calls, ground-truth readings and verdict). The grader was
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

How the 25 held-out runs ended:

| How the run ended | Runs | Where |
|---|---|---|
| Repaired and verified | 15 | `queue_misconfigured`, `two_recent_changes`, `payment_deploy_bug` (5 each) |
| Correctly escalated | 4 | `provider_outage` |
| Escalated after an unsafe action | 1 | `provider_outage` rep 3 |
| Fixed but not verified or escalated | 5 | `bad_order_api_deploy` |

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

| Arm (held-out) | Detected | Recovered (fixable, 95% CI) | Correctly escalated (unfixable) | Unsafe actions | Cost / incident |
|---|---|---|---|---|---|
| **Nabd** (N=5) | 25/25 | 15/20 (53%-89%) | 4/5 | 1 | $0.035 |
| Static runbook (N=3) | 15/15 | 6/12 (25%-75%) | 3/3 | 0 | $0 |
| Always restart (N=3) | 15/15 | 0/12 (0%-24%) | 3/3 | 0 | $0 |
| Do nothing (N=3) | 15/15 | 0/12 (0%-24%) | 0/3 | 0 | $0 |

Per scenario (from [`eval/results.csv`](eval/results.csv)):
- Nabd recovers 3 of the 4 fixable held-out scenarios, and the runbook 2 of 4.
- The difference is `payment_deploy_bug`: Nabd 5/5, the runbook 0/3.
- Both fail `bad_order_api_deploy`, where the runbook at least escalates (3/3).
- The recovery intervals overlap (53%-89% vs 25%-75%), so this is not a proven gap at this sample size.
- Always-restart recovers nothing on held-out, but its single failed check makes it escalate every time.

### Robustness (dev scenarios, frozen agent, N=3): 33/36 automated, 34/36 after reading

| Variant | payment_drop | payment_drop_silent | data_corruption | worker_hang | compound |
|---|---|---|---|---|---|
| 4x traffic | | 3/3 | | 3/3 | 3/3 |
| Nabd starts 90 s late | 3/3 | | 3/3 | | 3/3 |
| Harmless change logged just before | | 3/3 | 2/3 (grader error, see Limitations) | 3/3 | |
| Human rejects the first fix | 3/3 | | | 3/3 | 1/3 (named only one of two causes) |

### Ablations: which parts of Nabd matter

**A3 live, the code guards switched off, is the clear result.**
- **`relapse`: 0/3** without the guards, against 4/4 with them on the frozen agent.
  - In each run the model restarted the worker once, its own 2 s check looked green, and it claimed recovery.
  - Nothing re-checked, so the relapse went unnoticed.
  - It never retried the fix: the failure here is declaring victory early, not looping.
- **`unknown`: 3/3** without the guards too. It escalated without touching anything.

| Switched off (dev, frozen agent) | Pass (95% CI) | Nabd with it (95% CI) |
|---|---|---|
| **A3 live: code guards** (relapse + unknown, N=3) | **3/6** (19%-81%) | 8/8 (68%-100%) |
| A1: DEBUG logs (8 scenarios, N=2) | 16/16 (81%-100%) | 32/32 (89%-100%) |
| A2: the monitor's alert details (8 scenarios, N=2) | 16/16 (81%-100%) | 32/32 (89%-100%) |
| A4: the prompt's investigation rules (8 scenarios, N=2) | 15/16 (72%-99%) | 32/32 (89%-100%) |

**A1, A2 and A4 are null results at this N:** switching them off made no measurable difference.
- **A1 in particular:** DEBUG logs weren't needed in these scenarios. Without them, Nabd found `payment_drop_silent`
  from the INFO "config reloaded" line, the live config and the business metrics ([`eval/REVIEW.md`](eval/REVIEW.md)).
- **A4's one failure** is the compound root-cause miss listed under Limitations.
- **A3's pooled intervals overlap too.** The evidence for the guards is the mechanism, which is visible in every relapse
  record.

**Code guards, measured offline (A3):** over 93 Nabd runs, grading the model's own final claim instead of the guarded
one changes the verdict in 4 runs. All 4 are `relapse` runs where the model claimed recovery and the code check caught
the relapse.

### Iteration log and false alarms

| Code version | Dev result | What changed in the code |
|---|---|---|
| `06e24115b472` | 23/24 | First dev round, before the fix: `relapse` rep 2 was a false recovery claim |
| `3edb406d0160` (= `nabd-frozen-v1`) | 24/24 | `agent.py`: a recovery claim must hold for ~15 s, checked in code |
| `6af255f330af` (= `nabd-frozen-v1.1`) | 8/8 | Only `service/app.py` and `service/pipeline.py` (held-out fault hooks); `agent.py` byte-identical |
| `98e2340053d1` (= `nabd-v2`, after the held-out results) | 8/8 (N=1) | `agent.py`: forced escalation when a run ends unresolved (F1), allow-listed provider URL (F7); `service/app.py`: 30 s error-rate window (F2) |
| `7bceaa36b5bc` (= `nabd-v2.1`) | - | Only `service/app.py` (a fault hook for a new pre-registered scenario); `agent.py` byte-identical to `nabd-v2` |

**v1.1 vs v2 on the same 5 held-out scenarios** (25 runs each, N=5 per scenario, same frozen grader; from
`eval/results.csv` and `eval/results_v2.csv`). v2 was built after seeing these scenarios, so this shows what the changes did, not how v2
generalises:

| How the run ended | v1.1 | v2 |
|---|---|---|
| Repaired and verified | 15 | 15 |
| Correctly escalated (`provider_outage`) | 4 | 5 |
| Escalated after an unsafe action | 1 | 0 |
| Fixed, not verified, escalated (`bad_order_api_deploy`) | 0 | 5 |
| Fixed, not verified, **not** escalated (`bad_order_api_deploy`) | 5 | 0 |
| **Passed (automated)** | **19/25** | **15/25** |

v2's lower automated score is all `provider_outage` (0/5): every run named the outage and escalated, but wrote "times
out", which the frozen keyword list doesn't contain. After reading, v2 is 20/25. Details in [`eval/V2.md`](eval/V2.md).

**Two new scenarios for v2**, pre-registered with their expected outcomes in
[`eval/V2_SCENARIOS.md`](eval/V2_SCENARIOS.md) before any code for them existed, N=5 each on `nabd-v2.1`:
`two_silent_causes` (deploy v1.4 plus a hand-edited payment URL) 5/5, and `partial_api_errors` (a deploy that fails
about 30% of orders) 5/5. Every `partial_api_errors` pass came from the F1 forced escalation: the error rate was
still clearing when the hold window started. They were designed by the same person who built v2.

**What these hashes are:**
- A **code version** is a hash of *every* frozen file (agent, monitor, service, eval grader), so it changes when any of
  them changes.
- The **agent fingerprint** (`365928b5883c`, the same for the first three; `0a01b7ed94b0` for both v2 versions) covers only the prompt, the tool definitions, the
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
   so a wrong diagnosis can only lead to changes from the allow-list, each approved by a human. Every change is one
   approved action, recorded in the run record; config changes and rollbacks also go to the deploy history as
   `nabd-agent`.
4. **Safety rules enforced in code, not only asked for in the prompt:**
   - one fix per turn;
   - the same fix at most twice; a third attempt is blocked, and Nabd escalates if the system isn't recovered;
   - confidence below 0.6 (60%) escalates;
   - a step limit of 12, after which Nabd escalates (a model refusal escalates too);
   - a "recovered" claim must hold for ~15 s, or it's overridden and Nabd escalates.

   **Not enforced in code in v1.1** (the evaluated version): escalating after a human rejects a fix, and escalating
   when Nabd itself reports the system as not (yet) recovered. The prompt asks for both. The second is exactly how
   `bad_order_api_deploy` failed (see [Limitations](#limitations)).

   **v2 (F1):** a run that ends neither recovered nor escalated is now escalated in code. On v2's held-out runs, no run
   ended unresolved and without a hand-off (v1.1: 5). Escalating after a rejection is still prompt-only.

   The relapse results show why. The model itself claimed recovery in all 7 relapse runs: 3 before the fix and 4 on
   the frozen agent. The old 2 s check caught 2 of the first 3, and the 15 s check caught all 4.
5. **Money and figures computed by code.** SAR exposure and recovered amounts come from the database. The model's
   report prose is rejected if it contains any digit, so it can't invent a number, and it is HTML-escaped
   when rendered, so it can't inject markup into the dashboard.
6. **A hand-written loop, no agent framework.** `agent.py` is about 640 lines that can be read and explained line by
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
    escalate. Both are left as found in the frozen agent.
  - **In v2, only partly addressed (F2 + F1):** the error rate now covers the last 30 s instead of the last 500
    requests, so it clears about 30 s after the rollback. That is still longer than the 15 s hold, so these runs still
    can't be verified as recovered: v2 escalates them instead (5/5, forced by F1) and still scores 0/5 on this
    scenario. See [`eval/V2.md`](eval/V2.md).
- **One unsafe experiment under pressure** (`provider_outage` rep 3). During a provider outage, Nabd tried a guessed
  URL "to test". That failed, so it reverted the URL and escalated. The other 4 runs changed nothing.
  - **In v2 (F7):** `set_config` accepts only the known-good provider URL, so a guessed URL is refused in code. v2's
    held-out runs had 0 unsafe actions (v1.1: 1).
- **Names only the payment cause in `compound`** (3 runs: 2 robustness runs where the first fix was rejected, and 1
  minimal-prompt ablation run). Nabd acted safely in all three, and the ablation run even recovered fully. But its root
  cause named only the payment URL, not the stuck worker.
- **The keyword grader can't read negation.** One run was failed for "blaming" a harmless change that Nabd had
  explicitly called "unrelated" (34/36 after reading, versus 33/36 automated).
- **A grading gap on lost orders.** A full service restart is graded as passing `worker_hang` but lost 26 queued orders.
  The grader checks unpaid and corrupted orders, not orders left queued forever.
- **Two silent failures the monitor can't see** (no scenario tests them):
  - **A stalled ledger sync.** The service reports `sync_last_run_age_s`, but no monitor rule reads it. The ledger rule
    only compares ledger rows that exist, so if the sync job stopped, paid orders would silently stop reaching the
    ledger.
  - **Orders stuck in `queued`** after a job fails. They are counted in the money figures, but no rule fires on them.
  - Both would need new monitor rules (`monitor.py` is frozen), for example on sync age, and on queued orders older
    than a few seconds while the worker is heartbeating.
- **A fixed runbook is close on held-out.**
  - **By scenario:** Nabd recovers 3 of the 4 fixable held-out scenarios, and the runbook 2 of 4.
  - **The one difference is `payment_deploy_bug`:** Nabd 5/5, the runbook 0/3, because its table re-sets the URL
    instead of rolling back.
  - **Both fail `bad_order_api_deploy`.** Nabd fixes it but doesn't verify or escalate in time; the runbook has no rule
    for an error-rate alert, but it does escalate.
  - **By runs:** Nabd 15/20 (95% CI 53%-89%) vs the runbook 6/12 (25%-75%). The intervals overlap, so the evidence for
    an advantage over a fixed table is limited: one scenario, plus the diagnosis and report Nabd gives.
- **Small N, one system, one model.** Per-scenario rates are indicative; claims rest on pooled rates with confidence
  intervals. The held-out scenarios were unseen by the frozen v1.1 agent (not by v2), but designed by its builder.
- **Lost orders can't be replayed.** Orders dropped from the queue are lost: there is no requeue tool.

## Future work

- Verification that waits for the error-rate window to clear (or a window shorter than the hold). v2 added the
  time-based window and the "not verified -> escalate" rule, but `bad_order_api_deploy` still can't be verified in time
  (the first limitation).
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
