"""
fetcher.py — Twelve Data Daily OHLCV client.

Symbol conventions:
  - US tickers: passed as-is (e.g. 'GOOG', 'AAPL')
  - SG tickers: append ':SES' suffix (e.g. 'D05' → 'D05:SES')

Rate limiting:
  - Free tier: 8 calls/minute. Hard minimum 8s between calls → max 7.5 calls/min.
  - Daily ceiling: 800 calls/day. 60 tickers × 1 call = 60 calls, well within budget.
  - 429 responses are retried up to MAX_RETRIES_ON_429 times with RETRY_WAIT_SECONDS delay.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests
from dotenv import load_dotenv
import os

load_dotenv()

logger = logging.getLogger(__name__)

_TWELVE_DATA_BASE = "https://api.twelvedata.com"

# Twelve Data free tier: 8 calls/minute.
# Hard minimum 8 seconds between any API call → max 7.5 calls/min.
# Bulletproof: no sliding window math, no race conditions.
_MIN_API_INTERVAL_SECONDS = 8.0
_api_lock = threading.Lock()
_last_api_call_time: float = 0.0

# 429 retry config
MAX_RETRIES_ON_429 = 3
RETRY_WAIT_SECONDS = 65


def _enforce_min_interval() -> None:
    """Block until at least _MIN_API_INTERVAL_SECONDS has passed since the last API call.
    Thread-safe via _api_lock. Called before every Twelve Data HTTP request."""
    global _last_api_call_time
    with _api_lock:
        now = time.monotonic()
        elapsed = now - _last_api_call_time
        if elapsed < _MIN_API_INTERVAL_SECONDS:
            wait = _MIN_API_INTERVAL_SECONDS - elapsed
            logger.info("Rate limiter: waiting %.1fs before next API call", wait)
            time.sleep(wait)
        _last_api_call_time = time.monotonic()


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
        _enforce_min_interval()
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            logger.error("Network error fetching %s (%s): %s", ticker, symbol, exc)
            return None

        if response.status_code == 429:
            logger.warning(
                "Rate limit hit for %s (attempt %d/%d). Sleeping %ds before retry.",
                ticker, attempt + 1, MAX_RETRIES_ON_429, RETRY_WAIT_SECONDS,
            )
            time.sleep(RETRY_WAIT_SECONDS)
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
        logger.error("Rate limit retries exhausted for %s", ticker)
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
                "date": raw["datetime"][:10],   # 'YYYY-MM-DD HH:MM:SS' → 'YYYY-MM-