# MA Alert System — v1

Daily scanner that fires Telegram alerts when watchlist stocks hit Moving Average support, reclaim, or accumulation conditions across Daily, Weekly, and Monthly timeframes.

Runs once per trading day via GitHub Actions cron. State is persisted to Turso (hosted SQLite). No dashboard in v1 — alerts only.

---

## Prerequisites

Before running locally or deploying, you need:

| Service | Purpose | Sign-up |
|---|---|---|
| [Twelve Data](https://twelvedata.com) | OHLCV market data | Free tier, 800 calls/day |
| [Turso](https://turso.tech) | Hosted SQLite state storage | Free tier |
| Telegram Bot | Alert delivery | via @BotFather |

**Create Turso DB:**
```bash
turso db create ma-alerts
turso db show ma-alerts --url          # → TURSO_DATABASE_URL
turso db tokens create ma-alerts       # → TURSO_AUTH_TOKEN
```

**Create Telegram bot:** message `@BotFather` on Telegram → `/newbot`. Then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging your bot once to find your chat ID.

---

## Local setup

```bash
# 1. Clone / enter the project folder
cd ma-alerts

# 2. Create virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and fill in all five secrets
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TWELVE_DATA_API_KEY` | Yes | Twelve Data API key |
| `TURSO_DATABASE_URL` | Yes | `libsql://your-db.turso.io` |
| `TURSO_AUTH_TOKEN` | Yes | Turso auth token |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Your Telegram chat ID |
| `TELEGRAM_TEST_CHAT_ID` | No | Separate chat for smoke test alerts |
| `TURSO_TEST_DATABASE_URL` | No | Separate Turso DB for smoke test |
| `TURSO_TEST_AUTH_TOKEN` | No | Auth token for test DB |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` (default: `INFO`) |

---

## Running locally

**Test Telegram connectivity:**
```bash
python -m src.runner --market US --test-telegram
```

**Run a full US scan:**
```bash
python -m src.runner --market US
```

**Run a full SG scan:**
```bash
python -m src.runner --market SG
```

---

## Adding tickers to the watchlist

Edit `watchlist.yml`:
```yaml
tickers:
  - ticker: GOOG
    market: US
    added: 2026-05-22
  - ticker: D05
    market: SG
    added: 2026-05-22
```

On next run, new tickers are automatically bootstrapped (250-day backfill, silent cascade replay).

**Note for SG tickers:** Enter the base symbol (e.g. `D05`). The system appends `:SES` automatically when calling Twelve Data.

---

## Running the smoke test

Runs a 90-day replay on 5 known US tickers against an isolated test database. Does **not** modify production data.

```bash
python -m src.smoke_test
```

If `TURSO_TEST_DATABASE_URL` is set, it uses that Turso DB. Otherwise, it uses a local temporary SQLite file.

Output: a JSON/text report listing all alerts that would have fired during the 90-day window. Inspect 2–3 sample alerts against a chart before deploying.

To also send sample alerts to Telegram (requires `TELEGRAM_TEST_CHAT_ID`):
```bash
python -m src.smoke_test --live-test
```

---

## Running tests

```bash
pytest tests/
```

---

## Deploying to GitHub Actions

1. Push this repo to GitHub
2. Add the following secrets to **Settings → Secrets and variables → Actions**:
   - `TWELVE_DATA_API_KEY`
   - `TURSO_DATABASE_URL`
   - `TURSO_AUTH_TOKEN`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Go to **Actions → Daily MA Alert Scan → Run workflow** to trigger manually
4. Confirm alerts arrive in Telegram
5. Cron triggers automatically from that point:
   - US market: 22:30 UTC weekdays (after NYSE/NASDAQ close)
   - SG market: 10:30 UTC weekdays (after SGX close)

---

## Signal types

| Signal | Trigger |
|---|---|
| **3A — MA Support** | Daily close: wick touched MA, closed above, volume ≥ 1.5× 20-day avg |
| **3B — MA Reclaim** | 7 consecutive closes above a previously broken Daily MA |
| **3C — Touch Accumulation** | 3 qualifying wick+close touches within a rolling 15-trading-day window |

---

## Cascade steps

The system tracks a per-stock cascade step (1–5) based on which Daily MAs price has broken:

| Step | Condition | MAs evaluated |
|---|---|---|
| 1 | Above D50 | D50 |
| 2 | Below D50 | D100, W50, W100, M50 |
| 3 | Below D100 | D150, W50, W100, M50, M100 |
| 4 | Below D150 | D200, W100, W150, W200, M100, M150 |
| 5 | Below D200 | W100, W150, W200, M100, M150, M200 |

---

## Architecture

```
runner.py          ← orchestration entry point (GitHub Actions calls this)
├── fetcher.py     ← Twelve Data API client
├── resample.py    ← Daily → Weekly/Monthly bars
├── ma.py          ← SMA computation
├── cascade.py     ← state machine transitions
├── signals.py     ← 3A / 3B / 3C detectors
├── alerter.py     ← Telegram dispatch + formatting
├── health.py      ← halt/null-data detection
├── bootstrap.py   ← new ticker backfill
└── db.py          ← Turso/SQLite persistence layer
```

---

## Not in v1

- Momentum signal (3D)
- FastAPI dashboard
- Holiday calendar handling
- Multi-user support
- HK / JP markets
