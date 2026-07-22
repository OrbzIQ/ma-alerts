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
    timeframe           TEXT NOT NULL DEFAULT 'D' CHECK (timeframe IN ('D', 'W', 'M')),
    ma_period           INTEGER NOT NULL,
    consecutive_closes  INTEGER NOT NULL DEFAULT 0,
    streak_start        DATE,
    last_bar_date       TEXT,                        -- most recently counted bar date (idempotency)
    was_broken          INTEGER NOT NULL DEFAULT 0,   -- 1 = price has closed <= MA since last reset
    last_break_date     TEXT,                        -- bar date of most recent close <= MA (display-only, never cleared by post-fire reset)
    last_updated        DATE NOT NULL,
    PRIMARY KEY (ticker, timeframe, ma_period),
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
    signal_type     TEXT NOT NULL,
    timeframe       TEXT,
    ma_period       INTEGER,
    price_at_fire   REAL,
    ma_value        REAL,
    volume_ratio    REAL,
    extra_json      TEXT,
    bar_date        TEXT,                         -- ISO date of the bar that triggered the signal
    fired_at        TEXT NOT NULL,
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE INDEX IF NOT EXISTS idx_alert_log_ticker_time
  ON alert_log(ticker, fired_at DESC);

CREATE INDEX IF NOT EXISTS idx_alert_log_dedup
  ON alert_log(ticker, ma_period, timeframe, signal_type);

CREATE TABLE IF NOT EXISTS data_health (
    ticker                  TEXT NOT NULL PRIMARY KEY,
    last_success            DATE,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    last_warning_sent       DATE,
    last_deep_audit         TEXT,                        -- Fix 2b: date of the last rotating deep drift audit (outputsize=250)
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE TABLE IF NOT EXISTS api_usage (
    date            TEXT NOT NULL PRIMARY KEY,     -- 'YYYY-MM-DD' UTC
    credits_used    INTEGER NOT NULL DEFAULT 0
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

    def execute_batch_atomic(self, statements: list[tuple[str, list[Any] | None]]) -> None:
        """
        Execute a list of (sql, params) statements as a single atomic transaction.
        All statements commit together, or none do (rollback on any exception).
        """
        assert self._conn is not None
        try:
            for sql, params in statements:
                self._conn.execute(sql, params or [])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


class _LibsqlBackend:
    """
    Turso backend using the HTTP pipeline API.

    Replaces the libsql_client WebSocket backend which has compatibility
    issues with Python 3.14+. Uses plain HTTPS requests instead — no
    aiohttp, no WebSockets, no async runtime required.

    API reference: https://docs.turso.tech/sdk/http/reference
    """

    def __init__(self, url: str, auth_token: str) -> None:
        # Convert libsql:// → https://
        self._base_url = url.replace("libsql://", "https://").rstrip("/")
        self._auth_token = auth_token
        self._connected = False
        self._session = None

    def _get_session(self):
        """Lazily create and return the shared requests.Session for this backend.

        Reuses one HTTPS connection across all pipeline calls instead of
        paying a fresh TCP+TLS handshake per request. Auth/content-type
        headers are set once here rather than passed per-call.
        """
        if self._session is None:
            import requests as _req
            self._session = _req.Session()
            self._session.headers.update({
                "Authorization": f"Bearer {self._auth_token}",
                "Content-Type": "application/json",
            })
        return self._session

    def connect(self) -> None:
        """Verify connectivity by running a no-op pipeline request."""
        session = self._get_session()
        resp = session.post(
            f"{self._base_url}/v2/pipeline",
            json={"requests": [{"type": "close"}]},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Turso connection failed: HTTP {resp.status_code} — {resp.text[:200]}"
            )
        self._connected = True

    def _pipeline(self, requests_payload: list[dict]) -> list[dict]:
        """Execute a pipeline of statements and return result sets."""
        session = self._get_session()
        resp = session.post(
            f"{self._base_url}/v2/pipeline",
            json={"requests": requests_payload},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Turso pipeline error: HTTP {resp.status_code} — {resp.text[:300]}"
            )
        return resp.json().get("results", [])

    def _to_stmt(self, sql: str, params: list | None) -> dict:
        """Convert SQL + params into a Turso pipeline execute request."""
        stmt: dict = {"sql": sql}
        if params:
            args = []
            for p in params:
                if p is None:
                    args.append({"type": "null", "value": None})
                elif isinstance(p, bool):
                    args.append({"type": "integer", "value": str(int(p))})
                elif isinstance(p, int):
                    args.append({"type": "integer", "value": str(p)})
                elif isinstance(p, float):
                    args.append({"type": "float", "value": p})
                else:
                    args.append({"type": "text", "value": str(p)})
            stmt["args"] = args
        return {"type": "execute", "stmt": stmt}

    def _parse_cell(self, cell: dict):
        """Convert a Turso cell dict to the appropriate Python type."""
        t = cell.get("type", "text")
        v = cell.get("value")
        if t == "null" or v is None:
            return None
        if t == "integer":
            return int(v)
        if t == "float":
            return float(v)
        return v  # text, blob

    def _rows_from_result(self, result: dict) -> list[dict]:
        """Parse a pipeline result into a list of row dicts."""
        if result.get("type") == "error":
            raise RuntimeError(f"Turso SQL error: {result.get('error', result)}")
        rs = result.get("response", {}).get("result", {})
        cols = [c["name"] for c in rs.get("cols", [])]
        rows = []
        for row in rs.get("rows", []):
            rows.append(dict(zip(cols, (self._parse_cell(c) for c in row))))
        return rows

    def execute(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        stmt = self._to_stmt(sql, params)
        results = self._pipeline([stmt, {"type": "close"}])
        return self._rows_from_result(results[0]) if results else []

    def executemany(self, sql: str, param_list: list[list[Any]]) -> None:
        if not param_list:
            return
        _CHUNK = 250
        for i in range(0, len(param_list), _CHUNK):
            chunk = param_list[i : i + _CHUNK]
            requests_payload = [self._to_stmt(sql, p) for p in chunk]
            requests_payload.append({"type": "close"})
            self._pipeline(requests_payload)

    def execute_script(self, script: str) -> None:
        stmts = [s.strip() for s in script.split(";") if s.strip()]
        if not stmts:
            return
        requests_payload = [self._to_stmt(s, None) for s in stmts]
        requests_payload.append({"type": "close"})
        self._pipeline(requests_payload)

    def execute_batch_atomic(self, statements: list[tuple[str, list[Any] | None]]) -> None:
        """
        Execute a list of (sql, params) statements as a single atomic transaction.

        Wraps the statements in explicit BEGIN/COMMIT so the whole batch is one
        transaction even though it's sent as a single pipeline request (Turso's
        HTTP pipeline API does not itself guarantee statement-list atomicity —
        it just executes each statement in the connection context in order).
        """
        if not statements:
            return
        requests_payload = [self._to_stmt("BEGIN", None)]
        requests_payload.extend(self._to_stmt(sql, params) for sql, params in statements)
        requests_payload.append(self._to_stmt("COMMIT", None))
        requests_payload.append({"type": "close"})
        try:
            self._pipeline(requests_payload)
        except Exception:
            # Best-effort rollback — if BEGIN succeeded but a later statement
            # failed, the connection this pipeline opened is already closing
            # (pipeline calls are per-request), so an explicit ROLLBACK in a
            # fresh pipeline is required to undo a partially-applied BEGIN.
            try:
                self._pipeline([self._to_stmt("ROLLBACK", None), {"type": "close"}])
            except Exception:
                pass
            raise

    def close(self) -> None:
        self._connected = False
        if self._session is not None:
            self._session.close()
            self._session = None


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

def _migrate_alert_log_v2() -> None:
    """
    V2 migration: remove the restrictive signal_type CHECK constraint from alert_log
    so that new signal types (e.g. '3D') can be inserted.

    SQLite/libsql does not support DROP CONSTRAINT, so we recreate the table.
    The migration is idempotent: it reads the table DDL first and skips if the
    constraint is already gone. Safe to run on every startup.
    """
    try:
        rows = _db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_log'"
        )
    except Exception as exc:
        logger.warning("V2 migration: could not read sqlite_master (%s) — skipping", exc)
        return

    if not rows:
        return  # table doesn't exist yet; fresh _SCHEMA_SQL DDL will create it correctly

    table_sql: str = (rows[0].get("sql") or "")
    if "CHECK (signal_type IN" not in table_sql:
        return  # already migrated or constraint was never present

    logger.info("V2 migration: recreating alert_log to relax signal_type CHECK constraint")
    try:
        _db().execute_script("""
            CREATE TABLE alert_log_v2 (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL,
                signal_type     TEXT NOT NULL,
                timeframe       TEXT,
                ma_period       INTEGER,
                price_at_fire   REAL,
                ma_value        REAL,
                volume_ratio    REAL,
                extra_json      TEXT,
                fired_at        TEXT NOT NULL
            );
            INSERT INTO alert_log_v2
                (id, ticker, signal_type, timeframe, ma_period,
                 price_at_fire, ma_value, volume_ratio, extra_json, fired_at)
            SELECT id, ticker, signal_type, timeframe, ma_period,
                   price_at_fire, ma_value, volume_ratio, extra_json, fired_at
            FROM alert_log;
            DROP TABLE alert_log;
            ALTER TABLE alert_log_v2 RENAME TO alert_log;
            CREATE INDEX IF NOT EXISTS idx_alert_log_ticker_time
              ON alert_log(ticker, fired_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alert_log_dedup
              ON alert_log(ticker, ma_period, timeframe, signal_type)
        """)
        logger.info("V2 migration complete — alert_log signal_type CHECK constraint removed")
    except Exception as exc:
        logger.error("V2 migration failed: %s", exc)


def _migrate_alert_log_v3() -> None:
    """
    V3 migration: add bar_date TEXT column to alert_log.

    SQLite supports ALTER TABLE ADD COLUMN for nullable columns with no default —
    this is safe, non-destructive, and does not rewrite the table.
    Idempotent: reads sqlite_master DDL first and skips if bar_date already present.
    """
    try:
        rows = _db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_log'"
        )
    except Exception as exc:
        logger.warning("V3 migration: could not read sqlite_master (%s) — skipping", exc)
        return

    if not rows:
        return  # table doesn't exist yet; fresh _SCHEMA_SQL DDL creates it with bar_date

    table_sql: str = (rows[0].get("sql") or "")
    if "bar_date" in table_sql:
        return  # already migrated

    try:
        _db().execute("ALTER TABLE alert_log ADD COLUMN bar_date TEXT")
        logger.info("V3 migration: added bar_date column to alert_log")
    except Exception as exc:
        logger.error("V3 migration failed: %s", exc)


def _migrate_reclaim_tracker_v2() -> None:
    """
    V2 migration: add timeframe + last_bar_date columns to reclaim_tracker,
    and extend the primary key to (ticker, timeframe, ma_period).

    SQLite cannot ALTER TABLE to change a PK, so we recreate the table.
    Existing Daily streaks are preserved with timeframe = 'D'.
    Idempotent: checks sqlite_master for the timeframe column first.
    """
    try:
        rows = _db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reclaim_tracker'"
        )
    except Exception as exc:
        logger.warning("reclaim_tracker V2 migration: sqlite_master read failed (%s) — skipping", exc)
        return

    if not rows:
        return  # table doesn't exist yet; fresh _SCHEMA_SQL DDL will create it correctly

    table_sql: str = (rows[0].get("sql") or "")
    if "timeframe" in table_sql:
        return  # already migrated

    try:
        _db().execute_script("""
            CREATE TABLE reclaim_tracker_v2 (
                ticker              TEXT NOT NULL,
                timeframe           TEXT NOT NULL DEFAULT 'D',
                ma_period           INTEGER NOT NULL,
                consecutive_closes  INTEGER NOT NULL DEFAULT 0,
                streak_start        DATE,
                last_bar_date       TEXT,
                last_updated        DATE NOT NULL,
                PRIMARY KEY (ticker, timeframe, ma_period)
            );
            INSERT INTO reclaim_tracker_v2
                (ticker, timeframe, ma_period, consecutive_closes,
                 streak_start, last_bar_date, last_updated)
            SELECT ticker, 'D', ma_period, consecutive_closes,
                   streak_start, NULL, last_updated
            FROM reclaim_tracker;
            DROP TABLE reclaim_tracker;
            ALTER TABLE reclaim_tracker_v2 RENAME TO reclaim_tracker
        """)
        logger.info("reclaim_tracker V2 migration complete — timeframe + last_bar_date added")
    except Exception as exc:
        logger.error("reclaim_tracker V2 migration failed: %s", exc)


def _migrate_reclaim_tracker_v3() -> None:
    """
    V3 migration: add was_broken INTEGER column to reclaim_tracker, and zero out
    streak state that predates the break-gated single-fire fix.

    was_broken tracks whether price has closed AT OR BELOW the MA since the
    streak was last reset — W/M reclaim increments are only counted when
    was_broken == 1, so a reclaim cannot fire without a prior confirmed break.

    On rollout, existing streak state was accumulated under the old (ungated,
    >=-threshold, no-post-fire-reset) logic and cannot be trusted:
      - All W/M streaks are zeroed (they may have counted un-broken closes).
      - Daily streaks >= 7 are zeroed (rows left in a fired-but-not-reset state
        by the old logic, which would otherwise instant-refire on the next
        qualifying close).

    Idempotent: checks sqlite_master for the was_broken column first.
    """
    try:
        rows = _db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reclaim_tracker'"
        )
    except Exception as exc:
        logger.warning("reclaim_tracker V3 migration: sqlite_master read failed (%s) — skipping", exc)
        return

    if not rows:
        return  # table doesn't exist yet; fresh _SCHEMA_SQL DDL creates it with was_broken

    table_sql: str = (rows[0].get("sql") or "")
    if "was_broken" in table_sql:
        return  # already migrated

    try:
        _db().execute(
            "ALTER TABLE reclaim_tracker ADD COLUMN was_broken INTEGER NOT NULL DEFAULT 0"
        )
        _db().execute(
            "UPDATE reclaim_tracker SET consecutive_closes=0, streak_start=NULL "
            "WHERE timeframe IN ('W','M')"
        )
        _db().execute(
            "UPDATE reclaim_tracker SET consecutive_closes=0, streak_start=NULL "
            "WHERE timeframe='D' AND consecutive_closes >= 7"
        )
        logger.info(
            "reclaim_tracker V3 migration complete — was_broken added, "
            "W/M streaks zeroed, fired D streaks (>=7) zeroed"
        )
    except Exception as exc:
        logger.error("reclaim_tracker V3 migration failed: %s", exc)


def _migrate_reclaim_tracker_v4() -> None:
    """
    V4 migration: add last_break_date TEXT column to reclaim_tracker.

    last_break_date records the bar date on which price last closed AT OR
    BELOW this MA (the most recent "break"). Unlike was_broken (a boolean
    gate that gets cleared on post-fire reset), last_break_date is display-only
    transparency data and is NEVER cleared by a post-fire reset — it always
    reflects the most recent break, even after a reclaim has fired and the
    streak/was_broken fields have been zeroed for the next cycle.

    Idempotent: checks sqlite_master for the last_break_date column first.
    """
    try:
        rows = _db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reclaim_tracker'"
        )
    except Exception as exc:
        logger.warning("reclaim_tracker V4 migration: sqlite_master read failed (%s) — skipping", exc)
        return

    if not rows:
        return  # table doesn't exist yet; fresh _SCHEMA_SQL DDL creates it with last_break_date

    table_sql: str = (rows[0].get("sql") or "")
    if "last_break_date" in table_sql:
        return  # already migrated

    try:
        _db().execute(
            "ALTER TABLE reclaim_tracker ADD COLUMN last_break_date TEXT"
        )
        logger.info("reclaim_tracker V4 migration complete — last_break_date added")
    except Exception as exc:
        logger.error("reclaim_tracker V4 migration failed: %s", exc)


def _migrate_api_usage_v1() -> None:
    """
    B3 migration: ensure api_usage table exists on DBs created before B3.

    Fresh installs already have the table via _SCHEMA_SQL DDL.
    For existing DBs, CREATE TABLE IF NOT EXISTS in _SCHEMA_SQL handles it too,
    but this function makes the migration explicit and logged — matching the
    convention used by prior migrations.
    Idempotent — safe to run on every startup.
    """
    try:
        rows = _db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_usage'"
        )
    except Exception as exc:
        logger.warning("api_usage migration: sqlite_master read failed (%s) — skipping", exc)
        return

    if rows:
        return  # table already present

    try:
        _db().execute(
            "CREATE TABLE IF NOT EXISTS api_usage ("
            "    date TEXT NOT NULL PRIMARY KEY,"
            "    credits_used INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        logger.info("api_usage migration: table created")
    except Exception as exc:
        logger.error("api_usage migration failed: %s", exc)


def _migrate_data_health_v2() -> None:
    """
    Fix 2b migration: add last_deep_audit TEXT column to data_health.

    last_deep_audit records the date of the ticker's most recent rotating
    deep drift audit (outputsize=250 comparison — see
    runner._run_deep_drift_audit). Distinct from the A2 incremental drift
    check (which only ever sees 30 bars), this closes that check's 30-bar
    blind spot by rotating a widened comparison across the whole watchlist
    over time (K=3 tickers/day; every ticker audited roughly every 3 weeks).

    Idempotent: checks sqlite_master for the last_deep_audit column first.
    """
    try:
        rows = _db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='data_health'"
        )
    except Exception as exc:
        logger.warning("data_health V2 migration: sqlite_master read failed (%s) — skipping", exc)
        return

    if not rows:
        return  # table doesn't exist yet; fresh _SCHEMA_SQL DDL creates it with last_deep_audit

    table_sql: str = (rows[0].get("sql") or "")
    if "last_deep_audit" in table_sql:
        return  # already migrated

    try:
        _db().execute(
            "ALTER TABLE data_health ADD COLUMN last_deep_audit TEXT"
        )
        logger.info("data_health V2 migration complete — last_deep_audit added")
    except Exception as exc:
        logger.error("data_health V2 migration failed: %s", exc)


def init_schema() -> None:
    """
    Run DDL to create all tables and indexes. Idempotent — safe to call on every run.
    """
    _db().execute_script(_SCHEMA_SQL)
    _migrate_alert_log_v2()
    _migrate_alert_log_v3()
    _migrate_reclaim_tracker_v2()
    _migrate_reclaim_tracker_v3()
    _migrate_reclaim_tracker_v4()
    _migrate_api_usage_v1()
    _migrate_data_health_v2()
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


def get_closes_for_dates(ticker: str, dates: list[str]) -> dict[str, float]:
    """
    Return stored close prices for a ticker on the given dates (ISO strings).

    Used by the A2 basis-drift check to compare freshly-fetched closes
    against what's already stored, for dates where both exist.

    Returns {date_iso: close}. Dates with no stored row are simply absent
    from the result (not an error).
    """
    if not dates:
        return {}
    placeholders = ",".join("?" for _ in dates)
    rows = _db().execute(
        f"SELECT date, close FROM ohlcv WHERE ticker = ? AND date IN ({placeholders})",
        [ticker, *dates],
    )
    return {r["date"]: float(r["close"]) for r in rows}


def replace_ohlcv_atomic(ticker: str, candles: list[dict]) -> None:
    """
    Atomically replace ALL stored OHLCV rows for a ticker with `candles`
    (delete existing rows, insert new ones, single transaction — all-or-nothing).

    Used by the A2 basis-drift repair path: once a data-basis revision is
    detected, the ticker's entire history is untrustworthy and must be
    replaced wholesale rather than patched incrementally.
    """
    statements: list[tuple[str, list[Any] | None]] = [
        ("DELETE FROM ohlcv WHERE ticker = ?", [ticker]),
    ]
    insert_sql = (
        "INSERT INTO ohlcv (ticker, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    for c in candles:
        statements.append(
            (insert_sql, [ticker, c["date"], c["open"], c["high"], c["low"], c["close"], c["volume"]])
        )
    _db().execute_batch_atomic(statements)
    logger.info("Atomically replaced %d OHLCV rows for %s", len(candles), ticker)


def reset_reclaim_tracker_for_ticker(ticker: str) -> None:
    """
    Reset all reclaim_tracker rows for a ticker to the post-rebaseline state
    prescribed by A2: streak 0, streak_start NULL, was_broken 0. last_break_date
    is left as-is (spec: "keep last_break_date NULL ok" — i.e. no requirement
    to force it, existing values are acceptable to leave since the OHLCV basis
    itself is being fully replaced, not the historical break record).
    """
    _db().execute(
        "UPDATE reclaim_tracker SET consecutive_closes = 0, streak_start = NULL, was_broken = 0 "
        "WHERE ticker = ?",
        [ticker],
    )
    logger.info("Reset reclaim_tracker rows for %s (post-rebaseline)", ticker)


def get_ohlcv(ticker: str, days: int) -> pd.DataFrame:
    """Return last N days of Daily OHLCV for ticker as a DataFrame indexed by date.

    The inner query orders DESC so LIMIT keeps the N MOST RECENT rows (ORDER BY
    date ASC LIMIT N would instead return the N OLDEST rows — the bug this fixes).
    The DataFrame is then re-sorted ascending via set_index().sort_index() below,
    so the returned shape/order is unchanged for callers.
    """
    rows = _db().execute(
        "SELECT date, open, high, low, close, volume FROM ohlcv "
        "WHERE ticker = ? ORDER BY date DESC LIMIT ?",
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

def get_reclaim_streak(ticker: str, timeframe_or_period, ma_period: int | None = None) -> int:
    """
    Returns current consecutive-closes-above count for (ticker, timeframe, ma_period).

    Backward-compatible: old 2-arg call (ticker, ma_period) still works — timeframe
    defaults to 'D'. New 3-arg call (ticker, timeframe, ma_period) is preferred.
    """
    if isinstance(timeframe_or_period, int):
        # Old call shape: get_reclaim_streak(ticker, ma_period)
        timeframe = "D"
        period = timeframe_or_period
    else:
        timeframe = timeframe_or_period
        period = ma_period
    rows = _db().execute(
        "SELECT consecutive_closes FROM reclaim_tracker "
        "WHERE ticker = ? AND timeframe = ? AND ma_period = ?",
        [ticker, timeframe, period],
    )
    return rows[0]["consecutive_closes"] if rows else 0


def get_reclaim_streak_full(ticker: str, timeframe: str, ma_period: int) -> dict:
    """Returns full reclaim tracker row as dict, or defaults if not found."""
    rows = _db().execute(
        "SELECT consecutive_closes, streak_start, last_bar_date, was_broken, last_break_date, last_updated "
        "FROM reclaim_tracker WHERE ticker = ? AND timeframe = ? AND ma_period = ?",
        [ticker, timeframe, ma_period],
    )
    if rows:
        return dict(rows[0])
    return {
        "consecutive_closes": 0,
        "streak_start": None,
        "last_bar_date": None,
        "was_broken": 0,
        "last_break_date": None,
        "last_updated": None,
    }


def set_reclaim_streak(
    ticker: str,
    timeframe_or_period,
    ma_period_or_streak,
    streak_or_start=None,
    streak_start_or_sentinel=None,
    last_bar_date: date | None = None,
    was_broken: int | None = None,
    last_break_date: date | None = None,
) -> None:
    """
    Upsert reclaim streak for this (ticker, timeframe, ma_period).

    Backward-compatible with old 4-arg call (ticker, ma_period, streak, streak_start).
    New 5-arg call (ticker, timeframe, ma_period, streak, streak_start) is preferred.
    last_bar_date, was_broken, and last_break_date are keyword-only and always optional.

    was_broken: None preserves the existing stored value (read-modify-write); pass
    an explicit 0 or 1 to set it.

    last_break_date: None preserves the existing stored value (read-modify-write) —
    this field is display-only transparency data and must NOT be cleared by the
    post-fire reset in _detect_3b_for_timeframe, so callers that aren't recording a
    fresh break should never pass this argument. Pass an explicit date to update it
    (only the break branch in _detect_3b_for_timeframe does this).
    """
    if isinstance(timeframe_or_period, int):
        # Old call shape: set_reclaim_streak(ticker, ma_period, streak, streak_start)
        timeframe = "D"
        ma_period = timeframe_or_period
        streak = ma_period_or_streak
        streak_start = streak_or_start
    else:
        # New call shape: set_reclaim_streak(ticker, timeframe, ma_period, streak, streak_start)
        timeframe = timeframe_or_period
        ma_period = ma_period_or_streak
        streak = streak_or_start
        streak_start = streak_start_or_sentinel

    existing = None
    if was_broken is None or last_break_date is None:
        existing = get_reclaim_streak_full(ticker, timeframe, ma_period)

    if was_broken is None:
        was_broken_val = (existing.get("was_broken") or 0) if existing else 0
    else:
        was_broken_val = was_broken

    if last_break_date is None:
        break_date_str = existing.get("last_break_date") if existing else None
    else:
        break_date_str = last_break_date.isoformat()

    today = date.today().isoformat()
    start_str = streak_start.isoformat() if streak_start else None
    bar_str = last_bar_date.isoformat() if last_bar_date else None
    _db().execute(
        "INSERT OR REPLACE INTO reclaim_tracker "
        "(ticker, timeframe, ma_period, consecutive_closes, streak_start, last_bar_date, was_broken, last_break_date, last_updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [ticker, timeframe, ma_period, streak, start_str, bar_str, was_broken_val, break_date_str, today],
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


def get_touches_since(
    ticker: str,
    timeframe: str,
    ma_period: int,
    since_date: date,
) -> list[date]:
    """Return touches on or after since_date for this MA.

    Used by detect_3c_touch_accumulation to apply a trading-day window:
    the caller derives since_date from the daily_df DatetimeIndex so that
    the window tracks trading days, not calendar days.
    """
    rows = _db().execute(
        "SELECT touch_date FROM touch_log "
        "WHERE ticker = ? AND timeframe = ? AND ma_period = ? AND touch_date >= ? "
        "ORDER BY touch_date ASC",
        [ticker, timeframe, ma_period, since_date.isoformat()],
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
    """Returns {'last_success', 'consecutive_failures', 'last_warning_sent', 'last_deep_audit'}."""
    rows = _db().execute(
        "SELECT last_success, consecutive_failures, last_warning_sent, last_deep_audit "
        "FROM data_health WHERE ticker = ?",
        [ticker],
    )
    if rows:
        return dict(rows[0])
    return {
        "last_success": None,
        "consecutive_failures": 0,
        "last_warning_sent": None,
        "last_deep_audit": None,
    }


def update_data_health(ticker: str, success: bool) -> None:
    """On success: set last_success, reset failures to 0. On fail: increment failures.

    Every branch here uses INSERT OR REPLACE, which overwrites the ENTIRE row —
    so last_warning_sent and last_deep_audit must always be explicitly carried
    forward from the existing row, or a plain fetch/failure update would
    silently wipe out the halt-warning throttle and the Fix 2b deep-audit
    rotation state.
    """
    today = date.today().isoformat()
    existing = get_data_health(ticker)
    if success:
        _db().execute(
            "INSERT OR REPLACE INTO data_health "
            "(ticker, last_success, consecutive_failures, last_warning_sent, last_deep_audit) "
            "VALUES (?, ?, 0, ?, ?)",
            [ticker, today, existing["last_warning_sent"], existing["last_deep_audit"]],
        )
    else:
        new_count = existing["consecutive_failures"] + 1
        _db().execute(
            "INSERT OR REPLACE INTO data_health "
            "(ticker, last_success, consecutive_failures, last_warning_sent, last_deep_audit) "
            "VALUES (?, ?, ?, ?, ?)",
            [ticker, existing["last_success"], new_count, existing["last_warning_sent"], existing["last_deep_audit"]],
        )


def mark_warning_sent(ticker: str) -> None:
    """Record that halt warning was sent today, to avoid spamming."""
    today = date.today().isoformat()
    existing = get_data_health(ticker)
    _db().execute(
        "INSERT OR REPLACE INTO data_health "
        "(ticker, last_success, consecutive_failures, last_warning_sent, last_deep_audit) "
        "VALUES (?, ?, ?, ?, ?)",
        [ticker, existing["last_success"], existing["consecutive_failures"], today, existing["last_deep_audit"]],
    )


def set_deep_audit_date(ticker: str, audit_date: date) -> None:
    """Fix 2b: record that `ticker` had a rotating deep drift audit on audit_date.

    Preserves the rest of the data_health row (read-modify-write), same
    reasoning as update_data_health/mark_warning_sent above.
    """
    existing = get_data_health(ticker)
    _db().execute(
        "INSERT OR REPLACE INTO data_health "
        "(ticker, last_success, consecutive_failures, last_warning_sent, last_deep_audit) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ticker,
            existing["last_success"],
            existing["consecutive_failures"],
            existing["last_warning_sent"],
            audit_date.isoformat(),
        ],
    )


def get_tickers_for_deep_audit(k: int) -> list[str]:
    """Fix 2b: return up to k active watchlist tickers due for a rotating deep audit.

    Ordered by last_deep_audit ascending with NULLs first (a ticker that has
    never been deep-audited is always more "due" than one audited on any
    real date) — so every active ticker eventually rotates through, and new
    tickers are prioritised on their first few runs.
    """
    rows = _db().execute(
        "SELECT w.ticker AS ticker FROM watchlist w "
        "LEFT JOIN data_health h ON h.ticker = w.ticker "
        "WHERE w.active = 1 "
        "ORDER BY (h.last_deep_audit IS NOT NULL), h.last_deep_audit ASC "
        "LIMIT ?",
        [k],
    )
    return [r["ticker"] for r in rows]


# ---------------------------------------------------------------------------
# Alert log
# ---------------------------------------------------------------------------

def get_last_fired_bar_date(
    ticker: str,
    timeframe: str,
    ma_period: int,
    signal_type: str,
) -> date | None:
    """
    Return the bar_date of the most recently fired alert for this signal key, or None.

    Used by W/M detector gate: if the current completed bar's date is not newer
    than the last-fired bar_date, the signal has already been dispatched for that
    bar and must be suppressed.
    """
    rows = _db().execute(
        "SELECT bar_date FROM alert_log "
        "WHERE ticker = ? AND signal_type = ? AND timeframe = ? AND ma_period = ? "
        "AND bar_date IS NOT NULL "
        "ORDER BY bar_date DESC LIMIT 1",
        [ticker, signal_type, timeframe, ma_period],
    )
    if rows and rows[0].get("bar_date"):
        return date.fromisoformat(rows[0]["bar_date"])
    return None


def recent_alert_exists(
    ticker: str,
    ma_period: int | None,
    timeframe: str | None,
    signal_type: str,
    days: int = 5,
) -> bool:
    """
    Return True if a matching alert fired within the last `days` days.

    Used by the dispatch cooldown gate to suppress duplicate alerts.
    Matches on (ticker, signal_type, timeframe, ma_period) — all four columns.
    NULL-safe: timeframe and ma_period are compared with IS so NULL rows match
    correctly when the signal has no timeframe/period (e.g. HALT_WARNING).
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = _db().execute(
        "SELECT id FROM alert_log "
        "WHERE ticker = ? AND signal_type = ? "
        "AND timeframe IS ? AND ma_period IS ? "
        "AND fired_at >= ? LIMIT 1",
        [ticker, signal_type, timeframe, ma_period, cutoff],
    )
    return bool(rows)


def insert_alert(alert: dict) -> None:
    """Append-only insert into alert_log."""
    bar_date_raw = alert.get("bar_date")
    bar_date_str: str | None = (
        bar_date_raw.isoformat() if isinstance(bar_date_raw, date) else bar_date_raw
    )
    _db().execute(
        "INSERT INTO alert_log "
        "(ticker, signal_type, timeframe, ma_period, price_at_fire, ma_value, "
        " volume_ratio, extra_json, bar_date, fired_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            alert.get("ticker"),
            alert.get("signal_type"),
            alert.get("timeframe"),
            alert.get("ma_period"),
            alert.get("price"),
            alert.get("ma_value"),
            alert.get("volume_ratio"),
            json.dumps(alert.get("extra", {})),
            bar_date_str,
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


# ---------------------------------------------------------------------------
# API usage tracking (B3)
# ---------------------------------------------------------------------------

def get_credits_used(date_str: str) -> int:
    """
    Return credits_used for date_str ('YYYY-MM-DD' UTC).
    Returns 0 if no row exists yet for that date.
    """
    rows = _db().execute(
        "SELECT credits_used FROM api_usage WHERE date = ?",
        [date_str],
    )
    return rows[0]["credits_used"] if rows else 0


def increment_credits(date_str: str, n: int = 1) -> int:
    """
    Increment credits_used for date_str by n. Creates the row if it does not exist.
    Returns the new running total for that date.
    """
    _db().execute(
        "INSERT OR IGNORE INTO api_usage (date, credits_used) VALUES (?, 0)",
        [date_str],
    )
    _db().execute(
        "UPDATE api_usage SET credits_used = credits_used + ? WHERE date = ?",
        [n, date_str],
    )
    rows = _db().execute(
        "SELECT credits_used FROM api_usage WHERE date = ?",
        [date_str],
    )
    return rows[0]["credits_used"] if rows else n
