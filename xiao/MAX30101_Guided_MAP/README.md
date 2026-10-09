# Guided reference-MAP project

This is a direct PlatformIO port of the supplied Guided MAP Arduino sketch. The original MAP logic is retained, with a new continuous plot command. The runnable Arduino copy matches `src/main.cpp`; the byte-identical upload is preserved separately under the package's `original/` folder. No synthetic reference points or replacement regression model were introduced. See the package README for wiring, installation and graphs.

## Guided use

1. Upload and open the monitor at 115200 with newline-terminated input.
2. Type `START` (or `CALIBRATE`). Follow placement prompts. The firmware measures the dark baseline and independently tunes red, IR and green.
3. Complete 12 calibration points, each with three accepted optical windows, entering the measured reference MAP when prompted. Keep reference timing, site and contact consistent. Do not deliberately alter pressure to meet a software target span.
4. The firmware selects ridge regularization by leave-one-out cross-validation, then asks for three held-out validation points with reference MAP inputs.
5. The model and calibration graph are saved in ESP32 Preferences namespace `mapproto`, key `model`, with the original version 3 layout.
6. Type `CALPLOT` to inspect reference and predicted values. Type `MEASURE` for the guided multi-window prediction. Read the result's explicit `COUNT THIS MEASUREMENT: YES/NO` and any warning.

| Command | Behavior |
|---|---|
| START / CALIBRATE | Full 12-point fit and 3-point validation |
| MEASURE | Three requested windows; up to five accepted windows if initial spread is high |
| RETUNE | Guided optical retune |
| LIVEPLOT / CONTINUOUS | Continuous PPG visualization; STOP + Enter returns to the menu |
| PLOT | Capture normalized red/IR/green PPG |
| CALPLOT | Saved reference-versus-prediction traces |
| STATUS | Model readiness, errors, range and LED codes |
| CLEAR | Delete reference calibration after typing YES |
| HELP | Command menu |

The original optical checks include signal level, saturation, drift, motion heuristics and quality. Weak or absent HR is allowed. The model uses eight features, standardization and ridge regularization; configured MAP hard bounds are 35–180 mmHg. Precalibration output is an 85 mmHg **prior**, and rejected output can be a held value: neither is a new pressure measurement.

## Parity and inherited limitations

The original retunes before measurements even though DC features depend on LED drive; retuning can change the feature distribution relative to training. Readiness and optical quality are heuristic scores, not accuracy probabilities. Three held-out points do not constitute clinical validation. The HR estimator only searches lags corresponding approximately to its 35–200 bpm bounds, so its separate out-of-range rejection cannot reliably detect all rates outside that search range; harmonic errors are also possible.

The source has user-paced retry loops with no cancel command, an unbounded additional-window retry path when valid optical windows produce rejected predictions, basic flash compatibility checks without a checksum, and numeric input parsing through a 40-byte buffer. A rejected final aggregate can still update the original held-value state. These behaviors are documented rather than silently changed, to retain requested parity. A failing acquisition or tuning path may return before the final `COUNT` summary, but prints its failure reason. Use reset if a guided workflow stalls, and do not interpret priors or held values as measurements.

This project preserves a research prototype, not a validated blood-pressure device. Confirm against the intended reference method in any research evaluation; firmware acceptance alone is insufficient.

For a scrolling desktop graph, close the serial monitor and run `python tools/serial_plot.py --port COM5 --command LIVEPLOT` from the package root. Stop with the graph button or close the window. See the package README for rolling normalization and visualization limits.
