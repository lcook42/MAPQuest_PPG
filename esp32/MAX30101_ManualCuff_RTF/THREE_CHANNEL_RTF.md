# Three-channel calibration integrated with RTF

Release: 2026-10-12-r3-three. Target: current ESP32 DevKit V1 + MAX30101.

Run **Start_Forearm_Auto.cmd** for the current forearm placement. It prefers the
connected CP210x USB board and uses the saved Wi-Fi address if USB cannot be opened.
The dashboard reports the actual transport. There is no automatic transport
switch during a trial: a lost connection stops detection and requires a restart.
In automatic mode, Reset session after a failed USB session tries the saved Wi-Fi
address and keeps Wi-Fi for subsequent resets in that app run. Restarting the app
tries USB first again. A transport change always creates a fresh, uncalibrated
session. A new recording also resets and reconfigures the MAX30101 sensor itself.

Press **Calibrate** with the cuff fully deflated and the sensor stationary.

1. All three wavelengths are recorded at 100 samples per second. Each LED receives
   an independent brightness adjustment within drive codes 1–200.
2. Up to seven current configurations are compared: the brightness-adjusted setting,
   then each individual emitter at 75% and 120% of that setting. These are bounded
   candidates, not an exhaustive search of every possible setting.
3. At each setting, two separate four-second windows test RED/IR, RED/GREEN, and
   IR/GREEN. Rankings use the weaker window's pulse fit, repetition, channel agreement
   and frequency agreement. Qualified pairs rank ahead of unqualified pairs.
   Qualification also requires stable amplitude and frequency across both windows.
4. The best qualifying pair and all three LED currents are frozen. Three fresh
   four-second baseline windows must pass before the cuff-phase button is enabled.
   A failed search stays unarmed; it does not relax thresholds to force a pass.
5. The same selected pair is used for baseline, sustained pulse suppression, return
   confirmation, and recovery. No channel switching or LED retuning occurs during
   the cuff phase. The third channel remains recorded and visible.

The complete calibration typically takes 1–2 minutes. The dashboard shows separate
RED/IR/GREEN traces, drive codes, pulse amplitude, pulse fit, repetition, selected pair,
and search progress. Pair agreement and pulse-model fit are different measurements;
neither percentage represents accuracy. A drive code is not a current in milliamps.

## Reset and recovery controls

**Reset session** closes the current recording, clears calibration, results,
annotations, timers and live plots, reconnects to the recorder, and opens a new
timestamped recording folder. Previous recordings are preserved. It also recovers
from a stopped or failed connection. If a cuff phase was active, first physically
release the cuff and press **Cuff is fully released**; reset is then available.

**Skip recovery wait** removes the remaining 60-second software countdown after
release acknowledgement. It does not start a cuff phase or accept a baseline.
Recalibration is still required. The override is logged. Each subsequent release
starts its normal countdown again. Skipping the timer is an operator override,
not a measurement that physiological recovery has occurred.

## Waveform display and noise information

The raw chart removes each channel's DC offset and uses a shared scale. A second
chart removes a straight-line trend from the same samples, without smoothing.
Each chart labels its own ADC-count scale and time span. These displays do not
change the detector. Plots reset when LED current changes and omit the initial
0.5-second settling samples; the CSV still saves all received raw samples.

Calibration also reports slow content below 0.5 Hz and high-frequency content
above 8 Hz after linear detrending. These are descriptive energy fractions, not
validated signal-to-noise ratios: motion can overlap pulse frequencies. They do
not override any pulse-quality gate. An offline low-pass experiment on the
2026-10-12 calibration did not make IR meet its pulse-fit gate; smoothing alone
was insufficient. No adaptive filter or relaxed pulse threshold was enabled.

## Reading quality

Use consistent flat sensor contact and light shielding with the arm supported.
Changing placement or attachment requires a new calibration. A weak/clipped third
channel does not veto a usable pair; fewer than two usable channels cannot qualify.
Sequential current trials can be affected by movement or changing contact. Hold the
mount stable throughout; the fresh baseline is a further check after selection.

The current pulse model and engineering thresholds are retained. Better channel
selection increases the available setup choices; physiological RTF accuracy still
requires recordings compared with an independent flow reference. This prototype
reports PPG pulse return, not MAP or an exact opening pressure. Confirmation uses
two non-overlapping four-second windows and therefore has a detection delay.

## Records and reproduction

Each session's Data folder contains all three raw ADC channels and LED codes in
`samples.csv`, a wire log, and replayable detector windows in `replay.jsonl`.
`calibration.json` records each setting/pair evaluation and the selected result,
including failures. `result.json`, when present, identifies the selected channels.
Replay with `python PlatformIO/tools/replay.py <session>/replay.jsonl` using this
same release. Old release logs require their original detector version.

The Arduino sketch is the firmware source; PlatformIO copies it before building.
Upload the matching firmware before running this dashboard. The legacy two-channel
path remains for regression testing; normal launchers use three-channel RTF.
Start_Diagnostics.cmd remains available for standalone exploration, but green is
now included in normal RTF calibration and detection selection.

## Hardware reference

The multi-LED acquisition uses RED, IR, GREEN slots in that order, mode 7, at
100 samples/s with 411 µs pulse width and 18-bit conversion. See the
[MAX30101 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/max30101.pdf)
for LED slots, timing and FIFO format. These acquisition settings do not establish
physiological detection accuracy.
