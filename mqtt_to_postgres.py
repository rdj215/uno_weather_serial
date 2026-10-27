#!/usr/bin/env python3
"""
mqtt_to_postgres.py — subscribes to the weather node's MQTT topics and stores
readings and device logs in Postgres.

It's deliberately separate from the bridge: it only cares about MQTT, so it
keeps working unchanged once the Uno publishes over cellular and the laptop
bridge is retired.

Setup:
    docker compose --profile bench up -d   # Postgres + local Mosquitto
    pip install -r requirements.txt    # needs paho-mqtt and psycopg[binary]

Run:
    python mqtt_to_postgres.py                         # local broker, local Postgres
    python mqtt_to_postgres.py --broker test.mosquitto.org

What goes where:
    hologram-mvp/<device>/reading  -> readings
    hologram-mvp/<device>/error    -> device_logs (level=error)
    hologram-mvp/<device>/event    -> device_logs (boot, device_silent, ...)
    hologram-mvp/<device>/status   -> device_logs (online / offline)

Messages that can't be stored (database down, bad data) are appended to
dead_letter.jsonl so nothing disappears silently.
"""

import argparse
import json
import logging
import re
import signal
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import psycopg
from psycopg.types.json import Jsonb

from storage import connect, database_url, touch_device

log = logging.getLogger("mqtt_to_postgres")

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PAYLOAD_BYTES = 4096
KINDS = ("reading", "error", "event", "status")

# Severity for each known event code; anything unlisted is "info".
EVENT_LEVELS = {
    "boot": "info",
    "device_resumed": "info",
    "device_silent": "warning",
    "serial_reopened": "warning",
}
STATUS_LEVELS = {"online": "info", "offline": "warning"}


class BadMessage(ValueError):
    """A message that's well-formed MQTT but not something we should store."""


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    """Parse the bridge's ISO timestamp; fall back to now if missing or bad."""
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value)
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return utc_now()


def number(msg, key, required=True):
    value = msg.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadMessage(f"{key!r} missing or not a number")
    return value


class Store:
    """Thin wrapper around one Postgres connection that reconnects on failure."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.conn = None

    def connect(self):
        self.conn = connect(self.dsn)
        log.info("connected to Postgres")

    def run(self, fn, attempts=3):
        """Run fn(cursor) in a transaction, reconnecting on connection errors."""
        for attempt in range(1, attempts + 1):
            try:
                if self.conn is None or self.conn.closed:
                    self.connect()
                with self.conn.transaction(), self.conn.cursor() as cur:
                    return fn(cur)
            except psycopg.OperationalError as e:
                log.warning("Postgres unavailable (attempt %d/%d): %s", attempt, attempts, e)
                self.conn = None
                if attempt < attempts:
                    time.sleep(2 * attempt)
        raise ConnectionError("Postgres unavailable")

    def close(self):
        if self.conn is not None:
            self.conn.close()


def store_reading(cur, device_id, msg):
    received_at = parse_time(msg.get("received_at"))
    touch_device(cur, device_id, received_at, update_last_seen=True)
    cur.execute(
        """INSERT INTO readings
               (device_id, received_at, temp_c, humidity_pct, uptime_s, reads, read_errors, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (device_id, received_at) DO NOTHING""",
        (
            device_id, received_at,
            number(msg, "t_c"), number(msg, "rh"),
            number(msg, "uptime_s", required=False),
            number(msg, "reads", required=False),
            number(msg, "read_errors", required=False),
            Jsonb(msg),
        ),
    )
    return "reading" if cur.rowcount else "reading (duplicate, skipped)"


def store_log(cur, device_id, received_at, level, category, code, source, payload):
    # Only messages from the device itself count as the device being alive.
    touch_device(cur, device_id, received_at, update_last_seen=(source == "device"))
    cur.execute(
        """INSERT INTO device_logs (device_id, received_at, level, category, code, source, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (device_id, received_at, level, category, code, source,
         Jsonb(payload) if payload is not None else None),
    )
    return f"{category}:{code}"


def handle(store, device_id, kind, raw, retained):
    """Validate one message and write it. Returns a short description for logging."""
    if kind == "status":
        code = raw.decode("utf-8", errors="replace").strip()
        if code not in STATUS_LEVELS:
            raise BadMessage(f"unknown status {code!r}")
        if retained:
            # A retained status is a replay of an old state, not a new change.
            return None
        return store.run(lambda cur: store_log(
            cur, device_id, utc_now(), STATUS_LEVELS[code], "status", code, "bridge", None))

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BadMessage(f"invalid JSON: {e}") from None
    if not isinstance(msg, dict):
        raise BadMessage("payload is not a JSON object")
    if msg.get("device_id") not in (None, device_id):
        log.warning("payload device_id %r differs from topic %r; using topic",
                    msg.get("device_id"), device_id)

    if kind == "reading":
        return store.run(lambda cur: store_reading(cur, device_id, msg))

    received_at = parse_time(msg.get("received_at"))
    if kind == "error":
        code = str(msg.get("error") or "unknown_error")[:64]
        return store.run(lambda cur: store_log(
            cur, device_id, received_at, "error", "error", code, "device", msg))

    # kind == "event"
    code = str(msg.get("event") or "unknown_event")[:64]
    source = msg.get("source", "device")
    if source not in ("device", "bridge"):
        raise BadMessage(f"unknown source {source!r}")
    return store.run(lambda cur: store_log(
        cur, device_id, received_at, EVENT_LEVELS.get(code, "info"), "event", code, source, msg))


class Subscriber:
    def __init__(self, args):
        self.args = args
        self.store = Store(args.database_url)
        self.stats = {"stored": 0, "duplicates": 0, "rejected": 0, "dead_lettered": 0}
        self.running = True

    def dead_letter(self, topic, raw, reason):
        record = {
            "at": utc_now().isoformat(timespec="seconds"),
            "topic": topic,
            "payload": raw.decode("utf-8", errors="replace"),
            "reason": reason,
        }
        with open(self.args.dead_letter, "a") as f:
            f.write(json.dumps(record) + "\n")
        self.stats["dead_lettered"] += 1

    def on_message(self, client, userdata, message):
        topic, raw = message.topic, message.payload
        parts = topic.split("/")
        if len(parts) != 3 or parts[2] not in KINDS or not DEVICE_ID_RE.match(parts[1]):
            log.debug("ignoring topic %s", topic)
            return
        _, device_id, kind = parts

        if len(raw) > MAX_PAYLOAD_BYTES:
            log.warning("rejected %s: payload too large (%d bytes)", topic, len(raw))
            self.stats["rejected"] += 1
            return

        try:
            result = handle(self.store, device_id, kind, raw, message.retain)
        except BadMessage as e:
            log.warning("rejected %s: %s", topic, e)
            self.stats["rejected"] += 1
            self.dead_letter(topic, raw, str(e))
            return
        except psycopg.errors.CheckViolation as e:
            log.warning("rejected %s: out-of-range value (%s)", topic, e.diag.constraint_name)
            self.stats["rejected"] += 1
            self.dead_letter(topic, raw, f"check violation: {e.diag.constraint_name}")
            return
        except (ConnectionError, psycopg.Error) as e:
            log.error("could not store %s: %s", topic, e)
            self.dead_letter(topic, raw, f"database error: {e}")
            return

        if result is None:
            return
        if "duplicate" in result:
            self.stats["duplicates"] += 1
        else:
            self.stats["stored"] += 1
        log.info("%s <- %s", result, device_id)

    def run(self):
        try:
            self.store.connect()
        except psycopg.OperationalError as e:
            sys.exit(f"can't connect to Postgres: {e}\nIs it running? Try: docker compose up -d")

        # A fixed client ID plus clean_session=False gives a persistent session:
        # the broker queues QoS 1 messages while this script is stopped and
        # delivers them when it reconnects.
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.args.client_id,
            clean_session=False,
        )
        if self.args.username:
            client.username_pw_set(self.args.username, self.args.password)
        topic = f"{self.args.topic_root}/+/+"

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                log.error("MQTT connect failed: %s", reason_code)
                return
            log.info("connected to %s:%s, subscribing to %s",
                     self.args.broker, self.args.mqtt_port, topic)
            client.subscribe(topic, qos=1)

        def on_disconnect(client, userdata, flags, reason_code, properties):
            if self.running:
                log.warning("MQTT disconnected (%s); reconnecting", reason_code)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = self.on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(self.args.broker, self.args.mqtt_port, keepalive=60)
        client.loop_start()

        while self.running:
            time.sleep(0.5)

        client.disconnect()
        client.loop_stop()
        self.store.close()
        log.info("stopped; %s", ", ".join(f"{k}={v}" for k, v in self.stats.items()))


def main():
    p = argparse.ArgumentParser(description="Store weather node MQTT messages in Postgres.")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--username")
    p.add_argument("--password")
    p.add_argument("--topic-root", default="hologram-mvp")
    p.add_argument("--client-id", default="weather-db-writer")
    p.add_argument("--database-url", default=database_url())
    p.add_argument("--dead-letter", default="dead_letter.jsonl")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    sub = Subscriber(args)

    def handle_stop(signum, frame):
        sub.running = False

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    sub.run()


if __name__ == "__main__":
    main()
