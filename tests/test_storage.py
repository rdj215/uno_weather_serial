"""Device registration shared by both writers."""

from datetime import datetime, timezone

from storage import DEFAULT_DATABASE_URL, database_url, touch_device

SEEN = datetime(2026, 10, 27, 20, 21, 8, tzinfo=timezone.utc)


def test_touch_device_moves_last_seen_forward_only(cur):
    touch_device(cur, "uno-weather-01", SEEN)
    [(sql, params)] = cur.calls
    assert "greatest(devices.last_seen, excluded.last_seen)" in sql
    assert params == ("uno-weather-01", SEEN, SEEN)


def test_touch_device_can_register_without_updating_last_seen(cur):
    touch_device(cur, "uno-weather-01", SEEN, update_last_seen=False)
    assert "DO NOTHING" in cur.calls[0][0]


def test_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url() == DEFAULT_DATABASE_URL
    monkeypatch.setenv("DATABASE_URL", "postgresql://elsewhere/db")
    assert database_url() == "postgresql://elsewhere/db"
