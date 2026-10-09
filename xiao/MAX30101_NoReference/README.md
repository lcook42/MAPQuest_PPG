# No-reference experimental optical baseline project

Runnable guided firmware with **no reference MAP inputs**. This mode establishes an optical baseline, not a blood-pressure calibration. It outputs no numeric MAP or mmHg estimate. Its outputs are experimental, relative and uncalibrated, with no validated mapping to absolute pressure or pressure change.

See the package README for wiring, PlatformIO/VS Code setup, Arduino IDE support and desktop PPG graphs.

## Workflow

1. Upload, open the monitor at 115200 and type `START` (aliases `BASELINE` and `CALIBRATE`).
2. Follow the readiness prompt with the sensor at a consistent site/contact. The previous baseline is cleared, dark offset is measured and the three LED channels are tuned.
3. Keep still through six accepted six-second baseline windows. Failed windows have bounded retries. A failure ends START; readjust and run START again.
4. The baseline mean and sample standard deviation are saved with LED codes, a version and checksum under Preferences namespace `optrelative`, key `baseline`. The reference project's calibration is stored separately.
5. Type `MEASURE`, follow the placement prompt and capture three accepted windows. The output includes `RelativeOpticalDistance`, `WindowDistanceSpread`, five signed feature Z values, and `COUNT THIS RELATIVE MEASUREMENT: YES/NO`.

The baseline retains LED settings for later measurements; failed-window retries do not retune. `RETUNE` clears the baseline and requires a new START. Changes in site/contact, ambient light, temperature, placement, or session can affect optical features even at unchanged pressure. Prefer a fresh baseline for a new session and after sensor reattachment; a loaded baseline cannot verify that the physical setup is unchanged.

## Output definition

Five features are used: `log(redDC/redLEDcode)`, `log(irDC/irLEDcode)`, `log(greenDC/greenLEDcode)`, `log(1+1000*redPPI)` and `log(1+1000*irPPI)`. PPI is the inherited AC-RMS/DC measure. LED-code normalization is approximate, not a calibrated light-power correction.

For each feature, `z = (current - baselineMean) / baselineScale`. Scale is the baseline sample standard deviation, floored at 0.05 for DC log features or 0.10 for pulsatility log features. The per-window distance is `sqrt(mean(z*z))`; the reported distance and feature changes use the median of three windows. Distance is nonnegative and dimensionless. Zero means features match the baseline mean; an increase indicates optical difference, with **no inferred direction of pressure change**. Z values show the signed change of each optical feature only.

Baseline sample SD above 0.25 in any feature rejects START. Measurement distance spread above 2.0 rejects the relative measurement. Distance above 3.0 prints a placement/repeat check. These thresholds and floors are explicit experimental heuristics, not population-derived clinical limits. YES indicates optical and repeatability rules passed; a stable change can still be an artifact. No validated uncertainty interval, MAP calibration or clinical readiness score is claimed.

Weak or absent pulsatility is accepted when the inherited optical checks pass; unresolved HR is not required. The inherited HR search-range limitation described in the reference README remains. The green channel is supplemental for tuning but included in the distance; failure of green signal can prevent a usable feature vector.

| Command | Behavior |
|---|---|
| START / BASELINE / CALIBRATE | Clear prior baseline, tune, capture six windows |
| MEASURE | Fixed-drive, three-window relative optical comparison |
| LIVEPLOT / CONTINUOUS | Continuous PPG visualization; STOP + Enter returns to the menu |
| PLOT | Normalized PPG; a valid baseline's settings stay fixed |
| RETUNE | Clear baseline, retune; run START before measuring |
| STATUS | Baseline availability and LED codes |
| CLEAR | Delete baseline after typing YES |
| CALPLOT | Explains that no reference graph exists |
| HELP | Command list |

No trained MAP model, pressure prior, cuffless absolute-pressure formula, or reference-input prompt exists in this firmware. If absolute pressure is required, the original reference workflow provides a subject-specific research fit; even that requires further evaluation before any validated pressure claim.

For a scrolling desktop graph, close the serial monitor and run `python tools/serial_plot.py --port COM5 --command LIVEPLOT` from the package root. Stop with the graph button or close the window. See the package README for rolling normalization and visualization limits.
