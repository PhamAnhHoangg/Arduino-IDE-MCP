// Example sketch made to be checked by test_on_board.
// Ask Claude: "test_on_board examples/serial_selftest on my port,
// send ["ping"], expect ["READY", "pong"]".
void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
}

unsigned long last = 0;

void loop() {
  // Print READY every second so the test can't miss it after reset.
  if (millis() - last > 1000) {
    last = millis();
    Serial.println("READY");
    digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
  }
  // Reply to commands sent by the test.
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "ping") Serial.println("pong");
    else Serial.println("unknown: " + cmd);
  }
}
