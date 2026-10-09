/*
  MAX30101 + Seeed Studio XIAO ESP32-S3
  Guided MAP Research Prototype
  ------------------------------------

  Current hardware:
    XIAO ESP32-S3
    MAX30101 breakout over I2C
      SDA -> D4 / GPIO5
      SCL -> D5 / GPIO6
      VCC -> 3V3
      GND -> GND

  Arduino libraries:
    - SparkFun MAX3010x Sensor Library
    - Preferences (included with ESP32 Arduino core)

  Serial baud: 115200

  Main commands:
    START / CALIBRATE  Guided 12-point calibration + 3-point validation
    MEASURE            Guided MAP measurement
    RETUNE             Re-run optical LED tuning
    PLOT               Stream normalized PPG for Arduino Serial Plotter
    CALPLOT            Plot reference vs predicted MAP after calibration
    STATUS             Print saved model / sensor status
    CLEAR              Delete saved model
    HELP               Print commands

  Notes:
    - RED and IR are the primary optical channels.
    - GREEN is supplemental and has its own lower target range.
    - Weak/absent pulsatility does NOT automatically invalidate a window.
    - Confident but physiologically implausible HR DOES reject a window.
    - MAP predictions are range-checked. A failed estimate is held at the
      last sane value / prior and explicitly marked "DO NOT COUNT".
*/

#include <Arduino.h>
#include <Wire.h>
#include <MAX30105.h>
#include <Preferences.h>
#include <math.h>

// ============================================================================
// HARDWARE
// ============================================================================

MAX30105 ppg;
Preferences preferences;

static const int PPG_SDA = 5;   // XIAO D4
static const int PPG_SCL = 6;   // XIAO D5

static const uint8_t MAX30101_I2C_ADDRESS = 0x57;

// ============================================================================
// SENSOR CONFIGURATION
// ============================================================================

static const int SAMPLE_RATE_HZ = 100;
static const int WINDOW_SECONDS = 6;
static const int MAX_SAMPLES = SAMPLE_RATE_HZ * WINDOW_SECONDS;

static const int SAMPLE_AVERAGE = 1;
static const int PULSE_WIDTH_US = 411;
static const int ADC_RANGE = 16384;

static const uint32_t ADC_MAX_COUNT = 262143UL;
static const uint32_t HARD_SATURATION = 245000UL;

// RED/IR operating targets
static const uint32_t PRIMARY_TARGET = 120000UL;
static const uint32_t PRIMARY_LOW = 90000UL;
static const uint32_t PRIMARY_HIGH = 155000UL;

// GREEN is intentionally allowed to operate much lower
static const uint32_t GREEN_TARGET = 15000UL;
static const uint32_t GREEN_LOW = 6000UL;
static const uint32_t GREEN_HIGH = 30000UL;

static const uint8_t LED_START_CODE = 0x20;
static const uint8_t LED_MIN_CODE = 0x01;
static const uint8_t LED_MAX_CODE = 0xFF;
static const uint8_t GREEN_MAX_CODE = 0xA0; // 160 / 255

static const int DARK_SAMPLES = 32;
static const int TUNE_SAMPLES = 48;
static const int TUNE_MAX_ITERS = 18;

static const uint32_t MIN_PRIMARY_LIGHT_DELTA = 5000UL;
static const uint32_t MIN_GREEN_LIGHT_DELTA = 800UL;

// ============================================================================
// GUIDED PIPELINE SETTINGS
// ============================================================================

static const int CALIBRATION_POINTS = 12;
static const int VALIDATION_POINTS = 3;
static const int WINDOWS_PER_POINT = 3;
static const int MAX_WINDOW_RETRIES = 6;

static const double MIN_WINDOW_QUALITY = 65.0;
static const double MAX_ACCEPTABLE_DC_DRIFT = 0.08; // 8%
static const double DESIRED_MAP_SPAN = 15.0;

// MAP sanity gates for this research build
static const double MAP_HARD_MIN = 35.0;
static const double MAP_HARD_MAX = 180.0;
static const double MAP_TYPICAL_LOW = 60.0;
static const double MAP_TYPICAL_HIGH = 120.0;
static const double MAP_PRIOR = 85.0;

// HR sanity. HR is only used as a window sanity check if confidence is high.
static const double HR_HARD_MIN = 35.0;
static const double HR_HARD_MAX = 200.0;
static const double HR_SOFT_LOW = 45.0;
static const double HR_SOFT_HIGH = 160.0;
static const double HR_CONFIDENCE_FOR_SANITY = 0.55;

// Measurement agreement
static const double MAP_WINDOW_SPREAD_GOOD = 8.0;
static const double MAP_WINDOW_SPREAD_HARD = 15.0;

// ============================================================================
// MODEL SETTINGS
// ============================================================================

static const int MODEL_N = 8;
static const int MODEL_DIM = MODEL_N + 1; // intercept + standardized features

static const uint32_t MODEL_MAGIC = 0x4D415031UL; // "MAP1"
static const uint16_t MODEL_VERSION = 3;

static const int MAX_GRAPH_POINTS = CALIBRATION_POINTS + VALIDATION_POINTS;

const double RIDGE_CANDIDATES[] = {0.01, 0.1, 1.0, 10.0, 100.0};
static const int RIDGE_CANDIDATE_COUNT =
    sizeof(RIDGE_CANDIDATES) / sizeof(RIDGE_CANDIDATES[0]);

// ============================================================================
// DATA TYPES
// ============================================================================

struct LedState {
  uint8_t red;
  uint8_t ir;
  uint8_t green;
};

struct TripleStats {
  double redMean;
  double irMean;
  double greenMean;

  double redStd;
  double irStd;
  double greenStd;

  uint32_t redMin;
  uint32_t irMin;
  uint32_t greenMin;

  uint32_t redMax;
  uint32_t irMax;
  uint32_t greenMax;

  int n;
};

struct Features {
  bool valid;
  int n;

  double redDC;
  double irDC;
  double greenDC;

  double redACRms;
  double irACRms;
  double greenACRms;

  double redPPI;
  double irPPI;
  double greenPPI;

  double redIrCorr;

  double irSkew;
  double irKurtosis;

  double hrBpm;
  double hrConfidence;

  double opticalQuality;
  double pulsatilityScore;

  double redDrift;
  double irDrift;
  double greenDrift;

  bool possibleMotion;

  uint32_t redMin;
  uint32_t redMax;
  uint32_t irMin;
  uint32_t irMax;
  uint32_t greenMin;
  uint32_t greenMax;
};

struct MapResult {
  double map;
  bool accepted;
  bool softRangeWarning;
  const char *mode;
};

struct PersistentModel {
  uint32_t magic;
  uint16_t version;
  uint16_t reserved;

  bool valid;
  int32_t calibrationCount;
  int32_t validationCount;

  double featureMean[MODEL_N];
  double featureStd[MODEL_N];
  double beta[MODEL_DIM];

  double lambda;
  double cvMAE;
  double validationMAE;
  double readiness;

  double calibrationMinMAP;
  double calibrationMaxMAP;
  double meanOpticalQuality;

  uint8_t tunedRed;
  uint8_t tunedIR;
  uint8_t tunedGreen;
  uint8_t graphCount;

  float graphReference[MAX_GRAPH_POINTS];
  float graphPredicted[MAX_GRAPH_POINTS];
};

// Future active-pressure hook.
struct PressureOpticalSample {
  uint32_t tMs;
  float pressureMmHg;
  uint32_t red;
  uint32_t ir;
  uint32_t green;
};

// ============================================================================
// GLOBAL STATE
// ============================================================================

LedState led = {LED_START_CODE, LED_START_CODE, LED_START_CODE};
TripleStats darkStats = {};

PersistentModel model = {};

uint32_t redBuf[MAX_SAMPLES];
uint32_t irBuf[MAX_SAMPLES];
uint32_t greenBuf[MAX_SAMPLES];

float redFilt[MAX_SAMPLES];
float irFilt[MAX_SAMPLES];
float greenFilt[MAX_SAMPLES];

double calX[CALIBRATION_POINTS][MODEL_N];
double calY[CALIBRATION_POINTS];
double calQuality[CALIBRATION_POINTS];
int calCount = 0;

double lastSaneMAP = MAP_PRIOR;

// ============================================================================
// BASIC UTILITIES
// ============================================================================

double clampD(double x, double lo, double hi) {
  if (x < lo) return lo;
  if (x > hi) return hi;
  return x;
}

double safeLog(double x) {
  if (x < 1e-9) x = 1e-9;
  return log(x);
}

double ledCodeToApproxMilliamp(uint8_t code) {
  return 50.0 * ((double)code / 255.0);
}

void swapDouble(double &a, double &b) {
  double t = a;
  a = b;
  b = t;
}

double median3(double a, double b, double c) {
  if (a > b) swapDouble(a, b);
  if (b > c) swapDouble(b, c);
  if (a > b) swapDouble(a, b);
  return b;
}

String waitForLine() {
  while (true) {
    if (Serial.available()) {
      String line = Serial.readStringUntil('\n');
      line.trim();
      return line;
    }
    delay(10);
  }
}

void waitForEnter(const char *message = "Press ENTER when ready.") {
  Serial.println(message);
  while (!Serial.available()) delay(10);
  Serial.readStringUntil('\n');
}

bool parseDoubleStrict(const String &s, double &value) {
  if (s.length() == 0) return false;

  char buffer[40];
  s.toCharArray(buffer, sizeof(buffer));

  char *endPtr = nullptr;
  value = strtod(buffer, &endPtr);

  if (endPtr == buffer) return false;
  while (*endPtr == ' ' || *endPtr == '\t') endPtr++;
  return (*endPtr == '\0') && isfinite(value);
}

// ============================================================================
// PERSISTENT MODEL STORAGE
// ============================================================================

void initializeEmptyModel() {
  memset(&model, 0, sizeof(model));
  model.magic = MODEL_MAGIC;
  model.version = MODEL_VERSION;
  model.valid = false;
  model.readiness = 5.0;
  model.tunedRed = LED_START_CODE;
  model.tunedIR = LED_START_CODE;
  model.tunedGreen = LED_START_CODE;

  for (int i = 0; i < MODEL_N; i++) {
    model.featureStd[i] = 1.0;
  }

  model.beta[0] = MAP_PRIOR;
}

bool loadModelFromFlash() {
  initializeEmptyModel();

  if (!preferences.begin("mapproto", true)) {
    return false;
  }

  size_t len = preferences.getBytesLength("model");

  if (len != sizeof(PersistentModel)) {
    preferences.end();
    return false;
  }

  PersistentModel loaded = {};
  size_t got = preferences.getBytes("model", &loaded, sizeof(loaded));
  preferences.end();

  if (got != sizeof(loaded)) return false;
  if (loaded.magic != MODEL_MAGIC) return false;
  if (loaded.version != MODEL_VERSION) return false;
  if (!loaded.valid) return false;

  model = loaded;
  lastSaneMAP = MAP_PRIOR;

  led.red = model.tunedRed;
  led.ir = model.tunedIR;
  led.green = model.tunedGreen;

  return true;
}

bool saveModelToFlash() {
  if (!preferences.begin("mapproto", false)) {
    Serial.println("WARNING: could not open Preferences for saving.");
    return false;
  }

  size_t written = preferences.putBytes("model", &model, sizeof(model));
  preferences.end();

  if (written != sizeof(model)) {
    Serial.println("WARNING: calibration model was not fully saved.");
    return false;
  }

  Serial.println("Calibration model saved to ESP32 flash.");
  return true;
}

void clearModelFromFlash() {
  if (preferences.begin("mapproto", false)) {
    preferences.remove("model");
    preferences.end();
  }

  initializeEmptyModel();
  lastSaneMAP = MAP_PRIOR;

  Serial.println("Saved MAP calibration deleted.");
}

// ============================================================================
// MAX30101 LOW-LEVEL ACQUISITION
// ============================================================================

void applyLedCurrents() {
  ppg.setPulseAmplitudeRed(led.red);
  ppg.setPulseAmplitudeIR(led.ir);
  ppg.setPulseAmplitudeGreen(led.green);
}

bool readOneSample(uint32_t &red,
                   uint32_t &ir,
                   uint32_t &green,
                   uint32_t timeoutMs = 300) {
  uint32_t start = millis();

  while (millis() - start < timeoutMs) {
    ppg.check();

    if (ppg.available()) {
      red = ppg.getFIFORed();
      ir = ppg.getFIFOIR();
      green = ppg.getFIFOGreen();
      ppg.nextSample();
      return true;
    }

    delay(1);
  }

  return false;
}

bool collectStats(int count, TripleStats &s) {
  if (count <= 0) return false;

  memset(&s, 0, sizeof(s));

  double rMean = 0.0, iMean = 0.0, gMean = 0.0;
  double rM2 = 0.0, iM2 = 0.0, gM2 = 0.0;

  uint32_t rMin = 0xFFFFFFFFUL;
  uint32_t iMin = 0xFFFFFFFFUL;
  uint32_t gMin = 0xFFFFFFFFUL;

  uint32_t rMax = 0;
  uint32_t iMax = 0;
  uint32_t gMax = 0;

  ppg.clearFIFO();

  for (int n = 1; n <= count; n++) {
    uint32_t r, i, g;

    if (!readOneSample(r, i, g, 600)) {
      Serial.println("ERROR: MAX30101 sample timeout.");
      return false;
    }

    double dr = (double)r - rMean;
    rMean += dr / n;
    rM2 += dr * ((double)r - rMean);

    double di = (double)i - iMean;
    iMean += di / n;
    iM2 += di * ((double)i - iMean);

    double dg = (double)g - gMean;
    gMean += dg / n;
    gM2 += dg * ((double)g - gMean);

    if (r < rMin) rMin = r;
    if (i < iMin) iMin = i;
    if (g < gMin) gMin = g;

    if (r > rMax) rMax = r;
    if (i > iMax) iMax = i;
    if (g > gMax) gMax = g;
  }

  s.redMean = rMean;
  s.irMean = iMean;
  s.greenMean = gMean;

  s.redStd = (count > 1) ? sqrt(rM2 / (count - 1)) : 0.0;
  s.irStd = (count > 1) ? sqrt(iM2 / (count - 1)) : 0.0;
  s.greenStd = (count > 1) ? sqrt(gM2 / (count - 1)) : 0.0;

  s.redMin = rMin;
  s.irMin = iMin;
  s.greenMin = gMin;

  s.redMax = rMax;
  s.irMax = iMax;
  s.greenMax = gMax;

  s.n = count;

  return true;
}

void measureDarkBaseline() {
  uint8_t oldR = led.red;
  uint8_t oldI = led.ir;
  uint8_t oldG = led.green;

  ppg.setPulseAmplitudeRed(0);
  ppg.setPulseAmplitudeIR(0);
  ppg.setPulseAmplitudeGreen(0);

  delay(80);

  if (!collectStats(DARK_SAMPLES, darkStats)) {
    Serial.println("WARNING: dark baseline could not be measured.");
    memset(&darkStats, 0, sizeof(darkStats));
  }

  led.red = oldR;
  led.ir = oldI;
  led.green = oldG;

  applyLedCurrents();
  delay(80);
}

// ============================================================================
// LED AUTO-TUNING
// ============================================================================

uint8_t tuneChannelCode(uint8_t oldCode,
                        double mean,
                        double darkMean,
                        uint32_t target,
                        uint32_t low,
                        uint32_t high,
                        uint8_t maxCode,
                        uint32_t minUsefulDelta,
                        int maxStep) {

  if (mean >= HARD_SATURATION) {
    int reduced = (int)oldCode - max(8, (int)oldCode / 4);
    reduced = constrain(reduced, (int)LED_MIN_CODE, (int)maxCode);
    return (uint8_t)reduced;
  }

  if (mean >= low && mean <= high) {
    return oldCode;
  }

  double illuminatedDelta = mean - darkMean;

  if (illuminatedDelta < (double)minUsefulDelta && mean < low) {
    int next = (int)oldCode + maxStep;
    next = constrain(next, (int)LED_MIN_CODE, (int)maxCode);
    return (uint8_t)next;
  }

  double desired =
      ((double)oldCode) * ((double)target / max(1.0, mean));

  int delta = (int)lround(desired - oldCode);
  delta = constrain(delta, -maxStep, maxStep);

  if (delta == 0) {
    delta = (mean < low) ? 1 : -1;
  }

  int next = (int)oldCode + delta;
  next = constrain(next, (int)LED_MIN_CODE, (int)maxCode);

  return (uint8_t)next;
}

bool primaryChannelsInBand(const TripleStats &s) {
  return
      s.redMean >= PRIMARY_LOW &&
      s.redMean <= PRIMARY_HIGH &&
      s.irMean >= PRIMARY_LOW &&
      s.irMean <= PRIMARY_HIGH;
}

bool greenChannelInBand(const TripleStats &s) {
  return
      s.greenMean >= GREEN_LOW &&
      s.greenMean <= GREEN_HIGH;
}

void printTuneLine(int iter, const TripleStats &s) {
  Serial.print("TUNE ");
  Serial.print(iter);

  Serial.print(" | R ");
  Serial.print(led.red);
  Serial.print(" (~");
  Serial.print(ledCodeToApproxMilliamp(led.red), 1);
  Serial.print("mA) mean=");
  Serial.print(s.redMean, 0);

  Serial.print(" | IR ");
  Serial.print(led.ir);
  Serial.print(" (~");
  Serial.print(ledCodeToApproxMilliamp(led.ir), 1);
  Serial.print("mA) mean=");
  Serial.print(s.irMean, 0);

  Serial.print(" | G ");
  Serial.print(led.green);
  Serial.print(" (~");
  Serial.print(ledCodeToApproxMilliamp(led.green), 1);
  Serial.print("mA) mean=");
  Serial.println(s.greenMean, 0);
}

bool autoTuneLEDs(bool reusePrevious = true) {
  Serial.println();
  Serial.println("=== OPTICAL AUTO-TUNE ===");

  measureDarkBaseline();

  Serial.print("Dark R/IR/G = ");
  Serial.print(darkStats.redMean, 0);
  Serial.print(" / ");
  Serial.print(darkStats.irMean, 0);
  Serial.print(" / ");
  Serial.println(darkStats.greenMean, 0);

  if (!reusePrevious) {
    led.red = LED_START_CODE;
    led.ir = LED_START_CODE;
    led.green = LED_START_CODE;
  } else {
    if (led.red < LED_MIN_CODE) led.red = LED_START_CODE;
    if (led.ir < LED_MIN_CODE) led.ir = LED_START_CODE;
    if (led.green < LED_MIN_CODE) led.green = LED_START_CODE;
  }

  applyLedCurrents();
  delay(100);

  TripleStats s = {};
  bool tuned = false;

  for (int iter = 1; iter <= TUNE_MAX_ITERS; iter++) {
    if (!collectStats(TUNE_SAMPLES, s)) return false;

    printTuneLine(iter, s);

    bool primaryOK = primaryChannelsInBand(s);
    bool greenOK = greenChannelInBand(s);

    if (primaryOK && greenOK) {
      tuned = true;
      Serial.println("AUTO-TUNE: RED/IR primary channels and GREEN supplemental channel acceptable.");
      break;
    }

    led.red = tuneChannelCode(
        led.red, s.redMean, darkStats.redMean,
        PRIMARY_TARGET, PRIMARY_LOW, PRIMARY_HIGH,
        LED_MAX_CODE, MIN_PRIMARY_LIGHT_DELTA, 24);

    led.ir = tuneChannelCode(
        led.ir, s.irMean, darkStats.irMean,
        PRIMARY_TARGET, PRIMARY_LOW, PRIMARY_HIGH,
        LED_MAX_CODE, MIN_PRIMARY_LIGHT_DELTA, 24);

    led.green = tuneChannelCode(
        led.green, s.greenMean, darkStats.greenMean,
        GREEN_TARGET, GREEN_LOW, GREEN_HIGH,
        GREEN_MAX_CODE, MIN_GREEN_LIGHT_DELTA, 16);

    applyLedCurrents();
    delay(50);
  }

  if (!collectStats(TUNE_SAMPLES, s)) return false;

  bool primaryWeak =
      ((s.redMean - darkStats.redMean) < MIN_PRIMARY_LIGHT_DELTA &&
       led.red >= LED_MAX_CODE - 1) ||
      ((s.irMean - darkStats.irMean) < MIN_PRIMARY_LIGHT_DELTA &&
       led.ir >= LED_MAX_CODE - 1);

  bool saturated =
      s.redMax >= HARD_SATURATION ||
      s.irMax >= HARD_SATURATION ||
      s.greenMax >= HARD_SATURATION;

  bool primaryOK = primaryChannelsInBand(s);

  Serial.print("FINAL LED R/IR/G = ");
  Serial.print(led.red);
  Serial.print(" / ");
  Serial.print(led.ir);
  Serial.print(" / ");
  Serial.println(led.green);

  if (!greenChannelInBand(s)) {
    Serial.println("NOTE: GREEN did not reach its preferred band; RED/IR remain primary.");
  }

  if (primaryWeak) {
    Serial.println("AUTO-TUNE FAIL: RED/IR optical return is too weak.");
    Serial.println("Readjust sensor contact/position and retry.");
  }

  if (saturated) {
    Serial.println("AUTO-TUNE FAIL: optical ADC saturation remains.");
    Serial.println("Reduce ambient light / pressure / reposition sensor.");
  }

  if (!primaryOK) {
    Serial.println("AUTO-TUNE FAIL: RED/IR could not reach a stable operating range.");
  }

  if (primaryOK && !primaryWeak && !saturated) {
    tuned = true;
  }

  if (tuned) {
    model.tunedRed = led.red;
    model.tunedIR = led.ir;
    model.tunedGreen = led.green;
  }

  Serial.println("=========================");

  return tuned;
}

// ============================================================================
// SIGNAL PROCESSING
// ============================================================================

double meanU32(const uint32_t *a, int n) {
  if (n <= 0) return 0.0;

  double sum = 0.0;
  for (int i = 0; i < n; i++) sum += a[i];
  return sum / n;
}

double meanU32Range(const uint32_t *a, int start, int end) {
  if (end <= start) return 0.0;

  double sum = 0.0;
  for (int i = start; i < end; i++) sum += a[i];
  return sum / (end - start);
}

void minMaxU32(const uint32_t *a,
               int n,
               uint32_t &mn,
               uint32_t &mx) {
  mn = 0xFFFFFFFFUL;
  mx = 0;

  for (int i = 0; i < n; i++) {
    if (a[i] < mn) mn = a[i];
    if (a[i] > mx) mx = a[i];
  }
}

double rmsFloat(const float *a, int n) {
  if (n <= 0) return 0.0;

  double s2 = 0.0;
  for (int i = 0; i < n; i++) {
    s2 += (double)a[i] * (double)a[i];
  }

  return sqrt(s2 / n);
}

double corrFloat(const float *a,
                 const float *b,
                 int n) {
  if (n < 3) return 0.0;

  double ma = 0.0;
  double mb = 0.0;

  for (int i = 0; i < n; i++) {
    ma += a[i];
    mb += b[i];
  }

  ma /= n;
  mb /= n;

  double num = 0.0;
  double da2 = 0.0;
  double db2 = 0.0;

  for (int i = 0; i < n; i++) {
    double da = a[i] - ma;
    double db = b[i] - mb;

    num += da * db;
    da2 += da * da;
    db2 += db * db;
  }

  double den = sqrt(da2 * db2);

  if (den <= 1e-12) return 0.0;

  return num / den;
}

void momentsFloat(const float *a,
                  int n,
                  double &skew,
                  double &kurtosis) {
  skew = 0.0;
  kurtosis = 0.0;

  if (n < 5) return;

  double mean = 0.0;

  for (int i = 0; i < n; i++) {
    mean += a[i];
  }

  mean /= n;

  double m2 = 0.0;
  double m3 = 0.0;
  double m4 = 0.0;

  for (int i = 0; i < n; i++) {
    double d = a[i] - mean;
    double d2 = d * d;

    m2 += d2;
    m3 += d2 * d;
    m4 += d2 * d2;
  }

  m2 /= n;
  m3 /= n;
  m4 /= n;

  if (m2 <= 1e-12) return;

  skew = m3 / pow(m2, 1.5);
  kurtosis = m4 / (m2 * m2);
}

void bandpass(const uint32_t *raw,
              float *out,
              int n,
              float fs,
              float hpHz = 0.35f,
              float lpHz = 5.0f) {
  if (n <= 0) return;

  const float dt = 1.0f / fs;

  const float rcHP = 1.0f / (2.0f * PI * hpHz);
  const float alphaHP = rcHP / (rcHP + dt);

  const float rcLP = 1.0f / (2.0f * PI * lpHz);
  const float alphaLP = dt / (rcLP + dt);

  float prevX = (float)raw[0];
  float hp = 0.0f;
  float lp = 0.0f;

  out[0] = 0.0f;

  for (int i = 1; i < n; i++) {
    float x = (float)raw[i];

    hp = alphaHP * (hp + x - prevX);
    lp = lp + alphaLP * (hp - lp);

    out[i] = lp;
    prevX = x;
  }
}

void estimateHR(const float *sig,
                int n,
                float fs,
                double &bpm,
                double &confidence) {
  bpm = NAN;
  confidence = 0.0;

  if (n < (int)(fs * 3.0f)) return;

  int minLag = (int)floor(fs * 60.0 / 200.0);
  int maxLag = (int)ceil(fs * 60.0 / 35.0);

  if (minLag < 1) minLag = 1;
  if (maxLag > n / 2) maxLag = n / 2;

  double energy = 0.0;

  for (int i = 0; i < n; i++) {
    energy += (double)sig[i] * sig[i];
  }

  if (energy < 1e-6) return;

  double best = -1e30;
  int bestLag = -1;

  for (int lag = minLag; lag <= maxLag; lag++) {
    double num = 0.0;
    double e1 = 0.0;
    double e2 = 0.0;

    for (int i = lag; i < n; i++) {
      double a = sig[i];
      double b = sig[i - lag];

      num += a * b;
      e1 += a * a;
      e2 += b * b;
    }

    double den = sqrt(e1 * e2);
    double c = (den > 1e-12) ? num / den : 0.0;

    if (c > best) {
      best = c;
      bestLag = lag;
    }
  }

  if (bestLag > 0 && best >= 0.35) {
    bpm = 60.0 * fs / bestLag;
    confidence = clampD(best, 0.0, 1.0);
  }
}

bool acquireWindow(int &nOut) {
  nOut = 0;

  ppg.clearFIFO();

  uint32_t start = millis();

  while (nOut < MAX_SAMPLES) {
    ppg.check();

    while (ppg.available() && nOut < MAX_SAMPLES) {
      redBuf[nOut] = ppg.getFIFORed();
      irBuf[nOut] = ppg.getFIFOIR();
      greenBuf[nOut] = ppg.getFIFOGreen();

      ppg.nextSample();
      nOut++;
    }

    if (millis() - start >
        (uint32_t)(WINDOW_SECONDS + 4) * 1000UL) {
      Serial.println("ERROR: feature-window acquisition timeout.");
      return false;
    }

    delay(1);
  }

  return true;
}

Features computeFeatures(int n) {
  Features f = {};
  f.valid = false;
  f.n = n;
  f.hrBpm = NAN;

  if (n < SAMPLE_RATE_HZ * 3) return f;

  f.redDC = meanU32(redBuf, n);
  f.irDC = meanU32(irBuf, n);
  f.greenDC = meanU32(greenBuf, n);

  minMaxU32(redBuf, n, f.redMin, f.redMax);
  minMaxU32(irBuf, n, f.irMin, f.irMax);
  minMaxU32(greenBuf, n, f.greenMin, f.greenMax);

  int half = n / 2;

  double r1 = meanU32Range(redBuf, 0, half);
  double r2 = meanU32Range(redBuf, half, n);

  double i1 = meanU32Range(irBuf, 0, half);
  double i2 = meanU32Range(irBuf, half, n);

  double g1 = meanU32Range(greenBuf, 0, half);
  double g2 = meanU32Range(greenBuf, half, n);

  f.redDrift = fabs(r2 - r1) / max(1.0, f.redDC);
  f.irDrift = fabs(i2 - i1) / max(1.0, f.irDC);
  f.greenDrift = fabs(g2 - g1) / max(1.0, f.greenDC);

  bandpass(redBuf, redFilt, n, SAMPLE_RATE_HZ);
  bandpass(irBuf, irFilt, n, SAMPLE_RATE_HZ);
  bandpass(greenBuf, greenFilt, n, SAMPLE_RATE_HZ);

  const int skip = SAMPLE_RATE_HZ;
  const int nn = n - skip;

  f.redACRms = rmsFloat(redFilt + skip, nn);
  f.irACRms = rmsFloat(irFilt + skip, nn);
  f.greenACRms = rmsFloat(greenFilt + skip, nn);

  f.redPPI = (f.redDC > 1.0) ? f.redACRms / f.redDC : 0.0;
  f.irPPI = (f.irDC > 1.0) ? f.irACRms / f.irDC : 0.0;
  f.greenPPI = (f.greenDC > 1.0) ? f.greenACRms / f.greenDC : 0.0;

  f.redIrCorr = corrFloat(redFilt + skip, irFilt + skip, nn);

  momentsFloat(irFilt + skip, nn, f.irSkew, f.irKurtosis);

  estimateHR(
      irFilt + skip,
      nn,
      SAMPLE_RATE_HZ,
      f.hrBpm,
      f.hrConfidence);

  // Separate pulsatility from optical coupling.
  f.pulsatilityScore =
      100.0 * clampD((f.irPPI * 1000.0) / 8.0, 0.0, 1.0);

  double redContact =
      clampD((f.redDC - darkStats.redMean) / 30000.0, 0.0, 1.0);

  double irContact =
      clampD((f.irDC - darkStats.irMean) / 30000.0, 0.0, 1.0);

  double greenContact =
      clampD((f.greenDC - darkStats.greenMean) / 5000.0, 0.0, 1.0);

  double contactScore =
      0.45 * redContact +
      0.45 * irContact +
      0.10 * greenContact;

  uint32_t maxAll = max(f.redMax, max(f.irMax, f.greenMax));

  double headroomScore = 1.0;

  if (maxAll >= 255000UL) {
    headroomScore = 0.0;
  } else if (maxAll >= HARD_SATURATION) {
    headroomScore = 0.25;
  } else if (maxAll >= 225000UL) {
    headroomScore = 0.70;
  }

  double redCenter =
      clampD(1.0 - fabs(f.redDC - PRIMARY_TARGET) / PRIMARY_TARGET, 0.0, 1.0);

  double irCenter =
      clampD(1.0 - fabs(f.irDC - PRIMARY_TARGET) / PRIMARY_TARGET, 0.0, 1.0);

  double centerScore = 0.5 * (redCenter + irCenter);

  double maxDrift = max(f.redDrift, f.irDrift);

  double stabilityScore =
      clampD(1.0 - maxDrift / 0.10, 0.0, 1.0);

  f.opticalQuality =
      100.0 * (
          0.35 * contactScore +
          0.25 * headroomScore +
          0.20 * centerScore +
          0.20 * stabilityScore);

  bool correlationSuspicious =
      f.pulsatilityScore >= 10.0 &&
      fabs(f.redIrCorr) < 0.15;

  bool excessivePPI =
      f.redPPI > 0.08 ||
      f.irPPI > 0.08;

  f.possibleMotion =
      maxDrift > MAX_ACCEPTABLE_DC_DRIFT ||
      correlationSuspicious ||
      excessivePPI;

  bool primaryContact =
      (f.redDC - darkStats.redMean) >= MIN_PRIMARY_LIGHT_DELTA &&
      (f.irDC - darkStats.irMean) >= MIN_PRIMARY_LIGHT_DELTA;

  bool notSaturated =
      f.redMax < 255000UL &&
      f.irMax < 255000UL &&
      f.greenMax < 255000UL;

  f.valid =
      primaryContact &&
      notSaturated &&
      f.opticalQuality >= 35.0;

  return f;
}

// ============================================================================
// WINDOW SANITY CHECKS
// ============================================================================

void printFeatureSummary(const Features &f) {
  Serial.print("Optical quality: ");
  Serial.print(f.opticalQuality, 1);
  Serial.print("/100 | Pulsatility: ");
  Serial.print(f.pulsatilityScore, 1);
  Serial.print("/100 | R/IR corr: ");
  Serial.print(f.redIrCorr, 2);

  Serial.print(" | HR: ");

  if (isnan(f.hrBpm)) {
    Serial.print("not resolved");
  } else {
    Serial.print(f.hrBpm, 1);
    Serial.print(" bpm (conf ");
    Serial.print(f.hrConfidence, 2);
    Serial.print(")");
  }

  Serial.println();
}

bool windowPassesSanity(const Features &f, bool verbose = true) {
  bool accepted = true;

  if (!f.valid) {
    if (verbose) Serial.println("REJECT: basic optical validity failed.");
    accepted = false;
  }

  if (f.opticalQuality < MIN_WINDOW_QUALITY) {
    if (verbose) {
      Serial.print("REJECT: optical quality ");
      Serial.print(f.opticalQuality, 1);
      Serial.print(" < ");
      Serial.println(MIN_WINDOW_QUALITY, 1);
    }
    accepted = false;
  }

  if (f.redMax >= HARD_SATURATION ||
      f.irMax >= HARD_SATURATION) {
    if (verbose) {
      Serial.println("REJECT: RED/IR near ADC saturation. Readjust sensor.");
    }
    accepted = false;
  }

  if (f.redDrift > MAX_ACCEPTABLE_DC_DRIFT ||
      f.irDrift > MAX_ACCEPTABLE_DC_DRIFT) {
    if (verbose) {
      Serial.println("REJECT: contact/optical baseline drifted too much.");
    }
    accepted = false;
  }

  if (f.possibleMotion) {
    if (verbose) {
      Serial.println("REJECT: possible motion or unstable optical waveform.");
    }
    accepted = false;
  }

  // Do NOT require HR. Low pulsatility may be valid for the target LVAD use case.
  if (!isnan(f.hrBpm) &&
      f.hrConfidence >= HR_CONFIDENCE_FOR_SANITY) {

    if (f.hrBpm < HR_HARD_MIN || f.hrBpm > HR_HARD_MAX) {
      if (verbose) {
        Serial.print("REJECT: confidently detected HR ");
        Serial.print(f.hrBpm, 1);
        Serial.println(" bpm is outside the configured sanity range.");
      }
      accepted = false;
    } else if (verbose &&
               (f.hrBpm < HR_SOFT_LOW || f.hrBpm > HR_SOFT_HIGH)) {
      Serial.print("CHECK: unusual but possible HR = ");
      Serial.print(f.hrBpm, 1);
      Serial.println(" bpm. Window may still be counted.");
    }
  }

  return accepted;
}

bool getGoodWindow(Features &result) {
  for (int attempt = 1; attempt <= MAX_WINDOW_RETRIES; attempt++) {
    Serial.print("Collecting optical window ");
    Serial.print(attempt);
    Serial.print("/");
    Serial.println(MAX_WINDOW_RETRIES);

    int n = 0;

    if (!acquireWindow(n)) {
      continue;
    }

    Features f = computeFeatures(n);

    printFeatureSummary(f);

    if (windowPassesSanity(f, true)) {
      Serial.println("WINDOW ACCEPTED.");
      result = f;
      return true;
    }

    Serial.println("WINDOW NOT COUNTED. Keep still / readjust contact.");

    if (attempt == 3) {
      Serial.println("Three failed windows -> automatic re-tune.");
      autoTuneLEDs(true);
    }

    delay(600);
  }

  Serial.println("FAILED: could not obtain an acceptable optical window.");
  return false;
}

// ============================================================================
// MULTI-WINDOW AGGREGATION
// ============================================================================

Features aggregateThree(const Features w[WINDOWS_PER_POINT]) {
  Features a = {};
  a.valid = true;
  a.n = w[0].n;

  a.redDC = median3(w[0].redDC, w[1].redDC, w[2].redDC);
  a.irDC = median3(w[0].irDC, w[1].irDC, w[2].irDC);
  a.greenDC = median3(w[0].greenDC, w[1].greenDC, w[2].greenDC);

  a.redACRms = median3(w[0].redACRms, w[1].redACRms, w[2].redACRms);
  a.irACRms = median3(w[0].irACRms, w[1].irACRms, w[2].irACRms);
  a.greenACRms = median3(w[0].greenACRms, w[1].greenACRms, w[2].greenACRms);

  a.redPPI = median3(w[0].redPPI, w[1].redPPI, w[2].redPPI);
  a.irPPI = median3(w[0].irPPI, w[1].irPPI, w[2].irPPI);
  a.greenPPI = median3(w[0].greenPPI, w[1].greenPPI, w[2].greenPPI);

  a.redIrCorr = median3(w[0].redIrCorr, w[1].redIrCorr, w[2].redIrCorr);

  a.irSkew = median3(w[0].irSkew, w[1].irSkew, w[2].irSkew);
  a.irKurtosis = median3(w[0].irKurtosis, w[1].irKurtosis, w[2].irKurtosis);

  a.opticalQuality =
      median3(w[0].opticalQuality, w[1].opticalQuality, w[2].opticalQuality);

  a.pulsatilityScore =
      median3(w[0].pulsatilityScore, w[1].pulsatilityScore, w[2].pulsatilityScore);

  a.redDrift = median3(w[0].redDrift, w[1].redDrift, w[2].redDrift);
  a.irDrift = median3(w[0].irDrift, w[1].irDrift, w[2].irDrift);
  a.greenDrift = median3(w[0].greenDrift, w[1].greenDrift, w[2].greenDrift);

  // HR aggregation only if enough windows resolved HR.
  double hrValues[3];
  int hrN = 0;

  for (int i = 0; i < WINDOWS_PER_POINT; i++) {
    if (!isnan(w[i].hrBpm) &&
        w[i].hrConfidence >= HR_CONFIDENCE_FOR_SANITY) {
      hrValues[hrN++] = w[i].hrBpm;
    }
  }

  if (hrN == 3) {
    a.hrBpm = median3(hrValues[0], hrValues[1], hrValues[2]);
    a.hrConfidence =
        median3(w[0].hrConfidence, w[1].hrConfidence, w[2].hrConfidence);
  } else if (hrN == 2) {
    a.hrBpm = 0.5 * (hrValues[0] + hrValues[1]);
    a.hrConfidence = 0.5;
  } else if (hrN == 1) {
    a.hrBpm = hrValues[0];
    a.hrConfidence = 0.35;
  } else {
    a.hrBpm = NAN;
    a.hrConfidence = 0.0;
  }

  a.possibleMotion = false;

  return a;
}

bool collectStablePoint(int pointNumber,
                        int totalPoints,
                        const char *label,
                        Features &combined) {
  Serial.println();
  Serial.println("--------------------------------------------------");
  Serial.print(label);
  Serial.print(" ");
  Serial.print(pointNumber);
  Serial.print("/");
  Serial.println(totalPoints);
  Serial.println("--------------------------------------------------");

  Serial.println("Keep sensor placement and contact pressure consistent.");

  Features windows[WINDOWS_PER_POINT];

  for (int i = 0; i < WINDOWS_PER_POINT; i++) {
    Serial.print("Accepted window required ");
    Serial.print(i + 1);
    Serial.print("/");
    Serial.println(WINDOWS_PER_POINT);

    if (!getGoodWindow(windows[i])) {
      return false;
    }
  }

  combined = aggregateThree(windows);

  Serial.print("Point optical quality (median) = ");
  Serial.print(combined.opticalQuality, 1);
  Serial.println("/100");

  return true;
}

// ============================================================================
// FEATURE VECTOR
// ============================================================================

bool makeModelVector(const Features &f, double x[MODEL_N]) {
  if (!f.valid ||
      f.redDC <= 1.0 ||
      f.irDC <= 1.0 ||
      f.greenDC <= 1.0) {
    return false;
  }

  // Features deliberately avoid requiring a valid HR so the model can still
  // operate in weakly pulsatile / non-pulsatile experiments.
  x[0] = safeLog(f.irDC);
  x[1] = safeLog(f.redDC / f.irDC);
  x[2] = safeLog(f.greenDC / f.irDC);
  x[3] = f.irPPI * 1000.0;
  x[4] = f.redPPI * 1000.0;
  x[5] = f.greenPPI * 1000.0;
  x[6] = f.redIrCorr;
  x[7] = f.irSkew;

  for (int i = 0; i < MODEL_N; i++) {
    if (!isfinite(x[i])) return false;
  }

  return true;
}

// ============================================================================
// RIDGE REGRESSION
// ============================================================================

bool solveLinearSystem(double A[MODEL_DIM][MODEL_DIM],
                       double b[MODEL_DIM],
                       double x[MODEL_DIM],
                       int dim) {
  double aug[MODEL_DIM][MODEL_DIM + 1];

  for (int r = 0; r < dim; r++) {
    for (int c = 0; c < dim; c++) {
      aug[r][c] = A[r][c];
    }
    aug[r][dim] = b[r];
  }

  for (int col = 0; col < dim; col++) {
    int pivot = col;
    double pivotAbs = fabs(aug[pivot][col]);

    for (int r = col + 1; r < dim; r++) {
      double candidate = fabs(aug[r][col]);

      if (candidate > pivotAbs) {
        pivot = r;
        pivotAbs = candidate;
      }
    }

    if (pivotAbs < 1e-10) {
      return false;
    }

    if (pivot != col) {
      for (int c = col; c <= dim; c++) {
        double temp = aug[col][c];
        aug[col][c] = aug[pivot][c];
        aug[pivot][c] = temp;
      }
    }

    double divisor = aug[col][col];

    for (int c = col; c <= dim; c++) {
      aug[col][c] /= divisor;
    }

    for (int r = 0; r < dim; r++) {
      if (r == col) continue;

      double factor = aug[r][col];

      for (int c = col; c <= dim; c++) {
        aug[r][c] -= factor * aug[col][c];
      }
    }
  }

  for (int i = 0; i < dim; i++) {
    x[i] = aug[i][dim];
  }

  return true;
}

int includedCount(int n, int excludeIndex) {
  return n - ((excludeIndex >= 0 && excludeIndex < n) ? 1 : 0);
}

bool fitRidge(const double X[][MODEL_N],
              const double y[],
              int n,
              int excludeIndex,
              double lambda,
              double outMean[MODEL_N],
              double outStd[MODEL_N],
              double outBeta[MODEL_DIM]) {

  int nUsed = includedCount(n, excludeIndex);

  if (nUsed < 3) return false;

  for (int j = 0; j < MODEL_N; j++) {
    double sum = 0.0;

    for (int i = 0; i < n; i++) {
      if (i == excludeIndex) continue;
      sum += X[i][j];
    }

    outMean[j] = sum / nUsed;

    double ss = 0.0;

    for (int i = 0; i < n; i++) {
      if (i == excludeIndex) continue;

      double d = X[i][j] - outMean[j];
      ss += d * d;
    }

    double sd = sqrt(ss / max(1, nUsed - 1));

    if (!isfinite(sd) || sd < 1e-6) {
      sd = 1.0;
    }

    outStd[j] = sd;
  }

  double A[MODEL_DIM][MODEL_DIM] = {};
  double b[MODEL_DIM] = {};

  for (int i = 0; i < n; i++) {
    if (i == excludeIndex) continue;

    double row[MODEL_DIM];
    row[0] = 1.0;

    for (int j = 0; j < MODEL_N; j++) {
      row[j + 1] =
          (X[i][j] - outMean[j]) / outStd[j];
    }

    for (int r = 0; r < MODEL_DIM; r++) {
      b[r] += row[r] * y[i];

      for (int c = 0; c < MODEL_DIM; c++) {
        A[r][c] += row[r] * row[c];
      }
    }
  }

  // Ridge penalty on features only, not intercept.
  for (int j = 1; j < MODEL_DIM; j++) {
    A[j][j] += lambda;
  }

  return solveLinearSystem(A, b, outBeta, MODEL_DIM);
}

double predictWithParameters(const double x[MODEL_N],
                             const double mean[MODEL_N],
                             const double stddev[MODEL_N],
                             const double beta[MODEL_DIM]) {

  double y = beta[0];

  for (int j = 0; j < MODEL_N; j++) {
    double z = (x[j] - mean[j]) / max(1e-9, stddev[j]);
    y += beta[j + 1] * z;
  }

  return y;
}

bool chooseAndFitModel() {
  if (calCount < 6) return false;

  double bestLambda = RIDGE_CANDIDATES[0];
  double bestMAE = 1e30;

  Serial.println();
  Serial.println("Selecting ridge regularization by leave-one-out validation...");

  for (int li = 0; li < RIDGE_CANDIDATE_COUNT; li++) {
    double lambda = RIDGE_CANDIDATES[li];
    double absErrorSum = 0.0;
    int validPredictions = 0;

    for (int holdout = 0; holdout < calCount; holdout++) {
      double mean[MODEL_N];
      double sd[MODEL_N];
      double beta[MODEL_DIM];

      if (!fitRidge(
              calX,
              calY,
              calCount,
              holdout,
              lambda,
              mean,
              sd,
              beta)) {
        continue;
      }

      double pred =
          predictWithParameters(
              calX[holdout],
              mean,
              sd,
              beta);

      if (!isfinite(pred)) continue;

      absErrorSum += fabs(pred - calY[holdout]);
      validPredictions++;
    }

    if (validPredictions < calCount - 1) continue;

    double mae = absErrorSum / validPredictions;

    Serial.print("lambda=");
    Serial.print(lambda, 3);
    Serial.print(" | LOOCV MAE=");
    Serial.print(mae, 2);
    Serial.println(" mmHg");

    if (mae < bestMAE) {
      bestMAE = mae;
      bestLambda = lambda;
    }
  }

  if (!isfinite(bestMAE) || bestMAE > 1000.0) {
    return false;
  }

  double finalMean[MODEL_N];
  double finalStd[MODEL_N];
  double finalBeta[MODEL_DIM];

  if (!fitRidge(
          calX,
          calY,
          calCount,
          -1,
          bestLambda,
          finalMean,
          finalStd,
          finalBeta)) {
    return false;
  }

  for (int j = 0; j < MODEL_N; j++) {
    model.featureMean[j] = finalMean[j];
    model.featureStd[j] = finalStd[j];
  }

  for (int j = 0; j < MODEL_DIM; j++) {
    model.beta[j] = finalBeta[j];
  }

  model.lambda = bestLambda;
  model.cvMAE = bestMAE;
  model.calibrationCount = calCount;
  model.valid = true;

  model.graphCount = 0;

  for (int i = 0;
       i < calCount && model.graphCount < MAX_GRAPH_POINTS;
       i++) {

    double pred =
        predictWithParameters(
            calX[i],
            model.featureMean,
            model.featureStd,
            model.beta);

    int k = model.graphCount;

    model.graphReference[k] = (float)calY[i];
    model.graphPredicted[k] = (float)pred;
    model.graphCount++;
  }

  Serial.print("Selected lambda = ");
  Serial.println(bestLambda, 3);

  Serial.print("Cross-validated MAE = ");
  Serial.print(bestMAE, 2);
  Serial.println(" mmHg");

  return true;
}

// ============================================================================
// MAP ESTIMATION + SANITY
// ============================================================================

MapResult estimateMAP(const Features &f) {
  MapResult r = {};

  if (!model.valid) {
    r.map = MAP_PRIOR;
    r.accepted = true;
    r.softRangeWarning = false;
    r.mode = "PRIOR";
    lastSaneMAP = r.map;
    return r;
  }

  double x[MODEL_N];

  if (!makeModelVector(f, x)) {
    r.map = lastSaneMAP;
    r.accepted = false;
    r.softRangeWarning = false;
    r.mode = "HELD_FEATURE_FAIL";
    return r;
  }

  double pred =
      predictWithParameters(
          x,
          model.featureMean,
          model.featureStd,
          model.beta);

  if (!isfinite(pred) ||
      pred < MAP_HARD_MIN ||
      pred > MAP_HARD_MAX) {

    r.map = lastSaneMAP;
    r.accepted = false;
    r.softRangeWarning = false;
    r.mode = "HELD_MAP_SANITY";
    return r;
  }

  r.map = pred;
  r.accepted = true;
  r.softRangeWarning =
      pred < MAP_TYPICAL_LOW ||
      pred > MAP_TYPICAL_HIGH;

  r.mode = "MODEL";

  lastSaneMAP = pred;

  return r;
}

double errorScoreFromMAE(double mae) {
  if (!isfinite(mae)) return 0.0;

  if (mae <= 3.0) return 100.0;
  if (mae <= 5.0) return 100.0 - (mae - 3.0) * 7.5;   // 100 -> 85
  if (mae <= 8.0) return 85.0 - (mae - 5.0) * 8.33;   // 85 -> ~60
  if (mae <= 12.0) return 60.0 - (mae - 8.0) * 8.75;  // 60 -> 25
  if (mae <= 15.0) return 25.0 - (mae - 12.0) * 8.33; // 25 -> ~0

  return 0.0;
}

void calculateReadiness(double validationMAE,
                        int validationCount,
                        double minMAP,
                        double maxMAP,
                        double meanQuality) {

  double pointScore =
      clampD(
          100.0 * ((double)calCount / CALIBRATION_POINTS),
          0.0,
          100.0);

  double mapSpan = maxMAP - minMAP;

  double spanScore =
      clampD(
          100.0 * mapSpan / DESIRED_MAP_SPAN,
          0.0,
          100.0);

  double qualityScore =
      clampD(meanQuality, 0.0, 100.0);

  double cvScore =
      errorScoreFromMAE(model.cvMAE);

  double validationScore =
      (validationCount >= VALIDATION_POINTS)
          ? errorScoreFromMAE(validationMAE)
          : 0.0;

  model.readiness =
      0.15 * pointScore +
      0.15 * spanScore +
      0.15 * qualityScore +
      0.20 * cvScore +
      0.35 * validationScore;

  model.validationMAE = validationMAE;
  model.validationCount = validationCount;
  model.calibrationMinMAP = minMAP;
  model.calibrationMaxMAP = maxMAP;
  model.meanOpticalQuality = meanQuality;

  Serial.println();
  Serial.println("============== MODEL ASSESSMENT ==============");

  Serial.print("Calibration points: ");
  Serial.print(calCount);
  Serial.print("/");
  Serial.println(CALIBRATION_POINTS);

  Serial.print("Reference MAP span: ");
  Serial.print(mapSpan, 1);
  Serial.println(" mmHg");

  if (mapSpan < DESIRED_MAP_SPAN) {
    Serial.println("CHECK: calibration MAP range is narrow; model generalization will be limited.");
  }

  Serial.print("Mean optical quality: ");
  Serial.print(meanQuality, 1);
  Serial.println("/100");

  Serial.print("LOOCV MAE: ");
  Serial.print(model.cvMAE, 2);
  Serial.println(" mmHg");

  Serial.print("Independent validation MAE: ");
  Serial.print(validationMAE, 2);
  Serial.println(" mmHg");

  Serial.print("MODEL READINESS: ");
  Serial.print(model.readiness, 0);
  Serial.println("%");

  if (model.readiness >= 80.0) {
    Serial.println("Readiness assessment: STRONG for this prototype dataset.");
  } else if (model.readiness >= 60.0) {
    Serial.println("Readiness assessment: MODERATE; continue collecting varied reference points.");
  } else {
    Serial.println("Readiness assessment: LOW; do not treat estimates as established.");
  }

  Serial.println("==============================================");
}

// ============================================================================
// REFERENCE MAP ENTRY
// ============================================================================

double askReferenceMAP() {
  while (true) {
    Serial.println();
    Serial.print("Enter synchronized reference MAP (");
    Serial.print(MAP_HARD_MIN, 0);
    Serial.print("-");
    Serial.print(MAP_HARD_MAX, 0);
    Serial.println(" mmHg):");

    String line = waitForLine();

    double value = 0.0;

    if (!parseDoubleStrict(line, value)) {
      Serial.println("Invalid number. Re-enter the reference MAP.");
      continue;
    }

    if (value < MAP_HARD_MIN || value > MAP_HARD_MAX) {
      Serial.println("REFERENCE NOT COUNTED: outside configured hard MAP sanity range.");
      Serial.println("Re-check the reference measurement and enter it again.");
      continue;
    }

    if (value < MAP_TYPICAL_LOW || value > MAP_TYPICAL_HIGH) {
      Serial.print("CHECK: ");
      Serial.print(value, 1);
      Serial.println(" mmHg is outside the typical team-test range configured in this sketch.");
      Serial.println("Type YES to confirm this reference, or anything else to re-enter it.");

      String confirm = waitForLine();
      confirm.toUpperCase();

      if (confirm != "YES") {
        continue;
      }
    }

    return value;
  }
}

// ============================================================================
// GUIDED CALIBRATION
// ============================================================================

void runGuidedCalibration() {
  Serial.println();
  Serial.println("====================================================");
  Serial.println("          GUIDED MAX30101 MAP CALIBRATION");
  Serial.println("====================================================");
  Serial.println("This will collect 12 training points and 3 independent validation points.");
  Serial.println("Each point is the median of 3 accepted 6-second optical windows.");
  Serial.println("Bad motion/contact/sanity windows are rejected automatically.");
  Serial.println();

  waitForEnter("Place the sensor securely on the test site, hold still, then press ENTER.");

  Serial.println();
  Serial.println("STEP 1/5: Optical setup and LED auto-tuning");

  if (!autoTuneLEDs(false)) {
    Serial.println("CALIBRATION STOPPED: optical auto-tune failed.");
    return;
  }

  Serial.println();
  Serial.println("STEP 2/5: Placement stability check");

  Features stability = {};

  if (!getGoodWindow(stability)) {
    Serial.println("CALIBRATION STOPPED: stable placement could not be established.");
    return;
  }

  Serial.println("Placement accepted.");

  calCount = 0;

  double minMAP = 1e9;
  double maxMAP = -1e9;
  double qualitySum = 0.0;

  Serial.println();
  Serial.println("STEP 3/5: Training calibration");
  Serial.println("For each point, obtain the reference MAP as close in time as possible");
  Serial.println("to the optical acquisition.");

  for (int point = 1; point <= CALIBRATION_POINTS; point++) {
    Features combined = {};

    while (!collectStablePoint(
        point,
        CALIBRATION_POINTS,
        "CALIBRATION POINT",
        combined)) {

      Serial.println("Point failed. Readjust placement if necessary.");
      waitForEnter("Press ENTER to re-tune and retry this point.");

      if (!autoTuneLEDs(true)) {
        Serial.println("Re-tune failed. Retry placement.");
      }
    }

    double x[MODEL_N];

    if (!makeModelVector(combined, x)) {
      Serial.println("Feature vector invalid. Repeating this point.");
      point--;
      continue;
    }

    Serial.println("Take/confirm the reference pressure measurement now.");
    double referenceMAP = askReferenceMAP();

    for (int j = 0; j < MODEL_N; j++) {
      calX[calCount][j] = x[j];
    }

    calY[calCount] = referenceMAP;
    calQuality[calCount] = combined.opticalQuality;

    calCount++;

    minMAP = min(minMAP, referenceMAP);
    maxMAP = max(maxMAP, referenceMAP);
    qualitySum += combined.opticalQuality;

    Serial.print("STORED point ");
    Serial.print(calCount);
    Serial.print("/");
    Serial.print(CALIBRATION_POINTS);
    Serial.print(" | MAP=");
    Serial.print(referenceMAP, 1);
    Serial.print(" | quality=");
    Serial.print(combined.opticalQuality, 1);
    Serial.println("/100");
  }

  Serial.println();
  Serial.println("STEP 4/5: Fitting standardized ridge-regression model");

  if (!chooseAndFitModel()) {
    Serial.println("CALIBRATION STOPPED: regression fit failed.");
    return;
  }

  Serial.println();
  Serial.println("STEP 5/5: Independent validation");

  double validationErrorSum = 0.0;
  int validationCount = 0;

  for (int point = 1; point <= VALIDATION_POINTS; point++) {
    Features combined = {};

    while (!collectStablePoint(
        point,
        VALIDATION_POINTS,
        "VALIDATION POINT",
        combined)) {

      Serial.println("Validation point rejected.");
      waitForEnter("Press ENTER to re-tune and retry.");

      autoTuneLEDs(true);
    }

    MapResult prediction = estimateMAP(combined);

    if (!prediction.accepted) {
      Serial.println("Model output failed MAP sanity. Repeating validation point.");
      point--;
      continue;
    }

    Serial.println("Take/confirm the independent reference MAP now.");
    double referenceMAP = askReferenceMAP();

    double error = fabs(prediction.map - referenceMAP);

    validationErrorSum += error;
    validationCount++;

    if (model.graphCount < MAX_GRAPH_POINTS) {
      int k = model.graphCount;
      model.graphReference[k] = (float)referenceMAP;
      model.graphPredicted[k] = (float)prediction.map;
      model.graphCount++;
    }

    Serial.print("Predicted MAP = ");
    Serial.print(prediction.map, 1);
    Serial.println(" mmHg");

    Serial.print("Reference MAP = ");
    Serial.print(referenceMAP, 1);
    Serial.println(" mmHg");

    Serial.print("Absolute error = ");
    Serial.print(error, 1);
    Serial.println(" mmHg");
  }

  double validationMAE =
      (validationCount > 0)
          ? validationErrorSum / validationCount
          : 999.0;

  double meanQuality =
      (calCount > 0)
          ? qualitySum / calCount
          : 0.0;

  calculateReadiness(
      validationMAE,
      validationCount,
      minMAP,
      maxMAP,
      meanQuality);

  model.magic = MODEL_MAGIC;
  model.version = MODEL_VERSION;
  model.valid = true;
  model.tunedRed = led.red;
  model.tunedIR = led.ir;
  model.tunedGreen = led.green;

  saveModelToFlash();

  Serial.println();
  Serial.println("CALIBRATION PIPELINE COMPLETE.");
  Serial.println("Use MEASURE for a guided measurement.");
  Serial.println("Use CALPLOT to graph reference vs predicted MAP.");
}

// ============================================================================
// GUIDED MEASUREMENT
// ============================================================================

double medianOfArray(double *values, int n) {
  if (n <= 0) return NAN;

  for (int i = 0; i < n - 1; i++) {
    for (int j = i + 1; j < n; j++) {
      if (values[j] < values[i]) {
        swapDouble(values[i], values[j]);
      }
    }
  }

  if (n % 2 == 1) {
    return values[n / 2];
  }

  return 0.5 * (values[n / 2 - 1] + values[n / 2]);
}

void runGuidedMeasurement() {
  Serial.println();
  Serial.println("====================================================");
  Serial.println("                 GUIDED MAP MEASUREMENT");
  Serial.println("====================================================");

  if (!model.valid) {
    Serial.println("No saved calibrated model exists.");
    Serial.println("A numeric prior can be shown, but START is recommended first.");
  } else {
    Serial.print("Loaded model readiness = ");
    Serial.print(model.readiness, 0);
    Serial.println("%");
  }

  waitForEnter("Place the sensor at the calibrated site and press ENTER.");

  if (!autoTuneLEDs(true)) {
    Serial.println("MEASUREMENT NOT COUNTED: optical tuning failed.");
    Serial.print("MAP = ");
    Serial.print(lastSaneMAP, 1);
    Serial.println(" mmHg [HELD]");
    return;
  }

  const int MAX_MEASUREMENT_WINDOWS = 5;

  double maps[MAX_MEASUREMENT_WINDOWS];
  double qualities[MAX_MEASUREMENT_WINDOWS];

  int acceptedMaps = 0;
  int requestedWindows = 3;

  for (int i = 0; i < requestedWindows; i++) {
    Features f = {};

    if (!getGoodWindow(f)) {
      Serial.println("Measurement window failed.");
      continue;
    }

    MapResult r = estimateMAP(f);

    Serial.print("Window MAP = ");
    Serial.print(r.map, 1);
    Serial.print(" mmHg [");
    Serial.print(r.mode);
    Serial.println("]");

    if (!r.accepted) {
      Serial.println("WINDOW MAP NOT COUNTED: model output failed sanity.");
      continue;
    }

    if (r.softRangeWarning) {
      Serial.println("CHECK: MAP is outside the configured typical team-test range.");
    }

    maps[acceptedMaps] = r.map;
    qualities[acceptedMaps] = f.opticalQuality;
    acceptedMaps++;
  }

  if (acceptedMaps >= 2 && model.valid) {
    double temp[MAX_MEASUREMENT_WINDOWS];

    for (int i = 0; i < acceptedMaps; i++) temp[i] = maps[i];

    double minV = temp[0];
    double maxV = temp[0];

    for (int i = 1; i < acceptedMaps; i++) {
      if (temp[i] < minV) minV = temp[i];
      if (temp[i] > maxV) maxV = temp[i];
    }

    double initialSpread = maxV - minV;

    if (initialSpread > MAP_WINDOW_SPREAD_GOOD) {
      Serial.print("CHECK: first-window MAP spread = ");
      Serial.print(initialSpread, 1);
      Serial.println(" mmHg. Collecting up to 2 additional windows.");

      while (acceptedMaps < MAX_MEASUREMENT_WINDOWS) {
        Features f = {};

        if (!getGoodWindow(f)) break;

        MapResult r = estimateMAP(f);

        if (!r.accepted) continue;

        maps[acceptedMaps] = r.map;
        qualities[acceptedMaps] = f.opticalQuality;
        acceptedMaps++;
      }
    }
  }

  if (!model.valid) {
    Serial.println();
    Serial.println("============== RESULT ==============");
    Serial.print("MAP = ");
    Serial.print(MAP_PRIOR, 1);
    Serial.println(" mmHg [PRIOR]");
    Serial.println("COUNT THIS MEASUREMENT: NO");
    Serial.println("Reason: no calibrated subject model.");
    Serial.println("====================================");
    return;
  }

  if (acceptedMaps < 2) {
    Serial.println();
    Serial.println("============== RESULT ==============");
    Serial.print("MAP = ");
    Serial.print(lastSaneMAP, 1);
    Serial.println(" mmHg [HELD]");
    Serial.println("COUNT THIS MEASUREMENT: NO");
    Serial.println("Reason: fewer than 2 sane model windows.");
    Serial.println("====================================");
    return;
  }

  double mapCopy[MAX_MEASUREMENT_WINDOWS];

  for (int i = 0; i < acceptedMaps; i++) {
    mapCopy[i] = maps[i];
  }

  double finalMAP = medianOfArray(mapCopy, acceptedMaps);

  double minMAP = maps[0];
  double maxMAP = maps[0];
  double qualitySum = 0.0;

  for (int i = 0; i < acceptedMaps; i++) {
    if (maps[i] < minMAP) minMAP = maps[i];
    if (maps[i] > maxMAP) maxMAP = maps[i];

    qualitySum += qualities[i];
  }

  double spread = maxMAP - minMAP;
  double meanQuality = qualitySum / acceptedMaps;

  double agreementScore =
      clampD(
          100.0 * (1.0 - spread / MAP_WINDOW_SPREAD_HARD),
          0.0,
          100.0);

  double measurementQuality =
      0.45 * meanQuality +
      0.30 * agreementScore +
      0.25 * model.readiness;

  bool countMeasurement =
      spread <= MAP_WINDOW_SPREAD_HARD &&
      measurementQuality >= 60.0 &&
      finalMAP >= MAP_HARD_MIN &&
      finalMAP <= MAP_HARD_MAX;

  lastSaneMAP = finalMAP;

  Serial.println();
  Serial.println("============== RESULT ==============");

  Serial.print("MAP = ");
  Serial.print(finalMAP, 1);
  Serial.println(" mmHg");

  Serial.print("Accepted windows = ");
  Serial.println(acceptedMaps);

  Serial.print("Window-to-window MAP spread = ");
  Serial.print(spread, 1);
  Serial.println(" mmHg");

  Serial.print("Mean optical quality = ");
  Serial.print(meanQuality, 1);
  Serial.println("/100");

  Serial.print("Model readiness = ");
  Serial.print(model.readiness, 0);
  Serial.println("%");

  Serial.print("Measurement quality = ");
  Serial.print(measurementQuality, 0);
  Serial.println("%");

  if (finalMAP < MAP_TYPICAL_LOW ||
      finalMAP > MAP_TYPICAL_HIGH) {
    Serial.println("CHECK: final MAP is outside the configured typical team-test range.");
    Serial.println("Repeat the measurement and compare against a reference.");
  }

  Serial.print("COUNT THIS MEASUREMENT: ");
  Serial.println(countMeasurement ? "YES" : "NO");

  if (!countMeasurement) {
    Serial.println("Readjust / repeat before including this value in the dataset.");
  }

  Serial.println("====================================");
}

// ============================================================================
// GRAPH / SERIAL PLOTTER MODES
// ============================================================================

void plotPPG() {
  Serial.println();
  Serial.println("PPG SERIAL PLOTTER MODE");
  Serial.println("1. Keep sensor in place and still.");
  Serial.println("2. The firmware will capture one 6-second window.");
  Serial.println("3. When told, switch Arduino IDE from Serial Monitor to Serial Plotter.");
  Serial.println();

  waitForEnter();

  if (!autoTuneLEDs(true)) {
    Serial.println("PLOT cancelled: auto-tune failed.");
    return;
  }

  int n = 0;

  if (!acquireWindow(n)) {
    Serial.println("PLOT cancelled: acquisition failed.");
    return;
  }

  Features f = computeFeatures(n);

  Serial.println();
  Serial.println("Capture complete.");
  Serial.println("Open Arduino Serial Plotter at 115200 baud NOW.");
  Serial.println("Graph begins in 5 seconds: normalized RED / IR / GREEN PPG.");
  delay(5000);

  const int skip = SAMPLE_RATE_HZ;

  double rScale = max(1.0, f.redDC);
  double iScale = max(1.0, f.irDC);
  double gScale = max(1.0, f.greenDC);

  for (int i = skip; i < n; i++) {
    double rn = 1000.0 * redFilt[i] / rScale;
    double in = 1000.0 * irFilt[i] / iScale;
    double gn = 1000.0 * greenFilt[i] / gScale;

    Serial.print("RedPPG:");
    Serial.print(rn, 4);

    Serial.print("\tIRPPG:");
    Serial.print(in, 4);

    Serial.print("\tGreenPPG:");
    Serial.println(gn, 4);

    delay(10);
  }

  Serial.println("PLOT_COMPLETE");
}

void plotCalibrationGraph() {
  if (!model.valid || model.graphCount == 0) {
    Serial.println("No calibration graph is stored.");
    return;
  }

  Serial.println();
  Serial.println("CALIBRATION / VALIDATION GRAPH");
  Serial.println("Switch to Arduino Serial Plotter at 115200 baud.");
  Serial.println("ReferenceMAP and PredictedMAP will be shown as two traces.");
  Serial.println("Graph begins in 5 seconds.");
  delay(5000);

  for (int i = 0; i < model.graphCount; i++) {
    Serial.print("ReferenceMAP:");
    Serial.print(model.graphReference[i], 2);

    Serial.print("\tPredictedMAP:");
    Serial.println(model.graphPredicted[i], 2);

    delay(350);
  }

  Serial.println("CALPLOT_COMPLETE");
}

// ============================================================================
// STATUS / HELP
// ============================================================================

void printStatus() {
  Serial.println();
  Serial.println("================ STATUS ================");

  Serial.print("MAX30101 I2C address expected: 0x");
  Serial.println(MAX30101_I2C_ADDRESS, HEX);

  Serial.print("XIAO SDA = D4 / GPIO");
  Serial.println(PPG_SDA);

  Serial.print("XIAO SCL = D5 / GPIO");
  Serial.println(PPG_SCL);

  Serial.print("Current LED codes R/IR/G = ");
  Serial.print(led.red);
  Serial.print("/");
  Serial.print(led.ir);
  Serial.print("/");
  Serial.println(led.green);

  Serial.print("Saved calibrated model: ");
  Serial.println(model.valid ? "YES" : "NO");

  if (model.valid) {
    Serial.print("Calibration points: ");
    Serial.println(model.calibrationCount);

    Serial.print("Validation points: ");
    Serial.println(model.validationCount);

    Serial.print("MAP calibration range: ");
    Serial.print(model.calibrationMinMAP, 1);
    Serial.print(" to ");
    Serial.print(model.calibrationMaxMAP, 1);
    Serial.println(" mmHg");

    Serial.print("Ridge lambda: ");
    Serial.println(model.lambda, 3);

    Serial.print("LOOCV MAE: ");
    Serial.print(model.cvMAE, 2);
    Serial.println(" mmHg");

    Serial.print("Validation MAE: ");
    Serial.print(model.validationMAE, 2);
    Serial.println(" mmHg");

    Serial.print("Model readiness: ");
    Serial.print(model.readiness, 0);
    Serial.println("%");
  } else {
    Serial.print("MAP output before calibration: ");
    Serial.print(MAP_PRIOR, 1);
    Serial.println(" mmHg [PRIOR]");
  }

  Serial.println("========================================");
}

void printHelp() {
  Serial.println();
  Serial.println("Commands:");
  Serial.println("  START      -> full guided calibration + validation");
  Serial.println("  CALIBRATE  -> same as START");
  Serial.println("  MEASURE    -> guided multi-window MAP measurement");
  Serial.println("  RETUNE     -> auto-tune RED / IR / GREEN LEDs");
  Serial.println("  PLOT       -> PPG waveform graph for Arduino Serial Plotter");
  Serial.println("  CALPLOT    -> reference-vs-predicted MAP graph");
  Serial.println("  STATUS     -> print saved model and quality information");
  Serial.println("  CLEAR      -> delete saved calibration model");
  Serial.println("  HELP       -> show this menu");
  Serial.println();
}

// ============================================================================
// FUTURE ACTIVE-PRESSURE INTERFACE
// ============================================================================

void onPressureOpticalSample(const PressureOpticalSample &s) {
  // Future cuff/bladder mode:
  //   1. timestamp cuff pressure and optical samples together
  //   2. validate controlled deflation
  //   3. identify optical reperfusion / signal-return endpoint
  //   4. compare opening/reperfusion pressure with reference MAP
  //
  // This function is intentionally a hook so the current MAX30101 pipeline
  // does not have to be restructured when pressure hardware is added.
  (void)s;
}

// ============================================================================
// SETUP / COMMAND LOOP
// ============================================================================

void setup() {
  Serial.begin(115200);
  delay(1200);

  Serial.println();
  Serial.println("MAX30101 + XIAO ESP32-S3 GUIDED MAP PROTOTYPE");
  Serial.println("==============================================");

  Wire.begin(PPG_SDA, PPG_SCL);
  Wire.setClock(100000);

  if (!ppg.begin(Wire, I2C_SPEED_STANDARD, MAX30101_I2C_ADDRESS)) {
    Serial.println("ERROR: MAX30101 not found at I2C address 0x57.");
    Serial.println("Expected wiring:");
    Serial.println("  VCC -> 3V3");
    Serial.println("  GND -> GND");
    Serial.println("  SDA -> D4 / GPIO5");
    Serial.println("  SCL -> D5 / GPIO6");

    while (true) delay(1000);
  }

  Serial.println("MAX30101 FOUND.");

  ppg.setup(
      LED_START_CODE,
      SAMPLE_AVERAGE,
      3,                 // RED + IR + GREEN
      SAMPLE_RATE_HZ,
      PULSE_WIDTH_US,
      ADC_RANGE);

  bool loaded = loadModelFromFlash();

  if (loaded) {
    Serial.println("Saved calibration model loaded from flash.");
    Serial.print("Saved readiness = ");
    Serial.print(model.readiness, 0);
    Serial.println("%");

    led.red = model.tunedRed;
    led.ir = model.tunedIR;
    led.green = model.tunedGreen;
    applyLedCurrents();
  } else {
    Serial.println("No compatible saved calibration model found.");
    Serial.println("Type START to run the complete guided calibration.");
  }

  printHelp();
}

void loop() {
  if (!Serial.available()) {
    delay(10);
    return;
  }

  String command = Serial.readStringUntil('\n');
  command.trim();
  command.toUpperCase();

  if (command.length() == 0) {
    return;
  }

  if (command == "START" ||
      command == "CALIBRATE") {

    runGuidedCalibration();

  } else if (command == "MEASURE") {

    runGuidedMeasurement();

  } else if (command == "RETUNE") {

    waitForEnter("Place sensor securely, then press ENTER to auto-tune.");
    autoTuneLEDs(true);

  } else if (command == "PLOT") {

    plotPPG();

  } else if (command == "CALPLOT") {

    plotCalibrationGraph();

  } else if (command == "STATUS") {

    printStatus();

  } else if (command == "CLEAR") {

    Serial.println("Type YES to permanently delete the saved calibration model.");

    String confirm = waitForLine();
    confirm.toUpperCase();

    if (confirm == "YES") {
      clearModelFromFlash();
    } else {
      Serial.println("CLEAR cancelled.");
    }

  } else if (command == "HELP") {

    printHelp();

  } else {

    Serial.println("Unknown command. Type HELP.");
  }
}
