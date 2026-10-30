# Tests

Plain Python scripts (no test framework). Run them from the project root with the venv active.

```powershell
python tests/run_all.py           # every check that needs no API key (~15 min)
python tests/run_all.py --live    # plus the real-API checks (~5 min, ~$0.40)
```

| Script | What it checks | Needs |
|---|---|---|
| `check_agent_mock.py` | Agent loop and safety rules, using scripted fake model replies | nothing |
| `check_scenarios.py` | Every chaos scenario, `reset`, default config templates | the service (started automatically) |
| `check_monitor.py` | Monitor rules on fake data, then live on every scenario | the service |
| `check_serious_fixes.py` | Regressions: safe PID check, port check, worker survives DB errors, atomic config | the service |
| `check_minor_fixes.py` | Regressions: config validation, endpoint keywords, clear errors, safe reset | the service |
| `check_eval.py` | Eval code: grader verdicts on scripted runs, ablations engage and restore, freeze check, resume | nothing |
| `check_ui.py` | Dashboard renders every saved run in replay mode (Streamlit AppTest, no server) | nothing |
| `check_hooks.py` | Held-out fault hooks (v1.5, v1.6, provider_outage) work when triggered and are off after reset | the service |
| `live/live_diagnose.py` | Recommend mode finds the right root cause and fix order for each scenario | API key |
| `live/live_act.py` | Act mode really recovers each scenario, claims are truthful, rejections escalate | API key |

The live checks skip themselves (and count as passed) if `ANTHROPIC_API_KEY` is not set.
Run a single scenario with e.g. `python tests/live/live_act.py compound`.

The checks reset the service on port 8000 (wiping the local database and logs) and leave it stopped.
Files are named `check_*.py` rather than `test_*.py` so that pytest does not try to run them on import.
