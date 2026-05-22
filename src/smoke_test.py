"""
smoke_test.py — Pre-deployment validation against 5 known tickers over 90 days.

Replay structure:
  - Silent phase: days 1 through (total_days - 90) — builds cascade state silently
  - Alert phase: most recent 90 days — signals ARE computed and logged

Uses TURSO_TEST_DATABASE_URL if set; otherwise a local temporary SQLite file.
Does NOT modify production DB. Does NOT send to Telegram unless --live-test is passed.

Run: python -m src.smoke_test [--live-test]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile

from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

_DEFAULT_TICKERS: list[tuple[str, str]] = [
    ("GOOG", "US"),
    ("AAPL", "US"),
    ("NVDA", "US"),
    ("AXON", "US"),
    ("CLS",  "US"),
]


def _setup_test_db() -> tuple[str, str]:
    """
    Determine test DB URL and auth token.
    Returns (url, auth_token).
    """
    url = os.getenv("TURSO_TEST_DATABASE_URL", "")
    token = os.getenv("TURSO_TEST_AUTH_TOKEN", "")
    if url:
        logger.info("Using test Turso DB: %s", url)
        return url, token

    # Fall back to a temporary local SQLite file
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="ma_smoke_")
    tmp.close()
    local_url = f"file:{tmp.name}"
    logger.info("No TURSO_TEST_DATABASE_URL set — using local SQLite: %s", tmp.name)
    return local_url, ""


def _replay_day(
    ticker: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    day_idx: int,
    emit_alerts: bool,
    db_backend,
) -> list[dict]:
    """
    Replay a single day's signal detection using a slice of the DataFrames up to day_idx.

    Args:
        ticker:       Ticker symbol.
        daily_df:     Full daily DataFrame (with MA columns pre-computed).
        weekly_df:    Full weekly DataFrame (with MA columns pre-computed).
        monthly_df:   Full monthly DataFrame (with MA columns pre-computed).
        day_idx:      The index of today's row in daily_df (0-based).
        emit_alerts:  If False, suppress alert output (silent build phase).
        db_backend:   The active DB backend (used via src.db module-level functions).

    Returns:
        List of alert dicts generated on this day (empty during silent phase).
    """
    from src.cascade import detect_forward_transition, apply_reclaim_de_escalation
    from src.signals import detect_3a_ma_support, detect_3c_touch_accumulation, detect_3b_reclaim
    from src.config import MA_PERIODS
    import src.db as db

    # Slice DataFrames up to and including today
    daily_slice = daily_df.iloc[: day_idx + 1].copy()

    # Slice weekly/monthly to bars on or before today's date
    today_date = daily_df.index[day_idx]
    weekly_slice = weekly_df[weekly_df.index <= today_date].copy()
    monthly_slice = monthly_df[monthly_df.index <= today_date].copy()

    today_close = float(daily_slice.iloc[-1]["close"])

    # Get MAs for cascade detection
    daily_mas: dict[int, float | None] = {}
    for p in MA_PERIODS:
        col = f"ma_{p}"
        if col in daily_slice.columns:
            v = daily_slice[col].iloc[-1]
            daily_mas[p] = None if pd.isna(v) else float(v)
        else:
            daily_mas[p] = None

    # Load cascade state
    cascade_state = db.get_cascade_state(ticker)

    # Detect forward transition
    transition = detect_forward_transition(ticker, today_close, daily_mas, cascade_state)
    if transition:
        db.set_cascade_state(ticker, transition["to_step"], transition["broken_ma"])
        # Clear any stale reclaim streak for the newly broken MA
        old_ma_period = int(transition["broken_ma"].lstrip("D"))
        db.set_reclaim_streak(ticker, old_ma_period, 0, None)
        cascade_state = db.get_cascade_state(ticker)

    cascade_step = cascade_state["current_step"]

    if not emit_alerts:
        return []

    # Signal detection (strict order: 3A → 3C → 3B)
    alerts: list[dict] = []

    alerts.extend(
        detect_3a_ma_support(ticker, daily_slice, weekly_slice, monthly_slice, cascade_step)
    )
    alerts.extend(
        detect_3c_touch_accumulation(ticker, daily_slice, weekly_slice, monthly_slice, cascade_step)
    )

    # 3B: skip if forward transition fired this day
    if not transition:
        reclaim_alert = detect_3b_reclaim(ticker, daily_slice, cascade_state)
        if reclaim_alert:
            de_escalation = apply_reclaim_de_escalation(ticker, cascade_state)
            if de_escalation:
                db.set_cascade_state(ticker, de_escalation["new_step"], de_escalation["new_broken_ma"])
            alerts.append(reclaim_alert)

    return alerts


def run_smoke_test(
    tickers: list[tuple[str, str]] | None = None,
    live_test: bool = False,
) -> dict:
    """
    Run the smoke test.

    Args:
        tickers:   List of (ticker, market) tuples. Defaults to 5 known US stocks.
        live_test: If True, dispatch alerts to Telegram (uses TELEGRAM_TEST_CHAT_ID).

    Returns:
        {
            'tickers_tested': int,
            'alerts_generated': int,
            'errors': [str, ...],
            'alert_log': [dict, ...]
        }
    """
    if tickers is None:
        tickers = _DEFAULT_TICKERS

    # Set up isolated test DB
    test_url, test_token = _setup_test_db()

    import src.db as db
    # Override the global connection to point at the test DB
    db.close_connection()
    os.environ["_SMOKE_TEST_DB_URL"] = test_url
    os.environ["_SMOKE_TEST_DB_TOKEN"] = test_token
    # Temporarily monkey-patch get_connection to use the test DB
    original_make_backend = db._make_backend

    def _test_make_backend(url=None, auth_token=None):
        return original_make_backend(test_url, test_token)

    db._make_backend = _test_make_backend
    db.close_connection()
    db.init_schema()

    from src.config import MA_PERIODS
    from src.fetcher import fetch_daily_ohlcv
    from src.ma import compute_ma
    from src.resample import resample_to_monthly, resample_to_weekly

    results = {
        "tickers_tested": 0,
        "alerts_generated": 0,
        "errors": [],
        "alert_log": [],
    }

    for ticker, market in tickers:
        logger.info("Smoke test: processing %s", ticker)
        try:
            # Add to watchlist
            db.add_watchlist_ticker(ticker, market)

            # Fetch full history
            from src.config import BOOTSTRAP_MAX_CANDLES
            candles = fetch_daily_ohlcv(ticker, market, outputsize=BOOTSTRAP_MAX_CANDLES)
            if not candles or len(candles) < 90:
                msg = f"{ticker}: insufficient data ({len(candles) if candles else 0} candles)"
                logger.warning(msg)
                results["errors"].append(msg)
                continue

            # Persist to test DB
            db.upsert_ohlcv(ticker, candles)

            # Build DataFrames with MAs
            daily_df = db.get_all_ohlcv(ticker)
            compute_ma(daily_df, MA_PERIODS)
            weekly_df = resample_to_weekly(daily_df)
            compute_ma(weekly_df, MA_PERIODS)
            monthly_df = resample_to_monthly(daily_df)
            compute_ma(monthly_df, MA_PERIODS)

            total_days = len(daily_df)
            alert_start_idx = max(0, total_days - 90)

            ticker_alerts: list[dict] = []

            for idx in range(total_days):
                emit = idx >= alert_start_idx
                day_alerts = _replay_day(
                    ticker=ticker,
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    day_idx=idx,
                    emit_alerts=emit,
                    db_backend=None,
                )
                ticker_alerts.extend(day_alerts)

            results["tickers_tested"] += 1
            results["alerts_generated"] += len(ticker_alerts)
            results["alert_log"].extend(ticker_alerts)

            logger.info(
                "Smoke test %s: %d alerts in 90-day window", ticker, len(ticker_alerts)
            )

        except Exception as exc:
            import traceback
            msg = f"{ticker}: {exc}"
            logger.error("Smoke test error for %s: %s\n%s", ticker, exc, traceback.format_exc())
            results["errors"].append(msg)

    # Optional: dispatch to Telegram test chat
    if live_test and results["alert_log"]:
        test_chat_id = os.getenv("TELEGRAM_TEST_CHAT_ID", "")
        if not test_chat_id:
            logger.warning("--live-test passed but TELEGRAM_TEST_CHAT_ID not set; skipping dispatch")
        else:
            # Temporarily switch chat ID to test channel
            prod_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            os.environ["TELEGRAM_CHAT_ID"] = test_chat_id
            from src.alerter import dispatch_alerts
            sent = dispatch_alerts(results["alert_log"][:5])  # Cap at 5 for smoke test
            os.environ["TELEGRAM_CHAT_ID"] = prod_chat_id
            logger.info("Smoke test sent %d sample alerts to test Telegram channel", sent)

    # Restore DB
    db._make_backend = original_make_backend
    db.close_connection()

    return results


def _print_report(results: dict) -> None:
    print("\n" + "=" * 60)
    print("SMOKE TEST REPORT")
    print("=" * 60)
    print(f"Tickers tested:    {results['tickers_tested']}")
    print(f"Alerts generated:  {results['alerts_generated']}")
    print(f"Errors:            {len(results['errors'])}")

    if results["errors"]:
        print("\nErrors:")
        for e in results["errors"]:
            print(f"  - {e}")

    if results["alert_log"]:
        print(f"\nAlert log ({len(results['alert_log'])} entries):")
        for alert in results["alert_log"]:
            print(
                f"  [{alert.get('fired_at', 'N/A')[:10]}] "
                f"{alert['ticker']:6s} "
                f"{alert['signal_type']:<20s} "
                f"{alert.get('timeframe', '')}{alert.get('ma_period', '')}"
            )
    print("=" * 60 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MA Alerts smoke test")
    parser.add_argument(
        "--live-test",
        action="store_true",
        help="Send sample alerts to TELEGRAM_TEST_CHAT_ID",
    )
    args = parser.parse_args()

    results = run_smoke_test(live_test=args.live_test)
    _print_report(results)

    # Write JSON report to file for manual inspection
    report_path = Path("smoke_test_report.json")
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Full report written to: {report_path.resolve()}")

    sys.exit(0 if not results["errors"] else 1)
