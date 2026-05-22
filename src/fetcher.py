"""
fetcher.py — Twelve Data Daily OHLCV client.

Symbol conventions:
  - US tickers: passed as-is (e.g. 'GOOG', 'AAPL')
  - SG tickers: append ':SES' suffix (e.g. 'D05' → 'D05:SES')

Rate limiting:
  - Free tier: 8 calls/minute. Bulk helper sleeps 7.5s between calls.
  - Daily ceiling: 800 calls/day. 60 tickers × 1 call = 60 calls, well within budget.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger(__name__)

_TWELVE_DATA_BASE = "https://api.twelvedata.com"
_RATE_LIMIT_SLEEP = 7.5   # seconds between calls in bulk mode (8 calls/min ceiling)


def _api_key() -> str:
    key = os.getenv("TWELVE_DATA_API_KEY", "")
    if not key:
        raise RuntimeError("TWELVE_DATA_API_KEY environment variable is not set")
    return key


def _map_symbol(ticker: str, market: str) -> str:
    """Apply Twelve Data symbol convention for each market."""
    if market == "SG":
        return f"{ticker}:SES"
    return ticker


def _validate_candle(candle: dict[str, Any]) -> bool:
    """Return True if all required OHLCV fields are present and numeric."""
    required = ("datetime", "open", "high", "low", "close", "volume")
    for field in required:
        if field not in candle:
            return False
        if field != "datetime":
            try:
                float(candle[field])
            except (TypeError, ValueError):
                return False
    return True


def fetch_daily_ohlcv(
    ticker: str,
    market: str,
    outputsize: int = 30,
) -> list[dict] | None:
    """
    Fetch Daily OHLCV from Twelve Data for a single ticker.

    Returns a list of dicts with keys: date, open, high, low, close, volume.
    Sorted oldest first. Returns None on any failure.

    Args:
        ticker:     Base ticker symbol (e.g. 'GOOG', 'D05').
        market:     'US' or 'SG'. Determines symbol suffix.
        outputsize: Number of bars to request (max 5000). Use 30 for incremental,
                    5000 for bootstrap.
    """
    symbol = _map_symbol(ticker, market)
    params: dict[str, Any] = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": _api_key(),
    }

    try:
        response = requests.get(
            f"{_TWELVE_DATA_BASE}/time_series",
            params=params,
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.error("Network error fetching %s (%s): %s", ticker, symbol, exc)
        return None

    if response.status_code != 200:
        logger.error(
            "HTTP %d fetching %s (%s): %s",
            response.status_code,
            ticker,
            symbol,
            response.text[:200],
        )
        return None

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("JSON decode error for %s (%s): %s", ticker, symbol, exc)
        return None

    if payload.get("status") == "error" or "values" not in payload:
        code = payload.get("code", "unknown")
        message = payload.get("message", "no message")
        logger.error(
            "Twelve Data error for %s (%s): code=%s message=%s",
            ticker,
            symbol,
            code,
            message,
        )
        return None

    raw_candles: list[dict] = payload["values"]
    if not raw_candles:
        logger.warning("Empty OHLCV response for %s (%s)", ticker, symbol)
        return None

    candles: list[dict] = []
    for raw in raw_candles:
        if not _validate_candle(raw):
            logger.error(
                "Malformed candle for %s (%s): %s — aborting fetch", ticker, symbol, raw
            )
            raise ValueError(
                f"Twelve Data returned malformed candle for {ticker}: {raw}"
            )
        candles.append(
            {
                "date": raw["datetime"][:10],   # 'YYYY-MM-DD HH:MM:SS' → 'YYYY-MM-DD'
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": int(float(raw["volume"])),
            }
        )

    # Twelve Data returns newest first; reverse to oldest-first
    candles.sort(key=lambda c: c["date"])

    logger.debug("Fetched %d candles for %s (%s)", len(candles), ticker, symbol)
    return candles


def fetch_daily_ohlcv_bulk(
    tickers: list[tuple[str, str]],
    outputsize: int = 30,
) -> dict[str, list[dict] | None]:
    """
    Batch wrapper. Returns dict mapping ticker → candle list (or None on failure).

    Respects Twelve Data free-tier rate limit by sleeping 7.5s between calls.

    Args:
        tickers:    List of (ticker, market) tuples.
        outputsize: Passed to each individual fetch call.
    """
    results: dict[str, list[dict] | None] = {}

    for i, (ticker, market) in enumerate(tickers):
        if i > 0:
            time.sleep(_RATE_LIMIT_SLEEP)
        try:
            results[ticker] = fetch_daily_ohlcv(ticker, market, outputsize=outputsize)
        except ValueError as exc:
            # Malformed data — log and treat as failure rather than propagating
            logger.error("Malformed data for %s: %s", ticker, exc)
            results[ticker] = None
        except Exception as exc:
            logger.error("Unexpected error fetching %s: %s", ticker, exc)
            results[ticker] = None

    return results
