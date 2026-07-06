"""
run_watchdog.py -- Missed-run watchdog for the daily scan.

Checks that the scheduled scan (daily_scan.yml) has actually been running by
summing api_usage.credits_used over the 2 most recent UTC dates present in the
table. Using the 2 most recent dates (rather than "today" alone) tolerates a
run that straddles UTC midnight — e.g. a scan kicked off just before 00:00 UTC
whose API calls get logged under yesterday's date, or GitHub Actions scheduling
jitter that pushes a run slightly later than its cron time.

If total credits used across those 2 dates is below the threshold, no
meaningful scan activity occurred recently -- something is wrong (workflow
disabled, Actions outage, secret expired, etc.) and an ops alert is sent.

Exit codes:
  0 -- watchdog OK (sufficient recent activity, or nothing to compare)
  1 -- watchdog FAILED (insufficient recent activity); ops alert was sent

Usage:
    python scripts/run_watchdog.py
"""

from __future__ import annotations

import sys
import os
import logging

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import src.db as db
import src.alerter as alerter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Below this combined credit usage across the last 2 UTC dates, we consider
# the scan "not running" and raise an ops alert.
CREDIT_THRESHOLD = 35


def _recent_dates_credits(n: int = 2) -> list[tuple[str, int]]:
    """Return [(date, credits_used), ...] for the n most recent dates in api_usage."""
    backend = db._db()
    rows = backend.execute(
        "SELECT date, credits_used FROM api_usage ORDER BY date DESC LIMIT ?",
        [n],
    )
    return [(r["date"], r["credits_used"]) for r in rows]


def main() -> int:
    db.init_schema()

    recent = _recent_dates_credits(2)
    total = sum(credits for _, credits in recent)

    if total < CREDIT_THRESHOLD:
        dates_str = ", ".join(f"{d}={c}" for d, c in recent) if recent else "no rows found"
        message = (
            f"watchdog: no scan detected in last 2 UTC days — check Actions "
            f"(credits used: {total}, threshold: {CREDIT_THRESHOLD}; {dates_str})"
        )
        logger.error(message)
        alerter.send_ops_message(message)
        return 1

    logger.info(
        "watchdog OK — %d credits used across last %d UTC day(s): %s",
        total, len(recent), recent,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
