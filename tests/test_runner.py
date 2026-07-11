"""
test_runner.py -- Unit tests for runner.py's A2 OHLCV basis-drift detection
and repair path.

Uses a temporary local SQLite file (not Turso) and mocks src.fetcher.fetch_daily_ohlcv
so tests run without network access.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db():
    """Mirrors tests/test_signals.py's fixture."""
    db_file = os.path.join(tempfile.gettempdir(), f"ma_test_runner_{uuid.uuid4().hex}.db")
    db_url = f"file:{db_file}"

    import src.db as db
    db.close_connection()

    original_make = db._make_backend

    def _override_make(url=None, auth_token=None):
        return original_make(db_url, "")

    db._make_backend = _override_make
    db.close_connection()
    db.init_schema()
    db.add_watchlist_ticker("TEST", "US")

    yield

    db.close_connection()
    db._make_backend = original_make
    try:
        os.remove(db_file)
    except OSError:
        pass


def _candle(d: str, close: float) -> dict:
    return {"date": d, "open": close - 1, "high": close + 2, "low": close - 2, "close": close, "volume": 1_000_000}


# ---------------------------------------------------------------------------
# _check_and_repair_ohlcv_drift
# ---------------------------------------------------------------------------

class TestCheckAndRepairOhlcvDrift:

    def test_no_drift_when_closes_match_within_tolerance(self):
        import src.db as db
        from src.runner import _check_and_repair_ohlcv_drift

        db.upsert_ohlcv("TEST", [_candle("2026-01-01", 100.0), _candle("2026-01-02", 101.0)])

        # Fetched candles: same closes for overlapping dates (within tolerance),
        # plus one new bar.
        fetched = [_candle("2026-01-01", 100.0), _candle("2026-01-02", 101.0005), _candle("2026-01-03", 102.0)]

        with mock.patch("src.fetcher.fetch_daily_ohlcv") as mock_fetch:
            result = _check_and_repair_ohlcv_drift("TEST", "US", fetched)

        assert result is False
        mock_fetch.assert_not_called()

    def test_newest_bar_excluded_from_comparison(self):
        """A big diff on the newest (last) fetched bar alone must NOT trigger drift."""
        import src.db as db
        from src.runner import _check_and_repair_ohlcv_drift

        db.upsert_ohlcv("TEST", [_candle("2026-01-01", 100.0)])
        # Only one historical date overlaps; the newest bar has a huge diff
        # but there's no stored row for it to compare against anyway (it's new).
        fetched = [_candle("2026-01-01", 100.0), _candle("2026-01-02", 500.0)]

        with mock.patch("src.fetcher.fetch_daily_ohlcv") as mock_fetch:
            result = _check_and_repair_ohlcv_drift("TEST", "US", fetched)

        assert result is False
        mock_fetch.assert_not_called()

    def test_drift_beyond_tolerance_triggers_rebaseline(self):
        import src.db as db
        from src.runner import _check_and_repair_ohlcv_drift

        db.upsert_ohlcv("TEST", [_candle("2026-01-01", 100.0), _candle("2026-01-02", 101.0)])

        # Fetched close for 2026-01-01 is wildly different from stored (100.0 -> 150.0),
        # simulating a retroactive split/dividend basis change upstream.
        fetched = [_candle("2026-01-01", 150.0), _candle("2026-01-02", 101.0), _candle("2026-01-03", 102.0)]

        full_history = [
            _candle("2025-12-01", 145.0),
            _candle("2026-01-01", 150.0),
            _candle("2026-01-02", 151.5),
            _candle("2026-01-03", 153.0),
        ]

        with mock.patch("src.fetcher.fetch_daily_ohlcv", return_value=full_history) as mock_fetch, \
             mock.patch("src.alerter.send_ops_message") as mock_ops, \
             mock.patch("src.bootstrap.bootstrap_ticker", return_value=True) as mock_bootstrap:
            result = _check_and_repair_ohlcv_drift("TEST", "US", fetched)

        assert result is True
        mock_fetch.assert_called_once()
        mock_ops.assert_called_once()
        assert "OHLCV basis drift detected for TEST" in mock_ops.call_args[0][0]
        mock_bootstrap.assert_called_once_with("TEST", "US")

        # DB should now hold the fully-replaced history, not the original.
        df = db.get_all_ohlcv("TEST")
        assert len(df) == 4
        stored_dates = [d.isoformat() for d in df.index.date]
        assert "2025-12-01" in stored_dates

    def test_drift_repair_resets_reclaim_tracker(self):
        import src.db as db
        from datetime import date
        from src.runner import _check_and_repair_ohlcv_drift

        db.upsert_ohlcv("TEST", [_candle("2026-01-01", 100.0)])
        db.set_reclaim_streak("TEST", "D", 50, 5, date.today(), was_broken=1)

        fetched = [_candle("2026-01-01", 150.0), _candle("2026-01-02", 151.0)]
        full_history = [_candle("2026-01-01", 150.0), _candle("2026-01-02", 151.0)]

        with mock.patch("src.fetcher.fetch_daily_ohlcv", return_value=full_history), \
             mock.patch("src.alerter.send_ops_message"), \
             mock.patch("src.bootstrap.bootstrap_ticker", return_value=True):
            _check_and_repair_ohlcv_drift("TEST", "US", fetched)

        row = db.get_reclaim_streak_full("TEST", "D", 50)
        assert row["consecutive_closes"] == 0
        assert row["streak_start"] is None
        assert row["was_broken"] == 0

    def test_no_candles_returns_false(self):
        from src.runner import _check_and_repair_ohlcv_drift
        with mock.patch("src.fetcher.fetch_daily_ohlcv") as mock_fetch:
            result = _check_and_repair_ohlcv_drift("TEST", "US", [])
        assert result is False
        mock_fetch.assert_not_called()

    def test_single_candle_no_historical_overlap_returns_false(self):
        """With only one fetched candle, it's excluded as "the newest bar" —
        there's nothing left to compare, so no drift check is possible."""
        from src.runner import _check_and_repair_ohlcv_drift
        with mock.patch("src.fetcher.fetch_daily_ohlcv") as mock_fetch:
            result = _check_and_repair_ohlcv_drift("TEST", "US", [_candle("2026-01-01", 100.0)])
        assert result is False
        mock_fetch.assert_not_called()

    def test_full_refetch_failure_does_not_crash(self):
        import src.db as db
        from src.runner import _check_and_repair_ohlcv_drift

        db.upsert_ohlcv("TEST", [_candle("2026-01-01", 100.0)])
        fetched = [_candle("2026-01-01", 150.0), _candle("2026-01-02", 151.0)]

        with mock.patch("src.fetcher.fetch_daily_ohlcv", return_value=None) as mock_fetch, \
             mock.patch("src.alerter.send_ops_message"):
            result = _check_and_repair_ohlcv_drift("TEST", "US", fetched)

        assert result is True  # still signals "handled" so caller doesn't double-upsert
        mock_fetch.assert_called_once()
