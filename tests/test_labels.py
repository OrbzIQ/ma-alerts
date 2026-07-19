"""
test_labels.py -- Unit tests for src/labels.py's Fix 1 (direction-aware
BREAKTHROUGH label) and Fix 5 (live trend_status) helpers.

Pure logic tests -- no DB, no network.
"""

from __future__ import annotations

import math

from src.labels import (
    BREAKTHROUGH_DOWN_LABEL,
    BREAKTHROUGH_UP_LABEL,
    directional_breakthrough_label,
    trend_status,
)


class TestDirectionalBreakthroughLabel:

    def test_price_above_ma_is_upward_breakthrough(self):
        assert directional_breakthrough_label(110.0, 100.0) == BREAKTHROUGH_UP_LABEL
        assert "↑" in directional_breakthrough_label(110.0, 100.0)

    def test_price_at_or_below_ma_is_defensive_breakdown(self):
        # Unreachable in practice today (sanity.check_alert's direction gate
        # would quarantine a RECLAIM alert with price <= ma_value before it
        # ever reaches formatting) -- but the label must still be direction-
        # derived, not a hardcoded assumption of "always up".
        assert directional_breakthrough_label(90.0, 100.0) == BREAKTHROUGH_DOWN_LABEL
        assert directional_breakthrough_label(100.0, 100.0) == BREAKTHROUGH_DOWN_LABEL

    def test_none_values_default_to_breakdown_label(self):
        assert directional_breakthrough_label(None, 100.0) == BREAKTHROUGH_DOWN_LABEL
        assert directional_breakthrough_label(110.0, None) == BREAKTHROUGH_DOWN_LABEL


class TestTrendStatus:

    def test_above_all_mas(self):
        result = trend_status(210.0, {50: 200.0, 100: 180.0, 150: 160.0, 200: 140.0})
        assert result == "Above D50, D100, D150, D200"

    def test_mixed_above_and_below(self):
        # Price above D150/D200 but below D50/D100 -- the exact AMZN-style
        # contradiction Bug 5 was about: this string must never claim "below
        # the 100-day average" without also being consistent about D150/D200.
        result = trend_status(170.0, {50: 200.0, 100: 180.0, 150: 160.0, 200: 140.0})
        assert result == "Above D150, D200 · below D50, D100"

    def test_below_all_mas(self):
        result = trend_status(100.0, {50: 200.0, 100: 180.0, 150: 160.0, 200: 140.0})
        assert result == "below D50, D100, D150, D200"

    def test_missing_or_nan_mas_are_omitted_not_guessed(self):
        result = trend_status(170.0, {50: 200.0, 100: None, 150: float("nan"), 200: 140.0})
        assert result == "Above D200 · below D50"

    def test_no_valid_mas_returns_na(self):
        result = trend_status(170.0, {50: None, 100: None})
        assert result == "N/A"
