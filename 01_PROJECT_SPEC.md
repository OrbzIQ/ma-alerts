# MA-Based Stock Entry Alert System — Spec v3 (Locked)

**Status:** Ready to build
**Scope:** v1 — Telegram alerts only. Dashboard and Momentum signal deferred.

---

## 1. Purpose
Daily scanner that fires Telegram alerts when stocks on a user-defined watchlist hit specific Moving Average (MA) support, reclaim, or accumulation conditions across Daily, Weekly, and Monthly timeframes. Triggered once per trading day, after market close.

---

## 2. Entry logic — 5-step MA cascade

The system tracks a per-stock cascade state. At any time, a stock is in exactly one of five steps. The step determines which MAs to evaluate.

### Core proximity rule
Within each timeframe group (Daily, Weekly, Monthly), find the highest-period MA that price is currently above and evaluate only that one MA for that timeframe. Lower-period MAs within the same timeframe are ignored unless price has broken the higher one. The proximity rule is applied independently per timeframe — it is not a single global winner across all MAs at the current cascade step.

Example: at Step 2, the MAs are D100, W50, W100, M50. If price is above both W50 and W100, only W100 is checked. D100 and M50 are evaluated independently of the Weekly group.

### Cascade steps

| Step | Trigger to enter | MAs checked at this step |
|---|---|---|
| 1 | Default state (price above Daily 50) | Daily 50 |
| 2 | Daily close below Daily 50 | Daily 100; Weekly 50, 100; Monthly 50 |
| 3 | Daily close below Daily 100 | Daily 150; Weekly 50, 100; Monthly 50, 100 |
| 4 | Daily close below Daily 150 | Daily 200; Weekly 100, 150, 200; Monthly 100, 150 |
| 5 | Daily close below Daily 200 | Weekly 100, 150, 200; Monthly 100, 150, 200 |

### Multi-step transitions
If a single daily close breaks multiple cascade thresholds (e.g. gap-down through D50 and D100), the stock jumps directly to the deepest step reached. **Only one alert fires** — for the deepest MA broken.

### De-escalation (reclaim)
A stock moves back up one step when its broken Daily MA is reclaimed and held for 7 consecutive closes (see Signal 3B). De-escalation is exactly one step per reclaim event.

---

## 3. Signal definitions

### 3A — MA Support Signal (trend mode)
Fires on a daily close when all of the following are true:

| Condition | Rule |
|---|---|
| Wick | Candle low ≤ the MA value |
| Close | Candle close > the MA value |
| Volume | Day's volume ≥ 1.5 × the 20-day average daily volume |
| Proximity | This MA is the nearest support per the proximity rule above |

The volume comparator is **always 20-day daily volume**, regardless of whether the signal is on a Daily, Weekly, or Monthly MA.

A 3A alert fires every time the conditions are met. No time-based suppression. Repeated 3A alerts on the same MA also feed the 3C touch counter.

### 3B — MA Reclaim Signal
Fires when a previously broken Daily MA is reclaimed and price closes above it for 7 consecutive trading days.

| Condition | Rule |
|---|---|
| Trigger | First daily close above the most recently broken Daily MA |
| Confirmation | 7 consecutive daily closes above that MA |
| Reset | Any close back below the MA resets the streak to 0 |
| No volume filter | Price action alone confirms |
| Cascade effect | On confirmation, current step de-escalates by one |

Alert fires on the day the streak reaches 7. A new 3B can fire again if the stock breaks below the MA, then reclaims it, then holds 7 days again.

### 3C — Continuous Touch Signal (accumulation)
Fires when a stock wicks-and-closes above the same MA 3 times within a rolling 15-trading-day window.

| Condition | Rule |
|---|---|
| Qualifying touch | Low ≤ MA AND close > MA on the same day |
| Threshold | 3 qualifying touches within the most recent 21 calendar days (≈ 15 trading days) |
| Volume | No minimum |
| Log retention | Touches older than 21 calendar days are pruned daily |

Fires independently of 3A — both can fire on the same day for the same MA.

### 3D — Momentum Signal (DEFERRED to v2)
Not in scope for v1. Do not implement.

---

## 4. Cascade state machine

```
States: STEP_1 (default) → STEP_2 → STEP_3 → STEP_4 → STEP_5

Forward transitions (on daily close):
  STEP_1 → STEP_2 : close below D50
  STEP_1 → STEP_3 : close below D50 AND D100 (same day, gap-down)
  STEP_1 → STEP_4 : close below D50, D100, D150 (same day)
  STEP_1 → STEP_5 : close below D50, D100, D150, D200 (same day)
  (intermediate steps follow the same multi-break rule)

Backward transitions (on 3B confirmation):
  STEP_N → STEP_(N-1) for N in {2,3,4,5}
  STEP_1 cannot de-escalate further.

Per-stock state fields:
  cascade_step          : int 1–5
  broken_ma             : "D50" | "D100" | "D150" | "D200" | "NONE"
  reclaim_streak        : int (consecutive closes above broken_ma)
  touch_log[ma_key]     : list of dates per (timeframe, ma_period) key
```

Each ticker has its own independent state. Multiple tickers can fire alerts on the same day.

---

## 5. Alert suppression & deduplication

| Signal | Rule |
|---|---|
| 3A | No suppression. Every qualifying day fires. |
| 3B | Fires once on confirmation. New 3B requires break-below + new 7-day streak. |
| 3C | Fires once when 3-touch threshold is reached. Touch log is NOT cleared; further touches continue to accumulate. A new 3C can fire if the count reaches 3 again over a fresh rolling window. |

---

## 6. Data layer

| Item | Choice |
|---|---|
| Data source | Twelve Data API (free tier sufficient at ~60 calls/day) |
| Fetch granularity | Daily OHLCV only |
| Weekly/Monthly | Resampled locally from Daily using pandas |
| MA values | Computed on-the-fly each run; not persisted |
| Persistence | Turso (hosted libSQL — SQLite-compatible) |
| OHLCV retention | Rolling 5 years per ticker |
| Alert log retention | Append-only forever |
| Bootstrap (new ticker) | Backfill 250 trading days, replay cascade forward to current state. No alerts during backfill. |

### Twelve Data API call budget
- 60 tickers × 1 Daily OHLCV call = ~60 calls/day
- Buffer for retries: 30 calls
- Total: ~90/day vs. 800/day free-tier ceiling. Safe.

---

## 7. Scheduling

Run once per trading day, after market close, per market.

| Market | Close (local) | Cron trigger (UTC) |
|---|---|---|
| US (NYSE, NASDAQ) | 16:00 ET | `30 21 * * 1-5` (21:30 UTC, ~1.5h after close to allow data settlement) |
| SG (SGX) | 17:00 SGT | `30 10 * * 1-5` (10:30 UTC, ~1.5h after close) |

Two separate cron triggers in the GH Actions workflow. Each trigger scans only stocks in its market.

Market handling: each ticker in `watchlist.yml` has a `market` field (`US` or `SG`). The runner filters by market based on which cron triggered it.

Holiday handling: out of scope for v1. If a market is closed and Twelve Data returns no new candle, the 3-consecutive-failure detector (see §8) handles it.

---

## 8. Halt / null-data handling

If Twelve Data returns no fresh candle or null data for a ticker on a scan day:
- Increment `consecutive_failures` for that ticker
- Skip detection for that ticker on that day
- On the 3rd consecutive failure, send a Telegram warning ("⚠️ Ticker X — 3 consecutive data failures. Verify halted/delisted.")
- Continue scanning other tickers normally

Successful fetch resets `consecutive_failures` to 0 and clears any warning state.

---

## 9. Alert formats (Telegram)

Each signal is a separate Telegram message. No batching.

### 3A — MA Support
```
📊 [TICKER] — [Timeframe] [MA] Support Signal

Price:        $X.XX
MA Value:     $X.XX ([period] MA)
Volume:       2.1× avg  (above 1.5× threshold)
Timeframe:    Daily / Weekly / Monthly
Signal Type:  MA Support Hold
Touch count:  2 of 3 (within 15-day window)

Wick low touched $X.XX, closed at $X.XX
Next resistance: $X.XX ([next MA])

⚠️ Check chart before acting.
```

### 3B — Reclaim Confirmed
```
✅ [TICKER] — [Daily MA] Reclaim Confirmed

MA reclaimed:     D[period] @ $X.XX
Consecutive days: 7 closes above
Previous state:   Step [N] → now Step [N-1]

MA is now acting as support again.

⚠️ Check chart before acting.
```

### 3C — Touch Accumulation
```
🔁 [TICKER] — [Timeframe] [MA] Support Accumulation

Touch count:  3 of 3 within 15 days
MA Value:     $X.XX
Dates:        May 1 · May 8 · May 14

Level is repeatedly holding as support. Potential bottom forming.

⚠️ Monitor for breakout confirmation.
```

### Halt warning
```
⚠️ [TICKER] — 3 consecutive data failures

Last successful fetch: YYYY-MM-DD
Possible halt, delist, or ticker symbol change.
Verify and update watchlist if needed.
```

---

## 10. Markets in scope

- US: NYSE, NASDAQ
- SG: SGX

Other markets (HK, JP, etc.) deferred to v2.

---

## 11. Out of scope for v1

- Momentum signal (3D)
- Dashboard (FastAPI / HTML)
- Backtest engine (full)
- Intraday scanning
- Broker integration
- Holiday calendar handling
- Corporate actions / split adjustments (rely on Twelve Data adjusted data)
- Multi-user support

---

## 12. Pre-live validation

Before deploying to GH Actions cron, run `smoke_test.py` on 5 known tickers over the last 90 days of Daily OHLCV. Verify:
- Cascade transitions happen on the expected dates (manual chart cross-check)
- No alerts fire during the 250-day backfill phase
- Telegram dispatcher posts to a test channel without errors
- Halt warning fires after exactly 3 simulated consecutive failures

Smoke test runs locally only — does not deploy.
