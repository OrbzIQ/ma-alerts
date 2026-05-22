# Build Manifest — File-by-File Requirements

For each module: purpose, key functions/signatures, dependencies, and edge cases. Implement in the order listed in `00_COWORK_BRIEF.md`.

---

## `src/config.py`

**Purpose:** Single source of truth for all tunable parameters.

**Contents (module-level constants):**
```python
# Moving Average periods (applied to Daily, Weekly, Monthly)
MA_PERIODS = [50, 100, 150, 200]

# Signal thresholds
VOLUME_MULTIPLIER = 1.5            # 3A: vol >= 1.5 * 20-day avg
VOLUME_LOOKBACK_DAYS = 20          # Always daily, regardless of signal timeframe
RECLAIM_STREAK_DAYS = 7            # 3B: consecutive closes above broken MA
TOUCH_THRESHOLD = 3                # 3C: touches required
TOUCH_WINDOW_DAYS = 21             # 3C: rolling window in CALENDAR days (~15 trading days)

# Cascade thresholds (which Daily MA defines each step)
CASCADE_MA_BY_STEP = {
    2: 50,
    3: 100,
    4: 150,
    5: 200,
}

# MAs evaluated at each cascade step. Format: list of (timeframe, period) tuples.
# Timeframes: "D" = Daily, "W" = Weekly, "M" = Monthly.
CASCADE_CHECKS = {
    1: [("D", 50)],
    2: [("D", 100), ("W", 50), ("W", 100), ("M", 50)],
    3: [("D", 150), ("W", 50), ("W", 100), ("M", 50), ("M", 100)],
    4: [("D", 200), ("W", 100), ("W", 150), ("W", 200), ("M", 100), ("M", 150)],
    5: [("W", 100), ("W", 150), ("W", 200), ("M", 100), ("M", 150), ("M", 200)],
}

# Data
BOOTSTRAP_DAYS = 250               # Minimum acceptable history for a new ticker
BOOTSTRAP_MAX_CANDLES = 5000       # Pass to Twelve Data outputsize — fetch max available
OHLCV_RETENTION_YEARS = 5          # Trim policy
HALT_FAILURE_THRESHOLD = 3         # Telegram warning after N consecutive failed fetches
```

---

## `src/db.py`

**Purpose:** All Turso interactions. Connection management, schema init, CRUD wrappers, retention pruning.

**Dependencies:** `libsql-client>=0.3.0`, `python-dotenv`

**Public functions:**
```python
def get_connection() -> libsql_client.Client:
    """
    Returns a Turso client using TURSO_DATABASE_URL + TURSO_AUTH_TOKEN from env.
    Use libsql_client.create_client(url=..., auth_token=...) to construct.
    Execute queries with client.execute(sql, params) — returns a ResultSet.
    Always call client.close() when done, or use as a context manager.
    """

def init_schema() -> None:
    """Runs DDL from 03_SCHEMA.sql. Idempotent — safe to call on every run."""

def upsert_ohlcv(ticker: str, candles: list[dict]) -> None:
    """Insert or replace Daily OHLCV rows for a ticker."""

def get_ohlcv(ticker: str, days: int) -> pd.DataFrame:
    """Return last N days of Daily OHLCV for ticker as a DataFrame indexed by date."""

def get_cascade_state(ticker: str) -> dict:
    """Returns {'current_step': int, 'broken_ma': str, 'last_updated': date}."""

def set_cascade_state(ticker: str, step: int, broken_ma: str) -> None:
    """Upsert cascade state for ticker."""

def get_reclaim_streak(ticker: str, ma_period: int) -> int:
    """Returns current consecutive-closes-above count."""

def set_reclaim_streak(
    ticker: str,
    ma_period: int,
    streak: int,
    streak_start: date | None = None,
) -> None:
    """
    Upsert reclaim streak for this ticker + MA period.
    Sets last_updated to today's date automatically.
    streak_start: pass the date the streak began (first close above MA).
                  Pass None to clear (when resetting streak to 0).
    """

def record_touch(ticker: str, timeframe: str, ma_period: int, touch_date: date) -> None:
    """Insert a touch event into touch_log."""

def get_recent_touches(ticker: str, timeframe: str, ma_period: int, window_days: int) -> list[date]:
    """Return touches within the last N calendar days for this MA."""

def prune_old_touches(window_days: int) -> int:
    """Delete touch_log rows older than window_days (calendar days). Returns count deleted."""

def get_data_health(ticker: str) -> dict:
    """Returns {'last_success': date, 'consecutive_failures': int, 'last_warning_sent': date|None}."""

def update_data_health(ticker: str, success: bool) -> None:
    """On success: set last_success, reset failures to 0. On fail: increment failures."""

def mark_warning_sent(ticker: str) -> None:
    """Record that halt warning was sent today, to avoid spamming."""

def insert_alert(alert: dict) -> None:
    """Append-only insert into alert_log."""

def add_watchlist_ticker(ticker: str, market: str) -> bool:
    """Returns True if newly added, False if already existed."""

def get_watchlist(market: str | None = None) -> list[dict]:
    """Returns active watchlist, optionally filtered by market."""

def prune_old_ohlcv(retention_years: int) -> int:
    """Delete OHLCV older than retention_years. Returns count deleted."""
```

**Edge cases:**
- All write operations must be transactional where multiple tables are touched
- Reads return empty list/dict for unknown tickers — no exceptions
- `init_schema()` must use `CREATE TABLE IF NOT EXISTS` so it's safe to re-run

---

## `src/fetcher.py`

**Purpose:** Fetch Daily OHLCV from Twelve Data API.

**Dependencies:** `requests`, `python-dotenv`

**Public functions:**
```python
def fetch_daily_ohlcv(ticker: str, market: str, outputsize: int = 30) -> list[dict] | None:
    """
    Returns list of {'date': str, 'open': float, 'high': float, 'low': float,
                     'close': float, 'volume': int} or None on failure.
    Date format: 'YYYY-MM-DD'. Sorted oldest first.

    `outputsize` maps directly to Twelve Data's outputsize parameter (max 5000).
    Daily incremental updates: outputsize=30. Bootstrap: outputsize=5000.

    Uses TWELVE_DATA_API_KEY from env.
    Symbol mapping:
      - US tickers: raw ticker (e.g. 'GOOG')
      - SG tickers: append ':SES' (Twelve Data's SGX suffix), e.g. 'D05:SES'
    """

def fetch_daily_ohlcv_bulk(tickers: list[tuple[str, str]], outputsize: int = 30) -> dict[str, list[dict] | None]:
    """Batch wrapper. Returns dict mapping ticker → candle list (or None)."""
```

**Edge cases:**
- Returns None (not raises) on HTTP error, rate limit, or malformed response
- Logs full error context for debugging
- Validates that response contains all OHLCV fields and that values are numeric
- Respects Twelve Data free-tier rate limit (8 calls/minute) — add `time.sleep(7.5)` between calls when bulk fetching
- Twelve Data daily call ceiling on free tier: 800/day. Confirm watchlist size stays well under this.

---

## `src/resample.py`

**Purpose:** Resample Daily OHLCV into Weekly and Monthly bars using pandas.

**Public functions:**
```python
def resample_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """
    Input: DataFrame indexed by date with columns open/high/low/close/volume.
    Output: DataFrame indexed by week-ending date (Friday) with OHLCV aggregated.
    Aggregation: open=first, high=max, low=min, close=last, volume=sum.
    """

def resample_to_monthly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Same as weekly but month-ending bars."""
```

**Edge cases:**
- Partial weeks (e.g. holiday-shortened) still produce a bar
- Requires daily_df to have a proper DatetimeIndex
- Handle empty input → return empty DataFrame, not raise

---

## `src/ma.py`

**Purpose:** Compute Moving Averages.

**Public functions:**
```python
def compute_ma(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    """
    Adds columns 'ma_50', 'ma_100', 'ma_150', 'ma_200' (or whatever periods provided).
    Uses simple moving average on the 'close' column.
    Returns the modified DataFrame.
    """

def latest_ma_value(df: pd.DataFrame, period: int) -> float | None:
    """Returns the most recent MA value, or None if insufficient data."""
```

**Edge cases:**
- If df has fewer rows than the MA period, the MA column for those rows is NaN; `latest_ma_value` returns None
- Use SMA (not EMA) — spec explicitly references MA, not EMA
- Monthly MA periods (50, 100, 150, 200) require 50–200 months of history. If a ticker has insufficient history, those MAs will be NaN — this is expected, not an error.

---

## `src/cascade.py`

**Purpose:** Cascade state machine. Determines current step and detects forward/backward transitions.

**Public functions:**
```python
def determine_step_from_close(close: float, daily_mas: dict[int, float]) -> tuple[int, str]:
    """
    Given today's close and dict of {50: ma_50_value, 100: ma_100_value, ...},
    return (step, broken_ma) where step is 1-5 and broken_ma is 'D50'/'D100'/etc or 'NONE'.

    Rule: step is determined by the LOWEST MA the close is still above.
    - close > D50           → step 1, broken_ma = NONE
    - close <= D50, > D100  → step 2, broken_ma = D50
    - close <= D100, > D150 → step 3, broken_ma = D100
    - close <= D150, > D200 → step 4, broken_ma = D150
    - close <= D200         → step 5, broken_ma = D200

    Gap-down handling is implicit: a close below D50 AND D100 lands directly at step 3.
    """

def detect_forward_transition(ticker: str, today_close: float, daily_mas: dict, db_state: dict) -> dict | None:
    """
    Returns transition info if step has increased from db_state, else None.
    Output: {'from_step': int, 'to_step': int, 'broken_ma': str, 'date': date}
    """

def apply_reclaim_de_escalation(ticker: str, current_state: dict) -> dict | None:
    """
    Returns new state if a 3B confirmation has occurred (called after signals.detect_reclaim).
    Output: {'new_step': int, 'new_broken_ma': str} or None.

    De-escalation rule: step N → step (N-1).
    The new broken_ma is determined by CASCADE_MA_BY_STEP[N-1], i.e. the Daily MA
    that defines the new (lower) step. If N-1 == 1, broken_ma = 'NONE'.

    Examples:
      Step 3 → 2: new broken_ma = 'D50'   (CASCADE_MA_BY_STEP[2] = 50)
      Step 2 → 1: new broken_ma = 'NONE'
      Step 4 → 3: new broken_ma = 'D100'  (CASCADE_MA_BY_STEP[3] = 100)
    """
```

**Edge cases:**
- `daily_mas` may have NaN values if insufficient history — treat NaN as "not broken" (i.e. don't trigger)
- Multi-step jumps are handled naturally by `determine_step_from_close`
- Alert for forward transition: fire only for the DEEPEST MA broken (i.e. the new `broken_ma`)

---

## `src/signals.py`

**Purpose:** Three signal detectors. Each returns a list of alert dicts ready for the dispatcher.

**Public functions:**
```python
def detect_3a_ma_support(
    ticker: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    cascade_step: int,
) -> list[dict]:
    """
    Apply proximity rule INDEPENDENTLY per timeframe group at this cascade step.
    For each timeframe ('D', 'W', 'M') in CASCADE_CHECKS[cascade_step]:
      1. Collect the (timeframe, period) tuples for this timeframe only
      2. Find the highest-period MA whose value is below current price
         (skip MAs that price is already below — they're not the nearest support)
      3. On that one MA: check today's bar — wick touched, close above, volume >= 1.5x 20-day daily avg
      4. If all pass, emit one alert dict AND call db.record_touch() to log this touch

    Returns list of alert dicts. Empty list if nothing fires.
    Note: 3A's touch recording also feeds 3C's counter — order matters in runner.py.
    """

def detect_3b_reclaim(
    ticker: str,
    daily_df: pd.DataFrame,
    cascade_state: dict,
) -> dict | None:
    """
    If cascade_state['broken_ma'] is 'NONE', return None.
    Otherwise:
    1. Get the broken MA's period (e.g. 'D100' → 100)
    2. Compute MA on daily_df
    3. Check today's close: if close > MA, increment streak (DB-backed); if close < MA, reset to 0
    4. If streak hits 7, return an alert dict and trigger cascade de-escalation

    Streak counter is read from and written to DB inside this function via
    db.get_reclaim_streak() and db.set_reclaim_streak(ticker, ma_period, streak, streak_start).
    """

def detect_3c_touch_accumulation(
    ticker: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    cascade_step: int,
) -> list[dict]:
    """
    For each timeframe group at this cascade step, apply the proximity rule
    (same as 3A — per-timeframe winner) and check:
    1. If today's bar qualifies as a touch (low <= MA AND close > MA), this touch
       has already been recorded by detect_3a — do NOT record again.
    2. Query recent touches within TOUCH_WINDOW_DAYS (calendar days) using
       db.get_recent_touches().
    3. If count >= TOUCH_THRESHOLD, emit alert.

    Touch log is NOT cleared after firing. Future touches continue to accumulate;
    new 3C can fire if window count reaches threshold again later.

    MUST run AFTER detect_3a_ma_support() so today's touch is in the log.
    """

def build_alert(
    ticker: str,
    signal_type: str,
    timeframe: str,
    ma_period: int,
    price: float,
    ma_value: float,
    extra: dict,
) -> dict:
    """Constructs a standardised alert dict used downstream by alerter.py."""
```

**Alert dict schema:**
```python
{
    'ticker': 'GOOG',
    'signal_type': 'MA_SUPPORT' | 'RECLAIM' | 'TOUCH_ACCUMULATION',
    'timeframe': 'D' | 'W' | 'M',
    'ma_period': 50 | 100 | 150 | 200,
    'price': 195.43,
    'ma_value': 190.12,
    'volume_ratio': 2.1,           # null for 3B and 3C
    'extra': {                     # signal-specific fields
        # 3A: 'next_resistance_ma': 'W150', 'next_resistance_value': 175.0, 'touch_count': 2
        # 3B: 'streak': 7, 'previous_step': 3, 'new_step': 2
        # 3C: 'touch_count': 3, 'touch_dates': ['2026-05-01', '2026-05-08', '2026-05-14']
    },
    'fired_at': '2026-05-22T21:30:00Z',
}
```

**Edge cases:**
- All detectors must handle insufficient-history gracefully — if MA can't be computed (NaN), skip that MA, don't crash
- Proximity rule applied per-timeframe (NOT global): each of D/W/M gets its own highest-MA-above-price winner
- 3A and 3C can both fire for the same MA on the same day (additive, not exclusive)
- 3A firing also writes to touch_log so 3C sees today's touch — runner.py must call 3A before 3C

---

## `src/alerter.py`

**Purpose:** Format alert dicts as Telegram messages and dispatch them.

**Dependencies:** `requests` (direct calls to Telegram Bot API — lighter than python-telegram-bot)

**Public functions:**
```python
def format_alert(alert: dict) -> str:
    """Format alert dict as Markdown V2 Telegram message per spec §9."""

def send_telegram_message(message: str) -> bool:
    """Sends to TELEGRAM_CHAT_ID via TELEGRAM_BOT_TOKEN. Returns True on success."""

def dispatch_alerts(alerts: list[dict]) -> int:
    """Format and send each alert. Returns count successfully sent."""

def send_test_ping() -> bool:
    """
    Sends a fixed test message to TELEGRAM_CHAT_ID to verify bot credentials and
    connectivity. Used during deployment verification and smoke testing.
    Message text: '✅ MA Alert System — test ping successful.'
    Returns True if message was delivered, False otherwise.
    """
```

**Edge cases:**
- One message per alert; do not batch
- On send failure: log error, continue with remaining alerts (don't abort the run)
- Insert into alert_log table AFTER successful send only
- **Telegram Markdown V2 requires escaping of:** `. ! ( ) [ ] ~ > # + - = | { } $ \``
  This MUST be applied to every dynamic value inserted into the message template —
  ticker names (e.g. `D05:SES` → `D05:SES` — colons are fine but if ticker contains `.`
  it must be escaped), price strings (`$1.23` → `\$1\.23`), date strings, MA values,
  any float. Implement a helper:
  ```python
  def _escape_md2(text: str) -> str:
      for ch in r'\.!()[]~>#+-=|{}$`':
          text = text.replace(ch, f'\\{ch}')
      return text
  ```
  Apply `_escape_md2()` to every variable before inserting into the message string.
  **Failure to escape will cause Telegram to return HTTP 400 and the message will not send.**

---

## `src/health.py`

**Purpose:** Track halted/null-data tickers and emit warnings.

**Public functions:**
```python
def update_after_fetch(ticker: str, fetched_candles: list | None) -> None:
    """Call after every fetch attempt. Updates data_health table."""

def check_and_warn_halts() -> list[str]:
    """
    Iterates data_health. For each ticker with consecutive_failures >= 3
    AND last_warning_sent is null or older than 7 days:
      - Format and send halt warning
      - Update last_warning_sent
    Returns list of tickers warned about.
    """
```

---

## `src/bootstrap.py`

**Purpose:** Initialise state for a new ticker. Backfill OHLCV, replay cascade forward to current state, no alerts fired.

**Public functions:**
```python
def bootstrap_ticker(ticker: str, market: str) -> bool:
    """
    1. Add ticker to watchlist table (if not already there)
    2. Fetch the maximum available Daily OHLCV history from Twelve Data for this ticker.
       Use outputsize=BOOTSTRAP_MAX_CANDLES (5000, Twelve Data's per-request max) to
       retrieve as many daily bars as possible — do not cap at BOOTSTRAP_DAYS. This is
       required so that Weekly and Monthly MAs (which need years of history) have
       sufficient data. BOOTSTRAP_DAYS (250) is only used as the minimum acceptable —
       if fewer than 250 bars are returned, log a warning.
    3. Persist OHLCV to DB
    4. Iterate forward day by day, determining cascade state at each close
       (no alerts fired during this phase — this is silent state-building)
    5. Persist final cascade_state to DB
    6. Populate touch_log entries for any qualifying touches in the last TOUCH_WINDOW_DAYS (21 calendar days)
    7. Populate reclaim_tracker if the stock is currently in a reclaim streak
    Returns True on success.
    """
```

**Edge cases:**
- Idempotent: calling on existing ticker should be a no-op (or refresh)
- Failure modes: bad ticker, no data → log + return False, do not partially populate
- Monthly MA periods (50, 100, 150, 200) require 50–200 months of data respectively.
  If insufficient monthly bars exist to compute a given MA, that MA will be NaN —
  this is expected and handled gracefully by `latest_ma_value()` returning None.
  Do not treat NaN MAs as errors; simply skip them in signal detection.

---

## `src/smoke_test.py`

**Purpose:** Pre-deployment validation against 5 known stocks over the last 90 days.

**Public functions:**
```python
def run_smoke_test(tickers: list[tuple[str, str]] = None) -> dict:
    """
    Default tickers: GOOG, AAPL, NVDA, AXON, CLS (or take from arg).

    For each ticker:
      1. Fetch full history (up to BOOTSTRAP_MAX_CANDLES daily bars) from Twelve Data.
      2. Silent phase: replay days 1 through (total_days - 90) with no alerts recorded.
         This builds cascade state, touch_log, and reclaim_tracker silently — identical
         to bootstrap behaviour.
      3. Alert phase: replay the most recent 90 days day-by-day. Alerts ARE computed
         and recorded in this phase. Do not send to Telegram unless --live-test flag
         is passed; instead append to the in-memory alert_log for the report.
      4. The silent/alert boundary is determined at runtime from the fetched data length,
         not hardcoded. If fewer than 90 days of data exist, log a warning and skip.

    The replay uses TURSO_TEST_DATABASE_URL if set, otherwise an in-memory SQLite.
    Does NOT modify production DB.

    Returns: {'tickers_tested': N, 'alerts_generated': M, 'errors': [...], 'alert_log': [...]}
    """
```

---

## `src/runner.py`

**Purpose:** Single orchestration entry point. Invoked by GitHub Actions cron.

**Public functions:**
```python
def main(market: str) -> int:
    """
    Args: market = 'US' or 'SG' (passed via CLI or env var)

    Pipeline:
    1. Load .env / verify required env vars are set
    2. Initialise DB schema if needed
    3. Load watchlist filtered by market
    4. For each ticker:
        a. Fetch latest Daily OHLCV (outputsize=30 for incremental update)
        b. update_after_fetch in health module
        c. If fetch failed: skip detection
        d. Upsert OHLCV
        e. Compute Daily/Weekly/Monthly DataFrames + MAs
        f. Load cascade_state from DB
        g. Detect forward transition (cascade.detect_forward_transition)
           - If a transition is returned, immediately call db.set_cascade_state()
             to persist the new step and broken_ma BEFORE any signals run.
           - Also call db.set_reclaim_streak(ticker, old_ma_period, streak=0,
             streak_start=None) to clear any stale reclaim streak for the newly broken MA.
        h. Run signal detectors IN THIS EXACT ORDER:
             1. signals.detect_3a_ma_support       — may write to touch_log
             2. signals.detect_3c_touch_accumulation — reads touch_log; must run AFTER 3a
             3. signals.detect_3b_reclaim          — skip entirely if a forward transition
                                                     fired this run (a MA cannot be broken
                                                     and reclaimed on the same day)
           After detect_3b: if it returns an alert (streak hit 7), immediately call
           cascade.apply_reclaim_de_escalation() and persist the new state with
           db.set_cascade_state().
        i. Collect all alerts from this ticker
    5. After all tickers: collect halt warnings (health.check_and_warn_halts)
    6. Dispatch all alerts (alerter.dispatch_alerts)
    7. Persist successfully-sent alerts to alert_log
    8. Run retention pruning (OHLCV via prune_old_ohlcv, touch_log via prune_old_touches)
    9. Log summary: N tickers scanned, M alerts fired, K errors

    Returns exit code: 0 on success, non-zero on hard failure.
    """

if __name__ == '__main__':
    import sys, argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', choices=['US', 'SG'], required=True)
    parser.add_argument('--test-telegram', action='store_true',
                        help='Send a test ping to Telegram and exit (bypasses main pipeline)')
    args = parser.parse_args()
    if args.test_telegram:
        from src.alerter import send_test_ping
        sys.exit(0 if send_test_ping() else 1)
    sys.exit(main(args.market))
```

**Edge cases:**
- Per-ticker failures must not abort the run
- All exceptions logged with full traceback before continuing
- Summary log at end is the heartbeat — useful for GH Actions log inspection
- Order of signal detectors is non-negotiable: 3A → 3C → 3B

---

## `watchlist.yml`

**Format:**
```yaml
tickers:
  - ticker: GOOG
    market: US
    added: 2026-05-22
  - ticker: D05
    market: SG
    added: 2026-05-22
```

Start empty (no entries) — user populates manually. Loaded by `runner.py` and synced to DB watchlist table on each run. New entries trigger `bootstrap.bootstrap_ticker()`.

---

## `tests/`

Mandatory test files:
- `tests/test_cascade.py` — covers all forward and backward transitions including multi-step gap-downs
- `tests/test_signals.py` — covers 3A (wick + close + volume), 3B (streak counting and reset), 3C (touch counting and window pruning)

Use fixtures with hand-crafted OHLCV data, not live API calls. Mock the DB layer using an in-memory SQLite.

Other test files (`test_ma.py`, `test_resample.py`, etc.) are nice-to-have but not blockers.
