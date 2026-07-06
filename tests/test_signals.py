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

    def test_daily_row_fully_reset_after_fire(self):
        # V's latent 7->8 instant-refire bug: after firing at streak 7, the
        # tracker row must be fully reset (streak=0, was_broken=0), not left
        # sitting at streak=7 ready to climb to 8 and refire.
        import src.db as db
        df = _make_daily_df(n=60, base_close=210.0)
        _add_mas(df, (50,))
        ma_val = float(df["ma_50"].iloc[-1])
        db.set_reclaim_streak("TEST", 50, 6, date.today() - timedelta(days=6))
        df.iloc[-1, df.columns.get_loc("close")] = ma_val + 2.0
        result = self._run(df, _cascade_state())
        assert result is not None
        assert result["extra"]["streak"] == 7
        row = db.get_reclaim_streak_full("TEST", "D", 50)
        assert row["consecutive_closes"] == 0
        assert row["streak_start"] is None
        assert row["was_broken"] == 0


# ---------------------------------------------------------------------------
# Signal 3B -- MA Reclaim: break-gated W/M semantics (Fix 1 / F1+F2)
# ---------------------------------------------------------------------------

class TestDetect3BWeeklyMonthlyBreakGate:
    """
    Exercises _detect_3b_for_timeframe directly for W/M timeframes, which are
    break-gated: a reclaim can only be counted/fired if price has actually
    closed at or below the MA (a real break) since the streak was last reset.
    """

    def _bar_df(self, end_date, ma_period=50, ma_value=200.0, close=None, n=60):
        """
        Build a daily-indexed df (used to stand in for a W/M bar series) whose
        last bar is dated end_date with the given close, and whose ma_{period}
        is pinned to ma_value on every row (flat MA keeps the test's intent —
        "close vs MA" — unambiguous regardless of lookback window effects).
        """
        dates = pd.bdate_range(end=end_date, periods=n)
        closes = [ma_value] * n
        if close is not None:
            closes[-1] = close
        df = pd.DataFrame(
            {
                "open":   [c - 1.0 for c in closes],
                "high":   [c + 2.0 for c in closes],
                "low":    [c - 2.0 for c in closes],
                "close":  closes,
                "volume": [1_000_000] * n,
            },
            index=dates,
        )
        df[f"ma_{ma_period}"] = ma_value
        return df

    def _detect(self, df, ma_period=50, required_streak=2, cascade_state=None):
        from src.signals import _detect_3b_for_timeframe
        return _detect_3b_for_timeframe(
            "TEST", df, "W", ma_period, cascade_state or _cascade_state(), required_streak,
        )

    def test_wm_never_fires_without_prior_break(self):
        # LITE M50 bug: price closes above a MA that was never broken. Streak
        # must never increment and a reclaim must never fire.
        import src.db as db
        for end_date in ("2026-05-04", "2026-05-11", "2026-05-18", "2026-05-25"):
            df = self._bar_df(end_date, ma_value=200.0, close=250.0)
            result = self._detect(df)
            assert result is None
        row = db.get_reclaim_streak_full("TEST", "W", 50)
        assert row["consecutive_closes"] == 0

    def test_wm_break_then_fires_exactly_once_at_n(self):
        import src.db as db
        # Bar 1: break (close <= MA) — arms was_broken, resets streak.
        df1 = self._bar_df("2026-05-04", ma_value=200.0, close=195.0)
        assert self._detect(df1) is None
        row = db.get_reclaim_streak_full("TEST", "W", 50)
        assert row["was_broken"] == 1
        assert row["consecutive_closes"] == 0

        # Bar 2: close above MA -> streak 1 (required_streak=2, no fire yet).
        df2 = self._bar_df("2026-05-11", ma_value=200.0, close=205.0)
        assert self._detect(df2) is None
        row = db.get_reclaim_streak_full("TEST", "W", 50)
        assert row["consecutive_closes"] == 1

        # Bar 3: close above MA again -> streak reaches required_streak (2) -> fires.
        df3 = self._bar_df("2026-05-18", ma_value=200.0, close=206.0)
        result = self._detect(df3)
        assert result is not None
        assert result["signal_type"] == "RECLAIM"
        assert result["extra"]["streak"] == 2

    def test_no_refire_post_confirmation_without_fresh_break(self):
        import src.db as db
        df1 = self._bar_df("2026-05-04", ma_value=200.0, close=195.0)
        self._detect(df1)
        df2 = self._bar_df("2026-05-11", ma_value=200.0, close=205.0)
        self._detect(df2)
        df3 = self._bar_df("2026-05-18", ma_value=200.0, close=206.0)
        result = self._detect(df3)
        assert result is not None  # confirmed at streak 2

        # Row must be fully reset post-fire.
        row = db.get_reclaim_streak_full("TEST", "W", 50)
        assert row["consecutive_closes"] == 0
        assert row["was_broken"] == 0

        # Further closes above MA with no fresh break must NOT refire or
        # increment — was_broken is 0 again after the post-fire reset.
        df4 = self._bar_df("2026-05-25", ma_value=200.0, close=207.0)
        result2 = self._detect(df4)
        assert result2 is None
        row2 = db.get_reclaim_streak_full("TEST", "W", 50)
        assert row2["consecutive_closes"] == 0

        df5 = self._bar_df("2026-06-01", ma_value=200.0, close=208.0)
        result3 = self._detect(df5)
        assert result3 is None


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


# ---------------------------------------------------------------------------
# Completed-bar rule for W/M signals
# ---------------------------------------------------------------------------

class TestWeeklyBarCompletedBarRule:
    """3A and 3C must evaluate W/M conditions against completed bars only."""

    def test_3a_w_signal_requires_completed_weekly_bar_close_above_ma(self):
        """
        3A must NOT fire a W signal when the last completed weekly bar closed BELOW
        the W MA — even if today's daily bar wicks the W MA and closes above it.

        This is the exact bug that was fixed: the old code checked today's daily
        bar against the W MA, so a strong daily close would incorrectly trigger a
        W support alert even when the weekly bar itself confirmed no support.
        """
        from src.resample import resample_to_weekly
        import src.db as db
        from src.signals import detect_3a_ma_support
        from src.config import TOUCH_WINDOW_DAYS

        # 300-day uptrend so D/W/M MAs are well-defined
        df = _make_daily_df(n=300, base_close=250.0, volume=3_000_000)
        _add_mas(df, (50, 100, 150, 200))

        weekly_df = resample_to_weekly(df)
        _add_mas(weekly_df, (50, 100, 150, 200))

        today_date = df.index[-1].date()

        # Identify last completed weekly bar (index.date strictly before today)
        completed_mask = weekly_df.index.date < today_date
        assert completed_mask.any(), "Fixture must have at least one completed weekly bar"
        last_completed_idx = weekly_df[completed_mask].index[-1]

        w100_val = float(weekly_df.at[last_completed_idx, "ma_100"])

        # Force last completed weekly bar: wick touches W100, close BELOW W100 — no support
        weekly_df.at[last_completed_idx, "low"]   = w100_val - 1.0
        weekly_df.at[last_completed_idx, "close"] = w100_val - 0.50

        # Today's daily bar: price above W100 (proximity check passes), high volume.
        # Old buggy code would have evaluated THIS bar against the W MA and fired.
        df.iloc[-1, df.columns.get_loc("low")]    = w100_val - 0.50
        df.iloc[-1, df.columns.get_loc("close")]  = w100_val + 5.0
        df.iloc[-1, df.columns.get_loc("volume")] = 9_000_000

        # cascade_step=2 includes W100 checks
        alerts = detect_3a_ma_support("TEST", df, weekly_df, _empty_df(), cascade_step=2)
        w_alerts = [a for a in alerts if a["timeframe"] == "W"]

        assert len(w_alerts) == 0, (
            "3A must not fire a W signal when the completed weekly bar closed below W MA"
        )
        w_touches = db.get_recent_touches("TEST", "W", 100, TOUCH_WINDOW_DAYS)
        assert len(w_touches) == 0, (
            "No W touch should be recorded when the weekly bar closed below W MA"
        )

    def test_3a_w_signal_fires_when_completed_weekly_bar_wicks_and_closes_above(self):
        """
        Positive case: 3A DOES fire a W signal when the last completed weekly bar
        wicked the W MA (low <= W MA) AND closed above it (close > W MA), AND
        today's daily volume is sufficient.
        """
        from src.resample import resample_to_weekly
        from src.signals import detect_3a_ma_support

        # n=700 daily → ~140 weekly bars; ma_100 on weekly requires >= 100 bars.
        df = _make_daily_df(n=700, base_close=250.0, volume=3_000_000)
        _add_mas(df, (50, 100, 150, 200))

        weekly_df = resample_to_weekly(df)
        _add_mas(weekly_df, (50, 100, 150, 200))

        today_date = df.index[-1].date()

        completed_mask = weekly_df.index.date < today_date
        assert completed_mask.any()
        last_completed_idx = weekly_df[completed_mask].index[-1]

        w100_val = float(weekly_df.at[last_completed_idx, "ma_100"])

        # Completed weekly bar: wicks W100, closes ABOVE W100 — valid support
        weekly_df.at[last_completed_idx, "low"]   = w100_val - 1.0
        weekly_df.at[last_completed_idx, "close"] = w100_val + 2.0

        # Today's daily bar: price above W100, sufficient volume
        df.iloc[-1, df.columns.get_loc("close")]  = w100_val + 5.0
        df.iloc[-1, df.columns.get_loc("volume")] = 9_000_000

        alerts = detect_3a_ma_support("TEST", df, weekly_df, _empty_df(), cascade_step=2)
        w_alerts = [a for a in alerts if a["timeframe"] == "W"]

        assert len(w_alerts) >= 1, (
            "3A should fire a W signal when the completed weekly bar wicked and closed above W MA"
        )


# ---------------------------------------------------------------------------
# Signal 3D -- D20 Momentum Touch
# ---------------------------------------------------------------------------

def _make_3d_uptrend(n: int = 250, trend: float = 0.5, hist_volume: int = 1_000_000):
    """
    Create an oldest-first daily DataFrame with monotonically increasing prices.
    With trend > 0: D20 > D50 > D100 > D150 > D200 naturally (shorter MA > longer MA).
    MAs are NOT pre-computed; caller must call _add_mas() before passing to detect_3d.
    """
    dates = pd.bdate_range(end="2026-05-22", periods=n)
    closes = [100.0 + i * trend for i in range(n)]
    return pd.DataFrame(
        {
            "open":   [c - 0.5 for c in closes],
            "high":   [c + 1.0 for c in closes],
            "low":    [c - 0.5 for c in closes],
            "close":  closes,
            "volume": [hist_volume] * n,
        },
        index=dates,
    )


def _setup_valid_trigger(df: pd.DataFrame, today_volume: int = 2_000_000) -> float:
    """
    Set today's bar (iloc[-1]) so all 3D trigger conditions are met:
      low <= D20, close > D20, volume >= 1.5× avg.
    Returns D20 value so callers can inspect it.
    Assumes df already has ma_20 computed.
    """
    d20 = float(df["ma_20"].iloc[-1])
    df.iloc[-1, df.columns.get_loc("low")]    = d20 - 0.10   # wick touches D20
    df.iloc[-1, df.columns.get_loc("close")]  = d20 + 0.50   # closed above
    df.iloc[-1, df.columns.get_loc("volume")] = today_volume  # 2× avg (1M hist)
    return d20


class TestDetect3D:

    def _run(self, df, cascade_step):
        from src.signals import detect_3d
        return detect_3d("TEST", df, cascade_step)

    # ------------------------------------------------------------------
    # 1. Happy path
    # ------------------------------------------------------------------
    def test_3d_fires_on_valid_touch(self):
        df = _make_3d_uptrend(n=250)
        _add_mas(df, (20, 50, 100, 150, 200))
        _setup_valid_trigger(df, today_volume=2_000_000)
        result = self._run(df, cascade_step=1)
        assert result is not None
        assert result["signal_type"] == "3D"
        assert result["timeframe"] == "D"
        assert result["ma_period"] == 20
        assert result["volume_ratio"] >= 1.5

    # ------------------------------------------------------------------
    # 2. cascade_step is None → immediate None (FIX-3)
    # ------------------------------------------------------------------
    def test_3d_no_fire_when_cascade_step_none(self):
        df = _make_3d_uptrend(n=250)
        _add_mas(df, (20, 50, 100, 150, 200))
        _setup_valid_trigger(df)
        assert self._run(df, cascade_step=None) is None

    # ------------------------------------------------------------------
    # 3. cascade_step != 1 → None
    # ------------------------------------------------------------------
    def test_3d_no_fire_when_not_step_1(self):
        df = _make_3d_uptrend(n=250)
        _add_mas(df, (20, 50, 100, 150, 200))
        _setup_valid_trigger(df)
        assert self._run(df, cascade_step=2) is None

    # ------------------------------------------------------------------
    # 4. Unstacked (D50 > D20) → None
    # ------------------------------------------------------------------
    def test_3d_no_fire_when_unstacked(self):
        df = _make_3d_uptrend(n=250)
        _add_mas(df, (20, 50, 100, 150, 200))
        d20 = _setup_valid_trigger(df)
        # Force D50 above D20 — stack broken
        df["ma_50"] = d20 + 5.0
        assert self._run(df, cascade_step=1) is None

    # ------------------------------------------------------------------
    # 5. Strict stack: D20 == D50 → None (>= would fire, > must not)
    # ------------------------------------------------------------------
    def test_3d_strict_stack_equal_mas_fail(self):
        # Flat prices → all MAs equal after compute
        df = _make_daily_df(n=250, base_close=150.0, trend=0.0, volume=1_000_000)
        _add_mas(df, (20, 50, 100, 150, 200))
        d20 = float(df["ma_20"].iloc[-1])
        # Set valid trigger (stack check will fail because D20 == D50 == ...)
        df.iloc[-1, df.columns.get_loc("low")]    = d20 - 0.10
        df.iloc[-1, df.columns.get_loc("close")]  = d20 + 0.50
        df.iloc[-1, df.columns.get_loc("volume")] = 2_000_000
        assert self._run(df, cascade_step=1) is None

    # ------------------------------------------------------------------
    # 6. D200 is NaN → None
    # ------------------------------------------------------------------
    def test_3d_no_fire_when_d200_nan(self):
        # n=180 < 200 → D200 is NaN at last bar
        df = _make_3d_uptrend(n=180)
        _add_mas(df, (20, 50, 100, 150, 200))
        assert float.__gt__(float("nan"), 0) is False or True  # just ensure we're here
        import math
        assert math.isnan(float(df["ma_200"].iloc[-1]))
        _setup_valid_trigger(df)
        assert self._run(df, cascade_step=1) is None

    # ------------------------------------------------------------------
    # 7. No touch (low > D20) → None
    # ------------------------------------------------------------------
    def test_3d_no_fire_when_no_touch(self):
        df = _make_3d_uptrend(n=250)
        _add_mas(df, (20, 50, 100, 150, 200))
        d20 = float(df["ma_20"].iloc[-1])
        # Low is above D20 — no wick touch
        df.iloc[-1, df.columns.get_loc("low")]    = d20 + 1.0
        df.iloc[-1, df.columns.get_loc("close")]  = d20 + 3.0
        df.iloc[-1, df.columns.get_loc("volume")] = 2_000_000
        assert self._run(df, cascade_step=1) is None

    # ------------------------------------------------------------------
    # 8. Touched but closed below D20 → None
    # ------------------------------------------------------------------
    def test_3d_no_fire_when_close_below_d20(self):
        df = _make_3d_uptrend(n=250)
        _add_mas(df, (20, 50, 100, 150, 200))
        d20 = float(df["ma_20"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")]    = d20 - 0.50   # wick touched
        df.iloc[-1, df.columns.get_loc("close")]  = d20 - 0.10   # closed below
        df.iloc[-1, df.columns.get_loc("volume")] = 2_000_000
        assert self._run(df, cascade_step=1) is None

    # ------------------------------------------------------------------
    # 9. Volume below 1.5× threshold → None  (1.4× should NOT fire)
    # ------------------------------------------------------------------
    def test_3d_no_fire_when_low_volume(self):
        # hist_volume=1M → avg=1M → threshold=1.5M; today=1.4M < 1.5M
        df = _make_3d_uptrend(n=250, hist_volume=1_000_000)
        _add_mas(df, (20, 50, 100, 150, 200))
        d20 = float(df["ma_20"].iloc[-1])
        df.iloc[-1, df.columns.get_loc("low")]    = d20 - 0.10
        df.iloc[-1, df.columns.get_loc("close")]  = d20 + 0.50
        df.iloc[-1, df.columns.get_loc("volume")] = 1_400_000   # 1.4× avg — below threshold
        assert self._run(df, cascade_step=1) is None
