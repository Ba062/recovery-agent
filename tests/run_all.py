"""Run all checks, one after another (they share the service on port 8000), and print a summary.

Usage, from the project root:
    python tests/run_all.py           every check that needs no API key (~15 min)
    python tests/run_all.py --live    also the real-API checks in tests/live/ (~5 min, ~$0.40)

Warning: the checks reset the service (wiping the local database and logs) and leave it stopped.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
CHECKS = [
    "check_agent_mock.py",
    "check_report_mock.py",
    "check_scenarios.py",
    "check_monitor.py",
    "check_serious_fixes.py",
    "check_minor_fixes.py",
    "check_money.py",
    "check_eval.py",
]
LIVE_CHECKS = ["live/live_diagnose.py", "live/live_act.py"]


def run(script: str) -> tuple[bool, str]:
    """Run one check script. Returns (passed, one-line summary)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # model text can contain characters cp1252 can't print
    start = time.time()
    proc = subprocess.run([sys.executable, str(TESTS / script)], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    output = proc.stdout + proc.stderr
    secs = f"{time.time() - start:.0f}s"
    if "SKIPPED" in output:
        return True, f"skipped ({output.strip().splitlines()[-1]})"
    failures = [line for line in output.splitlines() if line.startswith("FAIL")]
    counts = re.findall(r"(\d+)/(\d+) (?:checks )?passed", output)
    summary = f"{counts[-1][0]}/{counts[-1][1]} passed" if counts else "no summary line"
    if proc.returncode != 0 and not failures:
        failures = output.strip().splitlines()[-5:]  # crashed: show the end of the traceback
    passed = proc.returncode == 0 and bool(counts)
    return passed, f"{summary} in {secs}" + "".join(f"\n      {line}" for line in failures)


if __name__ == "__main__":
    scripts = CHECKS + (LIVE_CHECKS if "--live" in sys.argv else [])
    all_passed = True
    for script in scripts:
        print(f"{script:28} ...", end=" ", flush=True)
        passed, summary = run(script)
        all_passed &= passed
        print(("OK    " if passed else "FAILED") + "  " + summary)
    print("\nALL PASSED" if all_passed else "\nSOME CHECKS FAILED")
    sys.exit(0 if all_passed else 1)
