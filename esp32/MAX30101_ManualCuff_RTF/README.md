# ESP32 DevKit V1 + MAX30101: pulsatile return-to-flow testing

Release **2026-10-12-r3-three**. See [three-channel RTF calibration](THREE_CHANNEL_RTF.md).
Target board: **DOIT ESP32 DEVKIT V1 / classic ESP32**, not XIAO or ESP32-S3.

The software compares RED/IR/GREEN currents and pairs with the cuff deflated,
freezes the best qualifying pair, learns a fresh baseline, detects sustained
pulse suppression during the cuff phase, then detects consistent pulse return
during release. It does not require a reference MAP value. It does not calculate
MAP, SpO2, or an exact opening pressure. The old passive-MAP and LOWPULSE modes are
not in this release. Nonpulsatile use requires a separately developed detector.

## What runs where

- ESP32: continuously records RED, IR and GREEN at 100 samples/s; hosts `RTF-ESP32` Wi-Fi;
  drives the ST7735 status display; watches acquisition and host heartbeat.
- Laptop: runs the pulse detector and serves the local live dashboard. It receives
  the same raw stream through either TCP/Wi-Fi or USB, and sends results to the TFT.
- The TFT is a live status display, **not a standalone detector without the laptop**.
- Wireless means no laptop USB data cable is required. The ESP32/display/sensor
  still need stable power. A USB power bank is a simple independent power source.

## Wiring

The current MAX30101 mapping assumes your `D4` wire is SDA and `D5` is SCL.
On this DevKit V1, these mean **GPIO4 and GPIO5**. Reverse the two if the sensor's
SDA/SCL labels show they are connected the other way around.

| MAX30101 breakout | ESP32 DevKit V1 |
|---|---|
| SDA | D4 / GPIO4 |
| SCL | D5 / GPIO5 |
| VCC | Your existing 3.3 V breakout supply |
| GND | GND |

The supply entry refers to your assembled breakout, not directly powering a bare
MAX30101 IC. Keep the existing appropriate breakout power arrangement.

For the Adafruit 1.44-inch **128 x 128 ST7735 TFT with microSD socket**:

| Display pin | ESP32 DevKit V1 |
|---|---|
| VIN / 3-5V | 3.3 V |
| GND | GND, shared with sensor |
| CLK / SCK | GPIO18 |
| MOSI / SI | GPIO23 |
| TFT_CS / TCS | GPIO27 |
| D/C / DC / A0 | GPIO26 |
| RST / RESET | GPIO25 |
| Card CS / CCS | GPIO33 (firmware holds HIGH) |
| MISO / SO | Not required for this release |
| Lite | Leave unconnected on the linked Adafruit board; pulled high internally |
| 3.3Vout | Leave unconnected; this is a display regulator output, not VIN |

Do not copy the Arduino Uno pin numbers from the Adafruit example. This build
uses explicit ESP32 SPI assignments, including CS=27 so GPIO5 stays dedicated
to your sensor's SCL. The SD card is not used; no card is required. Remove any
card during initial display tests. No display acknowledgement is possible over
this write-only SPI interface, so actual screen operation must be checked visually.
The selected initializer is `INITR_144GREENTAB`, as in the linked Adafruit guide.

## Home Wi-Fi (current connection)

See [Home Wi-Fi guide](HOME_WIFI.md). The ESP32 can join your router while the
laptop retains internet access. The direct network below remains a fallback.

Use **Start_Forearm_Auto.cmd** for the current forearm placement: USB is preferred,
with Wi-Fi fallback at startup. See THREE_CHANNEL_RTF.md for the current calibration sequence.

## First-time setup

1. Keep the cuff fully deflated. Connect the ESP32 to USB for programming.
2. While the laptop has internet access, run **Setup_Windows.cmd** once. This
   installs NumPy/pyserial, downloads build dependencies, builds and tests. It
   does not upload or start a cuff test.
3. Run **Upload_Firmware.cmd**. Select the actual DevKit V1 COM port. Close other
   serial monitors first. If the bootloader does not connect, use your board's
   BOOT button procedure. Upload the matching release before use.
4. For a wired first check, run **Start_USB.cmd** and select the same COM port.
5. For wireless use, power the board independently, connect the laptop to:
   - Network: **RTF-ESP32**
   - Password: **RTFtest2026**
   - This is a local test network with no internet connection.
6. Run **Start_WiFi.cmd**. The browser opens the laptop dashboard at
   `http://127.0.0.1:8766`. The ESP32 data connection is `192.168.4.1:8765`;
   that address is not a webpage. Wi-Fi credentials are configurable in the sketch.

Only one monitor may own the recorder. Do not connect USB and Wi-Fi monitor apps
at the same time. Bluetooth is not implemented; Wi-Fi is the selected wireless
transport. A normal Wi-Fi internet connection is not needed during measurement.

## Testing workflow

### Serial help

At 115200 baud, `HELP` shows the manual-cuff workflow and `HELP ADVANCED`
lists host diagnostics. Old `calplot`, `plot`, and `measure` commands are retired.
Use the dashboard for measurements. Close Serial Monitor before starting the USB
dashboard: they cannot share the same COM port.

### Manual cuff controls

1. Secure the reflectance sensor on the supported upper arm distal to the cuff,
   outside the cuff's compression area. The forearm is an alternative site.
   Keep sensor position, attachment pressure and arm support unchanged during a
   trial. Recalibrate after changing sites; do not reuse an upper-arm baseline
   for the forearm. This is arm testing, not a fingertip protocol.
2. With cuff completely deflated, click **Calibrate**. The software adjusts RED/IR/GREEN
   LED settings, freezes them, then needs three consecutive stable four-second
   windows. A baseline without clear pulses will not pass.
3. Click **Start cuff phase BEFORE starting inflation**. The timer starts now.
   Follow your established cuff-testing protocol; the software does not prescribe
   an inflation pressure or control the cuff.
4. Watch the remaining pulse percentages. Two independent four-second windows of
   strong suppression, with acceptable signal quality, establish **PULSE SUPPRESSED**.
   This is evidence consistent with occlusion, not proof of complete cessation of
   flow. Do not increase pressure or hold longer merely to make the algorithm pass.
5. Once suppression is detected, click **Start return phase BEFORE cuff release**.
   If pulses return before that phase is started, suppression is invalidated and
   the trial does not silently count the earlier return as a measured event.
6. Record actual gauge readings when convenient. Each reading is an annotation
   with its age; the system does not pretend the last entered value equals
   continuously changing cuff pressure or pressure at the event.
7. The first good window reports **Possible pulse return**. A second independent
   window with consistent selected-pair timing and amplitude above learned noise confirms
   **PPG RETURN DETECTED**. Fully release the cuff and click **Cuff is fully released**.
8. The app checks pulse recovery. Allow the built-in recovery interval and
   recalibrate before a new trial. STOP is available throughout.

The host and firmware both warn at 60 seconds and stop at 90 seconds from the
start of the cuff phase, including operator waits. These are software limits,
not guarantees of a safe exposure for a person. They **cannot release a manual
cuff**. Stop/release immediately for discomfort or problems; do not wait for a
timer. A disconnection displays STOP locally and on the laptop when detected.
After a connection fault, release the cuff and restart the app/board before retrying.

## What changed in the detector

- No 0.3% DC-contrast requirement and no 250-count DC normalization denominator.
- No passive PPG-to-MAP regression, artificial MAP prior, or DC-only fallback.
- Detrending, pulse-frequency fitting with harmonic support, waveform periodicity,
  Selected-pair correlation, frequency agreement and within-window consistency.
- Suppression assessed against the person's pulsatile baseline, with two separate
  windows and residual coherent-pulse rejection.
- Return threshold combines the observed pulse-like noise in suppressed windows,
  a fraction of baseline pulse amplitude, and a minimum absolute amplitude.
- Non-overlapping windows: the same beats cannot count as independent confirmations.
- Missing samples, timing gaps, optical configuration changes, flatlines, clipping
  and large contact/motion changes invalidate evidence. Transport faults abort.
- LED settings are locked during an armed cuff phase.

**Start_Sensitive_Test.cmd** selects an experimental mode that reduces the
baseline-relative return-amplitude floor from 6% to 3%. It also lowers the baseline pulse floor from 8 to 4 ADC RMS counts and the
absolute return floor from 4 to 2 counts. It retains the learned noise threshold,
pulse consistency and suppression requirements.
It labels results experimental; it is not evidence of greater accuracy. The main
improvement for the reported failure is removing the inappropriate DC contrast
gate, not weakening proof of pulse suppression.

## Timing and physiological limits

The window duration is four seconds. A candidate can appear after one acceptable
window; confirmation requires another. This deliberately delays confirmation.
Window mixing at onset can add more delay. Host reception and device FIFO-read
times are logged, but neither is an exact time of physiological flow onset.

PPG is an optical blood-volume signal, not a direct flowmeter. Periodic movement
that resembles pulses, vasoconstriction, weak perfusion, unusual rhythm, sensor
pressure and other real physiology can still cause errors. The supported baseline
search band is approximately 39-180 bpm; this is not a validated use population.
A repeatable synthetic pass does not establish physiological accuracy. This
release needs recordings and independent event verification before accuracy
claims. It is a research tool and is not for clinical decisions.

## Logs and reproducible analysis

Each launch creates a unique `Data/session_...` folder:

- `samples.csv`: all received raw RED/IR/GREEN samples, sequence numbers, LED settings
  and host/device times. These are the data needed to investigate weak signals.
- `wire.txt`: raw protocol records and firmware errors.
- `replay.jsonl`: commands, full analysis windows, features, states and events.
- `result.json`: pulse-return event and manual annotations, if detected. A result
  is historical; consult the event log for later disconnects or aborted recovery.

Run `python PlatformIO/tools/replay.py Data/session_.../replay.jsonl` to reproduce
the detector states using the exact recorded windows. Replay rejects a version
mismatch. It cannot establish whether physiological occlusion actually occurred.

The canonical source is `Arduino/MAX30101_ManualCuff_RTF/MAX30101_ManualCuff_RTF.ino`.
The pre-build script mirrors it into `PlatformIO/src/main.cpp`; do not edit the
generated copy alone. Detector logic is in `PlatformIO/tools/rtf_detector.py`.

## Future nonpulsatile version

The acquisition protocol and raw recording are reusable. The current detector
intentionally refuses calibration without pulses. A future nonpulsatile detector
must be a separate, explicitly identified analysis path with its own reference
experiments; lowering pulsatile thresholds is not a valid conversion.

## Sources used for design and wiring

- [MAX30101 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX30101.pdf)
- [Adafruit 1.44-inch TFT guide supplied by the user](https://learn.adafruit.com/adafruit-1-44-color-tft-with-micro-sd-socket?view=all)
- [ESP32 SPI pin assignment API](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/spi.html)
- [PlatformIO DevKit V1 configuration](https://docs.platformio.org/en/latest/boards/espressif32/esp32doit-devkit-v1.html)
- [Nitzan et al., PPG-based systolic opening detection](https://pubmed.ncbi.nlm.nih.gov/19857254/)

The literature supports the general pulse-return principle, not validation of this
specific detector. The published two-sensor method is not claimed as implemented
by this single-sensor prototype.

## Arm placement and weak-signal testing

Default launchers record `upper_arm` as the intended sensor site. For forearm use,
choose `Start_Forearm_USB.cmd` or `Start_Forearm_WiFi.cmd`. These use the standard
thresholds. `Start_Forearm_Weak_USB.cmd` selects the experimental lower-amplitude
profile; it is not a claim that weak signals can always be measured accurately.
For the same profile over Wi-Fi: `Run.cmd --site forearm --sensitive`.

The chosen site is visible in the dashboard and retained in session metadata,
results and replay. It describes the intended site, not proof a sensor is on skin.
With no person attached, low counts are expected and should not pass calibration.
Both wavelengths must still show repeatable pulses. Optical LED adjustment remains
bounded; a site that cannot provide sufficient signal must fail calibration rather
than invent a reading. This release acquires all three channels and freezes the best qualifying pair for RTF.

No upper-arm or forearm physiological accuracy has been validated. Synthetic
low-amplitude tests only exercise the algorithm. Record and assess upper arm and
forearm separately in future reference experiments; do not pool their accuracy.
Changes in attachment pressure, movement, ambient light or site can affect the
optical signal. No guarantee of unchanged accuracy follows from lowering a floor.

Research informing this distinction:
- [Hartmann et al., measurement-site waveform comparison](https://www.frontiersin.org/journals/physiology/articles/10.3389/fphys.2019.00198/full)
- [Abay and Kyriacou, forearm reflectance PPG during cuff-induced occlusions](https://openaccess.city.ac.uk/id/eprint/13269/)

The forearm study used a custom optical probe and different processing; its results
do not validate this MAX30101 detector or its return-to-flow timing.
