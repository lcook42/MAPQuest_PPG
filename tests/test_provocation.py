"""Tests for the arm-raise provocation analysis (host/provocation.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "host"
sys.path.insert(0, str(HOST))

import provocation as pv  # noqa: E402

FS = 100.0


def build(resp_counts: float, drift_counts: float = 0.0,
          invert: bool = False, noise: float = 45.0, seed: int = 3):
    """Synthesise a provocation run with a known response.

    ``resp_counts`` is the steady-state IR change for arm UP, positive meaning
    counts rise (blood drains, less absorption). Arm DOWN gets 80% of that
    magnitude with the opposite sign.
    """
    rng = np.random.default_rng(seed)
    proto = pv.DEFAULT_PROTOCOL

    phases, segs = [], []
    for i, (k, d, _l, _r) in enumerate(proto):
        n = int(d * FS)
        phases += [i] * n
        segs.append((k, n))

    n = len(phases)
    t = np.arange(n) / FS
    ir = np.full(n, 109_000.0)

    off = 0
    for key, ln in segs:
        level = 0.0
        if key == "up":
            level = resp_counts
        elif key == "down":
            level = -resp_counts * 0.8
        if invert:
            level = -level
        # Venous volume settles with a time constant, it does not step.
        tau = np.arange(ln) / FS
        ir[off:off + ln] += level * (1 - np.exp(-tau / 4.0))
        off += ln

    ir += drift_counts * (t / t[-1])
    ir += rng.normal(0, noise, n)
    red = 131_000 + (ir - 109_000) * 0.8 + rng.normal(0, noise * 1.2, n)
    return {"ir": ir, "red": red}, np.asarray(phases), proto


def analyse(resp, **kw):
    ch, ph, proto = build(resp, **kw)
    return pv.analyse(ch, ph, proto, FS, dark={"ir": 11, "red": 13})


# ---------------------------------------------------------------------------

def test_clear_response_detected():
    r = analyse(600.0)
    assert r.verdict == "responds"
    assert all(e["sign_ok"] for e in r.effects.values())
    assert r.effects["up"]["z"] > 5


def test_drift_alone_does_not_fake_a_response():
    """The failure mode the ABA design exists to prevent.

    Baseline drift at these timescales is large -- 1500 counts over 85 s is
    realistic. Comparing a manoeuvre only against the rest *before* it would
    read that drift as a response. Referencing the mean of the rests on both
    sides cancels it.
    """
    r = analyse(0.0, drift_counts=1500.0)
    assert r.verdict == "flat", r.effects
    for e in r.effects.values():
        assert e["z"] < 2.0, f"drift leaked into the effect size: {e}"


def test_inverted_response_flagged():
    """Wrong sign is as diagnostic as no response, and must not read as success."""
    r = analyse(600.0, invert=True)
    assert r.verdict == "inverted"
    assert not any(e["sign_ok"] for e in r.effects.values())


def test_weak_response_is_not_claimed_as_success():
    r = analyse(120.0)
    assert r.verdict in ("weak", "flat")
    assert r.verdict != "responds"


def test_sign_convention_arm_up_raises_counts():
    """More blood -> more absorption -> fewer counts, so UP must be positive."""
    r = analyse(600.0)
    assert r.effects["up"]["delta_counts"] > 0
    assert r.effects["down"]["delta_counts"] < 0
    assert r.effects["up"]["expected"] == "+"
    assert r.effects["down"]["expected"] == "-"


def test_blood_volume_sign_opposes_counts():
    """Arm up drains blood, so dV*L must go negative while counts rise."""
    r = analyse(600.0)
    assert r.effects["up"]["delta_dv_um"] < 0
    assert r.effects["down"]["delta_dv_um"] > 0


def test_steady_state_uses_phase_tail():
    """Early samples in a phase are mid-transition and must not dominate."""
    r = analyse(600.0)
    up = r.stats["up"]
    # The settling constant is 4 s; a tail-based mean should land near the
    # full level, not halfway through the exponential.
    assert up.ir_mean - r.stats["rest1"].ir_mean > 500


def test_all_phases_present_in_stats():
    r = analyse(300.0)
    assert set(r.stats) == {k for k, _d, _l, _r in pv.DEFAULT_PROTOCOL}
    for st in r.stats.values():
        assert st.n > 0


def test_format_result_mentions_verdict():
    r = analyse(600.0)
    text = pv.format_result(r, site="wrist")
    assert "wrist" in text
    assert "RESPONDS" in text or "responds" in text.lower()
    assert "up" in text


def test_format_result_explains_a_flat_run():
    r = analyse(0.0)
    text = pv.format_result(r, site="wrist")
    assert "light-piping" in text or "NO response" in text


def test_plot_renders_headless(tmp_path):
    import matplotlib
    matplotlib.use("Agg")
    r = analyse(600.0)
    out = tmp_path / "p.png"
    pv.plot(r, site="wrist", save=out, show=False)
    assert out.exists() and out.stat().st_size > 5000


def test_ir_only_fallback_when_red_missing():
    ch, ph, proto = build(600.0)
    del ch["red"]
    r = pv.analyse(ch, ph, proto, FS, dark={"ir": 11})
    assert r.verdict == "responds"
    assert np.all(np.isfinite(r.dv))


@pytest.mark.parametrize("seconds", [8.0, 30.0])
def test_phase_duration_override(seconds):
    """--phase-seconds rescales every phase without breaking the analysis."""
    proto = [(k, seconds, lbl, rest)
             for k, _d, lbl, rest in pv.DEFAULT_PROTOCOL]
    rng = np.random.default_rng(0)
    phases = []
    for i, _p in enumerate(proto):
        phases += [i] * int(seconds * FS)
    n = len(phases)
    ir = np.full(n, 109_000.0) + rng.normal(0, 40, n)
    ph = np.asarray(phases)
    ir[ph == 1] += 600
    ir[ph == 3] -= 480
    r = pv.analyse({"ir": ir, "red": ir * 1.2}, ph, proto, FS)
    assert r.verdict == "responds"


def test_csv_roundtrip(tmp_path):
    """A saved capture must reanalyse to the same verdict."""
    ch, ph, proto = build(600.0)
    keys = [p[0] for p in proto]
    csv = tmp_path / "p.csv"
    with csv.open("w") as fh:
        fh.write("# source=provocation\n# fs=100.00\n# site=wrist\n")
        fh.write("# ir_amp=42 red_amp=99\n")
        fh.write("# dark_ir=11\n# dark_red=14\n")
        fh.write(f"# phases={','.join(keys)}\n")
        fh.write("idx,phase,ir,red\n")
        for i in range(len(ph)):
            fh.write(f"{i},{keys[ph[i]]},{ch['ir'][i]:.0f},{ch['red'][i]:.0f}\n")

    channels, phase_of, protocol, meta = pv.load_csv(csv)
    assert meta["_fs"] == 100.0
    assert meta["_dark"] == {"ir": 11.0, "red": 14.0}
    assert meta["ir_amp"] == "42"          # LED current must survive the trip
    assert set(channels) == {"ir", "red"}
    assert len(phase_of) == len(ph)

    r = pv.analyse(channels, phase_of, protocol, meta["_fs"], dark=meta["_dark"])
    assert r.verdict == "responds"


def test_load_csv_rebuilds_phase_durations(tmp_path):
    """Durations come from the data, so --phase-seconds runs reanalyse too."""
    proto = [(k, 5.0, lbl, rest) for k, _d, lbl, rest in pv.DEFAULT_PROTOCOL]
    keys = [p[0] for p in proto]
    n_each = 500
    csv = tmp_path / "short.csv"
    with csv.open("w") as fh:
        fh.write("# fs=100.00\n")
        fh.write(f"# phases={','.join(keys)}\n")
        fh.write("idx,phase,ir\n")
        i = 0
        for k in keys:
            for _ in range(n_each):
                fh.write(f"{i},{k},109000\n")
                i += 1
    _ch, phase_of, protocol, _meta = pv.load_csv(csv)
    assert len(protocol) == len(keys)
    for _k, dur, _l, _r in protocol:
        assert abs(dur - 5.0) < 1e-6
    assert (phase_of == 0).sum() == n_each
