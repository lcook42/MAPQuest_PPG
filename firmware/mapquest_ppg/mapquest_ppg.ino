// MAPQuest — return-to-flow PPG logger firmware
//
// Target : Arduino Nano ESP32 (ESP32-S3)
// Sensor : SparkFun SEN-16474 Photodetector Breakout - MAX30101 (Qwiic), I2C 0x57
//
// Streams raw Red + IR counts at 100 Hz for the return-to-flow MAP pipeline
// described in docs/SPEC.md. The firmware does no filtering and no
// interpretation; all analysis happens on the host.
//
// Wiring (SPEC 2.2) — 3.3 V logic both sides, no level shifting:
//   breakout 3V3 -> 3V3        [HX710B VCC  -> 3V3 ]
//   breakout GND -> GND        [HX710B GND  -> GND ]
//   breakout SDA -> A4         [HX710B DOUT -> D3  ]
//   breakout SCL -> A5         [HX710B SCK  -> D2  ]
//   breakout INT -> unused
// HX710B is optional; set PRESSURE_ENABLED in config.h once it is fitted.
//
// No external libraries required — the MAX30101 driver is inline.

#include <WiFi.h>
#include <Wire.h>
#include <esp_bt.h>

#include "config.h"

// ---------------------------------------------------------------------------
// MAX30101 register map
// ---------------------------------------------------------------------------

static const uint8_t MAX30101_ADDR = 0x57;

enum : uint8_t {
  REG_INT_STATUS_1 = 0x00,
  REG_FIFO_WR_PTR  = 0x04,
  REG_OVF_COUNTER  = 0x05,
  REG_FIFO_RD_PTR  = 0x06,
  REG_FIFO_DATA    = 0x07,
  REG_FIFO_CONFIG  = 0x08,
  REG_MODE_CONFIG  = 0x09,
  REG_SPO2_CONFIG  = 0x0A,
  REG_LED1_PA      = 0x0C,  // red
  REG_LED2_PA      = 0x0D,  // ir
  REG_LED3_PA      = 0x0E,  // green (unused)
  REG_LED4_PA      = 0x0F,  // green (unused)
  REG_TEMP_INT     = 0x1F,
  REG_TEMP_FRAC    = 0x20,
  REG_TEMP_CONFIG  = 0x21,
  REG_REV_ID       = 0xFE,
  REG_PART_ID      = 0xFF,
};

static const uint8_t MODE_SPO2        = 0x03;  // Red + IR
static const uint8_t MODE_RESET_BIT   = 0x40;
static const uint8_t EXPECTED_PART_ID = 0x15;

// In SpO2 mode the FIFO holds 2 channels x 3 bytes. The FIFO is 32 deep, so at
// 100 Hz it fills in 320 ms — we must poll well inside that (SPEC 3.1).
static const uint8_t  ACTIVE_CHANNELS   = 2;
static const uint8_t  BYTES_PER_SAMPLE  = ACTIVE_CHANNELS * 3;
static const size_t   I2C_CHUNK_BYTES   = 120;  // ESP32 Wire buffer is 128

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

static uint8_t  g_ir_amp     = DEFAULT_IR_AMP;
static uint8_t  g_red_amp    = DEFAULT_RED_AMP;
static uint8_t  g_rev_id     = 0;
static uint8_t  g_part_id    = 0;      // as actually read, not as expected
static bool     g_sensor_ok  = false;  // did setup() find and configure the sensor?
static bool     g_streaming  = false;
static uint32_t g_sample_idx = 0;
static uint32_t g_dark_ir    = 0;
static uint32_t g_dark_red   = 0;
static bool     g_dark_valid = false;
static float    g_output_fs  = 0.0f;

static char     g_cmd[32];
static uint8_t  g_cmd_len = 0;

#if PRESSURE_ENABLED
static int32_t  g_p_raw = 0;
static uint32_t g_p_idx = 0;
#endif

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
// Register field encoders
// ---------------------------------------------------------------------------

static uint8_t encodeSampleRate(uint16_t sps) {
  switch (sps) {
    case 50: return 0;   case 100:  return 1;  case 200:  return 2;  case 400:  return 3;
    case 800: return 4;  case 1000: return 5;  case 1600: return 6;  case 3200: return 7;
    default: return 3;
  }
}

static uint8_t encodePulseWidth(uint16_t us) {
  switch (us) {
    case 69: return 0; case 118: return 1; case 215: return 2; case 411: return 3;
    default: return 3;
  }
}

static uint8_t resolutionBits(uint16_t us) {
  switch (us) {
    case 69: return 15; case 118: return 16; case 215: return 17; case 411: return 18;
    default: return 18;
  }
}

static uint8_t encodeAdcRange(uint16_t na) {
  switch (na) {
    case 2048: return 0; case 4096: return 1; case 8192: return 2; case 16384: return 3;
    default: return 1;
  }
}

static uint8_t encodeAveraging(uint8_t n) {
  switch (n) {
    case 1: return 0; case 2: return 1; case 4:  return 2;
    case 8: return 3; case 16: return 4; case 32: return 5;
    default: return 0;
  }
}

// ---------------------------------------------------------------------------
// Sensor setup
// ---------------------------------------------------------------------------

static void clearFifo() {
  regWrite(REG_FIFO_WR_PTR, 0x00);
  regWrite(REG_OVF_COUNTER, 0x00);
  regWrite(REG_FIFO_RD_PTR, 0x00);
}

static bool configureSensor() {
  if (!regWrite(REG_MODE_CONFIG, MODE_RESET_BIT)) return false;
  for (int i = 0; i < 100; i++) {
    uint8_t m;
    if (regRead(REG_MODE_CONFIG, m) && (m & MODE_RESET_BIT) == 0) break;
    delay(5);
  }

  // Averaging + rollover. Rollover keeps the FIFO live if the host stalls; we
  // detect the resulting gap from OVF_COUNTER rather than letting it wedge.
  const uint8_t fifoCfg = (encodeAveraging(SAMPLE_AVERAGING) << 5) | (1 << 4) | 0x0F;
  if (!regWrite(REG_FIFO_CONFIG, fifoCfg)) return false;

  const uint8_t spo2Cfg = (encodeAdcRange(ADC_RANGE_NA) << 5) |
                          (encodeSampleRate(SAMPLE_RATE_SPS) << 2) |
                          encodePulseWidth(PULSE_WIDTH_US);
  if (!regWrite(REG_SPO2_CONFIG, spo2Cfg)) return false;

  if (!regWrite(REG_LED1_PA, g_red_amp)) return false;
  if (!regWrite(REG_LED2_PA, g_ir_amp)) return false;
  if (!regWrite(REG_LED3_PA, 0x00)) return false;
  if (!regWrite(REG_LED4_PA, 0x00)) return false;

  if (!regWrite(REG_MODE_CONFIG, MODE_SPO2)) return false;
  clearFifo();

  g_output_fs = (float)SAMPLE_RATE_SPS / (float)SAMPLE_AVERAGING;
  return true;
}

// ---------------------------------------------------------------------------
// HX710B pressure (SPEC 3.2) — compiled out until the sensor is fitted
// ---------------------------------------------------------------------------

#if PRESSURE_ENABLED
static portMUX_TYPE g_hx_mux = portMUX_INITIALIZER_UNLOCKED;

static bool hx710bReady() {
  return digitalRead(PIN_HX710B_DOUT) == LOW;
}

// SCK must not stay HIGH longer than 60 us or the HX710B powers down, so the
// whole 24-bit shift runs inside a critical section where no interrupt can
// stretch a pulse (SPEC 3.2).
static bool hx710bRead(int32_t &out) {
  if (!hx710bReady()) return false;

  uint32_t raw = 0;
  portENTER_CRITICAL(&g_hx_mux);
  for (uint8_t i = 0; i < HX710B_PULSES; i++) {
    digitalWrite(PIN_HX710B_SCK, HIGH);
    delayMicroseconds(1);
    if (i < 24) raw = (raw << 1) | (uint32_t)digitalRead(PIN_HX710B_DOUT);
    digitalWrite(PIN_HX710B_SCK, LOW);
    delayMicroseconds(1);
  }
  portEXIT_CRITICAL(&g_hx_mux);

  // Sign-extend the 24-bit two's complement value.
  if (raw & 0x800000UL) raw |= 0xFF000000UL;
  out = (int32_t)raw;
  return true;
}

static void servicePressure() {
  int32_t v;
  if (hx710bRead(v)) {
    g_p_raw = v;
    g_p_idx++;
  }
}
#endif  // PRESSURE_ENABLED

// ---------------------------------------------------------------------------
// Sensor bring-up — probe, identify, configure. Safe to call again at runtime
// so a wiring fix does not need a reflash.
// ---------------------------------------------------------------------------

static bool bringUpSensor() {
  g_sensor_ok = false;
  g_part_id = 0;
  g_rev_id = 0;

  if (!regRead(REG_PART_ID, g_part_id)) {
    Serial.println(F("E,no I2C response at 0x57 — check SDA=A4, SCL=A5, 3V3, GND"));
    return false;
  }
  if (g_part_id != EXPECTED_PART_ID) {
    Serial.printf("E,unexpected part id 0x%02X (expected 0x%02X)\n",
                  g_part_id, EXPECTED_PART_ID);
    return false;
  }
  regRead(REG_REV_ID, g_rev_id);

  if (!configureSensor()) {
    Serial.println(F("E,sensor configuration failed"));
    return false;
  }

  g_sensor_ok = true;
  return true;
}

// ---------------------------------------------------------------------------
// Metadata header (SPEC 3.4 / 4)
// ---------------------------------------------------------------------------

static void printInfo() {
  Serial.printf("# firmware=%s\n", FIRMWARE_VERSION);
  // Report the part id we actually read, never the expected constant. Echoing
  // the constant would make a missing sensor look present to the host, which
  // is the one case this field exists to catch.
  Serial.printf("# part_id=0x%02X rev=0x%02X sensor_ok=%d\n",
                g_part_id, g_rev_id, g_sensor_ok ? 1 : 0);
  if (!g_sensor_ok) {
    Serial.println(F("E,sensor not initialised — check SDA=A4, SCL=A5, 3V3, GND, "
                     "then send R to retry"));
  }
  Serial.printf("# mode=spo2 channels=ir,red\n");
  Serial.printf("# fs=%.2f\n", g_output_fs);
  Serial.printf("# internal_sps=%u smp_ave=%u\n", SAMPLE_RATE_SPS, SAMPLE_AVERAGING);
  Serial.printf("# pulse_width_us=%u resolution_bits=%u adc_range_na=%u\n",
                PULSE_WIDTH_US, resolutionBits(PULSE_WIDTH_US), ADC_RANGE_NA);
  Serial.printf("# ir_amp=%u red_amp=%u\n", g_ir_amp, g_red_amp);
  Serial.printf("# ir_amp_ma=%.1f red_amp_ma=%.1f\n", g_ir_amp * 0.2f, g_red_amp * 0.2f);
  if (g_dark_valid) {
    Serial.printf("# dark_ir=%lu dark_red=%lu\n",
                  (unsigned long)g_dark_ir, (unsigned long)g_dark_red);
  }
#if PRESSURE_ENABLED
  Serial.println(F("# pressure=hx710b fields=p_idx,p_raw"));
#else
  Serial.println(F("# pressure=none"));
#endif
  Serial.printf("# columns=%s\n",
#if PRESSURE_ENABLED
                "idx,t_us,ir,red,p_idx,p_raw"
#else
                "idx,t_us,ir,red"
#endif
  );
}

// ---------------------------------------------------------------------------
// DARK offset measurement (SPEC 3.4)
// ---------------------------------------------------------------------------

static bool readOneSampleBlocking(uint32_t &ir, uint32_t &red, uint32_t timeout_ms) {
  const uint32_t deadline = millis() + timeout_ms;
  while (millis() < deadline) {
    uint8_t wr = 0, rd = 0;
    if (!regRead(REG_FIFO_WR_PTR, wr) || !regRead(REG_FIFO_RD_PTR, rd)) return false;
    if (((wr - rd) & 0x1F) == 0) continue;

    Wire.beginTransmission(MAX30101_ADDR);
    Wire.write(REG_FIFO_DATA);
    if (Wire.endTransmission(false) != 0) return false;
    if (Wire.requestFrom((uint8_t)MAX30101_ADDR, (uint8_t)BYTES_PER_SAMPLE) != BYTES_PER_SAMPLE) {
      return false;
    }
    // SpO2 mode FIFO order is Red then IR.
    uint32_t v[2];
    for (uint8_t c = 0; c < 2; c++) {
      const uint32_t b0 = Wire.read(), b1 = Wire.read(), b2 = Wire.read();
      v[c] = ((b0 << 16) | (b1 << 8) | b2) & 0x03FFFF;
    }
    red = v[0];
    ir  = v[1];
    return true;
  }
  return false;
}

static void measureDark() {
  if (g_streaming) {
    Serial.println(F("E,DARK refused while streaming — send STOP first"));
    return;
  }

  const uint8_t savedIr = g_ir_amp, savedRed = g_red_amp;
  regWrite(REG_LED1_PA, 0x00);
  regWrite(REG_LED2_PA, 0x00);
  delay(50);
  clearFifo();

  uint64_t sumIr = 0, sumRed = 0;
  uint16_t n = 0;
  for (uint16_t i = 0; i < DARK_SAMPLES; i++) {
    uint32_t ir, red;
    if (!readOneSampleBlocking(ir, red, 200)) break;
    sumIr += ir;
    sumRed += red;
    n++;
  }

  regWrite(REG_LED1_PA, savedRed);
  regWrite(REG_LED2_PA, savedIr);
  delay(50);
  clearFifo();

  if (n == 0) {
    Serial.println(F("E,DARK failed — no samples"));
    return;
  }
  g_dark_ir    = (uint32_t)(sumIr / n);
  g_dark_red   = (uint32_t)(sumRed / n);
  g_dark_valid = true;
  Serial.printf("# dark_ir=%lu dark_red=%lu dark_n=%u\n",
                (unsigned long)g_dark_ir, (unsigned long)g_dark_red, n);
}

// ---------------------------------------------------------------------------
// FIFO drain + streaming
// ---------------------------------------------------------------------------

static void drainFifo() {
  uint8_t wr = 0, rd = 0;
  if (!regRead(REG_FIFO_WR_PTR, wr) || !regRead(REG_FIFO_RD_PTR, rd)) return;

  uint8_t nSamples = (uint8_t)((wr - rd) & 0x1F);
  if (nSamples == 0) return;

  uint8_t ovf = 0;
  if (regRead(REG_OVF_COUNTER, ovf) && ovf > 0) {
    // Samples were lost before we got here. Report it rather than silently
    // splicing the gap shut — SPEC 3.5 requires zero dropped samples, so this
    // line is an acceptance-criteria failure, not a nuisance warning.
    Serial.printf("E,FIFO overflow, %u samples lost\n", ovf);
  }

  const uint32_t t_us = micros();
  const uint8_t samplesPerChunk = (uint8_t)(I2C_CHUNK_BYTES / BYTES_PER_SAMPLE);

  while (nSamples > 0) {
    const uint8_t n = (nSamples < samplesPerChunk) ? nSamples : samplesPerChunk;
    const uint8_t want = n * BYTES_PER_SAMPLE;

    Wire.beginTransmission(MAX30101_ADDR);
    Wire.write(REG_FIFO_DATA);
    if (Wire.endTransmission(false) != 0) return;
    if (Wire.requestFrom((uint8_t)MAX30101_ADDR, want) != want) return;

    for (uint8_t s = 0; s < n; s++) {
      // SpO2 mode FIFO order is Red then IR; data is left-aligned with bit 17
      // as MSB, so mask to 18 bits.
      const uint32_t r0 = Wire.read(), r1 = Wire.read(), r2 = Wire.read();
      const uint32_t i0 = Wire.read(), i1 = Wire.read(), i2 = Wire.read();
      const uint32_t red = ((r0 << 16) | (r1 << 8) | r2) & 0x03FFFF;
      const uint32_t ir  = ((i0 << 16) | (i1 << 8) | i2) & 0x03FFFF;

#if PRESSURE_ENABLED
      Serial.printf("S,%lu,%lu,%lu,%lu,%lu,%ld\n",
                    (unsigned long)g_sample_idx++, (unsigned long)t_us,
                    (unsigned long)ir, (unsigned long)red,
                    (unsigned long)g_p_idx, (long)g_p_raw);
#else
      Serial.printf("S,%lu,%lu,%lu,%lu\n",
                    (unsigned long)g_sample_idx++, (unsigned long)t_us,
                    (unsigned long)ir, (unsigned long)red);
#endif
    }
    nSamples -= n;
  }
}

// ---------------------------------------------------------------------------
// Command handling (SPEC 3.4)
// ---------------------------------------------------------------------------

static bool setAmp(const char *arg, bool isIr) {
  if (g_streaming) {
    Serial.println(F("E,LED amplitude locked while streaming — send STOP first"));
    return false;
  }
  char *end = nullptr;
  const long v = strtol(arg, &end, 10);
  if (end == arg || v < 0 || v > 255) {
    Serial.println(F("E,amplitude must be 0-255"));
    return false;
  }
  if (isIr) {
    g_ir_amp = (uint8_t)v;
    regWrite(REG_LED2_PA, g_ir_amp);
    Serial.printf("# ir_amp=%u ir_amp_ma=%.1f\n", g_ir_amp, g_ir_amp * 0.2f);
  } else {
    g_red_amp = (uint8_t)v;
    regWrite(REG_LED1_PA, g_red_amp);
    Serial.printf("# red_amp=%u red_amp_ma=%.1f\n", g_red_amp, g_red_amp * 0.2f);
  }
  return true;
}

static void handleCommand(char *line) {
  // Split the verb from its argument.
  char *arg = strchr(line, ' ');
  if (arg) { *arg = '\0'; arg++; while (*arg == ' ') arg++; }

  if (strcmp(line, "START") == 0) {
    if (!g_sensor_ok) {
      // Streaming from an unconfigured sensor would emit a plausible-looking
      // flat line, which is worse than refusing.
      Serial.println(F("E,cannot START — sensor not initialised; send R to retry"));
      return;
    }
    g_sample_idx = 0;
    clearFifo();
    printInfo();
    Serial.println(F("# streaming=1"));
    g_streaming = true;

  } else if (strcmp(line, "STOP") == 0) {
    g_streaming = false;
    Serial.println(F("# streaming=0"));

  } else if (strcmp(line, "INFO") == 0) {
    printInfo();

  } else if (strcmp(line, "DARK") == 0) {
    measureDark();

  } else if (strcmp(line, "IRAMP") == 0) {
    setAmp(arg ? arg : "", true);

  } else if (strcmp(line, "REDAMP") == 0) {
    setAmp(arg ? arg : "", false);

  } else if (strcmp(line, "R") == 0 || strcmp(line, "RETRY") == 0) {
    g_streaming = false;
    if (bringUpSensor()) {
      Serial.println(F("# sensor re-initialised"));
    }
    printInfo();

  } else if (strcmp(line, "TEMP") == 0) {
    regWrite(REG_TEMP_CONFIG, 0x01);
    delay(100);
    uint8_t ti = 0, tf = 0;
    if (regRead(REG_TEMP_INT, ti) && regRead(REG_TEMP_FRAC, tf)) {
      Serial.printf("# temp_c=%.4f\n", (float)(int8_t)ti + (tf & 0x0F) * 0.0625f);
    }

  } else if (line[0] != '\0') {
    Serial.printf("E,unknown command: %s\n", line);
  }
}

static void pollSerial() {
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (g_cmd_len > 0) {
        g_cmd[g_cmd_len] = '\0';
        handleCommand(g_cmd);
        g_cmd_len = 0;
      }
    } else if (g_cmd_len < sizeof(g_cmd) - 1) {
      g_cmd[g_cmd_len++] = c;
    }
  }
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(SERIAL_BAUD);
  const uint32_t deadline = millis() + 3000;
  while (!Serial && millis() < deadline) delay(10);

  // Radios off — they add nothing here and cost power and noise (SPEC 3.1).
  WiFi.mode(WIFI_OFF);
  btStop();

  Wire.begin();
  Wire.setClock(400000);

#if PRESSURE_ENABLED
  pinMode(PIN_HX710B_SCK, OUTPUT);
  pinMode(PIN_HX710B_DOUT, INPUT);
  digitalWrite(PIN_HX710B_SCK, LOW);
#endif

  bringUpSensor();

  printInfo();
  if (g_sensor_ok) {
    Serial.println(F("# ready — send START to stream, INFO for settings"));
  } else {
    Serial.println(F("# not ready — fix the wiring and send R, or reset the board"));
  }
}

void loop() {
  pollSerial();
#if PRESSURE_ENABLED
  servicePressure();
#endif
  if (g_streaming) {
    drainFifo();
  }
}
