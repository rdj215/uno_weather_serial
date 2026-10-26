# Cellular Weather Node (Arduino Uno + Hologram SIM)

A small IoT weather station built to work through the full life of a cellular IoT deployment: device bring-up, data transport over MQTT, connectivity monitoring, and troubleshooting.

The end goal is an Arduino Uno that reports temperature, humidity, and its own connection health over LTE Cat-M1 using a Hologram SIM, with Python tooling that watches device health through the Hologram API. It's being built in stages; the current stage uses a laptop as the network link while the cellular shield is on the way.

<p align="center">
  <img src="docs/uno-dht11.jpg" alt="ELEGOO Uno R3 with a DHT11 sensor module plugged into the GND, 13 and 12 header pins, powered over USB" width="450">
  <br>
  <em>Stage 1: DHT11 plugged straight into the Uno's GND, 13 and 12 pins. The orange "L" LED is lit because pin 13 powers the sensor.</em>
</p>

## Status

| Stage | What it covers | Status |
|---|---|---|
| 1. Sensor bring-up | Uno reads a DHT11 and emits one JSON line per reading over USB serial | ✅ Done |
| 2. Serial-to-MQTT bridge | Python bridge publishes readings to MQTT with online/offline status and fault handling | ✅ Done |
| 3. Cellular | Botletics SIM7000G shield + Hologram SIM; Uno publishes over Cat-M1 directly, with signal and network diagnostics | ⏳ Hardware ordered |
| 4. Storage | Readings and device logs stored in Postgres, with a device health view | ✅ Done (local) |
| 5. Monitoring | Python checks against the Hologram REST API (device status, last session, data usage) with alerting | Planned |

## Architecture

**Today**

```mermaid
flowchart LR
    S[DHT11 sensor] --> U[Arduino Uno]
    U -- "USB serial, JSON lines" --> B[serial_mqtt_bridge.py<br/>on laptop]
    B -- MQTT --> M[(Mosquitto broker)]
    M --> W[mqtt_to_postgres.py]
    W --> D[(Postgres)]
```

The broker and Postgres run locally in Docker.

**Planned**

```mermaid
flowchart LR
    S[DHT11 sensor] --> U[Arduino Uno + SIM7000G]
    U -- "LTE Cat-M1<br/>Hologram SIM" --> M[(MQTT broker)]
    M --> D[(SQL database)]
    H[Hologram API] --> P[Python health checks]
    P --> A[Alerts]
```

The bridge is a deliberate stand-in for the cellular link: the Uno's output format stays the same, so when the shield arrives only the transport changes.

## Hardware

- ELEGOO Uno R3 (Arduino Uno compatible)
- DHT11 temperature/humidity module (3-pin, from the ELEGOO 37-in-1 sensor kit; has a built-in pull-up resistor)
- Coming in stage 3: Botletics SIM7000G LTE Cat-M1/NB-IoT shield, Hologram SIM, 3.7 V LiPo battery (1000 mAh or more)

### Wiring

The DHT11 module plugs directly into the Uno's top header, no jumper wires needed:

| DHT11 pin | Uno pin | Role |
|---|---|---|
| − (ground) | GND (next to AREF) | Ground |
| + (power) | 13 | Power, driven HIGH by the sketch |
| S (signal) | 12 | Data |

Pin 13 can power the sensor because the DHT11 draws only a few milliamps. The Uno's "L" LED stays lit as a side effect.

## Repository layout

| File | Purpose |
|---|---|
| `uno_weather_serial.ino` | Arduino sketch: reads the DHT11 every 10 s and prints JSON |
| `serial_mqtt_bridge.py` | Reads the Uno's serial output and publishes it to MQTT |
| `mqtt_to_postgres.py` | Subscribes to MQTT and writes readings and device logs to Postgres |
| `docker-compose.yml` | Local Postgres and Mosquitto |
| `db/init/001_schema.sql` | Tables, indexes and views, applied on first database start |
| `mosquitto/mosquitto.conf` | Local broker config |
| `requirements.txt` | Python dependencies |

## Setup

### 1. Flash the Uno

1. In the Arduino IDE, install **DHT sensor library** by Adafruit (choose *Install All* to include Adafruit Unified Sensor).
2. Open `uno_weather_serial.ino`, select **Arduino Uno** and the board's port, and upload.
3. Open the Serial Monitor at 9600 baud to confirm readings, then close it before starting the bridge.

### 2. Start Postgres and the broker

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
docker compose up -d
docker compose ps        # both services should show "running"
```

The schema in `db/init/` is applied automatically the first time Postgres starts. Both services listen only on `127.0.0.1`.

### 3. Install the Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Run the database writer and the bridge

In one terminal:

```bash
python3 mqtt_to_postgres.py
```

In a second terminal (find the Uno's port with `ls /dev/cu.*`):

```bash
python3 serial_mqtt_bridge.py --port /dev/cu.usbmodem1101
```

Use `--dry-run` on the bridge to print messages without publishing. To watch the raw MQTT traffic:

```bash
docker compose exec mosquitto mosquitto_sub -t 'hologram-mvp/#' -v
```

Both scripts accept `--broker` to use a different broker, such as `test.mosquitto.org`. That one is public, so anyone can read what's published there, and anyone could publish to your topics too.

## Data format

The Uno prints one JSON object per line. The bridge adds `device_id` and `received_at` (UTC) and publishes to a topic based on the message type.

| Topic | When | Example payload |
|---|---|---|
| `hologram-mvp/<device>/reading` | Successful read (retained) | `{"t_c":28.9,"rh":37.0,"uptime_s":20,"reads":3,"read_errors":1,"device_id":"uno-weather-01","received_at":"2026-10-25T06:14:14+00:00"}` |
| `hologram-mvp/<device>/error` | Sensor read failed | `{"error":"dht_read_failed","uptime_s":0,"reads":1,"read_errors":1,...}` |
| `hologram-mvp/<device>/event` | Device boot, or a health event seen by the bridge | `{"event":"boot",...}` / `{"event":"device_silent","source":"bridge","silent_s":36,...}` |
| `hologram-mvp/<device>/status` | Bridge connects or drops (retained) | `online` / `offline` |

`reads` and `read_errors` are running counters, so a subscriber can compute the sensor's error rate without storing history.

## Storage

`mqtt_to_postgres.py` writes every message into one of two tables:

| Table | Contents |
|---|---|
| `readings` | Temperature, humidity, the device's counters, and the original JSON payload |
| `device_logs` | Everything else, with a `level` (`info`/`warning`/`error`), `category` (`event`/`error`/`status`), `code` (`boot`, `dht_read_failed`, `device_silent`, `offline`, ...) and `source` (`device` or `bridge`) |
| `devices` | One row per device with `first_seen` and `last_seen`; only messages from the device itself update `last_seen` |

Two views sit on top: `latest_readings` (the newest reading per device) and `device_health`, which gives one row per device with how long since it was last heard from, its sensor error rate, problems in the last 24 hours, and an overall `ok` / `stale` / `offline` status. That view is the query a future alerting job would poll.

Connect with any Postgres client using `postgresql://weather:weather@localhost:5432/weather`, or:

```bash
docker compose exec postgres psql -U weather
```

Example queries:

```sql
-- Is the device healthy?
SELECT device_id, health, seconds_since_seen, sensor_error_pct, problems_24h
FROM device_health;

-- Hourly averages for the last day
SELECT date_trunc('hour', received_at) AS hour,
       round(avg(temp_c), 1) AS avg_temp_c,
       round(avg(humidity_pct), 1) AS avg_humidity
FROM readings
WHERE received_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 1;

-- Recent warnings and errors
SELECT received_at, level, code, source, payload
FROM device_logs
WHERE level <> 'info'
ORDER BY received_at DESC
LIMIT 20;

-- Gaps: how long the device was silent each time
SELECT received_at, (payload->>'silent_s')::int AS silent_s
FROM device_logs
WHERE code = 'device_resumed'
ORDER BY received_at DESC;
```

## Reliability features

**Bridge**

- **Last will and testament:** the broker publishes `offline` to the status topic if the bridge disconnects unexpectedly, so subscribers can tell "no new data" apart from "bridge is down".
- **Reconnects:** MQTT reconnects with backoff (1–30 s) and queues QoS 1 messages while the broker is unreachable; the serial port is reopened automatically if the Uno is unplugged.
- **Silence detection:** if the device sends nothing for 35 s (readings arrive every 10 s), the bridge publishes a `device_silent` event, then `device_resumed` with the length of the gap when data returns. Serial reconnects are published as `serial_reopened`.
- **Input validation:** partial or unrecognized lines are skipped rather than published.
- **No guessing:** if more than one board is connected, the bridge lists the ports and asks for `--port` instead of picking one.

**Database writer**

- **No lost messages while it's stopped:** it uses a persistent MQTT session (fixed client ID, `clean_session=False`), so the broker holds QoS 1 messages until it reconnects.
- **No duplicates:** retained readings redelivered on reconnect hit a unique key and are skipped; retained status messages are ignored because they're replays, not changes.
- **Defensive about input:** device IDs come from the topic and must match `[A-Za-z0-9_-]`, payloads over 4 KB are dropped, and database `CHECK` constraints reject impossible values (for example a temperature outside the DHT11's range).
- **Dead-letter file:** anything that can't be stored, whether bad data or the database being down, is appended to `dead_letter.jsonl` with the reason, instead of disappearing.
- **Reconnects** to Postgres and the broker on its own.

## Troubleshooting log

Real issues hit during the build, how they were diagnosed, and what changed as a result.

### Bridge connected but received no data

**Symptom:** the bridge opened `/dev/cu.usbserial-1140` and then logged `no data from device for 36 s`. The Arduino Serial Monitor showed readings normally.

**Diagnosis:** ruled out the usual causes (Serial Monitor holding the port, sketch not running), then listed the serial devices with `ls /dev/cu.*`. There were two: `usbmodem1101` and `usbserial-1140`. A second board (an ESP8266 NodeMCU, whose CP2102 USB chip also appears as `usbserial`) was plugged in, and auto-detection had picked it instead of the Uno.

**Fix:** ran the bridge with `--port /dev/cu.usbmodem1101`. Changed auto-detection to refuse to guess when several boards match and print the candidates instead, so the failure is obvious rather than silent.

### First reading after power-up fails

**Symptom:** each boot logs one `dht_read_failed`, then reads succeed.

**Diagnosis:** the DHT11 needs about a second after power-up before it responds. Since the sketch powers the sensor from pin 13, it starts cold on every boot.

**Fix:** the sketch waits one second after powering the sensor. Occasional first-read failures are expected and visible in the `read_errors` counter rather than hidden.

### Cut-off line when the bridge starts

**Symptom:** the first line received sometimes looks like `{"evs":2,"read_errors":1}`.

**Diagnosis:** opening the serial port resets the Uno, which interrupts whatever line it was printing. The tail of that line can still be valid JSON.

**Fix:** the bridge only publishes messages it recognizes (readings, errors, events) and skips everything else.

### Temperature reads a few degrees high

**Observation:** about 29 °C in a cooler room. The sensor is plugged directly into the Uno, so it picks up heat from the board, and the DHT11 is only accurate to ±2 °C. Moving the sensor a few inches away on jumper wires should bring readings closer to room temperature.

## Roadmap

- [ ] Solder headers onto the SIM7000G shield and power it from a LiPo (the modem's transmit spikes can reach about 2 A, more than USB reliably supplies)
- [ ] Activate the Hologram SIM and attach to Cat-M1, with NB-IoT as a fallback
- [ ] Report signal strength (RSSI), registration status, and radio technology alongside each reading
- [ ] Publish over cellular from the Uno, retiring the laptop bridge
- [x] Store readings and device logs in Postgres
- [ ] Move the broker somewhere the cellular device can reach (a hosted broker with TLS and authentication), since the local one is only reachable from this laptop
- [ ] Python health checks against the Hologram API with offline and data-usage alerts
- [ ] Write a bring-up runbook for provisioning a new device from SIM activation to first message
