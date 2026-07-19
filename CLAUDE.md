# ma-alerts — Project notes (Fable Week 1 fix pass)

These notes record audit findings and design decisions from the Week 1 fix
pass (see `fable_week1_fix_spec.md` in the Cowork session that produced this
branch) that aren't obvious from reading any single file in isolation.

## Streak rule (Bug 4 / Fix 4)

`signals._detect_3b_for_timeframe()` counts a reclaim streak using **closes
only**. An intraday wick that touches or even pierces the MA never resets or
increments the streak — only `bar_close > ma_val` (increment) or
`bar_close <= ma_val` (reset) matter. This was already the code's behavior;
it just wasn't documented or visible in the alert copy. Every Daily 3B
message now includes an explicit "Streak rule: closes only — wicks ignored."
line (`alerter._format_3b`).

## No breakdown alert path exists (Bug 1 / Fix 1)

`_detect_3b_for_timeframe()` can only ever fire on an upward cross
(`bar_close > ma_val` reaching the required streak). Forward cascade
transitions (`cascade.detect_forward_transition`, called from
`runner._process_ticker()` step 4g) — i.e. a Daily MA actually breaking to
the downside — are **logged only**; no alert is dispatched for them today.
So `labels.SIGNAL_LABELS["3b"] == "BREAKTHROUGH"` was directionally correct
by construction, it just never said so. The header is now derived at format
time from the alert's own price vs ma_value
(`labels.directional_breakthrough_label`) with a defensive "BREAKDOWN ↓"
branch for a downward-crossing RECLAIM alert — unreachable today (also
quarantined upstream by `sanity.check_alert`'s direction gate), but the
label is no longer a hardcoded assumption.

## Trend status is a live computation (Bug 5 / Fix 5)

`labels.STEP_LABELS` phrases a cascade step as a live price claim ("below
the 150-day average"), but `cascade_state.current_step` is only
definitionally true at break/reclaim time. `labels.trend_status(close,
daily_mas)` builds the Daily 3B message's "Trend status" line from a live
close-vs-MA comparison instead, so it can never contradict the same
message's own numbers. Populated in `extra.trend_status` at detection time
in `signals._detect_3b_for_timeframe` (Daily only).

Option B (locked 2026-07-19): after a Daily 3B confirmation,
`runner._process_ticker()` derives the post-reclaim cascade step via
`cascade.determine_step_from_close()` on that day's close/MAs — not the
fixed N-1 rule (`cascade.apply_reclaim_de_escalation`, now unused in the
runner path but kept for tests/compat). Guard: a reclaim can never result
in a step ≥ the pre-reclaim step; if it would, the pre-reclaim state is
kept, a warning is logged, and one `[OPS]` note is sent (data anomaly, not
a valid de-escalation).

## OHLCV basis integrity (Bug 2 / Fix 2)

Root cause was a mixed price basis in older stored bars (Session 6's fetcher
fix wasn't backfilled beyond the 4 tickers it rebaselined). `ma.py` and
`TWELVE_DATA_ADJUST` were never wrong and are unchanged.

- **Fix 2a** (`python -m src.runner --rebaseline-all`): one-off, manually
  triggered remediation across the full watchlist.
- **Fix 2b** (`data_health.last_deep_audit`, rotating K=3/day at
  `outputsize=250`): structural fix closing `_check_and_repair_ohlcv_drift`'s
  30-bar blind spot going forward.

## Break-and-bounce break dates (Bug 6 / Fix 6)

`reclaim_tracker.last_break_date` is only ever written by the close-below
branch inside `_detect_3b_for_timeframe`. On the day a Daily MA actually
breaks, 3B is skipped (forward transition fires instead), so a
break-and-bounce (price closes back above before any subsequent scan
observes a close ≤ MA) left `last_break_date` null forever. Fixed by
recording `last_break_date` directly inside the `if transition:` branch in
`runner._process_ticker()`, using that day's bar date.
