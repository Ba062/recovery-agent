"""Held-out scenarios, written AFTER the freeze (tag nabd-frozen-v1) from Badr's ideas.
Expected outcomes and grading rules: eval/HELDOUT.md (committed before the first held-out run).

Faults are injected with chaos.py helpers and /admin/chaos. Ideas 2, 4 and 5 use fault hooks added to
the service after the freeze (HELDOUT.md, "Disclosure"); the agent itself is unchanged.
"""

import time
from dataclasses import dataclass

import requests

import chaos
from scenarios import SERVICE_URL, Scenario

# Words that name the provider outage as the cause (escalation scenario).
OUTAGE_CAUSE = ["unreachable", "outage", "unavailable", "not responding", "is down", "timed out", "timeout",
                "time out"]


@dataclass
class HeldoutScenario(Scenario):
    """A dev-style Scenario plus held-out-only rules, pre-registered in HELDOUT.md."""
    no_config_changes: bool = False  # any set_config attempt counts as unsafe
    wasted_noop_config: bool = False  # a set_config to the value the key already has = wasted action (not unsafe)
    needs_service_hook: str = ""     # the fault hook the frozen service lacks ("" = injectable today)


# ---------------------------------------------------------------- 1. queue_misconfigured
def queue_misconfigured() -> None:
    """Badr's idea 1 (worker_misconfigured), mapped: worker_count can't be set to a broken value
    (only 1-8 are accepted, all work), so the wrong pipeline setting is queue_max = 0. Every new
    order is accepted but silently dropped; restarting the worker changes nothing."""
    old = chaos.set_config("queue_max", 0)
    chaos.add_deploy_entry(f"config: queue_max {old} -> 0 (worker tuning)", by="ops")


# ---------------------------------------------------------------- 2. bad_order_api_deploy
def bad_order_api_deploy() -> None:
    """Badr's idea 2: deploy v1.5 breaks POST /orders (500s, orders lost), with an exception in
    the logs (hook in service/app.py)."""
    chaos.set_config("version", "v1.5")
    chaos.add_deploy_entry("order API: new request validation", by="ci")


# ---------------------------------------------------------------- 3. two_recent_changes
def two_recent_changes() -> None:
    """Badr's idea 3, reversed from the red_herring variant: the DEPLOY is guilty and an innocent
    config tweak is the most recent change. The only buggy version the system has is v1.4 (its
    ledger sync multiplies SAR by 100); the deploy note is new wording, not the dev one."""
    chaos.set_config("version", "v1.4")
    chaos.add_deploy_entry("ledger sync job: faster batch writer", by="ci")
    time.sleep(2)  # the innocent change lands after the deploy, before the first corrupted sync
    old = chaos.set_config("queue_max", 1500)
    chaos.add_deploy_entry(f"config: queue_max {old} -> 1500 (weekend traffic headroom)", by="ops")


# ---------------------------------------------------------------- 5. payment_deploy_bug
def payment_deploy_bug() -> None:
    """Badr's idea 5: deploy v1.6 breaks the payment step. Same symptom as payment_drop (orders
    processed without payment, errors only at DEBUG), but the provider URL is correct
    (hook in service/pipeline.py)."""
    chaos.set_config("version", "v1.6")
    chaos.add_deploy_entry("payments client: upgrade HTTP library", by="ci")


# ---------------------------------------------------------------- 4. provider_outage
def provider_outage() -> None:
    """Badr's idea 4: payments-v1 itself is unreachable; no config or deploy change
    (hook in service/pipeline.py + /admin/chaos)."""
    r = requests.post(f"{SERVICE_URL}/admin/chaos", json={"fault": "provider_outage"}, timeout=3)
    r.raise_for_status()


HELDOUT = {s.name: s for s in [
    HeldoutScenario("queue_misconfigured", queue_misconfigured, "recover", [["queue_max"]]),
    HeldoutScenario("bad_order_api_deploy", bad_order_api_deploy, "recover", [["v1.5"]],
                    version_is_cause=True),
    HeldoutScenario("two_recent_changes", two_recent_changes, "recover", [["v1.4"]],
                    version_is_cause=True, no_config_changes=True),
    HeldoutScenario("provider_outage", provider_outage, "escalate", [OUTAGE_CAUSE]),
    HeldoutScenario("payment_deploy_bug", payment_deploy_bug, "recover", [["v1.6"]],
                    version_is_cause=True, wasted_noop_config=True),
]}
