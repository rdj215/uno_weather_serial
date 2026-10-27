/*
  cellular_weather.ino — Arduino Uno + DHT11 + Botletics SIM7000G shield.
  Sends weather readings and connection health to the Hologram cloud over
  LTE Cat-M1, with no laptop involved.

  Every SEND_INTERVAL_MS it:
    1. reads the DHT11
    2. reads the modem's signal, registration, carrier, band and battery
    3. opens a TCP connection to cloudsocket.hologram.io:9999 and sends one
       message: {"k":"<device key>","d":"<reading as JSON>","t":["weather"]}
    4. checks Hologram's reply ([0,0] = accepted) and closes the connection

  hologram_poller.py then pulls the messages from Hologram's REST API into
  Postgres.

  Every message carries:
    seq        message counter since boot; a gap means messages were lost
    send_fail  failed sends since boot, so a gap can be explained afterwards
    rssi_dbm, reg, op, band, batt_mv   the device's view of its connection

  Setup:
    - Copy secrets.example.h to secrets.h and put your Hologram device key in it.
    - Libraries: "DHT sensor library" by Adafruit (+ Adafruit Unified Sensor).
    - Serial Monitor: 115200 baud, "Both NL & CR". Type "send" to send a
      reading now; anything else is sent to the modem as an AT command.

  Wiring:
    DHT11 plugged straight into the shield's top header: - -> GND (next to AREF),
    + -> 13 (powered by the sketch), S -> 12. Pins 12 and 13 are free on the shield.
    Shield: 6 = power key, 10 = modem TX -> Uno RX, 11 = Uno TX -> modem RX
    The LiPo must be connected: the modem runs from it, not from USB.

  Data use: each message is roughly 1 KB including TCP overhead (check the
  dashboard's usage after a few sends). At 15 minutes that's about 3 MB/month.

  Memory: the Uno has 2 KB of RAM, so fixed strings are kept in flash with F()
  and the *_P string functions.
*/

#include <SoftwareSerial.h>
#include <DHT.h>

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Copy secrets.example.h to secrets.h and put your Hologram device key in it"
#endif

// ---------------------------------------------------------------- settings
const unsigned long SEND_INTERVAL_MS = 15UL * 60UL * 1000UL;  // 15 minutes
const unsigned long FIRST_SEND_DELAY_MS = 60UL * 1000UL;       // after start-up
const unsigned long STATUS_INTERVAL_MS = 60UL * 1000UL;        // local log only, no data used
#define HOLOGRAM_TOPIC "weather"

const uint8_t DHT_PIN = 12;
const uint8_t DHT_POWER_PIN = 13;  // powers the sensor
const uint8_t MODEM_RX_PIN = 10;
const uint8_t MODEM_TX_PIN = 11;
const uint8_t PWRKEY_PIN = 6;
const long MODEM_BAUD = 9600;
const uint8_t MISSES_BEFORE_RECOVERY = 3;

typedef const __FlashStringHelper* FStr;

SoftwareSerial modem(MODEM_RX_PIN, MODEM_TX_PIN);
DHT dht(DHT_PIN, DHT11);

enum Result { RESULT_OK, RESULT_ERROR, RESULT_TIMEOUT };

char modemLine[96];
uint8_t modemLen = 0;
char userLine[64];
uint8_t userLen = 0;
char lastUnsolicited[48] = "(none)";
char reading[180];   // the reading, as JSON
char message[280];   // the reading wrapped in Hologram's envelope

unsigned long lastSend = 0, lastStatus = 0;
bool firstSendDone = false;
unsigned long seq = 0, sendFailures = 0, reads = 0, readErrors = 0;
uint8_t missedChecks = 0;

// Snapshot of the connection, refreshed before each send and status line.
struct NetStatus {
  int rssiDbm;   // 0 = unknown
  int reg;       // 1 home, 5 roaming, 2 searching, ...
  int battMv;
  char op[12];   // MCC-MNC, e.g. 310-260 = T-Mobile US
  char band[8];  // e.g. B12
  char mode[10]; // e.g. CAT-M
} net;

// ---------------------------------------------------------------- strings

bool startsWithP(const char* text, FStr prefix) {
  return strncmp_P(text, (PGM_P)prefix, strlen_P((PGM_P)prefix)) == 0;
}
bool containsP(const char* text, FStr word) { return strstr_P(text, (PGM_P)word) != NULL; }

void stamp() {
  Serial.print('[');
  Serial.print(millis() / 1000UL);
  Serial.print(F("s] "));
}

void reportUnsolicited(const char* text) {
  strncpy(lastUnsolicited, text, sizeof(lastUnsolicited) - 1);
  lastUnsolicited[sizeof(lastUnsolicited) - 1] = '\0';
  stamp();
  bool serious = containsP(text, F("POWER DOWN")) || containsP(text, F("VOLTAGE"));
  Serial.print(serious ? F("!! modem: ") : F("modem: "));
  Serial.println(text);
  if (containsP(text, F("UNDER-VOLTAGE"))) {
    Serial.println(F("   -> battery too low for the modem: charge the LiPo / check its connector"));
  }
}

// ---------------------------------------------------------------- modem I/O

bool readModemLine() {
  while (modem.available()) {
    char c = modem.read();
    if (c == '\r') continue;
    if (c == '\n') {
      modemLine[modemLen] = '\0';
      bool blank = strspn(modemLine, " >") == modemLen;  // e.g. leftover "> " prompt
      modemLen = 0;
      if (blank) continue;
      return true;
    }
    if (modemLen < sizeof(modemLine) - 1) modemLine[modemLen++] = c;
  }
  return false;
}

void drainUnsolicited() {
  while (readModemLine()) reportUnsolicited(modemLine);
}

// cmd is either a RAM string or (flash == true) a flash string.
void sendRaw(const char* cmd, bool flash, bool verbose) {
  drainUnsolicited();
  if (verbose) {
    Serial.print(F("  -> "));
    if (flash) Serial.println((FStr)cmd);
    else Serial.println(cmd);
  }
  if (flash) modem.print((FStr)cmd);
  else modem.print(cmd);
  modem.print('\r');
}
void sendRaw(FStr cmd, bool verbose = true) { sendRaw((const char*)cmd, true, verbose); }

// Waits for a line starting with one of up to three prefixes.
// Returns 1, 2 or 3 for the prefix matched, 0 on timeout. Lines are printed
// when verbose; otherwise unmatched lines are reported as unsolicited.
uint8_t expectLine(unsigned long timeoutMs, FStr a, FStr b = NULL, FStr c = NULL,
                   bool verbose = true, char* out = NULL, size_t outSize = 0) {
  unsigned long start = millis();
  while (millis() - start < timeoutMs) {
    if (!readModemLine()) continue;
    uint8_t hit = 0;
    if (startsWithP(modemLine, a)) hit = 1;
    else if (b && startsWithP(modemLine, b)) hit = 2;
    else if (c && startsWithP(modemLine, c)) hit = 3;
    if (verbose) {
      Serial.print(F("  <- "));
      Serial.println(modemLine);
    } else if (!hit) {
      reportUnsolicited(modemLine);
    }
    if (hit) {
      if (out && outSize) {
        strncpy(out, modemLine, outSize - 1);
        out[outSize - 1] = '\0';
      }
      return hit;
    }
  }
  if (verbose) Serial.println(F("  <- (no reply)"));
  return 0;
}

// Sends a command that ends in OK / ERROR. If capturePrefix is given, the
// matching reply line is copied into out.
Result sendCommand(const char* cmd, bool flash, unsigned long timeoutMs, FStr capturePrefix,
                   char* out, size_t outSize, bool verbose) {
  sendRaw(cmd, flash, verbose);
  if (out && outSize) out[0] = '\0';
  unsigned long start = millis();
  while (millis() - start < timeoutMs) {
    if (!readModemLine()) continue;
    bool echo = flash ? strcmp_P(modemLine, cmd) == 0 : strcmp(modemLine, cmd) == 0;
    if (echo) continue;
    if (strcmp_P(modemLine, PSTR("OK")) == 0) {
      if (verbose) Serial.println(F("  <- OK"));
      return RESULT_OK;
    }
    if (strcmp_P(modemLine, PSTR("ERROR")) == 0 || startsWithP(modemLine, F("+CME ERROR"))) {
      Serial.print(F("  <- "));
      Serial.println(modemLine);
      return RESULT_ERROR;
    }
    bool captured = false;
    if (out && outSize && (capturePrefix ? startsWithP(modemLine, capturePrefix) : out[0] == '\0')) {
      strncpy(out, modemLine, outSize - 1);
      out[outSize - 1] = '\0';
      captured = true;
    }
    if (verbose) {
      Serial.print(F("  <- "));
      Serial.println(modemLine);
    } else if (!captured) {
      reportUnsolicited(modemLine);
    }
  }
  if (verbose) Serial.println(F("  <- (no reply)"));
  return RESULT_TIMEOUT;
}
Result sendCommand(FStr cmd, unsigned long timeoutMs, FStr capturePrefix = NULL,
                   char* out = NULL, size_t outSize = 0, bool verbose = true) {
  return sendCommand((const char*)cmd, true, timeoutMs, capturePrefix, out, outSize, verbose);
}

// ---------------------------------------------------------------- power-up

bool answers() {
  for (uint8_t i = 0; i < 3; i++) {
    if (sendCommand(F("AT"), 600, NULL, NULL, 0, false) == RESULT_OK) return true;
  }
  return false;
}

bool answersAtAnySpeed() {
  modem.begin(MODEM_BAUD);
  delay(50);
  if (answers()) return true;
  modem.begin(115200);  // the modem's factory speed
  delay(50);
  for (uint8_t i = 0; i < 3; i++) {
    modem.print(F("AT\r"));
    delay(200);
  }
  modem.print(F("AT+IPR=9600\r"));
  delay(300);
  modem.begin(MODEM_BAUD);
  delay(50);
  while (modem.available()) modem.read();
  modemLen = 0;
  if (answers()) {
    sendCommand(F("AT&W"), 1000);
    return true;
  }
  return false;
}

// Only presses the power key if the modem is silent: the key toggles power,
// so pressing it while the modem is on would switch it off.
bool ensureModemOn() {
  stamp();
  Serial.println(F("looking for the modem..."));
  if (answersAtAnySpeed()) {
    stamp();
    Serial.println(F("modem is on and answering"));
    return true;
  }
  stamp();
  Serial.println(F("modem silent: pressing the power key"));
  digitalWrite(PWRKEY_PIN, LOW);
  delay(500);
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
  Serial.println(F("!! modem not answering: check the LiPo, the NET LED, and pins 6/10/11"));
  return false;
}

void configureModem() {
  sendCommand(F("ATE0"), 1000);
  sendCommand(F("AT+CMEE=2"), 1000);
  sendCommand(F("AT+CPIN?"), 2000);
  sendCommand(F("AT+CNMP=38"), 2000);                       // LTE only
  sendCommand(F("AT+CMNB=1"), 2000);                        // Cat-M only
  sendCommand(F("AT+CBANDCFG=\"CAT-M\",2,4,12,13"), 3000);  // US Cat-M bands
  sendCommand(F("AT+CGDCONT=1,\"IP\",\"hologram\""), 2000);
  // Close data sessions other sketches (e.g. LTE_Demo) may have left open;
  // an error here just means there was nothing to close.
  sendCommand(F("AT+CNACT=0"), 3000);
  sendCommand(F("AT+SAPBR=0,1"), 3000);
}

// ---------------------------------------------------------------- status

int numberAfter(const char* text, uint8_t fieldIndex) {
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

// Copies comma-separated field n of "+CPSI: a,b,c,..." into out.
void cpsiField(const char* text, uint8_t n, char* out, size_t outSize) {
  out[0] = '\0';
  const char* p = strchr(text, ':');
  if (!p) return;
  p++;
  while (*p == ' ') p++;
  for (uint8_t i = 0; i < n; i++) {
    p = strchr(p, ',');
    if (!p) return;
    p++;
  }
  size_t len = strcspn(p, ",");
  if (len >= outSize) len = outSize - 1;
  memcpy(out, p, len);
  out[len] = '\0';
}

// Returns false if the modem didn't answer.
bool refreshNetStatus() {
  char line[96];
  memset(&net, 0, sizeof(net));
  if (sendCommand(F("AT+CSQ"), 1500, F("+CSQ:"), line, sizeof(line), false) == RESULT_TIMEOUT) {
    return false;
  }
  int csq = numberAfter(line, 0);
  net.rssiDbm = (csq >= 0 && csq <= 31) ? -113 + 2 * csq : 0;
  if (sendCommand(F("AT+CGREG?"), 1500, F("+CGREG:"), line, sizeof(line), false) == RESULT_OK) {
    net.reg = numberAfter(line, 1);
  }
  if (sendCommand(F("AT+CBC"), 1500, F("+CBC:"), line, sizeof(line), false) == RESULT_OK) {
    net.battMv = numberAfter(line, 2);
  }
  if (sendCommand(F("AT+CPSI?"), 2000, F("+CPSI:"), line, sizeof(line), false) == RESULT_OK) {
    cpsiField(line, 0, net.mode, sizeof(net.mode));
    cpsiField(line, 2, net.op, sizeof(net.op));
    char band[16];
    cpsiField(line, 6, band, sizeof(band));  // "EUTRAN-BAND12"
    const char* digits = strstr_P(band, PSTR("BAND"));
    if (digits) {
      net.band[0] = 'B';
      strncpy(net.band + 1, digits + 4, sizeof(net.band) - 2);
    }
  }
  return true;
}

bool registered() { return net.reg == 1 || net.reg == 5; }

void printStatus() {
  if (!refreshNetStatus()) {
    missedChecks++;
    stamp();
    Serial.print(F("!! modem not answering ("));
    Serial.print(missedChecks);
    Serial.print(F(" in a row); last message from it: "));
    Serial.println(lastUnsolicited);
    if (missedChecks >= MISSES_BEFORE_RECOVERY) {
      missedChecks = 0;
      if (ensureModemOn()) configureModem();
    }
    return;
  }
  missedChecks = 0;
  stamp();
  Serial.print(F("STATUS rssi="));
  Serial.print(net.rssiDbm);
  Serial.print(F("dBm reg="));
  Serial.print(net.reg);
  Serial.print(registered() ? F(" (registered)") : F(" (NOT registered)"));
  Serial.print(F(" net="));
  Serial.print(net.mode);
  Serial.print(' ');
  Serial.print(net.op);
  Serial.print(' ');
  Serial.print(net.band);
  Serial.print(F(" batt="));
  Serial.print(net.battMv);
  Serial.print(F("mV sent="));
  Serial.print(seq - sendFailures);
  Serial.print(F(" failed="));
  Serial.println(sendFailures);
}

// ---------------------------------------------------------------- message

// Builds a string into a fixed buffer without overflowing it.
struct Builder {
  char* buf;
  size_t size;
  size_t len;
  void addChar(char c) {
    if (len < size - 1) buf[len++] = c;
    buf[len] = '\0';
  }
  void add(const char* s) { while (*s) addChar(*s++); }
  void add(FStr s) {
    PGM_P p = (PGM_P)s;
    char c;
    while ((c = pgm_read_byte(p++))) addChar(c);
  }
  void add(long v) {
    char tmp[12];
    ltoa(v, tmp, 10);
    add(tmp);
  }
  void add(float v, uint8_t decimals) {
    char tmp[12];
    dtostrf(v, 1, decimals, tmp);
    add(tmp);
  }
  void field(FStr name) {  // ,"name":
    if (len > 1) addChar(',');
    addChar('"');
    add(name);
    add(F("\":"));
  }
  void text(FStr name, const char* value) {
    field(name);
    addChar('"');
    add(value);
    addChar('"');
  }
};

// Builds this send's reading into `reading`.
void buildReading(bool isBoot) {
  Builder b = {reading, sizeof(reading), 0};
  reading[0] = '\0';
  b.addChar('{');
  if (isBoot) {
    b.field(F("event"));
    b.add(F("\"boot\""));
  } else {
    float h = dht.readHumidity();
    float t = dht.readTemperature();
    reads++;
    if (isnan(h) || isnan(t)) {
      readErrors++;
      b.field(F("error"));
      b.add(F("\"dht_read_failed\""));
    } else {
      b.field(F("t_c"));
      b.add(t, 1);
      b.field(F("rh"));
      b.add(h, 1);
    }
  }
  b.field(F("uptime_s"));    b.add((long)(millis() / 1000UL));
  b.field(F("reads"));       b.add((long)reads);
  b.field(F("read_errors")); b.add((long)readErrors);
  b.field(F("seq"));         b.add((long)seq);
  b.field(F("send_fail"));   b.add((long)sendFailures);
  b.field(F("rssi_dbm"));    b.add((long)net.rssiDbm);
  b.field(F("reg"));         b.add((long)net.reg);
  b.text(F("op"), net.op);
  b.text(F("band"), net.band);
  b.field(F("batt_mv"));     b.add((long)net.battMv);
  b.addChar('}');
}

// Wraps the reading in Hologram's envelope. The reading goes in "d" as a
// string, so its quotes are escaped.
void buildMessage() {
  Builder b = {message, sizeof(message), 0};
  message[0] = '\0';
  b.add(F("{\"k\":\"" HOLOGRAM_DEVICE_KEY "\",\"d\":\""));
  for (const char* p = reading; *p; p++) {
    if (*p == '"') b.addChar('\\');
    b.addChar(*p);
  }
  b.add(F("\",\"t\":[\"" HOLOGRAM_TOPIC "\"]}\r\r"));  // \r\r ends the message
}

// ---------------------------------------------------------------- sending

// Brings up the data connection (PDP context) if it isn't already.
bool ensureDataConnection() {
  char state[40];
  sendCommand(F("AT+CIPSTATUS"), 2000);
  if (expectLine(2000, F("STATE:"), NULL, NULL, true, state, sizeof(state)) == 0) return false;
  if (containsP(state, F("IP STATUS")) || containsP(state, F("IP PROCESSING")) ||
      containsP(state, F("TCP CLOSED")) || containsP(state, F("IP CLOSE"))) {
    return true;  // already up
  }
  if (containsP(state, F("CONNECT OK")) || containsP(state, F("CONNECTING"))) {
    sendRaw(F("AT+CIPCLOSE"));
    expectLine(5000, F("CLOSE OK"), F("ERROR"));
    return true;
  }
  sendRaw(F("AT+CIPSHUT"));  // reset the IP stack to a known state
  if (expectLine(10000, F("SHUT OK"), F("ERROR")) != 1) return false;
  if (sendCommand(F("AT+CIPMUX=0"), 2000) != RESULT_OK) return false;
  if (sendCommand(F("AT+CSTT=\"hologram\""), 2000) != RESULT_OK) return false;
  if (sendCommand(F("AT+CIICR"), 60000) != RESULT_OK) return false;  // can take a while
  sendRaw(F("AT+CIFSR"));  // replies with the IP address, no OK
  unsigned long start = millis();
  while (millis() - start < 5000) {
    if (!readModemLine()) continue;
    Serial.print(F("  <- "));
    Serial.println(modemLine);
    if (isdigit(modemLine[0])) return true;  // got an IP address
    if (containsP(modemLine, F("ERROR"))) return false;
  }
  return false;
}

// Returns true if Hologram accepted the message, or it was sent and Hologram
// didn't reply (which happens; it's logged separately).
bool sendToHologram() {
  stamp();
  Serial.println(F("sending to Hologram..."));
  if (!ensureDataConnection()) {
    Serial.println(F("  !! couldn't bring up the data connection"));
    return false;
  }
  sendRaw(F("AT+CIPSTART=\"TCP\",\"cloudsocket.hologram.io\",9999"));
  // The modem replies OK straight away, then CONNECT OK once connected.
  if (expectLine(30000, F("CONNECT OK"), F("CONNECT FAIL"), F("ERROR")) != 1) {
    Serial.println(F("  !! TCP connection to Hologram failed"));
    return false;
  }

  size_t len = strlen(message);
  char cmd[20];
  strcpy_P(cmd, PSTR("AT+CIPSEND="));
  ltoa((long)len, cmd + strlen(cmd), 10);
  sendRaw(cmd, false, true);
  // Wait for the ">" prompt (it has no line ending).
  unsigned long start = millis();
  bool prompt = false;
  while (millis() - start < 5000 && !prompt) {
    if (modem.available() && modem.read() == '>') prompt = true;
  }
  if (!prompt) {
    Serial.println(F("  !! no send prompt"));
    sendRaw(F("AT+CIPCLOSE"));
    expectLine(5000, F("CLOSE OK"), F("ERROR"));
    return false;
  }
  modem.print(message);
  Serial.print(F("  -> ("));
  Serial.print(len);
  Serial.println(F(" bytes)"));
  if (expectLine(10000, F("SEND OK"), F("SEND FAIL"), F("ERROR")) != 1) {
    Serial.println(F("  !! send failed"));
    return false;
  }

  // Hologram replies with e.g. [0,0]; the first number is the result code.
  char reply[16];
  uint8_t got = expectLine(10000, F("["), F("CLOSED"), NULL, true, reply, sizeof(reply));
  bool ok = true;
  stamp();
  if (got == 1) {
    int code = atoi(reply + 1);
    if (code == 0) {
      Serial.println(F("Hologram accepted the message"));
    } else {
      Serial.print(F("!! Hologram rejected the message, code "));
      Serial.print(code);
      Serial.println(code == 3 ? F(" (device key invalid)") : F(""));
      ok = false;
    }
  } else {
    Serial.println(F("sent, but no reply from Hologram (usually still delivered)"));
  }
  sendRaw(F("AT+CIPCLOSE"), false);  // may already be closed; either is fine
  // Hologram usually closes first, so "+CME ERROR: operation not allowed" is normal here.
  expectLine(3000, F("CLOSE OK"), F("ERROR"), F("+CME ERROR"), false);
  return ok;
}

void sendReading(bool isBoot) {
  if (!refreshNetStatus()) {
    stamp();
    Serial.println(F("!! modem not answering; skipping this send"));
    seq++;
    sendFailures++;
    return;
  }
  buildReading(isBoot);
  stamp();
  Serial.print(F("reading: "));
  Serial.println(reading);
  seq++;
  if (!registered()) {
    stamp();
    Serial.println(F("!! not registered on the network; skipping this send"));
    sendFailures++;
    return;
  }
  buildMessage();
  if (!sendToHologram()) sendFailures++;
}

// ---------------------------------------------------------------- main

void handleUserInput() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (userLen == 0) continue;
      userLine[userLen] = '\0';
      userLen = 0;
      if (strcmp_P(userLine, PSTR("send")) == 0) {
        sendReading(false);
        lastSend = millis();
      } else {
        sendCommand(userLine, false, 10000, NULL, NULL, 0, true);
      }
      return;
    }
    if (userLen < sizeof(userLine) - 1) userLine[userLen++] = c;
  }
}

void setup() {
  pinMode(PWRKEY_PIN, OUTPUT);
  digitalWrite(PWRKEY_PIN, HIGH);
  Serial.begin(115200);
  pinMode(DHT_POWER_PIN, OUTPUT);
  digitalWrite(DHT_POWER_PIN, HIGH);  // switch the sensor on
  delay(1000);                        // the DHT11 needs ~1 s before its first read
  dht.begin();
  Serial.println();
  Serial.println(F("=== cellular_weather ==="));
  if (ensureModemOn()) configureModem();
  lastSend = lastStatus = millis();
}

void loop() {
  drainUnsolicited();
  handleUserInput();
  unsigned long now = millis();
  if (now - lastStatus >= STATUS_INTERVAL_MS) {
    lastStatus = now;
    printStatus();
  }
  if (!firstSendDone && now - lastSend >= FIRST_SEND_DELAY_MS) {
    firstSendDone = true;
    sendReading(true);   // boot event: shows restarts in the database
    sendReading(false);  // and a first reading, so you don't wait 15 minutes
    lastSend = millis();
  } else if (firstSendDone && now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    sendReading(false);
  }
}
