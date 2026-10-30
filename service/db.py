"""SQLite schema and helpers for the order pipeline.

Each call opens its own short-lived connection, so it is safe to use from any thread.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "nabd.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    amount     REAL NOT NULL,
    currency   TEXT NOT NULL,
    status     TEXT NOT NULL,          -- 'queued' -> 'processed'
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL UNIQUE, -- UNIQUE = an order can never be charged twice
    amount     REAL NOT NULL,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    order_id   INTEGER PRIMARY KEY,
    amount     REAL NOT NULL,
    currency   TEXT NOT NULL,
    synced_at  REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def ping() -> None:
    """Read-only check used by the shallow /health endpoint."""
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()


# ---------------------------------------------------------------- orders + payments
def create_order(amount: float, currency: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO orders (amount, currency, status, created_at) VALUES (?, ?, 'queued', ?)",
            (amount, currency, time.time()),
        )
        return cur.lastrowid


def mark_processed(order_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE orders SET status = 'processed' WHERE id = ?", (order_id,))


def get_order(order_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def record_payment(order_id: int, amount: float) -> bool:
    """Insert a payment. Returns False if the order was already paid (no double charge)."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO payments (order_id, amount, status, created_at) VALUES (?, ?, 'captured', ?)",
            (order_id, amount, time.time()),
        )
        return cur.rowcount == 1


def unpaid_processed_order_ids() -> list[int]:
    """Orders the worker marked 'processed' but that have no payment: the silent payment drop."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT o.id FROM orders o LEFT JOIN payments p ON p.order_id = o.id "
            "WHERE o.status = 'processed' AND p.id IS NULL ORDER BY o.id"
        ).fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- ledger
def paid_orders_not_in_ledger() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT o.id, o.amount, o.currency FROM orders o "
            "JOIN payments p ON p.order_id = o.id "
            "LEFT JOIN ledger l ON l.order_id = o.id WHERE l.order_id IS NULL"
        ).fetchall()


def add_ledger_entry(order_id: int, amount: float, currency: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ledger (order_id, amount, currency, synced_at) VALUES (?, ?, ?, ?)",
            (order_id, amount, currency, time.time()),
        )


def mismatched_ledger_order_ids() -> list[int]:
    """Ledger rows whose amount differs from the order: the silent data corruption."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.order_id FROM ledger l JOIN orders o ON o.id = l.order_id "
            "WHERE ABS(l.amount - o.amount) > 0.001 ORDER BY l.order_id"
        ).fetchall()
    return [r["order_id"] for r in rows]


def ledger_mismatch_details(limit: int = 10) -> list[dict]:
    """A sample of mismatched ledger rows with both amounts, so the cause is visible."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.order_id, o.amount AS order_amount, l.amount AS ledger_amount, o.currency "
            "FROM ledger l JOIN orders o ON o.id = l.order_id "
            "WHERE ABS(l.amount - o.amount) > 0.001 ORDER BY l.order_id LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def fix_ledger_entry(order_id: int) -> bool:
    """Copy the correct amount from orders back into the ledger."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE ledger SET amount = (SELECT amount FROM orders WHERE id = ?) WHERE order_id = ?",
            (order_id, order_id),
        )
        return cur.rowcount == 1


# ---------------------------------------------------------------- money impact
# Computed here from the DB, never by the LLM. The API only accepts SAR and USD.
RATES_TO_SAR = {"SAR": 1.0, "USD": 3.75}  # the riyal is pegged to the dollar at 3.75


def sum_money(rows: list) -> dict:
    """Sum (amount, currency) rows per currency, plus one total in SAR."""
    by_currency: dict[str, float] = {}
    for row in rows:
        by_currency[row["currency"]] = by_currency.get(row["currency"], 0.0) + row["amount"]
    total_sar = sum(amount * RATES_TO_SAR[cur] for cur, amount in by_currency.items())
    return {"count": len(rows),
            "by_currency": {cur: round(amount, 2) for cur, amount in sorted(by_currency.items())},
            "total_sar": round(total_sar, 2)}


def money_impact() -> dict:
    """What each silent failure is costing right now, in money:
    - unpaid_orders: processed but never charged (payment_drop)
    - queued_orders: accepted but not processed yet, i.e. delayed (worker_hang).
      A few in-flight orders are normal; a stuck or failed job also stays queued.
    - ledger_overstatement: ledger amount minus real order amount, over mismatched rows (data_corruption)
    """
    with connect() as conn:
        unpaid = conn.execute(
            "SELECT o.amount, o.currency FROM orders o LEFT JOIN payments p ON p.order_id = o.id "
            "WHERE o.status = 'processed' AND p.id IS NULL").fetchall()
        queued = conn.execute("SELECT amount, currency FROM orders WHERE status = 'queued'").fetchall()
        overstated = conn.execute(
            "SELECT l.amount - o.amount AS amount, o.currency FROM ledger l JOIN orders o ON o.id = l.order_id "
            "WHERE ABS(l.amount - o.amount) > 0.001").fetchall()
    return {"unpaid_orders": sum_money(unpaid), "queued_orders": sum_money(queued),
            "ledger_overstatement": sum_money(overstated), "rates_to_sar": RATES_TO_SAR}


def order_money(order_ids: list[int]) -> dict:
    """Value of these orders (e.g. the ones a reprocess just paid)."""
    if not order_ids:
        return sum_money([])
    with connect() as conn:
        rows = conn.execute(f"SELECT amount, currency FROM orders WHERE id IN ({','.join('?' * len(order_ids))})",
                            order_ids).fetchall()
    return sum_money(rows)


def ledger_overstatement_of(order_ids: list[int]) -> dict:
    """How much these ledger rows overstate their orders right now. Read BEFORE repairing them,
    so the result is the overstatement the repair removes."""
    if not order_ids:
        return sum_money([])
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.amount - o.amount AS amount, o.currency FROM ledger l JOIN orders o ON o.id = l.order_id "
            f"WHERE ABS(l.amount - o.amount) > 0.001 AND l.order_id IN ({','.join('?' * len(order_ids))})",
            order_ids).fetchall()
    return sum_money(rows)


# ---------------------------------------------------------------- counts for /business-metrics
def counts() -> dict:
    with connect() as conn:
        orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        payments = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    return {
        "orders_created": orders,
        "payments_recorded": payments,
        "orders_without_payment": len(unpaid_processed_order_ids()),
        "integrity_failures": len(mismatched_ledger_order_ids()),
    }
