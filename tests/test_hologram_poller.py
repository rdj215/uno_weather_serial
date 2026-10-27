"""Decoding and storage for messages pulled from the Hologram REST API."""

import base64
import json
import os
from datetime import datetime, timezone

import pytest
from conftest import FakeCursor

from hologram_poller import (
    BadRecord,
    as_int,
    as_number,
    decode_record,
    load_dotenv,
    maybe_base64,
    parse_logged,
    store,
)

# What the device sends (see README "Cellular messages").
READING = {"t_c": 22.4, "rh": 41.0, "uptime_s": 965, "reads": 2, "read_errors": 0, "seq": 2,
           "send_fail": 0, "rssi_dbm": -77, "reg": 5, "op": "310-260", "band": "B12", "batt_mv": 3987}


def live_record(payload):
    """Record shape confirmed against the live API: data.data is already-parsed JSON."""
    return {"id": 1007776448, "record_id": "6f1c-uuid", "deviceid": 1234567,
            "logged": "2026-10-27 20:21:08",
            "data": {"authtype": "psk", "source": "devicekey", "data": payload,
                     "tags": ["weather", "_SOCKETAPI_", "_JSONSTRING_"]}}


def legacy_record(payload):
    """Older shape: data is a JSON string, with the device's JSON base64-encoded inside."""
    inner = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"id": 99, "record_id": "legacy-uuid", "deviceid": 1234567,
            "logged": "2026-10-27 13:05:01.123456",
            "data": json.dumps({"data": inner, "tags": ["weather"]})}


# ---------------------------------------------------------------- decode_record

def test_decodes_live_record_shape():
    record_id, logged, payload = decode_record(live_record(READING))
    assert record_id == "6f1c-uuid"
    assert logged == datetime(2026, 10, 27, 20, 21, 8, tzinfo=timezone.utc)
    assert payload == READING


def test_decodes_legacy_base64_record_shape():
    record_id, logged, payload = decode_record(legacy_record(READING))
    assert record_id == "legacy-uuid"
    assert logged.microsecond == 123456
    assert payload == READING


def test_inner_plain_json_string_is_accepted():
    rec = live_record(None)
    rec["data"]["data"] = json.dumps(READING)  # a JSON string that isn't base64
    assert decode_record(rec)[2] == READING


def test_falls_back_to_numeric_id_when_record_id_missing():
    rec = live_record(READING)
    del rec["record_id"]
    assert decode_record(rec)[0] == "1007776448"


def test_record_without_any_id_is_rejected():
    rec = live_record(READING)
    del rec["record_id"], rec["id"]
    with pytest.raises(BadRecord, match="no id"):
        decode_record(rec)


def test_record_without_data_is_rejected():
    rec = live_record(READING)
    rec["data"] = {"tags": ["weather"]}
    with pytest.raises(BadRecord, match="no data"):
        decode_record(rec)


def test_non_object_payload_is_rejected():
    rec = live_record(None)
    rec["data"]["data"] = base64.b64encode(b"hello").decode()
    with pytest.raises(BadRecord, match="isn't a JSON object"):
        decode_record(rec)


# ---------------------------------------------------------------- small parsers

@pytest.mark.parametrize("value, expected", [
    ("2026-10-27 20:21:08", datetime(2026, 10, 27, 20, 21, 8, tzinfo=timezone.utc)),
    ("2026-10-27T20:21:08Z", datetime(2026, 10, 27, 20, 21, 8, tzinfo=timezone.utc)),
])
def test_parse_logged_treats_times_as_utc(value, expected):
    assert parse_logged(value) == expected


@pytest.mark.parametrize("value", [None, 1730000000, "yesterday"])
def test_parse_logged_rejects_missing_or_unreadable(value):
    with pytest.raises(BadRecord):
        parse_logged(value)


def test_maybe_base64_decodes_valid_utf8():
    assert maybe_base64(base64.b64encode(b'{"t_c":1}').decode()) == '{"t_c":1}'


@pytest.mark.parametrize("text", ['{"t_c":1}', "not base64!", base64.b64encode(b"\xff\xfe").decode()])
def test_maybe_base64_returns_text_unchanged_otherwise(text):
    assert maybe_base64(text) == text


def test_as_int_ignores_bools_and_missing():
    assert as_int({"seq": 3.0}, "seq") == 3
    assert as_int({"seq": True}, "seq") is None
    assert as_int({}, "seq") is None


def test_as_number_requires_a_real_number():
    assert as_number({"t_c": 22.4}, "t_c") == 22.4
    for bad in ({}, {"t_c": "22"}, {"t_c": True}):
        with pytest.raises(BadRecord):
            as_number(bad, "t_c")


# ---------------------------------------------------------------- store

LOGGED = datetime(2026, 10, 27, 20, 21, 8, tzinfo=timezone.utc)


def test_reading_goes_to_readings_with_connection_fields(cur):
    assert store(cur, "uno-weather-01", "rec-1", LOGGED, READING) == "reading"
    [(sql, params)] = cur.sql_for("readings")
    assert "'cellular'" in sql
    assert params[:4] == ("uno-weather-01", LOGGED, 22.4, 41.0)
    assert params[-7:] == ("rec-1", 2, -77, 5, "310-260", "B12", 3987)


def test_zero_signal_and_battery_are_stored_as_null(cur):
    # 0 means "no value" for these fields; NULL keeps it out of averages.
    store(cur, "uno-weather-01", "rec-1", LOGGED, {**READING, "rssi_dbm": 0, "batt_mv": 0})
    record_id, seq, rssi, reg, op, band, batt = cur.sql_for("readings")[0][1][-7:]
    assert rssi is None and batt is None
    assert seq == 2 and reg == 5  # other fields untouched


def test_already_stored_reading_reports_duplicate():
    assert store(FakeCursor(rowcount=0), "uno-weather-01", "rec-1", LOGGED, READING) == "duplicate"


def test_sensor_error_goes_to_device_logs(cur):
    assert store(cur, "uno-weather-01", "rec-2", LOGGED, {"error": "dht_read_failed", "seq": 3}) == "error"
    [(_, params)] = cur.sql_for("device_logs")
    assert params[:5] == ("uno-weather-01", LOGGED, "error", "error", "dht_read_failed")


def test_boot_event_goes_to_device_logs(cur):
    assert store(cur, "uno-weather-01", "rec-3", LOGGED, {"event": "boot"}) == "event"
    assert cur.sql_for("device_logs")[0][1][2:5] == ("info", "event", "boot")


def test_unrecognized_payload_is_rejected(cur):
    with pytest.raises(BadRecord):
        store(cur, "uno-weather-01", "rec-4", LOGGED, {"hello": 1})


def test_missing_humidity_is_rejected(cur):
    with pytest.raises(BadRecord, match="rh"):
        store(cur, "uno-weather-01", "rec-5", LOGGED, {"t_c": 22.4})


# ---------------------------------------------------------------- load_dotenv

def test_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nHOLOGRAM_API_KEY="abc"\nHOLOGRAM_DEVICE_ID=123\nnot a pair\n')
    monkeypatch.delenv("HOLOGRAM_API_KEY", raising=False)
    monkeypatch.setenv("HOLOGRAM_DEVICE_ID", "from-shell")
    load_dotenv(env)
    assert os.environ["HOLOGRAM_API_KEY"] == "abc"  # quotes stripped
    assert os.environ["HOLOGRAM_DEVICE_ID"] == "from-shell"  # real environment wins


def test_load_dotenv_missing_file_is_fine(tmp_path):
    load_dotenv(tmp_path / "nope.env")
