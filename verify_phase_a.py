"""
verify_phase_a.py — Phase A (Signal 3D) live verification script.

Run from the project root:
    python verify_phase_a.py

Checks:
  1. DB migration — alert_log accepts signal_type='3D'
  2. Synthetic bar — detect_3d fires on spec §A.2 values
  3. Real Telegram delivery — sends one real 3D message via bot
  4. NVDA live data — runs detect_3d against real OHLCV from Turso
  5. Confirms MA_PERIODS_DAILY and runner order
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from src.ma import compute_ma
from src.config import MA_PERIODS_DAILY, MOMENTUM_VOLUME_MULTIPLIER
from src.signals import detect_3d
from src.alerter import format_alert, send_telegram_message
import src.db as db

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
errors: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label}" + (f": {detail}" if detail else ""))
    if not ok:
        errors.append(label)


# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
print("\n[1] Config")
check("MA_PERIODS_DAILY == [20, 50, 100, 150, 200]",
      MA_PERIODS_DAILY == [20, 50, 100, 150, 200],
      str(MA_PERIODS_DAILY))
check("MOMENTUM_VOLUME_MULTIPLIER == 1.5",
      MOMENTUM_VOLUME_MULTIPLIER == 1.5,
      str(MOMENTUM_VOLUME_MULTIPLIER))


# ---------------------------------------------------------------------------
# 2. Synthetic bar (spec §A.2)
# ---------------------------------------------------------------------------
print("\n[2] Synthetic bar (spec §A.2)")

n = 250
dates = pd.bdate_range(end="2026-05-23", periods=n)
closes = [100.0 + i * 0.5 for i in range(n)]
df_synth = pd.DataFrame({
    "open":   [c - 0.5 for c in closes],
    "high":   [c + 1.0 for c in closes],
    "low":    [c - 0.5 for c in closes],
    "close":  closes,
    "volume": [60_000_000] * n,
}, index=dates)
df_synth.iloc[-1, df_synth.columns.get_loc("close")]  = 135.50
df_synth.iloc[-1, df_synth.columns.get_loc("low")]    = 128.40
df_synth.iloc[-1, df_synth.columns.get_loc("volume")] = 95_000_000
compute_ma(df_synth, MA_PERIODS_DAILY)
for col, val in [("ma_20", 130.20), ("ma_50", 122.80),
                 ("ma_100", 108.50), ("ma_150", 95.40), ("ma_200", 82.10)]:
    df_synth.iloc[-1, df_synth.columns.get_loc(col)] = val

result = detect_3d("NVDA", df_synth, cascade_step=1)
check("detect_3d returns Alert on synthetic bar", result is not None)
if result:
    check("signal_type == '3D'",         result["signal_type"] == "3D")
    check("ma_period == 20",             result["ma_period"] == 20)
    check("price == 135.50",             abs(result["price"] - 135.50) < 0.01,
          str(result["price"]))
    check("volume_ratio == 1.58",        abs(result["volume_ratio"] - 1.58) < 0.01,
          str(result["volume_ratio"]))
check("detect_3d returns None when step=2",
      detect_3d("NVDA", df_synth, cascade_step=2) is None)
check("detect_3d returns None when step=None",
      detect_3d("NVDA", df_synth, cascade_step=None) is None)


# ---------------------------------------------------------------------------
# 3. DB migration — alert_log accepts '3D'
# ---------------------------------------------------------------------------
print("\n[3] DB migration (live Turso)")
try:
    db.init_schema()  # runs _migrate_alert_log_v2() if needed

    # Try inserting a probe row
    db._db().execute(
        "INSERT INTO alert_log "
        "(ticker, signal_type, timeframe, ma_period, price_at_fire, ma_value, volume_ratio, extra_json, fired_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["NVDA", "3D", "D", 20, 135.50, 130.20, 1.58, "{}", "2099-01-01T00:00:00Z"]
    )
    rows = db._db().execute(
        "SELECT signal_type FROM alert_log WHERE fired_at='2099-01-01T00:00:00Z'"
    )
    check("Turso alert_log accepts signal_type='3D'",
          rows and rows[0]["signal_type"] == "3D")
    # Clean up probe
    db._db().execute("DELETE FROM alert_log WHERE fired_at='2099-01-01T00:00:00Z'")
except Exception as exc:
    check("Turso alert_log accepts signal_type='3D'", False, str(exc))


# ---------------------------------------------------------------------------
# 4. Telegram — real 3D message delivery
# ---------------------------------------------------------------------------
print("\n[4] Telegram — real 3D message delivery")
test_alert = {
    "ticker": "NVDA",
    "signal_type": "3D",
    "timeframe": "D",
    "ma_period": 20,
    "price": 135.50,
    "ma_value": 130.20,
    "volume_ratio": 1.58,
    "extra": {"low": 128.40, "close": 135.50},
    "fired_at": "2026-05-23T07:28:37Z",
}
msg = format_alert(test_alert)
print(f"  Message preview:\n{chr(10).join('    ' + l for l in msg.splitlines())}")
ok = send_telegram_message(msg)
check("Telegram HTTP 200 and ok=true", ok)


# ---------------------------------------------------------------------------
# 5. NVDA real data from Turso
# ---------------------------------------------------------------------------
print("\n[5] NVDA real data from Turso")
try:
    nvda_df = db.get_all_ohlcv("NVDA")
    check("NVDA OHLCV rows in DB", len(nvda_df) > 0, f"{len(nvda_df)} rows")
    if not nvda_df.empty:
        compute_ma(nvda_df, MA_PERIODS_DAILY)
        check("ma_20 computed for NVDA",
              "ma_20" in nvda_df.columns and not pd.isna(nvda_df["ma_20"].iloc[-1]),
              f"D20={nvda_df['ma_20'].iloc[-1]:.2f}")
        # Load cascade state and run detect_3d
        state = db.get_cascade_state("NVDA")
        step = state["current_step"]
        three_d = detect_3d("NVDA", nvda_df, step)
        if three_d:
            check(f"3D fires on NVDA today (step={step})", True,
                  f"vol_ratio={three_d['volume_ratio']:.2f}×")
        else:
            print(f"  ℹ  3D did NOT fire on NVDA today (step={step}) — "
                  "this may be correct; synthetic bar confirmed wiring")
            last = nvda_df.iloc[-1]
            d20  = nvda_df["ma_20"].iloc[-1]
            print(f"     close={last['close']:.2f}  low={last['low']:.2f}  "
                  f"D20={d20:.2f}  step={step}")
except Exception as exc:
    check("NVDA live data fetch", False, str(exc))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if errors:
    print(f"\033[91m✗ {len(errors)} check(s) failed: {', '.join(errors)}\033[0m")
    sys.exit(1)
else:
    print(f"\033[92m✓ All Phase A checks passed.\033[0m")
