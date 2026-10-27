# Bring-up runbook: Botletics SIM7000G + Hologram SIM on an Arduino Uno

How to take a new device from parts on a desk to a verified cellular data session on Hologram, with the checks to run at each step and what to do when a check fails.

Written from the first bring-up of this project (evening of 2026-10-26 and morning of 2026-10-27, US Eastern). Every "expected output" below is what this hardware actually returned; problems hit along the way are in [Troubleshooting](#troubleshooting) and in the README's troubleshooting log.

**End state:** the modem is registered on an LTE Cat-M1 network through the Hologram SIM, a data session is open, and traffic has been confirmed from both the device and the Hologram dashboard.

**Time:** about 1–2 hours the first time, most of it soldering. Allow for charging the battery.

---

## Parts and accounts

| Item | Notes |
|---|---|
| Arduino Uno R3 (or compatible; ELEGOO used here) | |
| Botletics SIM7000 shield, **G** variant (global bands) | Comes with stacking headers (need soldering) and a dual LTE/GPS antenna |
| 3.7 V LiPo battery, 1000 mAh or more, 2-pin JST-PH connector | **Required.** The modem runs from the battery, not USB |
| Hologram SIM | Activated in the Hologram dashboard (step 2) |
| Soldering iron, rosin-core solder | 0.6–0.8 mm solder is easiest for header pins |
| Male-to-female jumper wires | Optional, for moving a sensor off the board |
| Arduino IDE with the **BotleticsSIM7000** library | Library Manager → search "Botletics" |

---

## 1. Hardware assembly

1. **Solder every header pin.** Stacking headers go in from the **top** of the shield (sockets up, long pins out the bottom); solder on the **underside of the shield**. Never solder the Uno.
   - Seat the shield on an unpowered Uno to hold the pins straight, tack one pin per header, check alignment, then solder the rest.
   - Good joint: a small shiny cone around the pin. Check for bridges between neighboring pins.
   - Critical pins: **6** (power key), **7** (reset), **10** (modem TX → Uno RX), **11** (Uno TX → modem RX), **GND**, **5V**.
   - ⚠️ Headers that are pushed in but not soldered hold the shield in place but make no electrical contact. Symptom: `Couldn't find modem`.
2. **Check the gap over the USB port.** The shield sits slightly raised on most Unos. Make sure no solder joint touches the metal USB housing; a strip of electrical tape on top of the port is standard practice.
3. **Insert the SIM:** contacts facing down, notched corner matching the holder.
4. **Attach the antenna:** the two cables cross over. LTE cable → **LTE** connector, GPS cable → **GPS/GNSS** connector, pressed straight down until they click. Lay the antenna flat, away from metal.
5. **Battery:** confirm the **+/−** markings at the shield's battery socket match the battery's **red (+)/black (−)** wires **before** plugging in. Some batteries are wired in reverse.
6. Power-up order: **battery first, then USB.** Leave the battery connected; USB charges it.

**Check:** the Uno's green ON LED lights, nothing gets warm.

---

## 2. Activate the SIM in Hologram

1. Hologram dashboard → activate the SIM and choose a plan.
2. Note, for later checks:
   - the SIM's **ICCID** (printed on the card and shown in the dashboard),
   - the **profile** the dashboard shows (this SIM: **Global-3**),
   - the **required APN**: `hologram`.

Keep the ICCID out of public documents; write it masked, e.g. `8946…5119`.

---

## 3. First contact with the modem

1. Arduino IDE: **File → Examples → BotleticsSIM7000 → LTE_Demo**. Check the pin definitions match the shield (PWRKEY 6, RST 7, TX 10, RX 11) and that `fona.setNetworkSettings(F("hologram"));` is not commented out.
2. Select **Arduino Uno** and its port. Close anything else using the port (the serial bridge locks it), then upload.
3. Serial Monitor: **9600 baud, "Both NL & CR"**.

**Expected:** the sketch powers the modem on, gets replies to `AT`, and prints a command menu ending in `Modem>`.

Menu letters are **case-sensitive**: lowercase `s` sends a text message, capital `S` opens the raw AT command passthrough.

### Baseline checks

| Send | Command | Healthy result | This device |
|---|---|---|---|
| `b` | `AT+CBC` | 3700–4200 mV | — |
| `i` | `AT+CSQ` | 10–31 (99 = no signal) | 99 on first attach |
| `n` | `AT+CGREG?` | `0,1` home or `0,5` roaming | `0,2` searching on first attach |
| `1` | `AT+CPSI?` | `CAT-M,Online,...` with operator and band | `NO SERVICE` before step 4 |

If `n` stays at `0,2` (searching) and `i` at 99 for more than a few minutes, go to step 4.

---

## 4. Network registration

On first power-up the SIM7000G scans every band and radio type worldwide, which can take a long time or never settle. Restricting it to Cat-M on the US bands fixes that.

1. Send capital **`S`** (serial passthrough). Everything typed now goes straight to the modem. Check the line ending is **Both NL & CR**.
2. Send `AT`. Expected `OK`. (A first `ERROR` is normal if earlier keystrokes were sent without a line ending; send it again.)
3. Verify the SIM:

   | Command | Expected | Meaning |
   |---|---|---|
   | `AT+CPIN?` | `+CPIN: READY` | SIM readable, no PIN lock |
   | `AT+CCID` | the ICCID | Matches the SIM activated in the dashboard |

4. Restrict the radio:

   | Command | Meaning |
   |---|---|
   | `AT+CNMP=38` | LTE only (no 2G fallback) |
   | `AT+CMNB=1` | Cat-M only (not NB-IoT) |
   | `AT+CBANDCFG="CAT-M",2,4,12,13` | US Cat-M bands |

   Each should return `OK`.
5. Restart the radio so the settings take effect: `AT+CFUN=0`, then `AT+CFUN=1`. The modem prints `SMS Ready` when the radio is back.
6. After a minute, check `AT+CSQ`, `AT+CGREG?`, `AT+CPSI?`.

**Verify from both sides:**
- Device: `+CGREG: 0,5` (registered, roaming: normal for a Hologram SIM) and a signal above 10.
- Hologram dashboard (SIM → Last connection): a session opened within the last few minutes.

**This device:** the dashboard showed a session opened at 03:48 UTC, right after the radio restart, on **T-Mobile USA**, RAT **LTE**, profile **Global-3**, APN `hologram`, data usage **0 B**. Registration had been stuck at `0,2` with signal 99 until the band restriction.

Leave raw mode by closing and reopening the Serial Monitor (this restarts the sketch; see the power-key note in Troubleshooting).

---

## 5. Data session test

From the `Modem>` menu:

| Send | What it does | Healthy result | This device |
|---|---|---|---|
| `G` | Opens a data session (`AT+CNACT=1,"hologram"`) and syncs the clock over NTP | `+APP PDP: ACTIVE`, then `+CNTP: 1,"<date>,<time>"` | `+CNTP: 1,"2026/10/27,12:54:08"` |
| `w` | Fetches a web page over HTTP | `Status: 200` | `example.com`: status 200, 577 bytes |

The NTP sync is already proof of a round trip to the internet: device → carrier → Hologram → internet → back.

**Verify in the dashboard:** data usage for the session rises above 0 B within a few minutes.

---

## 6. SMS (optional)

**To the device:** send a message to the SIM from the Hologram dashboard. The modem announces it with `+CMTI: "SM",0` (stored on the SIM in slot 0). Read it with `r` then `0`; delete it with `d` then `0`, because SIM storage is small and a full SIM stops accepting texts.

**From the device:** send to Hologram's cloud number for the SIM's profile (Global-3: `+467190002980389`); it appears in the dashboard's event history. Numbers must be in full international format: `+` and the country code, so a US number is `+1` followed by 10 digits. Hologram bills device-originated texts per message, and its documentation doesn't promise delivery to ordinary phone numbers.

---

## 7. Hand-off to the production sketch

Once steps 3–5 pass:

1. Copy `cellular_weather/secrets.example.h` to `cellular_weather/secrets.h` and put the device key in it (dashboard → device → Device key). It's gitignored.
2. Upload `cellular_weather.ino`. Serial Monitor at **115200 baud**.
3. Expected within about a minute: `modem is on and answering`, a `STATUS` line showing `(registered)`, then `Hologram accepted the message` for a boot event and a first reading.
4. On the laptop, run `hologram_poller.py` (see the README). The reading should appear in Postgres with `transport = 'cellular'`.

If the modem drops out, `modem_diagnostics.ino` prints why: battery voltage every 15 s and every message the modem sends on its own (`UNDER-VOLTAGE POWER DOWN`, `NORMAL POWER DOWN`, `SMS Ready`).

---

## Troubleshooting

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `Couldn't find modem`, every `AT` unanswered | Headers not soldered | Underside of the shield | Solder every pin (step 1) |
| Same, after it had been working | Modem off: the demo's start-up power-key pulse **toggles** power, so restarting the sketch while the modem is on can switch it off; or the battery is low | Shield's NET LED: dark = off | Full power-down: USB out, battery out, 10 s, battery in, USB in, then open the Serial Monitor. Charge the battery |
| Raw mode: typing `AT` does nothing | Line ending set to "No Line Ending" | Dropdown at the bottom of the Serial Monitor | Set **Both NL & CR** |
| First `AT` returns `ERROR` | Earlier `AT`s without line endings are still in the modem's buffer | — | Send `AT` again |
| `+CSQ: 99,99`, `+CGREG: 0,2`, `+CPSI: NO SERVICE` | Antenna, or the modem scanning every band worldwide | Antenna on the **LTE** connector and clicked in | Band restriction and radio restart (step 4) |
| Menu appears, then every command's reply is blank (`RSSI = 0`, `status 0`) | The modem stopped answering after start-up. **Not fully diagnosed** (see below) | NET LED; battery voltage | Full power-down and restart; charge the battery; run `modem_diagnostics.ino` to catch the cause |
| `+CMGS: <n>` but the text never arrives | Number missing the country code, or the destination isn't reachable from the SIM | Number format | Use `+1XXXXXXXXXX` for US numbers; test against Hologram's cloud number instead |
| Upload or bridge fails with "resource busy" | Another program has the serial port | Serial Monitor, bridge | Close the other program |

### Open issue: modem going silent after working

On the first night the modem registered (confirmed in the Hologram dashboard), then stopped answering commands. Restarting the demo alternated between `Couldn't find modem` and a menu followed by blank replies. A full power-down didn't immediately fix it. The next morning, after the battery had charged overnight on USB, it answered again without a restart and stayed up through the data and SMS tests.

Two explanations fit, and the evidence doesn't separate them:

1. **Battery:** the modem draws current bursts when attaching to the network; a low or poorly seated LiPo can't supply them and the modem browns out.
2. **Power-key toggling:** `LTE_Demo` pulses the power key on every start, which switches off a modem that's already running.

`modem_diagnostics.ino` and `cellular_weather.ino` address both: they press the power key only when the modem doesn't answer, and they log the battery voltage and the modem's own power-down messages, so the next occurrence will show which one it was.
