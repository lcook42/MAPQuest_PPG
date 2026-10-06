#!/usr/bin/env python3
"""Hardware bench check — the SPEC 3.5 acceptance criteria, as pass/fail.

This is the first thing to run with the board and sensor wired up. It needs no
cuff and no pressure sensor.

    python bench_check.py                  # quick 60 s check
    python bench_check.py --auto-tune      # also set IR/red current for you
    python bench_check.py --full           # 5 min, the real M1 gate
    python bench_check.py --plot --save    # eyeball the trace, keep the CSV

Checks, from SPEC 3.5:
  1. I2C finds the MAX30101 at 0x57
  2. DARK offsets <= ~128 counts
  3. IR DC in 105k-157k counts (40-60% of full scale)
  4. Output rate within 1% of the configured fs, measured from t_us
  5. Zero dropped samples

The pressure criterion is skipped: it needs the HX710B, which is not fitted.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapquest.io import Device, check_continuity, find_port  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# SPEC 3.1 / 3.5 targets.
DC_MIN, DC_MAX = 105_000, 157_000
DC_TARGET = (DC_MIN + DC_MAX) // 2
DARK_MAX = 128
RATE_TOLERANCE = 0.01  # 1%

OK, BAD, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class Report:
    """Collects check outcomes and prints a verdict."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        mark = {OK: "  ok  ", BAD: " FAIL ", WARN: " warn ", SKIP: " skip "}[status]
        print(f"  [{mark}] {name}" + (f"\n           {detail}" if detail else ""))

    def verdict(self) -> int:
        failed = [r for r in self.rows if r[0] == BAD]
        warned = [r for r in self.rows if r[0] == WARN]
        print("\n" + "=" * 64)
        if failed:
            print(f"RESULT: {len(failed)} check(s) FAILED")
            for _s, name, detail in failed:
                print(f"  - {name}: {detail}")
            return 1
        if warned:
            print(f"RESULT: all checks passed, {len(warned)} warning(s)")
            return 0
        print("RESULT: all checks passed — M1 hardware criteria met")
        return 0


def capture(dev: Device, seconds: float, progress: bool = True):
    """Stream for a while and return (idx, t_us, {channel: counts})."""
    cols = dev.meta.columns
    i_idx, i_us = cols.index("idx"), cols.index("t_us")
    chan_idx = {c: cols.index(c) for c in dev.meta.channels if c in cols}

    rows: list[list[int]] = []
    t0 = time.time()
    last_print = 0.0

    for fields, _line in dev.stream():
        rows.append(fields)
        elapsed = time.time() - t0
        if progress and elapsed - last_print >= 1.0:
            ir = fields[chan_idx["ir"]] if "ir" in chan_idx else 0
            sys.stdout.write(
                f"\r    {elapsed:5.1f}/{seconds:.0f} s   "
                f"{len(rows):>7,} samples   IR DC {ir:>7,}   "
            )
            sys.stdout.flush()
            last_print = elapsed
        if elapsed >= seconds:
            break

    dev.stop()
    if progress:
        print()

    if not rows:
        raise RuntimeError("No samples received")

    arr = np.asarray(rows, dtype=np.int64)
    channels = {c: arr[:, i] for c, i in chan_idx.items()}
    return arr[:, i_idx], arr[:, i_us], channels


def measure_dc(dev: Device, seconds: float = 1.0) -> dict[str, float]:
    """Mean DC per channel over a short burst."""
    _idx, _us, channels = capture(dev, seconds, progress=False)
    # Skip the first few samples: the FIFO may hold pre-settling values.
    return {c: float(np.mean(v[5:])) if v.size > 10 else float(np.mean(v))
            for c, v in channels.items()}


def auto_tune(dev: Device, report: Report, max_iter: int = 6) -> dict[str, int]:
    """Drive IR (and red) LED current until DC lands in the target window.

    DC above the dark floor is roughly proportional to LED current, so a
    proportional step converges in a few iterations. Amplitude changes are
    refused while streaming, so each iteration stops, sets, and restarts.
    """
    print(f"\n  Auto-tuning LED current for IR DC near {DC_TARGET:,}...")
    amps = {"ir": int(float(dev.meta.raw.get("ir_amp", 31))),
            "red": int(float(dev.meta.raw.get("red_amp", 31)))}
    dark = dev.meta.dark
    full_scale = dev.meta.full_scale
    final: dict[str, float] = {}

    for channel in ("ir", "red"):
        if channel not in dev.meta.channels:
            continue
        floor = dark.get(channel, 0.0)

        for it in range(max_iter):
            dc = measure_dc(dev, 0.6).get(channel)
            if dc is None:
                break
            final[channel] = dc
            print(f"    {channel:>3}  amp={amps[channel]:>3} "
                  f"({amps[channel] * 0.2:4.1f} mA)  DC={dc:>8,.0f}")

            if DC_MIN <= dc <= DC_MAX:
                break
            if dc >= full_scale * 0.98:
                # Saturated: the proportional step is meaningless here, so
                # halve and try again.
                new_amp = max(1, amps[channel] // 2)
            else:
                signal = max(dc - floor, 1.0)
                scale = (DC_TARGET - floor) / signal
                new_amp = int(round(amps[channel] * scale))
                new_amp = max(1, min(255, new_amp))

            if new_amp == amps[channel]:
                break
            amps[channel] = new_amp
            dev.set_amp(channel, new_amp)
            time.sleep(0.2)
        else:
            it = max_iter

        dc = final.get(channel, float("nan"))
        if channel == "ir":
            if DC_MIN <= dc <= DC_MAX:
                report.add(OK, f"auto-tune {channel}",
                           f"amp={amps[channel]} ({amps[channel] * 0.2:.1f} mA), "
                           f"DC={dc:,.0f}")
            else:
                report.add(WARN, f"auto-tune {channel}",
                           f"could not reach {DC_MIN:,}-{DC_MAX:,}; "
                           f"settled at DC={dc:,.0f} with amp={amps[channel]}. "
                           "Check sensor contact and shielding.")
    return amps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="capture duration for the rate/continuity checks")
    ap.add_argument("--full", action="store_true",
                    help="use the full 5 minutes the SPEC 3.5 drop test calls for")
    ap.add_argument("--auto-tune", action="store_true",
                    help="adjust LED current to bring IR DC into range")
    ap.add_argument("--no-dark", action="store_true", help="skip the DARK check")
    ap.add_argument("--plot", action="store_true", help="plot the captured trace")
    ap.add_argument("--save", action="store_true",
                    help="keep the capture as a CSV in data/")
    args = ap.parse_args()

    seconds = 300.0 if args.full else args.seconds
    report = Report()

    port = find_port(args.port)
    print(f"Port : {port}\n")
    print("MAPQuest bench check (SPEC 3.5)")
    print("=" * 64)

    with Device(port, baud=args.baud) as dev:
        # -- 1. I2C / identity --------------------------------------------
        dev.info()
        part = dev.meta.raw.get("part_id")
        if part is None:
            report.add(BAD, "MAX30101 found at 0x57",
                       "no part_id in the firmware header — is the sketch uploaded, "
                       "and is this the right port?")
            return report.verdict()

        # sensor_ok is authoritative: the firmware reports the part id it
        # actually read, and whether configuration succeeded.
        if dev.meta.sensor_ok is False:
            report.add(BAD, "MAX30101 found at 0x57",
                       f"firmware reports sensor_ok=0, part_id={part}. "
                       "Check SDA=A4, SCL=A5, 3V3 (not 5V), GND. Fix the wiring "
                       "and send R over serial to retry without reflashing.")
            for e in dev.errors[:3]:
                print(f"           firmware: {e}")
            return report.verdict()
        if part.lower() not in ("0x15",):
            report.add(BAD, "MAX30101 found at 0x57",
                       f"part_id={part}, expected 0x15. Wrong device on the bus?")
            return report.verdict()

        report.add(OK, "MAX30101 found at 0x57",
                   f"part_id={part} rev={dev.meta.raw.get('rev', '?')} "
                   f"firmware={dev.meta.firmware}")

        if dev.errors:
            report.add(WARN, "firmware reported no errors",
                       "; ".join(dev.errors[:3]))

        fs_cfg = dev.meta.fs
        print(f"\n  Config: {dev.meta.describe()}")
        print(f"  Columns: {','.join(dev.meta.columns)}")

        # -- 2. DARK -------------------------------------------------------
        if args.no_dark:
            report.add(SKIP, "DARK offsets <= 128 counts", "--no-dark")
        else:
            print("\n  Measuring DARK (LEDs off, ~2 s)...")
            dark = dev.measure_dark()
            if not dark:
                report.add(BAD, "DARK offsets <= 128 counts", "DARK returned nothing")
            else:
                worst = max(dark.values())
                detail = ", ".join(f"{k}={v:,.0f}" for k, v in dark.items())
                if worst <= DARK_MAX:
                    report.add(OK, "DARK offsets <= 128 counts", detail)
                else:
                    report.add(WARN, "DARK offsets <= 128 counts",
                               f"{detail} — ambient light is leaking in. Shield the "
                               "sensor; this biases every delta-OD downstream.")

        # -- 3. LED current / DC window ------------------------------------
        if args.auto_tune:
            auto_tune(dev, report)

        # -- 4/5. Capture, rate, continuity --------------------------------
        print(f"\n  Capturing {seconds:.0f} s — keep the sensor still on the forearm.")
        idx, t_us, channels = capture(dev, seconds)

    n = len(idx)
    print(f"\n  Captured {n:,} samples")

    # DC window (checked on the captured run, not the tuning burst).
    if "ir" in channels:
        ir = channels["ir"].astype(float)
        dc = float(np.mean(ir))
        clipped = float(np.mean(ir >= (1 << 18) - 1) * 100)
        detail = (f"mean={dc:,.0f}  min={ir.min():,.0f}  max={ir.max():,.0f}  "
                  f"({dc / ((1 << 18) - 1) * 100:.0f}% of full scale)")
        if clipped > 0.01:
            report.add(BAD, f"IR DC in {DC_MIN:,}-{DC_MAX:,}",
                       f"{detail} — {clipped:.2f}% of samples CLIPPED. Lower IR current.")
        elif DC_MIN <= dc <= DC_MAX:
            report.add(OK, f"IR DC in {DC_MIN:,}-{DC_MAX:,}", detail)
        else:
            hint = ("raise IR current (IRAMP) or improve contact"
                    if dc < DC_MIN else "lower IR current (IRAMP)")
            report.add(WARN, f"IR DC in {DC_MIN:,}-{DC_MAX:,}",
                       f"{detail} — {hint}. Try --auto-tune.")

    # Output rate from t_us (SPEC 3.5). micros() wraps at 2^32; mask the
    # difference so a wrap mid-capture does not read as a negative interval.
    span_us = int((int(t_us[-1]) - int(t_us[0])) % (1 << 32))
    if span_us > 0 and n > 1:
        measured = (n - 1) / (span_us / 1e6)
        err = (measured - fs_cfg) / fs_cfg
        detail = f"{measured:.2f} Hz measured vs {fs_cfg:g} Hz configured ({err * 100:+.2f}%)"
        if abs(err) <= RATE_TOLERANCE:
            report.add(OK, f"output rate within {RATE_TOLERANCE * 100:g}%", detail)
        else:
            report.add(BAD, f"output rate within {RATE_TOLERANCE * 100:g}%", detail)
    else:
        report.add(WARN, "output rate", "could not measure (t_us span is zero)")

    # Continuity (SPEC 3.5: zero dropped samples).
    n_gaps, n_missing = check_continuity(idx)
    if n_gaps == 0:
        report.add(OK, "zero dropped samples",
                   f"{n:,} consecutive indices over {seconds:.0f} s")
    else:
        report.add(BAD, "zero dropped samples",
                   f"{n_gaps} discontinuity/ies, {n_missing} samples missing. "
                   "Close other serial monitors; the host is not keeping up.")

    report.add(SKIP, "pressure stream at ~40 Hz",
               "HX710B not fitted (PRESSURE_ENABLED=0)")

    # -- Optional artefacts ------------------------------------------------
    out_path = None
    if args.save:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DATA_DIR / f"bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        with out_path.open("w") as fh:
            fh.write("# source=bench_check\n")
            fh.write(f"# fs={fs_cfg:.2f}\n")
            fh.write(f"# columns=idx,t_us,{','.join(channels)}\n")
            fh.write(f"idx,t_us,{','.join(channels)}\n")
            for i in range(n):
                vals = ",".join(str(int(channels[c][i])) for c in channels)
                fh.write(f"{idx[i]},{t_us[i]},{vals}\n")
        print(f"\n  Saved capture to {out_path}")

    if args.plot:
        import matplotlib.pyplot as plt

        t = (idx - idx[0]) / fs_cfg
        fig, axes = plt.subplots(len(channels), 1, figsize=(11, 3 * len(channels)),
                                 sharex=True)
        axes = np.atleast_1d(axes)
        colors = {"ir": "#1f4e79", "red": "#c43b2f"}
        for ax, (name, v) in zip(axes, channels.items()):
            ax.plot(t, v, lw=0.7, color=colors.get(name))
            ax.axhspan(DC_MIN, DC_MAX, color="#2e9e4f", alpha=0.10, lw=0)
            ax.set_ylabel(f"{name} [counts]")
            ax.grid(alpha=0.25, lw=0.5)
            ax.margins(x=0)
        axes[0].set_title("Bench capture — green band is the SPEC 3.1 DC target window",
                          fontsize=9)
        axes[-1].set_xlabel("time (s)")
        fig.tight_layout()
        if out_path:
            fig.savefig(out_path.with_suffix(".png"), dpi=130, bbox_inches="tight")
            print(f"  Saved plot to {out_path.with_suffix('.png')}")
        plt.show()

    return report.verdict()


if __name__ == "__main__":
    raise SystemExit(main())
