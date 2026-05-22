# Verification Checklist

Tick every item before declaring the build complete. If any item is impossible or unclear, stop and ask the user.

## 1. Project structure
- [ ] Folder structure matches the layout in `00_COWORK_BRIEF.md` exactly
- [ ] `requirements.txt` matches `06_REQUIREMENTS.txt`
- [ ] `.env.example` matches `05_ENV_TEMPLATE.txt`
- [ ] `.gitignore` exists and excludes: `.venv/`, `.env`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `data/`, `*.db`
- [ ] `README.md` exists with: setup instructions, env var list, how to run smoke test, how to deploy to GH Actions
- [ ] `pyproject.toml` or equivalent configures the project

## 2. Configuration module
- [ ] `src/config.py` exposes all constants listed in `02_BUILD_MANIFEST.md`
- [ ] No constants are duplicated in other modules — single source of truth

## 3. Database layer
- [ ] `src/db.py` connects to Turso using env vars
- [ ] `init_schema()` runs DDL from `03_SCHEMA.sql` successfully
- [ ] Re-running `init_schema()` does not error (idempotent)
- [ ] All public functions in the manifest are implemented with matching signatures

## 4. Data fetching + processing
- [ ] `fetcher.py` handles a single ticker correctly (verified with a test fixture)
- [ ] `fetcher.py` returns None (not raise) on HTTP errors
- [ ] SG ticker symbol mapping appends `:SES` suffix (Twelve Data SGX convention)
- [ ] `fetcher.py` `outputsize` parameter is passed through to Twelve Data API
- [ ] `resample.py` weekly resample uses Friday-ending bars
- [ ] `resample.py` monthly resample uses month-end bars
- [ ] `ma.py` computes SMA correctly (verified with hand-calculated fixture)
- [ ] Bootstrap fetches maximum available history (outputsize=BOOTSTRAP_MAX_CANDLES=5000), not capped at 250 days
- [ ] Monthly MA columns are NaN when insufficient history exists — this does not raise an error

## 5. Cascade state machine
- [ ] `determine_step_from_close` returns correct step for all 5 buckets
- [ ] Multi-step gap-down test passes: close below D50 + D100 in one bar → step 3
- [ ] De-escalation reduces step by exactly 1
- [ ] `tests/test_cascade.py` exists and passes with `pytest`

## 6. Signal detectors
- [ ] 3A fires when wick + close + volume conditions all met
- [ ] 3A does NOT fire if volume < 1.5× 20-day daily avg
- [ ] 3A proximity rule: only the highest MA above price in a group fires
- [ ] 3B streak increments on close above broken MA
- [ ] 3B streak resets to 0 on close below broken MA
- [ ] 3B fires alert and triggers de-escalation when streak hits 7
- [ ] 3C records every qualifying touch
- [ ] 3C fires when 3 touches accumulate within rolling 15-day window
- [ ] 3C log is NOT cleared after firing — additional touches continue accumulating
- [ ] `tests/test_signals.py` exists and passes with `pytest`

## 7. Alert dispatch
- [ ] Each alert produces exactly one Telegram message (no batching)
- [ ] Message format matches spec §9 templates exactly
- [ ] Failed sends do not abort the run
- [ ] Successfully sent alerts are persisted to `alert_log`
- [ ] `send_test_ping()` sends a message to TELEGRAM_CHAT_ID and returns True
- [ ] Markdown V2 escaping helper `_escape_md2()` is applied to all dynamic values (tickers, prices, dates, MA values)
- [ ] SG ticker with dot (e.g. `D05.SI` if used) does not cause HTTP 400 from Telegram

## 8. Halt detection
- [ ] `consecutive_failures` increments on fetch failure
- [ ] `consecutive_failures` resets to 0 on next successful fetch
- [ ] Halt warning fires only after 3 consecutive failures
- [ ] Halt warning does not re-fire daily — uses `last_warning_sent` to throttle (max once per 7 days per ticker)

## 9. Bootstrap
- [ ] New ticker bootstrap fetches 250 days
- [ ] Bootstrap replays cascade forward to current state correctly
- [ ] NO alerts fired during bootstrap (verify this is true even if conditions are met)
- [ ] Last-15-days touches are populated in touch_log
- [ ] Active reclaim streak is populated in reclaim_tracker
- [ ] Calling bootstrap on an existing ticker is a no-op or safe refresh

## 10. Smoke test
- [ ] `smoke_test.py` runs end-to-end without errors on 5 test tickers
- [ ] Smoke test uses a separate test database (not prod)
- [ ] Smoke test produces a report listing all alerts that would have fired over the 90-day replay
- [ ] User can manually inspect the report and chart-check 3 sample alerts

## 11. Orchestration
- [ ] `runner.py --market US` and `runner.py --market SG` both run end-to-end
- [ ] `runner.py --test-telegram` sends a ping and exits
- [ ] Forward transition triggers immediate `db.set_cascade_state()` call before signals run
- [ ] Reclaim streak is cleared via `set_reclaim_streak(..., streak=0)` on new forward transition
- [ ] Signal detectors run in exact order: 3A → 3C → 3B
- [ ] 3B is skipped on days when a forward transition fires
- [ ] After successful 3B confirmation, new cascade state is persisted via `set_cascade_state()`
- [ ] Per-ticker exceptions are caught and logged; don't abort the run
- [ ] Summary log at end shows: tickers scanned, alerts fired, errors

## 12. GitHub Actions workflow
- [ ] `.github/workflows/daily_scan.yml` matches `04_WORKFLOW_TEMPLATE.yml`
- [ ] Workflow references all required secrets
- [ ] Both cron triggers configured (US + SG)
- [ ] `workflow_dispatch` enabled for manual testing

## 13. Documentation
- [ ] `README.md` covers: install, run locally, run smoke test, deploy to GH Actions, how to add tickers, how to read alerts
- [ ] Inline docstrings on all public functions

## 14. Cleanup
- [ ] No hardcoded credentials anywhere
- [ ] No `print()` statements (use logging)
- [ ] No `TODO` or `FIXME` left in committed code
- [ ] No unused imports

---

## Final manual checks (for the user, not Cowork)

After Cowork declares done, the user will verify:
1. Run `pytest tests/` locally — all tests pass
2. Run `python -m src.smoke_test` — smoke test produces a report
3. Eyeball the smoke test report against chart history of 1-2 tickers
4. Push to GitHub
5. Add the 5 secrets to repo settings
6. Trigger workflow manually (`workflow_dispatch`) to confirm it runs in cloud
7. Wait for the next cron trigger and confirm alerts arrive in Telegram
