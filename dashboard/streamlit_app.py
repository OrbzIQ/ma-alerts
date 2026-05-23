# Path setup — MUST be first executable code (FIX-1)
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml
from dotenv import load_dotenv

# Project root (resolves correctly regardless of CWD)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# src imports — after sys.path.insert (FIX-1)
from src.config import MA_PERIODS_DAILY  # noqa: E402
from src.ma import compute_ma  # noqa: E402
from src.resample import resample_to_weekly, resample_to_monthly  # noqa: E402


# ---------------------------------------------------------------------------
# Secrets helper — checks st.secrets first (Streamlit Cloud), falls back to .env
# ---------------------------------------------------------------------------

def get_secret(key: str) -> str:
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, "")


# ---------------------------------------------------------------------------
# DB: sync wrapper — plain HTTPS, fresh request per call (FIX-2)
#
# Uses the same Turso HTTP pipeline approach as V1 db.py so it works on all
# Python versions (including 3.14 where libsql_client's aiohttp breaks).
# No @st.cache_resource pool. No async outside this function.
# ---------------------------------------------------------------------------

def run_query(sql: str, args=None) -> list[dict]:
    """Execute SQL against Turso via plain HTTPS. Fresh request per call."""
    import requests as _req

    base_url = get_secret("TURSO_DATABASE_URL").replace("libsql://", "https://").rstrip("/")
    token = get_secret("TURSO_AUTH_TOKEN")

    stmt: dict = {"sql": sql}
    if args:
        typed: list = []
        for v in args:
            if v is None:
                typed.append({"type": "null", "value": None})
            elif isinstance(v, bool):
                typed.append({"type": "integer", "value": str(int(v))})
            elif isinstance(v, int):
                typed.append({"type": "integer", "value": str(v)})
            elif isinstance(v, float):
                typed.append({"type": "float", "value": v})
            else:
                typed.append({"type": "text", "value": str(v)})
        stmt["args"] = typed

    payload = {"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]}
    resp = _req.post(
        f"{base_url}/v2/pipeline",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()

    results = resp.json().get("results", [])
    if not results:
        return []

    result = results[0]
    if result.get("type") == "error":
        raise RuntimeError(f"Turso SQL error: {result.get('error', result)}")

    rs = result.get("response", {}).get("result", {})
    cols = [c["name"] for c in rs.get("cols", [])]
    rows: list[dict] = []
    for row in rs.get("rows", []):
        parsed = []
        for cell in row:
            t, v = cell.get("type", "text"), cell.get("value")
            if t == "null" or v is None:
                parsed.append(None)
            elif t == "integer":
                parsed.append(int(v))
            elif t == "float":
                parsed.append(float(v))
            else:
                parsed.append(v)
        rows.append(dict(zip(cols, parsed)))
    return rows


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Python-derived — NOT a DB column (spec §B.5)
BROKEN_MA_BY_STEP: dict[int, str] = {
    1: "NONE", 2: "D50", 3: "D100", 4: "D150", 5: "D200"
}

# UI label → DB value mapping for signal_type filter
SIGNAL_LABEL_TO_DB: dict[str, str] = {
    "3A": "MA_SUPPORT",
    "3B": "RECLAIM",
    "3C": "TOUCH_ACCUMULATION",
    "3D": "3D",
}
DB_TO_SIGNAL_LABEL: dict[str, str] = {v: k for k, v in SIGNAL_LABEL_TO_DB.items()}

# UI label → DB value mapping for timeframe filter
TF_LABEL_TO_DB: dict[str, str] = {"Daily": "D", "Weekly": "W", "Monthly": "M"}

# MA line colors — keyed by period number (D50/W50/M50 all blue, spec §B.5)
MA_COLORS: dict[int, str] = {
    20: "cyan",
    50: "royalblue",
    100: "mediumpurple",
    150: "orange",
    200: "crimson",
}

# Alert marker colors — keyed by DB signal_type value
ALERT_MARKER_COLORS: dict[str, str] = {
    "MA_SUPPORT": "yellow",
    "RECLAIM": "limegreen",
    "TOUCH_ACCUMULATION": "orange",
    "3D": "cyan",
}


# ---------------------------------------------------------------------------
# Cached data loaders — ALL decorated with @st.cache_data(ttl=300)
# ALL DB access goes through run_query() — no exceptions (FIX-2)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_watchlist() -> pd.DataFrame:
    """Load from watchlist.yml via _PROJECT_ROOT. NOT from a DB table (FIX-4)."""
    path = os.path.join(_PROJECT_ROOT, "watchlist.yml")
    with open(path) as f:
        data = yaml.safe_load(f)
    return pd.DataFrame(
        [{"ticker": t["ticker"], "market": t.get("market", "US")} for t in data["tickers"]]
    )


@st.cache_data(ttl=3600)
def _has_volume_ratio_column() -> bool:
    """Check if alert_log has a volume_ratio column. Cached 1h — run once."""
    rows = run_query("PRAGMA table_info(alert_log)")
    return any(r.get("name") == "volume_ratio" for r in rows)


@st.cache_data(ttl=300)
def load_overview() -> pd.DataFrame:
    rows = run_query("""
        SELECT cs.ticker, cs.current_step,
               latest.signal_type  AS last_signal,
               latest.fired_at     AS last_fired_at,
               latest.price_at_fire AS last_price
        FROM cascade_state cs
        LEFT JOIN (
            SELECT ticker, signal_type, fired_at, price_at_fire,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY fired_at DESC) AS rn
            FROM alert_log
        ) latest ON latest.ticker = cs.ticker AND latest.rn = 1
    """)
    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["ticker", "current_step", "last_signal", "last_fired_at", "last_price"]
    )

    # Merge with watchlist for market column (FIX-4)
    wl = load_watchlist()[["ticker", "market"]]
    df = wl.merge(df, on="ticker", how="left")

    today = date.today()

    def _days_since(fired_at) -> int | None:
        if not fired_at:
            return None
        try:
            return (today - date.fromisoformat(str(fired_at)[:10])).days
        except Exception:
            return None

    # Derive broken_ma from Python dict — NOT a DB column (spec §B.5)
    df["broken_ma"] = df["current_step"].apply(
        lambda s: BROKEN_MA_BY_STEP.get(int(s), "—") if pd.notna(s) and s is not None else "—"
    )
    df["last_signal"] = df["last_signal"].map(
        lambda s: DB_TO_SIGNAL_LABEL.get(str(s), str(s)) if pd.notna(s) and s else "—"
    )
    df["days_since"] = df["last_fired_at"].apply(_days_since)  # calendar days
    df["last_fired_at"] = df["last_fired_at"].apply(
        lambda s: str(s)[:10] if pd.notna(s) and s else "—"
    )
    df["last_price"] = df["last_price"].apply(
        lambda p: f"${float(p):.2f}" if pd.notna(p) and p is not None else "—"
    )
    df["current_step"] = df["current_step"].apply(
        lambda s: int(s) if pd.notna(s) and s is not None else 1
    )

    # Sort: step desc, days_since asc (None last) — spec §B.5
    df = df.sort_values(
        ["current_step", "days_since"],
        ascending=[False, True],
        na_position="last",
    ).reset_index(drop=True)

    return df


@st.cache_data(ttl=300)
def load_alert_log(
    tickers: tuple,       # UI ticker strings — already DB values
    signal_types: tuple,  # UI labels e.g. ("3A", "3D")
    timeframes: tuple,    # UI labels e.g. ("Daily",)
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    # Map UI labels → DB values
    db_signals = tuple(SIGNAL_LABEL_TO_DB[s] for s in signal_types if s in SIGNAL_LABEL_TO_DB)
    db_tfs = tuple(TF_LABEL_TO_DB[t] for t in timeframes if t in TF_LABEL_TO_DB)

    # Check for volume_ratio column (spec §B.4)
    vol_col = ", volume_ratio" if _has_volume_ratio_column() else ""
    # TODO: volume_ratio missing from V1 schema — omit if not present

    sql = (
        f"SELECT fired_at, ticker, signal_type, timeframe, ma_period, "
        f"price_at_fire AS price, ma_value{vol_col} "
        f"FROM alert_log "
        f"WHERE fired_at >= ? AND fired_at < ?"
    )
    args: list = [date_from.isoformat(), (date_to + timedelta(days=1)).isoformat()]

    if tickers:
        sql += f" AND ticker IN ({','.join('?' * len(tickers))})"
        args.extend(tickers)
    if db_signals:
        sql += f" AND signal_type IN ({','.join('?' * len(db_signals))})"
        args.extend(db_signals)
    if db_tfs:
        sql += f" AND timeframe IN ({','.join('?' * len(db_tfs))})"
        args.extend(db_tfs)

    sql += " ORDER BY fired_at DESC"

    rows = run_query(sql, args)
    df = pd.DataFrame(rows) if rows else pd.DataFrame()

    # Map DB signal_type values to user-friendly labels for display
    if not df.empty and "signal_type" in df.columns:
        df["signal_type"] = df["signal_type"].map(
            lambda s: DB_TO_SIGNAL_LABEL.get(str(s), str(s))
        )
    return df


@st.cache_data(ttl=300)
def load_ohlcv(ticker: str, days: int = 250) -> pd.DataFrame:
    rows = run_query(
        "SELECT date, open, high, low, close, volume FROM ohlcv "
        "WHERE ticker = ? ORDER BY date DESC LIMIT ?",
        [ticker, days],
    )
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()  # oldest-first for resample + MA
    return df


@st.cache_data(ttl=300)
def load_alert_markers(ticker: str) -> pd.DataFrame:
    """All alerts for the per-ticker chart — unfiltered by date."""
    rows = run_query(
        "SELECT fired_at, signal_type, price_at_fire AS price FROM alert_log "
        "WHERE ticker = ? ORDER BY fired_at ASC",
        [ticker],
    )
    return pd.DataFrame(rows) if rows else pd.DataFrame()


@st.cache_data(ttl=300)
def load_data_health() -> pd.DataFrame:
    rows = run_query(
        "SELECT ticker, last_success, consecutive_failures, last_warning_sent "
        "FROM data_health"
    )
    return pd.DataFrame(rows) if rows else pd.DataFrame()


@st.cache_data(ttl=300)
def load_last_scan_timestamp() -> str:
    # Primary: MAX(last_success) from data_health
    rows = run_query("SELECT MAX(last_success) AS ts FROM data_health")
    ts = rows[0]["ts"] if rows else None
    if not ts:
        # Fallback: MAX(fired_at) from alert_log
        rows = run_query("SELECT MAX(fired_at) AS ts FROM alert_log")
        ts = rows[0]["ts"] if rows else None
    if not ts:
        return "Last scan: unknown"
    try:
        clean = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return f"Last scan: {dt.strftime('%Y-%m-%d %H:%M')} UTC"
    except Exception:
        return f"Last scan: {ts}"


# ---------------------------------------------------------------------------
# Auth — password gate renders FIRST; tabs only after authed (spec §B.6)
# ---------------------------------------------------------------------------

def check_password() -> bool:
    if st.session_state.get("authed"):
        return True
    pw = st.text_input("Password", type="password", key="_pw")
    if pw and pw == get_secret("DASHBOARD_PASSWORD"):
        st.session_state["authed"] = True
        st.rerun()
    elif pw:
        st.error("Incorrect password")
    return False


# ---------------------------------------------------------------------------
# Tab: Overview
# ---------------------------------------------------------------------------

def _style_overview(df: pd.DataFrame):
    """Red row if step >= 4. Green row if step == 1. Spec §B.5."""
    def _row(row):
        try:
            step = int(row.get("current_step", 1) or 1)
        except (ValueError, TypeError):
            step = 1
        if step >= 4:
            return ["background-color: #4a1a1a"] * len(row)
        if step == 1:
            return ["background-color: #1a3d1a"] * len(row)
        return [""] * len(row)
    return df.style.apply(_row, axis=1)


def render_overview() -> None:
    df = load_overview()
    if df.empty:
        st.info("No cascade state data yet. Run the scanner first.")
        return

    display_cols = [
        "ticker", "market", "current_step", "broken_ma",
        "last_signal", "last_fired_at", "last_price", "days_since",
    ]
    display_df = df[[c for c in display_cols if c in df.columns]]
    st.dataframe(_style_overview(display_df), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab: Alert Log
# ---------------------------------------------------------------------------

def render_alert_log() -> None:
    wl = load_watchlist()
    all_tickers = sorted(wl["ticker"].tolist())

    col1, col2, col3 = st.columns(3)
    with col1:
        # CRITICAL: tuple() before cached function — lists are unhashable (spec FIX BUG-1)
        t = tuple(st.multiselect("Tickers", options=all_tickers, key="al_t"))
    with col2:
        s = tuple(st.multiselect("Signal", options=list(SIGNAL_LABEL_TO_DB.keys()), key="al_s"))
    with col3:
        tf = tuple(st.multiselect("Timeframe", options=list(TF_LABEL_TO_DB.keys()), key="al_tf"))

    col4, col5 = st.columns(2)
    with col4:
        date_from = st.date_input("From", value=date.today() - timedelta(days=90), key="al_fd")
    with col5:
        date_to = st.date_input("To", value=date.today(), key="al_td")

    df = load_alert_log(
        tickers=t,
        signal_types=s,
        timeframes=tf,
        date_from=date_from,
        date_to=date_to,
    )

    if df.empty:
        st.info("No alerts found for the selected filters.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(f"{len(df)} alert(s)")


# ---------------------------------------------------------------------------
# Tab: Per-Ticker Chart
# ---------------------------------------------------------------------------

def render_per_ticker() -> None:
    wl = load_watchlist()
    ticker = st.selectbox("Ticker", options=wl["ticker"].tolist(), key="pt_ticker")
    tf_label = st.radio(
        "Timeframe", options=["Daily", "Weekly", "Monthly"], horizontal=True, key="pt_tf"
    )

    daily_df = load_ohlcv(ticker, 250)
    if daily_df.empty:
        st.warning(f"No OHLCV data for {ticker}.")
        return

    # Resample and select MA periods — W20/M20 hidden per spec §B.5
    if tf_label == "Weekly":
        chart_df = resample_to_weekly(daily_df)
        periods = [50, 100, 150, 200]
        tf_prefix = "W"
    elif tf_label == "Monthly":
        chart_df = resample_to_monthly(daily_df)
        periods = [50, 100, 150, 200]
        tf_prefix = "M"
    else:  # Daily
        chart_df = daily_df.copy()
        periods = [20, 50, 100, 150, 200]
        tf_prefix = "D"

    compute_ma(chart_df, periods)

    fig = go.Figure()

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=chart_df.index,
        open=chart_df["open"],
        high=chart_df["high"],
        low=chart_df["low"],
        close=chart_df["close"],
        name=ticker,
        showlegend=False,
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
    ))

    # MA overlays — color keyed to period number, NOT timeframe (spec §B.5)
    for period in periods:
        col = f"ma_{period}"
        if col not in chart_df.columns:
            continue
        fig.add_trace(go.Scatter(
            x=chart_df.index,
            y=chart_df[col],
            mode="lines",
            name=f"{tf_prefix}{period}",
            line=dict(color=MA_COLORS.get(period, "white"), width=1.2),
        ))

    # Alert markers — y=price, triangle-up, 12px (spec §B.5)
    markers_df = load_alert_markers(ticker)
    if not markers_df.empty:
        markers_df["fired_at"] = pd.to_datetime(markers_df["fired_at"])
        for db_sig, color in ALERT_MARKER_COLORS.items():
            sig_df = markers_df[markers_df["signal_type"] == db_sig].copy()
            if sig_df.empty:
                continue
            label = DB_TO_SIGNAL_LABEL.get(db_sig, db_sig)
            fig.add_trace(go.Scatter(
                x=sig_df["fired_at"],
                y=sig_df["price"].astype(float),
                mode="markers",
                name=label,
                marker=dict(symbol="triangle-up", size=12, color=color),
                hovertemplate=f"{label} — %{{x|%Y-%m-%d}}<extra></extra>",
            ))

    fig.update_layout(
        title=f"{ticker} — {tf_label} ({len(chart_df)} bars)",
        xaxis_rangeslider_visible=False,
        height=600,
        template="plotly_dark",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=20, t=60, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Tab: Data Health
# ---------------------------------------------------------------------------

def _style_data_health(df: pd.DataFrame):
    """Red if failures > 0. Yellow if last_success > 24h ago. Spec §B.5."""
    now = datetime.now(timezone.utc)

    def _row(row):
        try:
            failures = int(row.get("consecutive_failures") or 0)
        except (ValueError, TypeError):
            failures = 0
        if failures > 0:
            return ["background-color: #4a1a1a"] * len(row)

        last_ok = row.get("last_success")
        if last_ok:
            try:
                dt = datetime.fromisoformat(str(last_ok).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() > 86400:
                    return ["background-color: #3d3a1a"] * len(row)
            except Exception:
                pass
        return [""] * len(row)

    return df.style.apply(_row, axis=1)


def render_data_health() -> None:
    df = load_data_health()
    if df.empty:
        st.info("No data health records yet.")
        return
    st.dataframe(_style_data_health(df), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Main — password gate FIRST, tabs only after authed (spec §B.6)
# NO auto-refresh checkbox (spec §B.6 — cache TTL handles freshness)
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="MA Alerts", layout="wide")
    st.title("MA Alerts Dashboard")

    if not check_password():
        return

    st.caption(load_last_scan_timestamp())

    tabs = st.tabs(["Overview", "Alert Log", "Per-Ticker Chart", "Data Health"])
    with tabs[0]:
        render_overview()
    with tabs[1]:
        render_alert_log()
    with tabs[2]:
        render_per_ticker()
    with tabs[3]:
        render_data_health()


if __name__ == "__main__":
    main()
