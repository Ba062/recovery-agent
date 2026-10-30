"""FastAPI front end for the order pipeline.

Run from the project root:  python -m uvicorn service.app:app --port 8000
(or use supervisor.py, which does this in the background).

/health is deliberately shallow (process + DB readable), so silent failures stay "green".
/business-metrics is where the real truth is: orders vs payments, queue, heartbeat, ledger.
"""

import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Literal

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from service import db
from service.jsonlog import log
from service.pipeline import Pipeline

pipeline: Pipeline | None = None
recent_requests: deque = deque(maxlen=500)  # (latency_ms, status_code) of the last 500 requests
request_count = 0


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
        recent_requests.append((latency_ms, response.status_code))
        request_count += 1
        level = "ERROR" if response.status_code >= 500 else "INFO"
        log(level, "api", "request", path=request.url.path, status=response.status_code, latency_ms=latency_ms)
    return response


# ---------------------------------------------------------------- traffic
class OrderIn(BaseModel):
    amount: float = Field(gt=0)
    currency: Literal["SAR", "USD"] = "SAR"


@app.post("/orders")
def create_order(order: OrderIn):
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
    latencies = sorted(lat for lat, _ in recent_requests)
    errors = sum(1 for _, status in recent_requests if status >= 500)
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else 0.0
    return {
        "error_rate": round(errors / len(recent_requests), 3) if recent_requests else 0.0,
        "p95_latency_ms": p95,
        "memory_mb": round(psutil.Process().memory_info().rss / 1e6, 1),
        "request_count": request_count,
    }


@app.get("/business-metrics")
def business_metrics():
    """Business invariants: is the business actually working?"""
    return {**db.counts(), **pipeline.stats()}


# ---------------------------------------------------------------- admin (used by the agent's write tools)
class OrderIdsIn(BaseModel):
    order_ids: list[int] | Literal["all_unpaid", "all_flagged"]


@app.post("/admin/restart-worker")
def restart_worker():
    return pipeline.restart_workers()


@app.post("/admin/reprocess")
def reprocess(body: OrderIdsIn):
    ids = db.unpaid_processed_order_ids() if body.order_ids == "all_unpaid" else body.order_ids
    return pipeline.reprocess(ids)


@app.post("/admin/repair-ledger")
def repair_ledger(body: OrderIdsIn):
    ids = db.mismatched_ledger_order_ids() if body.order_ids == "all_flagged" else body.order_ids
    fixed = sum(1 for order_id in ids if db.fix_ledger_entry(order_id))
    log("INFO", "admin", "ledger repaired", fixed=fixed)
    return {"fixed": fixed}
