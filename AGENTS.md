# Agent guide

Context for AI coding agents working in this repo: the hardware, architecture, how to run it, and the conventions to follow.

## Project goal

A cellular IoT weather node. An Arduino Uno with a SIM7000G shield reads a DHT11 temperature/humidity sensor and sends readings, together with its own connection health (signal, registration, carrier, band, battery), over LTE-M through a Hologram SIM. Python tooling pulls the messages from the Hologram REST API into Postgres, where views report device health. A USB serial → MQTT path remains for bench testing.

When choosing between options, prefer the one that improves **connectivity diagnostics, monitoring, automation, and documentation** over polish on the sensor side.

## Hardware

- **ELEGOO Uno R3** (Arduino Uno compatible). Shows up as `/dev/cu.usbmodem1101` on macOS.
- **Botletics SIM7000G shield** stacked on the Uno, headers soldered, 3.7 V LiPo connected (the modem's transmit spikes of ~2 A exceed what USB supplies). Shield pins: 6 power key, 7 reset, 10 modem TX → Uno RX, 11 Uno TX → modem RX. **Hologram SIM** on the Global-3 profile, APN `hologram`; registers roaming on T-Mobile or AT&T LTE-M.
- **DHT11 module** (3-pin, built-in pull-up) plugged straight into the shield's top header, which passes through to the Uno: − → GND (next to AREF), + → 13 (driven HIGH by the sketch), S → 12. Pins 12 and 13 are free on the shield. A pin-2 jumper-wire setup failed every read and was abandoned; cause not found.
- **ThingPulse ESP8266 weather kit (NodeMCU Amica, CP2102 USB chip)**: not used. The Uno was chosen because the SIM7000 shield is built for the Uno form factor. The NodeMCU shows up as `/dev/cu.usbserial-1140`, which caused a real port-selection bug (see Troubleshooting history).

## Architecture

Main path (cellular):

```
DHT11 -> Uno + SIM7000G (cellular_weather.ino) --LTE-M, TCP--> Hologram cloud --REST API--> hologram_poller.py --> Postgres
```

Bench-test path (serial):

```
DHT11 -> Uno --USB serial, JSON lines--> serial_mqtt_bridge.py --MQTT--> Mosquitto --> mqtt_to_postgres.py --> Postgres
```

Both paths write the same tables, marked `transport = 'serial'` or `'cellular'`. Postgres and Mosquitto run locally via `docker-compose.yml`, bound to 127.0.0.1. Mosquitto is only used by the serial path, so it's behind the `bench` Compose profile and doesn't start by default. Hologram's cloud was chosen over MQTT-over-cellular because a hosted broker needs TLS, which doesn't fit the Uno's 2 KB of RAM, and the local broker isn't reachable from the cellular network.

### Files

- `firmware/cellular_weather/cellular_weather.ino`: device sketch. Reads the DHT11 and the modem's status, sends both as JSON to `cloudsocket.hologram.io:9999` every 15 min, authenticated with the device key from `secrets.h`. Presses the modem's power key only if the modem doesn't answer, because the key toggles power.
- `firmware/cellular_weather/secrets.example.h`: template for `secrets.h` (gitignored), which holds the Hologram device key (dashboard: SIM → Webhook tab → "SIM key").
- `firmware/modem_diagnostics/modem_diagnostics.ino`: bring-up and troubleshooting sketch. Finds the modem safely and logs signal, registration, battery voltage and the modem's own power-down messages.
- `hologram_poller.py`: polls the Hologram REST API (`GET /csr/data`) every 60 s and writes messages to Postgres. Reads `HOLOGRAM_API_KEY` and `HOLOGRAM_DEVICE_ID` from `.env`. Real API records have `data` as an object whose `data` field is the device's JSON already parsed; older records use a JSON string with a base64 payload, and the decoder handles both. `--once --dump` prints the raw response.
- `firmware/uno_weather_serial/uno_weather_serial.ino`: bench-test sketch. Reads the DHT11 every 10 s and prints one JSON object per line at 9600 baud. Uses `DHT_PIN = 12`, `DHT_POWER_PIN = 13`, Adafruit "DHT sensor library".
- `serial_mqtt_bridge.py`: serial → MQTT. Topics `hologram-mvp/<device_id>/{reading,error,event,status}`. Retained readings, QoS 1, last-will `offline` on the status topic, serial and MQTT auto-reconnect, exclusive port lock, publishes bridge health events (`device_silent`, `device_resumed`, `serial_reopened`, with `"source":"bridge"`). Refuses to auto-pick a port when more than one board is connected. Default broker is `localhost`.
- `mqtt_to_postgres.py`: MQTT → Postgres. Persistent session (fixed client ID, `clean_session=False`) so messages queue while it's stopped; dedupes retained replays; validates device IDs from the topic; rejects bad data and writes it with a reason to `dead_letter.jsonl`.
- `db/init/001_schema.sql`: tables `devices`, `readings`, `device_logs` (level / category / code / source / payload jsonb); views `latest_readings` and `device_health`.
- `db/init/002_cellular.sql`: cellular columns on `readings` and `device_logs`, views `cellular_messages` and `cellular_gaps`, and a transport-aware `device_health` (ok / stale / offline; stale means 2 min without data over serial or 35 min over cellular). Scripts in `db/init/` run only when the database is first created; apply this one manually to an existing database.
- `docs/bringup-runbook.md`: hardware to verified cellular data session.
- `mosquitto/mosquitto.conf`, `docker-compose.yml`, `requirements.txt`, `README.md`, `docs/uno-dht11.jpg`.

### Running it

```bash
docker compose up -d               # Postgres only; add --profile bench to also start Mosquitto
source .venv/bin/activate          # Homebrew Python blocks system-wide pip, so always use the venv

# Existing database only: apply the cellular migration once
docker compose exec -T postgres psql -U weather < db/init/002_cellular.sql

# Cellular path
python3 hologram_poller.py --once --dump   # check the API response
python3 hologram_poller.py                 # poll every 60 s

# Serial bench-test path (needs Mosquitto: docker compose --profile bench up -d)
python3 mqtt_to_postgres.py                                # terminal 1
python3 serial_mqtt_bridge.py --port /dev/cu.usbmodem1101  # terminal 2

docker compose exec postgres psql -U weather -c "SELECT * FROM device_health"
```

Close the Arduino Serial Monitor before running the bridge; only one program can hold the port.

## Status

1. Sensor bring-up: done.
2. Serial → MQTT bridge → Postgres: done.
3. Cellular bring-up: done. Registered on LTE-M via Hologram after restricting bands (`AT+CNMP=38`, `AT+CMNB=1`, `AT+CBANDCFG="CAT-M",2,4,12,13`); data session, NTP, HTTP and SMS verified. Written up in `docs/bringup-runbook.md`.
4. Cellular data path: running live. The device has roamed between T-Mobile (310-260) and AT&T (310-410), band 12.
5. Hologram API health checks (connection status, data usage, alerts): planned.

Open issue: every message reports `batt_mv` ≈ 5,000, impossible for a LiPo, so the modem probably isn't seeing the battery. Check the connector.

Open issue: the modem once went silent after registering; cause not confirmed (low battery vs a power-key pulse toggling a running modem off). `modem_diagnostics.ino` logs battery voltage and power-down messages to catch it.

## Next steps

- Resolve the 5,000 mV battery reading; confirm per-message data cost in the dashboard.
- Python health checks against the Hologram REST API, alerting on offline devices and data-usage spikes.
- Root-cause the modem going silent.
- Possibly an AI-assisted troubleshooting helper over `device_logs` and `cellular_gaps`.

## Troubleshooting history (keep adding to the README's log)

- **Bridge opened a port but got no data:** two USB serial devices were connected (Uno and NodeMCU), and auto-detect picked the NodeMCU. Diagnosed with `ls /dev/cu.*`; fixed by passing `--port` and making auto-detect refuse to guess.
- **One `dht_read_failed` on every boot:** the sensor needs ~1 s after power-up; expected and visible in the `read_errors` counter.
- **Cut-off first line like `{"evs":2,...}`:** opening the port resets the Uno mid-print; the bridge only publishes recognized message types.
- **`pip install` failed with `externally-managed-environment`:** Homebrew Python; fixed with a venv.
- Cellular bring-up issues (unsoldered headers, band scan, line endings, silent modem, SMS number format, failing sensor reads, battery reading, carrier changes) are in the README's troubleshooting log and the runbook.

## Working conventions

- Keep the README accurate as things change: update the status table, and add real issues to the troubleshooting log (never invented ones).
- Don't overstate progress: work stays marked as planned or pending until it actually runs.
- Use the venv for Python; don't use `--break-system-packages`.
- Don't commit secrets. `.env`, `.venv/`, `dead_letter.jsonl`, `.DS_Store` and `firmware/cellular_weather/secrets.h` are gitignored. The Hologram API key goes in `.env`, the device key in `secrets.h`. Don't put the full ICCID, phone numbers, or cell tower location/IDs in public docs.
- Keep this file in sync with any engineering changes.
