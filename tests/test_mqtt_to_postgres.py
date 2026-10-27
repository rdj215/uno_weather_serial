"""Validation and routing of MQTT messages before they're written to Postgres."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeStore

from mqtt_to_postgres import BadMessage, handle, number, parse_time


def payload(**fields):
    return json.dumps(fields).encode()


# ---------------------------------------------------------------- handle

def test_reading_is_stored(store):
    raw = payload(t_c=28.9, rh=37.0, uptime_s=20, reads=3, read_errors=1,
                  received_at="2026-10-25T06:14:14+00:00")
    assert handle(store, "uno-weather-01", "reading", raw, retained=False) == "reading"
    [(_, params)] = store.cur.sql_for("readings")
    assert params[0] == "uno-weather-01"
    assert params[2:7] == (28.9, 37.0, 20, 3, 1)


def test_replayed_reading_is_reported_as_duplicate():
    raw = payload(t_c=28.9, rh=37.0, received_at="2026-10-25T06:14:14+00:00")
    assert "duplicate" in handle(FakeStore(rowcount=0), "uno-weather-01", "reading", raw, retained=True)


def test_reading_missing_temperature_is_rejected(store):
    with pytest.raises(BadMessage, match="t_c"):
        handle(store, "uno-weather-01", "reading", payload(rh=37.0), retained=False)


@pytest.mark.parametrize("raw, reason", [
    (b'{"t_c": 28.9', "invalid JSON"),
    (b"[1, 2]", "not a JSON object"),
])
def test_malformed_payloads_are_rejected(store, raw, reason):
    with pytest.raises(BadMessage, match=reason):
        handle(store, "uno-weather-01", "reading", raw, retained=False)


def test_sensor_error_is_logged_as_error(store):
    handle(store, "uno-weather-01", "error", payload(error="dht_read_failed"), retained=False)
    [(_, params)] = store.cur.sql_for("device_logs")
    assert params[2:6] == ("error", "error", "dht_read_failed", "device")


def test_bridge_health_event_gets_its_level_and_does_not_count_as_device_alive(store):
    handle(store, "uno-weather-01", "event", payload(event="device_silent", source="bridge", silent_s=36),
           retained=False)
    [(_, params)] = store.cur.sql_for("device_logs")
    assert params[2:6] == ("warning", "event", "device_silent", "bridge")
    # The bridge reporting silence must not refresh the device's last_seen.
    [(device_sql, _)] = store.cur.sql_for("devices")
    assert "DO NOTHING" in device_sql


def test_unknown_event_code_is_info_and_truncated(store):
    handle(store, "uno-weather-01", "event", payload(event="x" * 100), retained=False)
    params = store.cur.sql_for("device_logs")[0][1]
    assert params[2] == "info" and params[4] == "x" * 64


def test_unknown_event_source_is_rejected(store):
    with pytest.raises(BadMessage, match="source"):
        handle(store, "uno-weather-01", "event", payload(event="boot", source="laptop"), retained=False)


def test_offline_status_is_logged(store):
    assert handle(store, "uno-weather-01", "status", b"offline", retained=False) == "status:offline"
    assert store.cur.sql_for("device_logs")[0][1][2] == "warning"


def test_retained_status_is_a_replay_and_skipped(store):
    assert handle(store, "uno-weather-01", "status", b"online", retained=True) is None
    assert store.cur.calls == []


def test_unknown_status_is_rejected(store):
    with pytest.raises(BadMessage, match="unknown status"):
        handle(store, "uno-weather-01", "status", b"rebooting", retained=False)


# ---------------------------------------------------------------- helpers

def test_parse_time_keeps_offsets_and_assumes_utc():
    assert parse_time("2026-10-25T06:14:14+00:00") == datetime(2026, 10, 25, 6, 14, 14, tzinfo=timezone.utc)
    assert parse_time("2026-10-25T06:14:14").tzinfo == timezone.utc


@pytest.mark.parametrize("value", [None, "garbage", 12345])
def test_parse_time_falls_back_to_now(value):
    assert datetime.now(timezone.utc) - parse_time(value) < timedelta(seconds=5)


def test_number():
    assert number({"t_c": 1}, "t_c") == 1
    assert number({}, "reads", required=False) is None
    for bad in ({}, {"t_c": "1"}, {"t_c": False}):
        with pytest.raises(BadMessage):
            number(bad, "t_c")
