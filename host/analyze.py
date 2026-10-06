#!/usr/bin/env python3
"""Return-to-flow analysis (docs/SPEC.md section 5).

    python analyze.py ../data/synth_map90.csv
    python analyze.py <csv> --t-occ0 5 --t-occ1 10     # explicit reference window
    python analyze.py <csv> --ir-only                  # single-wavelength fallback
    python analyze.py <csv> --k 3 --hold 2.0           # onset sensitivity
    python analyze.py <csv> --all-stages               # every intermediate stage

Prints onset time and pressure, the noise floor, and pulsatility; renders the
stacked figure; appends a row to data/results.csv.
"""

from __future__ import annotations

import argparse
import csv as csvmod
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapquest.io import check_continuity, load_calibration, load_csv  # noqa: E402
from mapquest.pipeline import (  # noqa: E402
    STAGES,
    PipelineConfig,
    calibrate_pressure,
    derive,
    pulsatility_rms,
    resample_pressure,
)

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent / "data"
# IR is invisible light, so its colour is free; a blue keeps it clearly
# distinct from the red channel, which should read as red.
COLORS = {"ir": "#1f4e79", "red": "#c43b2f"}


def plot_result(d, pressure_mmhg, meta, title, all_stages: bool,
                save: Path | None, show: bool) -> None:
    """SPEC 5.2: four stacked panels, or every stage with --all-stages."""
    if all_stages:
        keys = [k for k, _, _ in STAGES]
    else:
        keys = ["raw", "dv_lp", "ddv"]

    n_panels = len(keys) + (1 if pressure_mmhg is not None else 0)
    fig, axes = plt.subplots(n_panels, 1, figsize=(12, 1.9 * n_panels + 1.2),
                             sharex=True)
    axes = np.atleast_1d(axes)
    fig.suptitle(title, fontsize=11, fontweight="bold")

    labels = {k: lbl for k, lbl, _ in STAGES}
    units = {k: u for k, _, u in STAGES}
    onset_t = d.onset_time

    for ax, key in zip(axes, keys):
        value = d.stage(key)
        if isinstance(value, dict):
            # Per-wavelength stage: IR on the left spine, red on a right spine,
            # since their absolute levels differ.
            target = ax
            for i, (name, series) in enumerate(value.items()):
                if i > 0:
                    target = ax.twinx()
                target.plot(d.t, series, lw=0.9, label=name, color=COLORS.get(name))
                target.tick_params(axis="y", labelsize=7,
                                   colors=COLORS.get(name, "k"))
                if i > 0:
                    target.set_ylabel(name, fontsize=7, color=COLORS.get(name))
            ax.legend(
                handles=[plt.Line2D([], [], color=COLORS.get(nm), label=nm)
                         for nm in value],
                loc="upper right", fontsize=7, ncol=len(value),
            )
        else:
            ax.plot(d.t, value, lw=1.0, color="#1f4e79")

        ax.set_ylabel(f"{labels[key].split('. ', 1)[-1]}\n[{units[key]}]", fontsize=7.5)
        ax.grid(alpha=0.25, lw=0.5)
        ax.tick_params(labelsize=7.5)
        ax.margins(x=0)

        # Reference window shading on every panel.
        ax.axvspan(d.t[d.ref_slice.start], d.t[d.ref_slice.stop - 1],
                   color="#888", alpha=0.12, lw=0)

        if key == "ddv":
            ax.axhline(d.threshold, color="#e07b00", lw=1.0, ls="--",
                       label=f"mu0 + {d.config.k_sigma:g}sigma0")
            ax.axhline(d.mu0, color="#888", lw=0.6, ls=":")
            ax.legend(loc="upper left", fontsize=7)
        if onset_t is not None:
            ax.axvline(onset_t, color="#0a7d2c", lw=1.2, alpha=0.85)

    if pressure_mmhg is not None:
        ax = axes[-1]
        ax.plot(d.t, pressure_mmhg, lw=1.1, color="#5b3fa8")
        ax.set_ylabel("Cuff pressure\n[mmHg]", fontsize=7.5)
        ax.grid(alpha=0.25, lw=0.5)
        ax.tick_params(labelsize=7.5)
        ax.margins(x=0)
        ax.axvspan(d.t[d.ref_slice.start], d.t[d.ref_slice.stop - 1],
                   color="#888", alpha=0.12, lw=0)
        if onset_t is not None:
            p_on = float(np.interp(onset_t, d.t, pressure_mmhg))
            ax.axvline(onset_t, color="#0a7d2c", lw=1.2)
            ax.plot([onset_t], [p_on], "o", color="#0a7d2c", ms=7, zorder=5)
            ax.annotate(f"  onset: {p_on:.1f} mmHg", (onset_t, p_on),
                        fontsize=9, color="#0a7d2c", fontweight="bold",
                        va="bottom")

    axes[-1].set_xlabel("time (s)", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.975))

    if save:
        fig.savefig(save, dpi=140, bbox_inches="tight")
        print(f"\nSaved figure to {save}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path)
    ap.add_argument("--t-occ0", type=float, help="reference window start, s")
    ap.add_argument("--t-occ1", type=float, help="reference window end, s")
    ap.add_argument("--deflation-start", type=float,
                    help="start the onset search here, s (default: end of reference window)")
    ap.add_argument("--ir-only", action="store_true",
                    help="single-wavelength fallback instead of the two-wavelength solve")
    ap.add_argument("--k", type=float, default=3.0, help="onset threshold in sigma")
    ap.add_argument("--hold", type=float, default=2.0, help="sustain time for onset, s")
    ap.add_argument("--onset-method", choices=("intersect", "threshold"),
                    default="intersect",
                    help="'threshold' is SPEC 5.1 verbatim but reads ~2.6 mmHg high "
                         "due to symmetric-filter corner smearing; 'intersect' "
                         "extrapolates the fitted ramp back to baseline (default)")
    ap.add_argument("--sg-window", type=float, default=2.0,
                    help="Savitzky-Golay derivative window, s")
    ap.add_argument("--lowpass", type=float, default=0.3, help="low-pass corner, Hz")
    ap.add_argument("--calibration", type=Path, default=HERE / "calibration.json")
    ap.add_argument("--all-stages", action="store_true",
                    help="plot every intermediate stage, not just the SPEC 5.2 four")
    ap.add_argument("--save", type=Path, help="figure path (default: next to the CSV)")
    ap.add_argument("--no-show", action="store_true")
    ap.add_argument("--no-results", action="store_true",
                    help="do not append to data/results.csv")
    args = ap.parse_args()

    meta, t, channels, extra = load_csv(args.csv)
    if "ir" not in channels:
        ap.error(f"recording has no IR channel; found {list(channels)}")

    print(f"File      : {args.csv}")
    print(f"Config    : {meta.describe()}")
    print(f"Duration  : {t[-1]:.1f} s, {len(t):,} samples")

    n_gaps, n_missing = check_continuity(extra["idx"])
    if n_gaps:
        print(f"WARNING   : {n_gaps} index discontinuity/ies, {n_missing} samples missing "
              "(SPEC 3.5 requires zero)")
    else:
        print("Continuity: OK, no dropped samples")

    ref_window = None
    if args.t_occ0 is not None and args.t_occ1 is not None:
        ref_window = (args.t_occ0, args.t_occ1)

    # -- Pressure ----------------------------------------------------------
    pressure_mmhg = None
    cal = load_calibration(args.calibration)
    if "p_raw" in extra:
        p_resampled = resample_pressure(
            extra.get("p_idx", np.arange(len(t))), extra["p_raw"], len(t)
        )
        if meta.raw.get("synthetic") == "true":
            # Synthetic recordings carry their own exact calibration.
            raw0 = float(meta.raw["p_raw0"])
            cpm = float(meta.raw["p_counts_per_mmhg"])
            pressure_mmhg = (p_resampled - raw0) / cpm
        elif cal:
            pressure_mmhg = calibrate_pressure(
                p_resampled, cal["RAW0"], cal["RAW_REF"], cal["P_REF"]
            )
        else:
            print(f"NOTE      : pressure present but {args.calibration.name} missing — "
                  "run `python logger.py --calibrate` to report onset in mmHg")

    # If the deflation start was not given, infer it from the pressure trace:
    # the last sample at the plateau before pressure starts falling.
    deflation_start = args.deflation_start
    if deflation_start is None and pressure_mmhg is not None:
        dp = np.gradient(pressure_mmhg, t)
        falling = dp < -0.5
        if falling.any():
            deflation_start = float(t[int(np.argmax(falling))])
            print(f"Deflation : detected at t = {deflation_start:.2f} s")

    cfg = PipelineConfig(
        lowpass_hz=args.lowpass,
        sg_window_s=args.sg_window,
        k_sigma=args.k,
        hold_s=args.hold,
        ir_only=args.ir_only,
        onset_method=args.onset_method,
    )

    d = derive(
        channels, meta.fs, dark=meta.dark, ref_window=ref_window,
        deflation_start_s=deflation_start, t=t, config=cfg,
    )

    # -- Report ------------------------------------------------------------
    print("\nResult")
    print("-" * 62)
    method = "IR-only" if args.ir_only else "two-wavelength (660 + 880 nm)"
    print(f"  Method            : {method}")
    print(f"  Reference window  : {t[d.ref_slice.start]:.2f} – "
          f"{t[d.ref_slice.stop - 1]:.2f} s")
    if not meta.dark:
        print("  ! no dark offsets in header — run DARK before recording")
    print(f"  Noise floor mu0   : {d.mu0:+.5f} um/s")
    print(f"  Noise floor sigma0: {d.sigma0:.5f} um/s")
    print(f"  Threshold         : {d.threshold:+.5f} um/s "
          f"(mu0 + {args.k:g}sigma0, held {args.hold:g} s)")

    puls = pulsatility_rms(d.d_od["ir"][d.ref_slice], meta.fs, cfg.pulsatility_band)
    print(f"  Pulsatility RMS   : {puls:.6f} OD (0.5–5 Hz, reference window)")

    onset_pressure = float("nan")
    if d.onset_time is None:
        print("\n  ONSET             : none detected")
        if d.threshold_index is None:
            print("    No sustained excursion above threshold. Either the cuff never")
            print("    dropped below arterial pressure, or lower --k / --hold.")
        else:
            print(f"    Threshold crossed at t = {d.threshold_time:.2f} s, but the ramp")
            print("    fit did not extrapolate to a plausible onset. Inspect the figure;")
            print("    --onset-method threshold will report the raw crossing.")
    else:
        print(f"\n  ONSET ({args.onset_method:<9})  : t = {d.onset_time:.2f} s")
        if pressure_mmhg is not None:
            onset_pressure = float(np.interp(d.onset_time, t, pressure_mmhg))
            print(f"  ONSET PRESSURE    : {onset_pressure:.1f} mmHg")

        # Always show the other estimator so the difference stays visible.
        other_t = (d.threshold_time if args.onset_method == "intersect"
                   else d.onset_time_intersect)
        other_name = "threshold" if args.onset_method == "intersect" else "intersect"
        if other_t is not None:
            line = f"  (for comparison)  : {other_name} = {other_t:.2f} s"
            if pressure_mmhg is not None:
                line += f", {float(np.interp(other_t, t, pressure_mmhg)):.1f} mmHg"
            print(line)
        if np.isfinite(d.ramp_slope):
            print(f"  Fitted ramp slope : {d.ramp_slope:.3f} um/s")

        if meta.raw.get("map_true") and pressure_mmhg is not None:
            truth = float(meta.raw["map_true"])
            print(f"  Ground truth      : {truth:.1f} mmHg "
                  f"(error {onset_pressure - truth:+.2f} mmHg)")

    if puls > 1e-4:
        print("\n  ! Pulsatile subject: onset reads near SYSTOLIC, not MAP (SPEC 1).")

    # -- Figure ------------------------------------------------------------
    save = args.save or args.csv.with_suffix(".png")
    title = f"MAPQuest return-to-flow — {args.csv.name} — {meta.describe()}"
    plot_result(d, pressure_mmhg, meta, title, args.all_stages, save,
                show=not args.no_show)

    # -- Results row (SPEC 5.2) -------------------------------------------
    if not args.no_results:
        results = DATA_DIR / "results.csv"
        results.parent.mkdir(parents=True, exist_ok=True)
        new = not results.exists()
        with results.open("a", newline="") as fh:
            w = csvmod.writer(fh)
            if new:
                w.writerow(["analysed_at", "file", "method", "onset_s",
                            "onset_mmhg", "mu0", "sigma0", "threshold",
                            "pulsatility_rms", "k", "hold_s", "dropped_samples"])
            w.writerow([
                datetime.now().isoformat(timespec="seconds"), args.csv.name, method,
                f"{d.onset_time:.3f}" if d.onset_time is not None else "",
                f"{onset_pressure:.2f}" if np.isfinite(onset_pressure) else "",
                f"{d.mu0:.6f}", f"{d.sigma0:.6f}", f"{d.threshold:.6f}",
                f"{puls:.8f}", args.k, args.hold, n_missing,
            ])
        print(f"Appended summary row to {results}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
