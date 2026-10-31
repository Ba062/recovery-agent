"""Live mode for the dashboard: run a real incident end to end, with a human approving each fix.

    reset the service -> start traffic -> inject a fault -> watch the monitor
    -> agent.run_agent (unchanged) -> final check -> bilingual report

Streamlit re-runs the page script on every click, so the incident can't run inside the page.
It runs in a background thread instead (LiveRun). The page and the thread share the LiveRun object:
- the thread appends events (pipeline steps and everything the agent does);
- for each fix, the agent calls approve(), which posts a request and WAITS on a queue;
- the page shows Approve / Reject buttons and puts the answer on that queue.

Only one live run at a time: it owns the service on port 8000. No Streamlit code in this file,
so it can be tested on its own (tests/check_ui.py).
"""

import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "eval"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
import agent       # noqa: E402  (used unchanged: run_agent with our approve/on_event callbacks)
import chaos       # noqa: E402
import monitor     # noqa: E402
import report      # noqa: E402
import scenarios   # noqa: E402  (eval/scenarios.py: the dev faults; inject() may return a stop function)

LIVE_DIR = ROOT / "live_runs"   # records + reports of live runs (git-ignored)
TRAFFIC_RATE = 5                 # orders per second, as in the eval
TRAFFIC_SECONDS = 900            # long enough for any run; killed when the run ends
WARMUP_S = 3
POLL_S = 3                       # monitor reading interval, as in the eval
DETECT_TIMEOUT_S = 60
APPROVAL_TIMEOUT_S = 600         # no answer in 10 minutes = treated as a rejection

FAULTS = {
    "payment_drop": "Payment drop: payment provider URL changed (with a deploy note)",
    "payment_drop_silent": "Payment drop, silent: URL changed by hand, no deploy note",
    "data_corruption": "Data corruption: deploy v1.4 multiplies SAR in the ledger",
    "worker_hang": "Worker hang: the queue stops moving",
    "compound": "Compound: payment drop + worker hang",
    "unknown": "Unknown: database made read-only (no tool can fix it)",
    "relapse": "Relapse: the worker hangs again after every restart",
}
STAGES = ["Reset service", "Start traffic", "Inject fault", "Monitor", "Nabd investigates", "Final check", "Report"]


class Cancelled(Exception):
    """The human pressed Stop."""


# ---------------------------------------------------------------- steps (module-level, so tests can replace them)
def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))  # agent.py has already loaded .env


def reset_service() -> str:
    return chaos.reset()  # stop, wipe DB + logs, restore config + deploy history, start


def start_traffic() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "chaos.py", "load", str(TRAFFIC_SECONDS), str(TRAFFIC_RATE)], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def inject(fault: str):
    return scenarios.DEV[fault].inject()


def run_agent(alert: str, approve, on_event):
    return agent.run_agent(alert, approve=approve, on_event=on_event)


def save_run(record: dict, fault: str) -> Path:
    """Write the record and its bilingual report (one extra API call) to live_runs/. Returns the record path."""
    LIVE_DIR.mkdir(exist_ok=True)
    stem = LIVE_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_{fault}"
    report.save(record, stem)  # writes <stem>.report.md and adds record["report"]
    path = stem.with_suffix(".json")
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return path


# ---------------------------------------------------------------- Nabd's reasoning, for the approval prompt
def latest_reasoning(events: list[dict]) -> dict | None:
    """When Nabd asks for a fix: the text of its last "thinking" (summarized reasoning) or "thought" (its own words)
    event, and how many tool calls it made after writing it, not counting the fix itself (0 = written for this fix).
    With adaptive thinking the model often skips reasoning on simple steps, so it can be about an earlier step."""
    calls = 0  # tool_call events walking back; the first one is the fix's own call (agent.py emits it, then approves)
    for event in reversed(events):
        if event["kind"] in ("thinking", "thought"):
            return {"text": event["text"].strip(), "calls_after": max(calls - 1, 0)}
        if event["kind"] == "tool_call":
            calls += 1
    return None


# ---------------------------------------------------------------- one live run
class LiveRun:
    def __init__(self, fault: str) -> None:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}")
        self.fault = fault
        self.started = time.time()
        self.lock = threading.Lock()   # the page thread and the run thread both touch the fields below
        self.events: list[dict] = []
        self.stage = STAGES[0]
        self.pending: dict | None = None  # awaiting the human: {"id", "tool", "args", "reasoning", "reply", "answered"}
        self.ids = itertools.count(1)
        self.cancelled = threading.Event()
        self.alert: str | None = None
        self.record: dict | None = None
        self.after: list[tuple[str, str]] | None = None  # (rule, message) still broken at the final check
        self.saved: Path | None = None
        self.error: str | None = None
        self.done = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="nabd-live")

    # ---- shared state
    def _event(self, kind: str, **data) -> None:
        with self.lock:
            self.events.append({"t": round(time.time() - self.started, 1), "kind": kind, **data})

    def _stage(self, name: str) -> None:
        if self.cancelled.is_set():
            raise Cancelled()
        with self.lock:
            self.stage = name
        self._event("stage", text=name)

    def view(self) -> dict:
        """A consistent copy of the state for the page to draw."""
        with self.lock:
            pending = None if self.pending is None or self.pending["answered"] else {
                k: self.pending[k] for k in ("id", "tool", "args", "reasoning")}
            return {"stage": self.stage, "events": list(self.events), "pending": pending, "alert": self.alert,
                    "record": self.record, "after": self.after, "saved": self.saved, "error": self.error,
                    "done": self.done, "cancelled": self.cancelled.is_set()}

    # ---- approvals: called by the agent thread / by the page
    def approve(self, tool: str, args: dict) -> bool:
        """agent.run_agent calls this for every fix. Blocks until the human answers (or Stop / timeout)."""
        request = {"id": next(self.ids), "tool": tool, "args": args, "reply": queue.Queue(maxsize=1), "answered": False}
        with self.lock:  # checked and posted under the lock Stop takes, so a Stop can't slip in between
            request["reasoning"] = latest_reasoning(self.events)
            stopped = self.cancelled.is_set()
            if not stopped:
                self.pending = request
        if stopped:
            self._event("approval", tool=tool, args=args, approved=False, how="run stopped")
            return False
        try:
            approved = request["reply"].get(timeout=APPROVAL_TIMEOUT_S)
            how = "by you"
        except queue.Empty:
            approved, how = False, f"no answer in {APPROVAL_TIMEOUT_S // 60} min"
        with self.lock:
            self.pending = None
        self._event("approval", tool=tool, args=args, approved=approved, how=how)
        return approved

    def answer(self, request_id: int, approved: bool) -> bool:
        """The page calls this when Approve / Reject is clicked. A stale or repeated click is ignored."""
        with self.lock:
            request = self.pending
            if request is None or request["id"] != request_id or request["answered"]:
                return False
            request["answered"] = True
        request["reply"].put(bool(approved))  # each request has its own queue, so a late answer can't leak
        return True

    def cancel(self) -> None:
        """Stop: reject the fix being asked about (if any) and every later one; skip the remaining steps."""
        with self.lock:
            self.cancelled.set()
            request = self.pending
            if request is not None and not request["answered"]:
                request["answered"] = True
            else:
                request = None
        if request is not None:
            request["reply"].put(False)

    # ---- the pipeline (runs in self.thread)
    def _run(self) -> None:
        traffic, stop_fault = None, None
        try:
            self._stage("Reset service")
            self._event("note", text=reset_service())
            self._stage("Start traffic")
            traffic = start_traffic()
            time.sleep(WARMUP_S)
            self._stage("Inject fault")
            stop_fault = inject(self.fault)
            self._event("note", text=f"Injected: {FAULTS[self.fault]}")
            self._stage("Monitor")
            self.alert = self._watch()
            if self.alert is None:
                self._event("note", text=f"No alert within {DETECT_TIMEOUT_S} s: nothing for Nabd to investigate.")
                return
            self._stage("Nabd investigates")
            _, record = run_agent(self.alert, self.approve,
                                  lambda kind, data: self._event(kind, **data))
            with self.lock:
                self.record = record
            self._stage("Final check")
            after = [(a.rule, a.message) for a in monitor.check_once(POLL_S)]
            with self.lock:
                self.after = after
            self._stage("Report")
            saved = save_run(record, self.fault)
            with self.lock:
                self.saved = saved
            self._event("note", text=f"Saved to {saved.parent.name}/{saved.name} (+ incident report).")
        except Cancelled:
            self._event("note", text="Stopped by you. Remaining steps skipped.")
        except Exception as e:  # shown on the page; never lost in a background thread
            with self.lock:
                self.error = f"{type(e).__name__}: {e}"
        finally:
            if stop_fault:
                stop_fault()
            if traffic is not None:
                traffic.kill()
            with self.lock:
                self.done = True

    def _watch(self) -> str | None:
        """Read the monitor like production does until it alerts. Returns the alert text, or None."""
        prev = monitor.snapshot()
        deadline = time.time() + DETECT_TIMEOUT_S
        reading = 0
        while time.time() < deadline:
            if self.cancelled.is_set():
                raise Cancelled()
            time.sleep(POLL_S)
            cur = monitor.snapshot()
            reading += 1
            found = monitor.evaluate(prev, cur)
            health = "passing" if cur["health_ok"] else "FAILING"
            if found:
                self._event("note", text=f"Monitor reading {reading}: ALERT ({', '.join(a.rule for a in found)}). "
                                         f"/health is {health}.")
                return monitor.format_alert(monitor.build_alert(found, cur))
            self._event("note", text=f"Monitor reading {reading}: business invariants green, /health {health}.")
            prev = cur
        return None


# ---------------------------------------------------------------- the one run of this dashboard process
_current: LiveRun | None = None
_current_lock = threading.Lock()


def current() -> LiveRun | None:
    return _current


def start(fault: str) -> LiveRun:
    """Start a live run, unless one is already running (it would fight over the service)."""
    global _current
    with _current_lock:
        if _current is not None and not _current.done:
            raise RuntimeError("A live run is already in progress.")
        _current = LiveRun(fault)
        _current.thread.start()
        return _current
