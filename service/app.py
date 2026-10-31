"""FastAPI front end for the order pipeline.

Run from the project root:  python -m uvicorn service.app:app --port 8000
(or use supervisor.py, which does this in the background).

/health is deliberately shallow (process + DB readable), so silent failures stay "green".
/business-metrics is where the real truth is: orders vs payments, queue, heartbeat, ledger.
"""

import itertools
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Literal

import psutil
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from service import db
from service.jsonlog import log
from service.pipeline import Pipeline

pipeline: Pipeline | None = None
METRICS_WINDOW_S = 30  # error rate + latency cover the last 30 s, so they clear 30 s after a fix
recent_requests: deque = deque()  # (time, latency_ms, status_code) of /orders requests in the window
request_count = 0
v17_requests = itertools.count()  # numbers /orders requests for the v1.7 fault hook


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pipeline
    db.init_db()
    pipeline = Pipeline()
    pipeline.start()
    log("INFO", "api", "service started")
    yield
    log("INFO", "api", "service stopping")


app = FastAPI(title="Nabd target: order pipeline", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """422 without echoing the input back: an input like NaN can't be written as JSON,
    so FastAPI's default error response would itself crash with a 500."""
    errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.middleware("http")
async def track_requests(request: Request, call_next):
    """Record latency + status for /metrics, and turn crashes into logged 500s."""
    global request_count
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:
        log("ERROR", "api", f"unhandled error: {e}", path=request.url.path)
        response = JSONResponse({"error": str(e)}, status_code=500)
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    if request.url.path == "/orders":  # only real traffic counts towards error rate / latency
        now = time.monotonic()
        recent_requests.append((now, latency_ms, response.status_code))
        while recent_requests and recent_requests[0][0] < now - METRICS_WINDOW_S:
            recent_requests.popleft()  # older than the window
        request_count += 1
        level = "ERROR" if response.status_code >= 500 else "INFO"
        log(level, "api", "request", path=request.url.path, status=response.status_code, latency_ms=latency_ms)
    return response


# ---------------------------------------------------------------- traffic
class OrderIn(BaseModel):
    amount: float = Field(gt=0, le=1_000_000, allow_inf_nan=False)  # "1e309" would be infinity
    currency: Literal["SAR", "USD"] = "SAR"


@app.post("/orders")
def create_order(order: OrderIn):
    if pipeline.config.get("version") == "v1.5":  # held-out fault: BUG in v1.5's order validation (on purpose)
        raise RuntimeError("order validation failed: unknown field 'currency_code'")  # -> 500, logged at ERROR
    if pipeline.config.get("version") == "v1.7" and next(v17_requests) % 10 in (0, 3, 6):
        # v2 scenario partial_api_errors (eval/V2_SCENARIOS.md): BUG in v1.7 fails 3 in 10 orders (on purpose)
        raise RuntimeError("order rejected: rate limiter misread request headers")  # -> 500, logged at ERROR
    order_id = db.create_order(order.amount, order.currency)
    pipeline.submit(order_id, order.amount, order.currency)
    return {"id": order_id, "status": "accepted"}  # always "accepted", even if the queue dropped it


# ---------------------------------------------------------------- monitoring
@app.get("/health")
def health():
    db.ping()
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """Technical metrics only. These mostly look fine during silent failures."""
    cutoff = time.monotonic() - METRICS_WINDOW_S  # also drop old requests when traffic has stopped
    window = [(lat, status) for t, lat, status in list(recent_requests) if t >= cutoff]
    latencies = sorted(lat for lat, _ in window)
    errors = sum(1 for _, status in window if status >= 500)
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else 0.0
    return {
        "error_rate": round(errors / len(window), 3) if window else 0.0,
        "p95_latency_ms": p95,
        "memory_mb": round(psutil.Process().memory_info().rss / 1e6, 1),
        "request_count": request_count,
    }


@app.get("/business-metrics")
def business_metrics():
    """Business invariants: is the business actually working?"""
    return {**db.counts(), **pipeline.stats(), "money_impact": db.money_impact()}


@app.get("/config")
def live_config():
    """The config the pipeline is ACTUALLY using right now (it may lag config.yaml by ~2s)."""
    return pipeline.config


@app.get("/data-integrity")
def data_integrity():
    """Which orders are affected (read-only). Gives the agent exact ids and impact."""
    unpaid = db.unpaid_processed_order_ids()
    mismatched = db.mismatched_ledger_order_ids()
    return {
        "unpaid_processed_orders": {"count": len(unpaid), "sample_ids": unpaid[:10]},
        "ledger_mismatches": {"count": len(mismatched), "sample": db.ledger_mismatch_details(10)},
        "money_impact": db.money_impact(),
    }


# ---------------------------------------------------------------- admin (used by the agent's write tools)
# Each endpoint accepts only ITS keyword. A shared model let "all_flagged" reach /reprocess,
# where the string was looped over letter by letter.
class ReprocessIn(BaseModel):
    order_ids: list[int] | Literal["all_unpaid"]


class RepairLedgerIn(BaseModel):
    order_ids: list[int] | Literal["all_flagged"]


@app.post("/admin/restart-worker")
def restart_worker():
    return pipeline.restart_workers()


@app.post("/admin/reprocess")
def reprocess(body: ReprocessIn):
    ids = db.unpaid_processed_order_ids() if body.order_ids == "all_unpaid" else body.order_ids
    return pipeline.reprocess(ids)


class ChaosIn(BaseModel):
    fault: Literal["worker_hang", "provider_outage"]  # provider_outage: held-out eval only


@app.post("/admin/chaos")
def chaos(body: ChaosIn):
    """Used ONLY by chaos.py / the eval to inject in-process faults. Not one of the agent's tools."""
    if body.fault == "worker_hang":
        pipeline.stuck = True
        log("DEBUG", "worker", "worker entered wait state")  # the only (quiet) trace of the hang
    else:  # provider_outage: nothing is logged here; only the failing payment calls show it (at DEBUG)
        pipeline.provider_down = True
    return {"injected": body.fault}


@app.post("/admin/repair-ledger")
def repair_ledger(body: RepairLedgerIn):
    ids = db.mismatched_ledger_order_ids() if body.order_ids == "all_flagged" else body.order_ids
    corrected = db.ledger_overstatement_of(ids)  # measured before the fix: what the repair removes
    fixed = sum(1 for order_id in ids if db.fix_ledger_entry(order_id))
    log("INFO", "admin", "ledger repaired", fixed=fixed)
    return {"fixed": fixed, "money_recovered": corrected}
