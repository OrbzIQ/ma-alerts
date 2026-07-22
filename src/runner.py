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
  4-bis. Fix 2b: rotating deep drift audit (K tickers/day, outputsize=250) —
         closes the 30-bar blind spot in step 4's incremental drift check.
  5. Collect halt warnings
  6. Dispatch all alerts
  7. Persist to alert_log
  8. Retention pruning
  9. Summary log

Usage:
    python -m src.runner --market US
    python -m src.runner --market SG
    python -m src.runner --market US --test-telegram
    python -m src.runner --rebaseline AAPL,MSFT
    python -m src.runner --rebaseline-all
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

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


def _fail_and_alert(reason: str) -> int:
    """
    Send an ops alert for a pre-flight failure and return exit code 1.

    Lazy-imports send_ops_message so this is safe to call before the
    main() import block runs (e.g. from the env-check path).
    Never raises — if alert dispatch itself fails, the error is logged
    and the function still returns 1.
    """
    _ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"scanner pre-flight failed: {reason}\nUTC: {_ts}"
    try:
        from src.alerter import send_ops_message
        send_ops_message(msg)
    except Exception as alert_exc:
        logger.error("Failed to dispatch pre-flight ops alert: %r", alert_exc)
    return 1


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
# A2: OHLCV data-basis integrity
# ---------------------------------------------------------------------------

def _check_and_repair_ohlcv_drift(ticker: str, market: str, candles: list[dict]) -> bool:
    """
    Compare freshly-fetched closes against stored closes for overlapping
    dates (excluding the newest fetched bar, which is expected to move) and
    trigger a full rebaseline if any historical close has drifted beyond
    OHLCV_REVISION_TOLERANCE.

    A drift of this kind indicates the ticker's stored OHLCV basis is stale
    relative to the provider's current adjustment (e.g. a split/dividend
    adjustment was applied retroactively upstream) — patching incrementally
    would leave old bars on the old basis and new bars on the new basis,
    corrupting every MA that spans the boundary.

    Returns True if drift was detected and the repair path ran (caller should
    treat the DB as already holding the full, correct history and skip its
    own upsert of `candles`). Returns False if no drift was detected (caller
    proceeds with its normal incremental upsert).
    """
    import src.db as db
    from src.alerter import send_ops_message
    from src.bootstrap import bootstrap_ticker
    from src.config import BOOTSTRAP_MAX_CANDLES, OHLCV_REVISION_TOLERANCE

    if not candles:
        return False

    # Exclude the newest fetched bar — only compare bars that were already
    # stored from a prior run and are not expected to still be moving.
    historical = candles[:-1] if len(candles) > 1 else []
    if not historical:
        return False

    fetched_by_date = {c["date"]: float(c["close"]) for c in historical}
    stored_by_date = db.get_closes_for_dates(ticker, list(fetched_by_date.keys()))

    drifted_dates = []
    for d, fetched_close in fetched_by_date.items():
        stored_close = stored_by_date.get(d)
        if stored_close is None or stored_close == 0:
            continue
        rel_diff = abs(fetched_close - stored_close) / abs(stored_close)
        if rel_diff > OHLCV_REVISION_TOLERANCE:
            drifted_dates.append((d, stored_close, fetched_close, rel_diff))

    if not drifted_dates:
        return False

    logger.warning(
        "OHLCV basis drift detected for %s — %d date(s) exceed tolerance %.4f "
        "(e.g. %s: stored=%.4f fetched=%.4f diff=%.4f) — refetching full history",
        ticker, len(drifted_dates), OHLCV_REVISION_TOLERANCE, *drifted_dates[0],
    )

    try:
        send_ops_message(f"OHLCV basis drift detected for {ticker} — refetching full history")
    except Exception as exc:
        logger.error("Failed to send drift-detection ops alert for %s: %s", ticker, exc)

    # Refetch full history and atomically replace all stored rows.
    from src.fetcher import fetch_daily_ohlcv
    full_candles = fetch_daily_ohlcv(ticker, market, outputsize=BOOTSTRAP_MAX_CANDLES)
    if not full_candles:
        logger.error(
            "OHLCV drift repair for %s: full refetch returned no candles — "
            "leaving existing (drifted) history in place, skipping this run's detection",
            ticker,
        )
        return True  # still signal "drift handled" so caller doesn't double-upsert

    db.replace_ohlcv_atomic(ticker, full_candles)
    db.reset_reclaim_tracker_for_ticker(ticker)

    # Recompute cascade_state from the repaired history. Reuses bootstrap_ticker
    # wholesale per the accepted tradeoff (redundant network re-fetch inside
    # bootstrap_ticker, and touch_log gets repopulated by its own logic rather
    # than staying strictly untouched) rather than duplicating bootstrap's
    # replay loop as a standalone function.
    bootstrap_ok = bootstrap_ticker(ticker, market)
    if not bootstrap_ok:
        logger.error("OHLCV drift repair for %s: bootstrap_ticker re-run failed", ticker)

    # bootstrap_ticker() may have repopulated reclaim_tracker from its own
    # backward-scan (mid-streak detection) — re-apply A2's prescribed reset
    # (streak 0, streak_start NULL, was_broken 0) so the repaired ticker starts
    # this cycle from a clean slate rather than bootstrap's inferred streak.
    db.reset_reclaim_tracker_for_ticker(ticker)

    logger.info("OHLCV drift repair complete for %s", ticker)
    return True


# ---------------------------------------------------------------------------
# Fix 2b: rotating deep drift audit
# ---------------------------------------------------------------------------

def _run_deep_drift_audit() -> None:
    """
    Each scan, re-check the K=DEEP_AUDIT_TICKERS_PER_DAY tickers with the
    oldest last_deep_audit using a much wider fetch (outputsize=
    DEEP_AUDIT_OUTPUTSIZE) than the normal 30-bar incremental fetch, reusing
    _check_and_repair_ohlcv_drift's existing close-comparison-and-repair
    logic unchanged. This closes the structural blind spot in the regular
    A2 check: a basis discontinuity older than 30 bars is invisible to it,
    but still corrupts every 50-200 bar MA that spans the boundary.

    Never raises — a failure for one ticker (or the whole audit) is logged
    and does not abort the run; this is a background maintenance pass, not
    part of the critical scan path.

    last_deep_audit is only advanced on a successful fetch, so a transient
    fetch failure leaves the ticker "due" and it will be retried on the next
    run rather than silently skipping its turn in the rotation.
    """
    import src.db as db
    from src.config import DEEP_AUDIT_OUTPUTSIZE, DEEP_AUDIT_TICKERS_PER_DAY
    from src.fetcher import fetch_daily_ohlcv

    try:
        due_tickers = db.get_tickers_for_deep_audit(DEEP_AUDIT_TICKERS_PER_DAY)
    except Exception as exc:
        logger.error("Deep drift audit: failed to select tickers: %s", exc)
        return

    if not due_tickers:
        return

    market_by_ticker = {e["ticker"]: e["market"] for e in db.get_watchlist(market=None)}

    for ticker in due_tickers:
        ticker_market = market_by_ticker.get(ticker)
        if ticker_market is None:
            logger.warning("Deep drift audit: %s not found in watchlist — skipping", ticker)
            continue
        try:
            logger.info(
                "Deep drift audit: checking %s (%s) with outputsize=%d",
                ticker, ticker_market, DEEP_AUDIT_OUTPUTSIZE,
            )
            candles = fetch_daily_ohlcv(ticker, ticker_market, outputsize=DEEP_AUDIT_OUTPUTSIZE)
            if not candles:
                logger.warning(
                    "Deep drift audit: no candles returned for %s — leaving "
                    "last_deep_audit unchanged, will retry next run", ticker,
                )
                continue
            _check_and_repair_ohlcv_drift(ticker, ticker_market, candles)
            db.set_deep_audit_date(ticker, date.today())
        except Exception as exc:
            logger.error("Deep drift audit failed for %s: %s", ticker, exc)


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
    from src.cascade import detect_forward_transition, determine_step_from_close
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

        # 4c-bis. A2: OHLCV basis-drift check. Compare freshly-fetched closes
        # against what's already stored, for overlapping dates EXCLUDING the
        # newest bar (the newest bar is expected to change intraday/on revision
        # and isn't itself evidence of a basis change). If any overlapping
        # historical close has drifted beyond OHLCV_REVISION_TOLERANCE, the
        # ticker's whole stored history is untrustworthy (e.g. a retroactive
        # split/dividend adjustment was applied upstream) — refetch full
        # history, atomically replace all stored rows, reset reclaim state,
        # and recompute cascade_state before continuing the pipeline.
        _drift_detected = _check_and_repair_ohlcv_drift(ticker, market, candles)
        if _drift_detected:
            # Repair path already refetched + replaced OHLCV + reset reclaim
            # state + recomputed cascade_state via bootstrap_ticker(). Reload
            # candles from the now-repaired DB history for the rest of this
            # run instead of re-upserting the (possibly still basis-shifted)
            # incremental fetch on top.
            candles = None  # signal: DB already holds the correct, full history

        # 4d. Upsert OHLCV (skipped if the drift-repair path already replaced
        # the full history — re-upserting the original incremental fetch on
        # top of freshly-rebaselined data would reintroduce the drift).
        if candles is not None:
            db.upsert_ohlcv(ticker, candles)

        # 4e. Build DataFrames with MAs (use all history for MA accuracy)
        daily_df = db.get_all_ohlcv(ticker)
        if daily_df.empty:
            logger.warning("No OHLCV in DB for %s after upsert", ticker)
            return []

        # 4e-bis. OHLCV freshness gate (L6).
        # Count business days (Mon–Fri) between the last stored bar and today.
        # > 2 means the data is too stale to trust for signal detection — skip.
        import pandas as pd
        _last_bar = daily_df.index[-1].date()
        _stale_days = len(pd.bdate_range(
            start=_last_bar + timedelta(days=1),
            end=date.today(),
        ))
        if _stale_days > 2:
            logger.warning(
                "Skipping %s — OHLCV stale by %d trading days (last bar: %s)",
                ticker, _stale_days, _last_bar,
            )
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
            # Fix 6: record the break date at the moment it happens. 3B
            # (_detect_3b_for_timeframe) is skipped entirely on a transition
            # day, so last_break_date would otherwise stay null forever for
            # a break-and-bounce (price closes back above before any later
            # scan observes the close <= MA branch that normally sets it).
            db.set_reclaim_streak(
                ticker, "D", old_ma_period, 0, None,
                last_break_date=daily_df.index[-1].date(),
            )
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
                # Only Daily reclaim triggers cascade de-escalation.
                #
                # Fix 5 Option B (locked 2026-07-19): the post-reclaim step is
                # now DERIVED from today's close vs today's Daily MAs via
                # determine_step_from_close() — the same live comparison the
                # forward-transition path already uses — instead of the old
                # fixed N-1 rule (cascade.apply_reclaim_de_escalation, now
                # unused here; kept for tests/compat). The 7-close
                # confirmation gate in _detect_3b_for_timeframe is unchanged
                # and remains the sole anti-whipsaw gate before any downward
                # move — this only changes WHAT the new state is, not WHEN
                # a reclaim is allowed to fire.
                #
                # Guard: a reclaim can never legitimately result in a step
                # that is >= the pre-reclaim step (that would mean the
                # "reclaim" made things worse or unchanged, which is a data
                # anomaly, not a valid de-escalation) — keep the current
                # state, log a warning, and send one [OPS] note instead.
                if reclaim_alert.get("timeframe") == "D":
                    pre_reclaim_step = cascade_state.get("current_step", 1)
                    derived_step, derived_broken_ma = determine_step_from_close(
                        today_close, today_daily_mas
                    )
                    if derived_step >= pre_reclaim_step:
                        logger.warning(
                            "Reclaim de-escalation anomaly for %s: derived step "
                            "%d is not shallower than pre-reclaim step %d — "
                            "keeping current cascade state unchanged",
                            ticker, derived_step, pre_reclaim_step,
                        )
                        try:
                            from src.alerter import send_ops_message
                            send_ops_message(
                                f"Reclaim de-escalation anomaly for {ticker}: "
                                f"derived step {derived_step} >= pre-reclaim step "
                                f"{pre_reclaim_step} — state left unchanged"
                            )
                        except Exception as exc:
                            logger.error(
                                "Failed to send de-escalation anomaly ops alert for %s: %s",
                                ticker, exc,
                            )
                        reclaim_alert["extra"]["new_step"] = pre_reclaim_step
                    else:
                        db.set_cascade_state(ticker, derived_step, derived_broken_ma)
                        cascade_state = db.get_cascade_state(ticker)
                        # The alert's new_step must reflect the derived
                        # post-reclaim state (previous_step/new_step were
                        # computed inside _detect_3b_for_timeframe before
                        # this re-derivation ran).
                        reclaim_alert["extra"]["new_step"] = derived_step
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
    On unexpected exception: sends [OPS] failure alert then re-raises.
    """
    logger.info("MA Alert runner starting — market=%s date=%s", market, date.today())

    # Tracked outside the try so the except block can report them.
    _watchlist_size: int = 0
    _tickers_processed: int = 0

    try:
        # 1. Verify env
        try:
            _check_env()
        except RuntimeError as exc:
            logger.critical("%s", exc)
            return _fail_and_alert(str(exc))

        import src.db as db
        from src.alerter import dispatch_alerts, send_ops_message
        from src.config import OHLCV_RETENTION_YEARS, RUN_BUDGET_SECONDS, TOUCH_WINDOW_DAYS
        from src.health import check_and_warn_halts

        # 2. Init schema
        try:
            db.init_schema()
        except Exception as exc:
            logger.critical("Schema init failed: %s", exc)
            return _fail_and_alert(f"schema init failed: {exc}")

        # 3. Load watchlist
        try:
            watchlist = _load_watchlist_from_yaml(market)
        except Exception as exc:
            logger.critical("Watchlist load failed: %s", exc)
            return _fail_and_alert(f"watchlist load failed: {exc}")

        if not watchlist:
            logger.warning("Watchlist is empty for market=%s — nothing to scan", market)
            send_ops_message(
                "[OPS] scanner completed but watchlist empty — "
                "zero tickers processed"
            )
            return 0

        _watchlist_size = len(watchlist)
        logger.info("Scanning %d tickers for market=%s", _watchlist_size, market)

        # 4. Process tickers
        all_alerts: list[dict] = []
        scan_errors = 0
        sent = 0
        _loop_start = time.monotonic()

        for _i, entry in enumerate(watchlist):
            # Fix 4: wall-clock budget guard. If the loop has been running
            # longer than RUN_BUDGET_SECONDS, stop picking up new tickers —
            # graceful early stop, not a failure — and let the rest of the
            # pipeline (halt sweep, deep audit, pruning, summary) still run.
            if time.monotonic() - _loop_start > RUN_BUDGET_SECONDS:
                _remaining = [e["ticker"] for e in watchlist[_i:]]
                _elapsed = time.monotonic() - _loop_start
                logger.error(
                    "Run budget exceeded (%.0fs > %ds) — stopping with %d ticker(s) unscanned",
                    _elapsed, RUN_BUDGET_SECONDS, len(_remaining),
                )
                try:
                    _shown = _remaining[:10]
                    _more_note = f" +{len(_remaining) - 10} more" if len(_remaining) > 10 else ""
                    send_ops_message(
                        f"[OPS] scan run-budget exceeded ({_elapsed:.0f}s > "
                        f"{RUN_BUDGET_SECONDS}s) — {len(_remaining)} ticker(s) unscanned this run: "
                        f"{', '.join(_shown)}{_more_note}"
                    )
                except Exception as exc:
                    logger.error("Failed to send run-budget ops alert: %s", exc)
                break

            ticker = entry["ticker"]
            try:
                ticker_alerts = _process_ticker(ticker, market)
                all_alerts.extend(ticker_alerts)
                _tickers_processed += 1

                # Fix 3: dispatch each ticker's alerts immediately rather than
                # accumulating everything for a single post-loop dispatch. If
                # the run is killed mid-loop (e.g. the workflow timeout), any
                # ticker already processed has had its alerts delivered —
                # nothing is discarded wholesale. Safe because dispatch_alerts
                # gates on (ticker, signal_type, timeframe, ma_period), all
                # ticker-scoped, so dispatching earlier cannot change any
                # other ticker's detection.
                try:
                    sent += dispatch_alerts(ticker_alerts)
                except Exception as exc:
                    logger.error(
                        "Dispatch failed for %s (continuing scan): %s", ticker, exc
                    )
            except Exception as exc:
                logger.error("Unexpected exception for %s (should have been caught): %s", ticker, exc)
                scan_errors += 1

        # Zero-tickers guard: warn if loop ran but every iteration raised.
        # Distinct from the exception path — run is technically clean, do not re-raise.
        if _tickers_processed == 0 and _watchlist_size > 0:
            _ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            send_ops_message(
                f"Zero tickers processed — market={market} watchlist_size={_watchlist_size}\n"
                f"All per-ticker iterations failed. Check run logs.\n"
                f"UTC: {_ts}"
            )

        # 4-bis. Fix 2b: rotating deep drift audit (K tickers/day, outputsize=250).
        # Runs after the main per-ticker loop so a repair triggered here doesn't
        # reorder this run's own alert detection; it takes effect starting next run.
        try:
            _run_deep_drift_audit()
        except Exception as exc:
            logger.error("Deep drift audit step failed (non-fatal): %s", exc)

        # 5. Halt warnings
        halt_warnings = check_and_warn_halts()
        if halt_warnings:
            logger.warning("Halt warnings dispatched for: %s", ", ".join(halt_warnings))

        # 6 + 7. Dispatch already happened per-ticker inside the loop above
        # (Fix 3) — this just reports the accumulated totals.
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
            _watchlist_size,
            len(all_alerts),
            sent,
            scan_errors,
        )

        return 0 if scan_errors == 0 else 1

    except Exception as exc:
        _ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        _msg = (
            f"scanner failed mid-run — market={market}\n"
            f"Tickers attempted: {_tickers_processed}/{_watchlist_size}\n"
            f"Type: {type(exc).__name__}\n"
            f"Detail: {repr(exc)[:200]}\n"
            f"UTC: {_ts}"
        )
        try:
            from src.alerter import send_ops_message
            send_ops_message(_msg)
        except Exception as alert_exc:
            logger.error("Failed to send ops failure alert: %s", alert_exc)
        raise


# ---------------------------------------------------------------------------
# A3: manual rebaseline entry point
# ---------------------------------------------------------------------------

def main_rebaseline(tickers: list[str]) -> int:
    """
    Force the A2 OHLCV repair path for the named tickers, then exit.

    Unlike a normal scan, this doesn't compare against a freshly-fetched
    incremental candle set to detect drift — it unconditionally treats each
    named ticker as needing a full rebaseline: refetch full history, replace
    all stored OHLCV atomically, reset reclaim state, recompute cascade_state.

    Returns exit code: 0 if all tickers rebaselined successfully, 1 if any
    failed (details logged per-ticker; does not abort the batch early).
    """
    import src.db as db
    from src.alerter import send_ops_message
    from src.bootstrap import bootstrap_ticker
    from src.config import BOOTSTRAP_MAX_CANDLES
    from src.fetcher import fetch_daily_ohlcv

    try:
        _check_env()
    except RuntimeError as exc:
        logger.critical("%s", exc)
        return _fail_and_alert(str(exc))

    try:
        db.init_schema()
    except Exception as exc:
        logger.critical("Schema init failed: %s", exc)
        return _fail_and_alert(f"schema init failed: {exc}")

    # Look up each ticker's market from the watchlist (any market, active or not
    # doesn't matter for a manual rebaseline — the ticker must already exist).
    all_entries = db.get_watchlist(market=None)
    market_by_ticker = {e["ticker"]: e["market"] for e in all_entries}

    failures: list[str] = []
    for ticker in tickers:
        market = market_by_ticker.get(ticker)
        if market is None:
            logger.error("--rebaseline %s: ticker not found in watchlist — skipping", ticker)
            failures.append(ticker)
            continue

        logger.info("Rebaselining %s (%s)...", ticker, market)
        full_candles = fetch_daily_ohlcv(ticker, market, outputsize=BOOTSTRAP_MAX_CANDLES)
        if not full_candles:
            logger.error("--rebaseline %s: full refetch returned no candles", ticker)
            failures.append(ticker)
            continue

        db.replace_ohlcv_atomic(ticker, full_candles)
        db.reset_reclaim_tracker_for_ticker(ticker)
        bootstrap_ok = bootstrap_ticker(ticker, market)
        db.reset_reclaim_tracker_for_ticker(ticker)  # re-apply after bootstrap's own repopulation

        if not bootstrap_ok:
            logger.error("--rebaseline %s: bootstrap_ticker re-run failed", ticker)
            failures.append(ticker)
            continue

        logger.info("Rebaseline complete for %s", ticker)

    summary = f"Rebaseline complete: {len(tickers) - len(failures)}/{len(tickers)} succeeded"
    if failures:
        summary += f" — failed: {', '.join(failures)}"
    logger.info(summary)
    try:
        send_ops_message(summary)
    except Exception as exc:
        logger.error("Failed to send rebaseline summary ops alert: %s", exc)

    return 0 if not failures else 1


def main_rebaseline_all() -> int:
    """
    Fix 2a — full-watchlist rebaseline (one-off remediation).

    Forces the A2 OHLCV repair path (see main_rebaseline) for EVERY active
    watchlist ticker across all markets, not just a named subset. This is
    the maintenance entry point Gate A's diagnosis calls for: Session 6
    rebaselined only 4 tickers (APH, ANET, GOOG, V) after fixing the fetcher's
    `adjust` pinning; the rest of the watchlist (~38 more tickers) still
    carries pre-fix, mixed-basis history deep enough to corrupt 50-200 bar
    MAs while the 30-bar A2 drift guard stays blind to it.

    Manually triggered only (CLI flag / workflow_dispatch input) — never on
    a cron schedule. Budget: ~2 Twelve Data credits per ticker (one full
    history fetch in this function + one more inside bootstrap_ticker's own
    re-fetch), comfortably inside the 800/day quota for a watchlist of this
    size, paced by the existing fetcher rate limiter (respects the
    8-calls/minute floor automatically — no additional sleep needed here).

    Returns exit code: 0 if every ticker rebaselined successfully, 1 if any
    failed (per-ticker failures are logged and included in the final [OPS]
    summary; the batch is not aborted early).
    """
    import src.db as db

    try:
        _check_env()
    except RuntimeError as exc:
        logger.critical("%s", exc)
        return _fail_and_alert(str(exc))

    try:
        db.init_schema()
    except Exception as exc:
        logger.critical("Schema init failed: %s", exc)
        return _fail_and_alert(f"schema init failed: {exc}")

    all_entries = db.get_watchlist(market=None)
    all_tickers = [e["ticker"] for e in all_entries]

    if not all_tickers:
        logger.warning("--rebaseline-all: watchlist is empty — nothing to do")
        return 0

    logger.info("--rebaseline-all: rebaselining %d ticker(s)", len(all_tickers))
    return main_rebaseline(all_tickers)


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
    parser.add_argument(
        "--rebaseline",
        type=str,
        default=None,
        metavar="TICKER[,TICKER...]",
        help=(
            "Force the A2 OHLCV data-basis repair path for the named ticker(s) "
            "(comma-separated), then exit. Mutually exclusive with a normal scan."
        ),
    )
    parser.add_argument(
        "--rebaseline-all",
        action="store_true",
        help=(
            "Fix 2a: force the A2 OHLCV data-basis repair path for EVERY active "
            "watchlist ticker (all markets), then exit. One-off remediation — "
            "manually triggered only, never on cron. Mutually exclusive with "
            "--market and --rebaseline."
        ),
    )
    args = parser.parse_args()

    if args.test_telegram:
        from src.alerter import send_test_ping
        sys.exit(0 if send_test_ping() else 1)

    if args.rebaseline_all:
        if args.market or args.rebaseline:
            parser.error(
                "--rebaseline-all cannot be combined with --market or --rebaseline "
                "(mutually exclusive)"
            )
        sys.exit(main_rebaseline_all())

    if args.rebaseline:
        if args.market:
            parser.error("--rebaseline cannot be combined with --market (mutually exclusive)")
        _tickers = [t.strip() for t in args.rebaseline.split(",") if t.strip()]
        if not _tickers:
            parser.error("--rebaseline requires at least one ticker")
        sys.exit(main_rebaseline(_tickers))

    if not args.market:
        parser.error("--market is required when not using --test-telegram, --rebaseline, or --rebaseline-all")

    sys.exit(main(args.market))
