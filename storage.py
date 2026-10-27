"""
storage.py — Postgres helpers shared by mqtt_to_postgres.py (serial path) and
hologram_poller.py (cellular path). Both write the same tables, so connecting
and registering devices live here; each script keeps its own INSERTs because
the cellular rows carry extra columns.
"""

import os

import psycopg

DEFAULT_DATABASE_URL = "postgresql://weather:weather@localhost:5432/weather"


def database_url():
    """DATABASE_URL from the environment, or the local docker-compose database."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def connect(dsn):
    return psycopg.connect(dsn, autocommit=True, connect_timeout=5)


def touch_device(cur, device_id, seen_at, update_last_seen=True):
    """Register the device on first sight; optionally move its last_seen forward.

    last_seen only ever moves forward, so replayed or out-of-order messages
    can't make a device look older than it is.
    """
    if update_last_seen:
        cur.execute(
            """INSERT INTO devices (device_id, first_seen, last_seen) VALUES (%s, %s, %s)
               ON CONFLICT (device_id) DO UPDATE
               SET last_seen = greatest(devices.last_seen, excluded.last_seen)""",
            (device_id, seen_at, seen_at),
        )
    else:
        cur.execute(
            """INSERT INTO devices (device_id, first_seen, last_seen) VALUES (%s, %s, %s)
               ON CONFLICT (device_id) DO NOTHING""",
            (device_id, seen_at, seen_at),
        )
