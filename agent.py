"""
Recovery agent: Claude Sonnet 5.5 + a hand-written tool-calling loop.

Flow: alert -> read-only investigation -> hypothesis -> (human-approved) fix
      -> verify health -> submit_diagnosis (structured, Pydantic-validated).

Setup:
    pip install -r requirements.txt
    .env  ->  ANTHROPIC_API_KEY=sk-ant-...
Run:
    python agent.py "Error rate 45% on /orders, health check failing"
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import requests
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

load_dotenv()
client = Anthropic()  # reads ANTHROPIC_API_KEY

MODEL = "claude-sonnet-5-5"
EFFORT = "medium"  # low | medium | high | xhigh | max  (no temperature on Sonnet 5.5)
MAX_STEPS = 10
MIN_CONFIDENCE = 0.6  # below this the agent must escalate (enforced in code, not just the prompt)

# Paths are relative to this file, so it works no matter where it is launched from
# (e.g. `streamlit run ui/app.py` or `python eval/run_eval.py`).
ROOT = Path(__file__).resolve().parent
SERVICE_URL = os.getenv("SERVICE_URL", "http://127.0.0.1:8000")
LOG_FILE = ROOT / "service" / "logs" / "app.jsonl"
CONFIG_FILE = ROOT / "service" / "config.yaml"
DEPLOY_LOG = ROOT / "service" / "deploys.jsonl"
SUPERVISOR = ROOT / "supervisor.py"


# ---------------------------------------------------------------- output schema
class Diagnosis(BaseModel):
    root_cause: str = Field(description="Most likely root cause, one sentence")
    evidence: list[str] = Field(description="Exact log lines / metric values supporting it")
    confidence: float = Field(ge=0, le=1, description="0-1 confidence in the root cause")
    action_taken: str = Field(description="Fix applied, or 'none'")
    recovered: bool = Field(description="True only if check_health passed after the fix")
    escalate: bool = Field(description="True if a human must take over")
    summary: str = Field(description="2-3 sentence incident summary for the on-call engineer")


# ---------------------------------------------------------------- tools (read-only)
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def read_logs(min_level: str = "WARNING", last_n: int = 40) -> str:
    """Filter in Python first so the LLM only sees what matters (cheaper + more accurate)."""
    if not LOG_FILE.exists():
        return "Log file not found."
    out = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines()[-3000:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if LEVELS.get(rec.get("level", "INFO"), 20) >= LEVELS.get(min_level, 30):
            out.append(line)
    return "\n".join(out[-last_n:]) or f"No log lines at {min_level} or above."


def get_metrics() -> str:
    try:
        return json.dumps(requests.get(f"{SERVICE_URL}/metrics", timeout=3).json())
    except Exception as e:
        return f"Metrics endpoint unreachable: {e}"


def check_health() -> str:
    try:
        r = requests.get(f"{SERVICE_URL}/health", timeout=3)
        return f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return f"Service unreachable: {e}"


def read_config() -> str:
    return CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else "Config not found."


def recent_changes() -> str:
    if not DEPLOY_LOG.exists():
        return "No deploy history."
    return "\n".join(DEPLOY_LOG.read_text(encoding="utf-8").splitlines()[-5:])


# ---------------------------------------------------------------- tools (write: need approval)
def _supervisor(cmd: str) -> str:
    r = subprocess.run([sys.executable, str(SUPERVISOR), cmd], cwd=ROOT,
                       capture_output=True, text=True, timeout=30)
    time.sleep(3)  # give the service time to come back
    return f"{r.stdout.strip()} {r.stderr.strip()}\nHealth now: {check_health()}"


def restart_service() -> str:
    return _supervisor("restart")


def rollback_deploy() -> str:
    return _supervisor("rollback")


def set_config(key: str, value: str) -> str:
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    old = cfg.get(key)
    cfg[key] = yaml.safe_load(value)  # "8000" -> int, "true" -> bool
    CONFIG_FILE.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return f"{key}: {old!r} -> {cfg[key]!r}\n" + _supervisor("restart")


TOOL_FUNCS: dict[str, Callable[..., str]] = {
    "read_logs": read_logs, "get_metrics": get_metrics, "check_health": check_health,
    "read_config": read_config, "recent_changes": recent_changes,
    "restart_service": restart_service, "rollback_deploy": rollback_deploy, "set_config": set_config,
}
WRITE_TOOLS = {"restart_service", "rollback_deploy", "set_config"}


def _tool(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}


TOOLS = [
    _tool("read_logs", "Read recent service logs at or above a severity level.",
          {"min_level": {"type": "string", "enum": list(LEVELS)},
           "last_n": {"type": "integer", "minimum": 1, "maximum": 200}}),
    _tool("get_metrics", "Current metrics: error rate, p95 latency, memory, request count."),
    _tool("check_health", "Call the service health endpoint."),
    _tool("read_config", "Read the service config file."),
    _tool("recent_changes", "Last 5 deploys/config changes with timestamps."),
    _tool("restart_service", "[WRITE - needs human approval] Restart the service process."),
    _tool("rollback_deploy", "[WRITE - needs human approval] Roll back to the previous deploy."),
    _tool("set_config", "[WRITE - needs human approval] Set one top-level config key, then restart.",
          {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    _tool("submit_diagnosis", "Submit your final structured diagnosis. Always finish by calling this.",
          Diagnosis.model_json_schema()["properties"], list(Diagnosis.model_fields)),
]

SYSTEM_PROMPT = """You are an on-call SRE agent recovering a failing web service.

Process:
1. Investigate with read-only tools first (logs, metrics, health, config, recent changes).
2. Form a hypothesis and confirm it with concrete evidence. Quote exact log lines.
3. Choose the smallest, safest fix that addresses the ROOT CAUSE. Do not restart blindly
   if logs point to bad config or a bad deploy - a restart will not fix those.
4. Apply one write action at a time, then call check_health to verify.
5. If health still fails, try your next hypothesis. If you are under 0.6 confident,
   the fault does not match anything you can explain, or a human rejects your action,
   stop and escalate rather than guessing.
6. Always finish by calling submit_diagnosis.

Write actions require human approval and may be rejected."""


# ---------------------------------------------------------------- approval + loop
def cli_approve(name: str, args: dict) -> bool:
    return input(f"\nAgent wants to run {name}({args}). Approve? [y/N] ").strip().lower() == "y"


def run_tool(name: str, args: dict) -> str:
    """Run an allow-listed tool. Errors go back to the model instead of crashing the loop."""
    try:
        return TOOL_FUNCS[name](**args)
    except Exception as e:
        return f"Tool error: {e}"


def enforce_escalation(diagnosis: Diagnosis) -> Diagnosis:
    """Safety rule in code: low confidence always escalates, whatever the model said."""
    if diagnosis.confidence < MIN_CONFIDENCE:
        diagnosis.escalate = True
    return diagnosis


def run_agent(alert: str,
              approve: Callable[[str, dict], bool] = cli_approve,
              on_event: Callable[..., None] = lambda *e: print(*e)) -> tuple[Diagnosis, dict]:
    messages = [{"role": "user", "content": f"ALERT from monitor:\n{alert}\n\nInvestigate and recover the service."}]
    stats = {"steps": 0, "input_tokens": 0, "output_tokens": 0, "start": time.time()}

    for step in range(1, MAX_STEPS + 1):
        stats["steps"] = step
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,  # leave headroom: adaptive thinking is on by default
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            extra_body={"output_config": {"effort": EFFORT}},
        )
        stats["input_tokens"] += resp.usage.input_tokens
        stats["output_tokens"] += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})  # keep thinking blocks intact

        for block in resp.content:
            if block.type == "text" and block.text.strip():
                on_event("THOUGHT:", block.text)

        # Check for tool calls rather than stop_reason: if the reply was cut off at max_tokens
        # mid tool call, every tool_use still needs a matching tool_result or the API returns 400.
        tool_calls = [block for block in resp.content if block.type == "tool_use"]
        if not tool_calls:
            messages.append({"role": "user", "content": "Call submit_diagnosis now with your final findings."})
            continue

        results = []
        wrote_this_turn = False
        for block in tool_calls:
            on_event("TOOL:", block.name, block.input)

            if block.name == "submit_diagnosis":
                try:
                    diagnosis = Diagnosis(**block.input)
                except ValidationError as e:
                    # Bad fields (e.g. confidence "high"): tell the model and let it retry.
                    output = f"Invalid diagnosis, fix these fields and call submit_diagnosis again:\n{e}"
                else:
                    stats["seconds"] = round(time.time() - stats.pop("start"), 1)
                    return enforce_escalation(diagnosis), stats
            elif block.name in WRITE_TOOLS:
                # Only one write action per turn, so each fix is verified before the next.
                if wrote_this_turn:
                    output = "SKIPPED: only one write action per turn. Call check_health first."
                elif not approve(block.name, block.input):
                    output = "REJECTED by human operator. Pick another action or escalate."
                else:
                    output = run_tool(block.name, block.input)
                wrote_this_turn = True
            else:
                output = run_tool(block.name, block.input)

            on_event("RESULT:", output[:400])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})

    stats["seconds"] = round(time.time() - stats.pop("start"), 1)
    return Diagnosis(root_cause="Unknown", evidence=[], confidence=0.0, action_taken="none",
                     recovered=False, escalate=True,
                     summary=f"Step limit ({MAX_STEPS}) reached without resolution. Escalating."), stats


if __name__ == "__main__":
    alert = sys.argv[1] if len(sys.argv) > 1 else "Health check failing, error rate elevated."
    diagnosis, stats = run_agent(alert)
    print("\n=== DIAGNOSIS ===")
    print(diagnosis.model_dump_json(indent=2))
    print("=== STATS ===", stats)