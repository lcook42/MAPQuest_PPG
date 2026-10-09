# Build guide: PPG + measured occlusion pressure for non-pulsatile MAP research

## What you can build now

Build a system that records **raw red/infrared/green PPG, calibrated pneumatic pressure and operator event markers** on a shared microcontroller clock. First use a bench pressure circuit and optical target. Add independent flow observation and a vascular phantom before considering supervised clinical experiments.

The deliverable is a working acquisition platform. It does **not** yet measure accurate MAP. A MAX30101 senses optical changes associated with tissue and blood volume; its DC level is not a unique measure of flow or arterial pressure. A slow optical change during compression can also reflect venous filling, contact deformation or motion. Historical DC-PPG/flow comparisons found agreement only under restricted conditions. [Primary study](https://journals.sagepub.com/doi/10.1177/000331978803900906)

For a patient with little pulsatility, cuff pressure at flow return is a candidate measurement to investigate. With residual pulsatility it can behave differently. Published LVAD Doppler comparisons show why it must be evaluated against an independent reference rather than automatically named MAP. [HeartMate 3 study](https://pmc.ncbi.nlm.nih.gov/articles/PMC7754743/), [multicenter study](https://pubmed.ncbi.nlm.nih.gov/34172641/)

**No-reference means no entered patient MAP; it does not mean no calibration.** Instrument pressure calibration is still necessary. A future validated physical occlusion method might avoid fitting to each patient's MAP, but this kit does not establish that method. The prior no-reference optical firmware remains a relative tracker.

## Materials / BOM

Prices below are manufacturer page prices observed while preparing this guide; shipping, tax, variants and availability can change. Reuse your existing XIAO and MAX30101 where applicable. Unpriced items require a supplier quote or clinical-equipment loan.

| Item | Qty | Specification / purpose | Source / price basis |
|---|---:|---|---|
| Seeed XIAO ESP32-S3 | 1 | Arduino/PlatformIO controller, 3.3 V logic, USB data interface | [Seeed board documentation](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/); reuse existing board |
| SparkFun MAX30101 Qwiic breakout, SEN-16474 | 1 | Raw RED/IR/GREEN acquisition; onboard regulators; I²C address 0x57. Buy the breakout, not a bare IC | [SparkFun product](https://www.sparkfun.com/sparkfun-photodetector-breakout-max30101-qwiic.html?___store=default); $34.39 |
| Adafruit MPRLS breakout, #3965 | 1, default | Easy bench pressure channel, **absolute** 0–25 psi; atmospheric zero required | [Adafruit product](https://www.adafruit.com/product/3965?gQT=1); $29.95 |
| Optional alternative pressure board | 1 instead | Honeywell **MPRLS0300YG00001BB** evaluation board: gauge 0–300 mmHg, transfer B. Requires its own verified harness/pinout; select `bench_gauge300` | [Honeywell MPR datasheet](https://automation.honeywell.com/content/dam/honeywell-edam/sps/ast/en-us/campaigns/pressure-sensors/documents/sps-siot-mpr-series-datasheet-32332628-ciid-172626.pdf); quote/availability check |
| USB-C data cable | 1 | Data-capable, not charge-only | Existing cable or electronics supplier |
| Qwiic-to-header cable / soldered headers / insulated jumpers | As needed | 3.3 V power, common GND, SDA/SCL, short secured wiring | Match the actual breakout connectors |
| Manual sphygmomanometer cuff, bulb and release valve | 1 set | Properly functioning manual equipment; cuff sizes for intended later protocol | Medical-equipment supplier; clinical team selects patient equipment |
| Pneumatic tee, compatible dry tubing and adapters | 1 set | Sensor taps the same pressure chamber; minimize extra dead volume | Adafruit port is 2.5 mm OD; guide suggests 3/32 inch ID tubing. Adapt to cuff hose rather than stretching unsuitable tubing. [Port details](https://learn.adafruit.com/adafruit-mprls-ported-pressure-sensor-breakout?view=all) |
| Independently calibrated pressure reference | 1 | Pressure calibrator/manometer, current calibration certificate and stated uncertainty, suitable range | Borrow from biomedical engineering/metrology; cuff dial alone is not a traceable reference |
| Bench pressure chamber / cuff on rigid mock limb | 1 | Test pneumatics without any human limb | Rated dry chamber, mechanical fixture, pressure-rated fittings |
| Optical fixture and light shield | 1 | Repeatable position, distance/contact loading; opaque cover for dark readings | Nonconductive holder, matte shield; document dimensions/loading |
| Enclosure and strain relief | 1 set | Protect wiring; fix sensor and tubing connections | Electronics supplier / fabricated housing |
| Independent flow observation | Later stage | Clinical vascular Doppler with appropriate probe; for a phantom, compatible flow measurement with known detection threshold | [Parks vascular Dopplers](https://parksmed.com/collections/doppler); loan or quote including correct probe |
| Vascular phantom | Later stage | Compliant tubing, optical scattering material, controlled flow, upstream/downstream pressure and known fluid properties | Custom research apparatus; not included or validated by this kit |
| Clinical electrical-safety provisions | Before human testing | Power/interface arrangement reviewed by biomedical engineering; use qualified isolation where required | Research institution. Battery power alone does not certify electrical safety |
| Independent clinical MAP reference | Validation only | Clinically indicated, correctly set up arterial line/monitor; study team documents reference quality | Existing clinical equipment; do not insert a line just to try this build |

The two listed optical/pressure breakouts total **$64.34** before other parts. That is not a complete system cost. A traceable reference, mechanical fixture, flow instrumentation and clinical validation can dominate the budget.

### Pressure-sensor choice matters

The inexpensive default breakout is appropriate for learning acquisition. It is not an accuracy shortcut: 24-bit conversion describes resolution. For the 25 psi absolute part, a ±1.5% full-scale total-error example corresponds to about **±19.4 mmHg**. Zeroing removes atmospheric offset but does not prove span, linearity or temperature accuracy. The gauge 300 mmHg family also has errors of several mmHg; switching profiles does not establish clinical performance. [Honeywell specifications](https://automation.honeywell.com/content/dam/honeywell-edam/sps/ast/en-us/campaigns/pressure-sensors/documents/sps-siot-mpr-series-datasheet-32332628-ciid-172626.pdf)

For an accuracy-focused successor, specify a suitable gauge sensor and reference chain with demonstrated total uncertainty across the intended range and temperatures. An engineering allocation such as **≤1 mmHg for the pressure channel** is a design target, not this kit's verified performance or a clinical pass criterion. Replace the hardware/driver if a zero-plus-gain correction fails independent checks.

## Numbered build and development steps

### 1. Define the first milestone

The first milestone is: stable raw optical recordings, pressure readings independently checked on a bench, documented event timing and reproducibility after remounting. Do not set “display an MAP number” as the first pass criterion. For later clinical work, have the LVAD team define target population, allowable procedures and acceptance criteria before collecting measurements.

### 2. Assemble the electronics with power disconnected

Default breakout wiring:

| XIAO connection | MAX30101 SparkFun breakout | Adafruit MPRLS #3965 |
|---|---|---|
| 3V3 | 3.3V | **VIN** |
| GND | GND | GND |
| D4 / GPIO5 | SDA | SDA |
| D5 / GPIO6 | SCL | SCL |

Leave MPRLS RST and EOC unconnected with this driver. **Its pin labeled 3v3 is an output; power this breakout through VIN.** Do not put 5 V on XIAO signal pins. The exact Honeywell alternative has different connections: follow its evaluation-board pinout, not this Adafruit table. [Adafruit pinouts](https://learn.adafruit.com/adafruit-mprls-ported-pressure-sensor-breakout/pinouts), [SparkFun hookup guide](https://learn.sparkfun.com/tutorials/sparkfun-photodetector-max30101-hookup-guide/all?print=1)

Use short secured wires and common ground. Check continuity and shorts before USB power. Multiple breakout pull-ups can load I²C; check bus behavior if wiring grows or faults appear.

### 3. Assemble the dry bench pneumatics

```text
Manual bulb + manual release valve
                 |
          shared cuff/chamber
                 |
                tee ------ pressure breakout (dry air only)
                 |
       calibrated pressure reference
```

The sensor and reference must see the same chamber pressure. Secure compatible hoses, minimize added volume, avoid kinks and protect the sensor from liquid. Initially place the cuff on a mock limb, not a person. Keep a readily accessible manual pressure release. The firmware cannot detect which valve is open or physically release pressure.

Check leakage, pressure agreement, connection retention and complete return to atmospheric pressure within the equipment's rated range. Avoid compressed-gas sources unless the bench apparatus has a properly designed regulator and pressure protection.

### 4. Install and upload the firmware

1. Extract the kit into a convenient folder and open `PPG_Occlusion_Research.code-workspace` in VS Code.
2. Install the **PlatformIO IDE** extension if needed. Let project initialization complete.
3. Click the PlatformIO alien-head icon on the left sidebar. Under **PROJECT TASKS**, expand the firmware project, then **bench_absolute → General → Build**. If tasks are hidden, use **View → Command Palette → PlatformIO: Build** with the firmware project active.
4. Plug in the XIAO with a data cable. Click **Upload** for that same environment. For the exact supported gauge alternative, choose **bench_gauge300** instead for both Build and Upload.
5. Open **Monitor** in that environment's General tasks, or the command **PlatformIO: Serial Monitor**. Set 115200 baud. Enter `STATUS` followed by Enter.
6. Confirm the expected profile and both sensors `READY`. A missing device, wrong profile or unstable connection must be fixed before proceeding.

Optional terminal commands, run from the kit root:

```powershell
pio run -d firmware -e bench_absolute
pio run -d firmware -e bench_absolute -t upload
pio device monitor --baud 115200
```

If `pio` is not on PATH, use PlatformIO's tasks. The Arduino alternative is `arduino/Passive_PPG_Pressure/Passive_PPG_Pressure.ino`: install the ESP32 board package, choose XIAO ESP32-S3, enable USB CDC on boot and install SparkFun MAX3010x plus Adafruit MPRLS libraries with their dependencies. The `.ino` defaults to the Adafruit profile; changing hardware requires selecting the matching `PRESSURE_PROFILE` in the sketch.

### 5. Run the desktop simulation

From the kit root:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r tools\requirements.txt
.\.venv\Scripts\python.exe tools\acquire.py --simulate --out recordings\simulation.csv
```

The graph runs for 20 seconds. Close its window to finish the files. The signals are deliberately arbitrary, not simulated human physiology. An included example plot demonstrates the display.

### 6. Zero and calibrate the pressure channel on the bench

1. Warm/stabilize the equipment according to manufacturer instructions and record temperature.
2. Open the pneumatic path to atmosphere. At idle send `GAIN 1`, then `ZERO`. Wait for `ZERO set`.
3. Record stable readings alongside the calibrated reference at multiple pressures over your intended operating range, in both increasing and decreasing order. Example bench points are 0, 50, 100, 150 and 200 mmHg if the entire apparatus is rated appropriately. These are bench test points, not patient inflation instructions.
4. Repeat points, vent and re-zero between cycles; assess drift, leaks and hysteresis. Use stable plateau readings, not one convenient sample.
5. Copy `templates/pressure_calibration_EXAMPLE.json` to a new filename and replace every example number with your measurements. Use pressure observations acquired at `GAIN 1`. Record the reference certificate and uncertainty in session notes.
6. Reserve separately acquired intermediate pressures and repeat cycles as independent checks. Do not reuse fitted observations as check data.
7. Run:

```powershell
.\.venv\Scripts\python.exe tools\calibrate.py my_pressure_calibration.json --out my_calibration_result.json
```

8. Apply the printed `GAIN ...` command while idle, then verify new measurements against the independent reference. Settings are not saved across reboot; re-zero and reapply the approved instrument gain each session.
9. If remaining errors, hysteresis or temperature drift exceed your predeclared uncertainty budget, investigate/replace the sensor or implement a justified calibration model. This software supplies only zero-plus-gain fitting; it cannot repair an inadequate sensor through regression.

### 7. Verify the optical channel and fixture

Use a stable bench optical target first. With recording stopped, set `LED 0 0 0` to inspect dark/ambient readings, then restore `LED 32 32 16`. Record fixture geometry and shielding. Adjust LED codes between recordings if needed; codes are driver settings, not absolute optical power. Avoid clipping and retain raw DC levels. Do not auto-tune LEDs during a pressure sweep: changing illumination could mimic the optical change you are studying.

Check remounting, contact-force changes, ambient light and target motion as separate experiments. Treat a stable-looking plot as signal quality evidence, not pressure accuracy. The current clipping threshold is a bench heuristic; there is no validated contact/motion classifier.

### 8. Record continuously and mark events

Close all other serial monitors; only one application can own the port. Then run:

```powershell
.\.venv\Scripts\python.exe tools\acquire.py --port COM3 --out recordings\bench_001.csv
```

Replace COM3 with your board's port. The board may reset when the port opens. Wait for startup or send `STATUS`. Physically vent the sensor and click **ZERO**. Send the approved `GAIN` and chosen `LED` commands in the command box, then click **START**.

Two plots scroll continuously: raw optical counts and pneumatic gauge pressure. Enter `MARK BASELINE`, `MARK RAMP_BEGIN` or another short label and press **SEND**. During authorized later flow experiments, define marker names such as `FLOW_LOST` and `FLOW_SEEN` and the independent detection criterion in advance.

Click **STOP**, release pressure manually, and close the window. Closing the viewer attempts to send STOP, but this is not a physical pressure-release mechanism. Output includes parsed `.csv` rows and a `.serial.log` with the received stream, replies and rejected rows. Use a new filename per session; files are not overwritten.

### 9. Review data before interpreting physiology

```powershell
.\.venv\Scripts\python.exe tools\analyze.py recordings\bench_001.csv --out recordings\bench_001_review.png
```

The tool writes a plot and a text marker report. A reported number is **nearby measured pneumatic pressure at an operator marker**, not MAP. It interpolates only between unflagged pressure observations within 100 ms on either side; it rejects gaps, faults and extrapolation. The 100 ms criterion is a software quality rule, not a validated physiological tolerance. Manual reaction delay remains uncorrected.

### 10. Characterize timing and repeatability

Measure actual sample intervals and fault frequency, pressure response lag, USB backlogs, marker delay and reset behavior. Remount and repeat on different days. Compare fixed mounting versus remounting, different operators and temperature conditions. Preserve every exclusion and failure; do not keep only convincing traces.

Optical acquisition is configured at 100 Hz; pressure conversion is scheduled about 25 Hz. Conversion delays and serial/I²C service can reduce actual rates. PPG timestamps bracket FIFO servicing, **not exact light exposures**. Pressure timestamps bracket its conversion. These are shared-clock records with timing uncertainty, not proven simultaneous samples. A marker is timed when its command reaches the board, not when an operator first hears Doppler flow. Before endpoint analysis, quantify timing error; pressure ramp rate multiplied by delay gives a pressure-error contribution. For example, 3 mmHg/s × 0.5 s = 1.5 mmHg.

### 11. Add a controlled-flow phantom

With appropriate laboratory support, build a compliant vessel embedded in optical scattering material, controlled flow, and calibrated upstream/downstream pressure and flow sensing. Use steady-flow and residual-pulsatility cases separately. Verify that the optical fixture can detect the intended vessel before designing occlusion experiments.

Compare PPG changes with independently observed flow during pressure changes. Vary optical contact force, venous/outlet pressure, cuff placement, vessel depth, flow and compliance. A clear result in one phantom does not establish tissue performance. Pressure in a compliant tube, cuff pressure and flow-return threshold need not be equal: characterize their relationship rather than assuming it.

No phantom geometry, flow-control code or inferred endpoint is supplied as a validated model. That work requires selecting materials and measurements suitable for your specific research question.

### 12. Establish the supervised clinical protocol

Work with an LVAD clinician, biomedical engineer and research oversight team before patient contact. They must choose safe cuff procedures, site restrictions, stop conditions, electrical-safety arrangements and independent flow/reference measurements for the intended population. This kit has no clinical safety interlock, automated maximum-pressure control or validated inflation protocol.

Use a qualified vascular Doppler during development rather than declaring absent PPG pulsatility to mean absent arterial flow. The clinical team should distinguish genuinely non-pulsatile states from weak or intermittent pulsatility. ISHLT guidance discusses the difficulties of blood-pressure assessment during mechanical circulatory support. [ISHLT guideline](https://www.ishlt.org/docs/default-source/standards-guidelines/2023_guideline_ishlt_mcs10yearupdate.pdf)

During validation, compare with an independent, clinically indicated arterial-line reference where appropriate. The team checks leveling, zero, dynamic response and monitoring quality. Record cuff, optical-site and reference heights; control or account for hydrostatic differences. Compute reference MAP from the time-averaged arterial pressure over a defined matching interval; the usual `(SBP + 2×DBP)/3` approximation is not an appropriate substitute in this setting. Agree how to handle pressure changes during occlusion rather than casually pairing readings taken at different times. Do not electrically connect this hobby board to clinical monitoring equipment without an approved interface.

### 13. Develop the endpoint and validate it independently

Define a candidate endpoint using bench and development data, with an explicit physical rationale: for example, a particular independently confirmed flow transition and reproducible optical change. An event label alone is not an algorithm. If a regression is needed, train it on reference measurements and freeze its parameters before evaluating it.

Separate participants and sessions used for development from those used for validation. Test relevant pressures, varying residual pulsatility, perfusion, tissue characteristics, temperature, edema and remounting. Determine failure-to-measure and inconclusive rates as well as error among accepted measurements.

Report bias, spread and limits of agreement versus reference MAP, with analysis that accounts for repeated measurements within patients. Also report within-session repeatability and between-session reproducibility. A simple leave-one-window-out regression on the same patient cannot establish generalizable accuracy. Predefine tolerances with the clinical/statistical team and applicable requirements; do not call this prototype validated because it matches a few calibration points.

### 14. Only then add an MAP display or automate pressure

Add an MAP output only after the endpoint or model meets independent acceptance criteria in the intended population. Display uncertainty and an explicit “no result” when required signals fail. Any later pump/valve system needs a separately engineered safety architecture, including an independent pressure limit, timeout, fault release and manual release, assessed by the clinical engineering team. This kit intentionally contains no actuator connections or actuator code.

## Software reference

Commands are newline-terminated ASCII. `HELP`, `STATUS`, `ZERO`, `GAIN number`, `LED r ir g`, `START`, `STOP`, `MARK label` are supported. ZERO/GAIN/LED require idle mode; START requires both sensors and a zero from the current boot. Marker labels contain 1–40 letters, digits, `_` or `-`.

CSV columns are `host_received_utc,kind,seq,t_start_us,t_end_us,a,b,c,flags,label`. Device timestamps are microseconds since boot; host receipt time is separate. Sequence numbers identify emitted rows, not exact counts of physical sensor samples. A reset or out-of-order stream ends valid ingestion: start a new session and zero again.

| Kind | a | b | c |
|---|---|---|---|
| P | raw RED | raw IR | raw GREEN |
| C | zero/gain-corrected pneumatic mmHg | native pressure value | native atmospheric zero |
| E | NaN | NaN | NaN; label holds operator/start/stop marker |

Native pressure units are hPa for the default absolute profile and normalized mmHg for the supported gauge profile. Record the profile in session notes and retain board status lines.

Flag bits: **1** optical clipping heuristic; **2** PPG FIFO backlog/possible software loss; **4** hardware FIFO overflow; **8** pressure conversion fault; **16** pneumatic value outside −10..300 mmHg bench range. Combined bits are possible. The viewers hide flagged samples with gaps; the CSV keeps them. A clear flag field does not prove valid contact, motion-free data, calibrated pressure or loss-free communication. There is no reliable exact lost-sample count.

## What remains necessary for an accurate result

You still need verified pressure uncertainty, a repeatable mechanical/optical setup, independent proof of the flow event, a defensible relationship between that event and MAP, controlled timing, and validation in the intended patients. The supplied software gives you the acquisition and bench calibration tools to investigate those questions. It does not supply a clinically justified no-reference absolute-MAP algorithm.
