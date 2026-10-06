#!/usr/bin/env python3
"""Recording logger (docs/SPEC.md section 4).

    python logger.py --port COM5 --subject T01 --trial 3
    python logger.py --subject T01 --trial 1 --note "left arm, seated"
    python logger.py --calibrate          # two-point pressure calibration

Sends INFO, then DARK, then START. Writes every line to
``data/<date>_<subject>_<trial>.csv`` with a full metadata header. Ctrl-C sends
STOP and closes the file cleanly.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapquest.io import Device, find_port, load_calibration, save_calibration  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE.parent / "data"
CALIBRATION_PATH = HERE / "calibration.json"


# ---------------------------------------------------------------------------
# Pressure calibration (SPEC 4.1)
# ---------------------------------------------------------------------------

def collect_pressure(dev: Device, seconds: float) -> float:
    """Mean raw pressure over a window, read from the live stream."""
    values: list[int] = []
    t0 = time.time()
    for fields, _line in dev.stream():
        if "p_raw" not in dev.meta.columns:
            raise RuntimeError(
                "Firmware reports no pressure channel. Set PRESSURE_ENABLED=1 "
                "in firmware/mapquest_ppg/config.h and reflash."
            )
        values.append(fields[dev.meta.columns.index("p_raw")])
        elapsed = time.time() - t0
        sys.stdout.write(f"\r    {elapsed:4.1f}/{seconds:.0f} s  "
                         f"n={len(values):>5}  last={values[-1]:>10,}")
        sys.stdout.flush()
        if elapsed >= seconds:
            break
    dev.command("STOP")
    dev.drain(0.3)
    print()
    if not values:
        raise RuntimeError("No pressure samples received")
    return float(np.mean(values))


def run_calibration(dev: Device, window_s: float) -> dict:
    print("\nTwo-point pressure calibration (SPEC 4.1)")
    print("=" * 56)

    input("\n1. Open the cuff fully to air, then press Enter...")
    print(f"   Recording {window_s:.0f} s at ambient...")
    raw0 = collect_pressure(dev, window_s)
    print(f"   RAW0 = {raw0:,.1f}")

    print("\n2. Pump to a steady gauge reading and hold it.")
    p_ref = float(input("   Gauge reading in mmHg: ").strip())
    input("   Hold that pressure, then press Enter...")
    print(f"   Recording {window_s:.0f} s at {p_ref:g} mmHg...")
    raw_ref = collect_pressure(dev, window_s)
    print(f"   RAW_REF = {raw_ref:,.1f}")

    if abs(raw_ref - raw0) < 1e-6:
        raise RuntimeError("RAW0 and RAW_REF are identical — is the sensor connected?")

    cal = {
        "RAW0": raw0,
        "RAW_REF": raw_ref,
        "P_REF": p_ref,
        "counts_per_mmHg": (raw_ref - raw0) / p_ref,
        "calibrated_at": datetime.now().isoformat(timespec="seconds"),
        "firmware": dev.meta.firmware,
    }
    save_calibration(CALIBRATION_PATH, cal)

    print(f"\n   {cal['counts_per_mmHg']:,.1f} counts/mmHg")
    print(f"   Saved to {CALIBRATION_PATH}")
    print("\n3. Recommended: sweep 0 to max in ~20 mmHg steps once, comparing")
    print("   gauge against reported pressure, to check linearity and find")
    print("   where the module saturates (SPEC 4.1 / 9).")
    return cal


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record(dev: Device, out_path: Path, args) -> int:
    cal = load_calibration(CALIBRATION_PATH)
    counts_per_mmhg = cal["counts_per_mmHg"] if cal else None
    raw0 = cal["RAW0"] if cal else None

    with out_path.open("w", newline="") as fh:
        # Metadata header (SPEC 4).
        fh.write(f"# recorded_at={datetime.now().isoformat(timespec='seconds')}\n")
        fh.write(f"# subject={args.subject}\n")
        fh.write(f"# trial={args.trial}\n")
        if args.note:
            fh.write(f"# note={args.note}\n")
        for key, value in dev.meta.raw.items():
            fh.write(f"# {key}={value}\n")
        if cal:
            fh.write(f"# cal_RAW0={cal['RAW0']} cal_RAW_REF={cal['RAW_REF']} "
                     f"cal_P_REF={cal['P_REF']}\n")
        else:
            fh.write("# cal=none\n")
        fh.write(",".join(dev.meta.columns) + "\n")

        cols = dev.meta.columns
        i_ir = cols.index("ir") if "ir" in cols else None
        i_p = cols.index("p_raw") if "p_raw" in cols else None

        print("\nRecording — Ctrl-C to stop.\n")
        count = 0
        last_report = time.time()
        last_count = 0
        t0 = time.time()

        try:
            for fields, line in dev.stream():
                fh.write(line[2:] + "\n")  # strip the leading "S,"
                count += 1

                now = time.time()
                if now - last_report >= 1.0:
                    rate = (count - last_count) / (now - last_report)
                    parts = [f"\r  {now - t0:6.1f} s", f"{count:>8,} samples",
                             f"{rate:6.1f} Hz"]
                    if i_ir is not None:
                        parts.append(f"IR DC {fields[i_ir]:>7,}")
                    if i_p is not None:
                        if counts_per_mmhg:
                            mmhg = (fields[i_p] - raw0) / counts_per_mmhg
                            parts.append(f"{mmhg:6.1f} mmHg")
                        else:
                            parts.append(f"p={fields[i_p]:>10,}")
                    sys.stdout.write("  ".join(parts))
                    sys.stdout.flush()
                    last_report = now
                    last_count = count

                if args.seconds and (now - t0) >= args.seconds:
                    break
        except KeyboardInterrupt:
            print("\n  stopped by user")

        elapsed = time.time() - t0

    dev.command("STOP")
    dev.drain(0.3)

    print(f"\n\nWrote {count:,} samples ({elapsed:.1f} s) to {out_path}")
    if dev.errors:
        print(f"\n{len(dev.errors)} firmware error(s):")
        for e in dev.errors[:10]:
            print(f"  E: {e}")
    if count == 0:
        print("No samples captured.")
        return 1

    print(f"\nNext: python analyze.py {out_path}")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--subject", default="T00", help="subject ID, e.g. T01")
    ap.add_argument("--trial", default="1")
    ap.add_argument("--note", default="", help="free-text note into the header")
    ap.add_argument("--out", type=Path, help="output directory or file")
    ap.add_argument("--seconds", type=float, help="auto-stop after this long")
    ap.add_argument("--no-dark", action="store_true",
                    help="skip the DARK measurement (not recommended)")
    ap.add_argument("--ir-amp", type=int, help="set IR LED amplitude before recording")
    ap.add_argument("--red-amp", type=int, help="set red LED amplitude before recording")
    ap.add_argument("--calibrate", action="store_true",
                    help="run the two-point pressure calibration and exit")
    ap.add_argument("--cal-window", type=float, default=3.0,
                    help="averaging window per calibration point, s")
    args = ap.parse_args()

    port = find_port(args.port)
    print(f"Port    : {port}")

    with Device(port, baud=args.baud) as dev:
        dev.info()
        print(f"Config  : {dev.meta.describe()}")
        print(f"Firmware: {dev.meta.firmware}")

        if args.calibrate:
            run_calibration(dev, args.cal_window)
            return 0

        if args.ir_amp is not None:
            dev.set_amp("ir", args.ir_amp)
            print(f"IR amp  : set to {args.ir_amp}")
        if args.red_amp is not None:
            dev.set_amp("red", args.red_amp)
            print(f"Red amp : set to {args.red_amp}")

        if not args.no_dark:
            print("\nDARK    : measuring (LEDs off, ~2 s)...")
            dark = dev.measure_dark()
            if dark:
                print(f"          {dark}")
                if max(dark.values()) > 128:
                    print("          ! above the ~128 count expectation (SPEC 3.5) — "
                          "shield the sensor from ambient light")
            else:
                print("          failed — continuing without dark offsets")

        # Resolve the output path.
        stamp = datetime.now().strftime("%Y%m%d")
        name = f"{stamp}_{args.subject}_{args.trial}.csv"
        if args.out and args.out.suffix == ".csv":
            out_path = args.out
        else:
            out_path = (args.out or DATA_DIR) / name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            out_path = out_path.with_name(
                f"{out_path.stem}_{datetime.now().strftime('%H%M%S')}.csv"
            )
        print(f"Writing : {out_path}")

        return record(dev, out_path, args)


if __name__ == "__main__":
    raise SystemExit(main())
