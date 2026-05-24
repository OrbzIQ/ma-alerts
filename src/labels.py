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
