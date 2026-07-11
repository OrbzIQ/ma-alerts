"""
sanity.py — Pre-dispatch sanity gate (FIX D).

check_alert() runs a handful of cheap, alert-shape-only checks (no DB, no
network) immediately before an alert would be sent to the signal chat.
Alerts that fail any check are quarantined: not sent to the signal chat,
logged, and reported via a single [OPS] message — but still recorded in
alert_log (with the failure reason in extra_json) so the run's history is
complete and the quarantine is auditable.

This is a last-line defense against alerts built from corrupted, stale, or
logically inconsistent inputs slipping through to the user — it does not
replace or duplicate the signal detectors' own logic.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from src.config import MA_SANITY_MAX_DEVIATION, SANITY_MAX_STALE_TRADING_DAYS

logger = logging.getLogger(__name__)

# Signal types whose direction check requires price > ma_value (a "supported
# above" or "reclaimed above" signal firing with price at or below its MA is
# a contradiction — either the detector or the underlying data is wrong).
_DIRECTIONAL_SIGNAL_TYPES = {"RECLAIM", "MA_SUPPORT", "3D"}


def _as_date(value) -> date | None:
    """Coerce a date, ISO string, or None into a date | None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def check_alert(alert: dict) -> tuple[bool, str | None]:
    """
    Run all sanity checks against an alert dict before it is dispatched.

    Args:
        alert: Alert dict as built by src.signals.build_alert() (ticker,
               signal_type, timeframe, ma_period, price, ma_value, bar_date, ...).

    Returns:
        (True, None) if the alert passes all checks.
        (False, reason) if any check fails — reason is a short, human-readable
        string suitable for both the [OPS] message and alert_log's extra_json.

    Checks run in order; the first failure short-circuits the rest.
    """
    signal_type = alert.get("signal_type")
    timeframe = alert.get("timeframe")
    price = alert.get("price")
    ma_value = alert.get("ma_value")
    bar_date_raw = alert.get("bar_date")
    bar_date = _as_date(bar_date_raw)

    # --- 4. Completeness -----------------------------------------------
    # Checked first: the other checks all assume these fields are usable.
    if bar_date is None or ma_value is None or price is None:
        missing = [
            name for name, val in (
                ("bar_date", bar_date_raw),
                ("ma_value", ma_value),
                ("price", price),
            )
            if val is None or (name == "bar_date" and bar_date is None)
        ]
        return False, f"incomplete alert — missing/unparseable: {', '.join(missing)}"

    try:
        price = float(price)
        ma_value = float(ma_value)
    except (TypeError, ValueError):
        return False, "incomplete alert — price/ma_value not numeric"

    # --- 1. Direction ----------------------------------------------------
    if signal_type in _DIRECTIONAL_SIGNAL_TYPES and not (price > ma_value):
        return False, (
            f"direction check failed — {signal_type} requires price > ma_value "
            f"(price={price}, ma_value={ma_value})"
        )

    # --- 2. Plausibility ---------------------------------------------------
    max_deviation = MA_SANITY_MAX_DEVIATION.get(timeframe)
    if max_deviation is not None and price != 0:
        deviation = abs(price - ma_value) / abs(price)
        if deviation > max_deviation:
            return False, (
                f"plausibility check failed — |price-ma_value|/price = {deviation:.3f} "
                f"exceeds max {max_deviation:.2f} for timeframe {timeframe}"
            )

    # --- 3. Freshness --------------------------------------------------
    # Only applies to Daily alerts (W/M bars are inherently "fresh" relative
    # to their own cadence — a completed weekly/monthly bar isn't stale just
    # because several trading days have passed since it closed).
    if timeframe == "D":
        stale_trading_days = len(pd.bdate_range(
            start=bar_date + timedelta(days=1),
            end=date.today(),
        ))
        if stale_trading_days > SANITY_MAX_STALE_TRADING_DAYS:
            return False, (
                f"freshness check failed — bar_date {bar_date.isoformat()} is "
                f"{stale_trading_days} trading days old "
                f"(max {SANITY_MAX_STALE_TRADING_DAYS})"
            )

    return True, None
