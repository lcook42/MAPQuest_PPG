# Validation record — October 6, 2026

Both projects compiled and linked successfully for the real `seeed_xiao_esp32s3` target using PlatformIO Core 6.2.0, Espressif32 platform 6.9.0, Arduino ESP32 2.0.17, Xtensa compiler 8.4.0 and SparkFun MAX3010x library 1.1.2. Firmware images were generated; no upload was performed.

| Project | Build | Static RAM | Application flash |
|---|---|---:|---:|
| Guided reference MAP, with continuous view | PASS | 34,744 / 327,680 bytes | 326,245 / 3,342,336 bytes |
| No-reference relative optical baseline, with continuous view | PASS | 33,456 / 327,680 bytes | 310,169 / 3,342,336 bytes |

The initial build attempt failed with a Windows compiler `CreateProcess` error caused by long build paths. Both builds succeeded after temporarily mapping this workspace to a shorter drive path. No firmware edits or dependency changes were needed for that recovery. On Windows, extract this package to a short directory if you encounter a similar error.

Additional offline checks passed:

- Removing only the continuous plot function, dispatch and HELP addition from reference `src/main.cpp` recovers the uploaded `.ino` text; the separately preserved original Arduino file is byte-identical to the upload. Its SHA256 is in `SOURCE_SHA256.txt`.
- Experimental `.ino` and `src/main.cpp` contain identical text.
- Experimental source has no reference-MAP prompt, pressure prior, regression fitter, MAP estimator or reference-model namespace.
- Experimental failed-window retries do not retune baseline LED settings.
- Three desktop graph parser tests pass: waveform values (including negatives), calibration values and rejection of incomplete or diagnostic rows. Run `python -m unittest discover -s tools` from this package root; these tests need only Python's standard library.
- Desktop helper passes Python syntax compilation. ZIP contents were checked for archive integrity.

## What these checks do not establish

Compilation checks syntax, dependency compatibility and linkage, not sensor behavior or pressure accuracy. The firmware has not been exercised on connected MAX30101 hardware, with human participants or an LVAD system. I²C acquisition, prompts over USB, optical tuning, rejection behavior, flash persistence across real power cycles and live graph capture require bench testing. The desktop graph window has not been exercised here; only its parser and syntax were tested. The Arduino sketches are provided, but a separate Arduino IDE toolchain build was not run.

The reference version retains original limitations documented in its README. No-reference distances and thresholds have no validated pressure meaning. No absolute MAP accuracy, clinical validation, diagnostic capability or treatment suitability is claimed for either build.

## Suggested bench check

1. Build and upload one selected project. Confirm startup detects the sensor at `0x57` and HELP/STATUS respond at 115200.
2. Confirm unplugged sensor detection, invalid optical placement, acquisition timeouts and saturation/motion/drift messages before collecting data.
3. In reference mode, complete the supplied guided reference workflow, inspect the three held-out points, exercise MEASURE/PLOT/CALPLOT and verify saved-model loading after reset.
4. In no-reference mode, confirm START never requests a pressure, compare repeated stationary measurements, verify MEASURE refuses to run before a baseline, and confirm RETUNE clears the baseline. Check CLEAR and restart persistence independently.
5. Close the serial monitor, run the desktop helper and verify CSV rows match labeled serial output. Do not confuse a plotted training fit or a passed software gate with independent pressure validation.

The continuous-view update adds LIVEPLOT/CONTINUOUS and newline-terminated STOP to both firmware menus and Arduino sketches. The desktop helper supports a rolling live graph, bounded display memory and streamed CSV. Build and live-helper verification results for this update are recorded below. No connected-hardware live plot test has been performed.

- Both updated firmware targets compiled, linked and generated images successfully.
- A desktop helper integration check used 1,105 synthetic waveform rows delivered in fragmented serial chunks with a noninteractive Matplotlib backend. All rows reached CSV, the displayed lines retained only 1,000 samples, STOP was sent on exit, and the serial port closed. This checks stream handling and shutdown; it does not test a physical sensor or an interactive desktop window.
- The three existing parser tests still pass. Removing only the reference continuous-view extension recovers the original supplied sketch text, and both runnable Arduino sketches match their PlatformIO sources.
