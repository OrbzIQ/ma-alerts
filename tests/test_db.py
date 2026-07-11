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
