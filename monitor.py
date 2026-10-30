"""Rule-based monitor (NO LLM): checks business invariants, not just /health.

Polls /health, /metrics and /business-metrics. When any rule breaks, it bundles ALL active
anomalies (sorted so data loss comes first) into one alert. The LLM agent is only called then.

Usage:
    python monitor.py          poll every few seconds, print an alert when something breaks
    python monitor.py --once   one check (two snapshots a few seconds apart), print result
"""

import json
import os
import sys
import time
from dataclasses import asdict, dataclass

import requests

SERVICE_URL = os.getenv("SERVICE_URL", "http://127.0.0.1:8000")
POLL_SECONDS = 3

# Thresholds: a rule fires only when the value is ABOVE its threshold (exactly equal = OK).
MAX_UNPAID_ORDERS = 0          # processed orders with no payment: healthy = 0
MAX_INTEGRITY_FAILURES = 0     # ledger rows that don't match their order: healthy = 0
MAX_HEARTBEAT_AGE_S = 15       # worker heartbeats about every second
MAX_QUEUE_DEPTH = 20           # healthy queue drains almost instantly
MAX_ERROR_RATE = 0.05          # share of /orders requests that returned 5xx
MAX_P95_LATENCY_MS = 500

# Lower number = more urgent. Losing money/data beats a stuck queue beats slowness.
PRIORITY = {"service_down": 0, "payment_gap": 1, "messages_dropped": 1, "ledger_mismatch": 2,
            "high_error_rate": 2, "worker_stalled": 3, "queue_backlog": 4, "high_latency": 5}


@dataclass
class Anomaly:
    rule: str
    kind: str          # "business" (invariant) or "technical"
    value: float
    threshold: float
    message: str

    @property
    def priority(self) -> int:
        return PRIORITY[self.rule]


# ---------------------------------------------------------------- reading the service
def _get_json(path: str) -> dict | None:
    """GET an endpoint. Returns None on any error (timeout, non-200, bad JSON) instead of raising."""
    try:
        r = requests.get(f"{SERVICE_URL}{path}", timeout=3)
        r.raise_for_status()  # a 500 body like {"error": ...} must not be read as metrics
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def snapshot() -> dict:
    """One reading of the service. Never raises: an unreachable service is itself a finding."""
    return {
        "ts": time.time(),
        "health_ok": _get_json("/health") is not None,
        "metrics": _get_json("/metrics"),
        "business": _get_json("/business-metrics"),
    }


# ---------------------------------------------------------------- the rules
def evaluate(prev: dict, cur: dict) -> list[Anomaly]:
    """Compare two snapshots and return every broken rule, most urgent first.

    Two snapshots are needed for trends: a counter that is still rising (dropped messages)
    or a queue that keeps growing. A single reading can't tell "broke once" from "breaking now".
    """
    if not cur["health_ok"]:
        return [Anomaly("service_down", "technical", 0, 1, "Health check failing or service unreachable.")]
    if cur["business"] is None or cur["metrics"] is None:
        return [Anomaly("service_down", "technical", 0, 1,
                        "Health check passes but /metrics or /business-metrics is failing.")]

    found = []
    b, m = cur["business"], cur["metrics"]
    pb = prev["business"] or b  # if the previous reading failed, compare against now (no trend)

    # Counters reset to 0 when the service restarts. If a counter went DOWN, the service
    # restarted in between, so everything counted since then is new.
    restarted = b["enqueued"] < pb["enqueued"]
    if restarted:
        pb = {**pb, "dropped": 0, "queue_depth": 0}

    # --- business invariants (these catch the silent failures)
    if b["orders_without_payment"] > MAX_UNPAID_ORDERS:
        found.append(Anomaly("payment_gap", "business", b["orders_without_payment"], MAX_UNPAID_ORDERS,
                             f"{b['orders_without_payment']} of {b['orders_created']} orders were processed "
                             f"but have no payment."))
    new_drops = b["dropped"] - pb["dropped"]
    if new_drops > 0:
        found.append(Anomaly("messages_dropped", "business", new_drops, 0,
                             f"{new_drops} orders dropped from the queue in the last "
                             f"{cur['ts'] - prev['ts']:.0f}s (enqueued {b['enqueued']}, processed {b['processed']})."))
    if b["integrity_failures"] > MAX_INTEGRITY_FAILURES:
        found.append(Anomaly("ledger_mismatch", "business", b["integrity_failures"], MAX_INTEGRITY_FAILURES,
                             f"{b['integrity_failures']} ledger entries don't match their order amount."))
    if b["worker_heartbeat_age_s"] > MAX_HEARTBEAT_AGE_S:
        found.append(Anomaly("worker_stalled", "business", b["worker_heartbeat_age_s"], MAX_HEARTBEAT_AGE_S,
                             f"Worker heartbeat is {b['worker_heartbeat_age_s']:.0f}s old "
                             f"(queue depth {b['queue_depth']})."))
    if b["queue_depth"] > MAX_QUEUE_DEPTH and b["queue_depth"] > pb["queue_depth"]:
        found.append(Anomaly("queue_backlog", "business", b["queue_depth"], MAX_QUEUE_DEPTH,
                             f"Queue depth {b['queue_depth']} and growing (was {pb['queue_depth']})."))

    # --- technical metrics (classic monitoring; mostly green during silent failures)
    if m["error_rate"] > MAX_ERROR_RATE:
        found.append(Anomaly("high_error_rate", "technical", m["error_rate"], MAX_ERROR_RATE,
                             f"{m['error_rate']:.0%} of recent /orders requests failed with 5xx."))
    if m["p95_latency_ms"] > MAX_P95_LATENCY_MS:
        found.append(Anomaly("high_latency", "technical", m["p95_latency_ms"], MAX_P95_LATENCY_MS,
                             f"p95 latency {m['p95_latency_ms']:.0f}ms."))

    return sorted(found, key=lambda a: a.priority)


def check_once(gap_seconds: float = POLL_SECONDS) -> list[Anomaly]:
    """Two snapshots a few seconds apart, then evaluate. Used for one-off checks and verification."""
    first = snapshot()
    time.sleep(gap_seconds)
    return evaluate(first, snapshot())


# ---------------------------------------------------------------- alert bundle
def build_alert(anomalies: list[Anomaly], snap: dict) -> dict:
    return {
        "ts": snap["ts"],
        "health_check": "passing" if snap["health_ok"] else "failing",
        "anomalies": [{**asdict(a), "priority": a.priority} for a in anomalies],
        "business_metrics": snap["business"],
        "technical_metrics": snap["metrics"],
    }


def format_alert(alert: dict) -> str:
    """Plain-text alert for the agent's first message."""
    lines = [f"Health check: {alert['health_check']}. {len(alert['anomalies'])} anomaly(ies), most urgent first:"]
    for a in alert["anomalies"]:
        lines.append(f"- [{a['kind']}] {a['rule']}: {a['message']}")
    lines.append(f"Business metrics: {json.dumps(alert['business_metrics'])}")
    lines.append(f"Technical metrics: {json.dumps(alert['technical_metrics'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- polling loop
def run(on_alert=lambda alert: print("\n=== ALERT ===\n" + format_alert(alert) + "\n"),
        poll_seconds: float = POLL_SECONDS) -> None:
    """Poll forever. Fire on_alert ONCE per incident, then stay quiet until everything is clear."""
    prev = snapshot()
    in_incident = False
    while True:
        time.sleep(poll_seconds)
        cur = snapshot()
        try:
            anomalies = evaluate(prev, cur)
        except Exception as e:
            # A monitor that dies silently is the worst failure: log it, skip this reading, keep going.
            print(f"{time.strftime('%H:%M:%S')}  monitor error, skipping this reading: {e!r}")
            continue
        if anomalies and not in_incident:
            in_incident = True
            on_alert(build_alert(anomalies, cur))
        elif not anomalies and in_incident:
            in_incident = False
            print("=== RECOVERED: all invariants green ===")
        else:
            status = ", ".join(a.rule for a in anomalies) or "all green"
            print(f"{time.strftime('%H:%M:%S')}  {status}")
        prev = cur


if __name__ == "__main__":
    if "--once" in sys.argv:
        found = check_once()
        print(format_alert(build_alert(found, snapshot())) if found else "All invariants green.")
    else:
        run()
