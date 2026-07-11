"""
test_sanity.py -- Unit tests for src/sanity.py (FIX D pre-dispatch sanity gate).

Pure logic tests -- no DB, no network.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from src.sanity import check_alert


def _base_alert(**overrides) -> dict:
    alert = {
        "ticker": "TEST",
        "signal_type": "RECLAIM",
        "timeframe": "D",
        "ma_period": 50,
        "price": 110.0,
        "ma_value": 100.0,
        "bar_date": date.today(),
        "extra": {},
    }
    alert.update(overrides)
    return alert


# ---------------------------------------------------------------------------
# 1. Direction
# ---------------------------------------------------------------------------

class TestDirectionCheck:

    @pytest.mark.parametrize("signal_type", ["RECLAIM", "MA_SUPPORT", "3D"])
    def test_passes_when_price_above_ma(self, signal_type):
        alert = _base_alert(signal_type=signal_type, price=110.0, ma_value=100.0)
        ok, reason = check_alert(alert)
        assert ok is True
        assert reason is None

    @pytest.mark.parametrize("signal_type", ["RECLAIM", "MA_SUPPORT", "3D"])
    def test_fails_when_price_at_or_below_ma(self, signal_type):
        alert = _base_alert(signal_type=signal_type, price=100.0, ma_value=100.0)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "direction" in reason

    def test_touch_accumulation_not_subject_to_direction_check(self):
        # TOUCH_ACCUMULATION is not in the directional set -- price <= ma_value
        # should not fail the direction check for this signal type.
        alert = _base_alert(signal_type="TOUCH_ACCUMULATION", price=99.0, ma_value=100.0)
        ok, reason = check_alert(alert)
        assert ok is True


# ---------------------------------------------------------------------------
# 2. Plausibility
# ---------------------------------------------------------------------------

class TestPlausibilityCheck:

    def test_passes_within_daily_tolerance(self):
        # 0.30 deviation < 0.35 max for D
        alert = _base_alert(timeframe="D", price=100.0, ma_value=70.0)
        ok, reason = check_alert(alert)
        assert ok is True

    def test_fails_beyond_daily_tolerance(self):
        # deviation = |100-50|/100 = 0.50 > 0.35 max for D
        alert = _base_alert(timeframe="D", price=100.0, ma_value=50.0)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "plausibility" in reason

    def test_passes_within_weekly_tolerance(self):
        # deviation = |100-45|/100 = 0.55 < 0.60 max for W
        alert = _base_alert(timeframe="W", price=100.0, ma_value=45.0)
        ok, reason = check_alert(alert)
        assert ok is True

    def test_fails_beyond_weekly_tolerance(self):
        # deviation = |100-30|/100 = 0.70 > 0.60 max for W
        alert = _base_alert(timeframe="W", price=100.0, ma_value=30.0)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "plausibility" in reason

    def test_passes_within_monthly_tolerance(self):
        # deviation = |100-30|/100 = 0.70 < 0.75 max for M
        alert = _base_alert(timeframe="M", price=100.0, ma_value=30.0)
        ok, reason = check_alert(alert)
        assert ok is True

    def test_fails_beyond_monthly_tolerance(self):
        # deviation = |100-20|/100 = 0.80 > 0.75 max for M
        alert = _base_alert(timeframe="M", price=100.0, ma_value=20.0)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "plausibility" in reason


# ---------------------------------------------------------------------------
# 3. Freshness
# ---------------------------------------------------------------------------

class TestFreshnessCheck:

    def test_passes_when_bar_date_is_today(self):
        alert = _base_alert(timeframe="D", bar_date=date.today())
        ok, reason = check_alert(alert)
        assert ok is True

    def test_fails_when_bar_date_older_than_max_stale_trading_days(self):
        # Go back far enough that trading days > 2 regardless of weekday.
        stale_date = date.today() - timedelta(days=10)
        alert = _base_alert(timeframe="D", bar_date=stale_date)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "freshness" in reason

    def test_weekly_alerts_not_subject_to_freshness_check(self):
        # W/M bars aren't stale just because trading days have passed since
        # they closed -- the freshness check only applies to Daily.
        stale_date = date.today() - timedelta(days=30)
        alert = _base_alert(timeframe="W", bar_date=stale_date, price=110.0, ma_value=90.0)
        ok, reason = check_alert(alert)
        assert ok is True

    def test_monthly_alerts_not_subject_to_freshness_check(self):
        stale_date = date.today() - timedelta(days=60)
        alert = _base_alert(timeframe="M", bar_date=stale_date, price=110.0, ma_value=60.0)
        ok, reason = check_alert(alert)
        assert ok is True


# ---------------------------------------------------------------------------
# 4. Completeness
# ---------------------------------------------------------------------------

class TestCompletenessCheck:

    def test_fails_when_bar_date_missing(self):
        alert = _base_alert(bar_date=None)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "incomplete" in reason
        assert "bar_date" in reason

    def test_fails_when_ma_value_missing(self):
        alert = _base_alert(ma_value=None)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "incomplete" in reason
        assert "ma_value" in reason

    def test_fails_when_price_missing(self):
        alert = _base_alert(price=None)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "incomplete" in reason
        assert "price" in reason

    def test_fails_when_bar_date_unparseable_string(self):
        alert = _base_alert(bar_date="not-a-date")
        ok, reason = check_alert(alert)
        assert ok is False
        assert "incomplete" in reason

    def test_accepts_iso_string_bar_date(self):
        alert = _base_alert(bar_date=date.today().isoformat())
        ok, reason = check_alert(alert)
        assert ok is True

    def test_completeness_checked_before_other_checks(self):
        """
        Completeness failures should short-circuit before direction/plausibility
        checks run (which would otherwise raise on None arithmetic).
        """
        alert = _base_alert(signal_type="RECLAIM", price=None, ma_value=None, bar_date=None)
        ok, reason = check_alert(alert)
        assert ok is False
        assert "incomplete" in reason
