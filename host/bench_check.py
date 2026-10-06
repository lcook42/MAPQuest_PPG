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

from scipy import signal as sig  # noqa: E402

from mapquest.io import (  # noqa: E402
    Device,
    LinkLost,
    LinkStalled,
    check_continuity,
    describe_ports,
    find_port,
)
import provocation  # noqa: E402

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


def _link_advice(status: str) -> str:
    """What to actually try, ordered by how often it is the cause."""
    common = [
        "  Things to try, most likely first:",
        "    1. Swap the USB cable. Charge-only and thin cables are the most",
        "       common cause — the board enumerates but drops under sustained",
        "       traffic.",
        "    2. Use a port directly on the machine, not a hub, and not a",
        "       monitor/keyboard passthrough.",
        "    3. Close anything else holding the port — Arduino IDE Serial",
        "       Monitor and Serial Plotter both do.",
        "    4. Disable USB selective suspend for this device (Windows will",
        "       power down a CDC port it thinks is idle).",
    ]
    if status == "stalled":
        common.insert(1, "    1b. The port stayed open, so the cable is probably "
                         "fine; a board reset or a wedged sketch is more likely.")
    if status == "reset":
        common = [
            "  The board restarted mid-capture. Usual causes:",
            "    1. Brown-out — the LEDs pulse hard; try a powered hub or a",
            "       shorter cable.",
            "    2. A loose 3V3 or GND to the breakout momentarily opening.",
            "    3. The reset line being touched by the wiring.",
        ]
    return "\n".join(common)


def capture(dev: Device, seconds: float, progress: bool = True,
            stall_timeout: float = 3.0):
    """Stream for a while and return (idx, t_us, {channel: counts}, link).

    A capture that dies partway is salvaged rather than discarded: ``link``
    records what went wrong and the samples collected so far are returned, so
    a 5-minute run that drops at 4 minutes still yields usable data plus a
    diagnosis.
    """
    cols = dev.meta.columns
    i_idx, i_us = cols.index("idx"), cols.index("t_us")
    chan_idx = {c: cols.index(c) for c in dev.meta.channels if c in cols}

    link = {"status": "ok", "reason": "", "max_gap_s": 0.0,
            "resets": 0, "requested_s": seconds, "actual_s": 0.0}

    rows: list[list[int]] = []
    t0 = time.time()
    last_print = 0.0
    prev_idx: int | None = None

    try:
        for fields, _line in dev.stream(stall_timeout=stall_timeout):
            this_idx = fields[i_idx]
            # An index going backwards means the board restarted: LED
            # amplitudes reverted to defaults and the dark offsets are gone,
            # so samples past this point are not comparable to earlier ones.
            if prev_idx is not None and this_idx < prev_idx:
                link["resets"] += 1
                link["status"] = "reset"
                link["reason"] = (
                    f"board reset mid-capture (sample index {prev_idx:,} -> "
                    f"{this_idx:,}) after {len(rows):,} samples. LED and dark "
                    "settings were lost, so the capture stops here."
                )
                break
            prev_idx = this_idx
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
    except LinkStalled as exc:
        link["status"] = "stalled"
        link["reason"] = str(exc)
    except LinkLost as exc:
        link["status"] = "lost"
        link["reason"] = str(exc)

    link["actual_s"] = time.time() - t0
    link["max_gap_s"] = dev.max_gap_s
    dev.stop()
    if progress:
        print()

    if not rows:
        raise RuntimeError(
            "No samples received. "
            + (link["reason"] or "The firmware sent nothing after START.")
        )

    arr = np.asarray(rows, dtype=np.int64)
    channels = {c: arr[:, i] for c, i in chan_idx.items()}
    return arr[:, i_idx], arr[:, i_us], channels, link


def measure_dc(dev: Device, seconds: float = 1.0) -> dict[str, float]:
    """Mean DC per channel over a short burst."""
    _idx, _us, channels, _link = capture(dev, seconds, progress=False)
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


def analyse_pulse(channels: dict[str, np.ndarray], fs: float) -> dict:
    """Isolate the cardiac component and measure whether one is actually there.

    The acceptance criteria in SPEC 3.5 say nothing about whether the sensor is
    optically coupled to blood -- a disconnected photodiode sitting at a
    plausible DC would pass every one of them. A detectable pulse is the best
    evidence available without a cuff that the light path reaches perfused
    tissue.
    """
    nyq = fs / 2.0
    lo, hi = 0.7, min(3.5, nyq * 0.9)   # 42-210 bpm
    sos = sig.butter(2, [lo / nyq, hi / nyq], btype="band", output="sos")

    out: dict = {"band": (lo, hi), "ac": {}, "fs": fs}
    for name, v in channels.items():
        x = v.astype(float)
        padlen = min(3 * (2 * len(sos) + 1), max(len(x) // 3 - 1, 0))
        out["ac"][name] = sig.sosfiltfilt(sos, x, padlen=padlen)

    if not channels or len(next(iter(channels.values()))) < int(fs * 8):
        return out

    # Analyse every channel, not just IR: a pulse visible on one wavelength and
    # not the other is itself diagnostic.
    out["channels"] = {}
    for name, v in channels.items():
        out["channels"][name] = _channel_metrics(
            v.astype(float), out["ac"][name], fs, lo, hi
        )

    primary = out["channels"].get("ir") or next(iter(out["channels"].values()))
    out.update({k: primary[k] for k in
                ("f", "pxx", "dc", "ac_rms", "pi_pct", "drift_ratio") if k in primary})
    for k in ("peak_hz", "bpm", "prominence_db"):
        if primary.get(k) is not None:
            out[k] = primary[k]
    return out


def _coupling_advice(pulse: dict) -> str:
    """Point at the most likely cause given what the spectrum looks like."""
    ir = pulse.get("channels", {}).get("ir", {})
    drift = ir.get("drift_ratio", 0.0)
    pi = ir.get("pi_pct", 0.0)

    lines = []
    if pi < 0.05:
        lines.append(
            "Almost no pulsatile content. A healthy DC with no AC usually means "
            "light is reaching the photodiode WITHOUT passing through perfused "
            "tissue — it pipes across the surface from the LEDs. Raising LED "
            "current makes this worse, not better. Press the sensor flat against "
            "skin and strap it."
        )
    if drift > 20:
        lines.append(
            f"Drift and motion carry {drift:.0f}x the power of the cardiac band. "
            "Immobilise the arm and the cable before anything else."
        )
    lines.append(
        "Fastest way to tell hardware from placement: try a FINGERTIP. Finger "
        "PPG is 10-50x stronger than forearm. Clear beats there means the board, "
        "firmware and config are fine and this is purely a forearm coupling "
        "problem; no beats there means the fault is upstream of placement."
    )
    return "\n           ".join(lines)


def _channel_metrics(x: np.ndarray, ac: np.ndarray, fs: float,
                     lo: float, hi: float) -> dict:
    """Per-channel pulse metrics.

    The detector looks for a genuine *local peak* in the spectrum, not simply
    the largest in-band bin. That distinction matters: a drift-dominated
    recording has a 1/f spectrum that falls monotonically, so the in-band
    argmax lands on the band's lower edge and -- compared against a distant
    high-frequency noise floor -- reports a large, entirely spurious SNR. A
    real pulse is a bump standing above its own neighbourhood, which is what
    peak prominence measures.
    """
    dc = float(np.mean(x))
    nperseg = int(min(len(x), fs * 8))
    f, pxx = sig.welch(x - np.mean(x), fs=fs, nperseg=nperseg)

    m: dict = {
        "f": f,
        "pxx": pxx,
        "dc": dc,
        "ac_rms": float(np.sqrt(np.mean(ac**2))),
        "pi_pct": float(np.sqrt(np.mean(ac**2))) / dc * 100.0 if dc > 0 else float("nan"),
        "peak_hz": None,
        "bpm": None,
        "prominence_db": None,
    }

    # How far the spectrum is dominated by sub-cardiac drift and motion.
    drift = (f >= 0.03) & (f < lo)
    band = (f >= lo) & (f <= hi)
    if drift.any() and band.any():
        d = float(np.trapezoid(pxx[drift], f[drift]))
        b = float(np.trapezoid(pxx[band], f[band]))
        m["drift_ratio"] = d / b if b > 0 else float("inf")

    # Find peaks on the dB spectrum across the whole range, so prominence is
    # measured against real neighbouring structure, then keep in-band ones.
    pxx_db = 10.0 * np.log10(np.maximum(pxx, 1e-20))
    peaks, props = sig.find_peaks(pxx_db, prominence=2.0)
    if peaks.size:
        keep = (f[peaks] >= lo) & (f[peaks] <= hi)
        if keep.any():
            cand = peaks[keep]
            proms = props["prominences"][keep]
            k = int(np.argmax(proms))
            m["peak_hz"] = float(f[cand[k]])
            m["bpm"] = m["peak_hz"] * 60.0
            m["prominence_db"] = float(proms[k])
    return m


def plot_capture(idx, channels, fs, pulse, save: Path | None = None) -> None:
    """Four-panel diagnostic figure.

    Raw counts get one autoscaled axis per channel. The previous version drew
    the DC target window with axhspan, which forced the y-axis to span
    105k-157k -- so a pulse of ~100 counts on a 135k baseline was sub-pixel and
    every trace looked like a flat line. The target window is now annotated
    instead of drawn, and the cardiac component gets panels of its own.
    """
    import matplotlib.pyplot as plt

    t = (idx - idx[0]) / fs
    colors = {"ir": "#1f4e79", "red": "#c43b2f"}
    lo, hi = pulse["band"]

    fig = plt.figure(figsize=(12, 10.5))
    gs = fig.add_gridspec(4, 1, height_ratios=[1.0, 1.0, 1.3, 1.0], hspace=0.45)
    ax_ir = fig.add_subplot(gs[0])
    ax_red = fig.add_subplot(gs[1], sharex=ax_ir)
    ax_ac = fig.add_subplot(gs[2], sharex=ax_ir)
    ax_psd = fig.add_subplot(gs[3])          # frequency axis, not shared

    # -- Panels 1-2: raw counts, autoscaled per channel -------------------
    for ax, name in ((ax_ir, "ir"), (ax_red, "red")):
        if name not in channels:
            ax.set_visible(False)
            continue
        v = channels[name].astype(float)
        ax.plot(t, v, lw=0.8, color=colors[name])
        ax.set_ylabel(f"{name.upper()} raw\n[ADC counts]", fontsize=9)
        ax.grid(alpha=0.25, lw=0.5)
        ax.margins(x=0)
        ax.tick_params(labelsize=8)

        dc = float(np.mean(v))
        drift = float(v[-1] - v[0])
        span = float(np.ptp(v))
        in_window = DC_MIN <= dc <= DC_MAX
        ax.set_title(
            f"{name.upper()} raw — DC {dc:,.0f} counts "
            f"({dc / ((1 << 18) - 1) * 100:.0f}% of full scale, "
            f"{'inside' if in_window else 'OUTSIDE'} the {DC_MIN // 1000}k–"
            f"{DC_MAX // 1000}k target) · drift {drift:+,.0f} over "
            f"{t[-1]:.0f} s · span {span:,.0f}",
            fontsize=8.5, loc="left", color="#444", pad=3)
        ax.axhline(dc, color="#888", lw=0.6, ls=":")

    # -- Panel 3: the cardiac component -----------------------------------
    for name, ac in pulse["ac"].items():
        ax_ac.plot(t, ac, lw=0.9, color=colors.get(name), label=name.upper())
    ax_ac.axhline(0, color="k", lw=0.5, alpha=0.5)
    ax_ac.set_ylabel(f"AC component\n[counts, {lo:g}–{hi:g} Hz]", fontsize=9)
    ax_ac.set_xlabel("time (s)", fontsize=9)
    ax_ac.grid(alpha=0.25, lw=0.5)
    ax_ac.margins(x=0)
    ax_ac.tick_params(labelsize=8)
    ax_ac.legend(loc="upper right", fontsize=8, ncol=2)

    # Panels 1-2 share the time axis with panel 3; only label it once.
    for ax in (ax_ir, ax_red):
        ax.tick_params(labelbottom=False)

    bits = [f"band-pass {lo:g}–{hi:g} Hz ({lo * 60:.0f}–{hi * 60:.0f} bpm)"]
    if pulse.get("pi_pct") is not None:
        bits.append(f"perfusion index {pulse['pi_pct']:.3f}% rms")
    if pulse.get("bpm") is not None:
        bits.append(f"peak {pulse['bpm']:.0f} bpm "
                    f"({pulse['prominence_db']:.1f} dB prominence)")
    else:
        bits.append("NO spectral peak in band")
    ax_ac.set_title("Cardiac component — each bump should be one heartbeat.  "
                    + " · ".join(bits), fontsize=8.5, loc="left",
                    color="#444", pad=3)

    # -- Panel 4: spectrum ------------------------------------------------
    if "f" in pulse:
        f, pxx = pulse["f"], pulse["pxx"]
        keep = f <= min(20.0, fs / 2)
        ax_psd.semilogy(f[keep], pxx[keep], lw=0.9, color=colors.get("ir"))
        ax_psd.axvspan(lo, hi, color="#2e9e4f", alpha=0.12, lw=0,
                       label=f"cardiac band {lo:g}–{hi:g} Hz")
        if pulse.get("peak_hz") is not None:
            ax_psd.axvline(pulse["peak_hz"], color="#0a7d2c", lw=1.2)
            ax_psd.annotate(f"  {pulse['bpm']:.0f} bpm, "
                            f"{pulse['prominence_db']:.0f} dB prominence",
                            (pulse["peak_hz"], pxx[keep].max()),
                            fontsize=9, color="#0a7d2c", fontweight="bold",
                            va="top")
        else:
            ax_psd.annotate("  no peak in band — spectrum is drift, not pulse",
                            (lo, pxx[keep].max()), fontsize=9, color="#b00020",
                            fontweight="bold", va="top")
        ax_psd.set_xlim(0, float(f[keep].max()))
        ax_psd.set_xlabel("frequency (Hz)", fontsize=9)
        ax_psd.set_ylabel("IR power spectral density\n[counts²/Hz]", fontsize=9)
        ax_psd.grid(alpha=0.25, lw=0.5, which="both")
        ax_psd.tick_params(labelsize=8)
        ax_psd.legend(loc="upper right", fontsize=8)
        ax_psd.set_title("Spectrum of IR — a clear peak inside the green band "
                         "means a real pulse, not drift or noise",
                         fontsize=8.5, loc="left", color="#444", pad=3)
    else:
        ax_psd.set_visible(False)

    fig.suptitle(f"MAPQuest bench capture — {len(idx):,} samples @ {fs:g} Hz "
                 f"({t[-1]:.0f} s)", fontsize=11, fontweight="bold")

    if save:
        fig.savefig(save, dpi=130, bbox_inches="tight")
        print(f"  Saved plot to {save}")
    plt.show()


def run_provocation_mode(dev: Device, args, report: Report) -> int:
    """Arm-raise blood-volume response test (see host/provocation.py)."""
    protocol = provocation.DEFAULT_PROTOCOL
    if args.phase_seconds:
        protocol = [(k, args.phase_seconds, lbl, rest)
                    for k, _d, lbl, rest in protocol]

    print(f"\n  Provocation test — sensor on the {args.site}.")
    print("  Strap it on now and keep it in place for the whole run.")

    channels, phase_of, protocol, link = provocation.run_protocol(
        dev, protocol, stall_timeout=args.stall_timeout
    )

    if link["status"] != "ok":
        report.add(BAD, "serial link stable",
                   f"{link['status'].upper()} after {link['actual_s']:.1f} s. "
                   f"{link['reason']}")
    else:
        report.add(OK, "serial link stable",
                   f"ran the full {link['actual_s']:.0f} s")

    r = provocation.analyse(channels, phase_of, protocol, dev.meta.fs,
                            dark=dev.meta.dark, link=link)
    print(provocation.format_result(r, site=args.site))

    verdict = r.verdict
    best = max((e["z"] for e in r.effects.values()), default=float("nan"))
    detail = f"verdict {verdict}, strongest effect z={best:.1f}"
    if verdict == "responds":
        report.add(OK, "responds to blood volume", detail)
    elif verdict == "inverted":
        report.add(BAD, "responds to blood volume",
                   detail + " — response has the wrong sign")
    else:
        report.add(WARN, "responds to blood volume",
                   detail + " — see the diagnosis above")

    out_png = None
    if args.save or args.plot:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = DATA_DIR / f"provocation_{args.site}_{stamp}"
        if args.save:
            keys = [p[0] for p in protocol]
            with base.with_suffix(".csv").open("w") as fh:
                fh.write("# source=provocation\n")
                fh.write(f"# fs={dev.meta.fs:.2f}\n")
                fh.write(f"# site={args.site}\n")
                for k, v in dev.meta.dark.items():
                    fh.write(f"# dark_{k}={v:.0f}\n")
                fh.write(f"# phases={','.join(keys)}\n")
                fh.write("idx,phase," + ",".join(channels) + "\n")
                for i in range(len(phase_of)):
                    vals = ",".join(f"{channels[c][i]:.0f}" for c in channels)
                    fh.write(f"{i},{keys[phase_of[i]]},{vals}\n")
            print(f"\n  Saved capture to {base.with_suffix('.csv')}")
            out_png = base.with_suffix(".png")
    if args.plot:
        provocation.plot(r, site=args.site, save=out_png, show=True)

    return report.verdict()


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
    ap.add_argument("--provocation", action="store_true",
                    help="run the arm-raise blood-volume response test instead "
                         "of the plain capture")
    ap.add_argument("--site", default="wrist",
                    choices=("wrist", "forearm", "finger", "other"),
                    help="where the sensor is placed; recorded in the output")
    ap.add_argument("--phase-seconds", type=float,
                    help="override every provocation phase duration")
    ap.add_argument("--stall-timeout", type=float, default=3.0,
                    help="declare the link stalled after this many seconds of "
                         "silence mid-capture")
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
        if args.provocation:
            return run_provocation_mode(dev, args, report)

        print(f"\n  Capturing {seconds:.0f} s — keep the sensor still "
              f"on the {args.site}.")
        idx, t_us, channels, link = capture(dev, seconds,
                                            stall_timeout=args.stall_timeout)

    n = len(idx)
    actual_s = link["actual_s"]
    print(f"\n  Captured {n:,} samples over {actual_s:.1f} s")

    # -- Serial link health ------------------------------------------------
    # Checked first: if the link dropped, every number below is from a partial
    # capture and should be read in that light.
    if link["status"] == "ok":
        gap_note = f"longest gap between samples {link['max_gap_s'] * 1000:.0f} ms"
        if link["max_gap_s"] > 0.5:
            report.add(WARN, "serial link stable",
                       f"{gap_note} — the stream hiccuped without dropping. "
                       "Suspect a marginal cable or USB power saving.")
        else:
            report.add(OK, "serial link stable",
                       f"ran the full {actual_s:.0f} s, {gap_note}")
    else:
        kind = {"lost": "LINK LOST", "stalled": "LINK STALLED",
                "reset": "BOARD RESET"}[link["status"]]
        report.add(BAD, "serial link stable",
                   f"{kind} after {actual_s:.1f} s of {link['requested_s']:.0f} s "
                   f"requested ({n:,} samples kept).\n           {link['reason']}")
        print("\n  Partial capture retained — the checks below describe the "
              f"{actual_s:.0f} s that did arrive.")
        print(_link_advice(link["status"]))

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
                   f"{n:,} consecutive indices over {actual_s:.0f} s")
    else:
        report.add(BAD, "zero dropped samples",
                   f"{n_gaps} discontinuity/ies, {n_missing} samples missing. "
                   "Close other serial monitors; the host is not keeping up.")

    report.add(SKIP, "pressure stream at ~40 Hz",
               "HX710B not fitted (PRESSURE_ENABLED=0)")

    # -- Beyond SPEC 3.5: is the sensor actually seeing blood? ------------
    # Every criterion above would pass with the photodiode staring at a wall.
    pulse = analyse_pulse(channels, fs_cfg)
    if "channels" not in pulse:
        report.add(WARN, "cardiac pulse detected",
                   f"capture too short to measure ({actual_s:.1f} s; needs ~8 s)")
    else:
        for name, m in pulse["channels"].items():
            bits = [f"perfusion index {m['pi_pct']:.3f}% rms"]
            if m["bpm"] is not None:
                bits.append(f"peak {m['bpm']:.0f} bpm, "
                            f"{m['prominence_db']:.1f} dB prominence")
            else:
                bits.append("no spectral peak in the cardiac band")
            if "drift_ratio" in m:
                bits.append(f"drift/cardiac power {m['drift_ratio']:.0f}x")
            print(f"           {name:>4}: " + ", ".join(bits))

        m = pulse["channels"].get("ir", {})
        prom = m.get("prominence_db")
        bpm = m.get("bpm")
        plausible = bpm is not None and 40 <= bpm <= 180
        head = (f"IR {bpm:.0f} bpm at {prom:.1f} dB prominence"
                if plausible else "no periodic cardiac component on IR")

        if plausible and prom >= 6.0:
            report.add(OK, "cardiac pulse detected", head)
        elif plausible and prom >= 3.0:
            report.add(WARN, "cardiac pulse detected",
                       head + " — weak. Press the sensor flat and strap it.")
        else:
            report.add(WARN, "cardiac pulse detected",
                       head + ". Every check above can pass with no blood in "
                       "the light path, so do not trust a recording yet.\n"
                       "           " + _coupling_advice(pulse))

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
        plot_capture(idx, channels, fs_cfg, pulse,
                     save=out_path.with_suffix(".png") if out_path else None)

    return report.verdict()


if __name__ == "__main__":
    raise SystemExit(main())
