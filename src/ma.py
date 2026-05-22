"""
ma.py — Simple Moving Average (SMA) computation.

Uses SMA on the 'close' column. EMA is explicitly NOT used — the spec requires SMA.

If a DataFrame has fewer rows than a given MA period, those MA values are NaN.
This is expected (not an error) — callers must handle NaN via latest_ma_value() returning None.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def compute_ma(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    """
    Add SMA columns to a DataFrame.

    Adds columns named 'ma_50', 'ma_100', 'ma_150', 'ma_200' (or whatever periods
    are provided). Uses the 'close' column as input.

    Args:
        df:      DataFrame with a 'close' column. Modified in-place and returned.
        periods: List of SMA periods to compute.

    Returns:
        The same DataFrame with MA columns added.
    """
    if df.empty or "close" not in df.columns:
        for p in periods:
            df[f"ma_{p}"] = float("nan")
        return df

    for period in periods:
        col = f"ma_{period}"
        df[col] = df["close"].rolling(window=period, min_periods=period).mean()

    return df


def latest_ma_value(df: pd.DataFrame, period: int) -> float | None:
    """
    Return the most recent SMA value for a given period.

    Args:
        df:     DataFrame that has been passed through compute_ma().
        period: The MA period to look up.

    Returns:
        Float value, or None if the column is missing or the most recent value is NaN.
    """
    col = f"ma_{period}"
    if col not in df.columns or df.empty:
        return None
    val = df[col].iloc[-1]
    if pd.isna(val):
        return None
    return float(val)
