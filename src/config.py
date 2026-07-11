"""
config.py — Single source of truth for all tunable parameters.

Do not import from other src modules here. This module must have zero
dependencies on the rest of the package so it can be imported anywhere safely.
"""

import yaml
from pathlib import Path

# ---------------------------------------------------------------------------
# Moving Average periods
# ---------------------------------------------------------------------------
MA_PERIODS: list[int] = [50, 100, 150, 200]              # Weekly/Monthly (no D20)
MA_PERIODS_DAILY: list[int] = [20, 50, 100, 150, 200]   # Daily — 20 added for V2 3D

# ---------------------------------------------------------------------------
# Signal thresholds
# ---------------------------------------------------------------------------
VOLUME_MULTIPLIER: float = 1.5          # kept for backwards compat
MOMENTUM_VOLUME_MULTIPLIER: float = 1.5 # used by 3A (refactored) and 3D
VOLUME_LOOKBACK_DAYS: int = 20          # Always daily, regardless of signal timeframe
RECLAIM_STREAK_DAYS: int = 7            # 3B-D: consecutive daily closes above broken D MA
RECLAIM_STREAK_DAYS_WEEKLY: int = 2    # 3B-W: consecutive completed weekly closes above broken W MA
RECLAIM_STREAK_DAYS_MONTHLY: int = 2   # 3B-M: consecutive completed monthly closes above broken M MA
TOUCH_THRESHOLD: int = 3               # 3C: qualifying touches required
TOUCH_WINDOW_DAYS: int = 21            # 3C: rolling window in CALENDAR days (~15 trading days)

# ---------------------------------------------------------------------------
# Cascade thresholds — which Daily MA period defines each cascade step
# ---------------------------------------------------------------------------
CASCADE_MA_BY_STEP: dict[int, int] = {
    2: 50,
    3: 100,
    4: 150,
    5: 200,
}

# ---------------------------------------------------------------------------
# MAs evaluated at each cascade step.
# Format: list of (timeframe, period) tuples.
# Timeframes: "D" = Daily, "W" = Weekly, "M" = Monthly.
# ---------------------------------------------------------------------------
CASCADE_CHECKS: dict[int, list[tuple[str, int]]] = {
    1: [("D", 50)],
    2: [("D", 100), ("W", 50), ("W", 100), ("M", 50)],
    3: [("D", 150), ("W", 50), ("W", 100), ("M", 50), ("M", 100)],
    4: [("D", 200), ("W", 100), ("W", 150), ("W", 200), ("M", 100), ("M", 150)],
    5: [("W", 100), ("W", 150), ("W", 200), ("M", 100), ("M", 150), ("M", 200)],
}

# ---------------------------------------------------------------------------
# Data layer constants
# ---------------------------------------------------------------------------
BOOTSTRAP_DAYS: int = 250              # Minimum acceptable history for a new ticker
BOOTSTRAP_MAX_CANDLES: int = 5000      # outputsize passed to Twelve Data — fetch max available
OHLCV_RETENTION_YEARS: int = 5         # OHLCV rows older than this are pruned
HALT_FAILURE_THRESHOLD: int = 3        # Send Telegram warning after N consecutive fetch failures
HALT_WARNING_RESEND_DAYS: int = 7      # Minimum days between repeated halt warnings per ticker

# Twelve Data /time_series `adjust` param. Per Twelve Data docs (confirmed
# 2026-07-11): supports all|splits|dividends|none, default is "splits".
# Passed explicitly (rather than relying on the API default) so behavior is
# self-documenting and immune to Twelve Data silently changing their default.
TWELVE_DATA_ADJUST: str = "splits"

# A2: OHLCV basis-drift detection. If a re-fetched close for an already-stored
# (non-newest) bar differs from the stored close by more than this relative
# fraction, the ticker's full OHLCV history is considered untrustworthy
# (e.g. a retroactive split/dividend adjustment was applied upstream) and is
# fully rebaselined via bootstrap_ticker().
OHLCV_REVISION_TOLERANCE: float = 0.005

# D: pre-dispatch sanity gate (src/sanity.py). Plausibility check — max
# allowed relative distance between price and ma_value before an alert is
# considered implausible (likely a data or logic error) and quarantined
# rather than sent. Wider timeframes tolerate more spread since W/M MAs
# lag price more during strong trends.
MA_SANITY_MAX_DEVIATION: dict[str, float] = {
    "D": 0.35,
    "W": 0.60,
    "M": 0.75,
}

# D: freshness check. Daily alerts whose bar_date is older than this many
# trading days are considered stale and quarantined rather than sent.
SANITY_MAX_STALE_TRADING_DAYS: int = 2


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Walk upward from this file until we find the repo root (.git or pyproject.toml)."""
    for p in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (p / "pyproject.toml").exists() or (p / ".git").exists():
            return p
    raise RuntimeError("repo root not found")


def load_watchlist() -> list[str]:
    """Load US watchlist from watchlist.yaml at repo root.

    Returns a flat list of US ticker symbols (e.g. ['AAPL', 'MSFT', ...]).
    """
    data = yaml.safe_load((_repo_root() / "watchlist.yaml").read_text()) or {}
    return list(data.get("us") or [])
