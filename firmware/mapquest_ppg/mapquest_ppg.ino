// MAPQuest PPG — MAX30101 raw sample streamer
//
// Target : Arduino Nano ESP32 (ESP32-S3)
// Sensor : SparkFun SEN-16474 Photodetector Breakout - MAX30101 (Qwiic), I2C 0x57
//
// Wiring (3.3 V logic both sides, no level shifting):
//   breakout 3.3V -> 3V3
//   breakout GND  -> GND
//   breakout SDA  -> A4  (GPIO11, default Wire SDA)
//   breakout SCL  -> A5  (GPIO12, default Wire SCL)
//   breakout INT  -> not connected (we poll the FIFO pointers)
//
// This sketch does NO filtering and NO interpretation. It configures the sensor,
// drains the FIFO, and prints raw 18-bit counts as CSV. All signal processing
// happens in Python. See docs/design.md.
//
// No external libraries required — just open and upload.

#include <Wire.h>

// ---------------------------------------------------------------------------
// Configuration — the knobs you are most likely to turn.
// See docs/design.md §3 before changing the rate; 411 us pulse width caps
// two-channel operation at 400 sps (datasheet Table 11).
// ---------------------------------------------------------------------------

static const uint16_t SAMPLE_RATE_SPS   = 400;  // internal ADC rate: 50/100/200/400/800/1000/1600/3200
static const uint8_t  SAMPLE_AVERAGING  = 2;    // 1/2/4/8/16/32 -> output rate = SAMPLE_RATE_SPS / this
static const uint16_t PULSE_WIDTH_US    = 411;  // 69/118/215/411 -> 15/16/17/18 bit resolution
static const uint16_t ADC_RANGE_NA      = 4096; // 2048/4096/8192/16384

static uint8_t led_green_pa = 0x24;  // LED3_PA, x0.2 mA  -> 7.2 mA
static uint8_t led_ir_pa    = 0x24;  // LED2_PA, x0.2 mA  -> 7.2 mA

// Slot assignment. SLOT1 is the first 3 bytes of each FIFO sample, SLOT2 the next.
// 0 = off, 1 = RED (LED1), 2 = IR (LED2), 3 = GREEN (LED3).
static const uint8_t SLOT1_LED = 3;  // green
static const uint8_t SLOT2_LED = 2;  // ir
static const uint8_t SLOT3_LED = 0;  // off
static const uint8_t SLOT4_LED = 0;  // off

static const uint32_t SERIAL_BAUD = 921600;  // nominal; ignored by native USB CDC

// ---------------------------------------------------------------------------
// MAX30101 register map
// ---------------------------------------------------------------------------

static const uint8_t MAX30101_ADDR = 0x57;

enum : uint8_t {
  REG_INT_STATUS_1   = 0x00,
  REG_INT_STATUS_2   = 0x01,
  REG_INT_ENABLE_1   = 0x02,
  REG_INT_ENABLE_2   = 0x03,
  REG_FIFO_WR_PTR    = 0x04,
  REG_OVF_COUNTER    = 0x05,
  REG_FIFO_RD_PTR    = 0x06,
  REG_FIFO_DATA      = 0x07,
  REG_FIFO_CONFIG    = 0x08,
  REG_MODE_CONFIG    = 0x09,
  REG_SPO2_CONFIG    = 0x0A,
  REG_LED1_PA        = 0x0C,  // red
  REG_LED2_PA        = 0x0D,  // ir
  REG_LED3_PA        = 0x0E,  // green
  REG_LED4_PA        = 0x0F,  // green (second green driver)
  REG_MULTI_LED_1    = 0x11,  // slot2[6:4], slot1[2:0]
  REG_MULTI_LED_2    = 0x12,  // slot4[6:4], slot3[2:0]
  REG_TEMP_INT       = 0x1F,
  REG_TEMP_FRAC      = 0x20,
  REG_TEMP_CONFIG    = 0x21,
  REG_REV_ID         = 0xFE,
  REG_PART_ID        = 0xFF,
};

static const uint8_t MODE_MULTI_LED  = 0x07;
static const uint8_t MODE_RESET_BIT  = 0x40;
static const uint8_t EXPECTED_PART_ID = 0x15;

// ESP32 Wire buffer is 128 B; stay well under it when bursting the FIFO.
static const size_t I2C_CHUNK_BYTES = 120;

// ---------------------------------------------------------------------------
// Derived state
// ---------------------------------------------------------------------------

static uint8_t  g_active_slots = 0;
static float    g_output_fs    = 0.0f;
static uint8_t  g_rev_id       = 0;
static bool     g_streaming    = false;
static uint32_t g_sample_idx   = 0;

// ---------------------------------------------------------------------------
// I2C helpers
// ---------------------------------------------------------------------------

static bool regWrite(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MAX30101_ADDR);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

static bool regRead(uint8_t reg, uint8_t &value) {
  Wire.beginTransmission(MAX30101_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)MAX30101_ADDR, (uint8_t)1) != 1) return false;
  value = Wire.read();
  return true;
}

// ---------------------------------------------------------------------------
// Register field encoders. Each returns the bitfield value, falling back to a
// safe default if the configured constant is not a legal setting.
// ---------------------------------------------------------------------------

static uint8_t encodeSampleRate(uint16_t sps) {
  switch (sps) {
    case 50:   return 0;
    case 100:  return 1;
    case 200:  return 2;
    case 400:  return 3;
    case 800:  return 4;
    case 1000: return 5;
    case 1600: return 6;
    case 3200: return 7;
    default:   return 3;  // 400 sps
  }
}

static uint8_t encodePulseWidth(uint16_t us) {
  switch (us) {
    case 69:  return 0;
    case 118: return 1;
    case 215: return 2;
    case 411: return 3;
    default:  return 3;
  }
}

static uint8_t resolutionBits(uint16_t us) {
  switch (us) {
    case 69:  return 15;
    case 118: return 16;
    case 215: return 17;
    case 411: return 18;
    default:  return 18;
  }
}

static uint8_t encodeAdcRange(uint16_t na) {
  switch (na) {
    case 2048:  return 0;
    case 4096:  return 1;
    case 8192:  return 2;
    case 16384: return 3;
    default:    return 1;
  }
}

static uint8_t encodeAveraging(uint8_t n) {
  switch (n) {
    case 1:  return 0;
    case 2:  return 1;
    case 4:  return 2;
    case 8:  return 3;
    case 16: return 4;
    case 32: return 5;
    default: return 0;
  }
}

static const char *ledName(uint8_t slotLed) {
  switch (slotLed) {
    case 1:  return "red";
    case 2:  return "ir";
    case 3:  return "green";
    default: return "off";
  }
}

// ---------------------------------------------------------------------------
// Sensor configuration
// ---------------------------------------------------------------------------

static void clearFifoPointers() {
  regWrite(REG_FIFO_WR_PTR, 0x00);
  regWrite(REG_OVF_COUNTER, 0x00);
  regWrite(REG_FIFO_RD_PTR, 0x00);
}

static bool configureSensor() {
  // Soft reset, then wait for the RESET bit to self-clear.
  if (!regWrite(REG_MODE_CONFIG, MODE_RESET_BIT)) return false;
  for (int i = 0; i < 100; i++) {
    uint8_t m;
    if (regRead(REG_MODE_CONFIG, m) && (m & MODE_RESET_BIT) == 0) break;
    delay(5);
  }

  // FIFO: sample averaging, rollover enabled so a stalled host does not wedge
  // the FIFO (we detect the gap via OVF_COUNTER instead).
  const uint8_t fifoCfg = (encodeAveraging(SAMPLE_AVERAGING) << 5) | (1 << 4) | 0x0F;
  if (!regWrite(REG_FIFO_CONFIG, fifoCfg)) return false;

  // SpO2 config register also governs multi-LED timing: ADC range, rate, pulse width.
  const uint8_t spo2Cfg = (encodeAdcRange(ADC_RANGE_NA) << 5) |
                          (encodeSampleRate(SAMPLE_RATE_SPS) << 2) |
                          encodePulseWidth(PULSE_WIDTH_US);
  if (!regWrite(REG_SPO2_CONFIG, spo2Cfg)) return false;

  // LED drive currents. Slots reference LEDn_PA by LED number, so set the ones we use.
  if (!regWrite(REG_LED1_PA, 0x00)) return false;          // red unused
  if (!regWrite(REG_LED2_PA, led_ir_pa)) return false;     // ir
  if (!regWrite(REG_LED3_PA, led_green_pa)) return false;  // green
  if (!regWrite(REG_LED4_PA, 0x00)) return false;          // second green driver unused

  // Time slot assignment.
  if (!regWrite(REG_MULTI_LED_1, (uint8_t)((SLOT2_LED << 4) | SLOT1_LED))) return false;
  if (!regWrite(REG_MULTI_LED_2, (uint8_t)((SLOT4_LED << 4) | SLOT3_LED))) return false;

  // Enter multi-LED mode last, then clear pointers so sample 0 is real.
  if (!regWrite(REG_MODE_CONFIG, MODE_MULTI_LED)) return false;
  clearFifoPointers();

  g_active_slots = (SLOT1_LED ? 1 : 0) + (SLOT2_LED ? 1 : 0) +
                   (SLOT3_LED ? 1 : 0) + (SLOT4_LED ? 1 : 0);
  g_output_fs = (float)SAMPLE_RATE_SPS / (float)SAMPLE_AVERAGING;
  return true;
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

static void printHeader() {
  Serial.println(F("# mapquest-ppg v0"));
  Serial.printf("# part_id=0x%02X rev=0x%02X\n", EXPECTED_PART_ID, g_rev_id);
  Serial.printf("# fs=%.2f\n", g_output_fs);

  Serial.print(F("# channels="));
  bool first = true;
  const uint8_t slots[4] = {SLOT1_LED, SLOT2_LED, SLOT3_LED, SLOT4_LED};
  for (uint8_t i = 0; i < 4; i++) {
    if (!slots[i]) continue;
    if (!first) Serial.print(',');
    Serial.print(ledName(slots[i]));
    first = false;
  }
  Serial.println();

  Serial.printf("# adc_range_na=%u pulse_width_us=%u resolution_bits=%u\n",
                ADC_RANGE_NA, PULSE_WIDTH_US, resolutionBits(PULSE_WIDTH_US));
  Serial.printf("# internal_sps=%u smp_ave=%u\n", SAMPLE_RATE_SPS, SAMPLE_AVERAGING);
  Serial.printf("# led_green_ma=%.1f led_ir_ma=%.1f\n",
                led_green_pa * 0.2f, led_ir_pa * 0.2f);

  // CSV column header, matching the active slots.
  Serial.print(F("idx,us"));
  for (uint8_t i = 0; i < 4; i++) {
    if (!slots[i]) continue;
    Serial.print(',');
    Serial.print(ledName(slots[i]));
  }
  Serial.println();
}

static void reportTemperature() {
  regWrite(REG_TEMP_CONFIG, 0x01);
  delay(100);
  uint8_t ti = 0, tf = 0;
  if (regRead(REG_TEMP_INT, ti) && regRead(REG_TEMP_FRAC, tf)) {
    // Integer part is 2's complement in 1 degC steps; fraction always adds positively.
    const float t = (float)(int8_t)ti + (tf & 0x0F) * 0.0625f;
    Serial.printf("# temp=%.4f\n", t);
  } else {
    Serial.println(F("# temp=ERR"));
  }
}

// ---------------------------------------------------------------------------
// FIFO drain
// ---------------------------------------------------------------------------

static void drainFifo() {
  uint8_t wr = 0, rd = 0;
  if (!regRead(REG_FIFO_WR_PTR, wr) || !regRead(REG_FIFO_RD_PTR, rd)) return;

  uint8_t nSamples = (uint8_t)((wr - rd) & 0x1F);
  if (nSamples == 0) return;

  uint8_t ovf = 0;
  if (regRead(REG_OVF_COUNTER, ovf) && ovf > 0) {
    // FIFO overran: samples were lost before we got here. Report so the host
    // can mark a discontinuity rather than silently splicing the gap shut.
    Serial.printf("# OVF %u\n", ovf);
  }

  const uint8_t bytesPerSample = g_active_slots * 3;
  const uint32_t us = micros();

  // Burst-read the FIFO in chunks that fit the Wire buffer. One I2C transaction
  // per chunk, each an integral number of samples so a sample never straddles.
  const uint8_t samplesPerChunk = (uint8_t)(I2C_CHUNK_BYTES / bytesPerSample);

  while (nSamples > 0) {
    const uint8_t n = (nSamples < samplesPerChunk) ? nSamples : samplesPerChunk;
    const uint8_t want = n * bytesPerSample;

    Wire.beginTransmission(MAX30101_ADDR);
    Wire.write(REG_FIFO_DATA);
    if (Wire.endTransmission(false) != 0) return;
    if (Wire.requestFrom((uint8_t)MAX30101_ADDR, want) != want) return;

    for (uint8_t s = 0; s < n; s++) {
      Serial.print(g_sample_idx++);
      Serial.print(',');
      Serial.print(us);
      for (uint8_t c = 0; c < g_active_slots; c++) {
        const uint32_t b0 = Wire.read();
        const uint32_t b1 = Wire.read();
        const uint32_t b2 = Wire.read();
        // Data is left-aligned with bit 17 always MSB, so mask to 18 bits.
        const uint32_t v = ((b0 << 16) | (b1 << 8) | b2) & 0x03FFFF;
        Serial.print(',');
        Serial.print(v);
      }
      Serial.println();
    }
    nSamples -= n;
  }
}

// ---------------------------------------------------------------------------
// Serial commands
// ---------------------------------------------------------------------------

// Reads digits following a command letter, with a short timeout so a bare
// command letter does not hang the loop.
static int readIntArg() {
  int value = -1;
  const uint32_t deadline = millis() + 50;
  while (millis() < deadline) {
    if (!Serial.available()) continue;
    const int c = Serial.peek();
    if (c < '0' || c > '9') break;
    Serial.read();
    if (value < 0) value = 0;
    value = value * 10 + (c - '0');
    if (value > 255) { value = 255; break; }
  }
  return value;
}

static void handleCommand(char cmd) {
  switch (cmd) {
    case 's':
      g_streaming = !g_streaming;
      if (g_streaming) {
        g_sample_idx = 0;
        clearFifoPointers();
        printHeader();
      } else {
        Serial.println(F("# stopped"));
      }
      break;

    case '?':
      printHeader();
      break;

    case 't':
      reportTemperature();
      break;

    case 'g': {
      const int v = readIntArg();
      if (v >= 0) {
        led_green_pa = (uint8_t)v;
        regWrite(REG_LED3_PA, led_green_pa);
        Serial.printf("# led_green_ma=%.1f\n", led_green_pa * 0.2f);
      }
      break;
    }

    case 'i': {
      const int v = readIntArg();
      if (v >= 0) {
        led_ir_pa = (uint8_t)v;
        regWrite(REG_LED2_PA, led_ir_pa);
        Serial.printf("# led_ir_ma=%.1f\n", led_ir_pa * 0.2f);
      }
      break;
    }

    case 'r':
      if (configureSensor()) {
        g_sample_idx = 0;
        Serial.println(F("# reset ok"));
        printHeader();
      } else {
        Serial.println(F("# reset FAILED"));
      }
      break;

    default:
      break;  // ignore newlines and stray bytes
  }
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(SERIAL_BAUD);
  // Native USB CDC: give the host a moment to enumerate, but do not block
  // forever if the sketch is running headless.
  const uint32_t deadline = millis() + 3000;
  while (!Serial && millis() < deadline) delay(10);

  Wire.begin();
  Wire.setClock(400000);  // MAX30101 supports 400 kHz fast mode

  uint8_t partId = 0;
  if (!regRead(REG_PART_ID, partId)) {
    Serial.println(F("# ERROR: no I2C response at 0x57 — check wiring (SDA=A4, SCL=A5, 3V3, GND)"));
    return;
  }
  if (partId != EXPECTED_PART_ID) {
    Serial.printf("# ERROR: unexpected part id 0x%02X (expected 0x%02X)\n", partId, EXPECTED_PART_ID);
    return;
  }
  regRead(REG_REV_ID, g_rev_id);

  if (!configureSensor()) {
    Serial.println(F("# ERROR: sensor configuration failed"));
    return;
  }

  Serial.println(F("# ready — send 's' to start streaming, '?' for config"));
}

void loop() {
  while (Serial.available()) {
    handleCommand((char)Serial.read());
  }
  if (g_streaming) {
    drainFifo();
  }
}
