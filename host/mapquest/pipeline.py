"""Return-to-flow analysis pipeline (docs/SPEC.md section 5).

The measurement: an upper-arm cuff is inflated above arterial pressure, then
bled down at ~2 mmHg/s. While the cuff is above systolic the forearm's blood
volume is frozen. Once it falls below arterial pressure but stays above venous
pressure, blood flows in and cannot drain, so the forearm fills, reflected
light falls, and delta-OD rises. MAP is read off as the cuff pressure at the
onset of sustained filling.

The signal of interest is therefore the SLOW DC trend, not the pulsatile
waveform. Every stage is retained so the chain can be inspected end to end:

    raw counts
      -> dark-subtracted
      -> delta-OD        ln(ref / counts), per wavelength
      -> delta V*L       two-wavelength Beer-Lambert solve, in micrometres
      -> low-pass        0.3 Hz Butterworth, zero-phase
      -> dV/dt           Savitzky-Golay, 2.0 s window
      -> onset           first sustained excursion above mu0 + k*sigma0

Two choices worth knowing about:

* **Zero-phase filtering** (``filtfilt``). A causal filter group-delays the
  signal, which would bias the onset time and therefore the reported pressure.
  Offline we can afford to be non-causal and avoid that entirely. The real-time
  variant (SPEC 5.3) has to subtract a known delay instead.

* **A 2.0 s derivative window.** The HeartMate 3 artificial pulse repeats every
  2 s, so a window of exactly one period cancels it and its harmonics. This
  keeps the same pipeline valid on LVAD data without re-tuning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

# Two-wavelength Beer-Lambert inversion, SPEC 5.1 step 6. Coefficients convert
# per-wavelength optical density change into blood volume per unit area,
# expressed as a path-length-scaled volume dV*L in micrometres.
COEFF_RED_660 = 229.0
COEFF_IR_880 = 1555.0

# IR-only fallback: dV*L = delta-OD_880 / 6.13 cm^-1 (SPEC 5.1 step 6).
#
# That expression yields CENTIMETRES, while the two-wavelength solve above
# yields MICROMETRES. Mixing them unconverted would put the two paths 1e4
# apart, so the fallback is converted to um to keep one unit across the
# pipeline. Note 1e4/6.13 = 1631 um/OD lands close to the two-wavelength
# COEFF_IR_880 of 1555, which is the consistency check that the conversion is
# the right way round: the fallback is the two-wavelength formula with the red
# term dropped, not a different quantity.
MU_A_IR_PER_CM = 6.13
UM_PER_CM = 1.0e4

# (key, label, unit) for each retained stage, in pipeline order.
STAGES: tuple[tuple[str, str, str], ...] = (
    ("raw", "0. Raw counts", "counts"),
    ("dark_sub", "1. Dark-subtracted", "counts"),
    ("d_od", "2. delta-OD", "OD"),
    ("dv", "3. delta V*L", "um"),
    ("dv_lp", "4. Low-pass 0.3 Hz", "um"),
    ("ddv", "5. d(V*L)/dt", "um/s"),
)


@dataclass
class PipelineConfig:
    """Tunable parameters. Defaults follow SPEC 5.1."""

    lowpass_hz: float = 0.3
    """Butterworth corner for the volume signal."""

    filter_order: int = 2

    sg_window_s: float = 2.0
    """Savitzky-Golay window. One HeartMate 3 artificial-pulse period."""

    sg_polyorder: int = 2

    k_sigma: float = 3.0
    """Onset threshold in noise-floor standard deviations."""

    hold_s: float = 2.0
    """Excursion must be sustained this long to count as onset."""

    onset_method: str = "intersect"
    """``"threshold"`` is SPEC 5.1 step 10 verbatim: the first sample of a
    sustained excursion above mu0 + k*sigma0.

    That method is biased EARLY, and the bias is structural rather than noise.
    Both the 0.3 Hz zero-phase low-pass and the 2 s Savitzky-Golay window are
    symmetric kernels, so they smear the ramp's corner backwards in time as
    well as forwards. The derivative therefore starts climbing roughly half a
    kernel before the true onset, and at 2 mmHg/s that reads as a few mmHg too
    high -- measured at +2.6 mmHg with the SPEC defaults, against a SPEC 6
    acceptance criterion of +/-1 mmHg.

    ``"intersect"`` removes that bias: it fits the established filling ramp and
    extrapolates back to where it meets the occlusion baseline. Because it uses
    the ramp's slope and offset rather than the location of its corner, corner
    smearing cancels. This is the default because it is the one that meets the
    acceptance criterion; pass ``--onset-method threshold`` for the literal
    SPEC behaviour."""

    fit_s: float = 5.0
    """Length of the established-ramp segment fitted by the intersect method."""

    ir_only: bool = False
    """Use the single-wavelength fallback instead of the two-wavelength solve."""

    pulsatility_band: tuple[float, float] = (0.5, 5.0)
    """Band for the informational pulsatility check (SPEC 5.1 step 12)."""


@dataclass
class Derivation:
    """All retained stages plus the derived result."""

    fs: float
    t: np.ndarray
    raw: dict[str, np.ndarray]
    dark_sub: dict[str, np.ndarray]
    d_od: dict[str, np.ndarray]
    dv: np.ndarray
    dv_lp: np.ndarray
    ddv: np.ndarray
    ref_slice: slice
    mu0: float
    sigma0: float
    threshold: float
    onset_index: int | None
    config: PipelineConfig = field(default_factory=PipelineConfig)

    # Both estimators are always computed so the two can be compared on any
    # recording. `onset_index`/`onset_time` follow config.onset_method.
    threshold_index: int | None = None
    onset_time_intersect: float | None = None
    ramp_slope: float = float("nan")
    ramp_fit: tuple[float, float] | None = None
    """(slope, intercept) of the fitted ramp in dv_lp, for plotting."""

    @property
    def onset_time(self) -> float | None:
        if self.config.onset_method == "intersect":
            return self.onset_time_intersect
        return None if self.threshold_index is None else float(self.t[self.threshold_index])

    @property
    def threshold_time(self) -> float | None:
        return None if self.threshold_index is None else float(self.t[self.threshold_index])

    def stage(self, key: str):
        return getattr(self, key)


def _zero_phase_lowpass(x: np.ndarray, fs: float, cutoff: float, order: int) -> np.ndarray:
    sos = signal.butter(order, cutoff / (fs / 2.0), btype="low", output="sos")
    default_pad = 3 * (2 * len(sos) + 1)
    padlen = min(default_pad, max(len(x) // 3 - 1, 0))
    return signal.sosfiltfilt(sos, x, padlen=padlen)


def _sg_window_samples(cfg: PipelineConfig, fs: float, n: int) -> int:
    w = int(round(cfg.sg_window_s * fs))
    if w % 2 == 0:
        w += 1
    w = max(w, cfg.sg_polyorder + 2 + (cfg.sg_polyorder % 2))
    if w > n:
        w = n if n % 2 else n - 1
    return w


def delta_od(counts: np.ndarray, reference: float) -> np.ndarray:
    """delta-OD = ln(reference / counts). Rises as the tissue darkens.

    Counts are clamped away from zero first: a dark-subtracted count can land
    at or below zero on a noisy sample, and log of that would poison the whole
    downstream chain with inf/nan rather than failing visibly.
    """
    safe = np.maximum(counts, 1e-9)
    return np.log(max(reference, 1e-9) / safe)


def blood_volume(d_od: dict[str, np.ndarray], ir_only: bool) -> np.ndarray:
    """Convert per-wavelength delta-OD into dV*L in micrometres (SPEC 5.1 step 6)."""
    if ir_only or "red" not in d_od:
        return d_od["ir"] / MU_A_IR_PER_CM * UM_PER_CM
    return COEFF_RED_660 * d_od["red"] + COEFF_IR_880 * d_od["ir"]


def find_onset(
    ddv: np.ndarray,
    fs: float,
    search_from: int,
    mu0: float,
    sigma0: float,
    k: float,
    hold_s: float,
) -> tuple[int | None, float]:
    """First sample after ``search_from`` where dV/dt stays above threshold.

    Returns ``(index_of_run_start, threshold)``. The index is the FIRST sample
    of the qualifying run, not the end of it -- the cuff pressure there is the
    reported result (SPEC 5.1 step 11).
    """
    threshold = mu0 + k * sigma0
    hold_n = max(int(round(hold_s * fs)), 1)

    above = ddv > threshold
    above[:search_from] = False
    if not above.any():
        return None, threshold

    # Find a run of `hold_n` consecutive True values via a cumulative-sum
    # window, which is O(n) rather than scanning each candidate start.
    c = np.concatenate(([0], np.cumsum(above.astype(np.int32))))
    if len(above) < hold_n:
        return None, threshold
    run_sums = c[hold_n:] - c[:-hold_n]
    starts = np.flatnonzero(run_sums == hold_n)
    if starts.size == 0:
        return None, threshold
    return int(starts[0]), threshold


def ramp_intersection(
    dv_lp: np.ndarray,
    t: np.ndarray,
    fs: float,
    ref_slice: slice,
    crossing_index: int,
    hold_s: float,
    fit_s: float,
) -> tuple[float | None, float, tuple[float, float] | None]:
    """Extrapolate the established filling ramp back to the baseline.

    The threshold crossing tells us roughly where filling began; this refines
    it. We fit a straight line to the part of the ramp that is unambiguously
    established -- starting one hold period after the crossing, so the fit
    never includes the smeared corner -- and intersect that line with the mean
    baseline level from the occlusion reference window.

    Corner smearing shifts WHERE the signal starts to bend, but not the slope
    or offset of the straight part that follows, so the intersection is
    unbiased by the filtering.

    Returns ``(onset_time, slope, (slope, intercept))``.
    """
    baseline = float(np.mean(dv_lp[ref_slice]))

    i0 = crossing_index + int(round(hold_s * fs))
    i1 = min(i0 + int(round(fit_s * fs)), len(dv_lp))
    if i1 - i0 < 8:
        # Too little ramp left to fit; fall back to whatever remains.
        i0 = min(crossing_index, len(dv_lp) - 8)
        i1 = len(dv_lp)
        if i1 - i0 < 8:
            return None, float("nan"), None

    slope, intercept = np.polyfit(t[i0:i1], dv_lp[i0:i1], 1)
    if slope <= 0:
        return None, float(slope), (float(slope), float(intercept))

    onset_t = (baseline - intercept) / slope

    # The extrapolation must land inside the record and at or after the
    # reference window; otherwise the ramp fit was not describing real filling.
    lo, hi = float(t[ref_slice.stop - 1]), float(t[-1])
    if not (lo - 1.0 <= onset_t <= hi):
        return None, float(slope), (float(slope), float(intercept))

    return float(onset_t), float(slope), (float(slope), float(intercept))


def pulsatility_rms(x: np.ndarray, fs: float, band: tuple[float, float]) -> float:
    """RMS of the cardiac band during the reference window (SPEC 5.1 step 12).

    Informational: a healthy pulsatile subject will show a clear value here,
    which is the flag that onset is reading near systolic rather than MAP.
    """
    nyq = fs / 2.0
    lo, hi = band[0] / nyq, min(band[1], nyq * 0.95) / nyq
    if not (0 < lo < hi < 1) or len(x) < 32:
        return float("nan")
    sos = signal.butter(2, [lo, hi], btype="band", output="sos")
    padlen = min(3 * (2 * len(sos) + 1), max(len(x) // 3 - 1, 0))
    return float(np.sqrt(np.mean(signal.sosfiltfilt(sos, x, padlen=padlen) ** 2)))


def derive(
    channels: dict[str, np.ndarray],
    fs: float,
    dark: dict[str, float] | None = None,
    ref_window: tuple[float, float] | None = None,
    deflation_start_s: float | None = None,
    t: np.ndarray | None = None,
    config: PipelineConfig | None = None,
) -> Derivation:
    """Run the full chain.

    Parameters
    ----------
    channels : raw counts keyed by wavelength name ("ir", "red")
    fs : output sample rate, from the recording header
    dark : per-channel dark offsets from the DARK command
    ref_window : (start_s, end_s) occlusion reference window. Defaults to the
        last 3 s before ``deflation_start_s``, or the first 3 s of the record.
    deflation_start_s : onset search starts here; defaults to the reference
        window's end.
    """
    cfg = config or PipelineConfig()
    if "ir" not in channels:
        raise ValueError("an 'ir' channel is required")

    n = len(next(iter(channels.values())))
    if n < 64:
        raise ValueError(f"need at least 64 samples, got {n}")
    if t is None:
        t = np.arange(n) / fs

    # -- 1. Dark subtraction (SPEC 5.1 step 3) -----------------------------
    dark = dark or {}
    raw = {k: np.asarray(v, dtype=float) for k, v in channels.items()}
    dark_sub = {k: v - float(dark.get(k, 0.0)) for k, v in raw.items()}

    # -- 2. Reference window (SPEC 5.1 step 4) -----------------------------
    if ref_window is not None:
        i0 = int(np.searchsorted(t, ref_window[0]))
        i1 = int(np.searchsorted(t, ref_window[1]))
    elif deflation_start_s is not None:
        i1 = int(np.searchsorted(t, deflation_start_s))
        i0 = int(np.searchsorted(t, max(t[0], deflation_start_s - 3.0)))
    else:
        i0 = 0
        i1 = int(np.searchsorted(t, t[0] + 3.0))
    i0 = max(0, min(i0, n - 2))
    i1 = max(i0 + 2, min(i1, n))
    ref = slice(i0, i1)

    # -- 3. delta-OD (SPEC 5.1 step 5) -------------------------------------
    d_od = {k: delta_od(v, float(np.mean(v[ref]))) for k, v in dark_sub.items()}

    # -- 4. Blood volume (SPEC 5.1 step 6) ---------------------------------
    dv = blood_volume(d_od, cfg.ir_only)

    # -- 5. Low-pass (SPEC 5.1 step 7) -------------------------------------
    dv_lp = _zero_phase_lowpass(dv, fs, cfg.lowpass_hz, cfg.filter_order)

    # -- 6. Derivative (SPEC 5.1 step 8) -----------------------------------
    win = _sg_window_samples(cfg, fs, n)
    ddv = signal.savgol_filter(dv_lp, win, cfg.sg_polyorder, deriv=1, delta=1.0 / fs)

    # -- 7. Noise floor over the reference window (SPEC 5.1 step 9) --------
    mu0 = float(np.mean(ddv[ref]))
    sigma0 = float(np.std(ddv[ref]))

    # -- 8. Onset (SPEC 5.1 steps 10-11) -----------------------------------
    search_from = int(np.searchsorted(t, deflation_start_s)) if deflation_start_s is not None else i1
    threshold_index, threshold = find_onset(
        ddv, fs, search_from, mu0, sigma0, cfg.k_sigma, cfg.hold_s
    )

    # -- 9. Bias-free refinement (see PipelineConfig.onset_method) ---------
    onset_t_intersect: float | None = None
    slope = float("nan")
    ramp_fit = None
    if threshold_index is not None:
        onset_t_intersect, slope, ramp_fit = ramp_intersection(
            dv_lp, t, fs, ref, threshold_index, cfg.hold_s, cfg.fit_s
        )

    return Derivation(
        fs=fs, t=t, raw=raw, dark_sub=dark_sub, d_od=d_od,
        dv=dv, dv_lp=dv_lp, ddv=ddv,
        ref_slice=ref, mu0=mu0, sigma0=sigma0, threshold=threshold,
        onset_index=threshold_index, config=cfg,
        threshold_index=threshold_index,
        onset_time_intersect=onset_t_intersect,
        ramp_slope=slope, ramp_fit=ramp_fit,
    )


def calibrate_pressure(p_raw: np.ndarray, raw0: float, raw_ref: float,
                       p_ref: float) -> np.ndarray:
    """Two-point linear calibration (SPEC 4.1)."""
    span = raw_ref - raw0
    if abs(span) < 1e-9:
        raise ValueError("calibration span is zero — RAW0 and RAW_REF are identical")
    return (np.asarray(p_raw, dtype=float) - raw0) * p_ref / span


def resample_pressure(p_idx: np.ndarray, p_raw: np.ndarray,
                      n_samples: int) -> np.ndarray:
    """Put pressure on the PPG timebase (SPEC 5.1 step 2).

    The HX710B runs at its own ~40 Hz, and the firmware attaches the most
    recent value to each PPG sample. Interpolating on the HX710B's own sample
    counter recovers the real transitions instead of the stair-steps that
    holding the last value produces.
    """
    p_idx = np.asarray(p_idx, dtype=float)
    p_raw = np.asarray(p_raw, dtype=float)
    sample_pos = np.arange(n_samples, dtype=float)

    # Keep the first PPG sample at which each pressure reading appeared.
    _, first = np.unique(p_idx, return_index=True)
    first = np.sort(first)
    if first.size < 2:
        return np.full(n_samples, p_raw[0] if p_raw.size else np.nan)
    return np.interp(sample_pos, sample_pos[first], p_raw[first])
