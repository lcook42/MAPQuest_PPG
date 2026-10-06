#!/usr/bin/env python3
"""Record raw PPG samples from the board to a timestamped CSV.

    python record.py                      # record until Ctrl-C
    python record.py --seconds 60         # record a fixed duration
    python record.py --plot               # live scope while recording
    python record.py --port COM5          # explicit port

The CSV keeps the firmware's ``#`` metadata header, so recordings are
self-describing and ``analyze.py`` never has to assume a sample rate.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from mapquest.serial_io import PPGStream, find_port

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl-C)")
    ap.add_argument("--out", type=Path, help="output path (default: data/ppg_<timestamp>.csv)")
    ap.add_argument("--label", default="", help="tag included in the default filename")
    ap.add_argument("--plot", action="store_true",
                    help="also show a live scope (needs matplotlib)")
    args = ap.parse_args()

    port = find_port(args.port)

    if args.out:
        out_path = args.out
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = f"_{args.label}" if args.label else ""
        out_path = DATA_DIR / f"ppg_{stamp}{tag}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Port    : {port}")

    live = None
    with PPGStream(port, baud=args.baud) as stream, out_path.open("w", newline="") as fh:
        print(f"Config  : {stream.header.describe()}")
        print(f"Writing : {out_path}")

        # Mirror the firmware metadata into the file so it is self-describing.
        for key, value in stream.header.raw.items():
            fh.write(f"# {key}={value}\n")
        fh.write(f"# recorded_at={datetime.now().isoformat(timespec='seconds')}\n")
        if args.label:
            fh.write(f"# label={args.label}\n")
        fh.write(",".join(stream.header.columns) + "\n")

        if args.plot:
            from live_plot import LiveScope  # imported lazily; matplotlib is optional here

            live = LiveScope(stream.header)

        if args.seconds:
            print(f"Duration: {args.seconds:g} s")
        else:
            print("Duration: until Ctrl-C")
        print("Recording... keep still, and secure the sensor with a band.\n")

        count = 0
        t0 = time.time()
        try:
            for idx, us, values in stream:
                fh.write(f"{idx},{us}," + ",".join(str(v) for v in values) + "\n")
                count += 1

                if live is not None:
                    live.push(values)
                    if count % 20 == 0:
                        live.refresh()

                if count % 200 == 0:
                    elapsed = time.time() - t0
                    rate = count / elapsed if elapsed else 0.0
                    chans = " ".join(
                        f"{n}={v:>7,}" for n, v in zip(stream.header.channels, values)
                    )
                    sys.stdout.write(
                        f"\r  {elapsed:7.1f} s  {count:>8,} samples  "
                        f"{rate:6.1f} Hz  {chans}  ovf={stream.overflows}"
                    )
                    sys.stdout.flush()

                if args.seconds and (time.time() - t0) >= args.seconds:
                    break
        except KeyboardInterrupt:
            print("\n  stopped by user")

        elapsed = time.time() - t0

    print(f"\n\nWrote {count:,} samples ({elapsed:.1f} s) to {out_path}")
    if stream.overflows:
        print(f"WARNING: {stream.overflows} FIFO overflow event(s) — samples were dropped. "
              "The host could not keep up; close other serial monitors.")
    if count == 0:
        print("No samples captured. Check the sensor is responding ('?' over a serial monitor).")
        return 1

    print(f"\nNext: python analyze.py {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
