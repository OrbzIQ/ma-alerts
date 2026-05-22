"""
cascade.py — Per-stock cascade state machine.

Cascade step determines which MAs are evaluated for signal detection.
The step is derived solely from which Daily MAs the closing price has broken.

Step definitions:
  1  close > D50                      → watching D50 for signals
  2  close ≤ D50,  > D100             → watching D100, W50/100, M50
  3  close ≤ D100, > D150             → watching D150, W50/100, M50/100
  4  close ≤ D150, > D200             → watching D200, W100/150/200, M100/150
  5  close ≤ D200                     → watching W100/150/200, M100/150/200

Multi-step gap-downs land directly at the deepest step (handled implicitly by
determine_step_from_close — no special case needed).
"""

from __future__ import annotations

import logging
import math
from datetime import date

from src.config import CASCADE_MA_BY_STEP

logger = logging.getLogger(__name__)


def determine_step_from_close(
    close: float,
    daily_mas: dict[int, float | None],
) -> tuple[int, str]:
    """
    Determine the cascade step from today's closing price and Daily MA values.

    Args:
        close:      Today's closing price.
        daily_mas:  Dict of {period: ma_value}. Values may be None / NaN if
                    insufficient history exists — treated as "not broken".

    Returns:
        (step, broken_ma) where step is 1-5 and broken_ma is one of
        'NONE', 'D50', 'D100', 'D150', 'D200'.
    """
    def _is_valid(v: float | None) -> bool:
        return v is not None and not math.isnan(v)

    d50  = daily_mas.get(50)
    d100 = daily_mas.get(100)
    d150 = daily_mas.get(150)
    d200 = daily_mas.get(200)

    # Work from the deepest MA upward; first condition that holds is the step.
    if _is_valid(d200) and close <= d200:
        return 5, "D200"
    if _is_valid(d150) and close <= d150:
        return 4, "D150"
    if _is_valid(d100) and close <= d100:
        return 3, "D100"
    if _is_valid(d50)  and close <= d50:
        return 2, "D50"

    return 1, "NONE"


def detect_forward_transition(
    ticker: str,
    today_close: float,
    daily_mas: dict[int, float | None],
    db_state: dict,
) -> dict | None:
    """
    Compare today's step (from price) against the stored step in db_state.
    Returns a transition dict if the step has increased (forward), else None.

    Args:
        ticker:      Ticker symbol (for logging).
        today_close: Today's closing price.
        daily_mas:   {period: ma_value} for the Daily timeframe.
        db_state:    Dict from db.get_cascade_state(): {'current_step': int, ...}.

    Returns:
        {'from_step': int, 'to_step': int, 'broken_ma': str, 'date': date}
        or None if no forward transition occurred.
    """
    new_step, new_broken_ma = determine_step_from_close(today_close, daily_mas)
    old_step = db_state.get("current_step", 1)

    if new_step > old_step:
        logger.info(
            "Forward transition for %s: step %d -> %d (broke %s)",
            ticker, old_step, new_step, new_broken_ma,
        )
        return {
            "from_step": old_step,
            "to_step": new_step,
            "broken_ma": new_broken_ma,
            "date": date.today(),
        }
    return None


def apply_reclaim_de_escalation(
    ticker: str,
    current_state: dict,
) -> dict | None:
    """
    Compute the new cascade state after a 3B reclaim confirmation.

    De-escalation rule: step N -> step (N-1). Cannot go below step 1.

    The new broken_ma is determined by CASCADE_MA_BY_STEP[N-1] (the Daily MA that
    defines the lower step). If N-1 == 1, broken_ma = 'NONE'.

    Args:
        ticker:        Ticker symbol (for logging).
        current_state: Dict from db.get_cascade_state().

    Returns:
        {'new_step': int, 'new_broken_ma': str} or None if already at step 1.
    """
    current_step = current_state.get("current_step", 1)

    if current_step <= 1:
        logger.debug("De-escalation requested for %s but already at step 1", ticker)
        return None

    new_step = current_step - 1

    if new_step == 1:
        new_broken_ma = "NONE"
    else:
        period = CASCADE_MA_BY_STEP.get(new_step)
        if period is None:
            logger.error("No CASCADE_MA_BY_STEP entry for step %d (ticker %s)", new_step, ticker)
            return None
        new_broken_ma = f"D{period}"

    logger.info(
        "De-escalation for %s: step %d -> %d (new broken_ma=%s)",
        ticker, current_step, new_step, new_broken_ma,
    )
    return {"new_step": new_step, "new_broken_ma": new_broken_ma}
