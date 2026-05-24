"""
cleanup_touch_log.py — Delete stale W/M rows from touch_log.

Run ONCE after the feature/signals-may-refine merge to remove any
weekly/monthly touch entries that were recorded against in-progress
(non-completed) bars under the old logic.

DO NOT run before the merge is confirmed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from src.db import get_connection

conn = get_connection()

# Count before delete — execute() returns list[dict], not rowcount
before = conn.execute(
    "SELECT COUNT(*) AS n FROM touch_log WHERE timeframe IN ('W','M')"
)
n = before[0]["n"] if before else 0

conn.execute("DELETE FROM touch_log WHERE timeframe IN ('W','M')")

print(f"Deleted {n} rows")
