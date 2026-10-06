#!/usr/bin/env python3
"""Synthetic return-to-flow recordings (docs/SPEC.md section 6).

Generates IR + red counts and cuff pressure for a known ground-truth MAP, so
the pipeline can be validated before any hardware exists -- and so a change to
the analysis can be checked against a signal whose answer is known.

    python synth.py --map 90 --out ../data/synth_map90.csv
    python synth.py --cardiac --artificial-pulse     # pulsatile stress case
    python synth.py --no-filling                     # must produce NO onset
    python synth.py --desaturate --constant-volume   # two-wavelength check

Model: the cuff holds above systolic, then bleeds down linearly. Forearm blood
volume is flat until cuff pressure falls below MAP_true, then ramps. Counts
follow from the Beer-Lambert relation the analysis inverts, so the pipeline
should recover MAP_true.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapquest.pipeline import COEFF_IR_880, COEFF_RED_660  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Haemoglobin extinction difference (Hb - HbO2) at each wavelength, cm^-1/M,
# from the Prahl/OMLC tables the SPEC references. The SIGN FLIP between them
# is the physical basis of the two-wavelength solve: desaturation raises
# absorption at 660 nm and lowers it at 880 nm.
DELTA_EPS_660 = 2906.0
DELTA_EPS_880 = -488.0

# Resting counts on a forearm, mid-range of the 18-bit scale per SPEC 3.1.
BASE_IR = 130_000.0
BASE_RED = 110_000.0
DARK_IR = 95.0
DARK_RED = 88.0


def build(args, rng: np.random.Generator):
    fs = args.fs
    n = int(args.seconds * fs)
    t = np.arange(n) / fs

    # -- Cuff pressure: hold, then linear deflation ------------------------
    p = np.where(
        t < args.hold_s,
        args.hold_mmhg,
        args.hold_mmhg - args.deflate_rate * (t - args.hold_s),
    )
    p = np.maximum(p, 0.0)

    # -- Blood volume: flat until cuff drops below MAP, then fills ---------
    # dV*L in micrometres, the quantity the analysis reconstructs.
    below = p < args.map
    dv_true = np.zeros(n)
    if below.any() and not args.no_filling:
        start = int(np.argmax(below))
        ramp = (t - t[start]) * args.fill_rate
        dv_true = np.where(below, np.maximum(ramp, 0.0), 0.0)
    if args.constant_volume:
        dv_true = np.zeros(n)

    # -- Invert the two-wavelength relation to get per-channel delta-OD ----
    # The analysis computes dV = 229*dOD_red + 1555*dOD_ir. We need a split
    # that reproduces dv_true. Healthy oxygenated blood puts most of the
    # weight on IR, so distribute by the inverse of the coefficients with a
    # fixed, physiologically plausible red:IR optical-density ratio.
    od_ratio = 0.35  # dOD_red / dOD_ir for well-oxygenated blood
    d_od_ir = dv_true / (COEFF_IR_880 + COEFF_RED_660 * od_ratio)
    d_od_red = od_ratio * d_od_ir

    # -- Optional desaturation of the trapped blood ------------------------
    # As SpO2 falls at constant total haemoglobin, Hb rises and HbO2 falls.
    # At 660 nm Hb absorbs far more than HbO2, so absorption climbs steeply.
    # At 880 nm HbO2 absorbs slightly more, so absorption falls a little. The
    # two-wavelength solve is built to cancel this pair; the IR-only fallback
    # cannot tell it from blood arriving.
    #
    # The ratio between the two wavelengths is what decides whether the solve
    # cancels, so it has to come from real extinction coefficients rather than
    # being picked freely -- otherwise the test checks nothing.
    if args.desaturate:
        frac = np.clip(t / max(t[-1], 1e-9), 0.0, 1.0)
        sat = 0.98 + (args.desat_to - 0.98) * frac
        desat = 0.98 - sat                      # 0 -> 0.28 over the record
        d_od_660 = desat * (args.desat_od660 / max(0.98 - args.desat_to, 1e-9))
        d_od_red = d_od_red + d_od_660
        d_od_ir = d_od_ir + d_od_660 * (DELTA_EPS_880 / DELTA_EPS_660)

    # -- Counts from delta-OD ---------------------------------------------
    ir = BASE_IR * np.exp(-d_od_ir)
    red = BASE_RED * np.exp(-d_od_red)

    # -- Cardiac component (healthy subject) -------------------------------
    if args.cardiac:
        card = np.sin(2 * np.pi * args.cardiac_hz * t)
        # Pulsatility collapses under occlusion and returns as the cuff releases.
        gate = np.clip((args.map + 15.0 - p) / 30.0, 0.0, 1.0)
        ir *= 1.0 + args.cardiac_frac * card * gate
        red *= 1.0 + args.cardiac_frac * card * gate

    # -- HeartMate 3 style artificial pulse, 0.5 Hz ------------------------
    # The 2.0 s Savitzky-Golay window is chosen to cancel exactly this.
    if args.artificial_pulse:
        ap = np.sin(2 * np.pi * 0.5 * t)
        ir *= 1.0 + args.ap_frac * ap
        red *= 1.0 + args.ap_frac * ap

    # -- Slow instrumental drift -------------------------------------------
    if args.drift:
        drift = args.drift * np.sin(2 * np.pi * 0.008 * t + 0.7)
        ir *= 1.0 + drift
        red *= 1.0 + drift

    # -- Noise and dark offset ---------------------------------------------
    ir = ir + DARK_IR + rng.normal(0.0, args.noise, n)
    red = red + DARK_RED + rng.normal(0.0, args.noise, n)

    ir = np.clip(np.rint(ir), 0, (1 << 18) - 1).astype(np.int64)
    red = np.clip(np.rint(red), 0, (1 << 18) - 1).astype(np.int64)

    # -- Pressure channel at its own ~40 Hz --------------------------------
    p_idx = (t * args.p_fs).astype(np.int64)
    p_raw = np.rint(args.p_raw0 + p * args.p_counts_per_mmhg).astype(np.int64)

    return t, ir, red, p, p_idx, p_raw, dv_true


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--fs", type=float, default=100.0)
    ap.add_argument("--map", type=float, default=90.0, help="ground-truth MAP, mmHg")
    ap.add_argument("--hold-mmhg", type=float, default=150.0)
    ap.add_argument("--hold-s", type=float, default=10.0)
    ap.add_argument("--deflate-rate", type=float, default=2.0, help="mmHg/s")
    ap.add_argument("--fill-rate", type=float, default=3.0,
                    help="dV*L ramp slope, um/s, once filling starts")
    ap.add_argument("--noise", type=float, default=40.0, help="count std dev")
    ap.add_argument("--drift", type=float, default=0.0, help="fractional slow drift")
    ap.add_argument("--cardiac", action="store_true")
    ap.add_argument("--cardiac-hz", type=float, default=1.2)
    ap.add_argument("--cardiac-frac", type=float, default=0.008)
    ap.add_argument("--artificial-pulse", action="store_true",
                    help="HeartMate 3 style 0.5 Hz component")
    ap.add_argument("--ap-frac", type=float, default=0.004)
    ap.add_argument("--no-filling", action="store_true",
                    help="never fill; the pipeline must report no onset")
    ap.add_argument("--constant-volume", action="store_true")
    ap.add_argument("--desaturate", action="store_true",
                    help="trapped blood desaturates 98%% -> --desat-to")
    ap.add_argument("--desat-to", type=float, default=0.70,
                    help="final SpO2 fraction under occlusion")
    ap.add_argument("--desat-od660", type=float, default=0.03,
                    help="delta-OD at 660 nm for the full desaturation")
    ap.add_argument("--p-fs", type=float, default=40.0)
    ap.add_argument("--p-raw0", type=float, default=1_200_000.0)
    ap.add_argument("--p-counts-per-mmhg", type=float, default=4200.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    t, ir, red, p, p_idx, p_raw, dv_true = build(args, rng)

    out = args.out or DATA_DIR / f"synth_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w") as fh:
        fh.write("# firmware=synthetic\n")
        fh.write(f"# fs={args.fs:.2f}\n")
        fh.write("# mode=spo2 channels=ir,red\n")
        fh.write("# resolution_bits=18\n")
        fh.write(f"# dark_ir={DARK_IR:.0f} dark_red={DARK_RED:.0f}\n")
        fh.write("# pressure=synthetic\n")
        fh.write("# columns=idx,t_us,ir,red,p_idx,p_raw\n")
        fh.write(f"# synthetic=true map_true={args.map} deflate_rate={args.deflate_rate}\n")
        fh.write(f"# hold_mmhg={args.hold_mmhg} hold_s={args.hold_s} "
                 f"fill_rate={args.fill_rate}\n")
        fh.write(f"# p_raw0={args.p_raw0} p_counts_per_mmhg={args.p_counts_per_mmhg}\n")
        fh.write(f"# cardiac={int(args.cardiac)} artificial_pulse={int(args.artificial_pulse)} "
                 f"desaturate={int(args.desaturate)} no_filling={int(args.no_filling)}\n")
        fh.write("idx,t_us,ir,red,p_idx,p_raw\n")
        us = (t * 1e6).astype(np.int64)
        for i in range(len(t)):
            fh.write(f"{i},{us[i]},{ir[i]},{red[i]},{p_idx[i]},{p_raw[i]}\n")

    onset_t = float(t[np.argmax(p < args.map)]) if (p < args.map).any() else float("nan")
    print(f"Wrote {len(t):,} samples ({args.seconds:g} s @ {args.fs:g} Hz) to {out}")
    print(f"  ground truth: MAP = {args.map:g} mmHg at t = {onset_t:.2f} s")
    print(f"\nNext: python analyze.py {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
