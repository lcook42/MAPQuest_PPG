#!/usr/bin/env python3
"""Live scope showing every derivation stage, raw counts through d2V/dt2.

    python live_plot.py                  # all stages, all channels
    python live_plot.py --stages raw filt d1 d2
    python live_plot.py --window 8       # seconds of history

Use this to place the sensor and tune LED current before recording: watch the
raw DC level land mid-scale and the pulse appear in the filtered trace.

Note this re-filters the visible window each frame with ``filtfilt``, so edge
effects live at the left and right edges of the plot. Judge the signal from the
middle of the window.
"""

from __future__ import annotations

import argparse
from collections import deque

import matplotlib.pyplot as plt
import numpy as np

from mapquest.pipeline import STAGES, PipelineConfig, derive
from mapquest.serial_io import PPGStream, StreamHeader, find_port

_STAGE_KEYS = [k for k, _, _ in STAGES]


class LiveScope:
    """Rolling multi-panel view of the derivation chain."""

    def __init__(
        self,
        header: StreamHeader,
        window_s: float = 10.0,
        stages: list[str] | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        self.header = header
        self.fs = header.fs
        self.config = config or PipelineConfig()
        self.stages = [s for s in (stages or _STAGE_KEYS) if s in _STAGE_KEYS]
        self.labels = {k: lbl for k, lbl, _ in STAGES}
        self.units = {k: unit for k, _, unit in STAGES}

        self.maxlen = max(int(window_s * self.fs), 64)
        self.buffers = {ch: deque(maxlen=self.maxlen) for ch in header.channels}

        plt.ion()
        n = len(self.stages)
        self.fig, axes = plt.subplots(n, 1, figsize=(11, 1.5 * n + 1.5), sharex=True)
        self.axes = np.atleast_1d(axes)
        self.fig.suptitle(
            f"MAPQuest live — {header.describe()}", fontsize=10, fontweight="bold"
        )

        colors = {"green": "#2e9e4f", "ir": "#c43b2f", "red": "#d1495b"}
        self.lines: dict[tuple[str, str], plt.Line2D] = {}
        for ax, key in zip(self.axes, self.stages):
            for ch in header.channels:
                (line,) = ax.plot([], [], lw=1.0, label=ch,
                                  color=colors.get(ch, None))
                self.lines[(key, ch)] = line
            ax.set_ylabel(f"{self.labels[key].split('. ', 1)[-1]}\n[{self.units[key]}]",
                          fontsize=7)
            ax.grid(alpha=0.25, lw=0.5)
            ax.tick_params(labelsize=7)
            if key in ("ac", "norm", "filt", "d1", "d2"):
                ax.axhline(0, color="k", lw=0.4, alpha=0.4)

        self.axes[0].legend(loc="upper right", fontsize=7, ncol=len(header.channels))
        self.axes[-1].set_xlabel("time (s)", fontsize=8)
        self.fig.tight_layout(rect=(0, 0, 1, 0.97))
        plt.show(block=False)

    def push(self, values) -> None:
        for ch, v in zip(self.header.channels, values):
            self.buffers[ch].append(float(v))

    def refresh(self) -> None:
        if not plt.fignum_exists(self.fig.number):
            return

        n = min(len(b) for b in self.buffers.values())
        if n < 64:  # pipeline needs a minimum buffer
            return

        t = np.arange(n) / self.fs
        limits: dict[str, list[float]] = {k: [np.inf, -np.inf] for k in self.stages}

        for ch in self.header.channels:
            raw = np.fromiter(self.buffers[ch], dtype=float, count=len(self.buffers[ch]))[-n:]
            try:
                d = derive(raw, self.fs, name=ch, t=t, config=self.config)
            except ValueError:
                return
            for key in self.stages:
                y = d.stage(key)
                self.lines[(key, ch)].set_data(t, y)
                finite = y[np.isfinite(y)]
                if finite.size:
                    limits[key][0] = min(limits[key][0], float(finite.min()))
                    limits[key][1] = max(limits[key][1], float(finite.max()))

        for ax, key in zip(self.axes, self.stages):
            lo, hi = limits[key]
            if np.isfinite(lo) and np.isfinite(hi):
                pad = (hi - lo) * 0.08 or 1.0
                ax.set_ylim(lo - pad, hi + pad)
            ax.set_xlim(t[0], t[-1])

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

    def alive(self) -> bool:
        return plt.fignum_exists(self.fig.number)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--window", type=float, default=10.0, help="seconds of history")
    ap.add_argument("--stages", nargs="+", choices=_STAGE_KEYS, default=None,
                    help=f"subset of stages to show (default: all of {_STAGE_KEYS})")
    ap.add_argument("--invert", action="store_true",
                    help="flip sign so the systolic upstroke is positive")
    ap.add_argument("--refresh-every", type=int, default=20,
                    help="redraw after this many samples")
    args = ap.parse_args()

    port = find_port(args.port)
    print(f"Port : {port}")

    cfg = PipelineConfig(invert=args.invert)

    with PPGStream(port, baud=args.baud) as stream:
        print(f"Config: {stream.header.describe()}")
        print("\nClose the plot window or press Ctrl-C to stop.")
        print("Tune LED current from a serial monitor: 'g<n>' green, 'i<n>' IR (n = 0-255).\n")

        scope = LiveScope(stream.header, window_s=args.window,
                          stages=args.stages, config=cfg)

        count = 0
        try:
            for _idx, _us, values in stream:
                scope.push(values)
                count += 1
                if count % args.refresh_every == 0:
                    scope.refresh()
                    if not scope.alive():
                        break
        except KeyboardInterrupt:
            pass

    print(f"\nStopped after {count:,} samples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
