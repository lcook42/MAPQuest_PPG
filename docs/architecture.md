# MAPQuest — Architecture & Signal Pipeline

Living document. Updated as the system changes; see the status table at the bottom for what
is verified on hardware versus only on synthetic data. The reasoning behind each choice
lives in [`decisions.md`](decisions.md).

Last updated: 2026-10-06, after the first successful wrist provocation test.

---

## 1. End-to-end system

```mermaid
flowchart TD
    subgraph SUBJECT["Subject"]
        CUFF["Upper-arm cuff<br/>manual bulb, bled at ~2 mmHg/s"]
        TISSUE["Distal tissue — WRIST<br/>(SPEC says forearm; see D18)<br/>fills when cuff drops below<br/>arterial pressure"]
        CUFF -->|occludes| TISSUE
    end

    subgraph SENSORS["Sensors"]
        MAX["MAX30101 — SparkFun SEN-16474<br/>Red 660 nm + IR 880 nm<br/>reflectance, I2C 0x57, 18-bit"]
        HX["MPS20N0040D + HX710B<br/>0-300 mmHg, 24-bit<br/>NOT YET FITTED"]
    end

    TISSUE -->|"reflected light<br/>(more blood = fewer counts)"| MAX
    CUFF -.->|air pressure| HX

    subgraph FW["Firmware — Arduino Nano ESP32 (ESP32-S3)"]
        CFG["config.h<br/>SpO2 mode, 411 us PW<br/>400 sps / 4x avg = 100 Hz"]
        DRV["Inline MAX30101 driver<br/>no external libraries"]
        FIFO["FIFO drain<br/>32-deep, 6 B/sample<br/>overflow reported as E lines"]
        CMD["Command handler<br/>START STOP INFO DARK<br/>IRAMP REDAMP TEMP R"]
        CFG --> DRV --> FIFO
    end

    MAX -->|I2C 400 kHz| DRV
    HX -.->|"bit-bang, 27 pulses"| FIFO

    FIFO -->|"S,idx,t_us,ir,red"| LINK
    CMD <-->|commands| LINK
    LINK["USB CDC serial<br/>115200 nominal, ASCII"]

    subgraph HOST["Host — Python"]
        IO["mapquest/io.py<br/>protocol, metadata, link health"]
        BENCH["bench_check.py<br/>acceptance + diagnostics"]
        PROV["provocation.py<br/>blood-volume response test"]
        LOG["logger.py<br/>recording + pressure calibration"]
        PIPE["mapquest/pipeline.py<br/>return-to-flow analysis"]
        ANA["analyze.py<br/>onset + figure + results row"]
        IO --> BENCH
        IO --> PROV
        IO --> LOG
        LOG --> PIPE
        PIPE --> ANA
    end

    LINK --> IO

    subgraph OUT["Outputs"]
        CSV["data/*.csv<br/>self-describing, full firmware header"]
        PNG["Figures"]
        RES["data/results.csv<br/>one row per analysis"]
    end

    BENCH --> PNG
    PROV --> CSV
    PROV --> PNG
    LOG --> CSV
    ANA --> PNG
    ANA --> RES

    classDef missing fill:#ffe6e6,stroke:#c43b2f,stroke-dasharray:4 3
    class HX missing
```

---

## 2. Return-to-flow signal pipeline

The measurement detects **slow venous filling**, not the cardiac pulse. The pulse is roughly
an order of magnitude smaller and is treated only as a quality flag.

```mermaid
flowchart TD
    RAW["<b>0. Raw counts</b><br/>ir, red — 18-bit, 100 Hz"]
    DARK["<b>1. Dark subtraction</b><br/>counts - dark<br/>from the DARK command"]
    REF["Reference window<br/>last 3 s of occlusion<br/>before deflation starts"]
    OD["<b>2. delta-OD</b><br/>ln(ref / counts)<br/>per wavelength"]
    DV["<b>3. Blood volume</b><br/>dV·L = 229·dOD660 + 1555·dOD880<br/>micrometres"]
    LP["<b>4. Low-pass</b><br/>Butterworth 0.3 Hz, 2nd order<br/>zero-phase (filtfilt)"]
    D1["<b>5. Derivative</b><br/>Savitzky-Golay, 2.0 s window<br/>one HeartMate-3 pulse period"]
    NOISE["<b>6. Noise floor</b><br/>mu0, sigma0 over the<br/>reference window"]
    ONSET["<b>7. Onset</b><br/>sustained excursion above<br/>mu0 + 3·sigma0"]
    REFINE["<b>7b. Ramp intersection</b><br/>fit the established ramp,<br/>extrapolate to baseline"]
    MAP["<b>8. Result</b><br/>cuff pressure at onset"]

    RAW --> DARK --> OD --> DV --> LP --> D1
    REF -.->|"counts_ref"| OD
    D1 --> NOISE --> ONSET --> REFINE --> MAP

    PRAW["Pressure raw<br/>HX710B, ~40 Hz"]
    PCAL["Two-point calibration<br/>(raw - RAW0)·P_REF / (RAW_REF - RAW0)"]
    PRES["Resample to PPG timebase<br/>interpolate on HX710B counter"]
    PRAW --> PCAL --> PRES --> MAP

    IRONLY["IR-only fallback<br/>dOD880 / 6.13 cm^-1 × 1e4<br/>vulnerable to desaturation"]
    OD -.->|"--ir-only"| IRONLY
    IRONLY -.-> LP

    PULS["Pulsatility RMS<br/>0.5-5 Hz during reference<br/><i>flag only: pulsatile subject<br/>reads near systolic, not MAP</i>"]
    OD -.-> PULS

    classDef deviation fill:#fff4e0,stroke:#e07b00
    classDef missing fill:#ffe6e6,stroke:#c43b2f,stroke-dasharray:4 3
    class REFINE deviation
    class PRAW,PCAL,PRES missing
```

**Orange** = deliberate deviation from SPEC §5.1. The spec's threshold-crossing onset reads
~2.6 mmHg high, because the 0.3 Hz zero-phase filter and the 2 s Savitzky–Golay window are
both symmetric kernels and smear the ramp corner backwards in time. `--onset-method
intersect` fits the established ramp and extrapolates back, landing within ±0.2 mmHg.

**Red dashed** = not yet built or not yet fitted.

---

## 3. Diagnostic path — what we run before trusting a recording

Every SPEC §3.5 acceptance criterion can pass with the photodiode staring at a wall, so the
gates below exist to catch that.

```mermaid
flowchart TD
    START(["Board + sensor wired"]) --> ID{"INFO reports<br/>sensor_ok=1 and<br/>part_id 0x15?"}
    ID -->|no| FIXW["Check SDA=A4, SCL=A5, 3V3, GND<br/>send R to retry, no reflash"]
    FIXW --> ID
    ID -->|yes| DARKC{"DARK offsets<br/>under ~128 counts?"}
    DARKC -->|no| SHADE["Shade from ambient light"]
    SHADE --> DARKC
    DARKC -->|yes| TUNE["Auto-tune LED current<br/>IR DC into 105k-157k"]
    TUNE --> LINKC{"Link stable?<br/>rate within 1%,<br/>zero dropped samples"}
    LINKC -->|no| CABLE["Swap cable, avoid hubs,<br/>close other serial monitors"]
    CABLE --> LINKC
    LINKC -->|yes| PROVC{"<b>Provocation test</b><br/>does arm raise/lower<br/>move the DC?"}

    PROVC -->|"FLAT"| PIPE2["Light is piping across the surface<br/>without reaching blood.<br/><b>Raising LED current makes it worse.</b><br/>Press flat, strap it."]
    PROVC -->|"INVERTED"| SIGN["Wrong sign — check strap<br/>is not squeezing during movement"]
    PROVC -->|"RESPONDS"| READY(["Site is viable for<br/>return-to-flow"])
    PIPE2 --> PROVC
    SIGN --> PROVC

    PULSE["Cardiac pulse check<br/><i>advisory only</i><br/>needs a prominent spectral peak,<br/>not just the largest in-band bin"]
    PROVC -.-> PULSE

    classDef good fill:#e6f5ea,stroke:#0a7d2c
    classDef bad fill:#ffe6e6,stroke:#c43b2f
    class READY good
    class PIPE2,SIGN bad
```

---

## 4. Validation layer

```mermaid
flowchart LR
    subgraph SYN["Synthetic ground truth"]
        SYNTH["synth.py<br/>known MAP, cardiac +<br/>artificial pulse, desaturation"]
        PBUILD["test_provocation.build()<br/>known response + drift"]
    end
    subgraph MOCK["Mock firmware"]
        FAKE["FakeSerial<br/>full SPEC 3.4 protocol,<br/>injectable disconnect / stall /<br/>board reset / no-pulse"]
    end
    subgraph TESTS["64 tests"]
        T1["test_pipeline.py<br/>SPEC section 6 criteria"]
        T2["test_protocol.py<br/>link + bench_check"]
        T3["test_provocation.py<br/>response analysis"]
    end
    SYNTH --> T1
    FAKE --> T2
    PBUILD --> T3
```

---

## 5. Status

| Layer | State | Evidence |
|---|---|---|
| Firmware — PPG | **working on hardware** | 100 Hz, zero dropped samples over 60 s, part id 0x15 |
| Firmware — pressure | written, never run | compiles under `PRESSURE_ENABLED=1` only |
| Serial protocol | **working** | real captures; plus mock-firmware tests |
| Link failure handling | tested on mock only | injected disconnect / stall / reset |
| Provocation test | **working on hardware** | wrist, z=37.8 up / 19.2 down, correct signs |
| Analysis pipeline | synthetic only | ±0.2 mmHg on synthetic ground truth |
| Pressure calibration | never run | no HX710B |
| Bench trials | not started | — |

### Open issues

Tracked as O1–O3 in [`decisions.md`](decisions.md).

| Issue | Impact |
|---|---|
| Red channel drifts ~90x more than IR (up to −1 %/s) | contaminates ΔV·L; dominates the onset noise floor |
| Onset noise floor measured at **~7.7 µm/s** (best rest period) | `synth.py` defaults to a 3 µm/s filling ramp, i.e. below the real threshold — the synthetic tests are optimistic |
| Baseline did not recover after the arm-down phase | ABA correction for "down" is an underestimate |
| Cardiac rate unstable between analysis settings (54 vs 158 bpm) | pulse readout not yet trustworthy; advisory only |
| No HX710B | onset *time* only; no pressure, so no MAP |
