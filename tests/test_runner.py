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


# ---------------------------------------------------------------------------
# Fix 6 -- last_break_date recorded on forward transition
# ---------------------------------------------------------------------------

class TestFix6BreakDateOnForwardTransition:

    def test_forward_transition_records_last_break_date_same_day(self):
        """
        Fix 6 (Bug 6): on the day a Daily MA actually breaks, 3B is skipped
        (forward transition fires instead), so reclaim_tracker.last_break_date
        was previously never written for a break-and-bounce -- it stayed
        null forever. runner._process_ticker() must now record it directly
        inside the `if transition:` branch, using that day's bar date.
        """
        import pandas as pd
        import src.db as db
        from datetime import date, timedelta
        from src.runner import _process_ticker

        n_seed = 259
        end_seed = pd.Timestamp(date.today() - timedelta(days=1))
        seed_dates = pd.bdate_range(end=end_seed, periods=n_seed)
        # Mild uptrend so today's close currently sits above D50 (step 1).
        seed_closes = [200.0 + i * 0.05 for i in range(n_seed)]
        seed_candles = [
            _candle(d.date().isoformat(), c) for d, c in zip(seed_dates, seed_closes)
        ]
        db.upsert_ohlcv("TEST", seed_candles)

        # New day: close crashes well below D50 (and every other Daily MA) --
        # a clean forward transition from step 1 to some deeper step.
        new_date = pd.bdate_range(start=seed_dates[-1] + pd.Timedelta(days=1), periods=1)[0]
        # Fetch mock returns the tail of the seed (unchanged, so no drift is
        # detected) plus the new crashing bar.
        tail = seed_candles[-29:]
        new_candle = _candle(new_date.date().isoformat(), 100.0)
        fetched = tail + [new_candle]

        with mock.patch("src.fetcher.fetch_daily_ohlcv", return_value=fetched):
            alerts = _process_ticker("TEST", "US")

        assert isinstance(alerts, list)

        state = db.get_cascade_state("TEST")
        assert state["current_step"] > 1, "a forward transition must have fired"

        broken_period = int(state["broken_ma"].lstrip("D"))
        row = db.get_reclaim_streak_full("TEST", "D", broken_period)
        assert row["last_break_date"] == new_date.date().isoformat(), (
            "last_break_date must be recorded on the transition day itself, "
            "not left null for a later scan to fill in"
        )


# ---------------------------------------------------------------------------
# Fix 5 Option B -- reclaim de-escalation derives the new step live
# ---------------------------------------------------------------------------

class TestFix5OptionBReclaimDerivation:

    def test_reclaim_derives_step_from_live_close_not_fixed_n_minus_1(self):
        """
        Fix 5 Option B (locked 2026-07-19): after a Daily 3B confirmation,
        the new cascade step must come from determine_step_from_close() on
        that day's close/MAs -- not the old fixed N-1 rule. This test forces
        a reclaim of D200 (pre-reclaim step 5) on a bar whose close is so far
        above every Daily MA that the correct derived state is step 1, which
        the old fixed rule (N-1 = step 4) could never produce -- proving the
        runner actually re-derives rather than just decrementing by one.
        """
        import pandas as pd
        import src.db as db
        from datetime import date, timedelta
        from src.runner import _process_ticker

        n_seed = 259
        end_seed = pd.Timestamp(date.today() - timedelta(days=1))
        seed_dates = pd.bdate_range(end=end_seed, periods=n_seed)
        # Flat history -> all Daily MAs converge to ~100.
        seed_candles = [_candle(d.date().isoformat(), 100.0) for d in seed_dates]
        db.upsert_ohlcv("TEST", seed_candles)

        # Pre-existing state: step 5, broken D200, streak at 6/7 (one bar from firing).
        db.set_cascade_state("TEST", 5, "D200")
        db.set_reclaim_streak(
            "TEST", "D", 200, 6, seed_dates[-1].date() - timedelta(days=6),
            last_bar_date=seed_dates[-1].date() - timedelta(days=1),
            was_broken=0,
        )

        new_date = pd.bdate_range(start=seed_dates[-1] + pd.Timedelta(days=1), periods=1)[0]
        tail = seed_candles[-29:]
        # New close is far above every Daily MA (which are all still ~100) --
        # the 7th consecutive close above D200, AND high enough that the
        # live position is actually step 1, not just-reclaimed-step-4.
        new_candle = _candle(new_date.date().isoformat(), 150.0)
        fetched = tail + [new_candle]

        with mock.patch("src.fetcher.fetch_daily_ohlcv", return_value=fetched):
            alerts = _process_ticker("TEST", "US")

        reclaim_alerts = [a for a in alerts if a.get("signal_type") == "RECLAIM" and a.get("timeframe") == "D"]
        assert len(reclaim_alerts) == 1, "the 7th consecutive close above D200 must confirm the reclaim"

        state = db.get_cascade_state("TEST")
        assert state["current_step"] == 1, (
            "Option B must derive the live step from close vs today's MAs "
            "(step 1 here), not the old fixed N-1 rule (which would give step 4)"
        )
        assert state["broken_ma"] == "NONE"
        assert reclaim_alerts[0]["extra"]["new_step"] == 1, (
            "the alert's new_step must reflect the derived post-reclaim state, "
            "not the pre-derivation N-1 value computed inside _detect_3b_for_timeframe"
        )

    def test_anomaly_guard_keeps_state_when_derived_step_not_shallower(self):
        """
        Option B guard: if determine_step_from_close ever returns a step that
        is not strictly shallower than the pre-reclaim step, the runner must
        keep the current cascade state unchanged rather than apply a bogus
        de-escalation. This can't be reached through ordinary price action
        (the same bar-close/MA values that let 3B fire a RECLAIM in the first
        place also feed determine_step_from_close, and if that derivation
        came out >= the pre-reclaim step, runner's own forward-transition
        check earlier in the pipeline would already have fired instead of
        letting the reclaim path run at all). It's a defensive guard against
        a genuinely anomalous/inconsistent input -- tested here by forcing
        the derivation directly.
        """
        import pandas as pd
        import src.db as db
        from datetime import date, timedelta
        from src.runner import _process_ticker

        n_seed = 259
        end_seed = pd.Timestamp(date.today() - timedelta(days=1))
        seed_dates = pd.bdate_range(end=end_seed, periods=n_seed)
        seed_closes = [200.0 + i * 0.05 for i in range(n_seed)]
        seed_candles = [_candle(d.date().isoformat(), c) for d, c in zip(seed_dates, seed_closes)]
        db.upsert_ohlcv("TEST", seed_candles)

        db.set_cascade_state("TEST", 2, "D50")
        db.set_reclaim_streak(
            "TEST", "D", 50, 6, seed_dates[-1].date() - timedelta(days=6),
            last_bar_date=seed_dates[-1].date() - timedelta(days=1),
            was_broken=0,
        )

        new_date = pd.bdate_range(start=seed_dates[-1] + pd.Timedelta(days=1), periods=1)[0]
        tail = seed_candles[-29:]
        # A clean close above every Daily MA -- confirms the reclaim (7th
        # consecutive close above D50) and would ordinarily derive step 1.
        new_candle = _candle(new_date.date().isoformat(), 250.0)
        fetched = tail + [new_candle]

        # Force the derivation to claim "no improvement" (still step 2) so
        # the guard's condition (derived_step >= pre_reclaim_step) is hit
        # regardless of what the real MA math would have said.
        with mock.patch("src.fetcher.fetch_daily_ohlcv", return_value=fetched), \
             mock.patch("src.cascade.determine_step_from_close", return_value=(2, "D50")), \
             mock.patch("src.alerter.send_ops_message") as mock_ops:
            alerts = _process_ticker("TEST", "US")

        reclaim_alerts = [a for a in alerts if a.get("signal_type") == "RECLAIM" and a.get("timeframe") == "D"]
        assert len(reclaim_alerts) == 1, "the 7th consecutive close above D50 must still confirm the reclaim"

        state = db.get_cascade_state("TEST")
        assert state["current_step"] == 2, "anomaly guard must leave the pre-reclaim state unchanged"
        assert state["broken_ma"] == "D50"
        mock_ops.assert_called_once()
        assert "anomaly" in mock_ops.call_args[0][0].lower()
        assert reclaim_alerts[0]["extra"]["new_step"] == 2


# ---------------------------------------------------------------------------
# Fix 2a -- main_rebaseline_all
# ---------------------------------------------------------------------------

class TestMainRebaselineAll:

    def test_rebaselines_every_active_watchlist_ticker(self):
        import src.db as db
        from src.runner import main_rebaseline_all

        db.add_watchlist_ticker("AAA", "US")
        db.add_watchlist_ticker("BBB", "US")
        # TEST is already added by the isolated_db fixture.

        with mock.patch("src.runner.main_rebaseline", return_value=0) as mock_rebaseline, \
             mock.patch.dict(os.environ, {
                 "TWELVE_DATA_API_KEY": "x", "TURSO_DATABASE_URL": "file:x",
                 "TURSO_AUTH_TOKEN": "x", "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "x",
             }):
            result = main_rebaseline_all()

        assert result == 0
        mock_rebaseline.assert_called_once()
        called_tickers = set(mock_rebaseline.call_args[0][0])
        assert called_tickers == {"AAA", "BBB", "TEST"}

    def test_empty_watchlist_is_a_no_op(self):
        import src.db as db
        from src.runner import main_rebaseline_all

        # Deactivate the fixture-created TEST ticker so the watchlist is empty.
        db._db().execute("UPDATE watchlist SET active = 0 WHERE ticker = 'TEST'")

        with mock.patch("src.runner.main_rebaseline") as mock_rebaseline, \
             mock.patch.dict(os.environ, {
                 "TWELVE_DATA_API_KEY": "x", "TURSO_DATABASE_URL": "file:x",
                 "TURSO_AUTH_TOKEN": "x", "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "x",
             }):
            result = main_rebaseline_all()

        assert result == 0
        mock_rebaseline.assert_not_called()
