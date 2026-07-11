"""
test_alerter.py -- Unit tests for src/alerter.py's dispatch_alerts(), focused
on the FIX D pre-dispatch sanity gate integration.

Uses a temporary local SQLite file (not Turso) and mocks Telegram sends so
tests run without network access.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import date, timedelta
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def isolated_db():
    """Mirrors tests/test_signals.py's fixture."""
    db_file = os.path.join(tempfile.gettempdir(), f"ma_test_alerter_{uuid.uuid4().hex}.db")
    db_url = f"file:{db_file}"

    import src.db as db
    db.close_connection()

    original_make = db._make_backend

    def _override_make(url=None, auth_token=None):
        return original_make(db_url, "")

    db._make_backend = _override_make
    db.close_connection()
    db.init_schema()
    db.add_watchlist_ticker("TEST", "US")

    yield

    db.close_connection()
    db._make_backend = original_make
    try:
        os.remove(db_file)
    except OSError:
        pass


def _valid_alert(**overrides) -> dict:
    alert = {
        "ticker": "TEST",
        "signal_type": "RECLAIM",
        "timeframe": "D",
        "ma_period": 50,
        "price": 110.0,
        "ma_value": 100.0,
        "bar_date": date.today(),
        "volume_ratio": None,
        "extra": {"streak": 7, "previous_step": 2, "new_step": 1},
        "fired_at": "2026-07-11T00:00:00Z",
    }
    alert.update(overrides)
    return alert


class TestDispatchAlertsSanityGate:

    def test_valid_alert_is_sent_and_logged(self):
        from src.alerter import dispatch_alerts

        alert = _valid_alert()
        with mock.patch("src.alerter.send_telegram_message", return_value=True) as mock_send, \
             mock.patch("src.alerter.send_ops_message") as mock_ops:
            sent = dispatch_alerts([alert])

        assert sent == 1
        mock_send.assert_called_once()
        mock_ops.assert_not_called()

        import src.db as db
        rows = db._db().execute("SELECT * FROM alert_log WHERE ticker = ?", ["TEST"])
        assert len(rows) == 1
        extra = json.loads(rows[0]["extra_json"])
        assert "quarantined" not in extra

    def test_invalid_alert_is_quarantined_not_sent(self):
        from src.alerter import dispatch_alerts

        # Direction check failure: RECLAIM with price <= ma_value.
        alert = _valid_alert(price=90.0, ma_value=100.0)
        with mock.patch("src.alerter.send_telegram_message") as mock_send, \
             mock.patch("src.alerter.send_ops_message") as mock_ops:
            sent = dispatch_alerts([alert])

        assert sent == 0
        mock_send.assert_not_called()
        mock_ops.assert_called_once()
        assert "quarantined" in mock_ops.call_args[0][0].lower()

    def test_quarantined_alert_still_inserted_into_alert_log(self):
        from src.alerter import dispatch_alerts

        alert = _valid_alert(price=90.0, ma_value=100.0)
        with mock.patch("src.alerter.send_telegram_message") as mock_send, \
             mock.patch("src.alerter.send_ops_message"):
            dispatch_alerts([alert])

        import src.db as db
        rows = db._db().execute("SELECT * FROM alert_log WHERE ticker = ?", ["TEST"])
        assert len(rows) == 1
        extra = json.loads(rows[0]["extra_json"])
        assert "quarantined" in extra
        assert "direction" in extra["quarantined"]
        mock_send.assert_not_called()

    def test_quarantined_alert_preserves_original_extra_fields(self):
        from src.alerter import dispatch_alerts

        alert = _valid_alert(price=90.0, ma_value=100.0, extra={"streak": 7, "previous_step": 2})
        with mock.patch("src.alerter.send_telegram_message"), \
             mock.patch("src.alerter.send_ops_message"):
            dispatch_alerts([alert])

        import src.db as db
        rows = db._db().execute("SELECT * FROM alert_log WHERE ticker = ?", ["TEST"])
        extra = json.loads(rows[0]["extra_json"])
        assert extra["streak"] == 7
        assert extra["previous_step"] == 2
        assert "quarantined" in extra

    def test_stale_daily_alert_is_quarantined(self):
        from src.alerter import dispatch_alerts

        stale_date = date.today() - timedelta(days=10)
        alert = _valid_alert(bar_date=stale_date)
        with mock.patch("src.alerter.send_telegram_message") as mock_send, \
             mock.patch("src.alerter.send_ops_message") as mock_ops:
            sent = dispatch_alerts([alert])

        assert sent == 0
        mock_send.assert_not_called()
        mock_ops.assert_called_once()

    def test_sanity_gate_runs_after_cooldown_gate(self):
        """
        An alert suppressed by cooldown should never reach the sanity gate
        (and therefore never trigger an [OPS] quarantine message) -- cooldown
        is a silent, expected suppression, not a data-quality problem.
        """
        from src.alerter import dispatch_alerts
        import src.db as db

        # Pre-populate alert_log so recent_alert_exists() returns True.
        db.insert_alert(_valid_alert())

        alert = _valid_alert(price=90.0, ma_value=100.0)  # would also fail sanity
        with mock.patch("src.alerter.send_telegram_message") as mock_send, \
             mock.patch("src.alerter.send_ops_message") as mock_ops:
            sent = dispatch_alerts([alert])

        assert sent == 0
        mock_send.assert_not_called()
        mock_ops.assert_not_called()  # cooldown suppressed it before sanity gate ran
