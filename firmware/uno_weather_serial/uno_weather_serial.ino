/*
  uno_weather_serial.ino — Arduino Uno + DHT11 (ELEGOO 37-in-1 kit) weather node.

  Bench-test sketch: prints one JSON reading per line over USB serial, which
  serial_mqtt_bridge.py on the laptop publishes to MQTT. The cellular sketch
  (firmware/cellular_weather/cellular_weather.ino) is the main path; this one
  tests the sensor and the serial -> MQTT -> Postgres pipeline without the modem.

  Wiring: the DHT11 module plugs straight into the top header (the Uno's, or
  the SIM7000G shield's, which passes through), no jumper wires:
    -  (GND)    -> GND (next to AREF)
    +  (VCC)    -> 13  (driven HIGH by the sketch to power the sensor)
    S  (signal) -> 12
  Pin 13 can power the sensor because the DHT11 draws only a few milliamps.
  The board's "L" LED stays lit as a side effect.

  Library (Arduino Library Manager):
    - "DHT sensor library" by Adafruit (accept the prompt to also install
      "Adafruit Unified Sensor")

  Output: one JSON object per line at 9600 baud, e.g.
    {"t_c":22.0,"rh":41.0,"uptime_s":60,"reads":6,"read_errors":0}
*/

#include <DHT.h>

const uint8_t DHT_PIN = 12;
const uint8_t DHT_POWER_PIN = 13;  // powers the sensor
const uint8_t DHT_TYPE = DHT11;
const unsigned long READ_INTERVAL_MS = 10UL * 1000UL;  // DHT11 needs >= 1 s between reads

DHT dht(DHT_PIN, DHT_TYPE);

unsigned long lastRead = 0;
unsigned long reads = 0;
unsigned long readErrors = 0;

void setup() {
  Serial.begin(9600);
  pinMode(DHT_POWER_PIN, OUTPUT);
  digitalWrite(DHT_POWER_PIN, HIGH);  // turn the sensor on
  delay(1000);                        // let it settle
  dht.begin();
  Serial.println(F("{\"event\":\"boot\"}"));
}

void loop() {
  unsigned long now = millis();
  if (lastRead != 0 && now - lastRead < READ_INTERVAL_MS) return;
  lastRead = now;

  float h = dht.readHumidity();
  float t = dht.readTemperature();  // Celsius
  reads++;

  // The Uno's printf doesn't support %f, so build the line piece by piece.
  if (isnan(h) || isnan(t)) {
    readErrors++;
    Serial.print(F("{\"error\":\"dht_read_failed\",\"uptime_s\":"));
    Serial.print(now / 1000UL);
    Serial.print(F(",\"reads\":"));
    Serial.print(reads);
    Serial.print(F(",\"read_errors\":"));
    Serial.print(readErrors);
    Serial.println(F("}"));
    return;
  }

  Serial.print(F("{\"t_c\":"));
  Serial.print(t, 1);
  Serial.print(F(",\"rh\":"));
  Serial.print(h, 1);
  Serial.print(F(",\"uptime_s\":"));
  Serial.print(now / 1000UL);
  Serial.print(F(",\"reads\":"));
  Serial.print(reads);
  Serial.print(F(",\"read_errors\":"));
  Serial.print(readErrors);
  Serial.println(F("}"));
}
