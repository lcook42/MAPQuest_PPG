# MAPQuest

PPG prototype: measure pulsatile blood volume change with a MAX30101, stream raw samples over USB
to a host, and do all the signal processing in Python.

The firmware is deliberately dumb — it configures the sensor and prints raw ADC counts. Every
interpretive step happens in Python, where each stage of the derivation is retained and plotted:

**raw → DC baseline → AC → normalised AC/DC → band-passed → dV/dt → d²V/dt²**

![Example output](docs/example-output.png)

## Hardware

| | |
|---|---|
| MCU | Arduino Nano ESP32 (official, ESP32-S3) |
| Sensor | SparkFun SEN-16474 — Photodetector Breakout MAX30101 (Qwiic) |

### Wiring

| Breakout | Nano ESP32 |
|---|---|
| `3.3V` | `3V3` — **not 5 V** |
| `GND` | `GND` |
| `SDA` | `A4` |
| `SCL` | `A5` |
| `INT` | not connected |

Both sides are 3.3 V logic, so no level shifting. The breakout's onboard 2.2 kΩ pullups are the
only ones on the bus.

## Quick start

### 1. Flash the firmware

Open `firmware/mapquest_ppg/mapquest_ppg.ino` in Arduino IDE 2.x, select **Arduino Nano ESP32**,
and upload. **No libraries to install** — the MAX30101 driver is inline in the sketch.

Verify it works from the IDE's Serial Monitor at 921600 baud. You should see:

```
# ready — send 's' to start streaming, '?' for config
```

Send `s` to start streaming. If you instead see an `# ERROR:` line, the wiring is wrong.

### 2. Set up Python

```bash
pip install -r python/requirements.txt
```

### 3. Try it without hardware

Verify the processing half works before trusting a real recording:

```bash
cd python && python simulate.py && python analyze.py ../data/sim_*.csv
```

### 4. Place the sensor and tune

```bash
cd python && python live_plot.py
```

Watch the live scope and adjust until:

- **DC level sits roughly 30–60 % of full scale** (≈ 80 000–160 000 counts). Too low wastes ADC
  range; too high clips. Tune with `g<n>` (green) and `i<n>` (IR) over a serial monitor, where
  `n` is 0–255 in 0.2 mA steps.
- **The band-passed panel shows a clean, repeating pulse.**

Secure the sensor to the fingertip with a rubber band — holding it by hand varies contact pressure
and swamps the signal. Shield it from overhead light.

### 5. Record and analyse

```bash
cd python
python record.py --seconds 60 --label rest
python analyze.py ../data/ppg_<timestamp>_rest.csv
```

`analyze.py` prints per-channel quality metrics and renders all seven derivation stages.

## Layout

```
firmware/mapquest_ppg/   Arduino sketch — self-contained MAX30101 driver + CSV streamer
python/
  mapquest/
    serial_io.py         port discovery, header parsing, CSV load
    pipeline.py          the derivation chain (raw → d²V/dt²) + quality metrics
  record.py              stream to timestamped CSV
  live_plot.py           live scope of every stage
  analyze.py             offline analysis and the stage plot
  simulate.py            synthetic recordings for testing without hardware
data/                    recordings (gitignored)
docs/
  design.md              architecture, sensor config, wiring, open questions
  led-selection-report.md  why Green + IR, with sources
```

## Current configuration

Green (SLOT1) + IR (SLOT2) in Multi-LED mode, 411 µs pulse width (18-bit), 400 sps internal with
2× hardware averaging → **200 Hz output per channel**.

Green is expected to give the stronger signal in reflectance mode; IR is recorded alongside it so
the choice can be settled on measured evidence rather than assumed. See
[`docs/led-selection-report.md`](docs/led-selection-report.md) for the reasoning and
[§6](docs/led-selection-report.md) for how to decide.

**The 200 Hz sample rate is provisional** and expected to change — see
[`docs/design.md` §3](docs/design.md) for the tradeoffs and the one-line change to adjust it.

## Commands (serial)

| Key | Action |
|---|---|
| `s` | start / stop streaming |
| `?` | print config header |
| `t` | one-shot die temperature |
| `g<n>` | set green LED current, `n` = 0–255 (×0.2 mA) |
| `i<n>` | set IR LED current, `n` = 0–255 |
| `r` | reset sensor and reapply config |

## Scope

This is a v0 prototype aimed at a clean, trustworthy blood-volume waveform and its derivatives.
MAP/BP estimation, SpO2, and HRV are explicitly out of scope for now.

One caveat worth keeping in view: what a reflectance PPG records is a robust *correlate* of
pulsatile blood volume, not a calibrated volume measurement — and there is evidence that both green
and IR may be tracking dermal deformation driven by transmural arterial pressure rather than blood
volume directly. See the LED selection report §4.
