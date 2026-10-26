-- 001_schema.sql: tables and views for the cellular weather node.
--
-- Runs automatically the first time the Postgres container starts with an
-- empty data volume. To re-run it from scratch: docker compose down -v

-- One row per device, upserted on every message so "last seen" is always current.
CREATE TABLE devices (
    device_id   text PRIMARY KEY,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);

-- Successful sensor readings.
CREATE TABLE readings (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id    text        NOT NULL REFERENCES devices (device_id),
    received_at  timestamptz NOT NULL,              -- set by the bridge (UTC)
    temp_c       numeric(5,2) NOT NULL CHECK (temp_c BETWEEN -40 AND 125),
    humidity_pct numeric(5,2) NOT NULL CHECK (humidity_pct BETWEEN 0 AND 100),
    uptime_s     bigint,                            -- device uptime when read
    reads        bigint,                            -- running counter on the device
    read_errors  bigint,                            -- running counter on the device
    payload      jsonb       NOT NULL,              -- original message, for anything not broken out above
    inserted_at  timestamptz NOT NULL DEFAULT now(),
    -- Retained messages are redelivered when the subscriber reconnects;
    -- this key makes those replays a no-op instead of a duplicate row.
    UNIQUE (device_id, received_at)
);
CREATE INDEX readings_device_time ON readings (device_id, received_at DESC);

-- Everything that isn't a reading: boots, sensor errors, connectivity changes,
-- and health events raised by the bridge.
CREATE TABLE device_logs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id    text        NOT NULL REFERENCES devices (device_id),
    received_at  timestamptz NOT NULL,
    level        text        NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    category     text        NOT NULL CHECK (category IN ('event', 'error', 'status')),
    code         text        NOT NULL,              -- e.g. boot, dht_read_failed, device_silent, offline
    source       text        NOT NULL DEFAULT 'device' CHECK (source IN ('device', 'bridge', 'broker')),
    payload      jsonb,
    inserted_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX device_logs_device_time ON device_logs (device_id, received_at DESC);
CREATE INDEX device_logs_level ON device_logs (level, received_at DESC) WHERE level <> 'info';

-- Latest reading per device.
CREATE VIEW latest_readings AS
SELECT DISTINCT ON (device_id) *
FROM readings
ORDER BY device_id, received_at DESC;

-- One row per device summarizing whether it's healthy. This is the query an
-- alerting job would poll.
CREATE VIEW device_health AS
SELECT
    d.device_id,
    d.last_seen,
    round(extract(epoch FROM now() - d.last_seen))::int            AS seconds_since_seen,
    lr.received_at                                                  AS last_reading_at,
    lr.temp_c,
    lr.humidity_pct,
    -- Error rate from the device's own counters on its latest reading.
    CASE WHEN lr.reads > 0
         THEN round(100.0 * lr.read_errors / lr.reads, 1) END       AS sensor_error_pct,
    (SELECT count(*) FROM device_logs l
      WHERE l.device_id = d.device_id
        AND l.level <> 'info'
        AND l.received_at > now() - interval '24 hours')            AS problems_24h,
    (SELECT l.code FROM device_logs l
      WHERE l.device_id = d.device_id AND l.category = 'status'
      ORDER BY l.received_at DESC LIMIT 1)                          AS last_status,
    CASE
        WHEN now() - d.last_seen > interval '2 minutes' THEN 'stale'
        WHEN (SELECT l.code FROM device_logs l
               WHERE l.device_id = d.device_id AND l.category = 'status'
               ORDER BY l.received_at DESC LIMIT 1) = 'offline'     THEN 'offline'
        ELSE 'ok'
    END                                                             AS health
FROM devices d
LEFT JOIN latest_readings lr USING (device_id);
