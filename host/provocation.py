"""Arm-raise provocation test — does the sensor respond to blood volume?

Rationale. Return-to-flow detects slow venous filling under an occluding
cuff, not the cardiac pulse, and that filling signal is roughly an order of
magnitude larger than the pulse. So a site can be perfectly viable for the
real measurement while showing no resolvable heartbeat. Chasing a pulse is
therefore the wrong gate.

Raising the arm above heart level drains venous blood; lowering it pools
blood. That drives the same slow blood-volume pathway the measurement uses,
with the largest signal obtainable without a cuff.

Sign convention: more blood -> more absorption -> LESS light back -> LOWER
counts. So arm UP raises counts and arm DOWN lowers them. A response with the
wrong sign is as informative as no response at all.

The protocol is an ABA design -- rest before and after each manoeuvre -- so
baseline drift, which is large at these timescales, largely cancels instead of
masquerading as a response.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import numpy as np

from mapquest.io import Device, LinkLost, LinkStalled
from mapquest.pipeline import blood_volume, delta_od

# (key, seconds, on-screen instruction, is_rest)
DEFAULT_PROTOCOL: list[tuple[str, float, str, bool]] = [
    ("rest1", 15.0, "Arm at HEART LEVEL, relaxed and still", True),
    ("up",    20.0, "Raise arm STRAIGHT UP, above your head", False),
    ("rest2", 15.0, "Back to HEART LEVEL", True),
    ("down",  20.0, "Hang arm DOWN, well below heart level", False),
    ("rest3", 15.0, "Back to HEART LEVEL", True),
]

# Venous volume takes time to settle, so only the tail of each phase is used
# for the steady-state estimate.
STEADY_FRACTION = 0.5


@dataclass
class PhaseStats:
    key: str
    label: str
    is_rest: bool
    t0: float
    t1: float
    n: int
    ir_mean: float
    ir_std: float
    dv_mean: float


@dataclass
class ProvocationResult:
    t: np.ndarray
    channels: dict[str, np.ndarray]
    phase_of: np.ndarray          # phase index per sample
    protocol: list
    stats: dict[str, PhaseStats]
    dv: np.ndarray                # two-wavelength blood volume, um
    effects: dict                 # per-manoeuvre effect sizes
    link: dict

    @property
    def verdict(self) -> str:
        """'responds', 'weak', 'inverted' or 'flat'."""
        good = [e for e in self.effects.values()
                if e["sign_ok"] and e["z"] >= 3.0]
        wrong = [e for e in self.effects.values()
                 if not e["sign_ok"] and e["z"] >= 3.0]
        if good:
            return "responds"
        if wrong:
            return "inverted"
        if any(e["z"] >= 1.5 for e in self.effects.values()):
            return "weak"
        return "flat"


def run_protocol(dev: Device, protocol=None, stall_timeout: float = 3.0):
    """Stream continuously while cueing the operator through the phases.

    Prompts are printed on a countdown rather than waiting on input(): the
    sensor FIFO is only 32 samples deep, which at 100 Hz is 320 ms, so
    blocking on a keypress mid-capture would overflow it and drop samples.
    """
    protocol = protocol or DEFAULT_PROTOCOL
    cols = dev.meta.columns
    i_idx = cols.index("idx")
    chan_idx = {c: cols.index(c) for c in dev.meta.channels if c in cols}

    bounds, acc = [], 0.0
    for key, dur, label, is_rest in protocol:
        bounds.append((acc, acc + dur))
        acc += dur
    total = acc

    link = {"status": "ok", "reason": "", "max_gap_s": 0.0,
            "resets": 0, "requested_s": total, "actual_s": 0.0}

    rows: list[list[int]] = []
    phase_of: list[int] = []
    t0 = time.time()
    cur = -1
    last_print = -1.0
    prev_idx: int | None = None

    print(f"\n  Protocol: {total:.0f} s total. Follow the prompts; keep the "
          "sensor still against the skin throughout.\n")

    try:
        for fields, _line in dev.stream(stall_timeout=stall_timeout):
            this_idx = fields[i_idx]
            if prev_idx is not None and this_idx < prev_idx:
                link["status"] = "reset"
                link["resets"] += 1
                link["reason"] = (f"board reset mid-test after {len(rows):,} "
                                  "samples")
                break
            prev_idx = this_idx

            elapsed = time.time() - t0
            phase = next((i for i, (a, b) in enumerate(bounds)
                          if a <= elapsed < b), None)
            if phase is None:
                break

            if phase != cur:
                cur = phase
                key, dur, label, is_rest = protocol[phase]
                print(f"\n  >>> {label}   ({dur:.0f} s)")
            rows.append(fields)
            phase_of.append(phase)

            # Rate-limit the countdown. Writing it per sample is 100 lines a
            # second at fs=100: it floods the terminal and the I/O starts
            # competing with draining the FIFO.
            if elapsed - last_print >= 0.1:
                last_print = elapsed
                remaining = bounds[phase][1] - elapsed
                ir = fields[chan_idx["ir"]] if "ir" in chan_idx else 0
                sys.stdout.write(
                    f"\r      {remaining:4.1f} s left   IR {ir:>7,}   "
                    f"total {elapsed:5.1f}/{total:.0f} s  ")
                sys.stdout.flush()

            if elapsed >= total:
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
    print()

    if not rows:
        raise RuntimeError("No samples received. " + (link["reason"] or ""))

    arr = np.asarray(rows, dtype=np.int64)
    channels = {c: arr[:, i].astype(float) for c, i in chan_idx.items()}
    return channels, np.asarray(phase_of), protocol, link


def analyse(channels: dict[str, np.ndarray], phase_of: np.ndarray,
            protocol, fs: float, dark: dict[str, float] | None = None,
            link: dict | None = None) -> ProvocationResult:
    """Per-phase steady-state levels and ABA-corrected effect sizes."""
    n = len(phase_of)
    t = np.arange(n) / fs
    dark = dark or {}

    def steady(mask: np.ndarray) -> np.ndarray:
        """Tail of a phase, after venous volume has had time to settle."""
        where = np.flatnonzero(mask)
        if where.size == 0:
            return where
        cut = where[int(len(where) * (1.0 - STEADY_FRACTION)):]
        return cut if cut.size else where

    # Blood volume in the same units the onset detector uses, referenced to
    # the first rest phase.
    sub = {k: v - float(dark.get(k, 0.0)) for k, v in channels.items()}
    ref_mask = phase_of == 0
    ref_idx = steady(ref_mask)
    d_od = {k: delta_od(v, float(np.mean(v[ref_idx]))) for k, v in sub.items()}
    dv = blood_volume(d_od, ir_only="red" not in sub)

    stats: dict[str, PhaseStats] = {}
    for i, (key, dur, label, is_rest) in enumerate(protocol):
        mask = phase_of == i
        if not mask.any():
            continue
        idx = steady(mask)
        ir = channels["ir"]
        stats[key] = PhaseStats(
            key=key, label=label, is_rest=is_rest,
            t0=float(t[mask][0]), t1=float(t[mask][-1]), n=int(mask.sum()),
            ir_mean=float(np.mean(ir[idx])), ir_std=float(np.std(ir[idx])),
            dv_mean=float(np.mean(dv[idx])),
        )

    # Noise: within-rest scatter after removing each rest phase's own slope,
    # so slow drift is not counted as noise.
    resid = []
    for key, st in stats.items():
        if not st.is_rest:
            continue
        idx = steady(phase_of == [p[0] for p in protocol].index(key))
        if idx.size < 8:
            continue
        seg = channels["ir"][idx]
        tt = t[idx]
        slope, inter = np.polyfit(tt, seg, 1)
        resid.append(seg - (slope * tt + inter))
    noise = float(np.std(np.concatenate(resid))) if resid else float("nan")

    # ABA contrast: each manoeuvre against the mean of the rests either side.
    keys = [p[0] for p in protocol]
    effects: dict[str, dict] = {}
    for i, (key, dur, label, is_rest) in enumerate(protocol):
        if is_rest or key not in stats:
            continue
        before = next((keys[j] for j in range(i - 1, -1, -1)
                       if protocol[j][3] and keys[j] in stats), None)
        after = next((keys[j] for j in range(i + 1, len(protocol))
                      if protocol[j][3] and keys[j] in stats), None)
        refs = [stats[k].ir_mean for k in (before, after) if k]
        if not refs:
            continue
        base = float(np.mean(refs))
        delta = stats[key].ir_mean - base
        dv_refs = [stats[k].dv_mean for k in (before, after) if k]
        effects[key] = {
            "label": label,
            "delta_counts": delta,
            "delta_pct": delta / base * 100.0 if base else float("nan"),
            "delta_dv_um": stats[key].dv_mean - float(np.mean(dv_refs)),
            "z": abs(delta) / noise if noise and np.isfinite(noise) else float("nan"),
            # Arm up drains blood -> less absorption -> HIGHER counts.
            "expected": "+" if key == "up" else "-",
            "sign_ok": (delta > 0) if key == "up" else (delta < 0),
        }

    return ProvocationResult(
        t=t, channels=channels, phase_of=phase_of, protocol=protocol,
        stats=stats, dv=dv, effects=effects, link=link or {},
    )


def format_result(r: ProvocationResult, site: str = "") -> str:
    lines = ["", "Provocation result" + (f" — {site}" if site else ""),
             "-" * 64]
    lines.append("  Phase levels (steady-state tail of each phase):")
    for st in r.stats.values():
        tag = "rest" if st.is_rest else " >> "
        lines.append(f"    {tag} {st.key:<6} {st.label:<42} "
                     f"IR {st.ir_mean:>10,.0f}   dV·L {st.dv_mean:>+8.2f} um")

    lines.append("")
    lines.append("  Manoeuvre effects (vs mean of the rests either side):")
    if not r.effects:
        lines.append("    none computed")
    for key, e in r.effects.items():
        mark = "ok" if e["sign_ok"] else "WRONG SIGN"
        lines.append(
            f"    {key:<6} {e['delta_counts']:>+9,.0f} counts "
            f"({e['delta_pct']:+.3f}%)   dV·L {e['delta_dv_um']:>+7.2f} um   "
            f"z={e['z']:>5.1f}   expected {e['expected']}   {mark}"
        )

    v = r.verdict
    lines.append("")
    if v == "responds":
        lines.append("  => The sensor RESPONDS to blood volume. The optical path "
                     "reaches perfused")
        lines.append("     tissue, and this site is viable for return-to-flow "
                     "regardless of whether")
        lines.append("     a cardiac pulse is resolvable.")
    elif v == "inverted":
        lines.append("  => Response has the WRONG SIGN. Arm up should RAISE counts "
                     "(less blood,")
        lines.append("     less absorption). Check the channel labelling and that "
                     "the sensor is")
        lines.append("     not being squeezed by the strap as you move.")
    elif v == "weak":
        lines.append("  => Marginal response. Something is coupling, but barely. "
                     "Tighten contact,")
        lines.append("     hold each position longer, and re-run.")
    else:
        lines.append("  => NO response to blood volume. This is the strong version "
                     "of the")
        lines.append("     light-piping diagnosis: light is reaching the photodiode "
                     "without")
        lines.append("     passing through perfused tissue. Press the sensor flat, "
                     "strap it, and")
        lines.append("     shade it. Raising LED current will not help.")
    return "\n".join(lines)


def plot(r: ProvocationResult, site: str = "", save=None, show: bool = True) -> None:
    import matplotlib.pyplot as plt

    colors = {"ir": "#1f4e79", "red": "#c43b2f"}
    phase_colors = {True: "#dddddd", False: "#9ecae1"}

    n_rows = 2 + (1 if "red" in r.channels else 0)
    fig, axes = plt.subplots(n_rows, 1, figsize=(12, 3.0 * n_rows), sharex=True)
    axes = np.atleast_1d(axes)

    def shade(ax):
        for i, (key, dur, label, is_rest) in enumerate(r.protocol):
            m = r.phase_of == i
            if not m.any():
                continue
            a, b = r.t[m][0], r.t[m][-1]
            ax.axvspan(a, b, color=phase_colors[is_rest],
                       alpha=0.45 if not is_rest else 0.30, lw=0)
            if ax is axes[0]:
                ax.text((a + b) / 2, 0.97, key.upper(),
                        transform=ax.get_xaxis_transform(), ha="center",
                        va="top", fontsize=8, fontweight="bold", color="#333")

    row = 0
    ax = axes[row]
    ax.plot(r.t, r.channels["ir"], lw=0.8, color=colors["ir"])
    shade(ax)
    ax.set_ylabel("IR raw\n[ADC counts]", fontsize=9)
    ax.set_title("Arm UP should RAISE counts (blood drains); arm DOWN should "
                 "LOWER them", fontsize=8.5, loc="left", color="#444", pad=14)
    row += 1

    if "red" in r.channels:
        ax = axes[row]
        ax.plot(r.t, r.channels["red"], lw=0.8, color=colors["red"])
        shade(ax)
        ax.set_ylabel("RED raw\n[ADC counts]", fontsize=9)
        row += 1

    ax = axes[row]
    ax.plot(r.t, r.dv, lw=1.0, color="#0a7d2c")
    shade(ax)
    ax.axhline(0, color="k", lw=0.5, alpha=0.5)
    ax.set_ylabel("Blood volume\nΔV·L [um]", fontsize=9)
    ax.set_xlabel("time (s)", fontsize=9)
    ax.set_title("Same units as the return-to-flow onset detector", fontsize=8.5,
                 loc="left", color="#444", pad=3)

    for a in axes:
        a.grid(alpha=0.25, lw=0.5)
        a.margins(x=0)
        a.tick_params(labelsize=8)

    fig.suptitle(f"MAPQuest provocation test{' — ' + site if site else ''} — "
                 f"verdict: {r.verdict.upper()}",
                 fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.97))

    if save:
        fig.savefig(save, dpi=130, bbox_inches="tight")
        print(f"  Saved plot to {save}")
    if show:
        plt.show()
    else:
        plt.close(fig)
