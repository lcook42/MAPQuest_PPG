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
