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
VERIFY_READINGS = 5   # a "recovered" claim must stay green for 5 monitor readings (~15s), checked in code
# Server-side fallback: if Sonnet 5.5's safety classifier wrongly declines (e.g. log text that
# looks like an attack), the API retries on a suitable model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Sonnet 5.5 prices, USD per million tokens (input, output, cache read, 5-min cache write)
PRICE_PER_MTOK = {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50}

SERVICE_URL = os.getenv("SERVICE_URL", "http://127.0.0.1:8000")
LOG_FILE = ROOT / "service" / "logs" / "app.jsonl"
CONFIG_FILE = ROOT / "service" / "config.yaml"
DEPLOY_LOG = ROOT / "service" / "deploys.jsonl"
REPLAYS = ROOT / "replays"  # saved runs (record + incident report) for replay mode
AGENT_NAME = "nabd-agent"


# ---------------------------------------------------------------- output schema
class Diagnosis(BaseModel):
    root_causes: list[str] = Field(description="Every distinct root cause found, most urgent first, one sentence each")
    evidence: list[str] = Field(description="Exact log lines / metric values / config entries supporting each cause")
    confidence: float = Field(ge=0, le=1, description="0-1 confidence in the root causes")
    impact: str = Field(description="Business impact with numbers, e.g. '25 orders processed without payment; all 25 "
                                    "reprocessed'. Money: quote money_impact figures EXACTLY as a tool reported them, "
                                    "never estimate or calculate amounts yourself")
    actions_taken: list[str] = Field(description="Write actions actually applied, in order. Empty if none")
    recommended_actions: list[str] = Field(description="Fixes recommended but NOT applied (rejected, unavailable, or for a human). Empty if none")
    recovered: bool = Field(description="True only if check_invariants was green after your last fix")
    escalate: bool = Field(description="True if a human must take over")
    summary: str = Field(description="2-3 sentence incident summary for the on-call engineer")
    # Filled in by code from the DB (via /business-metrics), never by the model: see CODE_FIELDS.
    money_impact_before: dict | None = None  # when the run starts (alert time)
    money_impact_peak: dict | None = None    # just before the first fix is applied; None if no fix was applied
    money_recovered: dict | None = None      # value of orders reprocessed + ledger overstatement corrected
    money_impact_after: dict | None = None   # at the end of the run


CODE_FIELDS = {"money_impact_before", "money_impact_peak", "money_recovered", "money_impact_after"}
MODEL_FIELDS = [name for name in Diagnosis.model_fields if name not in CODE_FIELDS]


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
# v2 (review F7): URLs are allow-listed by value too, so Nabd can't point payments at a guessed endpoint.
# Known-good = the URL in the committed config template.
CONFIG_TEMPLATE = ROOT / "service" / "config.default.yaml"
KNOWN_GOOD_URLS = {yaml.safe_load(CONFIG_TEMPLATE.read_text(encoding="utf-8"))["payment_provider_url"]}


def set_config(key: str, value: str) -> str:
    if key not in SETTABLE_KEYS:
        raise ValueError(f"key {key!r} can't be set. Allowed: {sorted(SETTABLE_KEYS)}. "
                         f"To change the version, use rollback_deploy.")
    new = yaml.safe_load(value)  # "1000" -> int, "internal://x" stays a string
    if key == "payment_provider_url" and new not in KNOWN_GOOD_URLS:
        raise ValueError(f"{new!r} is not a known-good provider URL. Allowed: {sorted(KNOWN_GOOD_URLS)}. "
                         f"If the provider itself is down, escalate.")
    cfg = _read_config()
    old = cfg.get(key)
    cfg[key] = new
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
          f"payment_provider_url accepts only known-good URLs: {sorted(KNOWN_GOOD_URLS)}. "
          "No restart needed: returns once the service confirms it is using the new value.",
          {"key": {"type": "string", "enum": sorted(SETTABLE_KEYS)}, "value": {"type": "string"}}, ["key", "value"]),
    _tool("rollback_deploy", "[WRITE - needs human approval] Roll back the service VERSION to the previously "
          "deployed one. For a bad deploy only: it does NOT undo config changes (use set_config for those)."),
    _tool("restart_worker", "[WRITE - needs human approval] Restart the order worker thread(s). Queued orders "
          "are processed with the CURRENT config as soon as it restarts."),
    _tool("reprocess_orders", "[WRITE - needs human approval] Retry payment for processed orders that have no "
          "payment. Never double-charges.", {"order_ids": _ids("all_unpaid")}, ["order_ids"]),
    _tool("repair_ledger", "[WRITE - needs human approval] Copy correct amounts from orders back into "
          "mismatched ledger rows. If a bad version is still syncing, new rows get corrupted again "
          "after the repair, so stop the cause first.", {"order_ids": _ids("all_flagged")}, ["order_ids"]),
]
SUBMIT_TOOL = _tool("submit_diagnosis", "Submit your final structured diagnosis. Always finish by calling this.",
                    {name: prop for name, prop in Diagnosis.model_json_schema()["properties"].items()
                     if name in MODEL_FIELDS}, MODEL_FIELDS)

SYSTEM_PROMPT = """You are Nabd, an on-call SRE agent for an order-processing system.

The system: POST /orders -> in-memory queue -> worker thread -> payment provider -> payments table.
A sync job copies paid orders into a ledger. /health only checks that the process and database are up,
so it stays green during silent failures. Judge the system by its business invariants
(orders vs payments, enqueued vs processed, worker heartbeat, ledger integrity), not by /health.

How to work:
1. Investigate with read-only tools. Recent config changes and DEBUG-level logs often hold the cause.
   If a config value looks wrong and the change isn't in recent_changes, read the config logs
   before concluding.
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
6. Always finish by calling submit_diagnosis, with numbers for the impact. For money, quote the
   money_impact figures from get_business_metrics or check_data_integrity exactly; never estimate
   or calculate amounts yourself. Nabd adds the before/after money figures to the report itself.

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


def enforce_escalation(diagnosis: Diagnosis, repeated_fix_blocked: bool = False) -> Diagnosis:
    """Safety rules in code, whatever the model said:
    - low confidence always escalates
    - the same fix failed twice (and was blocked a third time) and the system is not
      verified as recovered -> escalate, never give up silently (official example 5)
    - any other run that ends not recovered and not escalated -> escalate (v2, review F1):
      nobody is left holding a broken system
    Call this AFTER verify_recovery_claim, so "recovered" can't be faked to dodge the rule.
    """
    if diagnosis.confidence < MIN_CONFIDENCE:
        diagnosis.escalate = True
    if repeated_fix_blocked and not diagnosis.recovered and not diagnosis.escalate:
        diagnosis.escalate = True
        diagnosis.summary += " [Nabd: the same fix failed twice without recovery - escalating to a human.]"
    if not diagnosis.recovered and not diagnosis.escalate:
        diagnosis.escalate = True
        diagnosis.summary += " [Nabd: recovery is not verified - escalating to a human.]"
    return diagnosis


def verify_recovery_claim(diagnosis: Diagnosis) -> tuple[Diagnosis, list[str]]:
    """Never trust "recovered" on the model's word: re-check the business invariants in code,
    and keep watching for ~15s (VERIFY_READINGS x 3s). A fix that holds for only a few seconds,
    like restarting a worker that hangs again, is not a recovery.
    Returns the (possibly corrected) diagnosis and the rules that are still broken."""
    if not diagnosis.recovered:
        return diagnosis, []
    still_broken = []
    for _ in range(VERIFY_READINGS):
        still_broken = [a.rule for a in monitor.check_once(3)]
        if still_broken:
            break
    if still_broken:
        diagnosis.recovered = False
        diagnosis.escalate = True
        diagnosis.summary += f" [Nabd verification: NOT recovered - still broken: {', '.join(still_broken)}.]"
    return diagnosis, still_broken


def fetch_config() -> dict | None:
    """The config the service is really using (for the report: version / URL before and after)."""
    try:
        return requests.get(f"{SERVICE_URL}/config", timeout=5).json()
    except (requests.RequestException, ValueError):
        return None


def fetch_money_impact() -> dict | None:
    """Money impact computed by the service from the DB. None if the service can't be read."""
    try:
        return requests.get(f"{SERVICE_URL}/business-metrics", timeout=5).json()["money_impact"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


NO_MONEY = {"count": 0, "by_currency": {}, "total_sar": 0.0}
# Fix tools whose service response includes money_recovered, and where that figure is added up.
RECOVERY_TOOLS = {"reprocess_orders": "orders_paid", "repair_ledger": "ledger_corrected"}


def money_recovered_from(output: str) -> dict | None:
    """The money_recovered figure in a fix's service response ("HTTP 200: {...}")."""
    try:
        return json.loads(output.split(": ", 1)[1])["money_recovered"]
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def add_money(total: dict, part: dict) -> dict:
    """Add one money figure (count, by_currency, total_sar) to another."""
    by_currency = dict(total["by_currency"])
    for cur, amount in part["by_currency"].items():
        by_currency[cur] = round(by_currency.get(cur, 0.0) + amount, 2)
    return {"count": total["count"] + part["count"], "by_currency": dict(sorted(by_currency.items())),
            "total_sar": round(total["total_sar"] + part["total_sar"], 2)}


def describe_amount(label: str, m: dict) -> str:
    """e.g. 'unpaid orders: 25 = 4,512.30 SAR (SAR 3,000.00, USD 403.28)'."""
    currencies = ", ".join(f"{cur} {amount:,.2f}" for cur, amount in m["by_currency"].items())
    return f"{label}: {m['count']} = {m['total_sar']:,.2f} SAR" + (f" ({currencies})" if currencies else "")


def describe_money(money: dict | None) -> str:
    """All three exposure figures on one line."""
    if money is None:
        return "unavailable (service unreachable)"
    return "; ".join(describe_amount(label, money[key]) for key, label in
                     [("unpaid_orders", "unpaid orders"), ("queued_orders", "queued orders"),
                      ("ledger_overstatement", "ledger overstated by")])


def attach_money_impact(diagnosis: Diagnosis, before: dict | None, peak: dict | None, fix_applied: bool,
                        recovered: dict, after: dict | None) -> Diagnosis:
    """Overwrite the money fields with figures from code, whatever the model sent, and add them to impact."""
    diagnosis.money_impact_before, diagnosis.money_impact_peak = before, peak
    diagnosis.money_recovered, diagnosis.money_impact_after = recovered, after
    peak_text = describe_money(peak) if fix_applied else "no fix applied"
    recovered_text = (describe_amount("orders paid by reprocessing", recovered["orders_paid"]) + "; "
                      + describe_amount("ledger overstatement corrected", recovered["ledger_corrected"]))
    diagnosis.impact += (f" [Nabd, computed from the DB (USD at 3.75 SAR) - at alert: {describe_money(before)}."
                         f" Just before the first fix (peak): {peak_text}."
                         f" Recovered: {recovered_text}."
                         f" At end of run: {describe_money(after)}.]")
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
              "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "config_before": fetch_config(), "stats": stats, "events": events}
    fix_counts: Counter = Counter()
    start = time.time()
    money_before = fetch_money_impact()  # "before" = when Nabd picks up the alert
    money_peak, fix_applied = None, False  # peak = snapshot just before the first approved fix runs
    money_recovered = {"orders_paid": dict(NO_MONEY), "ledger_corrected": dict(NO_MONEY)}

    def emit(kind: str, **data) -> None:
        event = {"t": round(time.time() - start, 1), "kind": kind, **data}
        events.append(event)
        on_event(kind, data)

    def finish(diagnosis: Diagnosis) -> tuple[Diagnosis, dict]:
        diagnosis = attach_money_impact(diagnosis, money_before, money_peak, fix_applied,
                                        money_recovered, fetch_money_impact())
        record["config_after"] = fetch_config()
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
                    diagnosis, still_broken = verify_recovery_claim(diagnosis)
                    stats["recovery_claim_overridden"] = bool(still_broken)
                    if still_broken:
                        emit("verification", text=f"Recovery claim rejected, still broken: {still_broken}")
                    return finish(enforce_escalation(diagnosis, repeated_fix_blocked=bool(stats["writes_blocked"])))
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
                    if not fix_applied:  # exposure at its peak: the moment before anything is changed
                        money_peak, fix_applied = fetch_money_impact(), True
                    output, is_error = run_tool(block.name, block.input)
                    fix_counts[fix_key] += 1
                    if not is_error:
                        stats["writes_applied"].append(fix_key)
                        part = money_recovered_from(output) if block.name in RECOVERY_TOOLS else None
                        if part:
                            key = RECOVERY_TOOLS[block.name]
                            money_recovered[key] = add_money(money_recovered[key], part)
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
    import report  # imported here: report.py imports this module
    REPLAYS.mkdir(exist_ok=True)
    stem = REPLAYS / f"{datetime.now():%Y%m%d-%H%M%S}_{run['mode']}"
    report_path = report.save(run, stem)
    stem.with_suffix(".json").write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
    print(f"=== SAVED === {stem}.json and {report_path.name}")
