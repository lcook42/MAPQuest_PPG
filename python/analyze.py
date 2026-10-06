#!/usr/bin/env python3
"""Offline analysis: load a recording, run the derivation chain, plot every stage.

    python analyze.py data/ppg_20261005_143022.csv
    python analyze.py <csv> --start 10 --duration 8    # zoom a clean window
    python analyze.py <csv> --channel green            # one channel only
    python analyze.py <csv> --save out.png

Prints per-channel quality metrics (perfusion index, derivative SNR) so you can
settle green vs IR on measurement rather than on literature -- see
docs/led-selection-report.md section 6.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from mapquest.pipeline import (
    STAGES,
    Derivation,
    PipelineConfig,
    derive,
    format_metrics,
    quality_metrics,
)
from mapquest.serial_io import load_csv

COLORS = {"green": "#2e9e4f", "ir": "#c43b2f", "red": "#d1495b"}


# Raw counts and the DC baseline live at wildly different absolute levels per
# channel (green ~95k, IR ~130k). Sharing one y-axis flattens both into
# straight lines and hides the drift you need to see, so these two stages get
# a per-channel axis instead.
_PER_CHANNEL_AXIS = {"raw", "dc"}


def plot_stages(derivations: list[Derivation], header, title: str,
                save: Path | None = None) -> None:
    """One row per derivation stage, channels overlaid, shared time axis."""
    n = len(STAGES)
    fig, axes = plt.subplots(n, 1, figsize=(12, 1.6 * n + 1.5), sharex=True)
    fig.suptitle(title, fontsize=11, fontweight="bold")

    for ax, (key, label, unit) in zip(axes, STAGES):
        if key in _PER_CHANNEL_AXIS and len(derivations) > 1:
            # First channel on the left spine, the rest on stacked right spines,
            # each tick label coloured to match its trace.
            target = ax
            for i, d in enumerate(derivations):
                if i > 0:
                    target = ax.twinx()
                    target.spines["right"].set_position(("outward", 42 * (i - 1)))
                    target.margins(x=0)
                color = COLORS.get(d.name)
                target.plot(d.t, d.stage(key), lw=0.9, label=d.name, color=color)
                target.tick_params(axis="y", labelsize=7, colors=color or "k")
                if i > 0:
                    target.set_ylabel(d.name, fontsize=7, color=color)
        else:
            for d in derivations:
                ax.plot(d.t, d.stage(key), lw=0.9, label=d.name,
                        color=COLORS.get(d.name))

        ax.set_ylabel(f"{label.split('. ', 1)[-1]}\n[{unit}]", fontsize=7.5)
        ax.grid(alpha=0.25, lw=0.5)
        ax.tick_params(labelsize=7.5)
        ax.margins(x=0)
        if key in ("ac", "norm", "filt", "d1", "d2"):
            ax.axhline(0, color="k", lw=0.4, alpha=0.4)

    # The DC panel is more informative with raw overlaid for scale comparison.
    axes[1].set_title("DC tracks baseline drift; a steadily sloping DC means "
                      "contact pressure is still settling",
                      fontsize=6.5, loc="left", color="#666", pad=2)
    axes[6].set_title("d2V/dt2 (APG): the dicrotic notch appears as the e-wave",
                      fontsize=6.5, loc="left", color="#666", pad=2)

    # The raw panel's legend would only list its own left-spine channel, so
    # build the legend from the first panel where all channels share an axis.
    shared = next(i for i, (k, _, _) in enumerate(STAGES) if k not in _PER_CHANNEL_AXIS)
    handles, labels = axes[shared].get_legend_handles_labels()
    axes[0].legend(handles, labels, loc="upper right", fontsize=8,
                   ncol=len(derivations))
    axes[-1].set_xlabel("time (s)", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.975))

    if save:
        fig.savefig(save, dpi=140, bbox_inches="tight")
        print(f"\nSaved plot to {save}")
    plt.show()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="recording from record.py")
    ap.add_argument("--channel", action="append",
                    help="restrict to this channel (repeatable)")
    ap.add_argument("--start", type=float, default=0.0, help="window start, seconds")
    ap.add_argument("--duration", type=float, help="window length, seconds")
    ap.add_argument("--invert", action="store_true",
                    help="flip sign so the systolic upstroke is positive")
    ap.add_argument("--sg-window", type=float, default=0.15,
                    help="Savitzky-Golay window, seconds (longer = smoother derivatives)")
    ap.add_argument("--band", type=float, nargs=2, default=(0.5, 8.0),
                    metavar=("LO", "HI"), help="band-pass corners, Hz")
    ap.add_argument("--save", type=Path, help="write the figure to this path")
    ap.add_argument("--no-show", action="store_true", help="skip the interactive window")
    args = ap.parse_args()

    header, t, channels = load_csv(args.csv)

    if args.channel:
        missing = [c for c in args.channel if c not in channels]
        if missing:
            ap.error(f"channel(s) {missing} not in recording; have {list(channels)}")
        channels = {c: channels[c] for c in args.channel}

    # Window selection.
    mask = t >= args.start
    if args.duration:
        mask &= t < (args.start + args.duration)
    if mask.sum() < 64:
        ap.error(f"window contains only {mask.sum()} samples; need at least 64")
    t_win = t[mask] - t[mask][0]
    channels = {k: v[mask] for k, v in channels.items()}

    cfg = PipelineConfig(
        band_hz=(args.band[0], args.band[1]),
        sg_window_s=args.sg_window,
        invert=args.invert,
    )

    print(f"File     : {args.csv}")
    print(f"Config   : {header.describe()}")
    print(f"Window   : {t_win[-1]:.1f} s, {len(t_win):,} samples, "
          f"channels {', '.join(channels)}")

    derivations = [
        derive(v, header.fs, name=name, t=t_win, config=cfg)
        for name, v in channels.items()
    ]

    print("\nQuality metrics")
    print("-" * 66)
    metrics = {}
    for d in derivations:
        m = quality_metrics(d, full_scale=header.full_scale)
        metrics[d.name] = m
        print(format_metrics(d.name, m))

    # Channel comparison — the green-vs-IR decision in one line.
    if len(metrics) > 1:
        print("\nChannel comparison")
        print("-" * 66)
        best_pi = max(metrics, key=lambda k: metrics[k]["perfusion_index_pct"])
        best_snr = max(metrics, key=lambda k: metrics[k]["snr_d2_db"])
        print(f"  Strongest pulsatile signal (perfusion index): {best_pi}")
        print(f"  Cleanest 2nd derivative (SNR on d2V/dt2)    : {best_snr}")
        if best_pi == best_snr:
            print(f"\n  -> {best_pi} wins on both. Consider dropping the other channel and")
            print(f"     spending the timing budget on 400 Hz single-channel instead.")
        else:
            print("\n  -> Split decision. Keep both channels for now and compare again")
            print("     on a longer, stiller recording.")

    if not args.no_show or args.save:
        title = f"MAPQuest derivation chain — {args.csv.name} — {header.describe()}"
        plot_stages(derivations, header, title, save=args.save)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
