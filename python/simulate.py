#!/usr/bin/env python3
"""Generate a synthetic PPG recording in the same format as record.py.

Useful for exercising the pipeline before the hardware is wired, and for
sanity-checking that a change to the derivation chain still recovers a known
ground truth.

    python simulate.py                        # 30 s, writes to data/
    python simulate.py --seconds 60 --bpm 72
    python simulate.py --noise 3.0            # stress the derivative stages

The beat model is a systolic Gaussian plus a smaller, later diastolic Gaussian,
which produces a dicrotic notch between them. It is a caricature, not a
physiological model -- good enough to verify that d2V/dt2 recovers the notch.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Per-channel character: green has a higher AC/DC ratio and less low-frequency
# wander than IR, matching what the literature predicts for reflectance PPG.
# `noise` is the white-noise standard deviation in ADC counts, chosen to be
# plausible for 18-bit conversions with 2x hardware averaging.
CHANNEL_PROFILE = {
    "green": {"dc": 95_000.0, "ac_frac": 0.010, "drift": 0.010, "noise": 35.0},
    "ir":    {"dc": 130_000.0, "ac_frac": 0.004, "drift": 0.025, "noise": 60.0},
}


def beat_waveform(phase: np.ndarray) -> np.ndarray:
    """One cardiac cycle over phase in [0, 1). Systolic + diastolic peaks."""
    systolic = np.exp(-0.5 * ((phase - 0.17) / 0.055) ** 2)
    diastolic = 0.38 * np.exp(-0.5 * ((phase - 0.42) / 0.085) ** 2)
    return systolic + diastolic


def synthesise(fs: float, seconds: float, bpm: float, noise: float,
               rng: np.random.Generator) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    n = int(seconds * fs)
    t = np.arange(n) / fs

    # Heart rate wanders slightly, plus respiratory sinus arrhythmia at ~0.25 Hz.
    rate_hz = bpm / 60.0
    rsa = 0.04 * np.sin(2 * np.pi * 0.25 * t)
    wander = 0.02 * np.sin(2 * np.pi * 0.013 * t + 1.1)
    inst_rate = rate_hz * (1.0 + rsa + wander)
    phase = np.cumsum(inst_rate) / fs
    cycle = beat_waveform(np.mod(phase, 1.0))
    cycle -= cycle.mean()

    channels: dict[str, np.ndarray] = {}
    for name, p in CHANNEL_PROFILE.items():
        # Baseline: slow contact-pressure drift plus a respiratory component.
        baseline = p["dc"] * (
            1.0
            + p["drift"] * np.sin(2 * np.pi * 0.021 * t + 0.4)
            + 0.3 * p["drift"] * np.sin(2 * np.pi * 0.25 * t)
        )
        ac = p["dc"] * p["ac_frac"] * cycle
        white = rng.normal(0.0, noise * p["noise"], n)
        sig = baseline + ac + white
        channels[name] = np.clip(np.rint(sig), 0, (1 << 18) - 1).astype(np.int64)

    return t, channels


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--fs", type=float, default=200.0)
    ap.add_argument("--bpm", type=float, default=68.0)
    ap.add_argument("--noise", type=float, default=1.0,
                    help="noise multiplier; >2 starts to challenge d2V/dt2")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    t, channels = synthesise(args.fs, args.seconds, args.bpm, args.noise, rng)
    names = list(channels)

    out = args.out or DATA_DIR / (
        f"sim_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="") as fh:
        fh.write("# mapquest-ppg v0 (SIMULATED)\n")
        fh.write(f"# fs={args.fs:.2f}\n")
        fh.write(f"# channels={','.join(names)}\n")
        fh.write("# adc_range_na=4096 pulse_width_us=411 resolution_bits=18\n")
        fh.write(f"# internal_sps={int(args.fs * 2)} smp_ave=2\n")
        fh.write(f"# simulated=true bpm={args.bpm} noise={args.noise} seed={args.seed}\n")
        fh.write("idx,us," + ",".join(names) + "\n")
        us = (t * 1e6).astype(np.int64)
        for i in range(len(t)):
            fh.write(f"{i},{us[i]}," + ",".join(str(channels[n][i]) for n in names) + "\n")

    print(f"Wrote {len(t):,} samples ({args.seconds:g} s @ {args.fs:g} Hz) to {out}")
    print(f"\nNext: python analyze.py {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
