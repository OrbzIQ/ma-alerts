# MA Alerts Dashboard

Read-only Streamlit dashboard for the MA Alerts system.

## Local run

From the **project root** (`ma-alerts/`):

```bash
streamlit run dashboard/streamlit_app.py
```

This works from any working directory — the app resolves paths relative to its own location.

## Required environment variables

Create a `.env` file in the project root (same one used by the runner):

```
TURSO_DATABASE_URL=libsql://<your-db>.turso.io
TURSO_AUTH_TOKEN=<your-token>
DASHBOARD_PASSWORD=<choose-a-password>
```

`DASHBOARD_PASSWORD` gates the dashboard UI. The runner variables (`TWELVE_DATA_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are **not** required by the dashboard.

## Streamlit Community Cloud deploy

1. Push this repo to GitHub (dashboard folder must be committed).
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Set **Main file path** to `dashboard/streamlit_app.py`.
4. Under **Advanced settings → Secrets**, add:
   ```toml
   TURSO_DATABASE_URL = "libsql://<your-db>.turso.io"
   TURSO_AUTH_TOKEN   = "<your-token>"
   DASHBOARD_PASSWORD = "<your-password>"
   ```
5. Click **Deploy**.

## Notes

- All DB reads are cached for **5 minutes** (`ttl=300`). Use the **Reload** button in each tab to force a refresh.
- The dashboard is **read-only** — it never writes to Turso.
- Secrets are loaded from `.env` locally and from Streamlit secrets in the cloud; no code change needed between environments.
