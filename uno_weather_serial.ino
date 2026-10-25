/*
  uno_weather_serial.ino — Arduino Uno + DHT11 (ELEGOO 37-in-1 kit) weather node.

  The Uno has no network of its own, so for now it prints one JSON reading per
  line over USB serial. serial_mqtt_bridge.py on your laptop reads those lines
  and publishes them to MQTT. When the LTE-M shield arrives, the shield takes
  the laptop's place and the Uno publishes over cellular directly.

  Wiring (DHT11 module from the ELEGOO kit, 3 pins):
    S  (signal) -> D2
    +  (VCC)    -> 5V
    -  (GND)    -> GND
  Pin labels vary slightly by module revision; the middle pin is usually VCC.

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

// void setup() {
//   Serial.begin(9600);
//   dht.begin();
//   Serial.println(F("{\"event\":\"boot\"}"));
// }

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
