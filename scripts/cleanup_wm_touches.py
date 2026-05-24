"""
cleanup_wm_touches.py -- One-off cleanup of stale W/M touch_log entries.

The old signal logic incorrectly recorded Weekly and Monthly touches using
daily candle data (today's daily bar low/close) rather than the corresponding
completed weekly/monthly bar. This produced spurious touch records with daily
dates that don't correspond to any weekly/monthly bar boundary.

Run this ONCE after deploying the W/M signal logic fix (commit
"Fix W/M signal logic: evaluate against completed weekly/monthly bars, not daily").

Effect:
  - Deletes all touch_log rows where timeframe IN ('W', 'M').
  - Daily ('D') touch records are unaffected -- those were always correct.

After the cleanup, the next daily scan (or a manual bootstrap run) will
repopulate W/M touches under the corrected logic.

Usage:
    python scripts/cleanup_wm_touches.py
"""

from __future__ import annotations

import sys
import os

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import src.db as db


def main() -> None:
    db.init_schema()
    backend = db._db()

    # Count before deletion
    rows = backend.execute("SELECT COUNT(*) AS cnt FROM touch_log WHERE timeframe IN ('W', 'M')")
    count_before = rows[0]["cnt"] if rows else 0

    print(f"W/M touch_log entries found: {count_before}")

    if count_before == 0:
        print("Nothing to delete — touch_log is already clean.")
        return

    backend.execute("DELETE FROM touch_log WHERE timeframe IN ('W', 'M')")

    # Verify
    rows_after = backend.execute("SELECT COUNT(*) AS cnt FROM touch_log WHERE timeframe IN ('W', 'M')")
    count_after = rows_after[0]["cnt"] if rows_after else 0

    print(f"Deleted {count_before - count_after} stale W/M touch_log entries.")
    print("Daily ('D') touches are untouched.")
    print()
    print("Next step: run the daily scan or bootstrap to repopulate W/M touches under the fixed logic.")


if __name__ == "__main__":
    main()
