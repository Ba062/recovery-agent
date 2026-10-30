"""The order pipeline: queue -> worker thread(s) -> payment step, plus a ledger sync job.

The bugs that chaos.py triggers live here on purpose, and they are all *silent*:
  - payment errors are swallowed and logged at DEBUG only   (official example 1)
  - a full queue drops the order and only bumps a counter   (official example 2)
  - sync job v1.4 multiplies SAR amounts by 100              (official example 3)
  - a "stuck" worker blocks forever and stops heartbeating   (official example 4)
"""

import os
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
REQUIRED_KEYS = {"version", "payment_provider_url", "queue_max", "worker_count", "sync_interval_s"}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)  # True/False are ints in Python


def validate_config(cfg: dict) -> list[str]:
    """Return a list of problems ([] = valid).

    This only rejects values that would crash or silently kill part of the pipeline.
    "Wrong but meaningful" values (a bad provider URL, a tiny queue_max, version v1.4)
    are allowed on purpose: those are the faults the agent has to diagnose.
    """
    missing = REQUIRED_KEYS - set(cfg)
    if missing:
        return [f"missing keys: {sorted(missing)}"]
    problems = []
    if not isinstance(cfg["version"], str):
        problems.append("version must be a string")
    if not isinstance(cfg["payment_provider_url"], str):
        problems.append("payment_provider_url must be a string")
    if not _is_int(cfg["queue_max"]) or cfg["queue_max"] < 0:
        problems.append("queue_max must be a whole number >= 0")
    if not _is_int(cfg["worker_count"]) or not 1 <= cfg["worker_count"] <= 8:
        problems.append("worker_count must be a whole number from 1 to 8")
    interval = cfg["sync_interval_s"]
    if not (_is_int(interval) or isinstance(interval, float)) or not 0.5 <= interval <= 3600:
        problems.append("sync_interval_s must be a number from 0.5 to 3600")
    return problems


def write_config(cfg: dict) -> None:
    """Validate, then write config.yaml atomically: write a temp file, then swap it in.

    A plain write empties the file first, so the hot-reloader could read a half-written
    file. os.replace swaps the whole file in one step. On Windows the swap fails if the
    reloader has the file open at that instant, so retry briefly.
    """
    problems = validate_config(cfg)
    if problems:
        raise ValueError("invalid config: " + "; ".join(problems))
    tmp = CONFIG_FILE.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, CONFIG_FILE)
            return
        except PermissionError:
            time.sleep(0.05)
    raise RuntimeError(f"could not write {CONFIG_FILE} (file kept busy)")


def call_payment_provider(url: str, amount: float) -> None:
    """Simulated payment provider. Only the correct URL works."""
    time.sleep(random.uniform(0.005, 0.02))
    if url != GOOD_PROVIDER_URL:
        raise ConnectionError(f"payment provider unreachable at {url}")


class Pipeline:
    def __init__(self) -> None:
        self.config = self._read_config()
        problems = validate_config(self.config)
        if problems:
            raise RuntimeError(f"{CONFIG_FILE.name} is invalid: {'; '.join(problems)}")
        self._config_mtime = CONFIG_FILE.stat().st_mtime
        self._bad_config_mtime = None
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
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}

    def _config_watcher(self) -> None:
        """Hot-reload config.yaml every 2s, so config fixes apply without a restart.

        An empty, half-written or invalid file is NOT applied: the last good config stays
        active and the file is read again on the next tick.
        """
        while True:
            time.sleep(2)
            try:
                mtime = CONFIG_FILE.stat().st_mtime
                if mtime == self._config_mtime:
                    continue
                new = self._read_config()
                problems = validate_config(new)
                if problems:
                    if mtime != self._bad_config_mtime:  # log once per bad version, not every 2s
                        log("ERROR", "config", "invalid config ignored, keeping last good config",
                            problems=problems)
                        self._bad_config_mtime = mtime
                    continue  # mtime not saved, so the next tick tries again
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
            try:
                self._process(job)
            except Exception as e:
                # A real error (e.g. the DB can't be written) is logged loudly, and the worker
                # keeps running: one bad job must never kill the thread silently.
                log("ERROR", "worker", f"job failed: {e}", order_id=job["order_id"])

    def _process(self, job: dict) -> None:
        try:
            call_payment_provider(self.config.get("payment_provider_url", ""), job["amount"])
        except Exception as e:
            # BUG (on purpose): the provider error is swallowed and only logged at DEBUG,
            # and the order is still marked processed -> the order silently has no payment.
            log("DEBUG", "payment", f"provider call failed: {e}", order_id=job["order_id"])
        else:
            db.record_payment(job["order_id"], job["amount"])  # DB errors go up to _worker
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
            try:  # the sleep is inside the try too, so nothing can kill this thread silently
                time.sleep(float(self.config.get("sync_interval_s", 5)))
                self.sync_once()
            except Exception as e:
                log("ERROR", "sync", f"ledger sync failed: {e}")
                time.sleep(5)

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
