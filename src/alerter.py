"""
alerter.py — Telegram message formatting and dispatch.

Uses direct Telegram Bot API calls (requests library, no python-telegram-bot dependency).
Parse mode: MarkdownV2. All dynamic values MUST be escaped via _escape_md2().

Failure handling: individual send failures are logged but do not abort the run.
Alert records are only written to alert_log AFTER a successful send.
"""

from __future__ import annotations

import logging
import os
from datetime import date

import requests
from dotenv import load_dotenv

import src.db as db

load_dotenv()

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"


# ---------------------------------------------------------------------------
# MarkdownV2 escaping
# ---------------------------------------------------------------------------

def _escape_md2(text: str) -> str:
    """
    Escape all characters that Telegram MarkdownV2 treats as special.
    Must be applied to every dynamic value inserted into message templates.
    Failure to escape causes HTTP 400 from Telegram.
    """
    for ch in r'\.!()[]~>#+-=|{}$`':
        text = text.replace(ch, f"\\{ch}")
    return text


def _fmt_price(value: float | None) -> str:
    """Format a price value as a string safe for MarkdownV2."""
    if value is None:
        return _escape_md2("N/A")
    return _escape_md2(f"${value:.2f}")


def _fmt_float(value: float | None, decimals: int = 2) -> str:
    """Format a float as a string safe for MarkdownV2."""
    if value is None:
        return _escape_md2("N/A")
    return _escape_md2(f"{value:.{decimals}f}")


def _tf_label(tf: str) -> str:
    """Human-readable timeframe label."""
    return {"D": "Daily", "W": "Weekly", "M": "Monthly"}.get(tf, tf)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_3a(alert: dict) -> str:
    """Format a 3A MA Support alert as MarkdownV2."""
    ticker = _escape_md2(alert["ticker"])
    tf = alert["timeframe"]
    period = alert["ma_period"]
    tf_label = _escape_md2(_tf_label(tf))
    ma_label = _escape_md2(f"{tf}{period}")

    price = _fmt_price(alert.get("price"))
    ma_val = _fmt_price(alert.get("ma_value"))
    vol_ratio = alert.get("volume_ratio") or 0.0
    vol_str = _escape_md2(f"{vol_ratio:.1f}× avg  (above 1.5× threshold)")

    extra = alert.get("extra", {})
    touch_count = extra.get("touch_count", 1)
    touch_str = _escape_md2(f"{touch_count} of 3 (within 15\\-day window)")
    wick_low = _fmt_price(alert.get("ma_value"))   # wick touched near MA

    next_res_ma = extra.get("next_resistance_ma")
    next_res_val = extra.get("next_resistance_value")
    if next_res_ma and next_res_val is not None:
        next_res_str = f"{_fmt_price(next_res_val)} \\({_escape_md2(next_res_ma)}\\)"
    else:
        next_res_str = _escape_md2("N/A")

    return (
        f"📊 {ticker} — {tf_label} {ma_label} Support Signal\n"
        f"\n"
        f"Price:        {price}\n"
        f"MA Value:     {ma_val} \\({_escape_md2(f'{period} MA')}\\)\n"
        f"Volume:       {vol_str}\n"
        f"Timeframe:    {tf_label}\n"
        f"Signal Type:  {_escape_md2('MA Support Hold')}\n"
        f"Touch count:  {_escape_md2(f'{touch_count} of 3 (within 15-day window)')}\n"
        f"\n"
        f"Wick low touched {wick_low}, closed at {price}\n"
        f"Next resistance: {next_res_str}\n"
        f"\n"
        f"⚠️ {_escape_md2('Check chart before acting.')}"
    )


def _format_3b(alert: dict) -> str:
    """Format a 3B Reclaim Confirmed alert as MarkdownV2."""
    ticker = _escape_md2(alert["ticker"])
    period = alert["ma_period"]
    ma_val = _fmt_price(alert.get("ma_value"))
    extra = alert.get("extra", {})
    streak = extra.get("streak", 7)
    prev_step = extra.get("previous_step", "?")
    new_step = extra.get("new_step", "?")

    return (
        f"✅ {ticker} — {_escape_md2(f'D{period}')} {_escape_md2('Reclaim Confirmed')}\n"
        f"\n"
        f"MA reclaimed:     {_escape_md2(f'D{period}')} @ {ma_val}\n"
        f"Consecutive days: {_escape_md2(f'{streak} closes above')}\n"
        f"Previous state:   {_escape_md2(f'Step {prev_step}')} → {_escape_md2(f'now Step {new_step}')}\n"
        f"\n"
        f"{_escape_md2('MA is now acting as support again.')}\n"
        f"\n"
        f"⚠️ {_escape_md2('Check chart before acting.')}"
    )


def _format_3c(alert: dict) -> str:
    """Format a 3C Touch Accumulation alert as MarkdownV2."""
    ticker = _escape_md2(alert["ticker"])
    tf = alert["timeframe"]
    period = alert["ma_period"]
    tf_label = _escape_md2(_tf_label(tf))
    ma_label = _escape_md2(f"{tf}{period}")
    ma_val = _fmt_price(alert.get("ma_value"))

    extra = alert.get("extra", {})
    touch_count = extra.get("touch_count", 3)
    touch_dates_raw: list[str] = extra.get("touch_dates", [])
    # Format dates as "May 1 · May 8 · May 14"
    formatted_dates = []
    for ds in touch_dates_raw:
        try:
            d = date.fromisoformat(ds)
            formatted_dates.append(d.strftime("%b %-d"))
        except ValueError:
            formatted_dates.append(ds)
    dates_str = _escape_md2(" · ".join(formatted_dates) if formatted_dates else "N/A")

    return (
        f"🔁 {ticker} — {tf_label} {ma_label} {_escape_md2('Support Accumulation')}\n"
        f"\n"
        f"Touch count:  {_escape_md2(f'{touch_count} of 3 within 15 days')}\n"
        f"MA Value:     {ma_val}\n"
        f"Dates:        {dates_str}\n"
        f"\n"
        f"{_escape_md2('Level is repeatedly holding as support. Potential bottom forming.')}\n"
        f"\n"
        f"⚠️ {_escape_md2('Monitor for breakout confirmation.')}"
    )


def _format_halt_warning(ticker: str, last_success: str | None) -> str:
    """Format a halt/data-failure warning message as MarkdownV2."""
    t = _escape_md2(ticker)
    last = _escape_md2(last_success or "unknown")
    return (
        f"⚠️ {t} — {_escape_md2('3 consecutive data failures')}\n"
        f"\n"
        f"Last successful fetch: {last}\n"
        f"{_escape_md2('Possible halt, delist, or ticker symbol change.')}\n"
        f"{_escape_md2('Verify and update watchlist if needed.')}"
    )


def format_alert(alert: dict) -> str:
    """
    Format an alert dict as a MarkdownV2 Telegram message per spec §9.
    Dispatches to the appropriate signal formatter.
    """
    signal_type = alert.get("signal_type", "")
    if signal_type == "MA_SUPPORT":
        return _format_3a(alert)
    if signal_type == "RECLAIM":
        return _format_3b(alert)
    if signal_type == "TOUCH_ACCUMULATION":
        return _format_3c(alert)
    if signal_type == "HALT_WARNING":
        extra = alert.get("extra", {})
        return _format_halt_warning(alert["ticker"], extra.get("last_success"))
    raise ValueError(f"Unknown signal_type: {signal_type!r}")


# ---------------------------------------------------------------------------
# Telegram dispatch
# ---------------------------------------------------------------------------

def send_telegram_message(message: str) -> bool:
    """
    Send a MarkdownV2 message to TELEGRAM_CHAT_ID via TELEGRAM_BOT_TOKEN.
    Returns True on success, False on any failure.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — cannot send message"
        )
        return False

    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2",
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        logger.error("Network error sending Telegram message: %s", exc)
        return False

    if response.status_code != 200:
        logger.error(
            "Telegram API error %d: %s", response.status_code, response.text[:300]
        )
        return False

    data = response.json()
    if not data.get("ok"):
        logger.error("Telegram returned ok=false: %s", data)
        return False

    return True


def dispatch_alerts(alerts: list[dict]) -> int:
    """
    Format and send each alert to Telegram.

    Inserts successfully-sent alerts into alert_log.
    Failed sends are logged but do not abort the run.

    Returns the count of successfully sent messages.
    """
    sent = 0
    for alert in alerts:
        try:
            message = format_alert(alert)
        except Exception as exc:
            logger.error("Failed to format alert for %s: %s", alert.get("ticker"), exc)
            continue

        success = send_telegram_message(message)
        if success:
            try:
                db.insert_alert(alert)
            except Exception as exc:
                logger.error("Failed to persist alert to DB: %s", exc)
            sent += 1
        else:
            logger.warning("Failed to send alert for %s %s", alert.get("ticker"), alert.get("signal_type"))

    return sent


def send_test_ping() -> bool:
    """
    Send a fixed test message to verify bot credentials and connectivity.
    Used during deployment verification and smoke testing.
    Returns True if message was delivered, False otherwise.
    """
    message = "✅ MA Alert System — test ping successful\\."
    success = send_telegram_message(message)
    if success:
        logger.info("Test ping delivered successfully")
    else:
        logger.error("Test ping failed")
    return success
