"""
resample.py — Resample Daily OHLCV into Weekly and Monthly bars using pandas.

Resampling conventions:
  - Weekly:  week-ending Friday (W-FRI anchor). Partial weeks produce a bar.
  - Monthly: month-end bars (ME anchor). pandas >= 2.2 uses 'ME'; older uses 'M'.

Input DataFrame must be indexed by DatetimeIndex with columns: open, high, low, close, volume.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

# Aggregation rules for OHLCV resampling
_OHLCV_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def _ensure_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    """Convert index to DatetimeIndex if it isn't already."""
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index)
    return df


def resample_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample a Daily OHLCV DataFrame to weekly bars.

    Output is indexed by week-ending Friday. Partial weeks (e.g. holiday-shortened)
    still produce a bar using whatever daily bars are available that week.

    Args:
        daily_df: DataFrame indexed by date with columns open/high/low/close/volume.

    Returns:
        DataFrame indexed by week-ending date (Friday) with OHLCV aggregated.
        Returns empty DataFrame if input is empty.
    """
    if daily_df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = _ensure_datetime_index(daily_df)

    try:
        weekly = df.resample("W-FRI").agg(_OHLCV_AGG).dropna(how="all")
    except Exception as exc:
        logger.error("Weekly resample failed: %s", exc)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    return weekly


def resample_to_monthly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample a Daily OHLCV DataFrame to monthly bars.

    Output is indexed by month-end date.

    Args:
        daily_df: DataFrame indexed by date with columns open/high/low/close/volume.

    Returns:
        DataFrame indexed by month-end date with OHLCV aggregated.
        Returns empty DataFrame if input is empty.
    """
    if daily_df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = _ensure_datetime_index(daily_df)

    # pandas >= 2.2 deprecates 'M' in favour of 'ME' (month-end)
    try:
        monthly = df.resample("ME").agg(_OHLCV_AGG).dropna(how="all")
    except ValueError:
        # Fallback for older pandas versions
        monthly = df.resample("M").agg(_OHLCV_AGG).dropna(how="all")

    return monthly
