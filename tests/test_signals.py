"""
test_signals.py -- Unit tests for signal detectors 3A, 3B, and 3C.

Uses hand-crafted OHLCV fixtures. The DB is backed by a temporary local SQLite file
(not Turso) so tests run without network access.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import date, timedelta

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db():
    """
    Override the module-level DB backend to use an isolated temp SQLite file in /tmp.
    Applied to every test automatically.
    """
    db_file = os.path.join(tempfile.gettempdir(), f"ma_test_{uuid.uuid4().hex}.db")
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


# ---------------------------------------------------------------------------
# DataFrame builders
# ---------------------------------------------------------------------------

def _make_daily_df(n=60, base_close=200.0, trend=0.0, volume=1_000_000):
    dates = pd.bdate_range(end="2026-05-22", periods=n)
    closes = [base_close + i * trend for i in range(n)]
    return pd.DataFrame(
        {
            "open":   [c - 1.0 for c in closes],
            "high":   [c + 2.0 for c in closes],
            "low":    [c - 2.0 for c in closes],
            "close":  closes,
            "volume": [volume] * n,
        },
        index=dates,
    )


def _add_mas(df, periods=(50, 100, 150, 200)):
    from src.ma import compute_ma
    return compute_ma(df, list(periods))


def _empty_df():
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _cascade_state(step=2, broken_ma="D50"):
    return {"current_step": step, "broken_ma": broken_ma}


# ---------------------------------------------------------------------------
# Signal 3A -- MA Support
# ---------------------------------------------------------------------------

class TestDetect3AMaSupport:

    def _run(self, daily_df, cascade_step=1, weekly_df=None, monthly_df=None):
        from src.signals import detect_3a_ma_support
        weekly  = weekly_df  if weekly_df  is not None else _empty_df()
        monthly = monthly_df if monthly_df is not None else _empty_df()
        return detect_3a_ma_support("TEST", daily_df, weekly, monthly, cascade_step)

    def test_fires_when_all_conditions_met(self):
        df = _make_daily_df(n=60, base_close=202.0, volume=2_000_000)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")]    = ma_val - 0.50
        df.iloc[-1, df.columns.get_loc("close")]  = ma_val + 1.00
        df.iloc[-1, df.columns.get_loc("volume")] = 4_000_000
        alerts = self._run(df, cascade_step=1)
        assert len(alerts) == 1
        assert alerts[0]["signal_type"] == "MA_SUPPORT"
        assert alerts[0]["timeframe"] == "D"
        assert alerts[0]["ma_period"] == 50

    def test_does_not_fire_without_wick(self):
        df = _make_daily_df(n=60, base_close=202.0, volume=3_000_000)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")]   = ma_val + 1.0
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 3.0
        assert len(self._run(df, cascade_step=1)) == 0

    def test_does_not_fire_when_close_below_ma(self):
        df = _make_daily_df(n=60, base_close=202.0, volume=3_000_000)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")]   = ma_val - 1.0
        df.iloc[-1, df.columns.get_loc("close")] = ma_val - 0.5
        assert len(self._run(df, cascade_step=1)) == 0

    def test_does_not_fire_when_volume_below_threshold(self):
        # avg vol = 1M; last bar = 1M -> ratio 1.0x < 1.5x
        df = _make_daily_df(n=60, base_close=202.0, volume=1_000_000)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")]    = ma_val - 0.5
        df.iloc[-1, df.columns.get_loc("close")]  = ma_val + 1.0
        df.iloc[-1, df.columns.get_loc("volume")] = 1_000_000
        assert len(self._run(df, cascade_step=1)) == 0

    def test_proximity_rule_only_highest_ma_fires(self):
        from src.resample import resample_to_weekly
        df = _make_daily_df(n=300, base_close=205.0, volume=3_000_000)
        _add_mas(df, (50, 100, 150, 200))
        weekly_df = resample_to_weekly(df)
        _add_mas(weekly_df, (50, 100, 150, 200))
        ma_w100 = float(weekly_df["ma_100"].iloc[-1])
        ma_w50  = float(weekly_df["ma_50"].iloc[-1])
        close = max(ma_w100, ma_w50) + 5.0
        low   = min(ma_w100, ma_w50) - 1.0
        df.iloc[-1, df.columns.get_loc("close")]  = close
        df.iloc[-1, df.columns.get_loc("low")]    = low
        df.iloc[-1, df.columns.get_loc("volume")] = 6_000_000
        from src.signals import detect_3a_ma_support
        alerts = detect_3a_ma_support("TEST", df, weekly_df, _empty_df(), cascade_step=2)
        weekly_alerts = [a for a in alerts if a["timeframe"] == "W"]
        assert len(weekly_alerts) <= 1

    def test_insufficient_history_no_crash(self):
        df = _make_daily_df(n=10, base_close=200.0, volume=3_000_000)
        _add_mas(df, (50,))
        alerts = self._run(df, cascade_step=1)
        assert isinstance(alerts, list)


# ---------------------------------------------------------------------------
# Signal 3B -- MA Reclaim
# ---------------------------------------------------------------------------

class TestDetect3BReclaim:

    def _run(self, daily_df, cascade_st):
        from src.signals import detect_3b_reclaim
        return detect_3b_reclaim("TEST", daily_df, cascade_st)

    def test_no_signal_when_broken_ma_is_none(self):
        df = _make_daily_df(n=60, base_close=210.0)
        _add_mas(df, (50,))
        result = self._run(df, _cascade_state(step=1, broken_ma="NONE"))
        assert result is None

    def test_streak_increments_on_close_above(self):
        import src.db as db
        df = _make_daily_df(n=60, base_close=210.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 5.0
        state = _cascade_state(step=2, broken_ma="D50")
        before = db.get_reclaim_streak("TEST", 50)
        assert before == 0
        self._run(df, state)
        after = db.get_reclaim_streak("TEST", 50)
        assert after == 1

    def test_streak_resets_on_close_below(self):
        import src.db as db
        df = _make_daily_df(n=60, base_close=210.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        db.set_reclaim_streak("TEST", 50, 3, date.today() - timedelta(days=3))
        df.iloc[-1, df.columns.get_loc("close")] = ma_val - 5.0
        self._run(df, _cascade_state())
        assert db.get_reclaim_streak("TEST", 50) == 0

    def test_fires_on_day_7(self):
        import src.db as db
        df = _make_daily_df(n=60, base_close=210.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        db.set_reclaim_streak("TEST", 50, 6, date.today() - timedelta(days=6))
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        result = self._run(df, _cascade_state())
        assert result is not None
        assert result["signal_type"] == "RECLAIM"
        assert result["extra"]["streak"] == 7
        assert result["extra"]["previous_step"] == 2

    def test_does_not_fire_at_streak_6(self):
        import src.db as db
        df = _make_daily_df(n=60, base_close=210.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        db.set_reclaim_streak("TEST", 50, 5, date.today() - timedelta(days=5))
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        result = self._run(df, _cascade_state())
        assert result is None

    def test_new_3b_after_break_and_rereclaim(self):
        import src.db as db
        df = _make_daily_df(n=60, base_close=210.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        state = _cascade_state()
        db.set_reclaim_streak("TEST", 50, 6, date.today() - timedelta(days=6))
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        r1 = self._run(df, state)
        assert r1 is not None
        df.iloc[-1, df.columns.get_loc("close")] = ma_val - 5.0
        self._run(df, state)
        assert db.get_reclaim_streak("TEST", 50) == 0
        db.set_reclaim_streak("TEST", 50, 6, date.today() - timedelta(days=6))
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        r2 = self._run(df, state)
        assert r2 is not None


# ---------------------------------------------------------------------------
# Signal 3C -- Touch Accumulation
# ---------------------------------------------------------------------------

class TestDetect3CTouchAccumulation:

    def _run(self, daily_df, cascade_step=1):
        from src.signals import detect_3c_touch_accumulation
        return detect_3c_touch_accumulation(
            "TEST", daily_df, _empty_df(), _empty_df(), cascade_step
        )

    def test_fires_at_3_touches(self):
        import src.db as db
        df = _make_daily_df(n=60, base_close=202.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        today = date.today()
        db.record_touch("TEST", "D", 50, today - timedelta(days=10))
        db.record_touch("TEST", "D", 50, today - timedelta(days=5))
        db.record_touch("TEST", "D", 50, today)
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        alerts = self._run(df, cascade_step=1)
        assert any(a["signal_type"] == "TOUCH_ACCUMULATION" for a in alerts)

    def test_does_not_fire_at_2_touches(self):
        import src.db as db
        df = _make_daily_df(n=60, base_close=202.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        today = date.today()
        db.record_touch("TEST", "D", 50, today - timedelta(days=5))
        db.record_touch("TEST", "D", 50, today)
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        alerts = self._run(df, cascade_step=1)
        assert not any(a["signal_type"] == "TOUCH_ACCUMULATION" for a in alerts)

    def test_old_touches_outside_window_not_counted(self):
        import src.db as db
        from src.config import TOUCH_WINDOW_DAYS
        df = _make_daily_df(n=60, base_close=202.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        today = date.today()
        db.record_touch("TEST", "D", 50, today - timedelta(days=TOUCH_WINDOW_DAYS + 5))
        db.record_touch("TEST", "D", 50, today - timedelta(days=TOUCH_WINDOW_DAYS + 3))
        db.record_touch("TEST", "D", 50, today)
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        alerts = self._run(df, cascade_step=1)
        assert not any(a["signal_type"] == "TOUCH_ACCUMULATION" for a in alerts)

    def test_touch_log_not_cleared_after_firing(self):
        import src.db as db
        from src.config import TOUCH_WINDOW_DAYS
        df = _make_daily_df(n=60, base_close=202.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        today = date.today()
        db.record_touch("TEST", "D", 50, today - timedelta(days=10))
        db.record_touch("TEST", "D", 50, today - timedelta(days=5))
        db.record_touch("TEST", "D", 50, today)
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        alerts1 = self._run(df, cascade_step=1)
        assert any(a["signal_type"] == "TOUCH_ACCUMULATION" for a in alerts1)
        touches = db.get_recent_touches("TEST", "D", 50, TOUCH_WINDOW_DAYS)
        assert len(touches) >= 3

    def test_3c_fires_independently_of_3a_volume(self):
        import src.db as db
        df = _make_daily_df(n=60, base_close=202.0, volume=100_000)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        today = date.today()
        db.record_touch("TEST", "D", 50, today - timedelta(days=10))
        db.record_touch("TEST", "D", 50, today - timedelta(days=5))
        db.record_touch("TEST", "D", 50, today)
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        alerts = self._run(df, cascade_step=1)
        assert any(a["signal_type"] == "TOUCH_ACCUMULATION" for a in alerts)


# ---------------------------------------------------------------------------
# Interaction: 3A writes touch that 3C reads
# ---------------------------------------------------------------------------

class TestSignalOrdering:

    def test_3a_touch_feeds_3c(self):
        import src.db as db
        from src.signals import detect_3a_ma_support, detect_3c_touch_accumulation

        df = _make_daily_df(n=60, base_close=202.0, volume=2_000_000)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")]    = ma_val - 0.5
        df.iloc[-1, df.columns.get_loc("close")]  = ma_val + 1.0
        df.iloc[-1, df.columns.get_loc("volume")] = 4_000_000

        today = date.today()
        db.record_touch("TEST", "D", 50, today - timedelta(days=10))
        db.record_touch("TEST", "D", 50, today - timedelta(days=5))

        alerts_3a = detect_3a_ma_support("TEST", df, _empty_df(), _empty_df(), cascade_step=1)
        assert len(alerts_3a) == 1

        alerts_3c = detect_3c_touch_accumulation("TEST", df, _empty_df(), _empty_df(), cascade_step=1)
        assert any(a["signal_type"] == "TOUCH_ACCUMULATION" for a in alerts_3c)
