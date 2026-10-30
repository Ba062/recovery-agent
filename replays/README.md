# Showcase replays

Ten saved incidents for the dashboard's replay mode (`streamlit run ui/app.py`). Replay mode needs no service and no
API key.

**These are copies of eval runs, with incident reports added.**
- Each `NN_name.json` started as a byte-identical copy of a run in `eval/runs/` (the source run ID is in its `row.run_id`).
- The bilingual incident report was then generated for it: the model wrote the prose, and code inserted every figure.
  - The report is saved next to it as `NN_name.report.md`.
  - The prose is also stored in the copy's `agent_record.report`, so it can be re-rendered offline.
- The originals in `eval/runs/` were not changed. The eval's verdicts and ground truth inside each copy are the
  originals'.
- Generating the 10 reports cost $0.30. Four reports needed a second attempt, because the report checker rejected
  digits or doubled nouns in the first draft.

| Replay | Source run | What it shows |
|---|---|---|
| `01_payment_drop` | `dev_payment_drop_normal_nabd_r1_6af255` | Silent payment drop: URL reverted, unpaid orders reprocessed |
| `02_payment_drop_silent` | `dev_payment_drop_silent_normal_nabd_r1_3edb40` | Same fault with no deploy-history entry: found from DEBUG and config logs |
| `03_data_corruption` | `dev_data_corruption_normal_nabd_r1_6af255` | Bad deploy corrupts the ledger: rollback, then repair |
| `04_worker_hang` | `dev_worker_hang_normal_nabd_r1_6af255` | Stuck worker: restarted, queue drained |
| `05_compound` | `dev_compound_normal_nabd_r1_6af255` | Two faults at once: payments fixed before the worker restart |
| `06_unknown_escalates` | `dev_unknown_normal_nabd_r1_6af255` | Read-only database, no tool can fix it: escalates without guessing |
| `07_healthy_no_action` | `dev_healthy_normal_nabd_r1_6af255` | No fault, human report: checks and changes nothing |
| `08_relapse_code_guard_overrides` | `dev_relapse_normal_nabd_r1_6af255` | The model claims recovery; Nabd's 15 s code check sees the relapse and escalates |
| `09_rejected_fix_escalates` | `dev_payment_drop_reject_first_nabd_r1_3edb40` | The human rejects the fix: no riskier workaround, escalates with the fix recommended |
| `10_heldout_payment_deploy_bug` | `heldout_payment_deploy_bug_normal_nabd_r1_6af255` | Held-out: a deploy breaks payments with the URL correct; rollback, then reprocess (the runbook fails this) |

The two code versions in the run IDs (`3edb40`, `6af255`) are the same agent. Only fault hooks in the service differ;
see `eval/HELDOUT.md`.
