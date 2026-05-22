"""
db.py — All Turso/SQLite interactions.

Backend selection:
  - TURSO_DATABASE_URL starts with 'libsql://' → uses libsql_client (remote Turso)
  - TURSO_DATABASE_URL starts with 'file:' or is unset → uses Python sqlite3 (local file / test)

This dual-backend design lets smoke tests and unit tests run against a local SQLite file
without needing live Turso credentials, while production always uses Turso.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Generator

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL (mirrors 03_SCHEMA.sql — kept in sync manually)
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watchlist (
    ticker          TEXT NOT NULL PRIMARY KEY,
    market          TEXT NOT NULL CHECK (market IN ('US', 'SG')),
    added_at        DATE NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ohlcv (
    ticker          TEXT NOT NULL,
    date            DATE NOT NULL,
    open            REAL NOT NULL,
    high            REAL NOT NULL,
    low             REAL NOT NULL,
    close           REAL NOT NULL,
    volume          INTEGER NOT NULL,
    PRIMARY KEY (ticker, date),
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date ON ohlcv(ticker, date DESC);

CREATE TABLE IF NOT EXISTS cascade_state (
    ticker          TEXT NOT NULL PRIMARY KEY,
    current_step    INTEGER NOT NULL CHECK (current_step BETWEEN 1 AND 5),
    broken_ma       TEXT NOT NULL CHECK (broken_ma IN ('NONE', 'D50', 'D100', 'D150', 'D200')),
    last_updated    DATE NOT NULL,
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE TABLE IF NOT EXISTS reclaim_tracker (
    ticker              TEXT NOT NULL,
    ma_period           INTEGER NOT NULL,
    consecutive_closes  INTEGER NOT NULL DEFAULT 0,
    streak_start        DATE,
    last_updated        DATE NOT NULL,
    PRIMARY KEY (ticker, ma_period),
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE TABLE IF NOT EXISTS touch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    timeframe       TEXT NOT NULL CHECK (timeframe IN ('D', 'W', 'M')),
    ma_period       INTEGER NOT NULL,
    touch_date      DATE NOT NULL,
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker),
    UNIQUE (ticker, timeframe, ma_period, touch_date)
);

CREATE INDEX IF NOT EXISTS idx_touch_lookup
  ON touch_log(ticker, timeframe, ma_period, touch_date DESC);

CREATE TABLE IF NOT EXISTS alert_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    signal_type     TEXT NOT NULL CHECK (signal_type IN ('MA_SUPPORT', 'RECLAIM', 'TOUCH_ACCUMULATION', 'HALT_WARNING')),
    timeframe       TEXT,
    ma_period       INTEGER,
    price_at_fire   REAL,
    ma_value        REAL,
    volume_ratio    REAL,
    extra_json      TEXT,
    fired_at        TEXT NOT NULL,
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE INDEX IF NOT EXISTS idx_alert_log_ticker_time
  ON alert_log(ticker, fired_at DESC);

CREATE TABLE IF NOT EXISTS data_health (
    ticker                  TEXT NOT NULL PRIMARY KEY,
    last_success            DATE,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    last_warning_sent       DATE,
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);
"""


# ---------------------------------------------------------------------------
# Connection abstraction
# ---------------------------------------------------------------------------

class _Sqlite3Backend:
    """Thin wrapper around sqlite3 that matches the interface used by _execute()."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        assert self._conn is not None, "Not connected"
        cur = self._conn.execute(sql, params or [])
        self._conn.commit()
        rows = cur.fetchall()
        return [dict(row) for row in rows]

    def executemany(self, sql: str, param_list: list[list[Any]]) -> None:
        assert self._conn is not None
        self._conn.executemany(sql, param_list)
        self._conn.commit()

    def execute_script(self, script: str) -> None:
        assert self._conn is not None
        self._conn.executescript(script)
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


class _LibsqlBackend:
    """Wrapper around libsql_client.ClientSync."""

    def __init__(self, url: str, auth_token: str) -> None:
        self._url = url
        self._auth_token = auth_token
        self._client: Any = None

    def connect(self) -> None:
        import libsql_client
        self._client = libsql_client.create_client_sync(
            url=self._url,
            auth_token=self._auth_token,
        )

    def execute(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        assert self._client is not None, "Not connected"
        if params:
            rs = self._client.execute(sql, params)
        else:
            rs = self._client.execute(sql)
        cols = rs.columns
        return [dict(zip(cols, row)) for row in rs.rows]

    def executemany(self, sql: str, param_list: list[list[Any]]) -> None:
        assert self._client is not None
        for params in param_list:
            self._client.execute(sql, params)

    def execute_script(self, script: str) -> None:
        assert self._client is not None
        # Split on semicolons, execute each statement individually
        stmts = [s.strip() for s in script.split(";") if s.strip()]
        for stmt in stmts:
            self._client.execute(stmt)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None


# ---------------------------------------------------------------------------
# Module-level backend singleton (one connection per process)
# ---------------------------------------------------------------------------
_backend: _Sqlite3Backend | _LibsqlBackend | None = None


def _make_backend(
    url: str | None = None,
    auth_token: str | None = None,
) -> _Sqlite3Backend | _LibsqlBackend:
    resolved_url = url or os.getenv("TURSO_DATABASE_URL", "")
    resolved_token = auth_token or os.getenv("TURSO_AUTH_TOKEN", "")

    if resolved_url.startswith("libsql://"):
        if not resolved_token:
            raise RuntimeError(
                "TURSO_AUTH_TOKEN is required when TURSO_DATABASE_URL is a libsql:// URL"
            )
        return _LibsqlBackend(resolved_url, resolved_token)

    # Local file path or empty → use sqlite3
    if resolved_url.startswith("file:"):
        file_path = resolved_url[5:]  # strip 'file:'
    elif resolved_url:
        file_path = resolved_url
    else:
        file_path = "ma_alerts_local.db"

    return _Sqlite3Backend(file_path)


def get_connection(
    url: str | None = None,
    auth_token: str | None = None,
) -> _Sqlite3Backend | _LibsqlBackend:
    """
    Returns a connected backend instance.

    Uses TURSO_DATABASE_URL + TURSO_AUTH_TOKEN from env unless overridden.
    Caches the backend globally within a process; call close_connection() to reset.
    """
    global _backend
    if _backend is None:
        _backend = _make_backend(url, auth_token)
        _backend.connect()
        logger.debug("DB connection established")
    return _backend


def close_connection() -> None:
    """Close and reset the module-level backend. Used by tests and smoke_test."""
    global _backend
    if _backend is not None:
        _backend.close()
        _backend = None


@contextmanager
def temporary_connection(
    url: str,
    auth_token: str = "",
) -> Generator[_Sqlite3Backend | _LibsqlBackend, None, None]:
    """
    Context manager that opens a fresh, isolated backend and closes it on exit.
    Used by smoke_test to avoid polluting the global singleton.
    """
    backend = _make_backend(url, auth_token)
    backend.connect()
    try:
        yield backend
    finally:
        backend.close()


def _db() -> _Sqlite3Backend | _LibsqlBackend:
    """Internal helper — returns the active backend, initialising if needed."""
    return get_connection()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_schema() -> None:
    """
    Run DDL to create all tables and indexes. Idempotent — safe to call on every run.
    """
    _db().execute_script(_SCHEMA_SQL)
    logger.info("Schema initialised (or already up to date)")


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------

def upsert_ohlcv(ticker: str, candles: list[dict]) -> None:
    """Insert or replace Daily OHLCV rows for a ticker."""
    if not candles:
        return
    sql = (
        "INSERT OR REPLACE INTO ohlcv (ticker, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    params_list = [
        [ticker, c["date"], c["open"], c["high"], c["low"], c["close"], c["volume"]]
        for c in candles
    ]
    _db().executemany(sql, params_list)
    logger.debug("Upserted %d OHLCV rows for %s", len(candles), ticker)


def get_ohlcv(ticker: str, days: int) -> pd.DataFrame:
    """Return last N days of Daily OHLCV for ticker as a DataFrame indexed by date."""
    rows = _db().execute(
        "SELECT date, open, high, low, close, volume FROM ohlcv "
        "WHERE ticker = ? ORDER BY date ASC LIMIT ?",
        [ticker, days],
    )
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def get_all_ohlcv(ticker: str) -> pd.DataFrame:
    """Return all stored Daily OHLCV for ticker as a DataFrame indexed by date."""
    rows = _db().execute(
        "SELECT date, open, high, low, close, volume FROM ohlcv "
        "WHERE ticker = ? ORDER BY date ASC",
        [ticker],
    )
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def prune_old_ohlcv(retention_years: int) -> int:
    """Delete OHLCV rows older than retention_years. Returns count deleted."""
    cutoff = (date.today() - timedelta(days=retention_years * 365)).isoformat()
    rows_before = _db().execute("SELECT COUNT(*) as n FROM ohlcv")
    _db().execute("DELETE FROM ohlcv WHERE date < ?", [cutoff])
    rows_after = _db().execute("SELECT COUNT(*) as n FROM ohlcv")
    deleted = (rows_before[0]["n"] if rows_before else 0) - (rows_after[0]["n"] if rows_after else 0)
    logger.info("Pruned %d OHLCV rows older than %s", deleted, cutoff)
    return deleted


# ---------------------------------------------------------------------------
# Cascade state
# ---------------------------------------------------------------------------

def get_cascade_state(ticker: str) -> dict:
    """Returns {'current_step': int, 'broken_ma': str, 'last_updated': str}."""
    rows = _db().execute(
        "SELECT current_step, broken_ma, last_updated FROM cascade_state WHERE ticker = ?",
        [ticker],
    )
    if rows:
        return dict(rows[0])
    # Default: no state stored yet → step 1
    return {"current_step": 1, "broken_ma": "NONE", "last_updated": None}


def set_cascade_state(ticker: str, step: int, broken_ma: str) -> None:
    """Upsert cascade state for ticker."""
    today = date.today().isoformat()
    _db().execute(
        "INSERT OR REPLACE INTO cascade_state (ticker, current_step, broken_ma, last_updated) "
        "VALUES (?, ?, ?, ?)",
        [ticker, step, broken_ma, today],
    )
    logger.debug("Set cascade state for %s: step=%d broken_ma=%s", ticker, step, broken_ma)


# ---------------------------------------------------------------------------
# Reclaim tracker
# ---------------------------------------------------------------------------

def get_reclaim_streak(ticker: str, ma_period: int) -> int:
    """Returns current consecutive-closes-above count."""
    rows = _db().execute(
        "SELECT consecutive_closes FROM reclaim_tracker WHERE ticker = ? AND ma_period = ?",
        [ticker, ma_period],
    )
    return rows[0]["consecutive_closes"] if rows else 0


def get_reclaim_streak_full(ticker: str, ma_period: int) -> dict:
    """Returns full reclaim tracker row as dict, or defaults if not found."""
    rows = _db().execute(
        "SELECT consecutive_closes, streak_start, last_updated "
        "FROM reclaim_tracker WHERE ticker = ? AND ma_period = ?",
        [ticker, ma_period],
    )
    if rows:
        return dict(rows[0])
    return {"consecutive_closes": 0, "streak_start": None, "last_updated": None}


def set_reclaim_streak(
    ticker: str,
    ma_period: int,
    streak: int,
    streak_start: date | None = None,
) -> None:
    """
    Upsert reclaim streak for this ticker + MA period.
    streak_start: the date the streak began; pass None when resetting streak to 0.
    """
    today = date.today().isoformat()
    start_str = streak_start.isoformat() if streak_start else None
    _db().execute(
        "INSERT OR REPLACE INTO reclaim_tracker "
        "(ticker, ma_period, consecutive_closes, streak_start, last_updated) "
        "VALUES (?, ?, ?, ?, ?)",
        [ticker, ma_period, streak, start_str, today],
    )


# ---------------------------------------------------------------------------
# Touch log
# ---------------------------------------------------------------------------

def record_touch(ticker: str, timeframe: str, ma_period: int, touch_date: date) -> None:
    """Insert a touch event into touch_log. Silently ignores duplicates."""
    try:
        _db().execute(
            "INSERT OR IGNORE INTO touch_log (ticker, timeframe, ma_period, touch_date) "
            "VALUES (?, ?, ?, ?)",
            [ticker, timeframe, ma_period, touch_date.isoformat()],
        )
    except Exception as exc:
        logger.warning("record_touch failed for %s %s %d: %s", ticker, timeframe, ma_period, exc)


def get_recent_touches(
    ticker: str,
    timeframe: str,
    ma_period: int,
    window_days: int,
) -> list[date]:
    """Return touches within the last window_days calendar days for this MA."""
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    rows = _db().execute(
        "SELECT touch_date FROM touch_log "
        "WHERE ticker = ? AND timeframe = ? AND ma_period = ? AND touch_date >= ? "
        "ORDER BY touch_date ASC",
        [ticker, timeframe, ma_period, cutoff],
    )
    return [date.fromisoformat(r["touch_date"]) for r in rows]


def prune_old_touches(window_days: int) -> int:
    """Delete touch_log rows older than window_days (calendar days). Returns count deleted."""
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    before = _db().execute("SELECT COUNT(*) as n FROM touch_log")
    _db().execute("DELETE FROM touch_log WHERE touch_date < ?", [cutoff])
    after = _db().execute("SELECT COUNT(*) as n FROM touch_log")
    deleted = (before[0]["n"] if before else 0) - (after[0]["n"] if after else 0)
    logger.debug("Pruned %d old touch_log rows", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Data health
# ---------------------------------------------------------------------------

def get_data_health(ticker: str) -> dict:
    """Returns {'last_success': str|None, 'consecutive_failures': int, 'last_warning_sent': str|None}."""
    rows = _db().execute(
        "SELECT last_success, consecutive_failures, last_warning_sent "
        "FROM data_health WHERE ticker = ?",
        [ticker],
    )
    if rows:
        return dict(rows[0])
    return {"last_success": None, "consecutive_failures": 0, "last_warning_sent": None}


def update_data_health(ticker: str, success: bool) -> None:
    """On success: set last_success, reset failures to 0. On fail: increment failures."""
    today = date.today().isoformat()
    if success:
        _db().execute(
            "INSERT OR REPLACE INTO data_health (ticker, last_success, consecutive_failures, last_warning_sent) "
            "VALUES (?, ?, 0, "
            "  (SELECT last_warning_sent FROM data_health WHERE ticker = ?))",
            [ticker, today, ticker],
        )
    else:
        existing = get_data_health(ticker)
        new_count = existing["consecutive_failures"] + 1
        _db().execute(
            "INSERT OR REPLACE INTO data_health "
            "(ticker, last_success, consecutive_failures, last_warning_sent) "
            "VALUES (?, ?, ?, ?)",
            [ticker, existing["last_success"], new_count, existing["last_warning_sent"]],
        )


def mark_warning_sent(ticker: str) -> None:
    """Record that halt warning was sent today, to avoid spamming."""
    today = date.today().isoformat()
    existing = get_data_health(ticker)
    _db().execute(
        "INSERT OR REPLACE INTO data_health "
        "(ticker, last_success, consecutive_failures, last_warning_sent) "
        "VALUES (?, ?, ?, ?)",
        [ticker, existing["last_success"], existing["consecutive_failures"], today],
    )


# ---------------------------------------------------------------------------
# Alert log
# ---------------------------------------------------------------------------

def insert_alert(alert: dict) -> None:
    """Append-only insert into alert_log."""
    _db().execute(
        "INSERT INTO alert_log "
        "(ticker, signal_type, timeframe, ma_period, price_at_fire, ma_value, "
        " volume_ratio, extra_json, fired_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            alert.get("ticker"),
            alert.get("signal_type"),
            alert.get("timeframe"),
            alert.get("ma_period"),
            alert.get("price"),
            alert.get("ma_value"),
            alert.get("volume_ratio"),
            json.dumps(alert.get("extra", {})),
            alert.get("fired_at", datetime.utcnow().isoformat() + "Z"),
        ],
    )


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def add_watchlist_ticker(ticker: str, market: str) -> bool:
    """
    Returns True if newly added, False if already existed.
    Also ensures a data_health row exists for the ticker.
    """
    existing = _db().execute(
        "SELECT ticker FROM watchlist WHERE ticker = ?", [ticker]
    )
    if existing:
        return False
    today = date.today().isoformat()
    _db().execute(
        "INSERT INTO watchlist (ticker, market, added_at, active) VALUES (?, ?, ?, 1)",
        [ticker, market, today],
    )
    # Initialise data_health row
    _db().execute(
        "INSERT OR IGNORE INTO data_health (ticker, consecutive_failures) VALUES (?, 0)",
        [ticker],
    )
    logger.info("Added ticker %s (%s) to watchlist", ticker, market)
    return True


def get_watchlist(market: str | None = None) -> list[dict]:
    """Returns active watchlist, optionally filtered by market."""
    if market:
        rows = _db().execute(
            "SELECT ticker, market, added_at FROM watchlist WHERE active = 1 AND market = ?",
            [market],
        )
    else:
        rows = _db().execute(
            "SELECT ticker, market, added_at FROM watchlist WHERE active = 1"
        )
    return [dict(r) for r in rows]
