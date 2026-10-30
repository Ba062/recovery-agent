"""Nabd recovery agent: Claude Sonnet 5.5 + a hand-written tool-calling loop.

Official pipeline: Investigate -> Diagnose -> Recommend or Act -> Verify.
  - Read-only tools run freely. Write tools need human approval and may be rejected.
  - Fixed tool allow-list: no shell, no SQL, no arbitrary code.
  - Safety rules are enforced in code, not only asked for in the prompt.

Setup:
    pip install -r requirements.txt
    .env  ->  ANTHROPIC_API_KEY=sk-ant-...
Run (service must be running: python chaos.py reset):
    python agent.py                  check the service now; investigate if anything is wrong
    python agent.py "alert text"     investigate a given alert
    python agent.py --recommend      diagnose and recommend fixes, change nothing
"""

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

import monitor
from service.pipeline import write_config

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# When output is piped on Windows, Python uses cp1252, which can't print characters the model
# often writes (→, ≈). Replace them instead of crashing AFTER a paid run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
client = Anthropic()  # reads ANTHROPIC_API_KEY

MODEL = "claude-sonnet-5-5"
EFFORT = "medium"     # low | medium | high | xhigh | max  (no temperature on Sonnet 5.5)
MAX_STEPS = 12
MIN_CONFIDENCE = 0.6  # below this the agent must escalate (enforced in code)
MAX_SAME_FIX = 2      # the same fix with the same arguments at most twice, then escalate
# Server-side fallback: if Sonnet 5.5's safety classifier wrongly declines (e.g. log text that
# looks like an attack), the API retries on a suitable model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Sonnet 5.5 prices, USD per million tokens (input, output, cache read, 5-min cache write)
PRICE_PER_MTOK = {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50}

SERVICE_URL = os.getenv("SERVICE_URL", "http://127.0.0.1:8000")
LOG_FILE = ROOT / "service" / "logs" / "app.jsonl"
CONFIG_FILE = ROOT / "service" / "config.yaml"
DEPLOY_LOG = ROOT / "service" / "deploys.jsonl"
AGENT_NAME = "nabd-agent"


# ---------------------------------------------------------------- output schema
class Diagnosis(BaseModel):
    root_causes: list[str] = Field(description="Every distinct root cause found, most urgent first, one sentence each")
    evidence: list[str] = Field(description="Exact log lines / metric values / config entries supporting each cause")
    confidence: float = Field(ge=0, le=1, description="0-1 confidence in the root causes")
    impact: str = Field(description="Business impact with numbers, e.g. '25 orders processed without payment; all 25 reprocessed'")
    actions_taken: list[str] = Field(description="Write actions actually applied, in order. Empty if none")
    recommended_actions: list[str] = Field(description="Fixes recommended but NOT applied (rejected, unavailable, or for a human). Empty if none")
    recovered: bool = Field(description="True only if check_invariants was green after your last fix")
    escalate: bool = Field(description="True if a human must take over")
    summary: str = Field(description="2-3 sentence incident summary for the on-call engineer")


# ---------------------------------------------------------------- helpers
def _get(path: str) -> str:
    try:
        r = requests.get(f"{SERVICE_URL}{path}", timeout=5)
        return f"HTTP {r.status_code}: {r.text[:2000]}"
    except requests.RequestException as e:
        return f"Service unreachable: {e}"


def _post(path: str, body: dict | None = None) -> str:
    """Call an admin endpoint. Raises on failure, so the model gets the result as an error."""
    try:
        r = requests.post(f"{SERVICE_URL}{path}", json=body or {}, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"service unreachable: {e}")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:2000]}")
    return f"HTTP {r.status_code}: {r.text[:2000]}"


def _read_config() -> dict:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def _wait_until_applied(key: str, value, timeout: float = 10) -> str:
    """Config is hot-reloaded within ~2s. Wait until the service confirms it is using the new
    value, so the next fix (e.g. restart_worker) never runs against the old config."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{SERVICE_URL}/config", timeout=3).json().get(key) == value:
                return "Confirmed: the service is now using the new value."
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.5)
    return f"WARNING: written, but the service has NOT applied it after {timeout:.0f}s. Check before continuing."


def _record_change(change: str, version: str | None = None) -> None:
    """Every change the agent makes goes into the deploy history: a visible audit trail."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "version": version or _read_config().get("version"), "change": change, "by": AGENT_NAME}
    with DEPLOY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------- tools (read-only)
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
COMPONENTS = ["api", "payment", "queue", "worker", "sync", "config", "admin", "pipeline"]


def read_logs(min_level: str = "WARNING", component: str | None = None, last_n: int = 30) -> str:
    """Filter and summarise logs in Python first, so the LLM sees signal, not thousands of lines.

    Under traffic most lines are routine request logs, so we count lines per message first
    (e.g. "27x DEBUG payment: provider call failed ...") and then show the most recent lines.
    """
    if not LOG_FILE.exists():
        return "Log file not found."
    matches = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines()[-5000:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if LEVELS.get(rec.get("level"), 20) < LEVELS.get(min_level, 30):
            continue
        if component and rec.get("component") != component:
            continue
        matches.append(rec)
    if not matches:
        return f"No log lines at {min_level} or above" + (f" from {component}." if component else ".")
    counts = Counter((r["level"], r.get("component"), r["msg"]) for r in matches)
    out = [f"{len(matches)} matching lines (of the last 5000). Most frequent messages:"]
    out += [f"  {n}x [{lvl}] {comp}: {msg}" for (lvl, comp, msg), n in counts.most_common(8)]
    out.append(f"Most recent {min(last_n, len(matches))} lines:")
    out += [json.dumps(r) for r in matches[-last_n:]]
    return "\n".join(out)


def get_business_metrics() -> str:
    return _get("/business-metrics")


def get_metrics() -> str:
    return _get("/metrics")


def check_health() -> str:
    return _get("/health")


def check_data_integrity() -> str:
    return _get("/data-integrity")


def check_invariants() -> str:
    """Run the monitor's business rules (two readings ~2s apart). Use this to verify a fix."""
    anomalies = monitor.check_once(2)
    if not anomalies:
        return "All business invariants green (health, payments, queue, worker, ledger, errors, latency)."
    return "Still broken:\n" + "\n".join(f"- [{a.kind}] {a.rule}: {a.message}" for a in anomalies)


def read_config() -> str:
    return CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else "Config not found."


def recent_changes() -> str:
    """Last 10 changes, each with its age, so it's obvious whether a change matches the incident."""
    if not DEPLOY_LOG.exists():
        return "No deploy history."
    now = datetime.now(timezone.utc)
    lines = []
    for line in DEPLOY_LOG.read_text(encoding="utf-8").splitlines()[-10:]:
        entry = json.loads(line)
        hours = (now - datetime.fromisoformat(entry["ts"])).total_seconds() / 3600
        age = f"{hours * 60:.0f} min ago" if hours < 1 else f"{hours:.1f}h ago"
        lines.append(f"[{age}] {line}")
    return "\n".join(lines)


# ---------------------------------------------------------------- tools (write: need approval)
SETTABLE_KEYS = {"payment_provider_url", "queue_max", "worker_count", "sync_interval_s"}


def set_config(key: str, value: str) -> str:
    if key not in SETTABLE_KEYS:
        raise ValueError(f"key {key!r} can't be set. Allowed: {sorted(SETTABLE_KEYS)}. "
                         f"To change the version, use rollback_deploy.")
    cfg = _read_config()
    old = cfg.get(key)
    cfg[key] = yaml.safe_load(value)  # "1000" -> int, "internal://x" stays a string
    write_config(cfg)                 # validates + atomic write; raises ValueError if invalid
    _record_change(f"config: {key} {old} -> {cfg[key]}")
    return f"{key}: {old!r} -> {cfg[key]!r}. " + _wait_until_applied(key, cfg[key])


def rollback_deploy() -> str:
    """Go back to the version deployed before the current one."""
    versions = []  # distinct versions in the order they were first deployed
    for line in DEPLOY_LOG.read_text(encoding="utf-8").splitlines():
        v = json.loads(line).get("version")
        if v and v not in versions:
            versions.append(v)
    cfg = _read_config()
    current = cfg.get("version")
    if current not in versions or versions.index(current) == 0:
        raise ValueError(f"no earlier version to roll back to (current {current}, history {versions})")
    target = versions[versions.index(current) - 1]
    cfg["version"] = target
    write_config(cfg)
    _record_change(f"rollback: version {current} -> {target}", version=target)
    return f"Rolled back version {current} -> {target}. " + _wait_until_applied("version", target)


def restart_worker() -> str:
    return _post("/admin/restart-worker")


def reprocess_orders(order_ids) -> str:
    return _post("/admin/reprocess", {"order_ids": order_ids})


def repair_ledger(order_ids) -> str:
    return _post("/admin/repair-ledger", {"order_ids": order_ids})


READ_FUNCS: dict[str, Callable[..., str]] = {
    "get_business_metrics": get_business_metrics, "get_metrics": get_metrics, "check_health": check_health,
    "read_logs": read_logs, "read_config": read_config, "recent_changes": recent_changes,
    "check_data_integrity": check_data_integrity, "check_invariants": check_invariants,
}
WRITE_FUNCS: dict[str, Callable[..., str]] = {
    "set_config": set_config, "rollback_deploy": rollback_deploy, "restart_worker": restart_worker,
    "reprocess_orders": reprocess_orders, "repair_ledger": repair_ledger,
}
TOOL_FUNCS = {**READ_FUNCS, **WRITE_FUNCS}
WRITE_TOOLS = set(WRITE_FUNCS)


def _tool(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}


def _ids(keyword: str) -> dict:
    return {"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "string", "enum": [keyword]}],
            "description": f'A list of order ids, or "{keyword}"'}


READ_TOOLS = [
    _tool("get_business_metrics", "Business invariants: orders vs payments, enqueued/processed/dropped, "
          "queue depth, worker heartbeat age, ledger integrity failures. The best first look."),
    _tool("get_metrics", "Technical metrics: error rate, p95 latency, memory, request count."),
    _tool("check_health", "The shallow /health endpoint (process + DB readable). Stays green during silent failures."),
    _tool("read_logs", "Recent logs, filtered and summarised (message counts + latest lines). "
          "Silent failures are often logged ONLY at DEBUG, so try min_level DEBUG with a component.",
          {"min_level": {"type": "string", "enum": list(LEVELS)},
           "component": {"type": "string", "enum": COMPONENTS},
           "last_n": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _tool("read_config", "The live service config (hot-reloaded every ~2s)."),
    _tool("recent_changes", "Last 10 deploys / config changes, with who made them."),
    _tool("check_data_integrity", "Which orders are affected: unpaid processed orders and ledger mismatches, with ids."),
    _tool("check_invariants", "Run the monitor's business rules now (takes ~2s). Use after every fix to verify recovery."),
]
WRITE_TOOL_SCHEMAS = [
    _tool("set_config", f"[WRITE - needs human approval] Set one config key: {sorted(SETTABLE_KEYS)}. "
          "No restart needed: returns once the service confirms it is using the new value.",
          {"key": {"type": "string", "enum": sorted(SETTABLE_KEYS)}, "value": {"type": "string"}}, ["key", "value"]),
    _tool("rollback_deploy", "[WRITE - needs human approval] Roll back the service VERSION to the previously "
          "deployed one. For a bad deploy only: it does NOT undo config changes (use set_config for those)."),
    _tool("restart_worker", "[WRITE - needs human approval] Restart the order worker thread(s). Queued orders "
          "are processed with the CURRENT config as soon as it restarts."),
    _tool("reprocess_orders", "[WRITE - needs human approval] Retry payment for processed orders that have no "
          "payment. Never double-charges.", {"order_ids": _ids("all_unpaid")}, ["order_ids"]),
    _tool("repair_ledger", "[WRITE - needs human approval] Copy correct amounts from orders back into "
          "mismatched ledger rows.", {"order_ids": _ids("all_flagged")}, ["order_ids"]),
]
SUBMIT_TOOL = _tool("submit_diagnosis", "Submit your final structured diagnosis. Always finish by calling this.",
                    Diagnosis.model_json_schema()["properties"], list(Diagnosis.model_fields))

SYSTEM_PROMPT = """You are Nabd, an on-call SRE agent for an order-processing system.

The system: POST /orders -> in-memory queue -> worker thread -> payment provider -> payments table.
A sync job copies paid orders into a ledger. /health only checks that the process and database are up,
so it stays green during silent failures. Judge the system by its business invariants
(orders vs payments, enqueued vs processed, worker heartbeat, ledger integrity), not by /health.

How to work:
1. Investigate with read-only tools. Recent config changes and DEBUG-level logs often hold the cause.
2. Diagnose every root cause. There may be more than one at once. Quote exact evidence.
   Only blame a change if its timing and evidence actually match when the incident started.
3. Fix with the smallest action that addresses each root cause, one write action at a time.
   With several issues, first stop ongoing data loss or corruption, then restore processing,
   then repair the affected data. Order matters: e.g. restarting a worker while payments are
   still broken processes the whole queue without payment.
   Mitigate first: if a component is stuck and restarting it is safe, that is a good first fix
   even before the deeper cause is known - say the deeper cause is still unknown.
4. After each fix, verify with check_invariants. If it is still broken, re-investigate.
5. Escalate (escalate=true) instead of guessing when: you are not confident in the cause, no
   available tool can fix it, a human rejected your action and there is no safe alternative,
   or the same fix has already failed.
6. Always finish by calling submit_diagnosis, with numbers for the impact.

Write actions need human approval and may be rejected."""

RECOMMEND_NOTE = """

THIS RUN IS RECOMMEND-ONLY: you have no write tools. Diagnose, then put the fixes you would
apply, in order, in recommended_actions. Set recovered=false and actions_taken=[].
These are the actions a human can approve (recommend them by name):
""" + "\n".join(f"- {t['name']}: {t['description'].split('] ', 1)[-1]}" for t in WRITE_TOOL_SCHEMAS)


# ---------------------------------------------------------------- approval + loop
def cli_approve(name: str, args: dict) -> bool:
    return input(f"\nNabd wants to run {name}({args}). Approve? [y/N] ").strip().lower() == "y"


def print_event(kind: str, data: dict) -> None:
    text = data.get("text") or json.dumps(data)
    print(f"[{kind}] {text[:500]}")


def run_tool(name: str, args: dict) -> tuple[str, bool]:
    """Run an allow-listed tool. Returns (output, is_error). Errors go back to the model."""
    try:
        return TOOL_FUNCS[name](**args), False
    except Exception as e:
        return f"Tool error: {e}", True


def enforce_escalation(diagnosis: Diagnosis) -> Diagnosis:
    """Safety rule in code: low confidence always escalates, whatever the model said."""
    if diagnosis.confidence < MIN_CONFIDENCE:
        diagnosis.escalate = True
    return diagnosis


def estimate_cost(stats: dict) -> float:
    return round((stats["input_tokens"] * PRICE_PER_MTOK["input"]
                  + stats["output_tokens"] * PRICE_PER_MTOK["output"]
                  + stats["cache_read_tokens"] * PRICE_PER_MTOK["cache_read"]
                  + stats["cache_write_tokens"] * PRICE_PER_MTOK["cache_write"]) / 1e6, 4)


def run_agent(alert: str,
              approve: Callable[[str, dict], bool] = cli_approve,
              on_event: Callable[[str, dict], None] = print_event,
              recommend_only: bool = False) -> tuple[Diagnosis, dict]:
    """Investigate one alert. Returns (diagnosis, record). The record holds stats + every event,
    so a run can be replayed later without an API key."""
    tools = READ_TOOLS + ([] if recommend_only else WRITE_TOOL_SCHEMAS) + [SUBMIT_TOOL]
    system = SYSTEM_PROMPT + (RECOMMEND_NOTE if recommend_only else "")
    messages = [{"role": "user", "content": f"ALERT from monitor:\n{alert}\n\nInvestigate and recover the system."}]
    stats = {"steps": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
             "writes_applied": [], "writes_rejected": [], "writes_blocked": []}
    events = []
    record = {"alert": alert, "mode": "recommend" if recommend_only else "act", "model": MODEL,
              "stats": stats, "events": events}
    fix_counts: Counter = Counter()
    start = time.time()

    def emit(kind: str, **data) -> None:
        event = {"t": round(time.time() - start, 1), "kind": kind, **data}
        events.append(event)
        on_event(kind, data)

    def finish(diagnosis: Diagnosis) -> tuple[Diagnosis, dict]:
        stats["seconds"] = round(time.time() - start, 1)
        stats["est_cost_usd"] = estimate_cost(stats)
        record["diagnosis"] = diagnosis.model_dump()
        emit("diagnosis", **diagnosis.model_dump())
        return diagnosis, record

    def give_up(root_cause: str, summary: str) -> tuple[Diagnosis, dict]:
        return finish(Diagnosis(root_causes=[root_cause], evidence=[], confidence=0.0, impact="unknown",
                                actions_taken=stats["writes_applied"], recommended_actions=[],
                                recovered=False, escalate=True, summary=summary))

    emit("alert", text=alert)
    for step in range(1, MAX_STEPS + 1):
        stats["steps"] = step
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,                                     # headroom: thinking is always on
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": EFFORT},
            thinking={"type": "adaptive", "display": "summarized"},  # readable reasoning for the UI
            cache_control={"type": "ephemeral"},                  # tools + system + history reused each step
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        usage = resp.usage
        stats["input_tokens"] += usage.input_tokens
        stats["output_tokens"] += usage.output_tokens
        stats["cache_read_tokens"] += usage.cache_read_input_tokens or 0
        stats["cache_write_tokens"] += usage.cache_creation_input_tokens or 0
        if resp.model != MODEL:
            emit("fallback", text=f"answered by fallback model {resp.model}")
        messages.append({"role": "assistant", "content": resp.content})  # unchanged: keeps thinking blocks valid

        if resp.stop_reason == "refusal":
            return give_up("Model declined to continue (safety refusal).",
                           "The AI model declined this request, so a human must investigate. Escalating.")

        for block in resp.content:
            if block.type == "thinking" and block.thinking.strip():
                emit("thinking", text=block.thinking)
            elif block.type == "text" and block.text.strip():
                emit("thought", text=block.text)

        # Check for tool calls rather than stop_reason: if the reply was cut off at max_tokens
        # mid tool call, every tool_use still needs a matching tool_result or the API returns 400.
        tool_calls = [block for block in resp.content if block.type == "tool_use"]
        if not tool_calls:
            messages.append({"role": "user", "content": "Call submit_diagnosis now with your final findings."})
            continue

        results = []
        wrote_this_turn = False
        for block in tool_calls:
            emit("tool_call", tool=block.name, args=block.input)
            is_error = False

            if block.name == "submit_diagnosis":
                try:
                    diagnosis = Diagnosis(**block.input)
                except ValidationError as e:
                    # Bad fields (e.g. confidence "high"): tell the model and let it retry.
                    output, is_error = f"Invalid diagnosis, fix these fields and call submit_diagnosis again:\n{e}", True
                else:
                    return finish(enforce_escalation(diagnosis))
            elif block.name in WRITE_TOOLS:
                fix_key = f"{block.name} {json.dumps(block.input, sort_keys=True)}"
                if wrote_this_turn:
                    # Only one write action per turn, so each fix is verified before the next.
                    output = "SKIPPED: only one write action per turn. Verify with check_invariants first."
                elif fix_counts[fix_key] >= MAX_SAME_FIX:
                    output = (f"BLOCKED: this exact fix was already applied {MAX_SAME_FIX} times. "
                              "It is not working. Escalate to a human.")
                    stats["writes_blocked"].append(fix_key)
                elif not approve(block.name, block.input):
                    output = "REJECTED by human operator. Pick another safe action or escalate."
                    stats["writes_rejected"].append(fix_key)
                else:
                    output, is_error = run_tool(block.name, block.input)
                    fix_counts[fix_key] += 1
                    if not is_error:
                        stats["writes_applied"].append(fix_key)
                wrote_this_turn = True
            else:
                output, is_error = run_tool(block.name, block.input)

            emit("tool_result", tool=block.name, text=output[:1500], is_error=is_error)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error})

        messages.append({"role": "user", "content": results})  # all results in ONE message

    return give_up("Unresolved within the step limit.", f"Step limit ({MAX_STEPS}) reached without resolution. Escalating.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    recommend = "--recommend" in sys.argv
    if args:
        alert_text = args[0]
    else:
        found = monitor.check_once()
        if not found:
            sys.exit("All business invariants green - nothing to investigate.")
        alert_text = monitor.format_alert(monitor.build_alert(found, monitor.snapshot()))
    diagnosis, run = run_agent(alert_text, recommend_only=recommend)
    print("\n=== DIAGNOSIS ===")
    print(diagnosis.model_dump_json(indent=2))
    print("=== STATS ===", json.dumps(run["stats"]))
