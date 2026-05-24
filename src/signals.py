"""
signals.py — Three signal detectors: 3A (MA Support), 3B (Reclaim), 3C (Touch Accumulation).

Key invariants:
  - detect_3a_ma_support  MUST run before detect_3c_touch_accumulation so today's
    touch is in the DB when 3C queries it.
  - detect_3b_reclaim     MUST be skipped on days when a forward cascade transition fires.
  - Proximity rule is applied INDEPENDENTLY per timeframe (D/W/M), not globally.
    Within each timeframe group at the current cascade step, find the highest-period
    MA whose value is still below current price and use only that one MA.

Volume comparator for 3A is ALWAYS 20-day daily volume, regardless of signal timeframe.
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


def _compute_avg_daily_volume(daily_df: pd.DataFrame) -> float | None:
    """Return the 20-day average daily volume (excludes today). Always uses daily bars.

    Uses iloc[-21:-1] — 20 bars ending yesterday — as the canonical slice (§A.1).
    Requires at least VOLUME_LOOKBACK_DAYS + 1 rows (today + 20 history bars).
    """
    if daily_df.empty or len(daily_df) < VOLUME_LOOKBACK_DAYS + 1:
        return None
    return float(daily_df["volume"].iloc[-(VOLUME_LOOKBACK_DAYS + 1):-1].mean())


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

    For each timeframe group at this step:
      1. Apply proximity rule (per-timeframe): find the highest-period MA that price
         is currently above.
      2. Check today's bar: wick touched MA (low ≤ MA), closed above MA (close > MA),
         and volume ≥ 1.5× 20-day daily average.
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

    avg_volume = _compute_avg_daily_volume(daily_df)

    checks = CASCADE_CHECKS.get(cascade_step, [])

    # Group checks by timeframe
    by_tf: dict[str, list[int]] = {}
    for tf, period in checks:
        by_tf.setdefault(tf, []).append(period)

    for tf, periods in by_tf.items():
        df = _get_timeframe_df(tf, daily_df, weekly_df, monthly_df)
        if df.empty:
            continue

        # For W/M, restrict to completed (closed) bars only.
        # An in-progress week/month must not be evaluated as a confirmed signal.
        if tf != "D":
            eval_df = df[df.index.date < today_date]
            if eval_df.empty:
                logger.debug("3A: no completed %s bars for %s", tf, ticker)
                continue
        else:
            eval_df = df

        winner_period = _get_proximity_winner(tf, periods, eval_df, today_close)
        if winner_period is None:
            continue

        ma_val_raw = eval_df[f"ma_{winner_period}"].iloc[-1]
        if not _is_valid(ma_val_raw):
            continue
        ma_val = float(ma_val_raw)

        # Bar to evaluate for wick/close conditions:
        # D  → today's daily bar.
        # W/M → last completed bar (strictly before today).
        if tf == "D":
            bar_low = today_low
            bar_close = today_close
            touch_date = today_date
        else:  # W or M
            bar = _latest_completed_bar(df, today_date)
            if bar is None:
                continue
            bar_low = float(bar["low"])
            bar_close = float(bar["close"])
            touch_date = bar.name.date()

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
        # Volume condition: always daily volume vs 20-day daily avg
        if avg_volume is None:
            volume_ok = False
        else:
            today_volume = float(today_bar["volume"])
            vol_ratio = today_volume / avg_volume if avg_volume > 0 else 0.0
            volume_ok = vol_ratio >= MOMENTUM_VOLUME_MULTIPLIER

        if not (wick_touched and closed_above and volume_ok):
            logger.debug(
                "3A not met for %s %s%d: wick=%s close=%s vol=%s",
                ticker, tf, winner_period, wick_touched, closed_above, volume_ok,
            )
            continue

        # All conditions met — record touch (using bar's own date for W/M) and emit alert
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

def detect_3b_reclaim(
    ticker: str,
    daily_df: pd.DataFrame,
    cascade_state: dict,
) -> dict | None:
    """
    Detect MA Reclaim signal (3B).

    Tracks consecutive daily closes above the broken Daily MA. Fires on day 7.
    Any close below resets the streak to 0.

    Args:
        ticker:        Ticker symbol.
        daily_df:      Full daily OHLCV DataFrame.
        cascade_state: Dict from db.get_cascade_state().

    Returns:
        Alert dict on confirmation (streak = 7), or None.
    """
    broken_ma_str: str = cascade_state.get("broken_ma", "NONE")
    if broken_ma_str == "NONE":
        return None

    # Parse MA period from string, e.g. 'D100' → 100
    try:
        ma_period = int(broken_ma_str.lstrip("D"))
    except ValueError:
        logger.error("Cannot parse broken_ma: %r for %s", broken_ma_str, ticker)
        return None

    if daily_df.empty:
        return None

    from src.config import MA_PERIODS
    from src.ma import compute_ma as _compute_ma
    _compute_ma(daily_df, MA_PERIODS)

    ma_val = latest_ma_value(daily_df, ma_period)
    if ma_val is None:
        logger.debug("MA%d not available for 3B on %s", ma_period, ticker)
        return None

    today_close = float(daily_df.iloc[-1]["close"])
    today_date = daily_df.index[-1].date()

    tracker = db.get_reclaim_streak_full(ticker, ma_period)
    current_streak: int = tracker["consecutive_closes"]
    streak_start_raw = tracker["streak_start"]
    streak_start: date | None = (
        date.fromisoformat(streak_start_raw) if streak_start_raw else None
    )

    if today_close > ma_val:
        # Price closed above broken MA — increment streak
        new_streak = current_streak + 1
        if new_streak == 1:
            streak_start = today_date
        db.set_reclaim_streak(ticker, ma_period, new_streak, streak_start)

        if new_streak >= RECLAIM_STREAK_DAYS:
            # Streak confirmed — fire alert
            current_step = cascade_state.get("current_step", 1)
            alert = build_alert(
                ticker=ticker,
                signal_type="RECLAIM",
                timeframe="D",
                ma_period=ma_period,
                price=today_close,
                ma_value=ma_val,
                extra={
                    "streak": new_streak,
                    "previous_step": current_step,
                    "new_step": max(1, current_step - 1),
                },
                volume_ratio=None,
                bar_date=today_date,
            )
            logger.info(
                "%s confirmed for %s D%d — streak=%d", label_for("3b"), ticker, ma_period, new_streak
            )
            return alert
    else:
        # Price closed below broken MA — reset streak
        if current_streak > 0:
            logger.debug("3B streak reset for %s D%d", ticker, ma_period)
        db.set_reclaim_streak(ticker, ma_period, 0, None)

    return None


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

    checks = CASCADE_CHECKS.get(cascade_step, [])

    # Group by timeframe for proximity rule
    by_tf: dict[str, list[int]] = {}
    for tf, period in checks:
        by_tf.setdefault(tf, []).append(period)

    alerts: list[dict] = []

    for tf, periods in by_tf.items():
        df = _get_timeframe_df(tf, daily_df, weekly_df, monthly_df)
        if df.empty:
            continue

        # For W/M, restrict to completed bars (same rule as 3A)
        if tf != "D":
            eval_df = df[df.index.date < today_date]
            if eval_df.empty:
                continue
        else:
            eval_df = df

        winner_period = _get_proximity_winner(tf, periods, eval_df, today_close)
        if winner_period is None:
            continue

        ma_val_raw = eval_df[f"ma_{winner_period}"].iloc[-1]
        if not _is_valid(ma_val_raw):
            continue
        ma_val = float(ma_val_raw)

        # Bar date for this signal: last completed bar for W/M, today for D.
        if tf != "D":
            current_bar_date = eval_df.index[-1].date()
        else:
            current_bar_date = today_date

        # W/M bar gate: suppress if this completed bar already fired a 3C signal.
        if tf in ("W", "M"):
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
        _n = min(15, len(daily_df))
        trading_cutoff = daily_df.index[-_n].date()
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

    today_bar   = daily_df.iloc[-1]
    today_low   = float(today_bar["low"])
    today_close = float(today_bar["close"])
    today_vol   = float(today_bar["volume"])

    # Volume average: canonical 20-bar slice ending yesterday
    volume_avg_20d = float(daily_df["volume"].iloc[-21:-1].mean())

    # Trigger conditions
    wick_touched = today_low <= d20
    closed_above = today_close > d20
    volume_ok    = today_vol >= MOMENTUM_VOLUME_MULTIPLIER * volume_avg_20d

    if not (wick_touched and closed_above and volume_ok):
        logger.debug(
            "3D not triggered for %s: wick=%s close=%s vol=%s (%.2f× avg)",
            ticker, wick_touched, closed_above, volume_ok,
            today_vol / volume_avg_20d if volume_avg_20d > 0 else 0.0,
        )
        return None

    volume_ratio = round(today_vol / volume_avg_20d, 2)

    today_date_3d = daily_df.index[-1].date()
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

