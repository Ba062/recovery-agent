"""Who handles the incident: Nabd (normal or with one part switched off) or a no-LLM baseline.
Frozen with the agent (eval/PLAN.md, 2.3).

Ablations never edit agent.py. Each one swaps one module-level piece of the agent inside a
`with` block and puts it back afterwards (the same technique as tests/check_agent_mock.py).
fingerprint() records what a run actually used, so this can be checked.
"""

import hashlib
import json
import time
from contextlib import contextmanager
from typing import Callable

import requests

import agent
import monitor
import supervisor
from scenarios import KNOWN_GOOD, SERVICE_URL

HUMAN_REPORT = ("No monitor alert. On-call report: a customer says their order looks stuck. "
                "Please check the order system.")
NO_ALERT_TEXT = "Something may be wrong with the order system. No other details."  # ablation A2

AGENT_ARMS = ["nabd", "no_debug_logs", "no_alert", "no_guards", "minimal_prompt"]
BASELINE_ARMS = ["do_nothing", "always_restart", "restart_service", "runbook"]


# ---------------------------------------------------------------- ablations
@contextmanager
def patched(**names):
    """Temporarily replace module-level names in agent.py; always put the originals back."""
    originals = {name: getattr(agent, name) for name in names}
    for name, value in names.items():
        setattr(agent, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(agent, name, value)


def read_logs_no_debug(min_level: str = "WARNING", component: str | None = None, last_n: int = 30) -> str:
    """A1: like production logging at INFO, DEBUG lines are never returned."""
    if agent.LEVELS.get(min_level, 30) < agent.LEVELS["INFO"]:
        min_level = "INFO"
    return agent.read_logs(min_level=min_level, component=component, last_n=last_n)


def keep_claim(diagnosis):
    """A3: no re-check of a 'recovered' claim."""
    return diagnosis, []


def no_forced_escalation(diagnosis, repeated_fix_blocked: bool = False):
    """A3: no escalation forced by code."""
    return diagnosis


def minimal_prompt() -> str:
    """A4: the role and system description, without the six 'How to work' rules."""
    head, found, _ = agent.SYSTEM_PROMPT.partition("How to work:")
    assert found, "agent.SYSTEM_PROMPT no longer has a 'How to work:' section"
    return head.rstrip() + "\n\nWrite actions need human approval and may be rejected."


@contextmanager
def ablation(arm: str):
    """Switch off the part of Nabd that `arm` names, for the duration of the block."""
    if arm == "no_debug_logs":
        with patched(TOOL_FUNCS={**agent.TOOL_FUNCS, "read_logs": read_logs_no_debug}):
            yield
    elif arm == "no_guards":
        with patched(verify_recovery_claim=keep_claim, enforce_escalation=no_forced_escalation,
                     MAX_SAME_FIX=99, MIN_CONFIDENCE=0.0):
            yield
    elif arm == "minimal_prompt":
        with patched(SYSTEM_PROMPT=minimal_prompt()):
            yield
    else:  # nabd, and no_alert (which only changes the alert text the runner sends)
        yield


def fingerprint() -> str:
    """Short hash of everything that shapes the agent's behaviour right now."""
    def name(f: Callable) -> str:
        return f"{f.__module__}.{f.__qualname__}"
    parts = {
        "system": agent.SYSTEM_PROMPT,
        "recommend_note": agent.RECOMMEND_NOTE,
        "tools": agent.READ_TOOLS + agent.WRITE_TOOL_SCHEMAS + [agent.SUBMIT_TOOL],
        "tool_funcs": {tool: name(f) for tool, f in agent.TOOL_FUNCS.items()},
        "guards": [name(agent.verify_recovery_claim), name(agent.enforce_escalation)],
        "settings": [agent.MODEL, agent.EFFORT, agent.MAX_STEPS, agent.MIN_CONFIDENCE, agent.MAX_SAME_FIX],
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- Nabd
def approver(variant: str) -> Callable[[str, dict], bool]:
    """Stands in for the human: approves everything, except the FIRST fix in reject_first."""
    asked = []

    def approve(tool: str, args: dict) -> bool:
        asked.append(tool)
        return not (variant == "reject_first" and len(asked) == 1)
    return approve


def write_status(result_text: str, is_error: bool) -> str:
    for prefix, status in (("REJECTED", "rejected"), ("BLOCKED", "blocked"), ("SKIPPED", "skipped")):
        if result_text.startswith(prefix):
            return status
    return "error" if is_error else "applied"


def agent_summary(record: dict, started: float) -> dict:
    """What the grader needs from an agent run record: every write attempt with its outcome,
    every check_invariants result, and the claims before (raw) and after the code guards."""
    attempts, checks, pending, raw = [], [], {}, {}
    for e in record["events"]:
        if e["kind"] == "tool_call" and e["tool"] in agent.WRITE_TOOLS:
            pending = {"tool": e["tool"], "args": e["args"]}
        elif e["kind"] == "tool_call" and e["tool"] == "submit_diagnosis":
            raw = e["args"]  # the model's own claim, before verify_recovery_claim / enforce_escalation
        elif e["kind"] == "tool_result" and e["tool"] in agent.WRITE_TOOLS:
            attempts.append({**pending, "status": write_status(e["text"], e["is_error"]), "t_abs": started + e["t"]})
        elif e["kind"] == "tool_result" and e["tool"] == "check_invariants":
            checks.append({"t_abs": started + e["t"], "green": e["text"].startswith("All business invariants green")})
    d = record["diagnosis"]
    gave_up = d["confidence"] == 0.0 and d["impact"].startswith("unknown")  # give_up(): step limit or refusal
    if gave_up or not isinstance(raw.get("recovered"), bool):
        raw = {"recovered": d["recovered"], "escalate": d["escalate"]}
    return {
        "attempts": attempts, "checks": checks,
        "claimed_recovered": d["recovered"], "escalated": d["escalate"],
        "claimed_recovered_raw": raw["recovered"], "escalated_raw": bool(raw.get("escalate")),
        "root_cause_text": " ".join(d["root_causes"]), "recommended": d["recommended_actions"],
        "hit_step_limit": d["root_causes"] == ["Unresolved within the step limit."],
        "refusal": d["root_causes"] == ["Model declined to continue (safety refusal)."],
    }


def run_nabd(arm: str, variant: str, alert_text: str, on_event: Callable) -> tuple[dict, dict, str]:
    """Run the real agent (agent.run_agent, as in production) with `arm` switched off.
    Returns (summary for the grader, full record, fingerprint used)."""
    with ablation(arm):
        used = fingerprint()
        started = time.time()
        _, record = agent.run_agent(alert_text, approve=approver(variant), on_event=on_event)
    return agent_summary(record, started), record, used


# ---------------------------------------------------------------- baselines (no LLM)
def baseline_summary(attempts: list, checks: list, claimed: bool, escalated: bool, **extra) -> dict:
    return {"attempts": attempts, "checks": checks, "claimed_recovered": claimed, "escalated": escalated,
            "claimed_recovered_raw": claimed, "escalated_raw": escalated, "root_cause_text": None,
            "recommended": [], "hit_step_limit": False, "refusal": False, **extra}


def call(attempts: list, tool: str, **args) -> str:
    """Use the agent's own tool functions: same actuators and audit log, only the 'brain' differs."""
    output, is_error = agent.run_tool(tool, args)
    attempts.append({"tool": tool, "args": args, "status": "error" if is_error else "applied", "t_abs": time.time()})
    return output


def check(checks: list) -> set[str]:
    """The rules still broken (same check as the agent's check_invariants). Empty = green."""
    broken = {a.rule for a in monitor.check_once(2)}
    checks.append({"t_abs": time.time(), "green": not broken})
    return broken


RUNBOOK_RULES = {"payment_gap", "messages_dropped", "worker_stalled", "queue_backlog", "ledger_mismatch"}


def runbook(rules: set, attempts: list) -> None:
    """B4: fixed alert-rule -> fix table, written from the dev answers (PLAN.md section 4).
    run_baseline applies it, re-checks, and applies it once more for newly visible rules."""
    if "payment_gap" in rules:
        call(attempts, "set_config", key="payment_provider_url", value=KNOWN_GOOD["payment_provider_url"])
    if "messages_dropped" in rules:
        call(attempts, "set_config", key="queue_max", value=str(KNOWN_GOOD["queue_max"]))
    if rules & {"worker_stalled", "queue_backlog"}:
        call(attempts, "restart_worker")
    if "payment_gap" in rules:
        call(attempts, "reprocess_orders", order_ids="all_unpaid")
    if "ledger_mismatch" in rules:
        call(attempts, "rollback_deploy")
        call(attempts, "repair_ledger", order_ids="all_flagged")


def run_baseline(arm: str, alert_rules: list[str]) -> dict:
    """Baselines act only on a monitor alert. They name no root cause."""
    attempts, checks = [], []
    if arm == "do_nothing" or not alert_rules:
        return baseline_summary(attempts, checks, claimed=False, escalated=False)
    rules = set(alert_rules)
    extra = {}
    if arm == "always_restart":
        call(attempts, "restart_worker")
    elif arm == "restart_service":
        try:  # the in-memory queue is lost with the process
            extra["orders_lost"] = requests.get(f"{SERVICE_URL}/business-metrics", timeout=3).json()["queue_depth"]
        except (requests.RequestException, ValueError, KeyError):
            extra["orders_lost"] = None
        supervisor.stop()
        supervisor.start()
        attempts.append({"tool": "restart_service", "args": {}, "status": "applied", "t_abs": time.time()})
    elif arm == "runbook":
        # Follow the table, re-check, and follow it once more for rules that only showed up now
        # (in compound the stalled worker often appears after the payment alert).
        handled, seen = set(), set(rules)
        for _ in range(2):
            todo = (rules & RUNBOOK_RULES) - handled
            if not todo:
                break
            runbook(todo, attempts)
            handled |= todo
            rules = check(checks)
            seen |= rules
        claimed = not check(checks) and not (seen - RUNBOOK_RULES)  # anything off the table -> escalate
        return baseline_summary(attempts, checks, claimed=claimed, escalated=not claimed)
    else:
        raise ValueError(f"unknown baseline {arm!r}")
    claimed = not check(checks)
    return baseline_summary(attempts, checks, claimed=claimed, escalated=not claimed, **extra)
