"""
labels.py — Human-readable display labels for signal type codes.

Internal signal codes (MA_SUPPORT, RECLAIM, TOUCH_ACCUMULATION) and
function names are unchanged. Use label_for() only at the display layer:
Telegram messages, Streamlit UI, and operator-facing log lines.
"""

SIGNAL_LABELS: dict[str, str] = {
    "3a": "SUPPORT",
    "3b": "BREAKTHROUGH",
    "3c": "ACCUMULATION",
}

# Fix 1 (Bug 1 — "BREAKTHROUGH" direction ambiguity): 3b previously mapped to
# a single static label regardless of which way price crossed the MA. In
# practice _detect_3b_for_timeframe() only ever fires on bar_close > ma_val
# (reclaim confirmations), and forward cascade transitions (downward crosses)
# never dispatch an alert — they only log — so "BREAKTHROUGH" was directionally
# correct by construction but never said so. This makes the direction explicit
# and derives it at format time from the alert's own price/ma_value rather than
# a static constant, with a defensive downward branch for completeness (today
# unreachable in practice — a price <= ma_value RECLAIM alert would also be
# quarantined by sanity.check_alert's direction gate before it ever reaches
# formatting).
BREAKTHROUGH_UP_LABEL: str = "BREAKTHROUGH ↑ (crossed above)"
BREAKTHROUGH_DOWN_LABEL: str = "BREAKDOWN ↓ (crossed below)"


def directional_breakthrough_label(price: float, ma_value: float) -> str:
    """Return the direction-aware 3B header label.

    Args:
        price:    The alert's price (bar_close for a reclaim confirmation).
        ma_value: The MA value price is being compared against.

    Returns:
        BREAKTHROUGH_UP_LABEL if price > ma_value, else BREAKTHROUGH_DOWN_LABEL.
    """
    if price is not None and ma_value is not None and price > ma_value:
        return BREAKTHROUGH_UP_LABEL
    return BREAKTHROUGH_DOWN_LABEL


def label_for(code: str) -> str:
    """Return the display label for a signal code.

    Args:
        code: Signal code, case-insensitive ('3a', '3A', '3b', etc.).

    Returns:
        Display label ('SUPPORT', 'BREAKTHROUGH', 'ACCUMULATION'),
        or code.upper() if unrecognised.
    """
    return SIGNAL_LABELS.get(code.lower(), code.upper())


STEP_LABELS: dict[int, str] = {
    1: "Uptrend intact — above the 50-day average",
    2: "Shallow pullback — below the 50-day average",
    3: "Deeper pullback — below the 100-day average",
    4: "Correction — below the 150-day average",
    5: "Deep correction — below the 200-day average",
}


def step_label(n: int) -> str:
    """Return the plain-language display label for a cascade step number.

    Args:
        n: Cascade step (1-5).

    Returns:
        Human-readable trend status, or f"Step {n}" if unrecognised (e.g. the
        alert code passes '?' or an out-of-range value).
    """
    return STEP_LABELS.get(n, f"Step {n}")


# ---------------------------------------------------------------------------
# Fix 5 — single source of truth for trend status (Bug 5)
# ---------------------------------------------------------------------------
#
# STEP_LABELS (above) phrases each cascade step as a live price claim
# ("below the 150-day average"), but the step number it's fed comes from
# cascade_state.current_step — which is only definitionally true at the
# moment a break or reclaim was recorded, not at message-render time. After
# a reclaim de-escalates the step, the old STEP_LABELS-based line could
# assert a price position nobody had actually checked (e.g. "below the
# 100-day average" printed next to "7 closes above the 150MA").
#
# trend_status() replaces that with a display string built directly from a
# live close-vs-MA comparison, so it can never contradict the alert's own
# numbers.

import math as _math


def trend_status(close: float, daily_mas: dict[int, float | None]) -> str:
    """Build a live trend-status string from close vs each Daily MA.

    Args:
        close:     Today's (or the alert bar's) closing price.
        daily_mas: {period: ma_value} for the Daily MAs (typically 50/100/150/200).
                   Values may be None or NaN — periods with no valid MA are
                   omitted rather than guessed at.

    Returns:
        e.g. "Above D50, D100, D150, D200" or "Above D150, D200 · below D50, D100".
        "N/A" if no MA period had a valid value.
    """
    above: list[str] = []
    below: list[str] = []
    for period in sorted(daily_mas.keys()):
        val = daily_mas.get(period)
        if val is None or (isinstance(val, float) and _math.isnan(val)):
            continue
        label = f"D{period}"
        if close > val:
            above.append(label)
        else:
            below.append(label)

    parts: list[str] = []
    if above:
        parts.append("Above " + ", ".join(above))
    if below:
        parts.append("below " + ", ".join(below))

    return " · ".join(parts) if parts else "N/A"
