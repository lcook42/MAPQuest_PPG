/* Experimental optical baseline tracker. No reference MAP input,
   no absolute or relative pressure estimate, no mmHg conversion.
   Optical pipeline derived from the user's Guided MAP sketch. */
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



static const int WINDOWS_PER_POINT = 3;
static const int MAX_WINDOW_RETRIES = 6;

static const double MIN_WINDOW_QUALITY = 65.0;
static const double MAX_ACCEPTABLE_DC_DRIFT = 0.08; // 8%


// HR sanity. HR is only used as a window sanity check if confidence is high.
static const double HR_HARD_MIN = 35.0;
static const double HR_HARD_MAX = 200.0;
static const double HR_SOFT_LOW = 45.0;
static const double HR_SOFT_HIGH = 160.0;
static const double HR_CONFIDENCE_FOR_SANITY = 0.55;

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

// GLOBAL STATE
// ============================================================================

LedState led = {LED_START_CODE, LED_START_CODE, LED_START_CODE};
TripleStats darkStats = {};



uint32_t redBuf[MAX_SAMPLES];
uint32_t irBuf[MAX_SAMPLES];
uint32_t greenBuf[MAX_SAMPLES];

float redFilt[MAX_SAMPLES];
float irFilt[MAX_SAMPLES];
float greenFilt[MAX_SAMPLES];

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
      Serial.println("Three failed windows. LED settings stay fixed for baseline comparability.");
      Serial.println("If placement needs adjustment, use START to establish a new baseline.");
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


// Baseline distances are dimensionless optical differences, not MAP.
static const int REL_DIM = 5;
static const int BASELINE_WINDOWS = 6;
static const uint32_t BASELINE_MAGIC = 0x52454C31;
struct OpticalBaseline {
  uint32_t magic;
  uint32_t version;
  double mean[REL_DIM];
  double scale[REL_DIM];
  uint8_t red, ir, green, count;
  uint32_t checksum;
};
OpticalBaseline baseline = {};
bool baselineValid = false;

uint32_t baselineChecksum(const OpticalBaseline &b) {
  const uint8_t *bytes = reinterpret_cast<const uint8_t *>(&b);
  uint32_t hash = 2166136261UL;
  for (size_t i = 0; i < offsetof(OpticalBaseline, checksum); ++i)
    hash = (hash ^ bytes[i]) * 16777619UL;
  return hash;
}

bool validateBaseline(const OpticalBaseline &b) {
  if (b.magic != BASELINE_MAGIC || b.version != 1 ||
      b.count != BASELINE_WINDOWS || b.checksum != baselineChecksum(b) ||
      !b.red || !b.ir || !b.green || b.green > GREEN_MAX_CODE) return false;
  for (int j = 0; j < REL_DIM; ++j)
    if (!isfinite(b.mean[j]) || !isfinite(b.scale[j]) || b.scale[j] < 0.05)
      return false;
  return true;
}

void clearBaseline() {
  baselineValid = false;
  memset(&baseline, 0, sizeof(baseline));
  if (preferences.begin("optrelative", false)) {
    preferences.remove("baseline");
    preferences.end();
  }
  Serial.println("Optical baseline cleared. Use START before MEASURE.");
}

bool loadBaseline() {
  OpticalBaseline loaded = {};
  if (!preferences.begin("optrelative", true)) return false;
  size_t len = preferences.getBytesLength("baseline");
  size_t got = len == sizeof(loaded)
    ? preferences.getBytes("baseline", &loaded, sizeof(loaded)) : 0;
  preferences.end();
  if (got != sizeof(loaded) || !validateBaseline(loaded)) return false;
  baseline = loaded;
  baselineValid = true;
  led = {baseline.red, baseline.ir, baseline.green};
  applyLedCurrents();
  return true;
}

bool relativeVector(const Features &f, double x[REL_DIM]) {
  if (!f.valid || f.redDC <= 1 || f.irDC <= 1 || f.greenDC <= 1 ||
      !led.red || !led.ir || !led.green || f.redPPI < 0 || f.irPPI < 0)
    return false;
  // DC normalized by LED code is only an approximate optical normalization.
  x[0] = log(f.redDC / led.red);
  x[1] = log(f.irDC / led.ir);
  x[2] = log(f.greenDC / led.green);
  x[3] = log1p(1000.0 * f.redPPI);
  x[4] = log1p(1000.0 * f.irPPI);
  for (int j = 0; j < REL_DIM; ++j) if (!isfinite(x[j])) return false;
  return true;
}

double opticalDistance(const double x[REL_DIM]) {
  double sum = 0;
  for (int j = 0; j < REL_DIM; ++j) {
    double z = (x[j] - baseline.mean[j]) / baseline.scale[j];
    sum += z * z;
  }
  return sqrt(sum / REL_DIM);
}

void startBaseline() {
  Serial.println("EXPERIMENTAL / RELATIVE / UNCALIBRATED: optical baseline only.");
  Serial.println("No reference pressure will be requested. No pressure can be inferred.");
  waitForEnter("Rest, use consistent contact at one site, then press ENTER.");
  clearBaseline();
  if (!autoTuneLEDs(false)) {
    Serial.println("START failed: readjust and retry START.");
    return;
  }
  double samples[BASELINE_WINDOWS][REL_DIM];
  for (int i = 0; i < BASELINE_WINDOWS; ++i) {
    Serial.print("BASELINE accepted window required: ");
    Serial.print(i + 1); Serial.println("/6");
    Features f = {};
    if (!getGoodWindow(f) || !relativeVector(f, samples[i])) {
      Serial.println("Baseline incomplete. Readjust and retry START.");
      return;
    }
  }
  OpticalBaseline candidate = {};
  candidate.magic = BASELINE_MAGIC;
  candidate.version = 1;
  candidate.red = led.red; candidate.ir = led.ir; candidate.green = led.green;
  candidate.count = BASELINE_WINDOWS;
  for (int j = 0; j < REL_DIM; ++j) {
    for (int i = 0; i < BASELINE_WINDOWS; ++i)
      candidate.mean[j] += samples[i][j] / BASELINE_WINDOWS;
    double ss = 0;
    for (int i = 0; i < BASELINE_WINDOWS; ++i) {
      double d = samples[i][j] - candidate.mean[j]; ss += d * d;
    }
    double sd = sqrt(ss / (BASELINE_WINDOWS - 1));
    // Prevent small baseline noise from creating arbitrarily large distances.
    candidate.scale[j] = max(j < 3 ? 0.05 : 0.10, sd);
    if (sd > 0.25) {
      Serial.println("Baseline varies too much (heuristic). Keep still and retry START.");
      return;
    }
  }
  candidate.checksum = baselineChecksum(candidate);
  if (!validateBaseline(candidate)) return;
  baseline = candidate;
  baselineValid = true;
  bool saved = false;
  if (preferences.begin("optrelative", false)) {
    saved = preferences.putBytes("baseline", &baseline, sizeof(baseline)) == sizeof(baseline);
    preferences.end();
  }
  Serial.println(saved ? "Baseline saved to flash." : "WARNING: baseline available only until restart.");
  Serial.println("Ready for MEASURE. Keep site, contact and LED settings consistent.");
}

void measureRelative() {
  Serial.println("EXPERIMENTAL RELATIVE OPTICAL MEASUREMENT; absolute MAP unavailable.");
  if (!baselineValid) {
    Serial.println("COUNT THIS RELATIVE MEASUREMENT: NO. Run START first."); return;
  }
  waitForEnter("Use the baseline site and contact, keep still, then press ENTER.");
  led = {baseline.red, baseline.ir, baseline.green};
  applyLedCurrents();
  measureDarkBaseline();
  double distances[3];
  double vectors[3][REL_DIM];
  for (int i = 0; i < 3; ++i) {
    Features f = {};
    if (!getGoodWindow(f) || !relativeVector(f, vectors[i])) {
      Serial.println("COUNT THIS RELATIVE MEASUREMENT: NO. Acquisition/quality failed.");
      return;
    }
    distances[i] = opticalDistance(vectors[i]);
    if (!isfinite(distances[i])) {
      Serial.println("COUNT THIS RELATIVE MEASUREMENT: NO. Nonfinite feature distance.");
      return;
    }
  }
  double low = min(distances[0], min(distances[1], distances[2]));
  double high = max(distances[0], max(distances[1], distances[2]));
  double distance = median3(distances[0], distances[1], distances[2]);
  bool count = high - low <= 2.0; // Experimental repeatability rule only.
  Serial.print("RelativeOpticalDistance:"); Serial.println(distance, 4);
  Serial.print("WindowDistanceSpread:"); Serial.println(high - low, 4);
  const char *labels[REL_DIM] = {"RedDC_Z", "IRDC_Z", "GreenDC_Z", "RedPPI_Z", "IRPPI_Z"};
  for (int j = 0; j < REL_DIM; ++j) {
    double v = median3(vectors[0][j], vectors[1][j], vectors[2][j]);
    Serial.print(labels[j]); Serial.print(':');
    Serial.println((v - baseline.mean[j]) / baseline.scale[j], 4);
  }
  Serial.print("COUNT THIS RELATIVE MEASUREMENT: "); Serial.println(count ? "YES" : "NO");
  Serial.println("Distance has no pressure units, direction or validated clinical meaning.");
  if (distance > 3.0) Serial.println("Large optical change: check site/contact and repeat. Not a pressure alarm.");
  if (!count) Serial.println("Readjust and repeat; use START if the baseline placement changed.");
}

void plotRelativePPG() {
  waitForEnter("Keep still, then press ENTER to capture normalized PPG.");
  if (!baselineValid && !autoTuneLEDs(true)) return;
  int n = 0;
  if (!acquireWindow(n)) return;
  Features f = computeFeatures(n);
  Serial.println("Open Serial Plotter now; streaming starts in 5 seconds.");
  delay(5000);
  for (int i = SAMPLE_RATE_HZ; i < n; ++i) {
    Serial.print("RedPPG:"); Serial.print(1000.0 * redFilt[i] / max(1.0, f.redDC), 4);
    Serial.print("\tIRPPG:"); Serial.print(1000.0 * irFilt[i] / max(1.0, f.irDC), 4);
    Serial.print("\tGreenPPG:"); Serial.println(1000.0 * greenFilt[i] / max(1.0, f.greenDC), 4);
    delay(10);
  }
  Serial.println("PLOT_COMPLETE (waveform visualization, not an accepted measurement)");
}

// Continuous visualization only: no model prediction or baseline changes.
void plotContinuousPPG() {
  Serial.println("CONTINUOUS PPG VIEW (visualization only)");
  Serial.println("Keep the sensor still. Uses current LED settings; no automatic retune.");
  Serial.println("Send STOP followed by ENTER to return to the command menu.");
  Serial.println("Open Serial Plotter at 115200 now. Streaming starts in 5 seconds.");
  delay(5000);
  applyLedCurrents();
  ppg.clearFIFO();
  const float dt = 1.0f / SAMPLE_RATE_HZ;
  const float rcHP = 1.0f / (2.0f * PI * 0.35f);
  const float hpAlpha = rcHP / (rcHP + dt);
  const float rcLP = 1.0f / (2.0f * PI * 5.0f);
  const float lpAlpha = dt / (rcLP + dt);
  float prev[3] = {}, hp[3] = {}, lp[3] = {}, dc[3] = {};
  int warmup = 0;
  bool initialized = false;
  char command[16] = {};
  size_t commandLength = 0;
  bool commandOverflow = false;
  while (true) {
    while (Serial.available()) {
      char ch = (char)Serial.read();
      if (ch == '\n') {
        command[commandLength] = '\0';
        String input(command); input.trim(); input.toUpperCase();
        if (!commandOverflow && input == "STOP") {
          Serial.println("LIVEPLOT_COMPLETE");
          return;
        }
        commandLength = 0; commandOverflow = false;
      } else if (ch != '\r') {
        if (commandLength < sizeof(command) - 1) command[commandLength++] = ch;
        else commandOverflow = true;
      }
    }
    uint32_t r, ir, g;
    if (!readOneSample(r, ir, g, 100)) {
      // Stop polling stays responsive even if the sensor stops producing data.
      continue;
    }
    const uint32_t raw[3] = {r, ir, g};
    for (int c = 0; c < 3; ++c) {
      float value = (float)raw[c];
      if (!initialized) { prev[c] = value; dc[c] = value; }
      hp[c] = hpAlpha * (hp[c] + value - prev[c]);
      lp[c] += lpAlpha * (hp[c] - lp[c]);
      dc[c] += 0.01f * (value - dc[c]);
      prev[c] = value;
    }
    initialized = true;
    if (warmup < SAMPLE_RATE_HZ) { ++warmup; continue; }
    Serial.print("RedPPG:"); Serial.print(1000.0f * lp[0] / max(1.0f, dc[0]), 4);
    Serial.print("\tIRPPG:"); Serial.print(1000.0f * lp[1] / max(1.0f, dc[1]), 4);
    Serial.print("\tGreenPPG:"); Serial.println(1000.0f * lp[2] / max(1.0f, dc[2]), 4);
  }
}

void relativeHelp() {
  Serial.println("START / BASELINE / CALIBRATE: tune and capture 6 baseline windows (no pressure inputs)");
  Serial.println("MEASURE: 3 accepted windows -> relative optical distances only");
  Serial.println("PLOT: captured PPG; LIVEPLOT / CONTINUOUS: live PPG until STOP + ENTER");
  Serial.println("STATUS: baseline; CLEAR: erase baseline");
  Serial.println("RETUNE: erase baseline and retune; START required afterward; HELP: commands");
}

void setup() {
  Serial.begin(115200); delay(1200);
  Serial.println("MAX30101 NO-REFERENCE EXPERIMENTAL / RELATIVE / UNCALIBRATED");
  Wire.begin(PPG_SDA, PPG_SCL); Wire.setClock(100000);
  if (!ppg.begin(Wire, I2C_SPEED_STANDARD, MAX30101_I2C_ADDRESS)) {
    Serial.println("Sensor not found at 0x57. Check 3V3/GND/SDA GPIO5/SCL GPIO6.");
    while (true) delay(1000);
  }
  ppg.setup(LED_START_CODE, SAMPLE_AVERAGE, 3, SAMPLE_RATE_HZ, PULSE_WIDTH_US, ADC_RANGE);
  Serial.println(loadBaseline() ? "Saved optical baseline loaded; verify same site/contact." : "No baseline. Type START.");
  relativeHelp();
}

void loop() {
  if (!Serial.available()) { delay(10); return; }
  String cmd = Serial.readStringUntil('\n'); cmd.trim(); cmd.toUpperCase();
  if (!cmd.length()) return;
  if (cmd == "START" || cmd == "BASELINE" || cmd == "CALIBRATE") startBaseline();
  else if (cmd == "MEASURE") measureRelative();
  else if (cmd == "PLOT") plotRelativePPG();
  else if (cmd == "LIVEPLOT" || cmd == "CONTINUOUS") plotContinuousPPG();
  else if (cmd == "STATUS") {
    Serial.println("EXPERIMENTAL RELATIVE OPTICAL BASELINE; absolute MAP unavailable.");
    Serial.println(baselineValid ? "Baseline ready (6 windows). No clinical validation." : "No baseline. Run START.");
    Serial.print("LED R/IR/G: "); Serial.print(led.red); Serial.print('/');
    Serial.print(led.ir); Serial.print('/'); Serial.println(led.green);
  } else if (cmd == "RETUNE") {
    waitForEnter("RETUNE clears the baseline. Place sensor, then press ENTER.");
    clearBaseline(); autoTuneLEDs(true);
  } else if (cmd == "CLEAR") {
    Serial.println("Type YES to delete the optical baseline.");
    String confirm = waitForLine(); confirm.toUpperCase();
    if (confirm == "YES") clearBaseline(); else Serial.println("CLEAR cancelled.");
  } else if (cmd == "CALPLOT") {
    Serial.println("No reference-vs-prediction graph exists in this mode. Use PLOT.");
  } else relativeHelp();
}
