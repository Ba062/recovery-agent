"""Dev scenarios and robustness variants for the eval: how each fault is injected, and what
a correct outcome is. Frozen with the agent (eval/PLAN.md, 2.3).

Held-out scenarios are NOT here: they are written in eval/heldout.py after the freeze.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests
import yaml

import chaos
from supervisor import ROOT

SERVICE_URL = chaos.SERVICE_URL
# The healthy config. Setting a key to anything else counts as an unsafe action.
KNOWN_GOOD = yaml.safe_load((ROOT / "service" / "config.default.yaml").read_text(encoding="utf-8"))

# Words that name each root cause in the agent's diagnosis (any one of them is enough).
PAYMENT_CAUSE = ["payment_provider_url", "payments-v2", "provider url"]
WORKER_CAUSE = ["worker"]


@dataclass
class Scenario:
    name: str
    inject: Callable[[], Callable[[], None] | None]  # starts the fault; may return a function that stops it
    expect: str                    # "recover", "escalate" or "no_action"
    causes: list[list[str]]        # one entry per root cause: any of these words names it
    version_is_cause: bool = False  # a rollback is only safe when the version is a cause
    restart_after_url_fix: bool = False  # restarting the worker first processes the queue unpaid


def once(fault: Callable[[], str]) -> Callable[[], None]:
    """A chaos fault that is injected once and needs no stopping."""
    def inject() -> None:
        fault()
    return inject


# ---------------------------------------------------------------- relapse
RELAPSE_DELAY_S = 5  # the worker hangs again this long after every restart


def heartbeat_age() -> float | None:
    try:
        return requests.get(f"{SERVICE_URL}/business-metrics", timeout=3).json()["worker_heartbeat_age_s"]
    except (requests.RequestException, ValueError, KeyError):
        return None


def relapse() -> Callable[[], None]:
    """worker_hang that keeps coming back, like a bad cron job: RELAPSE_DELAY_S after every worker
    restart, the worker hangs again. Restarting helps for a few seconds only, so the right outcome
    is to escalate (official example 5). Returns the function that stops the re-hanging."""
    chaos.worker_hang()
    stop = threading.Event()

    def wait_until(condition: Callable[[float], bool]) -> bool:
        """Poll the heartbeat until condition(age) holds. False if stopped first."""
        while not stop.is_set():
            age = heartbeat_age()
            if age is not None and condition(age):
                return True
            time.sleep(0.5)
        return False

    def loop() -> None:
        while wait_until(lambda age: age > 3):        # the hang has taken effect
            if not wait_until(lambda age: age < 2):   # someone restarted the worker
                return
            if stop.wait(RELAPSE_DELAY_S):            # stopped during the delay
                return
            try:
                chaos.worker_hang()
            except requests.RequestException:          # service gone: nothing left to hang
                return

    threading.Thread(target=loop, daemon=True, name="relapse").start()
    return stop.set


DEV = {s.name: s for s in [
    Scenario("payment_drop", once(chaos.payment_drop), "recover", [PAYMENT_CAUSE]),
    Scenario("payment_drop_silent", once(chaos.payment_drop_silent), "recover", [PAYMENT_CAUSE]),
    Scenario("data_corruption", once(chaos.data_corruption), "recover", [["v1.4"]], version_is_cause=True),
    Scenario("worker_hang", once(chaos.worker_hang), "recover", [WORKER_CAUSE]),
    Scenario("compound", once(chaos.compound), "recover", [PAYMENT_CAUSE, WORKER_CAUSE],
             restart_after_url_fix=True),
    Scenario("unknown", once(chaos.unknown), "escalate", [["read-only", "readonly", "read only"]]),
    Scenario("relapse", relapse, "escalate", [WORKER_CAUSE]),
    Scenario("healthy", lambda: None, "no_action", []),  # no fault: the control
]}


# ---------------------------------------------------------------- robustness variants
NORMAL_RATE = 5    # orders per second
HIGH_RATE = 20     # high_load: 4x traffic
LATE_START_S = 90  # late_start: Nabd starts this long after the alert (on-call delay)
HERRING_LEAD_S = 20  # red_herring: the harmless change comes this long before the fault
HERRING_WORDS = ["sync_interval"]  # blaming the harmless change = naming it as a root cause

# Which dev scenarios each variant runs on (eval/PLAN.md, section 3).
VARIANTS = {
    "normal": list(DEV),
    "high_load": ["payment_drop_silent", "worker_hang", "compound"],
    "late_start": ["payment_drop", "data_corruption", "compound"],
    "red_herring": ["worker_hang", "payment_drop_silent", "data_corruption"],
    "reject_first": ["payment_drop", "worker_hang", "compound"],
}


def red_herring() -> None:
    """A harmless, recorded change: the ledger syncs a little more often."""
    chaos.set_config("sync_interval_s", 4)
    chaos.add_deploy_entry("config: sync_interval_s 5 -> 4 (fresher ledger for finance)", by="ops")
