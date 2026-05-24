"""
runner.py — Orchestration entry point. Invoked by GitHub Actions cron.

Pipeline per run:
  1. Load .env / verify required env vars
  2. Initialise DB schema
  3. Load watchlist filtered by market
  4. For each ticker:
       a. Fetch latest OHLCV (outputsize=30)
       b. Update data health
       c. Skip detection if fetch failed
       d. Upsert OHLCV
       e. Compute DataFrames + MAs
       f. Load cascade state
       g. Detect forward transition → persist immediately, clear stale reclaim streak
       h. Signal detectors in strict order: 3A → 3C → 3B → 3D
          3B is skipped if a forward transition fired this run
          3D is stateless — runs unconditionally after cascade state is final
       i. Collect alerts
  5. Collect halt warnings
  6. Dispatch all alerts
  7. Persist to alert_log
  8. Retention pruning
  9. Summary log

Usage:
    python -m src.runner --market US
    python -m src.runner --market SG
    python -m src.runner --market US --test-telegram
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Logging setup — done before any other src imports so module loggers work
# ---------------------------------------------------------------------------
def _setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


_setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required env vars
# ---------------------------------------------------------------------------
_REQUIRED_VARS = [
    "TWELVE_DATA_API_KEY",
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


def _check_env() -> None:
    missing = [v for v in _REQUIRED_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# Watchlist loading
# ---------------------------------------------------------------------------

def _load_watchlist_from_yaml(market: str) -> list[dict]:
    """
    Sync watchlist.yaml → DB and return active tickers for the given market.
    YAML parsing is delegated to src.config.load_watchlist().
    """
    import src.db as db
    from src.bootstrap import bootstrap_ticker
    from src.config import load_watchlist

    tickers = load_watchlist()  # flat list of US ticker strings

    for ticker in tickers:
        is_new = db.add_watchlist_ticker(ticker, "US")
        if is_new:
            logger.info("New ticker %s detected in watchlist — bootstrapping", ticker)
            success = bootstrap_ticker(ticker, "US")
            if not success:
                logger.error("Bootstrap failed for %s — skipping this ticker", ticker)

    return db.get_watchlist(market)


# ---------------------------------------------------------------------------
# Per-ticker processing
# ---------------------------------------------------------------------------

def _process_ticker(ticker: str, market: str) -> list[dict]:
    """
    Process a single ticker: fetch, compute, detect signals.

    Returns list of alert dicts (may be empty).
    Exceptions are caught and logged — never propagates.
    """
    import src.db as db
    from src.cascade import apply_reclaim_de_escalation, detect_forward_transition
    from src.config import MA_PERIODS, MA_PERIODS_DAILY, OHLCV_RETENTION_YEARS
    from src.fetcher import fetch_daily_ohlcv
    from src.health import update_after_fetch
    from src.ma import compute_ma
    from src.resample import resample_to_monthly, resample_to_weekly
    from src.signals import (
        detect_3a_ma_support,
        detect_3b_reclaim,
        detect_3c_touch_accumulation,
        detect_3d,
    )

    alerts: list[dict] = []

    try:
        # 4a. Fetch latest OHLCV (incremental: 30 bars)
        candles = fetch_daily_ohlcv(ticker, market, outputsize=30)

        # 4b. Update data health
        update_after_fetch(ticker, candles)

        # 4c. Skip detection if fetch failed
        if not candles:
            logger.warning("Skipping %s — no candles returned", ticker)
            return []

        # 4d. Upsert OHLCV
        db.upsert_ohlcv(ticker, candles)

        # 4e. Build DataFrames with MAs (use all history for MA accuracy)
        daily_df = db.get_all_ohlcv(ticker)
        if daily_df.empty:
            logger.warning("No OHLCV in DB for %s after upsert", ticker)
            return []

        compute_ma(daily_df, MA_PERIODS_DAILY)  # includes D20 for 3D signal
        weekly_df = resample_to_weekly(daily_df)
        compute_ma(weekly_df, MA_PERIODS)
        monthly_df = resample_to_monthly(daily_df)
        compute_ma(monthly_df, MA_PERIODS)

        # 4f. Load cascade state
        cascade_state = db.get_cascade_state(ticker)

        # Get today's Daily MA values for cascade logic
        today_daily_mas: dict[int, float | None] = {}
        for p in MA_PERIODS:
            col = f"ma_{p}"
            if col in daily_df.columns:
                v = daily_df[col].iloc[-1]
                import pandas as pd
                today_daily_mas[p] = None if pd.isna(v) else float(v)
            else:
                today_daily_mas[p] = None

        today_close = float(daily_df.iloc[-1]["close"])

        # 4g. Detect forward transition
        transition = detect_forward_transition(
            ticker, today_close, today_daily_mas, cascade_state
        )
        if transition:
            db.set_cascade_state(ticker, transition["to_step"], transition["broken_ma"])
            # Clear stale reclaim streak for the newly broken MA
            old_ma_period = int(transition["broken_ma"].lstrip("D"))
            db.set_reclaim_streak(ticker, "D", old_ma_period, 0, None)
            # Reload updated state
            cascade_state = db.get_cascade_state(ticker)
            logger.info(
                "Cascade transition for %s: step %d → %d",
                ticker,
                transition["from_step"],
                transition["to_step"],
            )

            # L5: Log intermediate Daily touches for multi-step gap-downs.
            # A gap from step F to step T skips MAs at steps F+1 … T.
            # Each crossed MA gets a touch record so 3C has accurate history.
            from_step = transition["from_step"]
            to_step = transition["to_step"]
            if to_step - from_step > 1:
                from src.config import CASCADE_MA_BY_STEP
                today_date = date.today()
                for step in range(from_step + 1, to_step + 1):
                    period = CASCADE_MA_BY_STEP.get(step)
                    if period is not None:
                        db.record_touch(ticker, "D", period, today_date)
                        logger.debug(
                            "Intermediate touch logged for %s D%d (gap-down step %d→%d)",
                            ticker, period, from_step, to_step,
                        )

        cascade_step = cascade_state["current_step"]

        # 4h. Signal detectors — STRICT ORDER: 3A → 3C → 3B → 3D
        alerts.extend(
            detect_3a_ma_support(ticker, daily_df, weekly_df, monthly_df, cascade_step)
        )
        alerts.extend(
            detect_3c_touch_accumulation(ticker, daily_df, weekly_df, monthly_df, cascade_step)
        )

        # 3B: skip if a forward transition fired this run
        if not transition:
            reclaim_alerts = detect_3b_reclaim(
                ticker, daily_df, weekly_df, monthly_df, cascade_state, cascade_step
            )
            for reclaim_alert in reclaim_alerts:
                # Only Daily reclaim triggers cascade de-escalation
                if reclaim_alert.get("timeframe") == "D":
                    de_escalation = apply_reclaim_de_escalation(ticker, cascade_state)
                    if de_escalation:
                        db.set_cascade_state(
                            ticker,
                            de_escalation["new_step"],
                            de_escalation["new_broken_ma"],
                        )
                alerts.append(reclaim_alert)

        # 3D: stateless, runs unconditionally; receives post-transition cascade_step
        three_d = detect_3d(ticker, daily_df, cascade_step)
        if three_d:
            alerts.append(three_d)

    except Exception as exc:
        import traceback
        logger.error(
            "Unhandled error processing %s:\n%s", ticker, traceback.format_exc()
        )

    return alerts


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main(market: str) -> int:
    """
    Main entry point. Returns exit code: 0 on success, 1 on hard failure.
    """
    logger.info("MA Alert runner starting — market=%s date=%s", market, date.today())

    # 1. Verify env
    try:
        _check_env()
    except RuntimeError as exc:
        logger.critical("%s", exc)
        return 1

    import src.db as db
    from src.alerter import dispatch_alerts
    from src.config import OHLCV_RETENTION_YEARS, TOUCH_WINDOW_DAYS
    from src.health import check_and_warn_halts

    # 2. Init schema
    try:
        db.init_schema()
    except Exception as exc:
        logger.critical("Schema init failed: %s", exc)
        return 1

    # 3. Load watchlist
    try:
        watchlist = _load_watchlist_from_yaml(market)
    except Exception as exc:
        logger.critical("Watchlist load failed: %s", exc)
        return 1

    if not watchlist:
        logger.warning("Watchlist is empty for market=%s — nothing to scan", market)
        return 0

    logger.info("Scanning %d tickers for market=%s", len(watchlist), market)

    # 4. Process tickers
    all_alerts: list[dict] = []
    scan_errors = 0

    for entry in watchlist:
        ticker = entry["ticker"]
        try:
            ticker_alerts = _process_ticker(ticker, market)
            all_alerts.extend(ticker_alerts)
        except Exception as exc:
            logger.error("Unexpected exception for %s (should have been caught): %s", ticker, exc)
            scan_errors += 1

    # 5. Halt warnings
    halt_warnings = check_and_warn_halts()
    if halt_warnings:
        logger.warning("Halt warnings dispatched for: %s", ", ".join(halt_warnings))

    # 6 + 7. Dispatch alerts (persists to alert_log on success)
    sent = dispatch_alerts(all_alerts)
    logger.info("Dispatched %d / %d alerts", sent, len(all_alerts))

    # 8. Retention pruning
    try:
        db.prune_old_ohlcv(OHLCV_RETENTION_YEARS)
        db.prune_old_touches(TOUCH_WINDOW_DAYS)
    except Exception as exc:
        logger.warning("Retention pruning error (non-fatal): %s", exc)

    # 9. Summary
    logger.info(
        "Run complete — market=%s tickers_scanned=%d alerts_fired=%d alerts_sent=%d errors=%d",
        market,
        len(watchlist),
        len(all_alerts),
        sent,
        scan_errors,
    )

    return 0 if scan_errors == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MA Alert daily scanner")
    parser.add_argument(
        "--market",
        choices=["US", "SG"],
        required=False,
        default=None,
        help="Which market to scan (US or SG)",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test ping to Telegram and exit (bypasses main pipeline)",
    )
    args = parser.parse_args()

    if args.test_telegram:
        from src.alerter import send_test_ping
        sys.exit(0 if send_test_ping() else 1)

    if not args.market:
        parser.error("--market is required when not using --test-telegram")

    sys.exit(main(args.market))
