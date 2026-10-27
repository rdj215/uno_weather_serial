"""Serial line parsing and port selection in the bench-test bridge."""

from types import SimpleNamespace

import pytest

import serial_mqtt_bridge
from serial_mqtt_bridge import classify, find_port, parse_line


@pytest.mark.parametrize("raw, expected", [
    (b'{"t_c":28.9,"rh":37.0}\r\n', {"t_c": 28.9, "rh": 37.0}),
    (b'{"event":"boot"}\n', {"event": "boot"}),
    (b"\r\n", None),                  # blank line
    (b"DHT init\r\n", None),          # not JSON
    (b'{"t_c":28.9,"r\r\n', None),     # cut off mid-print
    (b"[1,2]\n", None),               # JSON, but not an object
    (b'{"t_c":\xff}\n', None),        # garbled bytes
])
def test_parse_line(raw, expected):
    assert parse_line(raw) == expected


@pytest.mark.parametrize("msg, kind", [
    ({"t_c": 1, "rh": 2}, "reading"),
    ({"error": "dht_read_failed"}, "error"),
    ({"event": "boot"}, "event"),
    ({"t_c": 1, "error": "x"}, "reading"),
])
def test_classify(msg, kind):
    assert classify(msg) == kind


def test_cut_off_first_line_is_not_published():
    # Real case: opening the port resets the Uno mid-print, leaving e.g. {"evs":2,...}.
    # It parses as JSON but isn't a recognized message type.
    assert classify(parse_line(b'{"evs":2,"rh":37.0}\n')) == "unknown"


UNO = SimpleNamespace(device="/dev/cu.usbmodem1101", description="Arduino Uno", manufacturer="Arduino")
NODEMCU = SimpleNamespace(device="/dev/cu.usbserial-1140", description="CP2102 USB to UART",
                          manufacturer="Silicon Labs")
BLUETOOTH = SimpleNamespace(device="/dev/cu.Bluetooth-Incoming-Port", description="n/a", manufacturer=None)


def use_ports(monkeypatch, *ports):
    monkeypatch.setattr(serial_mqtt_bridge.list_ports, "comports", lambda: list(ports))


def test_find_port_picks_the_only_board(monkeypatch):
    use_ports(monkeypatch, BLUETOOTH, UNO)
    assert find_port() == "/dev/cu.usbmodem1101"


def test_find_port_returns_none_without_a_board(monkeypatch):
    use_ports(monkeypatch, BLUETOOTH)
    assert find_port() is None


def test_find_port_refuses_to_guess_between_two_boards(monkeypatch):
    # Regression: with the Uno and the NodeMCU both plugged in, auto-detect
    # once opened the NodeMCU and the bridge received nothing.
    use_ports(monkeypatch, UNO, NODEMCU)
    with pytest.raises(SystemExit) as exc:
        find_port()
    assert "/dev/cu.usbmodem1101" in str(exc.value) and "/dev/cu.usbserial-1140" in str(exc.value)
