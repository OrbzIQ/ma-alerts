"""
fetcher.py — Twelve Data Daily OHLCV client.

Symbol conventions:
  - US tickers: passed as-is (e.g. 'GOOG', 'AAPL')
  - SG tickers: append ':SES' suffix (e.g. 'D05' → 'D05:SES')

Rate limiting:
  - Free tier: 8 calls/minute. Sliding-window rate limiter in _wait_for_rate_limit().
  - Daily ceiling: 800 calls/day. 60 tickers × 1 call = 60 calls, well within budget.
  - 429 responses are retried up to MAX_RETRIES_ON_429 times with RETRY_BACKOFF_SECONDS delay.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

import requests
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger(__name__)

_TWELVE_DATA_BASE = "https://api.twelvedata.com"

# Sliding-window rate limiter for Twelve Data free tier (8 calls / 60s)
_RATE_LIMIT_CALLS = 8
_RATE_LIMIT_WINDOW = 60.0       # seconds
_RATE_LIMIT_BUFFER = 2.0        # extra safety margin in seconds
_call_timestamps: deque[float] = deque(maxlen=_RATE_LIMIT_CALLS)

# 429 retry config
MAX_RETRIES_ON_429 = 3
RETRY_BACKOFF_SECONDS = 65      # full minute + buffer


def _wait_for_rate_limit() -> None:
    """Block until making one more call would stay under 8/60s."""
    now = time.monotonic()
    if len(_call_timestamps) < _RATE_LIMIT_CALLS:
        _call_timestamps.append(now)
        return
    oldest = _call_timestamps[0]
    age = now - oldest
    if age < _RATE_LIMIT_WINDOW:
        sleep_for = (_RATE_LIMIT_WINDOW - age) + _RATE_LIMIT_BUFFER
        logger.info("Rate limit pause: sleeping %.1fs", sleep_for)
        time.sleep(sleep_for)
    _call_timestamps.append(time.monotonic())


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

    url = f"{_TWELVE_DATA_BASE}/time_series"
    response = None

    for attempt in range(MAX_RETRIES_ON_429):
        _wait_for_rate_limit()
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            logger.error("Network error fetching %s (%s): %s", ticker, symbol, exc)
            return None

        if response.status_code == 429:
            logger.warning(
                "Rate limit hit for %s (attempt %d/%d). Sleeping %ds before retry.",
                ticker, attempt + 1, MAX_RETRIES_ON_429, RETRY_BACKOFF_SECONDS,
            )
            time.sleep(RETRY_BACKOFF_SECONDS)
            _call_timestamps.clear()    # reset window after long sleep
            continue

        if response.status_code != 200:
            logger.error(
                "HTTP %d fetching %s (%s): %s",
                response.status_code,
                ticker,
                symbol,
                response.text[:200],
            )
            return None

        break   # successful response
    else:
        logger.error("Twelve Data rate limit retries exhausted for %s", ticker)
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

    Rate pacing is handled automatically by _wait_for_rate_limit() inside each call.

    Args:
        tickers:    List of (ticker, market) tuples.
        outputsize: Passed to each individual fetch call.
    """
    results: dict[str, list[dict] | None] = {}

    for ticker, market in tickers:
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
