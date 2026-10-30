"""Fault injector and traffic generator for the target service.

Usage:
    python chaos.py load [seconds] [orders_per_second]   keep orders flowing (run in a 2nd terminal)
    python chaos.py payment_drop     bad payment provider URL   -> orders silently go unpaid
    python chaos.py worker_hang      worker blocks forever       -> queue silently grows
    python chaos.py compound         payment_drop + worker_hang at once
    python chaos.py unknown          DB file made read-only      -> no tool can fix it, must escalate
    python chaos.py reset            stop service, wipe DB + logs, restore default config, start again

Every fault except `unknown` keeps /health green on purpose.
"""

import json
import os
import random
import stat
import sys
import time
from datetime import datetime, timezone

import requests
import yaml

import supervisor
from service.db import DB_PATH
from service.jsonlog import LOG_FILE

SERVICE_URL = os.getenv("SERVICE_URL", "http://127.0.0.1:8000")
CONFIG_FILE = supervisor.ROOT / "service" / "config.yaml"
DEPLOY_LOG = supervisor.ROOT / "service" / "deploys.jsonl"
BAD_PROVIDER_URL = "internal://payments-v2"


# ---------------------------------------------------------------- helpers
def set_config(key: str, value) -> object:
    """Change one config key (the service hot-reloads it within ~2s). Returns the old value."""
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    old = cfg.get(key)
    cfg[key] = value
    CONFIG_FILE.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return old


def add_deploy_entry(change: str, by: str = "ops") -> None:
    """Record a change in the deploy history, like a real CI/ops system would."""
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "version": cfg.get("version"), "change": change, "by": by}
    with DEPLOY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------- faults
def payment_drop() -> str:
    """Official example 1: payments silently drop. Looks like a routine provider migration."""
    old = set_config("payment_provider_url", BAD_PROVIDER_URL)
    add_deploy_entry(f"config: payment_provider_url {old} -> {BAD_PROVIDER_URL} (provider migration)")
    return f"payment_drop injected: payment_provider_url {old} -> {BAD_PROVIDER_URL}"


def worker_hang() -> str:
    """Official example 4: the worker freezes. No config change, no deploy: only metrics show it."""
    r = requests.post(f"{SERVICE_URL}/admin/chaos", json={"fault": "worker_hang"}, timeout=3)
    r.raise_for_status()
    return "worker_hang injected: worker is blocked (run `chaos.py load` to watch the queue grow)"


def compound(gap_seconds: float = 5) -> str:
    """'Multiple issues at once': payment_drop, then worker_hang a few seconds later.

    The gap lets some orders get processed with the bad URL first (with `load` running),
    so both signals are visible: unpaid orders AND a stalled worker with a growing queue.
    """
    first = payment_drop()
    time.sleep(gap_seconds)
    return first + "\n" + worker_hang()


def unknown() -> str:
    """Official example 5 bait: a fault none of the agent's tools can fix. It must escalate."""
    os.chmod(DB_PATH, stat.S_IREAD)
    return f"unknown injected: {DB_PATH.name} is now read-only (writes fail)"


def reset() -> str:
    """Back to a clean, healthy state. Used between demo runs and eval runs."""
    steps = [supervisor.stop()]
    if DB_PATH.exists():
        os.chmod(DB_PATH, stat.S_IREAD | stat.S_IWRITE)  # undo `unknown` (Windows can't delete read-only files)
        DB_PATH.unlink()
    LOG_FILE.unlink(missing_ok=True)
    supervisor.restore_runtime_files(overwrite=True)
    steps.append("DB, logs, config and deploy history reset")
    steps.append(supervisor.start())
    return "; ".join(steps)


# ---------------------------------------------------------------- traffic
def load(seconds: float = 60, rate: float = 5) -> None:
    """Send a steady stream of realistic orders so the metrics keep moving."""
    end = time.time() + seconds
    sent = failed = 0
    while time.time() < end:
        order = {"amount": round(random.uniform(10, 500), 2), "currency": random.choice(["SAR", "SAR", "USD"])}
        try:
            requests.post(f"{SERVICE_URL}/orders", json=order, timeout=3).raise_for_status()
            sent += 1
        except requests.RequestException:
            failed += 1
        time.sleep(1 / rate)
    print(f"load done: {sent} orders sent, {failed} failed")


FAULTS = {"payment_drop": payment_drop, "worker_hang": worker_hang, "compound": compound,
          "unknown": unknown, "reset": reset}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "load":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 60
        per_sec = float(sys.argv[3]) if len(sys.argv) > 3 else 5
        load(secs, per_sec)
    elif cmd in FAULTS:
        print(FAULTS[cmd]())
    else:
        sys.exit(f"usage: python chaos.py load [seconds] [rate] | {' | '.join(FAULTS)}")
