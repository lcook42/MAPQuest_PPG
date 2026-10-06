# MAPQuest PPG Prototype: Firmware, Protocol and Analysis Spec

**Status:** low-fidelity bench prototype
**Last updated:** 2026-10-05
**Place this file at:** `docs/SPEC.md`

---

## 1. What we're building

A logger and analysis pipeline that detects when blood returns to the forearm as an upper-arm cuff deflates.

**Measurement principle (return-to-flow, PPG volume detection)**
1. Cuff inflates above arterial pressure → forearm blood volume frozen → PPG DC flat.
2. Cuff deflates (manual bulb valve, ~2 mmHg/s).
3. Cuff drops below MAP but stays above venous pressure → blood flows in, can't drain → forearm fills → reflected light falls → ΔOD rises.
4. **MAP = cuff pressure at the onset of sustained filling.**

**Important for interpreting bench data:** healthy teammates have a pulse, so on them onset reads near **systolic**, not MAP. Bench tests validate detection and the signal chain. MAP accuracy needs a non-pulsatile phantom or LVAD patients.

---

## 2. Hardware

### 2.1 Parts

| Part | Spec | Notes |
|---|---|---|
| Arduino Nano ESP32 | ESP32-S3, 3.3V logic, native USB | GPIO max 3.3V. No 5V pin (VBUS instead) |
| SparkFun MAX30101 breakout (SEN-16474) | Red 660 nm, IR 880 nm, green 537 nm; 18-bit ADC; I²C 0x57 | Power from 3.3V. Onboard 2.2 kΩ pull-ups + level shifting to 3.3V |
| MPS20N0040D + HX710B module | 0–40 kPa (0–300 mmHg) rated; 24-bit ADC | Some modules saturate at 15–20 kPa (~110–150 mmHg). Verify range |
| Manual sphygmomanometer + 1/8″ barbed tee + ~2.5 mm ID silicone tube | | Sensor teed between bulb and gauge |
| Velcro strap | | PPG on ventral forearm, 5–10 cm below cuff edge; snug, not tight |

### 2.2 Wiring

| Nano ESP32 | MAX30101 | | Nano ESP32 | HX710B |
|---|---|---|---|---|
| 3V3 | 3V3 | | 3V3 | VCC |
| GND | GND | | GND | GND |
| A4 (SDA / GPIO11) | SDA | | D3 | OUT (DOUT) |
| A5 (SCL / GPIO12) | SCL | | D2 | SCK |

**Why HX710B on 3V3:** at 5V, its DOUT output would drive 5V into an ESP32 GPIO. At 3.3V the bridge excitation is lower, so the pressure span per count is smaller. Calibrate at 3.3V; don't reuse constants measured at 5V.

---

## 3. Firmware requirements

### 3.1 MAX30101 configuration

```cpp
// ledBrightness, sampleAverage, ledMode (2 = Red+IR), sampleRate, pulseWidth, adcRange
particleSensor.setup(0x1F, 4, 2, 400, 411, 4096);
// → Red + IR, 18-bit, 15.63 pA/count, 400 sps averaged ×4 = 100 Hz output
```

- I²C at 400 kHz (`Wire.setClock(400000)`), default pins A4/A5.
- Tune IR amplitude so **IR DC sits at 105k–157k of 262,143 counts** (40–60% of full scale). Leaves headroom both ways as blood volume changes.
- Settings live in `config.h` and are echoed in the metadata header.
- FIFO is 32 samples deep: at 100 Hz, drain it at least every ~300 ms. Poll every loop.
- Wi-Fi and BT off.

### 3.2 HX710B reading

- Bit-bang: wait for DOUT LOW (conversion ready), clock 24 bits MSB first, then extra pulses to select mode. **27 total pulses = differential input at 40 Hz** (verify against the HX710B datasheet; 25 pulses = 10 Hz).
- Sign-extend the 24-bit two's-complement value.
- **SCK must not stay HIGH > 60 µs** or the HX710B powers down. Protect the 24-bit read with a short critical section so an interrupt can't stretch a pulse.
- Use symbolic pins `D2` (SCK) and `D3` (DOUT).

### 3.3 Timing

- **Sample index:** increment once per MAX30101 FIFO sample. Time = index × 10 ms. This is the master clock.
- Also record `micros()` at FIFO read for jitter diagnostics.
- Pressure: attach the most recent HX710B value (40 Hz) to each PPG sample; also log the HX710B's own sample counter so the host can resample properly.

### 3.4 Serial protocol (USB CDC, 115200)

**Device → host**

```
# key=value                      metadata lines, start with '#'
S,<idx>,<t_us>,<ir>,<red>,<p_idx>,<p_raw>   data line, one per PPG sample
E,<message>                      error / warning
```

**Host → device** (single-line commands, newline terminated)

| Command | Action |
|---|---|
| `START` | Begin streaming `S` lines (resets sample index) |
| `STOP` | Stop streaming |
| `DARK` | Set LED amplitudes to 0, average 200 samples, report `# dark_ir=… dark_red=…`, restore amplitudes |
| `IRAMP <0-255>` | Set IR LED amplitude (only between recordings) |
| `REDAMP <0-255>` | Set red LED amplitude |
| `INFO` | Print all settings as `#` metadata lines |

### 3.5 Firmware acceptance criteria

- [ ] I²C scan finds 0x57
- [ ] Output rate 100 Hz ± 1% (measured from `t_us`)
- [ ] **Zero dropped samples** over 5 minutes (sample index continuous)
- [ ] IR DC on a forearm adjustable into 105k–157k counts
- [ ] Pressure stream updates at ~40 Hz with no stale-value gaps > 50 ms
- [ ] `DARK` returns values ≤ ~128 counts

---

## 4. Host logger (`host/logger.py`)

- `python logger.py --port <PORT> --subject T01 --trial 3 --out data/`
- Sends `INFO`, `DARK`, then `START`. Writes every line to `data/<date>_<subject>_<trial>.csv`.
- File header (as `#` lines): date/time, subject ID, trial, all device settings, dark offsets, pressure calibration constants, firmware version, free-text notes (`--note`).
- Ctrl-C sends `STOP` and closes the file cleanly.
- Prints a live line every second: sample rate, IR DC, current cuff pressure (mmHg).

### 4.1 Pressure calibration (`host/logger.py --calibrate`)

Two-point:
1. Cuff open to air → record mean `p_raw` over 3 s → `RAW0`.
2. Pump to a gauge reading (e.g., 150 mmHg), hold → record mean → `RAW_REF`, enter `P_REF`.
3. Save to `host/calibration.json`; logger writes it into every CSV header.

`p_mmHg = (p_raw − RAW0) × P_REF / (RAW_REF − RAW0)`

Also sweep 0 → max in ~20 mmHg steps once to check linearity and find the saturation point.

---

## 5. Analysis pipeline (`host/analyze.py`)

### 5.1 Steps and parameters

| # | Step | Method | Parameters |
|---|---|---|---|
| 1 | Load | Parse `#` header + `S` lines | — |
| 2 | Pressure | Calibrate, resample to PPG timebase | Linear interpolation on HX710B sample counter |
| 3 | Dark subtract | `counts − dark` per channel | From `DARK` |
| 4 | Reference window | User-set or auto: last 3 s before deflation starts | `--t-occ0 --t-occ1` |
| 5 | ΔOD | `ΔOD = ln(counts_ref / counts(t))` per channel | counts_ref = mean over reference window |
| 6 | Blood volume | Two-wavelength: `ΔV·L [µm] = 229·ΔOD₆₆₀ + 1555·ΔOD₈₈₀`. IR-only fallback: `ΔOD₈₈₀ / 6.13 cm⁻¹` | Flag `--ir-only` |
| 7 | Low-pass | Butterworth, zero-phase (`filtfilt`) | 2nd order, 0.3 Hz |
| 8 | Derivative | Savitzky–Golay derivative, centered | Window 2.0 s (201 samples), polyorder 2 |
| 9 | Noise floor | μ₀, σ₀ of dV/dt over reference window | — |
| 10 | Onset | First sample after deflation start where dV/dt > μ₀ + 3σ₀ for ≥ 2 s | `--k 3 --hold 2.0` |
| 11 | Result | Cuff pressure at first sample of the run | — |
| 12 | Pulsatility flag | Band-pass ΔOD₈₈₀ at 0.5–5 Hz during baseline; report RMS | Informational |

**Why a 2 s derivative window:** the HeartMate 3 artificial pulse repeats every 2 s; a window of exactly one period cancels it and its harmonics. Keeps the same pipeline valid on LVAD data.

### 5.2 Outputs
- Console: onset time, onset pressure, μ₀, σ₀, pulsatility RMS, any warnings.
- Figure (PNG, saved next to the CSV), 4 stacked panels sharing a time axis:
  1. Raw IR and red counts
  2. ΔV·L (µm)
  3. dV/dt with μ₀ + 3σ₀ threshold line and onset marker
  4. Cuff pressure with onset pressure marked
- Summary row appended to `data/results.csv`.

### 5.3 Real-time version (later milestone)
Causal filters add delay: 2nd-order Butterworth at 0.3 Hz ≈ 0.75 s group delay; causal 2 s derivative ≈ 1 s. Total ≈ 1.75 s → ~3.5 mmHg low at 2 mmHg/s. Subtract the known delay from the onset timestamp before reading cuff pressure.

---

## 6. Tests (`tests/test_pipeline.py`, data from `host/synth.py`)

Synthetic signal generator produces IR + red counts and cuff pressure:
- Cuff: hold 150 mmHg for 10 s, then linear deflation at 2 mmHg/s
- Volume: flat until cuff < MAP_true, then filling ramp (configurable slope)
- Add: Gaussian noise, optional 1.2 Hz cardiac component, optional 0.5 Hz artificial-pulse component (HM3-like), optional slow drift
- Optional desaturation of trapped blood (98% → 70% over occlusion) to exercise the two-wavelength solve

**Required tests**
- [ ] Onset detected within **±1 mmHg** of MAP_true at clean SNR
- [ ] Still within ±2 mmHg with cardiac + artificial pulse components
- [ ] No false onset on a recording with no filling
- [ ] Desaturation with constant volume → two-wavelength ΔV stays ~0; IR-only drifts
- [ ] ΔOD of a 1% count drop ≈ 0.010

---

## 7. Milestones

| # | Goal | Done when |
|---|---|---|
| M0 | Toolchain | Blink uploads; I²C scan shows 0x57 |
| M1 | PPG streaming | 100 Hz, zero dropped samples over 5 min (§3.5) |
| M2 | Pressure | HX710B streaming; two-point calibration; linearity sweep |
| M3 | Logger | CSV with full metadata header; live status line |
| M4 | Analysis | `analyze.py` produces plots + onset pressure on recorded data |
| M5 | Tests | All §6 tests pass |
| M6 | Bench trials | 3 trials × 3 teammates, onset repeatable within ±5 mmHg per person |
| M7 (later) | Real-time | On-device pipeline with delay compensation; optional BLE |

---

## 8. Bench trial protocol (teammates)

**Safety**
- Occlusion ≤ 2 min per trial; rest 2–3 min between trials
- Stop immediately for pain, numbness or tingling
- Exclude anyone on anticoagulants, with vascular disease or fragile skin
- Don't exceed the pressure sensor's verified linear range
- Bench experiment only; not a medical device

**Procedure**
1. Reference BP on the other arm (automated monitor).
2. Seated, arm on table at heart level; PPG on ventral forearm 5–10 cm below cuff.
3. Run logger (`DARK` happens automatically). 30 s baseline, cuff off.
4. Inflate quickly to ~30 mmHg above the subject's systolic.
5. Hold 10 s (reference window).
6. Deflate ~2 mmHg/s with the bulb valve; the sensor logs the actual rate.
7. Release fully at ~20 mmHg. Stop logger.
8. Repeat ×3.

---

## 9. Known pitfalls

| Issue | Symptom | Fix |
|---|---|---|
| Pin-numbering mode | Code drives wrong pins | Symbolic names only (`D2`, `A4`) |
| MAX30101 not found | `begin()` fails | Check SDA/SCL swap, 3V3 power, I²C address 0x57 |
| Upload fails | Port disappears | Double-tap reset to enter bootloader, re-select port |
| FIFO overflow | Gaps in sample index | Poll faster; never block in `loop()` |
| HX710B powers down | Stale pressure | Keep SCK HIGH < 60 µs; critical section around read |
| HX710B saturates | Pressure flattens near 110–150 mmHg | Find limit in linearity sweep; stay below it |
| Strap too tight | Blanched skin, weak/odd PPG | Snug only |
| LED settings changed mid-run | ΔOD jumps | Lock settings during `START`…`STOP` |
| Teammate reads near systolic | "MAP" too high | Expected: pulsatile subject (§1) |

---

## 10. References

- MAX30101 datasheet: https://www.analog.com/media/en/technical-documentation/data-sheets/max30101.pdf
- SparkFun MAX30101 hookup guide: https://learn.sparkfun.com/tutorials/sparkfun-photodetector-max30101-hookup-guide/hardware-overview
- Arduino Nano ESP32 cheat sheet (pins, power): https://docs.arduino.cc/tutorials/nano-esp32/cheat-sheet/
- Nano ESP32 pin numbering: https://support.arduino.cc/hc/en-us/articles/10483225565980-Select-pin-numbering-for-Nano-ESP32-in-Arduino-IDE
- HX710B Arduino library (reference implementation): https://github.com/rppelayo/hx710B_pressure_sensor
- Hemoglobin extinction coefficients (Prahl, OMLC): https://omlc.org/spectra/hemoglobin/summary.html
- Modified Beer–Lambert law primer: https://fnirs.org/courses/introduction-to-fnirs/lessons/modified-beer-lambert-law-mbll/
- MAX30101 Counts → Blood Volume (project doc): https://claude.ai/code/artifact/f3555569-0a4e-4b68-8951-014d3b132c36