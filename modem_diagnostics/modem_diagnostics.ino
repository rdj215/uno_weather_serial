/*
  modem_diagnostics.ino — bring-up and connectivity monitor for the Botletics
  SIM7000G shield on an Arduino Uno with a Hologram SIM.

  What it does:
    1. Finds the modem WITHOUT blindly pressing the power key. The power key
       toggles the modem, so pressing it while the modem is already on can
       switch it off. This sketch first checks whether the modem answers (at
       9600 and 115200 baud) and only presses the key if it's silent.
    2. Prints the modem's identity, firmware, SIM status and battery voltage.
    3. Applies the network settings that got the SIM registered:
       LTE only, Cat-M only, US Cat-M bands 2/4/12/13, APN "hologram".
    4. Every 15 s prints one STATUS line: signal, registration, network, battery.
    5. Prints every unsolicited message from the modem with a timestamp. These
       explain why it drops: "UNDER-VOLTAGE POWER DOWN" means the battery
       couldn't supply it, "NORMAL POWER DOWN" means the power key was pressed,
       "SMS Ready" on its own means it just restarted.
    6. If the modem stops answering, it says so, shows the last message the
       modem sent, and tries to recover.
    7. Anything you type in the Serial Monitor is sent to the modem as an AT
       command, so you can run your own checks at any time.

  Serial Monitor: 115200 baud, "Both NL & CR".
  (115200 instead of 9600 so printing to the Mac never makes the Uno miss
  characters coming from the modem.)

  Shield pins: 6 = power key, 10 = modem TX -> Uno RX, 11 = Uno TX -> modem RX.
  The LiPo battery must be connected: the modem runs from it, not from USB.
*/

#include <SoftwareSerial.h>

const uint8_t MODEM_RX_PIN = 10;  // modem's TX arrives here
const uint8_t MODEM_TX_PIN = 11;  // Uno sends to the modem's RX from here
const uint8_t PWRKEY_PIN = 6;
const long MODEM_BAUD = 9600;
const unsigned long STATUS_INTERVAL_MS = 15000UL;
const uint8_t MISSES_BEFORE_RECOVERY = 3;

SoftwareSerial modem(MODEM_RX_PIN, MODEM_TX_PIN);

enum Result { RESULT_OK, RESULT_ERROR, RESULT_TIMEOUT };

char modemLine[100];   // line being received from the modem
uint8_t modemLen = 0;
char userLine[80];     // line being typed in the Serial Monitor
uint8_t userLen = 0;
char lastUnsolicited[80] = "(none)";  // last message the modem sent on its own

unsigned long lastStatus = 0;
uint8_t missedChecks = 0;
unsigned int recoveries = 0;

// ---------------------------------------------------------------------------
// Output helpers

void stamp() {
  Serial.print('[');
  Serial.print(millis() / 1000UL);
  Serial.print(F("s] "));
}

bool contains(const char* text, const char* word) {
  return strstr(text, word) != NULL;
}

// Lines the modem sends on its own (not in reply to a command).
void reportUnsolicited(const char* text) {
  strncpy(lastUnsolicited, text, sizeof(lastUnsolicited) - 1);
  lastUnsolicited[sizeof(lastUnsolicited) - 1] = '\0';
  stamp();
  bool serious = contains(text, "POWER DOWN") || contains(text, "UNDER-VOLTAGE") ||
                 contains(text, "OVER-VOLTAGE");
  Serial.print(serious ? F("!! modem: ") : F("modem: "));
  Serial.println(text);
  if (contains(text, "UNDER-VOLTAGE")) {
    Serial.println(F("   -> battery voltage too low for the modem. Charge the LiPo / check its connector."));
  } else if (contains(text, "NORMAL POWER DOWN")) {
    Serial.println(F("   -> the modem was switched off (power key pressed or AT+CPOWD)."));
  } else if (contains(text, "SMS Ready")) {
    Serial.println(F("   -> the modem has (re)started its radio."));
  }
}

// ---------------------------------------------------------------------------
// Reading lines from the modem

// Collects characters; returns true when a complete, non-empty line is ready.
bool readModemLine() {
  while (modem.available()) {
    char c = modem.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (modemLen == 0) continue;
      modemLine[modemLen] = '\0';
      modemLen = 0;
      return true;
    }
    if (modemLen < sizeof(modemLine) - 1) modemLine[modemLen++] = c;
  }
  return false;
}

void drainUnsolicited() {
  while (readModemLine()) reportUnsolicited(modemLine);
}

// Sends one command and waits for OK / ERROR.
//   capturePrefix: if a reply line starts with this, it's copied into out
//   verbose:       print every reply line
Result sendCommand(const char* cmd, unsigned long timeoutMs,
                   const char* capturePrefix = NULL, char* out = NULL, size_t outSize = 0,
                   bool verbose = true) {
  drainUnsolicited();
  if (out && outSize) out[0] = '\0';
  if (verbose) {
    Serial.print(F("  -> "));
    Serial.println(cmd);
  }
  modem.print(cmd);
  modem.print('\r');

  unsigned long start = millis();
  while (millis() - start < timeoutMs) {
    if (!readModemLine()) continue;
    if (strcmp(modemLine, cmd) == 0) continue;  // command echo
    if (strcmp(modemLine, "OK") == 0) {
      if (verbose) Serial.println(F("  <- OK"));
      return RESULT_OK;
    }
    if (strcmp(modemLine, "ERROR") == 0 || strncmp(modemLine, "+CME ERROR", 10) == 0) {
      Serial.print(F("  <- "));
      Serial.println(modemLine);
      return RESULT_ERROR;
    }
    bool captured = false;
    if (out && outSize) {
      // With a prefix, keep the matching line; without one (e.g. AT+CCID,
      // which replies with a bare value), keep the first line.
      bool wanted = capturePrefix
                        ? strncmp(modemLine, capturePrefix, strlen(capturePrefix)) == 0
                        : out[0] == '\0';
      if (wanted) {
        strncpy(out, modemLine, outSize - 1);
        out[outSize - 1] = '\0';
        captured = true;
      }
    }
    if (verbose) {
      Serial.print(F("  <- "));
      Serial.println(modemLine);
    } else if (!captured) {
      reportUnsolicited(modemLine);  // something else arrived mid-command
    }
  }
  if (verbose) Serial.println(F("  <- (no reply)"));
  return RESULT_TIMEOUT;
}

// ---------------------------------------------------------------------------
// Finding / powering the modem

bool answers() {
  for (uint8_t i = 0; i < 3; i++) {
    if (sendCommand("AT", 600, NULL, NULL, 0, false) == RESULT_OK) return true;
  }
  return false;
}

// Checks the modem at our speed, then at its factory speed (115200).
bool answersAtAnySpeed() {
  modem.begin(MODEM_BAUD);
  delay(50);
  if (answers()) return true;

  modem.begin(115200);
  delay(50);
  // Receiving at 115200 is unreliable on the Uno's software serial, so don't
  // wait for the reply; just tell the modem to switch to 9600 and re-check.
  for (uint8_t i = 0; i < 3; i++) {
    modem.print(F("AT\r"));
    delay(200);
  }
  modem.print(F("AT+IPR=9600\r"));
  delay(300);
  modem.begin(MODEM_BAUD);
  delay(50);
  while (modem.available()) modem.read();  // discard garbled 115200 replies
  modemLen = 0;
  if (answers()) {
    sendCommand("AT&W", 1000);  // save the speed so it survives a restart
    return true;
  }
  return false;
}

bool ensureModemOn() {
  stamp();
  Serial.println(F("looking for the modem..."));
  if (answersAtAnySpeed()) {
    stamp();
    Serial.println(F("modem is on and answering (power key not pressed)"));
    return true;
  }

  stamp();
  Serial.println(F("modem silent at 9600 and 115200 baud: assuming it's off, pressing the power key"));
  digitalWrite(PWRKEY_PIN, LOW);
  delay(500);  // >= 72 ms turns it on; < 1.2 s so it can't turn it off
  digitalWrite(PWRKEY_PIN, HIGH);

  for (uint8_t attempt = 0; attempt < 10; attempt++) {
    delay(2000);
    drainUnsolicited();
    if (answersAtAnySpeed()) {
      stamp();
      Serial.println(F("modem powered on and answering"));
      return true;
    }
  }

  stamp();
  Serial.println(F("!! modem still not answering. Check, in order:"));
  Serial.println(F("   1. LiPo battery plugged in and charged (the modem runs from it)"));
  Serial.println(F("   2. NET LED: dark = modem off, blinking = on but not hearing the Uno"));
  Serial.println(F("   3. solder joints on pins 6, 10, 11, GND and 5V"));
  Serial.print(F("   last message from the modem: "));
  Serial.println(lastUnsolicited);
  return false;
}

// ---------------------------------------------------------------------------
// Setup and status

void describeAndConfigure() {
  char value[40];
  Serial.println(F("--- modem and SIM ---"));
  sendCommand("ATE0", 1000);      // no command echo
  sendCommand("AT+CMEE=2", 1000); // errors as text instead of numbers
  sendCommand("ATI", 1000);
  sendCommand("AT+CGMR", 1000);   // firmware version
  sendCommand("AT+CPIN?", 2000);  // READY = SIM readable
  if (sendCommand("AT+CCID", 2000, NULL, value, sizeof(value), false) == RESULT_OK) {
    size_t n = strlen(value);
    Serial.print(F("  SIM ICCID: "));
    if (n > 8) {  // masked, so the full number never ends up in a screenshot
      Serial.write(value, 4);
      Serial.print(F("..."));
      Serial.println(value + n - 4);
    } else {
      Serial.println(value);
    }
  }
  sendCommand("AT+CBC", 1000);    // battery: charging state, percent, millivolts

  Serial.println(F("--- network settings ---"));
  sendCommand("AT+CNMP=38", 2000);                     // LTE only
  sendCommand("AT+CMNB=1", 2000);                      // Cat-M only
  sendCommand("AT+CBANDCFG=\"CAT-M\",2,4,12,13", 3000); // US Cat-M bands
  sendCommand("AT+CGDCONT=1,\"IP\",\"hologram\"", 2000); // Hologram APN
  Serial.println(F("--- monitoring: one STATUS line every 15 s; type AT commands to send them ---"));
}

int numberAfter(const char* text, uint8_t fieldIndex) {
  // "+CGREG: 0,5" -> fieldIndex 0 = 0, fieldIndex 1 = 5
  const char* p = strchr(text, ':');
  if (!p) return -1;
  p++;
  for (uint8_t i = 0; i < fieldIndex; i++) {
    p = strchr(p, ',');
    if (!p) return -1;
    p++;
  }
  return atoi(p);
}

void printStatus() {
  char csq[24], reg[24], psi[90], cbc[32];
  Result r = sendCommand("AT+CSQ", 1500, "+CSQ:", csq, sizeof(csq), false);
  if (r == RESULT_TIMEOUT) {
    missedChecks++;
    stamp();
    Serial.print(F("!! modem not answering ("));
    Serial.print(missedChecks);
    Serial.print(F(" in a row). Last message from it: "));
    Serial.println(lastUnsolicited);
    if (missedChecks >= MISSES_BEFORE_RECOVERY) {
      recoveries++;
      missedChecks = 0;
      stamp();
      Serial.print(F("attempting recovery #"));
      Serial.println(recoveries);
      if (ensureModemOn()) describeAndConfigure();
    }
    return;
  }
  missedChecks = 0;
  sendCommand("AT+CGREG?", 1500, "+CGREG:", reg, sizeof(reg), false);
  sendCommand("AT+CPSI?", 2000, "+CPSI:", psi, sizeof(psi), false);
  sendCommand("AT+CBC", 1500, "+CBC:", cbc, sizeof(cbc), false);

  stamp();
  Serial.print(F("STATUS signal="));
  int rssi = numberAfter(csq, 0);
  if (rssi >= 0 && rssi <= 31) {
    Serial.print(rssi);
    Serial.print(F(" ("));
    Serial.print(-113 + 2 * rssi);
    Serial.print(F(" dBm)"));
  } else {
    Serial.print(F("none"));
  }

  Serial.print(F(" reg="));
  switch (numberAfter(reg, 1)) {
    case 1: Serial.print(F("home")); break;
    case 5: Serial.print(F("roaming")); break;
    case 2: Serial.print(F("searching")); break;
    case 3: Serial.print(F("DENIED")); break;
    case 0: Serial.print(F("not-searching")); break;
    default: Serial.print(F("unknown")); break;
  }

  Serial.print(F(" batt="));
  int mv = numberAfter(cbc, 2);
  if (mv > 0) {
    Serial.print(mv);
    Serial.print(F("mV"));
    if (mv < 3500) Serial.print(F(" LOW"));
  } else {
    Serial.print('?');
  }

  Serial.print(F(" net="));
  Serial.println(psi[0] ? psi + 7 : "?");  // skip "+CPSI: "
  if (recoveries) {
    Serial.print(F("   (recoveries so far: "));
    Serial.print(recoveries);
    Serial.println(')');
  }
}

void forwardUserCommand() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (userLen == 0) continue;
      userLine[userLen] = '\0';
      userLen = 0;
      sendCommand(userLine, 10000);
      return;
    }
    if (userLen < sizeof(userLine) - 1) userLine[userLen++] = c;
  }
}

void setup() {
  pinMode(PWRKEY_PIN, OUTPUT);
  digitalWrite(PWRKEY_PIN, HIGH);  // idle; pulled LOW only to press the key
  Serial.begin(115200);
  Serial.println();
  Serial.println(F("=== modem_diagnostics ==="));
  if (ensureModemOn()) describeAndConfigure();
  lastStatus = millis();
}

void loop() {
  drainUnsolicited();
  forwardUserCommand();
  if (millis() - lastStatus >= STATUS_INTERVAL_MS) {
    lastStatus = millis();
    printStatus();
  }
}
