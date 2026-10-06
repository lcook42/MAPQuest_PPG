# MAPQuest — Prototype Design Doc

Status: **v0 prototype.** Goal is a clean, trustworthy blood-volume waveform and its first and
second derivatives. No BP/MAP modelling yet.

## 1. Scope

Measure pulsatile blood volume change at the fingertip with a MAX30101, stream raw samples to a
host over USB serial, and do all signal processing in Python. The firmware is deliberately dumb:
**it does no filtering, no averaging beyond the sensor's own, and no beat detection.** Everything
interpretive happens in Python where it is visible and revisable.

Out of scope for v0: MAP/BP estimation, SpO2, HRV, wireless, on-device storage.

## 2. Hardware

| Item | Part | Notes |
|---|---|---|
| MCU | Arduino Nano ESP32 (official) | ESP32-S3, native USB CDC, 3.3 V logic |
| Sensor | SparkFun SEN-16474 MAX30101 Qwiic | Reflectance mode, I²C `0x57`, 2.2 kΩ onboard pullups |

### Wiring

The Nano ESP32 has no Qwiic connector, so wire the breakout's broken-out pins directly:

| MAX30101 breakout | Nano ESP32 pin | Note |
|---|---|---|
| `3.3V` | `3V3` | **Not 5 V.** The MAX30101 die runs 1.8 V; the breakout regulates from 3.3 V. |
| `GND` | `GND` | |
| `SDA` | `A4` (GPIO11) | default `Wire` SDA |
| `SCL` | `A5` (GPIO12) | default `Wire` SCL |
| `INT` | *not connected* | Firmware polls the FIFO pointers; interrupt unused in v0. |

Both boards are 3.3 V logic — no level shifting needed. The breakout's 2.2 kΩ pullups are the only
ones on the bus; leave the I²C jumper intact unless you add more devices.

**Decoupling:** the datasheet asks for ≥1 µF of bypass close to the IC on the LED supply, with
supply path resistance/inductance well under 1 Ω. The LEDs pulse hard at low duty cycle, and supply
ripple turns directly into optical noise. The SparkFun board has onboard caps; if you later see
noise correlated with LED current, add bulk capacitance at the sensor end of the cable and keep the
cable short.

**Mechanical:** secure the sensor to the fingertip with a rubber band or light tape. Holding it by
hand varies contact pressure, which modulates the DC level and swamps the AC signal. Shield from
overhead ambient light — the photodiode is very sensitive to it.

## 3. Sensor configuration

See [`led-selection-report.md`](led-selection-report.md) for the reasoning behind the channel
choice. Summary:

| Parameter | Value | Register |
|---|---|---|
| Mode | Multi-LED `0b111` | `MODE_CONFIG` 0x09 |
| SLOT1 | Green (LED3) | `MULTI_LED_CTRL1` 0x11 `[2:0]` = 3 |
| SLOT2 | IR (LED2) | `MULTI_LED_CTRL1` 0x11 `[6:4]` = 2 |
| SLOT3/4 | disabled | `MULTI_LED_CTRL2` 0x12 = 0 |
| Pulse width | 411 µs → 18-bit | `SPO2_CONFIG` 0x0A `[1:0]` = 3 |
| Internal rate | 400 sps | `SPO2_CONFIG` 0x0A `[4:2]` = 3 |
| Sample averaging | 2 | `FIFO_CONFIG` 0x08 `[7:5]` = 1 |
| **Output rate** | **200 Hz/channel** | 400 / 2 |
| ADC range | 4096 nA (15.63 pA LSB) | `SPO2_CONFIG` 0x0A `[6:5]` = 1 |
| Green current | 7.2 mA (`0x24`) | `LED3_PA` 0x0E |
| IR current | 7.2 mA (`0x24`) | `LED2_PA` 0x0D |

### ⚠️ Sample rate is provisional — expect to change it

**200 Hz is a starting point chosen to balance derivative fidelity against bandwidth. It is not a
fixed requirement.** Revisit it as the project's needs become clear. Relevant facts for when you do:

- **The 411 µs pulse width caps two-channel operation at 400 sps** (datasheet Table 11). You cannot
  exceed that without dropping to 17-bit (215 µs), which buys 800 sps.
- **Output rate = internal rate ÷ `SMP_AVE`.** The current config spends the 400 sps ceiling on
  `SMP_AVE = 2` for noise reduction rather than on raw output rate. Other points on that tradeoff:

  | Want | Internal sps | `SMP_AVE` | Output | Resolution | Tradeoff |
  |---|---|---|---|---|---|
  | Max temporal resolution | 400 | 1 | 400 Hz | 18-bit | Noisier per sample; sharper `d²V/dt²` timing |
  | **Current default** | **400** | **2** | **200 Hz** | **18-bit** | **Balanced** |
  | Max smoothness | 400 | 4 | 100 Hz | 18-bit | Marginal for a crisp 2nd derivative |
  | Single channel, fast | 400 (HR-mode-like) | 1 | 400 Hz | 18-bit | Drop IR, green only |

- **Reasons you might go up to 400 Hz:** fiducial points in `d²V/dt²` (the a–e waves) landing
  between samples; pulse transit time or pulse wave velocity work, where timing precision is the
  whole measurement; any move toward cuffless BP.
- **Reasons you might go down to 100 Hz:** long unattended recordings, or if it turns out the
  derivative chain is noise-limited rather than resolution-limited.
- **You can always decimate in Python; you can never un-decimate.** When unsure, record faster.

To change it: edit `SAMPLE_RATE_SPS` and `SAMPLE_AVERAGING` at the top of
`firmware/mapquest_ppg/mapquest_ppg.ino`. The firmware recomputes and reports the effective output
rate in its `# fs=` header line, and the Python side reads `fs` from that header rather than
assuming — so nothing downstream needs editing.

### Other parameters likely to need tuning

- **LED current.** Tune so the DC level sits comfortably mid-scale — roughly 50 000–150 000 counts
  for a fingertip — without clipping near 262 143 (2¹⁸−1). Too low and you waste ADC range; too high
  and you saturate or cook the finger. Adjustable live over serial (`g<n>` / `i<n>`), no reflash.
- **ADC range.** If you cannot get into range with LED current alone, step `ADC_RANGE_NA` up
  (8192/16384 nA) to reduce sensitivity, or down (2048 nA) to increase it.

## 4. Firmware

`firmware/mapquest_ppg/mapquest_ppg.ino` — a single self-contained sketch, **no external libraries**.

The MAX30101 driver is written inline against `Wire` rather than using the SparkFun MAX3010x
library. Reasons:

1. **No library install step** in Arduino IDE — open and upload.
2. **The SparkFun library names FIFO getters by slot position, not by LED.** `getFIFORed()` returns
   whatever is in slot 1. With green in slot 1, `getFIFORed()` returns green. That is a real
   footgun for a project whose whole point is knowing which wavelength produced which number.
3. Full control of the slot/averaging configuration the design depends on.

### Serial protocol

USB CDC, nominal 921600 baud (ignored by CDC, set anyway). ASCII CSV — at 200 Hz × 2 channels this
is ~5 kB/s, nowhere near a bottleneck, and it is trivially debuggable in a terminal.

On start and on `?`, the firmware emits a metadata header. Python parses this instead of hardcoding:

```
# mapquest-ppg v0
# part_id=0x15 rev=0x__
# fs=200.00
# channels=green,ir
# adc_range_na=4096 pulse_width_us=411 resolution_bits=18
# internal_sps=400 smp_ave=2
# led_green_ma=7.2 led_ir_ma=7.2
idx,us,green,ir
```

Then one CSV line per sample:

| Field | Meaning |
|---|---|
| `idx` | monotonic sample counter from stream start |
| `us` | `micros()` when the FIFO burst containing this sample was read |
| `green` | 18-bit count, SLOT1 |
| `ir` | 18-bit count, SLOT2 |

**Use `idx / fs` as the timebase, not `us`.** `us` is the host-read time of a FIFO burst, not the
true conversion time of the individual sample — several samples in one burst share nearly the same
`us`. `us` is included only to detect dropped samples and clock drift.

Overflow is reported out-of-band as `# OVF n` lines if the FIFO overruns (host not keeping up).

### Serial commands

| Key | Action |
|---|---|
| `s` | start / stop streaming (starts stopped) |
| `?` | reprint the config header |
| `t` | one-shot die temperature, as `# temp=NN.NN` |
| `g<n>` | set green LED current register, `n` = 0–255 (×0.2 mA) |
| `i<n>` | set IR LED current register, `n` = 0–255 |
| `r` | soft-reset the sensor and reapply config |

## 5. Python pipeline

`python/` — the derivation chain, with **every stage retained and plotted** so you can see where
signal or noise enters.

| # | Stage | Method | Why |
|---|---|---|---|
| 0 | `raw` | counts from FIFO | ground truth; check DC level and clipping |
| 1 | `dc` | 2nd-order Butterworth low-pass @ 0.4 Hz, zero-phase | the slow baseline — contact pressure, venous pooling, drift |
| 2 | `ac` | `raw − dc` | pulsatile component in raw counts |
| 3 | `norm` | `ac / dc` | normalises out LED current and skin tone; makes channels comparable and gives perfusion index |
| 4 | `filt` | Butterworth band-pass 0.5–8 Hz, zero-phase | removes residual drift and HF noise before differentiating |
| 5 | `d1` | Savitzky–Golay, `deriv=1` | **dV/dt** — velocity plethysmogram |
| 6 | `d2` | Savitzky–Golay, `deriv=2` | **d²V/dt²** — APG; a–e waves, dicrotic notch as the e-wave |

Two deliberate choices:

- **`filtfilt`, not `lfilter`.** Zero-phase filtering. A causal filter group-delays the signal, and
  the delay is frequency-dependent — which would shift fiducial points in the derivatives by an
  unknown amount. That is fatal for morphology work. (Cost: non-causal, so this is offline/buffered
  processing, not true real-time. Acceptable here.)
- **Savitzky–Golay derivatives, not `np.diff`.** Finite differencing multiplies high-frequency noise
  by ω per derivative — a second derivative via `np.diff` is usually unusable. SG fits a local
  polynomial and differentiates that analytically, giving smoothing and differentiation in one
  well-conditioned step.

**Sign convention:** raw PPG counts *rise* when blood volume rises (the MAX30101 reports reflected
light as a current, and more blood means more absorption, so this is inverted relative to
transmissive pulse oximeters in some setups). The pipeline exposes an `invert` flag per channel and
`analyze.py` reports which polarity gives the canonical systolic-upstroke shape. Check this before
trusting the sign of `dV/dt`.

### Entry points

| Script | Purpose |
|---|---|
| `python/record.py` | Stream to timestamped CSV in `data/`. Optional `--plot` for a live scope while recording. |
| `python/live_plot.py` | Live scope only, nothing saved. For sensor placement and LED current tuning. |
| `python/analyze.py` | Offline: load a CSV, run the full chain, render the stacked stage plot, print quality metrics. |

## 6. Open questions / next steps

- [ ] Confirm measurement site. This doc assumes **fingertip**. Wrist would change LED current and
      strengthen the case for green-only.
- [ ] Settle green vs IR empirically — see §6 of the LED selection report.
- [ ] Verify sign convention on first real recording.
- [ ] Decide whether 200 Hz survives contact with the derivative plots, or whether to go to 400 Hz.
- [ ] If BP/MAP becomes the goal: beat segmentation, ensemble averaging, a–e wave extraction, and a
      reference BP source for any supervised work.
