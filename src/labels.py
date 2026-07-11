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
