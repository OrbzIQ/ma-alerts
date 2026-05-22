"""
health.py — Track halted/null-data tickers and emit halt warnings.

A ticker is considered potentially halted or delisted if Twelve Data returns no
fresh candle for 3 consecutive runs. On the 3rd failure a warning is sent once;
further warnings are throttled to once per 7 days per ticker.
"""

from __future__ import annotations

import logging
from datetime import date

import src.db as db
from src.alerter import _format_halt_warning, send_telegram_message
from src.config import HALT_FAILURE_THRESHOLD, HALT_WARNING_RESEND_DAYS

logger = logging.getLogger(__name__)


def update_after_fetch(ticker: str, fetched_candles: list | None) -> None:
    """
    Call after every fetch attempt (success or failure).

    On success (non-empty candle list): reset consecutive_failures to 0.
    On failure (None or empty list): increment consecutive_failures.
    """
    success = bool(fetched_candles)
    db.update_data_health(ticker, success)
    if success:
        logger.debug("Health update for %s: fetch OK", ticker)
    else:
        health = db.get_data_health(ticker)
        failures = health.get("consecutive_failures", 0)
        logger.warning(
            "Health update for %s: fetch FAILED (consecutive_failures=%d)",
            ticker,
            failures,
        )


def check_and_warn_halts() -> list[str]:
    """
    Scan all active tickers' data_health records. For any ticker that has
    reached or exceeded HALT_FAILURE_THRESHOLD consecutive failures AND has
    not had a warning sent within HALT_WARNING_RESEND_DAYS, send a halt
    warning via Telegram and record the send.

    Returns list of ticker symbols that were warned about this call.
    """
    warned: list[str] = []
    watchlist = db.get_watchlist()

    for entry in watchlist:
        ticker = entry["ticker"]
        health = db.get_data_health(ticker)
        failures = health.get("consecutive_failures", 0)

        if failures < HALT_FAILURE_THRESHOLD:
            continue

        last_sent_raw = health.get("last_warning_sent")
        if last_sent_raw:
            last_sent = date.fromisoformat(last_sent_raw)
            days_since = (date.today() - last_sent).days
            if days_since < HALT_WARNING_RESEND_DAYS:
                logger.debug(
                    "Halt warning for %s suppressed (last sent %d days ago)", ticker, days_since
                )
                continue

        # Send the warning
        last_success = health.get("last_success")
        message = _format_halt_warning(ticker, last_success)
        success = send_telegram_message(message)

        if success:
            db.mark_warning_sent(ticker)
            db.insert_alert(
                {
                    "ticker": ticker,
                    "signal_type": "HALT_WARNING",
                    "timeframe": None,
                    "ma_period": None,
                    "price": None,
                    "ma_value": None,
                    "volume_ratio": None,
                    "extra": {"last_success": last_success, "consecutive_failures": failures},
                    "fired_at": None,
                }
            )
            warned.append(ticker)
            logger.warning(
                "Halt warning sent for %s (failures=%d)", ticker, failures
            )
        else:
            logger.error("Failed to send halt warning for %s", ticker)

    return warned
