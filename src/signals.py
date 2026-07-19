"""
signals.py — Three signal detectors: 3A (MA Support), 3B (Reclaim), 3C (Touch Accumulation).

Key invariants:
  - detect_3a_ma_support  MUST run before detect_3c_touch_accumulation so today's
    touch is in the DB when 3C queries it.
  - detect_3b_reclaim     MUST be skipped on days when a forward cascade transition fires.
  - Proximity rule is applied INDEPENDENTLY per timeframe (D/W/M), not globally.
    Within each timeframe group at the current cascade step, find the highest-period
    MA whose value is still below current price and use only that one MA.

Volume comparator for 3A is timeframe-aware: Daily signals use the last 20 completed
daily bar volumes; Weekly uses the last 20 completed weekly bar volumes; Monthly uses
the last 20 completed monthly bar volumes. Median is used (not mean) so outlier bars
do not inflate the threshold. Threshold remains 1.5×.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone

import pandas as pd

import src.db as db
from src.config import (
    CASCADE_CHECKS,
    MA_PERIODS_DAILY,
    MOMENTUM_VOLUME_MULTIPLIER,
    RECLAIM_STREAK_DAYS,
    RECLAIM_STREAK_DAYS_MONTHLY,
    RECLAIM_STREAK_DAYS_WEEKLY,
    TOUCH_THRESHOLD,
    TOUCH_WINDOW_DAYS,
    VOLUME_LOOKBACK_DAYS,
)
from src.labels import label_for
from src.ma import latest_ma_value

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_valid(v: float | None) -> bool:
    return v is not None and not math.isnan(v)


def _get_timeframe_df(
    timeframe: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
) -> pd.DataFrame:
    """Return the appropriate OHLCV DataFrame for a given timeframe code."""
    if timeframe == "D":
        return daily_df
    if timeframe == "W":
        return weekly_df
    if timeframe == "M":
        return monthly_df
    raise ValueError(f"Unknown timeframe: {timeframe!r}")


def _compute_median_volume(df: pd.DataFrame, today: date, n: int = 20) -> float | None:
    """Return the median volume of the last n completed bars in df.

    Completed means index.date strictly before today — same rule as
    _latest_completed_bar. Works for any timeframe (D/W/M): pass daily_df,
    weekly_df, or monthly_df respectively.

    Returns None if fewer than n completed bars are available, which causes the
    volume filter to fail safe (volume_ok = False).
    """
    if df.empty:
        return None
    completed = df[df.index.date < today]
    if len(completed) < n:
        return None
    return float(completed["volume"].iloc[-n:].median())


def _get_proximity_winner(
    timeframe: str,
    periods_in_group: list[int],
    df: pd.DataFrame,
    current_price: float,
) -> int | None:
    """
    Apply the proximity rule for a single timeframe group.

    Finds the highest-period MA whose value is still BELOW current_price
    (i.e. price is above it → it can act as support). Lower-period MAs within
    the same timeframe are ignored.

    Returns the winning MA period, or None if no valid MA qualifies.
    """
    # Sort descending: evaluate highest period first
    for period in sorted(periods_in_group, reverse=True):
        col = f"ma_{period}"
        if col not in df.columns or df.empty:
            continue
        ma_val = df[col].iloc[-1]
        if not _is_valid(ma_val):
            continue
        if current_price > ma_val:
            return period
    return None


def _latest_completed_bar(df: pd.DataFrame, today: date) -> pd.Series | None:
    """Return the most recent bar whose index date is strictly before today.

    Prevents in-progress weekly/monthly bars from being evaluated as if they
    were closed. A weekly bar whose Friday index date equals today is excluded
    because the market is still open (or just closed — the cascade rule is
    conservative: strictly before today).

    Returns None if df is empty or all bars are dated today-or-later.
    """
    if df.empty:
        return None
    completed = df[df.index.date < today]
    if completed.empty:
        return None
    return completed.iloc[-1]


def _next_resistance(
    ticker: str,
    cascade_step: int,
    current_timeframe: str,
    current_period: int,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    current_price: float,
) -> tuple[str, float] | None:
    """
    Find the next higher MA that price is currently below (i.e. the next resistance).
    Searches all timeframes in CASCADE_CHECKS[cascade_step] above current_period.
    Returns ('W150', value) style tuple or None.
    """
    checks = CASCADE_CHECKS.get(cascade_step, [])
    candidates = []
    for tf, period in checks:
        if tf == current_timeframe and period <= current_period:
            continue
        df = _get_timeframe_df(tf, daily_df, weekly_df, monthly_df)
        if df.empty:
            continue
        val = latest_ma_value(df, period)
        if _is_valid(val) and val > current_price:
            candidates.append((tf, period, val))

    if not candidates:
        return None
    # Pick the closest (smallest) resistance value
    candidates.sort(key=lambda x: x[2])
    tf, period, val = candidates[0]
    return (f"{tf}{period}", val)


def build_alert(
    ticker: str,
    signal_type: str,
    timeframe: str,
    ma_period: int,
    price: float,
    ma_value: float,
    extra: dict,
    volume_ratio: float | None = None,
    bar_date: date | None = None,
) -> dict:
    """Construct a standardised alert dict used downstream by alerter.py."""
    return {
        "ticker": ticker,
        "signal_type": signal_type,
        "timeframe": timeframe,
        "ma_period": ma_period,
        "price": price,
        "ma_value": ma_value,
        "volume_ratio": volume_ratio,
        "extra": extra,
        "bar_date": bar_date,
        "fired_at": _utcnow_iso(),
    }


# ---------------------------------------------------------------------------
# Signal 3A — MA Support
# ---------------------------------------------------------------------------

def detect_3a_ma_support(
    ticker: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    cascade_step: int,
) -> list[dict]:
    """
    Detect MA Support signals (3A) for a ticker at its current cascade step.

    Daily (Fix 3 / Bug 3): evaluated INDEPENDENTLY across all of D50/D100/
    D150/D200 (src.config.MA_PERIODS) rather than just the single cascade-step
    proximity winner — a CASCADE_CHECKS/proximity-winner design that meant a
    wick spanning two stacked Daily MAs could only ever produce one alert (the
    designed behavior for the original spec, changed here per Glenn's decision).
    Volume is evaluated once per bar and shared across all qualifying levels.
    A touch_log row is recorded per qualifying Daily level (so 3C accumulates
    correctly per MA), but only ONE consolidated Telegram message is emitted
    per ticker per bar: the primary level is the highest-period qualifying MA,
    with any others listed in extra.also_touched (see alerter._format_3a).

    W/M (unchanged): for each timeframe group at this step,
      1. Apply proximity rule (per-timeframe): find the highest-period MA that price
         is currently above.
      2. Check the last completed bar: wick touched MA (low ≤ MA), closed above MA
         (close > MA), and volume ≥ 1.5× 20-day daily average.
      3. On pass: record the touch in touch_log AND emit an alert dict.

    Returns list of alert dicts (empty if nothing fires).
    """
    if daily_df.empty:
        return []

    alerts: list[dict] = []

    # Compute MAs on each timeframe upfront
    from src.ma import compute_ma as _compute_ma
    from src.config import MA_PERIODS
    for df_name, df in [("daily", daily_df), ("weekly", weekly_df), ("monthly", monthly_df)]:
        if not df.empty:
            _compute_ma(df, MA_PERIODS)

    today_bar = daily_df.iloc[-1]
    today_close: float = float(today_bar["close"])
    today_low: float = float(today_bar["low"])
    today_date: date = daily_df.index[-1].date()

    # --- Daily: all-levels independent evaluation (Fix 3) -------------------
    median_vol = _compute_median_volume(daily_df, today_date, VOLUME_LOOKBACK_DAYS)
    today_bar_volume = float(today_bar["volume"])
    vol_ratio = today_bar_volume / median_vol if median_vol else 0.0
    volume_ok = median_vol is not None and vol_ratio >= MOMENTUM_VOLUME_MULTIPLIER

    qualifying_levels: list[tuple[int, float]] = []
    for period in sorted(MA_PERIODS, reverse=True):
        col = f"ma_{period}"
        if col not in daily_df.columns:
            continue
        ma_val_raw = daily_df[col].iloc[-1]
        if not _is_valid(ma_val_raw):
            continue
        ma_val = float(ma_val_raw)
        wick_touched = today_low <= ma_val
        closed_above = today_close > ma_val
        if wick_touched and closed_above and volume_ok:
            qualifying_levels.append((period, ma_val))

    if qualifying_levels:
        # Record a touch_log row per qualifying level so 3C accumulates
        # correctly per MA, even though only one message is sent.
        for period, _ in qualifying_levels:
            db.record_touch(ticker, "D", period, today_date)

        primary_period, primary_val = qualifying_levels[0]  # highest period first
        also_touched = [
            {"ma": f"D{period}", "value": val} for period, val in qualifying_levels[1:]
        ]

        touch_count = len(db.get_recent_touches(ticker, "D", primary_period, TOUCH_WINDOW_DAYS))
        next_res = _next_resistance(
            ticker, cascade_step, "D", primary_period,
            daily_df, weekly_df, monthly_df, today_close,
        )

        daily_extra: dict = {
            "touch_count": touch_count,
            "next_resistance_ma": next_res[0] if next_res else None,
            "next_resistance_value": next_res[1] if next_res else None,
            "bar_low": today_low,
            "also_touched": also_touched,
        }

        daily_alert = build_alert(
            ticker=ticker,
            signal_type="MA_SUPPORT",
            timeframe="D",
            ma_period=primary_period,
            price=today_close,
            ma_value=primary_val,
            extra=daily_extra,
            volume_ratio=vol_ratio,
            bar_date=today_date,
        )
        alerts.append(daily_alert)
        logger.info(
            "%s fired for %s D%d @ %.4f (also touched: %s)",
            label_for("3a"), ticker, primary_period, today_close,
            ", ".join(f"D{p}" for p, _ in qualifying_levels[1:]) or "none",
        )

    # --- Weekly / Monthly: unchanged proximity-winner behavior --------------
    checks = [(tf, period) for tf, period in CASCADE_CHECKS.get(cascade_step, []) if tf != "D"]

    # Group checks by timeframe
    by_tf: dict[str, list[int]] = {}
    for tf, period in checks:
        by_tf.setdefault(tf, []).append(period)

    for tf, periods in by_tf.items():
        df = _get_timeframe_df(tf, daily_df, weekly_df, monthly_df)
        if df.empty:
            continue

        # Restrict to completed (closed) bars only — an in-progress week/month
        # must not be evaluated as a confirmed signal.
        eval_df = df[df.index.date < today_date]
        if eval_df.empty:
            logger.debug("3A: no completed %s bars for %s", tf, ticker)
            continue

        winner_period = _get_proximity_winner(tf, periods, eval_df, today_close)
        if winner_period is None:
            continue

        ma_val_raw = eval_df[f"ma_{winner_period}"].iloc[-1]
        if not _is_valid(ma_val_raw):
            continue
        ma_val = float(ma_val_raw)

        # Bar to evaluate for wick/close conditions: last completed bar
        # (strictly before today).
        bar = _latest_completed_bar(df, today_date)
        if bar is None:
            continue
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        touch_date = bar.name.date()
        # Volume confirmation uses today's DAILY bar and daily median. The
        # completed W/M bar defines the wick/close; today's session volume
        # confirms that price is actively finding support at the level right now.
        bar_volume = float(today_bar["volume"])

        # W/M bar gate: suppress if this completed bar already fired a signal.
        # Prevents the detector re-firing every daily scan while conditions hold.
        last_bar = db.get_last_fired_bar_date(ticker, tf, winner_period, "MA_SUPPORT")
        if last_bar is not None and touch_date <= last_bar:
            logger.debug(
                "3A W/M gate: skip %s %s%d — bar %s already fired (last_bar=%s)",
                ticker, tf, winner_period, touch_date, last_bar,
            )
            continue

        # Wick condition: bar low ≤ MA value
        wick_touched = bar_low <= ma_val
        # Close condition: bar close > MA value
        closed_above = bar_close > ma_val
        # Volume condition: always use daily_df median (today's session confirms the touch).
        median_vol_wm = _compute_median_volume(daily_df, today_date, VOLUME_LOOKBACK_DAYS)
        vol_ratio = bar_volume / median_vol_wm if median_vol_wm else 0.0
        volume_ok = median_vol_wm is not None and vol_ratio >= MOMENTUM_VOLUME_MULTIPLIER

        if not (wick_touched and closed_above and volume_ok):
            logger.debug(
                "3A not met for %s %s%d: wick=%s close=%s vol=%s",
                ticker, tf, winner_period, wick_touched, closed_above, volume_ok,
            )
            continue

        # All conditions met — record touch (using bar's own date) and emit alert
        db.record_touch(ticker, tf, winner_period, touch_date)

        touch_count = len(db.get_recent_touches(ticker, tf, winner_period, TOUCH_WINDOW_DAYS))

        next_res = _next_resistance(
            ticker, cascade_step, tf, winner_period,
            daily_df, weekly_df, monthly_df, today_close,
        )

        extra: dict = {
            "touch_count": touch_count,
            "next_resistance_ma": next_res[0] if next_res else None,
            "next_resistance_value": next_res[1] if next_res else None,
            "bar_low": bar_low,
        }

        alert = build_alert(
            ticker=ticker,
            signal_type="MA_SUPPORT",
            timeframe=tf,
            ma_period=winner_period,
            price=today_close,
            ma_value=ma_val,
            extra=extra,
            volume_ratio=vol_ratio,
            bar_date=touch_date,
        )
        alerts.append(alert)
        logger.info("%s fired for %s %s%d @ %.4f", label_for("3a"), ticker, tf, winner_period, today_close)

    return alerts


# ---------------------------------------------------------------------------
# Signal 3B — MA Reclaim
# ---------------------------------------------------------------------------

def _detect_3b_for_timeframe(
    ticker: str,
    df: pd.DataFrame,
    timeframe: str,
    ma_period: int,
    cascade_state: dict,
    required_streak: int,
) -> dict | None:
    """
    Core reclaim logic for a single (timeframe, ma_period) combination.

    Break-gated, single-fire semantics:
      - A close at or below the MA is always a "break": streak resets to 0, and
        for W/M timeframes was_broken is set to 1 (arms the reclaim gate).
      - For W/M, a close above the MA only increments the streak if was_broken
        is already 1 — i.e. price must have actually lost this MA as support
        at some point since the last reset before a reclaim can be counted.
        Without a prior break there is nothing to "reclaim", so the close is a
        no-op (no increment, no fire).
      - For Daily, a close above the MA always increments (Daily's required
        streak is preceded by cascade_state.broken_ma already being set, which
        establishes the break precondition upstream).
      - Fire is exact (new_streak == required_streak), not >=, so a row that
        drifts past the threshold cannot re-fire.
      - Immediately after building the alert, the tracker row is fully reset
        (streak=0, streak_start=None, was_broken=0) so the next close starts a
        fresh streak instead of continuing to climb past required_streak and
        instant-refiring.

    For W/M timeframes, each completed bar is counted at most once (guarded by
    last_bar_date in reclaim_tracker). For Daily, every calendar day of scan is
    a new bar so the guard is not needed but is harmless.

    Fix 4 (Bug 4): streak counting uses closes only — intraday wicks touching
    or piercing the MA never reset the streak (see the `bar_close > ma_val`
    branch below; `bar_low`/wick values are never read here).

    Returns an alert dict when the streak reaches required_streak, else None.
    """
    from src.config import MA_PERIODS
    from src.ma import compute_ma as _compute_ma

    if df is None or df.empty:
        return None

    _compute_ma(df, MA_PERIODS)

    if timeframe == "D":
        # Daily: evaluate the latest stored daily bar directly. Using
        # _latest_completed_bar here (which excludes "today" if the scan
        # runs intraday) introduces a one-day lag for Daily reclaim streaks —
        # the 7th qualifying close wouldn't be counted until the NEXT day's
        # scan. Daily bars in the DB are always end-of-day closes (fetched
        # once per scan), so df.iloc[-1] is always a completed bar; no
        # "still forming" risk the way there is for W/M.
        bar = df.iloc[-1]
    else:
        bar = _latest_completed_bar(df, date.today())
    if bar is None:
        return None

    bar_date_val: date = bar.name.date()
    ma_col = f"ma_{ma_period}"
    if ma_col not in bar.index or math.isnan(bar[ma_col]):
        logger.debug("MA%d not available for 3B %s on %s", ma_period, timeframe, ticker)
        return None

    bar_close = float(bar["close"])
    ma_val = float(bar[ma_col])

    tracker = db.get_reclaim_streak_full(ticker, timeframe, ma_period)
    current_streak: int = tracker["consecutive_closes"]
    streak_start_raw = tracker["streak_start"]
    last_bar_raw = tracker.get("last_bar_date")
    was_broken: int = tracker.get("was_broken") or 0

    streak_start: date | None = (
        date.fromisoformat(streak_start_raw) if streak_start_raw else None
    )
    last_bar_counted: date | None = (
        date.fromisoformat(last_bar_raw) if last_bar_raw else None
    )

    if bar_close > ma_val:
        # Guard: do not count the same completed bar twice (critical for W/M scanned daily).
        # Only applies to increments — resets are always allowed so a bar that fires on
        # day N and closes below MA later that same day (or in test re-runs) still resets.
        if last_bar_counted is not None and bar_date_val <= last_bar_counted:
            logger.debug(
                "3B %s %s%d: bar %s already counted, skipping",
                ticker, timeframe, ma_period, bar_date_val,
            )
            return None

        if timeframe in ("W", "M") and not was_broken:
            # No prior break since the last reset — nothing to reclaim. No-op:
            # do not increment, do not fire. Still record last_bar_date so this
            # bar isn't re-evaluated on a later scan.
            logger.debug(
                "3B %s %s%d: close above MA but no prior break — no-op",
                ticker, timeframe, ma_period,
            )
            db.set_reclaim_streak(
                ticker, timeframe, ma_period, current_streak, streak_start,
                last_bar_date=bar_date_val, was_broken=was_broken,
            )
            return None

        new_streak = current_streak + 1
        if new_streak == 1:
            streak_start = bar_date_val

        if new_streak == required_streak:
            current_step = cascade_state.get("current_step", 1)
            break_date_raw = tracker.get("last_break_date")
            break_date_iso = break_date_raw if break_date_raw else None
            first_reclaim_date_iso = streak_start.isoformat() if streak_start else None

            # Fix 5 (Bug 5): live trend-status string, computed only for Daily
            # (the only timeframe cascade steps/STEP_LABELS describe). Built
            # from this bar's own MA_PERIODS columns — the same live data the
            # runner uses for today_daily_mas — so it can never contradict the
            # numbers in this same alert. "new_step" below is the OLD fixed
            # N-1 de-escalation rule; for Daily, runner._process_ticker()
            # overwrites it with the Option-B-derived step (from
            # determine_step_from_close) before dispatch. Left as-is here for
            # W/M (which never de-escalates and never renders a state_line).
            trend_status_str = None
            if timeframe == "D":
                from src.labels import trend_status as _trend_status
                daily_mas = {
                    p: (None if math.isnan(bar[f"ma_{p}"]) else float(bar[f"ma_{p}"]))
                    for p in MA_PERIODS
                    if f"ma_{p}" in bar.index
                }
                trend_status_str = _trend_status(bar_close, daily_mas)

            alert = build_alert(
                ticker=ticker,
                signal_type="RECLAIM",
                timeframe=timeframe,
                ma_period=ma_period,
                price=bar_close,
                ma_value=ma_val,
                extra={
                    "streak": new_streak,
                    "previous_step": current_step,
                    "new_step": max(1, current_step - 1),
                    "break_date": break_date_iso,
                    "first_reclaim_date": first_reclaim_date_iso,
                    "trend_status": trend_status_str,
                },
                volume_ratio=None,
                bar_date=bar_date_val,
            )
            logger.info(
                "%s confirmed for %s %s%d — streak=%d",
                label_for("3b"), ticker, timeframe, ma_period, new_streak,
            )
            # Post-fire reset: prevents the row from continuing to climb past
            # required_streak and instant-refiring on the next qualifying close.
            db.set_reclaim_streak(
                ticker, timeframe, ma_period, 0, None,
                last_bar_date=bar_date_val, was_broken=0,
            )
            return alert

        db.set_reclaim_streak(
            ticker, timeframe, ma_period, new_streak, streak_start,
            last_bar_date=bar_date_val, was_broken=was_broken,
        )
    else:
        # Close at or below MA — reset streak. For W/M, this is the break that
        # arms the reclaim gate (was_broken=1) for the next above-MA close.
        # Also records this bar's date as the most recent break, for display
        # transparency (B1/B4) — this is never cleared by the post-fire reset.
        if current_streak > 0:
            logger.debug("3B streak reset for %s %s%d", ticker, timeframe, ma_period)
        new_was_broken = 1 if timeframe in ("W", "M") else 0
        db.set_reclaim_streak(
            ticker, timeframe, ma_period, 0, None,
            last_bar_date=bar_date_val, was_broken=new_was_broken,
            last_break_date=bar_date_val,
        )

    return None


def detect_3b_reclaim(
    ticker: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame | None = None,
    monthly_df: pd.DataFrame | None = None,
    cascade_state: dict | None = None,
    cascade_step: int | None = None,
) -> list[dict] | dict | None:
    """
    Detect MA Reclaim signals (3B) across Daily, Weekly, and Monthly timeframes.

    Daily: tracks 7 consecutive daily closes above the broken Daily MA.
    Weekly: tracks 2 consecutive completed weekly closes above *any* broken W MA
            at the current cascade step.
    Monthly: tracks 2 consecutive completed monthly closes above *any* broken M MA
             at the current cascade step.

    Only Daily reclaim triggers a cascade de-escalation (caller's responsibility).

    Backward-compatible: old 3-arg call (ticker, daily_df, cascade_state) still works —
    returns dict | None (old shape). Full 6-arg call returns list[dict] (new shape).

    Args:
        ticker:        Ticker symbol.
        daily_df:      Full daily OHLCV DataFrame (pre-indexed by date).
        weekly_df:     Resampled weekly OHLCV DataFrame. None → Daily-only compat mode.
        monthly_df:    Resampled monthly OHLCV DataFrame. None → Daily-only compat mode.
        cascade_state: Dict from db.get_cascade_state(). In compat mode, passed as 3rd arg.
        cascade_step:  Current cascade step (1–5). None → Daily-only compat mode.

    Returns:
        list[dict] in full mode; dict | None in compat (3-arg) mode.
    """
    # --- Backward-compat detection ---
    # Old call: detect_3b_reclaim(ticker, daily_df, cascade_state_dict)
    # In that case weekly_df receives the cascade_state dict.
    _compat_mode = isinstance(weekly_df, dict) or weekly_df is None and cascade_step is None
    if isinstance(weekly_df, dict):
        # 3-arg old-style call: weekly_df slot holds cascade_state
        cascade_state = weekly_df
        weekly_df = None
        monthly_df = None
        cascade_step = None

    if cascade_state is None:
        cascade_state = {}

    alerts: list[dict] = []

    # --- Daily reclaim (broken_ma from cascade_state) ---
    broken_ma_str: str = cascade_state.get("broken_ma", "NONE")
    if broken_ma_str != "NONE":
        try:
            d_ma_period = int(broken_ma_str.lstrip("D"))
        except ValueError:
            logger.error("Cannot parse broken_ma: %r for %s", broken_ma_str, ticker)
            d_ma_period = None

        if d_ma_period is not None:
            alert = _detect_3b_for_timeframe(
                ticker, daily_df, "D", d_ma_period, cascade_state, RECLAIM_STREAK_DAYS,
            )
            if alert:
                alerts.append(alert)

    # --- Weekly and Monthly reclaim (MAs at current cascade step) ---
    # Skipped in compat mode (cascade_step is None)
    if cascade_step is not None:
        step_checks = CASCADE_CHECKS.get(cascade_step, [])
        for tf, period in step_checks:
            if tf == "W" and weekly_df is not None:
                alert = _detect_3b_for_timeframe(
                    ticker, weekly_df, "W", period, cascade_state, RECLAIM_STREAK_DAYS_WEEKLY,
                )
                if alert:
                    alerts.append(alert)
            elif tf == "M" and monthly_df is not None:
                alert = _detect_3b_for_timeframe(
                    ticker, monthly_df, "M", period, cascade_state, RECLAIM_STREAK_DAYS_MONTHLY,
                )
                if alert:
                    alerts.append(alert)

    # --- Return shape ---
    # Compat mode (3-arg old callers): return dict | None
    if _compat_mode:
        return alerts[0] if alerts else None

    return alerts


# ---------------------------------------------------------------------------
# Signal 3C — Touch Accumulation
# ---------------------------------------------------------------------------

def detect_3c_touch_accumulation(
    ticker: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    cascade_step: int,
) -> list[dict]:
    """
    Detect Touch Accumulation signals (3C).

    Reads the touch_log (already updated by detect_3a_ma_support for today) and
    checks whether any MA has accumulated 3+ touches in the rolling 21-calendar-day
    window.

    Does NOT re-record touches — that is handled by 3A. This function only queries.

    Returns list of alert dicts (may be multiple if multiple MAs qualify).
    """
    if daily_df.empty:
        return []

    from src.config import MA_PERIODS
    from src.ma import compute_ma as _compute_ma
    for df_obj in [daily_df, weekly_df, monthly_df]:
        if not df_obj.empty:
            _compute_ma(df_obj, MA_PERIODS)

    today_close = float(daily_df.iloc[-1]["close"])
    today_date: date = daily_df.index[-1].date()

    alerts: list[dict] = []

    # --- Daily: all-levels independent evaluation (Fix 3, mirrors 3A) -------
    # Each of D50/D100/D150/D200 (MA_PERIODS) is checked independently rather
    # than restricting to the single cascade-step proximity winner, so touch
    # accumulation on a level that isn't this step's "primary" MA is still
    # detected (3A already records a touch_log row per qualifying level).
    _n = min(15, len(daily_df))
    trading_cutoff = daily_df.index[-_n].date()
    for period in sorted(MA_PERIODS, reverse=True):
        col = f"ma_{period}"
        if col not in daily_df.columns:
            continue
        ma_val_raw = daily_df[col].iloc[-1]
        if not _is_valid(ma_val_raw):
            continue
        ma_val = float(ma_val_raw)

        recent_touches = db.get_touches_since(ticker, "D", period, trading_cutoff)
        count = len(recent_touches)
        if count >= TOUCH_THRESHOLD:
            touch_date_strs = [d.isoformat() for d in sorted(recent_touches)[-TOUCH_THRESHOLD:]]
            alert = build_alert(
                ticker=ticker,
                signal_type="TOUCH_ACCUMULATION",
                timeframe="D",
                ma_period=period,
                price=today_close,
                ma_value=ma_val,
                extra={
                    "touch_count": count,
                    "touch_dates": touch_date_strs,
                },
                volume_ratio=None,
                bar_date=today_date,
            )
            alerts.append(alert)
            logger.info(
                "%s fired for %s D%d — %d touches in window", label_for("3c"), ticker, period, count
            )

    # --- Weekly / Monthly: unchanged proximity-winner behavior --------------
    checks = [(tf, period) for tf, period in CASCADE_CHECKS.get(cascade_step, []) if tf != "D"]

    # Group by timeframe for proximity rule
    by_tf: dict[str, list[int]] = {}
    for tf, period in checks:
        by_tf.setdefault(tf, []).append(period)

    for tf, periods in by_tf.items():
        df = _get_timeframe_df(tf, daily_df, weekly_df, monthly_df)
        if df.empty:
            continue

        # Restrict to completed bars (same rule as 3A) — this loop is W/M only.
        eval_df = df[df.index.date < today_date]
        if eval_df.empty:
            continue

        winner_period = _get_proximity_winner(tf, periods, eval_df, today_close)
        if winner_period is None:
            continue

        ma_val_raw = eval_df[f"ma_{winner_period}"].iloc[-1]
        if not _is_valid(ma_val_raw):
            continue
        ma_val = float(ma_val_raw)

        # Bar date for this signal: last completed bar.
        current_bar_date = eval_df.index[-1].date()

        # W/M bar gate: suppress if this completed bar already fired a 3C signal.
        last_bar = db.get_last_fired_bar_date(ticker, tf, winner_period, "TOUCH_ACCUMULATION")
        if last_bar is not None and current_bar_date <= last_bar:
            logger.debug(
                "3C W/M gate: skip %s %s%d — bar %s already fired (last_bar=%s)",
                ticker, tf, winner_period, current_bar_date, last_bar,
            )
            continue

        # 15-trading-day window: 15th-from-last row in daily_df is the cutoff.
        # daily_df is indexed by contiguous trading days, so this is exact —
        # no calendar-day approximation, no holiday drift.
        recent_touches = db.get_touches_since(ticker, tf, winner_period, trading_cutoff)
        count = len(recent_touches)

        if count >= TOUCH_THRESHOLD:
            touch_date_strs = [d.isoformat() for d in sorted(recent_touches)[-TOUCH_THRESHOLD:]]
            alert = build_alert(
                ticker=ticker,
                signal_type="TOUCH_ACCUMULATION",
                timeframe=tf,
                ma_period=winner_period,
                price=today_close,
                ma_value=ma_val,
                extra={
                    "touch_count": count,
                    "touch_dates": touch_date_strs,
                },
                volume_ratio=None,
                bar_date=current_bar_date,
            )
            alerts.append(alert)
            logger.info(
                "%s fired for %s %s%d — %d touches in window", label_for("3c"), ticker, tf, winner_period, count
            )

    return alerts


# ---------------------------------------------------------------------------
# Signal 3D — D20 Momentum Touch
# ---------------------------------------------------------------------------

def detect_3d(
    ticker: str,
    daily_df: pd.DataFrame,
    cascade_step: int | None,
) -> dict | None:
    """
    Detect D20 Momentum Touch signal (3D).

    PRECONDITIONS (all must hold; else return None):
      - cascade_step is not None
      - cascade_step == 1
      - D20, D50, D100, D150, D200 all non-NaN on latest bar
      - Strict stack: D20 > D50 > D100 > D150 > D200  (strict >, not >=)

    TRIGGER (all must hold on today's bar):
      - today.low  <= D20_today
      - today.close > D20_today
      - today.volume >= MOMENTUM_VOLUME_MULTIPLIER * volume_avg_20d

    volume_avg_20d = daily_df['volume'].iloc[-21:-1].mean()
      → 20 bars ending yesterday (excludes today). Assumes oldest-first ordering.

    NOTE: MAs must be pre-computed by the caller (runner uses MA_PERIODS_DAILY).
          This function does NOT call compute_ma internally.
    """
    # FIX-3: cascade guard — first line
    if cascade_step is None:
        return None

    if cascade_step != 1:
        return None

    if daily_df.empty:
        return None

    # Need at least 21 bars (20 for volume avg + today)
    if len(daily_df) < 21:
        return None

    # Check all required MA columns are present and non-NaN on the latest bar
    for period in (20, 50, 100, 150, 200):
        col = f"ma_{period}"
        if col not in daily_df.columns:
            logger.debug("3D: ma_%d column missing for %s — MAs not pre-computed", period, ticker)
            return None
        if not _is_valid(daily_df[col].iloc[-1]):
            logger.debug("3D: ma_%d is NaN for %s", period, ticker)
            return None

    d20  = float(daily_df["ma_20"].iloc[-1])
    d50  = float(daily_df["ma_50"].iloc[-1])
    d100 = float(daily_df["ma_100"].iloc[-1])
    d150 = float(daily_df["ma_150"].iloc[-1])
    d200 = float(daily_df["ma_200"].iloc[-1])

    # Strict stack check: D20 > D50 > D100 > D150 > D200
    if not (d20 > d50 > d100 > d150 > d200):
        logger.debug(
            "3D: MA stack not met for %s — D20=%.2f D50=%.2f D100=%.2f D150=%.2f D200=%.2f",
            ticker, d20, d50, d100, d150, d200,
        )
        return None

    today_bar     = daily_df.iloc[-1]
    today_low     = float(today_bar["low"])
    today_close   = float(today_bar["close"])
    today_vol     = float(today_bar["volume"])
    today_date_3d = daily_df.index[-1].date()

    # Volume median: last 20 completed daily bars (strictly before today).
    vol_median_3d = _compute_median_volume(daily_df, today_date_3d, VOLUME_LOOKBACK_DAYS)
    if vol_median_3d is None:
        logger.debug("3D: insufficient history for volume median on %s", ticker)
        return None

    # Trigger conditions
    wick_touched = today_low <= d20
    closed_above = today_close > d20
    volume_ok    = today_vol >= MOMENTUM_VOLUME_MULTIPLIER * vol_median_3d

    if not (wick_touched and closed_above and volume_ok):
        logger.debug(
            "3D not triggered for %s: wick=%s close=%s vol=%s (%.2f× median)",
            ticker, wick_touched, closed_above, volume_ok,
            today_vol / vol_median_3d if vol_median_3d > 0 else 0.0,
        )
        return None

    volume_ratio = round(today_vol / vol_median_3d, 2)
    alert = build_alert(
        ticker=ticker,
        signal_type="3D",
        timeframe="D",
        ma_period=20,
        price=today_close,
        ma_value=d20,
        extra={"low": today_low, "close": today_close},
        volume_ratio=volume_ratio,
        bar_date=today_date_3d,
    )
    logger.info(
        "3D fired for %s @ %.4f — vol %.2f× avg, D20=%.2f>D50=%.2f>D100=%.2f>D150=%.2f>D200=%.2f",
        ticker, today_close, volume_ratio, d20, d50, d100, d150, d200,
    )
    return alert
