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
from src.labels import directional_breakthrough_label, label_for, step_label, trend_status
from src.sanity import check_alert

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


def _ordinal(n) -> str:
    """Return the ordinal string for an int (1st, 2nd, 3rd, 7th, ...)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return f"{n}th"
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fmt_bar_date(bar_date) -> str:
    """Format an alert's bar_date as MarkdownV2, or 'N/A' if None."""
    if bar_date is None:
        return _escape_md2("N/A")
    if isinstance(bar_date, str):
        return _escape_md2(bar_date)
    return _escape_md2(bar_date.isoformat())


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
    wick_low = _fmt_price(extra.get("bar_low"))   # actual bar low that touched the MA
    bar_date_str = _fmt_bar_date(alert.get("bar_date"))

    next_res_ma = extra.get("next_resistance_ma")
    next_res_val = extra.get("next_resistance_value")
    if next_res_ma and next_res_val is not None:
        next_res_str = f"{_fmt_price(next_res_val)} \\({_escape_md2(next_res_ma)}\\)"
    else:
        next_res_str = _escape_md2("N/A")

    # Fix 3 (Bug 3): a Daily wick spanning multiple stacked MAs now qualifies
    # independently at every level; the primary (highest-period) level is the
    # main signal above, and any others are consolidated into one extra line
    # here rather than sending a separate message per level.
    also_touched = extra.get("also_touched") or []
    if also_touched:
        also_str = ", ".join(
            f"{_escape_md2(lvl['ma'])} @ {_fmt_price(lvl['value'])}" for lvl in also_touched
        )
        also_line = f"Also touched: {also_str}\n"
    else:
        also_line = ""

    return (
        f"📊 {ticker} — {tf_label} {ma_label} Support Signal\n"
        f"\n"
        f"Price:        {price}\n"
        f"MA Value:     {ma_val} \\({_escape_md2(f'{period} MA')}\\)\n"
        f"Volume:       {vol_str}\n"
        f"Timeframe:    {tf_label}\n"
        f"Signal Type:  {_escape_md2(label_for('3a'))}\n"
        f"Touch count:  {_escape_md2(f'{touch_count} of 3 (within 15-day window)')}\n"
        f"Bar date:     {bar_date_str}\n"
        f"\n"
        f"Wick low touched {wick_low}, closed at {price}\n"
        f"Next resistance: {next_res_str}\n"
        f"{also_line}"
        f"\n"
        f"⚠️ {_escape_md2('Check chart before acting.')}"
    )


def _format_3b(alert: dict) -> str:
    """Format a 3B Reclaim Confirmed alert as MarkdownV2."""
    ticker = _escape_md2(alert["ticker"])
    period = alert["ma_period"]
    tf = alert.get("timeframe", "D")
    tf_label = _escape_md2(_tf_label(tf))
    ma_label = _escape_md2(f"{tf}{period}")
    ma_value = alert.get("ma_value")
    ma_val = _fmt_price(ma_value)
    bar_date_str = _fmt_bar_date(alert.get("bar_date"))
    extra = alert.get("extra", {})
    streak = extra.get("streak", 7)
    prev_step = extra.get("previous_step", "?")
    new_step = extra.get("new_step", "?")
    break_date_str = _fmt_bar_date(extra.get("break_date"))
    first_reclaim_date_str = _fmt_bar_date(extra.get("first_reclaim_date"))

    # Daily: show trend status (Fix 5, live close-vs-MA — never a stale
    # STEP_LABELS claim) and streak-rule copy (Fix 4); W/M: omit (no
    # de-escalation, no per-MA trend concept at those cadences).
    streak_unit = "days" if tf == "D" else "closes"
    if tf == "D":
        trend_status_str = extra.get("trend_status")
        if trend_status_str is None:
            # Back-compat for alerts built before Fix 5 (or a caller that
            # didn't populate it): fall back to the old step-derived text
            # rather than crashing the formatter.
            trend_status_str = f"{step_label(prev_step)} → now: {step_label(new_step)}"
        state_line = f"Trend status:        {_escape_md2(trend_status_str)}\n"
        streak_rule_line = f"{_escape_md2('Streak rule: closes only — wicks ignored.')}\n"
    else:
        state_line = ""
        streak_rule_line = ""

    price = alert.get("price")
    if price is not None and ma_value:
        # Describes where the MA sits relative to price, e.g. "22.1% below price"
        # (MA below price is the normal case for a reclaim — price closed above it).
        pct = (price - ma_value) / ma_value * 100.0
        direction = "below" if pct >= 0 else "above"
        ma_vs_price_str = _escape_md2(f"{abs(pct):.1f}% {direction} price")
    else:
        ma_vs_price_str = _escape_md2("N/A")

    # Fix 1 (Bug 1): direction-derived header instead of the static
    # SIGNAL_LABELS["3b"] constant — see src/labels.py::directional_breakthrough_label.
    header_label = _escape_md2(directional_breakthrough_label(price, ma_value))

    return (
        f"✅ {ticker} — {tf_label} {ma_label} {header_label}\n"
        f"\n"
        f"MA reclaimed:        {ma_label} @ {ma_val}\n"
        f"Consecutive {streak_unit}: {_escape_md2(f'{streak} closes above')}\n"
        f"Timeframe:           {tf_label}\n"
        f"Broken on:           {break_date_str} {_escape_md2('(price first lost this MA)')}\n"
        f"First reclaimed:     {first_reclaim_date_str} {_escape_md2('(price moved back above)')}\n"
        f"Confirmed:           {bar_date_str} {_escape_md2(f'({_ordinal(streak)} consecutive close above — anti-whipsaw passed)')}\n"
        f"MA vs price:         {ma_vs_price_str}\n"
        f"{state_line}"
        f"{streak_rule_line}"
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
    bar_date_str = _fmt_bar_date(alert.get("bar_date"))

    return (
        f"🔁 {ticker} — {tf_label} {ma_label} {_escape_md2(label_for('3c'))}\n"
        f"\n"
        f"Touch count:  {_escape_md2(f'{touch_count} of 3 within 15 days')}\n"
        f"MA Value:     {ma_val}\n"
        f"Dates:        {dates_str}\n"
        f"Bar date:     {bar_date_str}\n"
        f"\n"
        f"{_escape_md2('Level is repeatedly holding as support. Potential bottom forming.')}\n"
        f"\n"
        f"⚠️ {_escape_md2('Monitor for breakout confirmation.')}"
    )


def _format_3d(alert: dict) -> str:
    """Format a 3D D20 Momentum Touch alert as MarkdownV2."""
    ticker = _escape_md2(alert["ticker"])
    price  = _fmt_price(alert.get("price"))
    d20    = _fmt_price(alert.get("ma_value"))

    extra      = alert.get("extra", {})
    wick_low   = _fmt_price(extra.get("low"))
    close_val  = _fmt_price(extra.get("close"))

    vol_ratio  = alert.get("volume_ratio") or 0.0
    vol_str    = _escape_md2(f"{vol_ratio:.1f}× avg")

    stack_str  = _escape_md2("D20 > D50 > D100 > D150 > D200 ✓")
    bar_date_str = _fmt_bar_date(alert.get("bar_date"))

    return (
        f"🚀 {ticker} — {_escape_md2('D20 Momentum Touch')}\n"
        f"\n"
        f"Price:        {price}\n"
        f"D20 MA:       {d20}\n"
        f"Volume:       {vol_str}\n"
        f"MA stack:     {stack_str}\n"
        f"Bar date:     {bar_date_str}\n"
        f"\n"
        f"Wick low touched {wick_low}, closed at {close_val}\n"
        f"\n"
        f"⚠️ {_escape_md2('Continuation entry — check chart for trend integrity.')}"
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
    if signal_type == "3D":
        return _format_3d(alert)
    if signal_type == "HALT_WARNING":
        extra = alert.get("extra", {})
        return _format_halt_warning(alert["ticker"], extra.get("last_success"))
    raise ValueError(f"Unknown signal_type: {signal_type!r}")


# ---------------------------------------------------------------------------
# Telegram dispatch
# ---------------------------------------------------------------------------

def send_telegram_message(
    message: str,
    *,
    chat_id: str | None = None,
    parse_mode: str | None = "MarkdownV2",
) -> bool:
    """
    Send a message to Telegram via TELEGRAM_BOT_TOKEN.

    chat_id:    target chat; defaults to TELEGRAM_CHAT_ID env var.
    parse_mode: Telegram parse mode; defaults to MarkdownV2.
                Pass None for plain text (e.g. ops alerts).
    Returns True on success, False on any failure.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    resolved_chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not resolved_chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN or chat_id not set — cannot send message"
        )
        return False

    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload: dict = {
        "chat_id": resolved_chat_id,
        "text": message,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

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


def send_ops_message(text: str) -> bool:
    """
    Send a plain-text [OPS] alert to the dedicated ops Telegram chat.

    Targets TELEGRAM_OPS_CHAT_ID. If unset, logs a warning and falls back to
    the signal chat (TELEGRAM_CHAT_ID) so no ops alert is silently lost.
    Always prefixes the message with '[OPS]' for visual scanability.
    """
    ops_chat_id = os.getenv("TELEGRAM_OPS_CHAT_ID", "")
    message = f"[OPS] {text}"

    if not ops_chat_id:
        logger.warning(
            "TELEGRAM_OPS_CHAT_ID not set — routing ops alert to signal chat"
        )
        return send_telegram_message(message, parse_mode=None)

    return send_telegram_message(message, chat_id=ops_chat_id, parse_mode=None)


def dispatch_alerts(alerts: list[dict]) -> int:
    """
    Format and send each alert to Telegram.

    Inserts successfully-sent alerts into alert_log.
    Failed sends are logged but do not abort the run.

    Cooldown gate: skips any alert whose (ticker, signal_type, timeframe,
    ma_period) combo already has a row in alert_log within the last
    ALERT_COOLDOWN_DAYS days (default 5, overridable via env var).

    Returns the count of successfully sent messages.
    """
    cooldown_days = int(os.getenv("ALERT_COOLDOWN_DAYS", "5"))
    sent = 0
    for alert in alerts:
        ticker = alert.get("ticker", "")
        signal_type = alert.get("signal_type", "")
        timeframe = alert.get("timeframe")
        ma_period = alert.get("ma_period")

        if db.recent_alert_exists(ticker, ma_period, timeframe, signal_type, days=cooldown_days):
            logger.info(
                "Suppressed by cooldown (%dd): %s %s %s%s",
                cooldown_days,
                ticker,
                signal_type,
                timeframe or "",
                ma_period or "",
            )
            continue

        # FIX D: pre-dispatch sanity gate. An alert that fails this check is
        # quarantined — not sent to the signal chat, but still recorded in
        # alert_log (with the failure reason in extra_json) so the run's
        # history is complete and the quarantine is auditable. A single
        # [OPS] message is sent per quarantined alert.
        ok, reason = check_alert(alert)
        if not ok:
            logger.warning(
                "Quarantined by sanity gate: %s %s %s%s — %s",
                ticker, signal_type, timeframe or "", ma_period or "", reason,
            )
            try:
                send_ops_message(
                    f"Alert quarantined — {ticker} {signal_type} {timeframe or ''}{ma_period or ''}: {reason}"
                )
            except Exception as exc:
                logger.error("Failed to send quarantine ops alert for %s: %s", ticker, exc)

            quarantined_alert = dict(alert)
            quarantined_alert["extra"] = {**alert.get("extra", {}), "quarantined": reason}
            try:
                db.insert_alert(quarantined_alert)
            except Exception as exc:
                logger.error("Failed to persist quarantined alert to DB: %s", exc)
            continue

        try:
            message = format_alert(alert)
        except Exception as exc:
            logger.error("Failed to format alert for %s: %s", alert.get("ticker"), exc)
            continue

        success = send_telegram_message(message)
        if success:
            try:
                db.insert_alert(alert)
                # Fix 3 (Bug 3): a consolidated Daily 3A message covers
                # multiple qualifying MA levels (primary + also_touched).
                # Only one Telegram message is sent, but the cooldown gate
                # (db.recent_alert_exists) keys on (ticker, signal_type,
                # timeframe, ma_period) — so each secondary level also needs
                # its own alert_log row, or a later scan would re-alert on a
                # level that was just covered by this consolidated message.
                also_touched = alert.get("extra", {}).get("also_touched") or []
                for lvl in also_touched:
                    try:
                        also_ma_period = int(str(lvl["ma"]).lstrip("D"))
                    except (KeyError, ValueError, TypeError):
                        continue
                    also_alert = dict(alert)
                    also_alert["ma_period"] = also_ma_period
                    also_alert["ma_value"] = lvl.get("value")
                    db.insert_alert(also_alert)
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
