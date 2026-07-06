-- MA Alerts — Turso/SQLite Schema
-- Run on first init. All statements use IF NOT EXISTS for idempotency.

CREATE TABLE IF NOT EXISTS watchlist (
    ticker          TEXT NOT NULL PRIMARY KEY,
    market          TEXT NOT NULL CHECK (market IN ('US', 'SG')),
    added_at        DATE NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1     -- boolean
);

CREATE TABLE IF NOT EXISTS ohlcv (
    ticker          TEXT NOT NULL,
    date            DATE NOT NULL,
    open            REAL NOT NULL,
    high            REAL NOT NULL,
    low             REAL NOT NULL,
    close           REAL NOT NULL,
    volume          INTEGER NOT NULL,
    PRIMARY KEY (ticker, date),
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date ON ohlcv(ticker, date DESC);

CREATE TABLE IF NOT EXISTS cascade_state (
    ticker          TEXT NOT NULL PRIMARY KEY,
    current_step    INTEGER NOT NULL CHECK (current_step BETWEEN 1 AND 5),
    broken_ma       TEXT NOT NULL CHECK (broken_ma IN ('NONE', 'D50', 'D100', 'D150', 'D200')),
    last_updated    DATE NOT NULL,
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE TABLE IF NOT EXISTS reclaim_tracker (
    ticker              TEXT NOT NULL,
    timeframe           TEXT NOT NULL DEFAULT 'D' CHECK (timeframe IN ('D', 'W', 'M')),
    ma_period           INTEGER NOT NULL,            -- e.g. 50, 100, 150, 200
    consecutive_closes  INTEGER NOT NULL DEFAULT 0,
    streak_start        DATE,                        -- nullable; null when streak == 0
    last_bar_date       TEXT,                        -- last bar date counted (prevents W/M double-count)
    was_broken          INTEGER NOT NULL DEFAULT 0,   -- 1 = price has closed <= MA since last reset (gates W/M reclaim counting)
    last_updated        DATE NOT NULL,
    PRIMARY KEY (ticker, timeframe, ma_period),
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE TABLE IF NOT EXISTS touch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    timeframe       TEXT NOT NULL CHECK (timeframe IN ('D', 'W', 'M')),
    ma_period       INTEGER NOT NULL,
    touch_date      DATE NOT NULL,
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker),
    UNIQUE (ticker, timeframe, ma_period, touch_date)   -- prevent duplicate touch records
);

CREATE INDEX IF NOT EXISTS idx_touch_lookup
  ON touch_log(ticker, timeframe, ma_period, touch_date DESC);

CREATE TABLE IF NOT EXISTS alert_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    signal_type     TEXT NOT NULL,
    timeframe       TEXT CHECK (timeframe IN ('D', 'W', 'M', NULL)),
    ma_period       INTEGER,
    price_at_fire   REAL,
    ma_value        REAL,
    volume_ratio    REAL,
    extra_json      TEXT,                         -- signal-specific fields as JSON
    bar_date        TEXT,                         -- ISO date of the bar that triggered the signal
    fired_at        TEXT NOT NULL,                -- UTC ISO 8601
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE INDEX IF NOT EXISTS idx_alert_log_ticker_time
  ON alert_log(ticker, fired_at DESC);

CREATE INDEX IF NOT EXISTS idx_alert_log_dedup
  ON alert_log(ticker, ma_period, timeframe, signal_type);

CREATE TABLE IF NOT EXISTS data_health (
    ticker                  TEXT NOT NULL PRIMARY KEY,
    last_success            DATE,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    last_warning_sent       DATE,                  -- nullable
    FOREIGN KEY (ticker) REFERENCES watchlist(ticker)
);

CREATE TABLE IF NOT EXISTS api_usage (
    date            TEXT NOT NULL PRIMARY KEY,     -- 'YYYY-MM-DD' UTC
    credits_used    INTEGER NOT NULL DEFAULT 0
);
