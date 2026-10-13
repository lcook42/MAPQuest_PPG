# Current three-channel RTF release

Normal RTF calibration now includes RED, IR and GREEN. See [THREE_CHANNEL_RTF.md](THREE_CHANNEL_RTF.md).
The description below covers the earlier two-channel search or optional standalone diagnostics.

# Three-channel arm diagnostics

Run Start_Diagnostics.cmd. It prefers the connected CP2102 ESP32 USB port, and
falls back to the saved Wi-Fi address when USB is absent or cannot be opened.
Multiple matching USB boards require disconnecting the extras. Do not run a
second recorder or Serial Monitor at the same time.

Keep the cuff fully deflated. This mode collects RED, IR and GREEN at 100 samples
per second with no baseline gate, occlusion/RTF decision, MAP estimate or automatic
current sweep. All three drive codes start at 32. The page permits manual bounded
adjustment (1–200 codes), saved alongside samples. Codes are not milliamps.

The page shows each channel independently centered/scaled, DC and detrended RMS.
RMS contains noise/movement and is not a pulse-strength or confidence measurement.
Raw CSV and session metadata are stored in Data/DIAGNOSTIC_timestamp.

USB/Wi-Fi selection occurs when launched. A connection loss stops the recording;
it does not silently splice a new transport into the same recording. Restart the
diagnostic launcher after changing cables or power. Independent board power is
required for wireless operation.

Firmware supports a separate DIAG multi-LED mode with RED/IR/GREEN slots and nine
bytes per sample. ARM is refused in this mode. The older START mode remains RED/IR
only and disables green. Return-to-flow analysis has not yet been validated or
extended to use green; use these recordings to investigate signal quality first.

Validation: firmware built/uploaded with flash verification; 37 automated tests
passed, including three-channel parsing and USB-first/Wi-Fi-fallback selection.
Actual USB data was received from all three channels. The three-channel Wi-Fi
path has not yet been exercised physically; previous two-channel Wi-Fi worked.
No physiological accuracy or anatomical representativeness is claimed.
