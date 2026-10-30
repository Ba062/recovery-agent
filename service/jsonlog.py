"""Tiny JSON-lines logger: one JSON object per line in service/logs/app.jsonl."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "logs" / "app.jsonl"
_lock = threading.Lock()  # several threads write to the same file


def log(level: str, component: str, msg: str, **fields) -> None:
    """Append one log record. level: DEBUG | INFO | WARNING | ERROR | CRITICAL."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "component": component,
        "msg": msg,
        **fields,
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock, LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
