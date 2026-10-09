/* Passive bench acquisition: raw PPG, measured pneumatic pressure and markers.
   No pump/valve control, arterial-line input or MAP prediction is implemented.
   Default hardware: MAX30101 + Adafruit MPRLS #3965 + XIAO ESP32-S3.
   Alternative profile: Honeywell MPRLS0300YG00001BB, 0-300 mmHg gauge,
   I2C 0x18, 2.5%-22.5% transfer function. Verify exact ordered variant.
*/
#include <Arduino.h>
#include <Wire.h>
#include <MAX30105.h>
#include <Adafruit_MPRLS.h>
#include <esp_timer.h>
#include <math.h>

#ifndef PRESSURE_PROFILE
#define PRESSURE_PROFILE 0
#endif
MAX30105 ppg;
#if PRESSURE_PROFILE == 0
Adafruit_MPRLS pressure;
const char *profile = "ADAFRUIT_3965_ABSOLUTE";
#else
// Normalize raw transfer to 0..1, then scale to 300 mmHg via constructor K.
Adafruit_MPRLS pressure(-1, -1, 0, 1, 2.5f, 22.5f, 300.0f);
const char *profile = "HONEYWELL_GAUGE300_TRANSFER_B";
#endif
const uint32_t PPG_RATE = 100;
const uint32_t PRESSURE_PERIOD_US = 40000; // Nominal 25 Hz, conversion is blocking.
uint8_t ledR = 32, ledIR = 32, ledG = 16;
bool ppgReady = false, pressureReady = false, zeroValid = false, recording = false;
double zeroNative = NAN, gain = 1.0, lastGauge = NAN;
uint64_t lastPressureAt = 0, nextPressureAt = 0;
uint32_t recordSeq = 0, ppgSamples = 0, pressureSamples = 0;
char commandBuffer[96];
size_t commandLength = 0;
bool commandTooLong = false;

uint64_t clockUs() { return (uint64_t)esp_timer_get_time(); }
double nativeToMmHg(double value) {
#if PRESSURE_PROFILE == 0
  return value * 0.750061683; // hPa -> mmHg
#else
  return value; // Constructor already returns mmHg for gauge300 profile.
#endif
}
void event(const char *label) {
  uint64_t now = clockUs();
  Serial.printf("E,%lu,%llu,%llu,nan,nan,nan,0,%s\n",
      (unsigned long)recordSeq++, (unsigned long long)now,
      (unsigned long long)now, label);
}
bool strictNumber(const String &text, double &value) {
  if (!text.length() || text.length() > 30) return false;
  char *end = nullptr;
  value = strtod(text.c_str(), &end);
  return end != text.c_str() && *end == '\0' && isfinite(value);
}
void status() {
  Serial.printf("# PROFILE %s; PPG %s; PRESSURE %s; ZERO %s; GAIN %.8f\n",
      profile, ppgReady ? "READY" : "MISSING", pressureReady ? "READY" : "MISSING",
      zeroValid ? "SET_THIS_BOOT" : "REQUIRED", gain);
  Serial.printf("# LED codes R/IR/G %u/%u/%u; emitted PPG %lu PRESSURE %lu\n",
      ledR, ledIR, ledG, (unsigned long)ppgSamples, (unsigned long)pressureSamples);
  Serial.println("# Pressure is pneumatic gauge pressure, NOT MAP. Instrument calibration is still required.");
}
void help() {
  Serial.println("# STATUS | ZERO (vent to atmosphere first) | GAIN number (calibration slope)");
  Serial.println("# LED red ir green (0..255, idle only; defaults 32 32 16)");
  Serial.println("# START (record only) | STOP (recording only; does not release pressure)");
  Serial.println("# MARK label (letters/digits/underscore/hyphen) | HELP");
  Serial.println("# Stream: kind,seq,t_start_us,t_end_us,a,b,c,flags,label");
  Serial.println("# P: raw RED/IR/GREEN; C: gauge_mmHg, native_sensor_value, zero_native; E: event.");
  Serial.println("# P timestamps bracket FIFO servicing, not exact optical sample times. Do not interpret them as simultaneous with C.");
}
bool readPressure(double &native, uint64_t &start, uint64_t &end) {
  start = clockUs();
  native = pressure.readPressure();
  end = clockUs();
  return isfinite(native);
}
void zeroPressure() {
  if (recording || !pressureReady) { Serial.println("# ZERO refused: stop recording and check pressure sensor."); return; }
  Serial.println("# ZERO assumes the pneumatic port is physically vented. Software cannot verify this.");
  double values[20];
  for (int i = 0; i < 20; ++i) {
    uint64_t start, end;
    if (!readPressure(values[i], start, end)) {
      zeroValid = false; Serial.println("# ZERO failed: pressure conversion fault."); return;
    }
    delay(20);
  }
  for (int i = 0; i < 19; ++i) for (int j = i + 1; j < 20; ++j)
    if (values[j] < values[i]) { double swap = values[i]; values[i] = values[j]; values[j] = swap; }
  if (nativeToMmHg(values[19] - values[0]) > 2.0) {
    zeroValid = false; Serial.println("# ZERO failed: readings unstable (>2 mmHg span, bench heuristic)."); return;
  }
  zeroNative = 0.5 * (values[9] + values[10]); zeroValid = true;
  Serial.printf("# ZERO set %.8f native units; GAIN %.8f; not saved across reboot.\n", zeroNative, gain);
}
void handleCommand(String input) {
  input.trim();
  int space = input.indexOf(' ');
  String cmd = space < 0 ? input : input.substring(0, space);
  String arg = space < 0 ? "" : input.substring(space + 1);
  cmd.toUpperCase(); arg.trim();
  if (cmd == "STOP") {
    if (recording) event("STOP");
    recording = false;
    Serial.println("# Recording stopped. Pressure remains under manual control; STOP does not vent the cuff.");
  } else if (cmd == "START") {
    if (recording) { Serial.println("# Already recording."); return; }
    if (!ppgReady || !pressureReady || !zeroValid) { Serial.println("# START refused: sensors must be ready and ZERO must be set."); return; }
    status(); ppg.clearFIFO(); nextPressureAt = clockUs(); recording = true;
    event("START");
  } else if (cmd == "ZERO") zeroPressure();
  else if (cmd == "GAIN") {
    double value;
    if (recording || !strictNumber(arg, value) || value < 0.5 || value > 1.5) {
      Serial.println("# GAIN refused: idle only, finite value 0.5..1.5. Check sensor profile if a larger correction is needed."); return;
    }
    gain = value; Serial.printf("# GAIN set %.8f (this boot only).\n", gain);
  } else if (cmd == "LED") {
    int r, ir, g; char extra;
    if (recording || sscanf(arg.c_str(), "%d %d %d %c", &r, &ir, &g, &extra) != 3 ||
        r < 0 || r > 255 || ir < 0 || ir > 255 || g < 0 || g > 255) {
      Serial.println("# LED refused: idle only, three integers 0..255."); return;
    }
    ledR = r; ledIR = ir; ledG = g;
    ppg.setPulseAmplitudeRed(ledR); ppg.setPulseAmplitudeIR(ledIR); ppg.setPulseAmplitudeGreen(ledG);
    status();
  } else if (cmd == "MARK") {
    if (!recording || !arg.length() || arg.length() > 40) { Serial.println("# MARK refused: record first, label 1..40 characters."); return; }
    for (unsigned int i = 0; i < arg.length(); ++i)
      if (!isalnum((unsigned char)arg[i]) && arg[i] != '_' && arg[i] != '-') {
        Serial.println("# Invalid marker label."); return;
      }
    event(arg.c_str());
  } else if (cmd == "STATUS") status();
  else if (cmd == "HELP") help();
  else if (cmd.length()) Serial.println("# Unknown command. HELP lists commands.");
}
void pollCommands() {
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n') {
      commandBuffer[commandLength] = 0;
      if (!commandTooLong) handleCommand(String(commandBuffer));
      else Serial.println("# Command too long; discarded.");
      commandLength = 0; commandTooLong = false;
    } else if (ch != '\r') {
      if (commandLength < sizeof(commandBuffer) - 1) commandBuffer[commandLength++] = ch;
      else commandTooLong = true;
    }
  }
}
void pollPPG() {
  uint64_t start = clockUs();
  // The library has a small software FIFO; check often and flag near-full batches.
  uint16_t fetched = ppg.check();
  uint64_t end = clockUs();
  int batch = ppg.available();
  uint8_t hardwareOverflow = ppg.readRegister8(0x57, 0x05);
  uint32_t flags = (batch >= 3 || fetched >= 3 ? 2 : 0) | (hardwareOverflow ? 4 : 0);
  if (hardwareOverflow) { // latch the fault in emitted rows, then allow recovery
    Wire.beginTransmission(0x57); Wire.write((uint8_t)0x05); Wire.write((uint8_t)0);
    Wire.endTransmission();
  }
  while (ppg.available()) {
    uint32_t r = ppg.getFIFORed(), ir = ppg.getFIFOIR(), g = ppg.getFIFOGreen();
    uint32_t rowFlags = flags | ((r >= 245000 || ir >= 245000 || g >= 245000) ? 1 : 0);
    Serial.printf("P,%lu,%llu,%llu,%lu,%lu,%lu,%lu,RAW\n",
        (unsigned long)recordSeq++, (unsigned long long)start, (unsigned long long)end,
        (unsigned long)r, (unsigned long)ir, (unsigned long)g, (unsigned long)rowFlags);
    ppg.nextSample(); ++ppgSamples;
  }
}
void pollPressure() {
  if (clockUs() < nextPressureAt) return;
  double native; uint64_t start, end;
  bool valid = readPressure(native, start, end);
  nextPressureAt = start + PRESSURE_PERIOD_US;
  lastPressureAt = end;
  lastGauge = valid ? gain * nativeToMmHg(native - zeroNative) : NAN;
  uint32_t flags = valid ? 0 : 8;
  if (valid && (lastGauge < -10 || lastGauge > 300)) flags |= 16;
  Serial.printf("C,%lu,%llu,%llu,%.6f,%.6f,%.6f,%lu,PNEUMATIC_NOT_MAP\n",
      (unsigned long)recordSeq++, (unsigned long long)start, (unsigned long long)end,
      lastGauge, native, zeroNative, (unsigned long)flags);
  ++pressureSamples;
}
void setup() {
  Serial.begin(115200); delay(1200);
  Wire.begin(5, 6); Wire.setClock(400000); Wire.setTimeOut(20);
  ppgReady = ppg.begin(Wire, I2C_SPEED_FAST, 0x57);
  if (ppgReady) {
    ppg.setup(32, 1, 3, PPG_RATE, 411, 16384);
    ppg.setPulseAmplitudeRed(ledR); ppg.setPulseAmplitudeIR(ledIR); ppg.setPulseAmplitudeGreen(ledG);
  }
  pressureReady = pressure.begin(0x18, &Wire);
  Serial.println("# PPG + PRESSURE PASSIVE BENCH LOGGER v1; no MAP estimator or pressure actuator");
  status(); help();
}
void loop() {
  pollCommands();
  if (!recording) { delay(1); return; }
  pollPPG();
  pollPressure();
}
