#!/usr/bin/env python3
"""
serial_mqtt_bridge.py — reads JSON lines from the Uno weather node over USB
serial and publishes them to an MQTT broker.

This is a stand-in for the SIM7000 cellular shield: for now the laptop is the
Uno's network connection. When the shield arrives, the Uno publishes over
cellular itself and this script is retired.

Setup:
    pip install pyserial paho-mqtt

Run:
    python serial_mqtt_bridge.py --dry-run     # print messages, don't publish
    python serial_mqtt_bridge.py               # auto-detect port, public test broker
    python serial_mqtt_bridge.py --port /dev/cu.usbmodem1101 --broker localhost

Close the Arduino Serial Monitor first; only one program can hold the port.

Topics (prefix defaults to hologram-mvp/<device-id>):
    <prefix>/reading   sensor readings (retained, so new subscribers see the latest)
    <prefix>/error     sensor read failures
    <prefix>/event     boot and other device events
    <prefix>/status    "online" / "offline" (retained; "offline" is the MQTT last will,
                       so it's published by the broker if the bridge dies unexpectedly)

Watch the messages from another terminal (brew install mosquitto):
    mosquitto_sub -h test.mosquitto.org -t 'hologram-mvp/#' -v
"""

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone

import serial
from serial.tools import list_ports

try:
    import paho.mqtt.client as mqtt
except ImportError:  # allows --dry-run without paho installed
    mqtt = None

log = logging.getLogger("bridge")

PORT_HINTS = ("usbmodem", "usbserial", "arduino", "ch340", "wch", "ttyacm", "ttyusb")


def find_port():
    """Return the one serial port that looks like an Arduino, or None.

    If several boards are connected (e.g. an Uno and an ESP8266), guessing would
    silently open the wrong one, so list them and ask for --port instead.
    """
    matches = []
    for p in list_ports.comports():
        text = f"{p.device} {p.description} {p.manufacturer or ''}".lower()
        if any(hint in text for hint in PORT_HINTS):
            matches.append(p)
    if len(matches) > 1:
        listing = "\n".join(f"  {p.device}  ({p.description})" for p in matches)
        sys.exit(f"Several boards are connected; pick one with --port:\n{listing}")
    return matches[0].device if matches else None


def parse_line(raw):
    """Turn one serial line into a dict, or None if it isn't a complete JSON object.

    Partial lines are normal right after the port opens (opening it resets the Uno
    mid-print), so they're logged at debug level rather than treated as errors.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if not text.startswith("{"):
        log.debug("skipping non-JSON line: %r", text)
        return None
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        log.debug("skipping malformed line: %r", text)
        return None
    return msg if isinstance(msg, dict) else None


def classify(msg):
    if "t_c" in msg:
        return "reading"
    if "error" in msg:
        return "error"
    if "event" in msg:
        return "event"
    return "unknown"


class Bridge:
    def __init__(self, args):
        self.args = args
        self.prefix = args.topic_prefix or f"hologram-mvp/{args.device_id}"
        self.client = None
        self.stats = {"lines": 0, "published": 0, "skipped": 0, "serial_reconnects": 0}
        self.running = True

    # ---- MQTT ---------------------------------------------------------------

    def start_mqtt(self):
        if self.args.dry_run:
            log.info("dry run: printing messages instead of publishing")
            return
        if mqtt is None:
            sys.exit("paho-mqtt is not installed: pip install paho-mqtt")

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bridge-{self.args.device_id}",
        )
        if self.args.username:
            self.client.username_pw_set(self.args.username, self.args.password)
        status = f"{self.prefix}/status"
        self.client.will_set(status, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.max_queued_messages_set(1000)  # buffer while the broker is unreachable

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                log.error("MQTT connect failed: %s", reason_code)
                return
            log.info("connected to %s:%s", self.args.broker, self.args.mqtt_port)
            client.publish(status, "online", qos=1, retain=True)

        def on_disconnect(client, userdata, flags, reason_code, properties):
            if self.running:
                log.warning("MQTT disconnected (%s); reconnecting", reason_code)

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect
        self.client.connect_async(self.args.broker, self.args.mqtt_port, keepalive=60)
        self.client.loop_start()  # background thread handles reconnects

    def publish(self, kind, msg):
        topic = f"{self.prefix}/{kind}"
        payload = json.dumps(msg, separators=(",", ":"))
        if self.args.dry_run:
            print(f"{topic} {payload}", flush=True)
        else:
            # qos=1 messages are queued and delivered after a reconnect
            self.client.publish(topic, payload, qos=1, retain=(kind == "reading"))
        self.stats["published"] += 1

    def stop_mqtt(self):
        if self.client is None:
            return
        info = self.client.publish(f"{self.prefix}/status", "offline", qos=1, retain=True)
        try:
            info.wait_for_publish(timeout=3)
        except (RuntimeError, ValueError):
            pass  # not connected; the broker's last will covers it
        self.client.disconnect()
        self.client.loop_stop()

    # ---- serial -------------------------------------------------------------

    def open_serial(self):
        """Open the serial port, retrying until it appears (e.g. after an unplug)."""
        while self.running:
            port = self.args.port or find_port()
            if port is None:
                log.warning("no Arduino port found; retrying in 5 s (use --port to set one)")
            else:
                try:
                    ser = serial.Serial(port, self.args.baud, timeout=2)
                    log.info("opened %s at %d baud", port, self.args.baud)
                    return ser
                except serial.SerialException as e:
                    log.warning("can't open %s: %s; retrying in 5 s", port, e)
            time.sleep(5)
        return None

    def run(self):
        self.start_mqtt()
        ser = self.open_serial()
        last_data = time.monotonic()
        warned_silent = False

        while self.running and ser is not None:
            try:
                raw = ser.readline()
            except serial.SerialException as e:
                log.warning("serial error (%s); reopening", e)
                ser.close()
                self.stats["serial_reconnects"] += 1
                ser = self.open_serial()
                last_data = time.monotonic()
                continue

            if not raw:  # read timed out with no data
                silent = time.monotonic() - last_data
                if silent > self.args.silence_warn and not warned_silent:
                    log.warning("no data from device for %.0f s", silent)
                    warned_silent = True
                continue

            last_data = time.monotonic()
            if warned_silent:
                log.info("device data resumed")
                warned_silent = False

            self.stats["lines"] += 1
            msg = parse_line(raw)
            if msg is None:
                self.stats["skipped"] += 1
                continue

            kind = classify(msg)
            if kind == "unknown":
                # e.g. the tail of a line cut off when the port opened and reset the Uno
                log.debug("skipping unrecognized message: %r", msg)
                self.stats["skipped"] += 1
                continue
            msg["device_id"] = self.args.device_id
            msg["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.publish(kind, msg)
            if not self.args.dry_run:
                log.info("%s -> %s/%s", kind, self.prefix, kind)

        if ser is not None:
            ser.close()
        self.stop_mqtt()
        log.info("stopped; %s", ", ".join(f"{k}={v}" for k, v in self.stats.items()))


def main():
    p = argparse.ArgumentParser(description="Bridge Uno serial JSON lines to MQTT.")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--broker", default="test.mosquitto.org")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--username")
    p.add_argument("--password")
    p.add_argument("--device-id", default="uno-weather-01")
    p.add_argument("--topic-prefix", help="default: hologram-mvp/<device-id>")
    p.add_argument("--silence-warn", type=float, default=35,
                   help="warn after this many seconds with no serial data")
    p.add_argument("--dry-run", action="store_true", help="print instead of publishing")
    p.add_argument("-v", "--verbose", action="store_true", help="show skipped lines")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    bridge = Bridge(args)

    def handle_stop(signum, frame):
        bridge.running = False

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    bridge.run()


if __name__ == "__main__":
    main()