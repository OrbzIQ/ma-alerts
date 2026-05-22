"""
test_cascade.py — Unit tests for cascade state machine.

Tests all forward and backward transitions, including multi-step gap-downs.
Uses no live API calls and no real DB — pure logic tests.
"""

import math
import pytest

from src.cascade import (
    apply_reclaim_de_escalation,
    detect_forward_transition,
    determine_step_from_close,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def standard_mas() -> dict:
    """Standard set of Daily MA values for step boundary testing."""
    return {50: 200.0, 100: 180.0, 150: 160.0, 200: 140.0}


@pytest.fixture
def partial_mas() -> dict:
    """Only D50 and D100 defined; D150 and D200 are None."""
    return {50: 200.0, 100: 180.0, 150: None, 200: None}


# ---------------------------------------------------------------------------
# determine_step_from_close
# ---------------------------------------------------------------------------

class TestDetermineStepFromClose:

    def test_step_1_above_all(self, standard_mas):
        step, broken = determine_step_from_close(210.0, standard_mas)
        assert step == 1
        assert broken == "NONE"

    def test_step_1_exactly_at_d50(self, standard_mas):
        # Exactly at D50 (close == D50) should break to step 2
        # close <= D50 → step 2
        step, broken = determine_step_from_close(200.0, standard_mas)
        assert step == 2
        assert broken == "D50"

    def test_step_2_between_d50_and_d100(self, standard_mas):
        step, broken = determine_step_from_close(190.0, standard_mas)
        assert step == 2
        assert broken == "D50"

    def test_step_3_between_d100_and_d150(self, standard_mas):
        step, broken = determine_step_from_close(170.0, standard_mas)
        assert step == 3
        assert broken == "D100"

    def test_step_4_between_d150_and_d200(self, standard_mas):
        step, broken = determine_step_from_close(150.0, standard_mas)
        assert step == 4
        assert broken == "D150"

    def test_step_5_below_d200(self, standard_mas):
        step, broken = determine_step_from_close(130.0, standard_mas)
        assert step == 5
        assert broken == "D200"

    def test_step_5_exactly_at_d200(self, standard_mas):
        step, broken = determine_step_from_close(140.0, standard_mas)
        assert step == 5
        assert broken == "D200"

    def test_gap_down_step1_to_step3(self, standard_mas):
        """Single close below both D50 and D100 → jumps to step 3."""
        step, broken = determine_step_from_close(175.0, standard_mas)
        assert step == 3
        assert broken == "D100"

    def test_gap_down_step1_to_step4(self, standard_mas):
        """Single close below D50, D100, D150 → step 4."""
        step, broken = determine_step_from_close(155.0, standard_mas)
        assert step == 4
        assert broken == "D150"

    def test_gap_down_step1_to_step5(self, standard_mas):
        """Single close below all MAs → step 5."""
        step, broken = determine_step_from_close(130.0, standard_mas)
        assert step == 5
        assert broken == "D200"

    def test_nan_d50_treated_as_not_broken(self):
        """NaN MA values must not trigger a step change."""
        mas = {50: float("nan"), 100: 180.0, 150: 160.0, 200: 140.0}
        step, broken = determine_step_from_close(195.0, mas)
        # D50 is NaN → ignored; price 195 > D100 (180) → step 1
        assert step == 1
        assert broken == "NONE"

    def test_none_d50_treated_as_not_broken(self):
        """None MA values must not trigger a step change."""
        mas = {50: None, 100: 180.0, 150: 160.0, 200: 140.0}
        step, broken = determine_step_from_close(195.0, mas)
        assert step == 1
        assert broken == "NONE"

    def test_all_mas_nan(self):
        """All NaN → step 1."""
        mas = {50: float("nan"), 100: float("nan"), 150: float("nan"), 200: float("nan")}
        step, broken = determine_step_from_close(100.0, mas)
        assert step == 1
        assert broken == "NONE"

    def test_only_d200_defined(self):
        """Only D200 valid; price below it → step 5."""
        mas = {50: None, 100: None, 150: None, 200: 150.0}
        step, broken = determine_step_from_close(140.0, mas)
        assert step == 5
        assert broken == "D200"

    def test_partial_mas_price_above_all_defined(self, partial_mas):
        """Only D50/D100 defined; price above both → step 1."""
        step, broken = determine_step_from_close(210.0, partial_mas)
        assert step == 1
        assert broken == "NONE"

    def test_partial_mas_price_below_d100(self, partial_mas):
        """Only D50/D100 defined; price below both → step 3 (D150/D200 not available)."""
        # close <= D100=180 → would be step 3 if D150 were defined,
        # but D150/D200 are None so close <= D100 and not <= D150 (None) → step 3
        step, broken = determine_step_from_close(170.0, partial_mas)
        assert step == 3
        assert broken == "D100"


# ---------------------------------------------------------------------------
# detect_forward_transition
# ---------------------------------------------------------------------------

class TestDetectForwardTransition:

    def test_forward_step1_to_step2(self, standard_mas):
        db_state = {"current_step": 1, "broken_ma": "NONE"}
        t = detect_forward_transition("TEST", 190.0, standard_mas, db_state)
        assert t is not None
        assert t["from_step"] == 1
        assert t["to_step"] == 2
        assert t["broken_ma"] == "D50"

    def test_forward_gap_down_step1_to_step3(self, standard_mas):
        db_state = {"current_step": 1, "broken_ma": "NONE"}
        t = detect_forward_transition("TEST", 170.0, standard_mas, db_state)
        assert t is not None
        assert t["to_step"] == 3
        assert t["broken_ma"] == "D100"

    def test_no_transition_same_step(self, standard_mas):
        db_state = {"current_step": 3, "broken_ma": "D100"}
        t = detect_forward_transition("TEST", 170.0, standard_mas, db_state)
        assert t is None

    def test_no_transition_going_back(self, standard_mas):
        """De-escalation is handled by 3B, not detect_forward_transition."""
        db_state = {"current_step": 5, "broken_ma": "D200"}
        t = detect_forward_transition("TEST", 210.0, standard_mas, db_state)
        # Price is back above all MAs but forward_transition only detects *increases*
        assert t is None

    def test_forward_step2_to_step4(self, standard_mas):
        """Already at step 2; drops below D150 in one bar → step 4."""
        db_state = {"current_step": 2, "broken_ma": "D50"}
        t = detect_forward_transition("TEST", 155.0, standard_mas, db_state)
        assert t is not None
        assert t["to_step"] == 4
        assert t["broken_ma"] == "D150"

    def test_forward_step4_to_step5(self, standard_mas):
        db_state = {"current_step": 4, "broken_ma": "D150"}
        t = detect_forward_transition("TEST", 130.0, standard_mas, db_state)
        assert t is not None
        assert t["to_step"] == 5
        assert t["broken_ma"] == "D200"

    def test_default_state_missing_current_step(self, standard_mas):
        """db_state with missing 'current_step' defaults to 1."""
        db_state = {}
        t = detect_forward_transition("TEST", 190.0, standard_mas, db_state)
        assert t is not None
        assert t["from_step"] == 1
        assert t["to_step"] == 2


# ---------------------------------------------------------------------------
# apply_reclaim_de_escalation
# ---------------------------------------------------------------------------

class TestApplyReclaimDeEscalation:

    def test_step5_to_step4(self):
        state = {"current_step": 5, "broken_ma": "D200"}
        result = apply_reclaim_de_escalation("TEST", state)
        assert result is not None
        assert result["new_step"] == 4
        assert result["new_broken_ma"] == "D150"

    def test_step4_to_step3(self):
        state = {"current_step": 4, "broken_ma": "D150"}
        result = apply_reclaim_de_escalation("TEST", state)
        assert result is not None
        assert result["new_step"] == 3
        assert result["new_broken_ma"] == "D100"

    def test_step3_to_step2(self):
        state = {"current_step": 3, "broken_ma": "D100"}
        result = apply_reclaim_de_escalation("TEST", state)
        assert result is not None
        assert result["new_step"] == 2
        assert result["new_broken_ma"] == "D50"

    def test_step2_to_step1(self):
        state = {"current_step": 2, "broken_ma": "D50"}
        result = apply_reclaim_de_escalation("TEST", state)
        assert result is not None
        assert result["new_step"] == 1
        assert result["new_broken_ma"] == "NONE"

    def test_step1_cannot_deescalate(self):
        state = {"current_step": 1, "broken_ma": "NONE"}
        result = apply_reclaim_de_escalation("TEST", state)
        assert result is None

    def test_de_escalation_is_exactly_one_step(self):
        """Each reclaim reduces by exactly one step, regardless of how many breaks occurred."""
        state = {"current_step": 5, "broken_ma": "D200"}
        result = apply_reclaim_de_escalation("TEST", state)
        assert result["new_step"] == 4  # Not 1 — only one step at a time

    def test_missing_current_step_defaults_to_1(self):
        state = {}
        result = apply_reclaim_de_escalation("TEST", state)
        assert result is None  # current_step defaults to 1, can't de-escalate
