#!/usr/bin/env python3
"""
hologram_poller.py — pulls the weather node's cellular messages from the
Hologram REST API and stores them in Postgres.

The device (cellular_weather.ino) sends each reading to Hologram's cloud over
LTE-M. Hologram keeps the messages; this script fetches new ones every minute
and writes them to the same tables as the serial bridge, marked
transport = 'cellular'.

Setup:
    1. Hologram dashboard -> Account -> API keys: create a key.
    2. Put it and your device ID (from the device's page) in a .env file next
       to this script (gitignored):
           HOLOGRAM_API_KEY=...
           HOLOGRAM_DEVICE_ID=1234567
    3. Apply the database migration once (see db/init/002_cellular.sql).
    4. pip install -r requirements.txt   (needs requests and psycopg)

Run:
    python hologram_poller.py --once --dump   # fetch once and print the raw API response
    python hologram_poller.py                 # keep polling every 60 s

Message envelope, as returned by GET /csr/data (one record per message,
confirmed against the live API in October 2026):
    {"id": 1007776448, "record_id": "uuid", "deviceid": 1234567,
     "logged": "2026-10-27 20:21:08",
     "data": {"authtype": "psk", "source": "devicekey",
              "data": {...the device's JSON, already parsed...},
              "tags": ["weather", "_SOCKETAPI_", "_JSONSTRING_", ...], ...}}
Older records and API versions return "data" as a JSON string with the
payload base64-encoded inside; the decoder handles both. --dump shows the raw
response.
"""

import argparse
import base64
import binascii
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests
from psycopg.types.json import Jsonb

log = logging.getLogger("hologram_poller")

API_BASE = "https://dashboard.hologram.io/api/1"
PAGE_SIZE = 100
MAX_PAGES = 50  # safety stop for one poll


# ---------------------------------------------------------------- config

def load_dotenv(path):
    """Minimal .env reader (KEY=value lines), so no extra dependency is needed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- decoding

class BadRecord(ValueError):
    pass


def parse_logged(value):
    """Hologram's 'logged' time ('2026-10-27 13:05:01.123456', UTC)."""
    if not isinstance(value, str):
        raise BadRecord("missing 'logged' time")
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BadRecord(f"unreadable 'logged' time {value!r}") from None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def maybe_base64(text):
    """Decode base64 if the text is valid base64 of UTF-8; otherwise return it."""
    try:
        raw = base64.b64decode(text, validate=True)
        return raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return text


def decode_record(rec):
    """Return (record_id, logged, payload dict) for one API record."""
    record_id = rec.get("record_id") or rec.get("id")
    if record_id is None:
        raise BadRecord("record has no id")
    logged = parse_logged(rec.get("logged"))

    envelope = maybe_json(rec.get("data"))
    inner = envelope.get("data") if isinstance(envelope, dict) else envelope
    if isinstance(inner, dict):
        payload = inner
    elif isinstance(inner, str):
        payload = maybe_json(maybe_base64(inner))
    else:
        raise BadRecord("record has no data")
    if not isinstance(payload, dict):
        raise BadRecord(f"payload isn't a JSON object: {str(payload)[:80]!r}")
    return str(record_id), logged, payload


def as_int(payload, key):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def as_number(payload, key):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRecord(f"{key!r} missing or not a number")
    return value


# ---------------------------------------------------------------- storage

def touch_device(cur, device_id, seen_at):
    cur.execute(
        """INSERT INTO devices (device_id, first_seen, last_seen) VALUES (%s, %s, %s)
           ON CONFLICT (device_id) DO UPDATE
           SET last_seen = greatest(devices.last_seen, excluded.last_seen)""",
        (device_id, seen_at, seen_at),
    )


def store(cur, device_id, record_id, logged, payload):
    """Write one message. Returns 'reading', 'error', 'event' or 'duplicate'."""
    touch_device(cur, device_id, logged)
    if "t_c" in payload:
        cur.execute(
            """INSERT INTO readings
                   (device_id, received_at, temp_c, humidity_pct, uptime_s, reads, read_errors,
                    payload, transport, hologram_record_id, seq, rssi_dbm, reg_status,
                    operator, band, battery_mv)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'cellular', %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (
                device_id, logged,
                as_number(payload, "t_c"), as_number(payload, "rh"),
                as_int(payload, "uptime_s"), as_int(payload, "reads"),
                as_int(payload, "read_errors"), Jsonb(payload), record_id,
                as_int(payload, "seq"), as_int(payload, "rssi_dbm") or None,
                as_int(payload, "reg"), payload.get("op") or None,
                payload.get("band") or None, as_int(payload, "batt_mv") or None,
            ),
        )
        return "reading" if cur.rowcount else "duplicate"

    if "error" in payload:
        level, category, code = "error", "error", str(payload["error"])[:64]
    elif "event" in payload:
        level, category, code = "info", "event", str(payload["event"])[:64]
    else:
        raise BadRecord("payload has no t_c, error or event field")
    cur.execute(
        """INSERT INTO device_logs
               (device_id, received_at, level, category, code, source, payload,
                transport, hologram_record_id)
           VALUES (%s, %s, %s, %s, %s, 'device', %s, 'cellular', %s)
           ON CONFLICT DO NOTHING""",
        (device_id, logged, level, category, code, Jsonb(payload), record_id),
    )
    return category if cur.rowcount else "duplicate"


def resume_point(conn, device_id, lookback):
    """Fetch from a little before the newest cellular message we already have."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT max(received_at) FROM (
                   SELECT received_at FROM readings WHERE device_id = %s AND transport = 'cellular'
                   UNION ALL
                   SELECT received_at FROM device_logs WHERE device_id = %s AND transport = 'cellular'
               ) t""",
            (device_id, device_id),
        )
        newest = cur.fetchone()[0]
    if newest is None:
        return datetime.now(timezone.utc) - lookback
    return newest - timedelta(minutes=10)  # overlap; duplicates are skipped


# ---------------------------------------------------------------- API

class Poller:
    def __init__(self, args):
        self.args = args
        self.session = requests.Session()
        self.session.auth = ("apikey", args.api_key)  # Hologram's HTTP Basic auth scheme
        self.conn = None
        self.running = True
        self.totals = {"reading": 0, "error": 0, "event": 0, "duplicate": 0, "rejected": 0}
        self.rejected_ids = self.load_rejected_ids()

    def load_rejected_ids(self):
        """Record IDs already dead-lettered, so a bad message is reported once,
        not again on every poll that overlaps it."""
        ids = set()
        try:
            with open(self.args.dead_letter) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("source") == "hologram":
                        rec = entry.get("record") or {}
                        ids.add(str(rec.get("record_id") or rec.get("id")))
        except FileNotFoundError:
            pass
        return ids

    def db(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(self.args.database_url, autocommit=True, connect_timeout=5)
        return self.conn

    def fetch_page(self, timestart, startafter):
        params = {
            "deviceid": self.args.device_id,
            "timestart": int(timestart.timestamp()),
            "limit": PAGE_SIZE,
        }
        if self.args.topic:
            params["topicname"] = self.args.topic
        if startafter is not None:
            params["startafter"] = startafter
        resp = self.session.get(f"{self.args.api_base}/csr/data", params=params, timeout=20)
        if resp.status_code in (401, 403):
            sys.exit(f"Hologram API refused the key (HTTP {resp.status_code}). "
                     "Check HOLOGRAM_API_KEY in .env.")
        resp.raise_for_status()
        body = resp.json()
        if self.args.dump:
            print(json.dumps(body, indent=2)[:4000])
            self.args.dump = False  # only the first page
        if isinstance(body, dict) and body.get("success") is False:
            raise RuntimeError(f"API error: {body.get('error') or body}")
        records = body.get("data", []) if isinstance(body, dict) else body
        continues = bool(body.get("continues")) if isinstance(body, dict) else False
        return records or [], continues

    def dead_letter(self, rec, reason):
        with open(self.args.dead_letter, "a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "source": "hologram", "reason": reason, "record": rec}) + "\n")

    def poll_once(self):
        conn = self.db()
        timestart = resume_point(conn, self.args.device_name, timedelta(hours=self.args.lookback_hours))
        counts = dict.fromkeys(self.totals, 0)
        startafter = None
        fetched = 0
        for _ in range(MAX_PAGES):
            records, continues = self.fetch_page(timestart, startafter)
            fetched += len(records)
            for rec in records:
                rec_key = str(rec.get("record_id") or rec.get("id"))
                if rec_key in self.rejected_ids:
                    continue  # already reported
                try:
                    record_id, logged, payload = decode_record(rec)
                    with conn.transaction(), conn.cursor() as cur:
                        result = store(cur, self.args.device_name, record_id, logged, payload)
                except (BadRecord, psycopg.errors.CheckViolation) as e:
                    reason = (f"out-of-range value ({e.diag.constraint_name})"
                              if isinstance(e, psycopg.errors.CheckViolation) else str(e))
                    log.warning("rejected record %s: %s", rec_key, reason)
                    self.dead_letter(rec, reason)
                    self.rejected_ids.add(rec_key)
                    result = "rejected"
                counts[result] += 1
            if not continues or not records:
                break
            startafter = records[-1].get("id")
        for k, v in counts.items():
            self.totals[k] += v
        new = counts["reading"] + counts["error"] + counts["event"]
        log.info("fetched %d since %s: %d new (%d readings, %d errors, %d events), "
                 "%d already stored, %d rejected",
                 fetched, timestart.strftime("%Y-%m-%d %H:%M UTC"), new, counts["reading"],
                 counts["error"], counts["event"], counts["duplicate"], counts["rejected"])

    def run(self):
        while self.running:
            try:
                self.poll_once()
            except requests.RequestException as e:
                log.warning("Hologram API unreachable (%s); retrying next poll", e)
            except psycopg.OperationalError as e:
                log.warning("Postgres unavailable (%s); retrying next poll", e)
                self.conn = None
            if self.args.once:
                break
            for _ in range(int(self.args.interval * 2)):
                if not self.running:
                    break
                time.sleep(0.5)
        if self.conn is not None:
            self.conn.close()
        log.info("stopped; totals: %s", ", ".join(f"{k}={v}" for k, v in self.totals.items()))


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    p = argparse.ArgumentParser(description="Pull Hologram cloud messages into Postgres.")
    p.add_argument("--api-key", default=os.environ.get("HOLOGRAM_API_KEY"))
    p.add_argument("--device-id", default=os.environ.get("HOLOGRAM_DEVICE_ID"),
                   help="Hologram device ID (number on the device's dashboard page)")
    p.add_argument("--device-name", default="uno-weather-01",
                   help="device_id to store rows under (same as the serial bridge)")
    p.add_argument("--topic", default="weather", help="only fetch this topic ('' for all)")
    p.add_argument("--interval", type=float, default=60, help="seconds between polls")
    p.add_argument("--lookback-hours", type=float, default=24,
                   help="how far back to fetch when the database has no cellular rows yet")
    p.add_argument("--database-url",
                   default=os.environ.get("DATABASE_URL",
                                          "postgresql://weather:weather@localhost:5432/weather"))
    p.add_argument("--dead-letter", default="dead_letter.jsonl")
    p.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)  # for testing
    p.add_argument("--once", action="store_true", help="poll once and exit")
    p.add_argument("--dump", action="store_true", help="print the first raw API response")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not args.api_key or not args.device_id:
        sys.exit("Set HOLOGRAM_API_KEY and HOLOGRAM_DEVICE_ID in .env (see the top of this file).")

    poller = Poller(args)

    def stop(signum, frame):
        poller.running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    poller.run()


if __name__ == "__main__":
    main()
