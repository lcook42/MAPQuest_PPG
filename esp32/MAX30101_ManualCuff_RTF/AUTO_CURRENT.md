# Current three-channel RTF release

Normal RTF calibration now includes RED, IR and GREEN. See [THREE_CHANNEL_RTF.md](THREE_CHANNEL_RTF.md).
The description below covers the earlier two-channel search or optional standalone diagnostics.

# Pulse-quality-based LED current calibration

Calibrate first adjusts optical brightness, then compares up to five current
combinations: initial, red -25%, IR -25%, red +20%, IR +20%. Settings remain within
the existing 1–200 register limit. These values are register codes, not milliamps.
Each setting is evaluated over two separate four-second windows after settling.
The score uses pulse fit, repetition, channel agreement and timing, with priority
for settings already meeting baseline quality criteria. Brightness alone earns
no quality score. This is a bounded local search, not a guaranteed global optimum.

An acceptable candidate can stop the search early. Otherwise the best usable
candidate is selected, LEDs are frozen, and three NEW stable baseline windows
are required. Search windows never count as baseline confirmation. Invalid
signals at every setting abort calibration. A sample gap during quality search
aborts the session. No current changes are allowed in an armed cuff phase.

Keep the cuff fully deflated throughout setup, which can take about a minute.
The setup does not turn green on or alter the RED/IR pulse-fit thresholds.
It does not guarantee a baseline pass or physiological accuracy on the arm.

33 tests passed, including current selection from synthetic noisy/clean signals,
no baseline reuse, flatline rejection and sequence-gap rejection during search.
The host update uses existing firmware LED commands; no reflash was required.
Logs now contain LED_QUALITY records with settings, raw windows and features.
