"""
test_db.py -- Unit tests for db.py migrations and A2 data-integrity helpers.

Uses a temporary local SQLite file (not Turso) so tests run without network access.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db():
    """
    Override the module-level DB backend to use an isolated temp SQLite file in /tmp.
    Applied to every test automatically. Mirrors tests/test_signals.py's fixture.
    """
    db_file = os.path.join(tempfile.gettempdir(), f"ma_test_db_{uuid.uuid4().hex}.db")
    db_url = f"file:{db_file}"

    import src.db as db
    db.close_connection()

    original_make = db._make_backend

    def _override_make(url=None, auth_token=None):
        return original_make(db_url, "")

    db._make_backend = _override_make
    db.close_connection()

    yield

    db.close_connection()
    db._make_backend = original_make
    try:
        os.remove(db_file)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Migration v4 idempotency
# ---------------------------------------------------------------------------

class TestReclaimTrackerV4Migration:

    def test_migration_v4_adds_last_break_date_column(self):
        import src.db as db
        db.init_schema()
        rows = db._db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reclaim_tracker'"
        )
        assert rows
        assert "last_break_date" in (rows[0].get("sql") or "")

    def test_migration_v4_is_idempotent_running_twice(self):
        """
        init_schema() (which runs _migrate_reclaim_tracker_v4) must be safe to
        call twice in a row without error -- e.g. a redeploy that re-runs
        migrations against an already-migrated DB.
        """
        import src.db as db
        db.init_schema()
        db.init_schema()  # must not raise, must not duplicate the column

        rows = db._db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reclaim_tracker'"
        )
        assert rows
        table_sql = rows[0].get("sql") or ""
        # Column name should appear exactly once in the DDL, not duplicated.
        assert table_sql.count("last_break_date") == 1

    def test_migration_v4_preserves_existing_reclaim_rows(self):
        """Running the v4 migration must not wipe out existing tracker state."""
        import src.db as db
        from datetime import date

        db.init_schema()
        db.add_watchlist_ticker("TEST", "US")
        db.set_reclaim_streak("TEST", "D", 50, 3, date.today())

        db.init_schema()  # re-run migrations again

        row = db.get_reclaim_streak_full("TEST", "D", 50)
        assert row["consecutive_closes"] == 3


# ---------------------------------------------------------------------------
# get_closes_for_dates
# ---------------------------------------------------------------------------

class TestGetClosesForDates:

    def test_returns_closes_for_stored_dates_only(self):
        import src.db as db
        db.init_schema()
        db.add_watchlist_ticker("TEST", "US")
        db.upsert_ohlcv("TEST", [
            {"date": "2026-01-01", "open": 1, "high": 2, "low": 0.5, "close": 100.0, "volume": 1000},
            {"date": "2026-01-02", "open": 1, "high": 2, "low": 0.5, "close": 101.0, "volume": 1000},
        ])
        result = db.get_closes_for_dates("TEST", ["2026-01-01", "2026-01-02", "2026-01-03"])
        assert result == {"2026-01-01": 100.0, "2026-01-02": 101.0}

    def test_empty_dates_list_returns_empty_dict(self):
        import src.db as db
        db.init_schema()
        assert db.get_closes_for_dates("TEST", []) == {}


# ---------------------------------------------------------------------------
# replace_ohlcv_atomic
# ---------------------------------------------------------------------------

class TestReplaceOhlcvAtomic:

    def test_replaces_all_rows_for_ticker(self):
        import src.db as db
        db.init_schema()
        db.add_watchlist_ticker("TEST", "US")
        db.upsert_ohlcv("TEST", [
            {"date": "2026-01-01", "open": 1, "high": 2, "low": 0.5, "close": 100.0, "volume": 1000},
        ])
        db.replace_ohlcv_atomic("TEST", [
            {"date": "2026-02-01", "open": 5, "high": 6, "low": 4, "close": 200.0, "volume": 2000},
            {"date": "2026-02-02", "open": 5, "high": 6, "low": 4, "close": 201.0, "volume": 2000},
        ])
        df = db.get_all_ohlcv("TEST")
        assert len(df) == 2
        assert "2026-01-01" not in [d.isoformat() for d in df.index.date]
        assert float(df.iloc[-1]["close"]) == 201.0

    def test_does_not_affect_other_tickers(self):
        import src.db as db
        db.init_schema()
        db.add_watchlist_ticker("TEST", "US")
        db.add_watchlist_ticker("OTHER", "US")
        db.upsert_ohlcv("OTHER", [
            {"date": "2026-01-01", "open": 1, "high": 2, "low": 0.5, "close": 50.0, "volume": 1000},
        ])
        db.replace_ohlcv_atomic("TEST", [
            {"date": "2026-02-01", "open": 5, "high": 6, "low": 4, "close": 200.0, "volume": 2000},
        ])
        other_df = db.get_all_ohlcv("OTHER")
        assert len(other_df) == 1
        assert float(other_df.iloc[-1]["close"]) == 50.0


# ---------------------------------------------------------------------------
# reset_reclaim_tracker_for_ticker
# ---------------------------------------------------------------------------

class TestResetReclaimTrackerForTicker:

    def test_resets_streak_start_and_was_broken(self):
        import src.db as db
        from datetime import date
        db.init_schema()
        db.add_watchlist_ticker("TEST", "US")
        db.set_reclaim_streak(
            "TEST", "D", 50, 5, date.today(), was_broken=1, last_break_date=date.today(),
        )
        db.reset_reclaim_tracker_for_ticker("TEST")
        row = db.get_reclaim_streak_full("TEST", "D", 50)
        assert row["consecutive_closes"] == 0
        assert row["streak_start"] is None
        assert row["was_broken"] == 0

    def test_does_not_affect_other_tickers(self):
        import src.db as db
        from datetime import date
        db.init_schema()
        db.add_watchlist_ticker("TEST", "US")
        db.add_watchlist_ticker("OTHER", "US")
        db.set_reclaim_streak("OTHER", "D", 50, 5, date.today())
        db.reset_reclaim_tracker_for_ticker("TEST")
        row = db.get_reclaim_streak_full("OTHER", "D", 50)
        assert row["consecutive_closes"] == 5


# ---------------------------------------------------------------------------
# Fix 2b -- data_health.last_deep_audit migration + rotation selection
# ---------------------------------------------------------------------------

class TestDataHealthV2Migration:

    def test_migration_adds_last_deep_audit_column(self):
        import src.db as db
        db.init_schema()
        rows = db._db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='data_health'"
        )
        assert rows
        assert "last_deep_audit" in (rows[0].get("sql") or "")

    def test_migration_is_idempotent_running_twice(self):
        import src.db as db
        db.init_schema()
        db.init_schema()  # must not raise on the already-migrated column
        rows = db._db().execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='data_health'"
        )
        assert "last_deep_audit" in (rows[0].get("sql") or "")


class TestDeepAuditRotation:

    def test_last_deep_audit_survives_a_fetch_success_update(self):
        """
        update_data_health() uses INSERT OR REPLACE, which overwrites the
        WHOLE row -- last_deep_audit must be explicitly carried forward or a
        plain daily fetch-success update would silently wipe out the Fix 2b
        rotation state.
        """
        import src.db as db
        from datetime import date
        db.init_schema()
        db.add_watchlist_ticker("AAA", "US")

        db.set_deep_audit_date("AAA", date(2026, 6, 1))
        db.update_data_health("AAA", success=True)

        health = db.get_data_health("AAA")
        assert health["last_deep_audit"] == "2026-06-01"
        assert health["last_success"] == date.today().isoformat()

    def test_last_deep_audit_survives_a_fetch_failure_update(self):
        import src.db as db
        from datetime import date
        db.init_schema()
        db.add_watchlist_ticker("AAA", "US")

        db.set_deep_audit_date("AAA", date(2026, 6, 1))
        db.update_data_health("AAA", success=False)

        health = db.get_data_health("AAA")
        assert health["last_deep_audit"] == "2026-06-01"
        assert health["consecutive_failures"] == 1

    def test_get_tickers_for_deep_audit_prioritises_never_audited(self):
        import src.db as db
        from datetime import date
        db.init_schema()
        db.add_watchlist_ticker("OLD_AUDIT", "US")
        db.add_watchlist_ticker("NEVER_AUDITED", "US")
        db.add_watchlist_ticker("RECENT_AUDIT", "US")

        db.set_deep_audit_date("OLD_AUDIT", date(2026, 1, 1))
        db.set_deep_audit_date("RECENT_AUDIT", date(2026, 7, 1))
        # NEVER_AUDITED has no data_health row touched -- last_deep_audit is NULL.

        due = db.get_tickers_for_deep_audit(2)
        assert due == ["NEVER_AUDITED", "OLD_AUDIT"], (
            "NULLs (never audited) must sort before any real date, then "
            "oldest date first"
        )

    def test_get_tickers_for_deep_audit_respects_k_limit(self):
        import src.db as db
        db.init_schema()
        for t in ("A", "B", "C", "D", "E"):
            db.add_watchlist_ticker(t, "US")
        due = db.get_tickers_for_deep_audit(3)
        assert len(due) == 3


# ---------------------------------------------------------------------------
# Fix 2 -- _LibsqlBackend reuses one requests.Session across pipeline calls
# ---------------------------------------------------------------------------

class TestLibsqlBackendSessionReuse:

    def test_pipeline_calls_reuse_same_session_and_close_clears_it(self, monkeypatch):
        """
        Two successive _pipeline() calls must reuse the same requests.Session
        instance (transport-only change -- removes the fresh TCP+TLS handshake
        per Turso round trip), and close() must clear it so a later
        get_connection() can still create a fresh one.
        """
        import src.db as db

        created_sessions = []

        class _FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"results": []}

        class _FakeSession:
            def __init__(self):
                self.headers = {}
                self.closed = False
                created_sessions.append(self)

            def post(self, *args, **kwargs):
                return _FakeResponse()

            def close(self):
                self.closed = True

        monkeypatch.setattr("requests.Session", _FakeSession)

        backend = db._LibsqlBackend("libsql://example.turso.io", "test-token")

        backend._pipeline([{"type": "execute", "stmt": {"sql": "SELECT 1"}}])
        backend._pipeline([{"type": "execute", "stmt": {"sql": "SELECT 2"}}])

        assert len(created_sessions) == 1, "second _pipeline call must reuse the existing session"
        assert backend._session is created_sessions[0]

        backend.close()
        assert backend._session is None
        assert created_sessions[0].closed is True
