"""Shared setup for the live checks (they call the real Claude API and cost a few cents)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # the project root (this file is in tests/live/)
OUTPUT = Path(__file__).resolve().parent / "output"  # saved run records (git-ignored)
PY = sys.executable
SCENARIOS = ["payment_drop", "payment_drop_silent", "data_corruption", "worker_hang", "compound", "unknown"]
sys.path.insert(0, str(ROOT))


def skip_without_api_key() -> None:
    """Exit successfully (skipped) if there is no API key. Call this BEFORE importing agent."""
    load_dotenv(ROOT / ".env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("SKIPPED: no ANTHROPIC_API_KEY (put it in .env to run the live checks)")
        sys.exit(0)


def pick_scenarios() -> list[str]:
    """Scenarios named on the command line, or all of them."""
    chosen = [a for a in sys.argv[1:] if not a.startswith("--")] or SCENARIOS
    unknown = set(chosen) - set(SCENARIOS)
    if unknown:
        sys.exit(f"unknown scenario(s) {sorted(unknown)}; choose from {SCENARIOS}")
    return chosen


def reset() -> None:
    subprocess.run([PY, "chaos.py", "reset"], cwd=ROOT, capture_output=True)


def stop() -> None:
    subprocess.run([PY, "supervisor.py", "stop"], cwd=ROOT, capture_output=True)


def start_traffic() -> subprocess.Popen:
    return subprocess.Popen([PY, "chaos.py", "load", "900", "5"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def inject_and_wait(scenario: str) -> str:
    """Inject a fault, wait until the monitor raises it (like production would), return the alert text."""
    import chaos
    import monitor
    time.sleep(3)
    getattr(chaos, scenario)()
    found, waited = [], 0
    while not found and waited < 40:
        time.sleep(2)
        waited += 5
        found = monitor.check_once()
    print(f"  alert after ~{waited}s: {[a.rule for a in found]}")
    return monitor.format_alert(monitor.build_alert(found, monitor.snapshot()))


def show_progress(kind: str, data: dict) -> None:
    if kind == "tool_call" and data["tool"] != "submit_diagnosis":
        print(f"    -> {data['tool']}({json.dumps(data['args'])})")


def save(name: str, record: dict) -> None:
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / f"{name}.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")


def mentions(text: str, *words: str) -> bool:
    text = text.lower()
    return any(w.lower() in text for w in words)


# Words that identify each fix in a recommendation written by the model.
ACTIONS = {
    "url_fix": ("set_config", "payment_provider_url", "payments-v1"),
    "restart": ("restart_worker", "restart"),
    "reprocess": ("reprocess",),
    "rollback": ("rollback",),
    "repair": ("repair_ledger", "repair"),
}


def main_action(item: str) -> str | None:
    """Which fix a recommendation is mainly about: the one mentioned EARLIEST in its text.

    "Revert the URL first, so a worker restart doesn't..." is about the URL, not the restart,
    so just searching for the word "restart" anywhere in it would get the order wrong.
    """
    text = item.lower()
    best = None
    for name, words in ACTIONS.items():
        positions = [text.find(w) for w in words if w in text]
        if positions and (best is None or min(positions) < best[0]):
            best = (min(positions), name)
    return best[1] if best else None


def action_index(items: list[str], action: str) -> int | None:
    """Position of the first recommendation that is mainly about this action (None if none is)."""
    for i, item in enumerate(items):
        if main_action(item) == action:
            return i
    return None


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.total = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.total += 1
        self.passed += bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))

    def finish(self) -> None:
        print(f"\n{self.passed}/{self.total} checks passed")
        sys.exit(0 if self.passed == self.total else 1)
