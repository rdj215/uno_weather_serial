# Cellular Weather Node (Arduino Uno + Hologram SIM)

A small IoT weather station built to work through the full life of a cellular IoT deployment: device bring-up, data transport over MQTT, connectivity monitoring, and troubleshooting.

An Arduino Uno with a Botletics SIM7000G shield reports temperature, humidity, and its own connection health over LTE Cat-M1 using a Hologram SIM. Readings go through the Hologram cloud, a Python poller pulls them from the Hologram REST API into Postgres, and SQL views flag stale devices and lost messages. A USB-serial path through a local MQTT broker is kept for bench testing.

The device bring-up is written up as a runbook: [docs/bringup-runbook.md](docs/bringup-runbook.md).

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
| 3. Cellular bring-up | SIM7000G shield + Hologram SIM registered on LTE-M; data session, HTTP, NTP and SMS verified | ✅ Done ([runbook](docs/bringup-runbook.md)) |
| 4. Storage | Readings and device logs stored in Postgres, with device health and message-gap views | ✅ Done (local) |
| 5. Cellular data path | `cellular_weather.ino` sends readings to the Hologram cloud every 15 minutes; `hologram_poller.py` pulls them from the REST API into Postgres | ✅ Running live |
| 6. Monitoring | Health checks against the Hologram API (connection status, data usage) with alerting | Planned |

## Architecture

**Cellular (the main path)**

```mermaid
flowchart LR
    S[DHT11 sensor] --> U[Arduino Uno + SIM7000G<br/>cellular_weather.ino]
    U -- "LTE Cat-M1, TCP<br/>Hologram SIM" --> H[Hologram cloud]
    H -- "REST API<br/>GET /csr/data" --> P[hologram_poller.py]
    P --> D[(Postgres)]
```

**Serial (bench testing)**

```mermaid
flowchart LR
    S[DHT11 sensor] --> U[Arduino Uno<br/>uno_weather_serial.ino]
    U -- "USB serial, JSON lines" --> B[serial_mqtt_bridge.py]
    B -- MQTT --> M[(Mosquitto)]
    M --> W[mqtt_to_postgres.py]
    W --> D[(Postgres)]
```

Both paths write the same tables, marked with `transport = 'cellular'` or `'serial'`. Mosquitto and Postgres run locally in Docker.

**Why the Hologram cloud instead of MQTT over cellular:** the local broker isn't reachable from a cellular device, and hosted brokers generally require TLS, which is hard to fit in an Uno's 2 KB of RAM. Sending a small TCP message to Hologram and pulling it back through the REST API needs no public server and no TLS on the device.

## Hardware

- ELEGOO Uno R3 (Arduino Uno compatible)
- Botletics SIM7000G LTE Cat-M1/NB-IoT shield, with its dual LTE/GPS antenna
- Hologram SIM (Global-3 profile, APN `hologram`)
- 3.7 V LiPo battery, 1000 mAh or more: the modem runs from it, not from USB
- DHT11 temperature/humidity module (3-pin, from the ELEGOO 37-in-1 sensor kit; has a built-in pull-up resistor)

### Wiring

The shield stacks on the Uno. The DHT11 plugs straight into the shield's top header, which passes through to the Uno, in the same position as in the photo above:

| DHT11 pin | Pin | Role |
|---|---|---|
| − (outer) | GND (next to AREF) | Ground |
| + (middle) | 13 | Power, driven HIGH by the sketch |
| S (signal) | 12 | Data |

The shield uses pins 6 (power key), 7 (reset), 10 (modem TX → Uno RX) and 11 (Uno TX → modem RX), so 12 and 13 are free. Sitting on the board, the sensor reads a few degrees high from the boards' heat; moving it onto jumper wires is a later improvement (see the troubleshooting log).

## Repository layout

| File | Purpose |
|---|---|
| `cellular_weather/cellular_weather.ino` | Device sketch: reads the DHT11 and the modem's connection status, sends both to the Hologram cloud every 15 minutes |
| `cellular_weather/secrets.example.h` | Template for `secrets.h`, which holds the Hologram device key (gitignored) |
| `modem_diagnostics/modem_diagnostics.ino` | Bring-up and troubleshooting sketch: finds the modem safely, logs signal, registration, battery and the modem's own power messages |
| `hologram_poller.py` | Pulls the device's messages from the Hologram REST API into Postgres |
| `docs/bringup-runbook.md` | Bring-up runbook: hardware to verified cellular data session |
| `uno_weather_serial.ino` | Bench-test sketch: reads the DHT11 every 10 s and prints JSON over USB |
| `serial_mqtt_bridge.py` | Reads the Uno's serial output and publishes it to MQTT |
| `mqtt_to_postgres.py` | Subscribes to MQTT and writes readings and device logs to Postgres |
| `docker-compose.yml` | Local Postgres, plus Mosquitto for bench testing (`--profile bench`) |
| `db/init/001_schema.sql` | Tables, indexes and views, applied on first database start |
| `db/init/002_cellular.sql` | Cellular columns, message-gap views and transport-aware health view |
| `mosquitto/mosquitto.conf` | Local broker config |
| `requirements.txt` | Python dependencies |

## Setup: cellular

Bring the hardware up first with the [runbook](docs/bringup-runbook.md). Then:

### 1. Configure and flash the device

1. Copy `cellular_weather/secrets.example.h` to `cellular_weather/secrets.h` and put the device key in it (Hologram dashboard → device → Device key).
2. Open `cellular_weather/cellular_weather.ino`, select **Arduino Uno** and the port, and upload.
3. Serial Monitor at **115200 baud**, "Both NL & CR". Within about a minute you should see a `STATUS` line marked `(registered)`, then `Hologram accepted the message` for a boot event and a first reading. Type `send` to send a reading on demand.

### 2. Start Postgres and apply the cellular migration

```bash
docker compose up -d
# only for a database created before 002_cellular.sql existed:
docker compose exec -T postgres psql -U weather -v ON_ERROR_STOP=1 < db/init/002_cellular.sql
```

### 3. Run the poller

Create an API key in the Hologram dashboard (Account → API keys) and a `.env` file next to the scripts (gitignored):

```
HOLOGRAM_API_KEY=your-api-key
HOLOGRAM_DEVICE_ID=your-device-id
```

The device ID is the number on the device's dashboard page. Then:

```bash
source .venv/bin/activate
python3 hologram_poller.py --once --dump   # first run: shows the raw API response
python3 hologram_poller.py                 # polls every 60 s
```

## Setup: serial bench testing

### 1. Flash the Uno

1. In the Arduino IDE, install **DHT sensor library** by Adafruit (choose *Install All* to include Adafruit Unified Sensor).
2. Open `uno_weather_serial.ino`, select **Arduino Uno** and the board's port, and upload.
3. Open the Serial Monitor at 9600 baud to confirm readings, then close it before starting the bridge.

### 2. Start Postgres and the broker

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
docker compose --profile bench up -d
docker compose ps        # both postgres and mosquitto should show "running"
```

Mosquitto is only needed for this serial path, so it sits behind the `bench` profile; a plain `docker compose up -d` starts Postgres alone. Stop the broker afterwards with `docker compose --profile bench stop mosquitto`. The schema in `db/init/` is applied automatically the first time Postgres starts. Both services listen only on `127.0.0.1`.

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

### Cellular messages

The device sends one TCP message per reading to `cloudsocket.hologram.io:9999`:

```json
{"k":"<device key>","d":"<reading as escaped JSON>","t":["weather"]}
```

followed by `\r\r`, Hologram's end-of-message marker. Hologram replies `[0,0]` when it accepts a message (the first number is the result code; `3` means the device key is invalid). The reading inside `d` carries the sensor values plus the device's own view of its connection:

```json
{"t_c":22.4,"rh":41.0,"uptime_s":965,"reads":2,"read_errors":0,"seq":2,"send_fail":0,
 "rssi_dbm":-77,"reg":5,"op":"310-260","band":"B12","batt_mv":3987}
```

| Field | Meaning |
|---|---|
| `seq` | Message counter since boot. A gap means messages were lost |
| `send_fail` | Sends that failed on the device since boot (no registration, TCP error, rejected). Lets a gap be split into "failed on the device" and "lost after sending" |
| `rssi_dbm`, `reg`, `op`, `band` | Signal strength, registration status (1 home, 5 roaming), carrier (MCC-MNC) and LTE band at send time |
| `batt_mv` | LiPo voltage |

Sensor failures are sent as `{"error":"dht_read_failed",...}` and restarts as `{"event":"boot",...}` with the same connection fields. About 220 bytes of payload per message; check the dashboard's data usage after the first few sends to confirm the real per-message cost, including TCP overhead.

## Storage

`mqtt_to_postgres.py` writes every message into one of two tables:

| Table | Contents |
|---|---|
| `readings` | Temperature, humidity, the device's counters, and the original JSON payload |
| `device_logs` | Everything else, with a `level` (`info`/`warning`/`error`), `category` (`event`/`error`/`status`), `code` (`boot`, `dht_read_failed`, `device_silent`, `offline`, ...) and `source` (`device` or `bridge`) |
| `devices` | One row per device with `first_seen` and `last_seen`; only messages from the device itself update `last_seen` |

Rows are marked `transport = 'serial'` or `'cellular'`; cellular readings also store `seq`, `rssi_dbm`, `reg_status`, `operator`, `band`, `battery_mv` and the Hologram record ID (used to skip messages fetched twice).

Views:

| View | What it answers |
|---|---|
| `latest_readings` | The newest reading per device |
| `device_health` | One row per device: time since last heard from, sensor error rate, problems in the last 24 hours, last signal/carrier/band/battery, and an overall `ok` / `stale` / `offline`. "Stale" means 2 minutes without data over serial, or 35 minutes (two missed sends) over cellular. This is the query an alerting job would poll |
| `cellular_messages` | Every cellular message (readings, errors, boots) in one time-ordered stream |
| `cellular_gaps` | Each gap in the device's message counter, split into messages that failed on the device and messages lost after sending |

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

-- Gaps: how long the device was silent each time (serial)
SELECT received_at, (payload->>'silent_s')::int AS silent_s
FROM device_logs
WHERE code = 'device_resumed'
ORDER BY received_at DESC;

-- Lost cellular messages, and whose side lost them
SELECT * FROM cellular_gaps ORDER BY first_message_after_gap DESC;

-- Signal and battery over the last day (cellular)
SELECT received_at, rssi_dbm, operator, band, battery_mv
FROM readings
WHERE transport = 'cellular' AND received_at > now() - interval '24 hours'
ORDER BY received_at;
```

## Reliability features

**Device (`cellular_weather.ino`)**

- **Safe power-on:** the SIM7000's power key toggles power, so pressing it while the modem is on switches it off. The sketch checks whether the modem answers (at 9600 and 115200 baud) and presses the key only if it's silent.
- **Self-recovery:** if the modem stops answering three status checks in a row, the sketch finds it again and reapplies its configuration.
- **Explains its own failures:** messages the modem sends on its own (`UNDER-VOLTAGE POWER DOWN`, `NORMAL POWER DOWN`, `SMS Ready`) are logged with a timestamp and kept as "last message from the modem" for when it goes quiet.
- **Doesn't send blind:** checks registration before each send and counts every failure in `send_fail`, which travels with the next successful message.
- **Checks Hologram's reply:** `[0,0]` is success; any other code is logged, with an explicit message for an invalid device key.
- **Fits the Uno:** fixed strings live in flash; RAM use is about 53% with headroom for the stack.

**Poller (`hologram_poller.py`)**

- **Resumes where it left off:** fetches from just before the newest cellular message already stored, with overlap; the Hologram record ID makes re-fetched messages a no-op.
- **Pages through results** with the API's cursor, with a safety cap per poll.
- **Rejects bad data once:** messages that fail validation or database checks go to `dead_letter.jsonl` with the reason and aren't re-reported on later polls.
- **Clear failure modes:** a refused API key stops with a message naming the setting to check; API or database outages are logged and retried on the next poll.

**Bridge**

- **Last will and testament:** the broker publishes `offline` to the status topic if the bridge disconnects unexpectedly, so subscribers can tell "no new data" apart from "bridge is down".
- **Reconnects:** MQTT reconnects with backoff (1–30 s) and queues QoS 1 messages while the broker is unreachable; the serial port is reopened automatically if the Uno is unplugged.
- **Silence detection:** if the device sends nothing for 35 s (readings arrive every 10 s), the bridge publishes a `device_silent` event, then `device_resumed` with the length of the gap when data returns. Serial reconnects are published as `serial_reopened`.
- **Input validation:** partial or unrecognized lines are skipped rather than published.
- **No guessing:** if more than one board is connected, the bridge lists the ports and asks for `--port` instead of picking one.
- **Exclusive port access:** the bridge locks the serial port while it runs, so the Arduino Serial Monitor or a second copy of the bridge gets "resource busy" instead of quietly sharing the port and taking some of the readings.

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

### Readings missing from the database while the bridge was running

**Symptom:** a SQL query for gaps of more than 15 s between readings found seven gaps of 20 s to nearly 3 minutes in a 13-minute window.

**Diagnosis:** the device's own counters ruled out the device. `uptime_s` climbed steadily (no restart), `read_errors` stayed at 1 (no sensor failures), and `reads` skipped exactly the missing readings, so the Uno took and printed every reading. The bridge logged no `device_silent` warning, so it was still receiving some lines. The likely cause: a second program had the same serial port open (the Arduino Serial Monitor or a second bridge). On macOS two programs can open a port at once, and each line goes to whichever reads it first. The second program had been closed by the time of the investigation, so this wasn't confirmed directly.

**Fix:** the bridge now locks the port for exclusive use. A second program gets "resource busy" instead of silently taking readings. Tested with a non-root process trying to open the locked port.

### Modem didn't answer at all: `Couldn't find modem`

**Symptom:** the first upload of the Botletics `LTE_Demo` sketch sent `AT` repeatedly with no reply.

**Diagnosis:** the shield's stacking headers had been pushed into place but not soldered. They held the shield on the Uno but made no electrical contact, so the Uno's signals on pins 10 and 11 never reached the modem.

**Fix:** soldered every header pin. The modem answered on the next attempt.

### Registered nowhere: signal 99, "searching"

**Symptom:** `AT+CSQ` returned `99,99` (no signal) and `AT+CGREG?` returned `0,2` (searching) for several minutes. `AT+CPSI?` showed `NO SERVICE`.

**Diagnosis:** the SIM was readable (`+CPIN: READY`, ICCID matched the dashboard), so not a SIM problem. The SIM7000G's default is to scan every band and radio type worldwide, which is slow and can fail to settle. This is a known issue with this modem and Hologram SIMs.

**Fix:** restricted the modem to LTE (`AT+CNMP=38`), Cat-M (`AT+CMNB=1`) and the US Cat-M bands (`AT+CBANDCFG="CAT-M",2,4,12,13`), then restarted the radio (`AT+CFUN=0`, `AT+CFUN=1`). The Hologram dashboard showed a session open within minutes, on T-Mobile LTE. The device sketches now apply these settings at every start.

### Raw AT commands got no reply, then `ERROR`

**Symptom:** in the demo's passthrough mode, typing `AT` produced nothing. After a settings change, the next `AT` returned `ERROR`.

**Diagnosis:** the Serial Monitor's line ending was "No Line Ending", so the modem never received the carriage return that ends a command. The earlier `AT`s accumulated in its buffer, and when one finally arrived with a line ending the modem saw `ATATAT…`.

**Fix:** line ending set to "Both NL & CR", and `AT` sent once more.

### Modem went silent after registering (open)

**Symptom:** after registering, the modem stopped answering. Restarting the demo alternated between `Couldn't find modem` and a menu whose commands all came back blank. The next morning, after the battery had charged overnight, it answered without a restart and stayed up through the data and SMS tests.

**Diagnosis so far:** two explanations fit and the evidence doesn't separate them: the LiPo was too low to supply the modem's current bursts when attaching to the network, or the demo's start-up power-key pulse switched off a modem that was already on. Not confirmed.

**Changes:** the device sketches press the power key only when the modem is silent, log the battery voltage, and log the modem's own power-down messages, so the next occurrence will identify the cause.

### Text message accepted but not delivered

**Symptom:** a test SMS from the device returned `+CMGS: 1` (accepted by the network), but the destination number lacked the US country code (`+215…` instead of `+1215…`).

**Lesson:** `+CMGS` means the network accepted the message, not that it was delivered. Numbers need full international format. For testing, Hologram's cloud number for the SIM's profile is the reliable target, since the result shows up in the dashboard.

### Every sensor read failing, seen remotely

**Symptom:** after the first live cellular messages arrived, every one was `{"error":"dht_read_failed",...}`, with `read_errors` equal to `reads`. The modem side was healthy: every send was accepted, signal −71 to −77 dBm.

**Diagnosis:** two separate faults, found in turn.
1. The sensor had been left unplugged after the shield was fitted. Reconnecting it (while powered) didn't fix the failures.
2. The bench-test sketch also failed, but it turned out to be reading pin 12 while the sensor was wired to pin 2, so that test proved nothing. Matching hardware to code instead (sensor plugged straight into GND/13/12, the configuration that had worked before) gave good readings immediately. So the sensor was fine and the fault was in the pin-2 jumper wiring. The exact bad connection wasn't identified.

**Fix:** the cellular sketch now uses the known-good pins 12/13. Real readings arrived in Hologram and Postgres within minutes.

**Lessons:** the device's own counters made the fault visible from the dashboard without touching the hardware; and a test only isolates a fault if it matches the setup under test.

### Battery voltage reads about 5,000 mV (open)

**Observation:** every cellular message reports `batt_mv` of about 4,990–5,020. A 3.7 V LiPo can't exceed about 4,200 mV, so the modem is most likely not seeing the battery and is running from the shield's charging circuit. Being investigated: the battery connector and seating. This matters because running without the battery is one of the two suspects for the modem going silent.

### Carrier changed between messages

**Observation:** messages arrived via T-Mobile (`op` 310-260) and later via AT&T (310-410), both on band 12, with no change on the device. The Hologram SIM roams onto partner networks on its own. Recording the carrier in every message makes switches like this visible, which matters when a device's behavior changes and the cause might be the network rather than the device.

### Temperature reads a few degrees high

**Observation:** about 28–29 °C in a cooler room. The sensor sits directly on the board stack, so it picks up heat from the boards, and the DHT11 is only accurate to ±2 °C. Moving it a few inches away on jumper wires (once the jumper wiring is sorted out) should bring readings closer to room temperature.

## Roadmap

- [x] Solder the SIM7000G shield and power it from a LiPo
- [x] Activate the Hologram SIM and register on LTE Cat-M1
- [x] Verify data (NTP, HTTP) and SMS in both directions
- [x] Write a bring-up runbook from SIM activation to first data session
- [x] Store readings and device logs in Postgres
- [x] Send readings with signal, registration, carrier, band and battery over cellular
- [x] Pull messages from the Hologram REST API into Postgres
- [ ] Find out why the modem reports about 5,000 mV for the battery (see troubleshooting log)
- [ ] Confirm the per-message data cost from the dashboard and tune the send interval
- [ ] Health checks against the Hologram API (connection status, data usage) with alerts
- [ ] Find the root cause of the modem going silent after registering (see troubleshooting log)
