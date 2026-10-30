"""Start / stop / restart the target service in the background (Windows-safe).

Usage:  python supervisor.py start | stop | restart | status
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import requests

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / "service" / "data" / "service.pid"
UVICORN_LOG = ROOT / "service" / "logs" / "uvicorn.log"
SERVICE_URL = os.getenv("SERVICE_URL", "http://127.0.0.1:8000")
PORT = SERVICE_URL.rsplit(":", 1)[-1]

# The live config and deploy history are git-ignored copies of these committed templates,
# so chaos experiments can never be committed by accident.
RUNTIME_FILES = {
    ROOT / "service" / "config.yaml": ROOT / "service" / "config.default.yaml",
    ROOT / "service" / "deploys.jsonl": ROOT / "service" / "deploys.default.jsonl",
}


def restore_runtime_files(overwrite: bool) -> None:
    """Copy the templates to the live files (only missing ones, unless overwrite=True)."""
    for live, default in RUNTIME_FILES.items():
        if overwrite or not live.exists():
            shutil.copyfile(default, live)


def running_process() -> psutil.Process | None:
    if not PID_FILE.exists():
        return None
    try:
        proc = psutil.Process(int(PID_FILE.read_text()))
        return proc if proc.is_running() else None
    except (psutil.NoSuchProcess, ValueError):
        return None


def wait_until_healthy(seconds: float = 10) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if requests.get(f"{SERVICE_URL}/health", timeout=1).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.3)
    return False


def start() -> str:
    if running_process():
        return "already running"
    restore_runtime_files(overwrite=False)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    UVICORN_LOG.parent.mkdir(parents=True, exist_ok=True)
    # uvicorn's output goes to a log file, NOT to our stdout: if the child inherited our stdout,
    # anyone capturing our output (agent.py, eval) would hang until the service exits.
    log_file = UVICORN_LOG.open("a", encoding="utf-8")
    # New process group on Windows, so Ctrl+C in this terminal doesn't kill the service.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "service.app:app", "--host", "127.0.0.1", "--port", PORT],
        cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    PID_FILE.write_text(str(proc.pid))
    if wait_until_healthy():
        return f"started (pid {proc.pid})"
    return f"started (pid {proc.pid}) but not healthy yet - check {UVICORN_LOG}"


def stop() -> str:
    proc = running_process()
    if proc is None:
        PID_FILE.unlink(missing_ok=True)
        return "not running"
    # On Windows the venv python.exe is a launcher that starts the real Python as a child,
    # so stop the children too or the server keeps running and holds the port.
    procs = proc.children(recursive=True) + [proc]
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        p.kill()
    PID_FILE.unlink(missing_ok=True)
    return f"stopped (pid {proc.pid})"


def status() -> str:
    proc = running_process()
    return f"running (pid {proc.pid})" if proc else "not running"


COMMANDS = {"start": start, "stop": stop, "status": status,
            "restart": lambda: f"{stop()}; {start()}"}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd not in COMMANDS:
        sys.exit(f"usage: python supervisor.py {' | '.join(COMMANDS)}")
    print(COMMANDS[cmd]())
