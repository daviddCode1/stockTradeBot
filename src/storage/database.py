"""SQLite storage (replaceable with PostgreSQL later: all SQL lives here and in repositories.py).

Uses only portable SQL (no SQLite-specific features beyond `INSERT OR REPLACE`, which maps to
`ON CONFLICT ... DO UPDATE` in PostgreSQL).
"""
from __future__ import annotations

import sqlite3  # built-in database
from pathlib import Path  # file location

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    ts TEXT NOT NULL, mode TEXT NOT NULL, account_ref TEXT, currency TEXT,
    total_value REAL, cash_available REAL, invested_value REAL, strategy_equity REAL
);
CREATE TABLE IF NOT EXISTS universe (
    session TEXT NOT NULL, ticker TEXT NOT NULL, broker_ticker TEXT, PRIMARY KEY (session, ticker)
);
CREATE TABLE IF NOT EXISTS signals (
    session TEXT NOT NULL, ticker TEXT NOT NULL, rank REAL, signal REAL, action TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, session TEXT, ticker TEXT NOT NULL,
    broker_ticker TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, order_type TEXT NOT NULL,
    limit_px REAL, stop_px REAL, purpose TEXT NOT NULL, status TEXT NOT NULL, broker_order_id TEXT,
    updated_at TEXT, note TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    broker_order_id TEXT PRIMARY KEY, ticker TEXT, side TEXT, qty REAL, price REAL, fx_rate REAL,
    fees_gbp REAL, net_value_gbp REAL, filled_at TEXT, order_type TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, broker_ticker TEXT NOT NULL, qty REAL NOT NULL, entry_px REAL NOT NULL,
    entry_date TEXT NOT NULL, stop REAL NOT NULL, initial_stop REAL NOT NULL, target REAL NOT NULL,
    sector TEXT, initial_risk_gbp REAL, highest_close REAL, partial_done INTEGER DEFAULT 0,
    stop_order_id TEXT, status TEXT NOT NULL, opened_at TEXT, closed_at TEXT, exit_reason TEXT,
    exit_px REAL, target_r REAL
);
CREATE TABLE IF NOT EXISTS risk_log (ts TEXT NOT NULL, ticker TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS daily_pnl (
    session TEXT PRIMARY KEY, equity_gbp REAL, cash_gbp REAL, managed_value_gbp REAL,
    heat_gbp REAL, n_positions INTEGER
);
CREATE TABLE IF NOT EXISTS params (ts TEXT NOT NULL, settings_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (ts TEXT NOT NULL, event TEXT NOT NULL, ticker TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


class Database:
    """Thin connection wrapper; one instance per process."""

    def __init__(self, path: Path | str):
        self.path = str(path)  # ':memory:' allowed in tests
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)  # create folder
        self.conn = sqlite3.connect(self.path)  # open (creates file if missing)
        self.conn.row_factory = sqlite3.Row  # dict-like rows
        self.conn.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None  # crash-safe writes
        self.conn.executescript(SCHEMA)  # idempotent schema creation
        self.conn.commit()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        """Run one statement and commit immediately (every state change is durable)."""
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        """Run a SELECT and return all rows."""
        return list(self.conn.execute(sql, params).fetchall())

    def close(self) -> None:
        self.conn.close()
