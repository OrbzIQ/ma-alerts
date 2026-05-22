# Cowork Build Brief — MA-Based Stock Entry Alert System (v1)

## Your role
You are building a Python application from scratch in this folder. The full specification is in this folder. Read every file in this folder before writing any code. Do not invent requirements — everything you need is here.

## What you are building
A once-daily Python application that:
1. Fetches Daily OHLCV for a list of US + SG stocks via the Twelve Data API
2. Resamples Daily into Weekly + Monthly bars locally
3. Computes Moving Averages (50, 100, 150, 200) on each timeframe
4. Tracks a per-stock cascade state machine
5. Detects three signal types and dispatches Telegram alerts
6. Runs unattended on GitHub Actions cron, persisting state to Turso (hosted SQLite)

## Reference files in this folder (read in this order)
1. `00_COWORK_BRIEF.md` — this file
2. `01_PROJECT_SPEC.md` — full functional spec, the source of truth for logic
3. `02_BUILD_MANIFEST.md` — file-by-file requirements with function-level detail
4. `03_SCHEMA.sql` — Turso/SQLite schema DDL
5. `04_WORKFLOW_TEMPLATE.yml` — GitHub Actions cron template
6. `05_ENV_TEMPLATE.txt` — environment variable template
7. `06_REQUIREMENTS.txt` — Python dependencies
8. `07_VERIFICATION_CHECKLIST.md` — acceptance criteria

## Build order (strict)
Build, test, and verify each module before moving to the next. Do not skip ahead.

1. Project scaffolding (`pyproject.toml`, `requirements.txt`, `.env.example`, `.gitignore`, `README.md`)
2. `src/config.py` — all tunables in one place
3. `src/db.py` — Turso client wrapper, schema init, retention pruning
4. `src/fetcher.py` — Twelve Data Daily OHLCV client
5. `src/resample.py` — Daily → Weekly + Monthly
6. `src/ma.py` — MA computation
7. `src/cascade.py` — state machine + transitions
8. `src/signals.py` — three detectors (3A, 3B, 3C)
9. `src/alerter.py` — Telegram dispatch + formatting (includes `send_test_ping()` for deployment verification)
10. `src/health.py` — halt detection + warning logic
11. `src/bootstrap.py` — 250-day backfill for new tickers
12. `src/smoke_test.py` — 5-stock × 90-day replay
13. `src/runner.py` — orchestration entry point
14. `.github/workflows/daily_scan.yml` — cron schedule
15. `watchlist.yml` — empty starter file with example structure

## Conventions
- **Python 3.11+**, type hints everywhere
- **Imports**: standard lib → third-party → local, grouped, sorted
- **Logging**: use `logging` module, not `print`. Set up a central logger in `runner.py`. Log level controlled by env var `LOG_LEVEL` (default `INFO`)
- **Time zones**: store all timestamps as UTC ISO 8601. Convert only at display time
- **Errors**: fail loudly on logic errors, fail gracefully on network/data errors (skip ticker, log, continue)
- **No silent fallbacks**: if Twelve Data returns malformed data, raise and log; do not guess
- **No hardcoded credentials**: all secrets via env vars, loaded with `python-dotenv` for local runs
- **Tests**: pytest, in `tests/` directory, mirroring `src/` structure. Unit tests for `cascade.py` and `signals.py` are mandatory. Other modules: tests are nice-to-have but not required for v1.

### Live credentials are available — test as you build
A `.env` file with real API keys is present in the project folder. Use it. After building each module, test it against the real API before moving to the next — do not defer all testing to the smoke test at the end. Specifically:
- After `db.py`: run `init_schema()` and confirm tables appear in the Turso DB
- After `fetcher.py`: fetch one real ticker (e.g. GOOG) and print the result
- After `alerter.py`: call `send_test_ping()` and confirm message arrives in Telegram
- After `runner.py`: run `python -m src.runner --market US` end-to-end before smoke test

If any module fails its live test, stop and fix before proceeding.

## Hard constraints — do NOT do these
- **Do not push to GitHub.** Build locally only. The user will review and push manually.
- **Do not generate or use real API keys.** Use placeholder strings in `.env.example`. The user will fill in actual values.
- **Do not call any external APIs during build.** All API code must be testable without live credentials. Use mock data fixtures for tests.
- **Do not modify files outside this folder.**
- **Do not install global packages.** Use a `.venv` inside this folder. Document in README.
- **Do not implement the Momentum signal (3D) or the dashboard.** Deferred to v2. If you find yourself writing code for them, stop.

## Acceptance — when you are done
You are done when every box in `07_VERIFICATION_CHECKLIST.md` can be ticked. Run through the checklist yourself before declaring completion. If any item is unclear or impossible, stop and ask the user.

## When in doubt
If any requirement is ambiguous after reading all 8 reference files, do NOT guess. Stop and ask the user a specific clarifying question. The user has been deeply involved in the spec design and would rather answer one more question than have you build the wrong thing.
