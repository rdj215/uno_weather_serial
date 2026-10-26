# Agent guide

Context for AI coding agents working in this repo: the hardware, architecture, how to run it, and the conventions to follow.

## Project goal

A cellular IoT weather node. An Arduino Uno reads a DHT11 temperature/humidity sensor, and readings flow over MQTT into Postgres, where views report device health. Today a laptop bridge carries the data over USB serial. The target is for the Uno to publish MQTT itself over LTE Cat-M1 (NB-IoT fallback) using a SIM7000G shield and a Hologram SIM.

When choosing between options, prefer the one that improves **connectivity diagnostics, monitoring, automation, and documentation** over polish on the sensor side.

## Hardware

- **ELEGOO Uno R3** (Arduino Uno compatible). Shows up as `/dev/cu.usbmodem1101` on macOS.
- **DHT11 module** (3-pin, built-in pull-up). Plugged directly into the Uno's top header: − → GND (next to AREF), + → pin 13, S → pin 12. The sketch drives pin 13 HIGH to power the sensor, which is why the "L" LED stays on. This layout avoids jumper wires; it will change when the shield is fitted.
- **Botletics SIM7000G shield** (LTE Cat-M1/NB-IoT + GPS) and a **Hologram SIM**: not yet set up. Needs headers soldered and a 3.7 V LiPo (1000 mAh+, check polarity), because the modem's transmit spikes (~2 A) exceed what USB supplies. It covers the Uno's header, so the sensor wiring has to move.
- **ThingPulse ESP8266 weather kit (NodeMCU Amica, CP2102 USB chip)**: not used. The Uno was chosen because the SIM7000 shield is built for the Uno form factor. The NodeMCU shows up as `/dev/cu.usbserial-1140`, which caused a real port-selection bug (see Troubleshooting history).

## Architecture

```
DHT11 -> Uno --USB serial, JSON lines--> serial_mqtt_bridge.py --MQTT--> Mosquitto --> mqtt_to_postgres.py --> Postgres
```

Mosquitto and Postgres run locally via `docker-compose.yml`, bound to 127.0.0.1. The laptop bridge stands in for the cellular link: when the shield works, the Uno publishes MQTT over Cat-M1 itself and the bridge is retired. The database writer is deliberately independent of the bridge so it doesn't change at that point.

### Files

- `uno_weather_serial.ino`: reads the DHT11 every 10 s and prints one JSON object per line at 9600 baud. Uses `DHT_PIN = 12`, `DHT_POWER_PIN = 13`, Adafruit "DHT sensor library". **Its header comment still describes the old D2/5V wiring and should be updated.**
- `serial_mqtt_bridge.py`: serial → MQTT. Topics `hologram-mvp/<device_id>/{reading,error,event,status}`. Retained readings, QoS 1, last-will `offline` on the status topic, serial and MQTT auto-reconnect, publishes bridge health events (`device_silent`, `device_resumed`, `serial_reopened`, with `"source":"bridge"`). Refuses to auto-pick a port when more than one board is connected. Default broker is `localhost`.
- `mqtt_to_postgres.py`: MQTT → Postgres. Persistent session (fixed client ID, `clean_session=False`) so messages queue while it's stopped; dedupes retained replays; validates device IDs from the topic; rejects bad data and writes it with a reason to `dead_letter.jsonl`.
- `db/init/001_schema.sql`: tables `devices`, `readings`, `device_logs` (level / category / code / source / payload jsonb); views `latest_readings` and `device_health` (ok / stale / offline, sensor error %, problems in 24 h). Applied automatically on first container start; `docker compose down -v` wipes data and re-applies it.
- `mosquitto/mosquitto.conf`, `docker-compose.yml`, `requirements.txt`, `README.md`, `docs/uno-dht11.jpg`.

### Running it

```bash
docker compose up -d
source .venv/bin/activate          # Homebrew Python blocks system-wide pip, so always use the venv
python3 mqtt_to_postgres.py        # terminal 1
python3 serial_mqtt_bridge.py --port /dev/cu.usbmodem1101   # terminal 2
docker compose exec postgres psql -U weather -c "SELECT * FROM device_health"
```

Close the Arduino Serial Monitor before running the bridge; only one program can hold the port.

## Status

1. Sensor bring-up: done, readings ~29 °C / 38% RH (reads a few degrees high because the sensor sits on the board; DHT11 is ±2 °C anyway).
2. Serial → MQTT bridge: done and working with the real Uno.
3. Cellular (SIM7000G + Hologram SIM): not started.
4. Postgres storage: built and tested end to end in a simulated setup; not yet confirmed with the real Uno.
5. Hologram API monitoring (device status, last session, data usage, alerts): planned.

## Next steps

- Run the Docker stack locally and confirm rows land in Postgres from the real Uno.
- Python health checks against the Hologram REST API, alerting on offline devices and data-usage spikes.
- Cellular bring-up: solder headers, LiPo power, activate the SIM, attach on Cat-M1 with NB-IoT fallback, report RSSI / registration status / RAT with each reading, publish MQTT over cellular. The local broker isn't reachable from a cellular device, so this needs a hosted broker with TLS and auth (or Hologram's own cloud routing).
- A bring-up runbook (SIM activation to first message).
- Possibly an AI-assisted troubleshooting helper over `device_logs`.

## Troubleshooting history (keep adding to the README's log)

- **Bridge opened a port but got no data:** two USB serial devices were connected (Uno and NodeMCU), and auto-detect picked the NodeMCU. Diagnosed with `ls /dev/cu.*`; fixed by passing `--port` and making auto-detect refuse to guess.
- **One `dht_read_failed` on every boot:** the sensor needs ~1 s after power-up; expected and visible in the `read_errors` counter.
- **Cut-off first line like `{"evs":2,...}`:** opening the port resets the Uno mid-print; the bridge only publishes recognized message types.
- **`pip install` failed with `externally-managed-environment`:** Homebrew Python; fixed with a venv.

## Working conventions

- Keep the README accurate as things change: update the status table, and add real issues to the troubleshooting log (never invented ones).
- Don't overstate progress: cellular and Hologram API work stay marked as planned until they actually run.
- Use the venv for Python; don't use `--break-system-packages`.
- Don't commit secrets. `.env`, `.venv/`, `dead_letter.jsonl` and `.DS_Store` are gitignored. Hologram API keys go in `.env`.
- Keep this file in sync with any engineering changes.
