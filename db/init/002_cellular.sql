-- 002_cellular.sql: columns and views for readings that arrive over cellular
-- through the Hologram cloud (hologram_poller.py).
--
-- New databases get this automatically after 001_schema.sql. For an existing
-- database, apply it once:
--   docker compose exec -T postgres psql -U weather -v ON_ERROR_STOP=1 < db/init/002_cellular.sql

-- How each row reached us: 'serial' = USB bridge, 'cellular' = Hologram cloud.
ALTER TABLE readings
    ADD COLUMN transport          text NOT NULL DEFAULT 'serial'
                                  CHECK (transport IN ('serial', 'cellular')),
    ADD COLUMN hologram_record_id text UNIQUE,   -- dedupes re-fetched API pages
    ADD COLUMN seq                bigint,        -- device message counter since boot
    ADD COLUMN rssi_dbm           int,           -- signal strength at send time
    ADD COLUMN reg_status         int,           -- +CGREG stat: 1 home, 5 roaming
    ADD COLUMN operator           text,          -- MCC-MNC, e.g. 310-260
    ADD COLUMN band               text,          -- e.g. B12
    ADD COLUMN battery_mv         int;

ALTER TABLE device_logs
    ADD COLUMN transport          text NOT NULL DEFAULT 'serial'
                                  CHECK (transport IN ('serial', 'cellular')),
    ADD COLUMN hologram_record_id text UNIQUE;

-- Every cellular message in one stream (readings, sensor errors, boots).
CREATE VIEW cellular_messages AS
SELECT device_id, received_at, 'reading' AS kind,
       seq,
       (payload->>'send_fail')::bigint AS send_fail,
       (payload->>'uptime_s')::bigint  AS uptime_s,
       rssi_dbm, band, battery_mv
FROM readings
WHERE transport = 'cellular'
UNION ALL
SELECT device_id, received_at, code AS kind,
       (payload->>'seq')::bigint,
       (payload->>'send_fail')::bigint,
       (payload->>'uptime_s')::bigint,
       (payload->>'rssi_dbm')::int, payload->>'band', (payload->>'batt_mv')::int
FROM device_logs
WHERE transport = 'cellular' AND payload ? 'seq';

-- Missing messages. The device numbers every message (seq, reset to 0 at boot)
-- and counts the sends that failed on its side (send_fail). So for each gap:
--   failed_on_device = sends the device knows failed (no signal, TCP error, ...)
--   lost_after_send  = messages the device sent but that never reached us
CREATE VIEW cellular_gaps AS
SELECT device_id,
       prev_at                             AS last_message_before_gap,
       received_at                         AS first_message_after_gap,
       seq - prev_seq - 1                  AS missing,
       send_fail - prev_send_fail          AS failed_on_device,
       (seq - prev_seq - 1) - (send_fail - prev_send_fail) AS lost_after_send
FROM (
    SELECT device_id, received_at, seq, send_fail,
           lag(received_at) OVER w AS prev_at,
           lag(seq)         OVER w AS prev_seq,
           lag(send_fail)   OVER w AS prev_send_fail
    FROM cellular_messages
    WINDOW w AS (PARTITION BY device_id ORDER BY received_at)
) m
WHERE seq > 0                 -- seq 0 is the first message after a boot
  AND prev_seq IS NOT NULL
  AND seq > prev_seq + 1;

-- Health thresholds depend on how the device reports: every 10 s over the
-- serial bridge, every 15 minutes over cellular. Rebuild the views so
-- "stale" means two missed cellular sends, not two minutes.
DROP VIEW device_health;
DROP VIEW latest_readings;

CREATE VIEW latest_readings AS
SELECT DISTINCT ON (device_id) *
FROM readings
ORDER BY device_id, received_at DESC;

CREATE VIEW device_health AS
SELECT
    d.device_id,
    d.last_seen,
    round(extract(epoch FROM now() - d.last_seen))::int            AS seconds_since_seen,
    lr.received_at                                                  AS last_reading_at,
    lr.temp_c,
    lr.humidity_pct,
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
        WHEN now() - d.last_seen > CASE WHEN lr.transport = 'cellular'
                                        THEN interval '35 minutes'
                                        ELSE interval '2 minutes' END THEN 'stale'
        WHEN lr.transport = 'serial'
             AND (SELECT l.code FROM device_logs l
                   WHERE l.device_id = d.device_id AND l.category = 'status'
                   ORDER BY l.received_at DESC LIMIT 1) = 'offline' THEN 'offline'
        ELSE 'ok'
    END                                                             AS health,
    -- added in 002
    lr.transport                                                    AS last_transport,
    lr.rssi_dbm                                                     AS last_rssi_dbm,
    lr.operator                                                     AS last_operator,
    lr.band                                                         AS last_band,
    lr.battery_mv                                                   AS last_battery_mv
FROM devices d
LEFT JOIN latest_readings lr USING (device_id);
