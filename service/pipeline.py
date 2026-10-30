"""The order pipeline: queue -> worker thread(s) -> payment step, plus a ledger sync job.

The bugs that chaos.py triggers live here on purpose, and they are all *silent*:
  - payment errors are swallowed and logged at DEBUG only   (official example 1)
  - a full queue drops the order and only bumps a counter   (official example 2)
  - sync job v1.4 multiplies SAR amounts by 100              (official example 3)
  - a "stuck" worker blocks forever and stops heartbeating   (official example 4)
"""

import queue
import random
import threading
import time
from pathlib import Path

import yaml

from service import db
from service.jsonlog import log

CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"
GOOD_PROVIDER_URL = "internal://payments-v1"


def call_payment_provider(url: str, amount: float) -> None:
    """Simulated payment provider. Only the correct URL works."""
    time.sleep(random.uniform(0.005, 0.02))
    if url != GOOD_PROVIDER_URL:
        raise ConnectionError(f"payment provider unreachable at {url}")


class Pipeline:
    def __init__(self) -> None:
        self.config = self._read_config()
        self._config_mtime = CONFIG_FILE.stat().st_mtime
        self.jobs: queue.Queue = queue.Queue()  # size limit is checked by hand in submit()
        self.lock = threading.Lock()
        self.enqueued = 0
        self.processed = 0
        self.dropped = 0
        self.last_heartbeat = time.time()

        # Worker restart: Python threads can't be killed, so each worker belongs to a
        # "generation". Restarting bumps the generation and wakes any stuck worker so it exits.
        self.generation = 0
        self.stuck = False                  # set by chaos (worker_hang)
        self.unstick = threading.Event()

    # ------------------------------------------------------------ config
    def _read_config(self) -> dict:
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}

    def _config_watcher(self) -> None:
        """Hot-reload config.yaml every 2s, so config fixes apply without a restart."""
        while True:
            time.sleep(2)
            try:
                mtime = CONFIG_FILE.stat().st_mtime
                if mtime == self._config_mtime:
                    continue
                new = self._read_config()
                changed = {k: {"old": self.config.get(k), "new": v}
                           for k, v in new.items() if self.config.get(k) != v}
                self.config, self._config_mtime = new, mtime
                log("INFO", "config", "config reloaded", changed=changed)
            except Exception as e:
                log("ERROR", "config", f"config reload failed: {e}")

    # ------------------------------------------------------------ start / submit
    def start(self) -> None:
        threading.Thread(target=self._config_watcher, daemon=True, name="config").start()
        threading.Thread(target=self._sync_loop, daemon=True, name="sync").start()
        self._start_workers()
        log("INFO", "pipeline", "pipeline started", version=self.config.get("version"))

    def submit(self, order_id: int, amount: float, currency: str) -> None:
        """Queue an order. If the queue is full the order is silently dropped (the bug)."""
        with self.lock:
            if self.jobs.qsize() >= int(self.config.get("queue_max", 1000)):
                self.dropped += 1
                log("DEBUG", "queue", "queue full, message dropped", order_id=order_id)
                return
            self.enqueued += 1
        self.jobs.put({"order_id": order_id, "amount": amount, "currency": currency})

    # ------------------------------------------------------------ workers
    def _start_workers(self) -> None:
        for i in range(int(self.config.get("worker_count", 1))):
            threading.Thread(target=self._worker, args=(self.generation,), daemon=True,
                             name=f"worker-{self.generation}-{i}").start()

    def restart_workers(self) -> dict:
        """Stop the current workers (even stuck ones) and start fresh ones."""
        old_age = round(time.time() - self.last_heartbeat, 1)
        self.generation += 1
        self.stuck = False
        self.unstick.set()               # wake stuck workers; they see the new generation and exit
        self.unstick = threading.Event()
        self.last_heartbeat = time.time()
        self._start_workers()
        log("INFO", "worker", "workers restarted", generation=self.generation, heartbeat_age_before_s=old_age)
        return {"generation": self.generation, "heartbeat_age_before_s": old_age}

    def _worker(self, generation: int) -> None:
        while generation == self.generation:
            if self.stuck:
                self.unstick.wait()      # blocks forever until restart: no heartbeat, queue grows
                continue
            self.last_heartbeat = time.time()
            try:
                job = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            self._process(job)

    def _process(self, job: dict) -> None:
        try:
            call_payment_provider(self.config.get("payment_provider_url", ""), job["amount"])
            db.record_payment(job["order_id"], job["amount"])
        except Exception as e:
            # BUG (on purpose): the error is swallowed and only logged at DEBUG,
            # and the order is still marked processed -> the order silently has no payment.
            log("DEBUG", "payment", f"provider call failed: {e}", order_id=job["order_id"])
        db.mark_processed(job["order_id"])
        with self.lock:
            self.processed += 1

    def reprocess(self, order_ids: list[int]) -> dict:
        """Retry payment for processed-but-unpaid orders. Safe to run twice: never double-charges."""
        result = {"paid": 0, "skipped_already_paid": 0, "failed": 0}
        for order_id in order_ids:
            order = db.get_order(order_id)
            if order is None:
                result["failed"] += 1
                continue
            try:
                call_payment_provider(self.config.get("payment_provider_url", ""), order["amount"])
            except Exception:
                result["failed"] += 1
                continue
            if db.record_payment(order_id, order["amount"]):
                result["paid"] += 1
            else:
                result["skipped_already_paid"] += 1
        log("INFO", "admin", "reprocess finished", **result)
        return result

    # ------------------------------------------------------------ ledger sync job
    def _sync_loop(self) -> None:
        while True:
            time.sleep(float(self.config.get("sync_interval_s", 5)))
            try:
                self.sync_once()
            except Exception as e:
                log("ERROR", "sync", f"ledger sync failed: {e}")

    def sync_once(self) -> int:
        """Copy paid orders into the ledger."""
        version = self.config.get("version")
        rows = db.paid_orders_not_in_ledger()
        for row in rows:
            amount = row["amount"]
            if version == "v1.4" and row["currency"] == "SAR":
                amount = amount * 100    # BUG in v1.4 (on purpose): halalas/riyals mix-up
            db.add_ledger_entry(row["id"], amount, row["currency"])
        if rows:
            log("DEBUG", "sync", "ledger synced", rows=len(rows), version=version)
        return len(rows)

    # ------------------------------------------------------------ metrics
    def stats(self) -> dict:
        return {
            "enqueued": self.enqueued,
            "processed": self.processed,
            "dropped": self.dropped,
            "queue_depth": self.jobs.qsize(),
            "worker_heartbeat_age_s": round(time.time() - self.last_heartbeat, 1),
        }
