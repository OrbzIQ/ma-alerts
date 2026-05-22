"""
bootstrap.py — Initialise state for a new ticker.

Bootstrap process:
  1. Add ticker to watchlist (no-op if already present)
  2. Fetch maximum available OHLCV history from Twelve Data (outputsize=5000)
  3. Persist all OHLCV to DB
  4. Replay cascade state forward, day by day — NO alerts fired
  5. Persist final cascade state
  6. Populate touch_log for the last TOUCH_WINDOW_DAYS calendar days
  7. Populate reclaim_tracker if stock is currently mid-streak

The bootstrap is idempotent: calling it on an existing ticker refreshes history
but does not alter cascade state if the ticker already has a stored state.

Monthly MA periods (50, 100, 150, 200) require 50–200 months of history.
If insufficient monthly bars exist, those MAs will be NaN — expected, not an error.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

import src.db as db
from src.cascade import determine_step_from_close
from src.config import (
    BOOTSTRAP_DAYS,
    BOOTSTRAP_MAX_CANDLES,
    CASCADE_CHECKS,
    MA_PERIODS,
    TOUCH_WINDOW_DAYS,
)
from src.fetcher import fetch_daily_ohlcv
from src.ma import compute_ma
from src.resample import resample_to_monthly, resample_to_weekly

logger = logging.getLogger(__name__)


def _daily_mas_from_df(daily_df: pd.DataFrame, as_of_idx: int) -> dict[int, float | None]:
    """
    Extract the Daily MA values (periods 50/100/150/200) at a specific row index.
    Returns {period: value_or_None}.
    """
    mas: dict[int, float | None] = {}
    for p in MA_PERIODS:
        col = f"ma_{p}"
        if col in daily_df.columns:
            val = daily_df[col].iloc[as_of_idx]
            mas[p] = None if pd.isna(val) else float(val)
        else:
            mas[p] = None
    return mas


def _get_timeframe_ma_at(
    tf: str,
    period: int,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    day_date: date,
) -> float | None:
    """
    Get the MA value for a given timeframe and period as of a specific day.
    Looks up the most recent bar on or before day_date.
    """
    if tf == "D":
        df = daily_df
    elif tf == "W":
        df = weekly_df
    elif tf == "M":
        df = monthly_df
    else:
        return None

    if df.empty:
        return None

    col = f"ma_{period}"
    if col not in df.columns:
        return None

    # Get rows on or before day_date
    mask = df.index.date <= day_date
    if not mask.any():
        return None

    val = df.loc[mask, col].iloc[-1]
    return None if pd.isna(val) else float(val)


def bootstrap_ticker(ticker: str, market: str) -> bool:
    """
    Initialise or refresh a ticker's state.

    Args:
        ticker: Ticker symbol (e.g. 'GOOG').
        market: 'US' or 'SG'.

    Returns:
        True on success, False on failure.
    """
    logger.info("Bootstrapping %s (%s)", ticker, market)

    # Step 1: Add to watchlist
    is_new = db.add_watchlist_ticker(ticker, market)
    if not is_new:
        logger.info("%s already in watchlist — refreshing OHLCV", ticker)

    # Step 2: Fetch maximum available history
    try:
        candles = fetch_daily_ohlcv(ticker, market, outputsize=BOOTSTRAP_MAX_CANDLES)
    except Exception as exc:
        logger.error("Bootstrap fetch failed for %s: %s", ticker, exc)
        return False

    if not candles:
        logger.error("No candles returned for %s — cannot bootstrap", ticker)
        return False

    if len(candles) < BOOTSTRAP_DAYS:
        logger.warning(
            "Only %d candles returned for %s (minimum is %d) — proceeding with limited history",
            len(candles),
            ticker,
            BOOTSTRAP_DAYS,
        )

    # Step 3: Persist OHLCV
    try:
        db.upsert_ohlcv(ticker, candles)
    except Exception as exc:
        logger.error("OHLCV upsert failed for %s: %s", ticker, exc)
        return False

    # Build full DataFrame with MAs
    daily_df = db.get_all_ohlcv(ticker)
    if daily_df.empty:
        logger.error("No OHLCV in DB after upsert for %s", ticker)
        return False

    compute_ma(daily_df, MA_PERIODS)

    weekly_df = resample_to_weekly(daily_df)
    compute_ma(weekly_df, MA_PERIODS)

    monthly_df = resample_to_monthly(daily_df)
    compute_ma(monthly_df, MA_PERIODS)

    # Step 4: Replay cascade forward, day by day (silent — no alerts)
    final_step = 1
    final_broken_ma = "NONE"

    for idx in range(len(daily_df)):
        day_date = daily_df.index[idx].date()
        close = float(daily_df["close"].iloc[idx])
        daily_mas = _daily_mas_from_df(daily_df, idx)
        step, broken_ma = determine_step_from_close(close, daily_mas)
        final_step = step
        final_broken_ma = broken_ma

    # Step 5: Persist final cascade state
    db.set_cascade_state(ticker, final_step, final_broken_ma)
    logger.info(
        "Bootstrap cascade state for %s: step=%d broken_ma=%s",
        ticker,
        final_step,
        final_broken_ma,
    )

    # Step 6: Populate touch_log for last TOUCH_WINDOW_DAYS calendar days
    window_cutoff = date.today() - timedelta(days=TOUCH_WINDOW_DAYS)
    recent_mask = daily_df.index.date >= window_cutoff
    recent_daily = daily_df[recent_mask]

    for idx in range(len(recent_daily)):
        day_date = recent_daily.index[idx].date()
        bar_close = float(recent_daily["close"].iloc[idx])
        bar_low = float(recent_daily["low"].iloc[idx])

        checks = CASCADE_CHECKS.get(final_step, [])
        by_tf: dict[str, list[int]] = {}
        for tf, period in checks:
            by_tf.setdefault(tf, []).append(period)

        for tf, periods in by_tf.items():
            if tf == "D":
                df_ref = recent_daily
            elif tf == "W":
                df_ref = weekly_df
            else:
                df_ref = monthly_df

            for period in sorted(periods, reverse=True):
                col = f"ma_{period}"
                ma_val = _get_timeframe_ma_at(tf, period, daily_df, weekly_df, monthly_df, day_date)
                if ma_val is None:
                    continue
                # Qualifying touch: low ≤ MA AND close > MA
                if bar_low <= ma_val and bar_close > ma_val:
                    db.record_touch(ticker, tf, period, day_date)
                    break  # Only record one touch per timeframe per day (proximity winner)

    # Step 7: Populate reclaim_tracker if currently mid-streak
    # Only relevant if there's a broken MA to reclaim
    if final_broken_ma != "NONE":
        try:
            ma_period = int(final_broken_ma.lstrip("D"))
        except ValueError:
            ma_period = None

        if ma_period is not None:
            ma_col = f"ma_{ma_period}"
            if ma_col in daily_df.columns:
                streak = 0
                streak_start: date | None = None

                # Scan backward from the end to find the current streak
                for idx in range(len(daily_df) - 1, -1, -1):
                    row_close = float(daily_df["close"].iloc[idx])
                    ma_val_raw = daily_df[ma_col].iloc[idx]
                    if pd.isna(ma_val_raw):
                        break
                    ma_val = float(ma_val_raw)

                    if row_close > ma_val:
                        streak += 1
                        streak_start = daily_df.index[idx].date()
                    else:
                        break  # streak broken

                if streak > 0:
                    db.set_reclaim_streak(ticker, ma_period, streak, streak_start)
                    logger.info(
                        "Bootstrap reclaim streak for %s D%d: %d days (started %s)",
                        ticker,
                        ma_period,
                        streak,
                        streak_start,
                    )

    logger.info("Bootstrap complete for %s", ticker)
    return True
