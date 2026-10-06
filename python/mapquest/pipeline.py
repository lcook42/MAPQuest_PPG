"""The PPG derivation chain, with every intermediate stage retained.

The point of this module is that nothing is hidden. Each step from raw ADC
counts to the second derivative is kept and labelled so it can be plotted and
inspected. If the final d2V/dt2 looks wrong, you can see which stage broke it.

Two deliberate choices worth knowing about:

* **Zero-phase filtering** (``sosfiltfilt``, not ``sosfilt``). A causal filter
  imposes a frequency-dependent group delay, which shifts fiducial points in
  the derivatives by an unknown amount -- fatal for morphology work. The cost
  is that this is non-causal: it needs the whole buffer, so it is offline or
  windowed processing, not true sample-by-sample real time.

* **Savitzky-Golay derivatives**, not ``np.diff``. Finite differencing
  multiplies high-frequency noise by omega per derivative, so a second
  derivative via ``np.diff`` is usually unusable. SG fits a local polynomial
  and differentiates it analytically -- smoothing and differentiation in one
  well-conditioned step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

# np.trapz was renamed to np.trapezoid in numpy 2.0.
_trapz = getattr(np, "trapezoid", None) or np.trapz

# (key, label, y-axis unit) for each retained stage, in pipeline order.
STAGES: tuple[tuple[str, str, str], ...] = (
    ("raw", "0. Raw ADC counts", "counts"),
    ("dc", "1. DC baseline (<0.4 Hz)", "counts"),
    ("ac", "2. AC = raw - DC", "counts"),
    ("norm", "3. Normalised AC/DC", "ratio"),
    ("filt", "4. Band-pass 0.5-8 Hz", "ratio"),
    ("d1", "5. dV/dt", "ratio/s"),
    ("d2", "6. d2V/dt2", "ratio/s2"),
)


@dataclass
class PipelineConfig:
    """Tunable parameters for the derivation chain."""

    dc_cutoff_hz: float = 0.4
    """Low-pass corner isolating the DC/baseline component."""

    band_hz: tuple[float, float] = (0.5, 8.0)
    """Band-pass for the pulsatile component. 0.5 Hz = 30 bpm floor; 8 Hz keeps
    enough harmonics for the dicrotic notch without admitting much noise."""

    filter_order: int = 2
    """Per-section Butterworth order. Doubled by filtfilt's forward-backward pass."""

    sg_window_s: float = 0.15
    """Savitzky-Golay window length in seconds. Longer = smoother derivatives
    but blunter fiducial peaks. 150 ms is ~1/5 of a beat at 70 bpm."""

    sg_polyorder: int = 3
    """Must exceed the derivative order; cubic supports a clean 2nd derivative."""

    invert: bool = False
    """Flip sign so the systolic upstroke is positive. See docs/design.md §5."""

    def sg_window(self, fs: float) -> int:
        """Window length in samples: odd, >= polyorder+2."""
        n = int(round(self.sg_window_s * fs))
        if n % 2 == 0:
            n += 1
        return max(n, self.sg_polyorder + 2 + (self.sg_polyorder % 2))


@dataclass
class Derivation:
    """All stages for a single channel."""

    name: str
    fs: float
    t: np.ndarray
    raw: np.ndarray
    dc: np.ndarray
    ac: np.ndarray
    norm: np.ndarray
    filt: np.ndarray
    d1: np.ndarray
    d2: np.ndarray
    config: PipelineConfig = field(default_factory=PipelineConfig)

    def stage(self, key: str) -> np.ndarray:
        return getattr(self, key)


def _sos_zero_phase(sos: np.ndarray, x: np.ndarray) -> np.ndarray:
    """filtfilt with a padlen that cannot exceed the signal length."""
    # scipy's default padlen can be longer than a short buffer, which raises.
    # Clamp it; for live windows this matters.
    default_pad = 3 * (2 * len(sos) + 1)
    padlen = min(default_pad, max(len(x) // 3 - 1, 0))
    return signal.sosfiltfilt(sos, x, padlen=padlen)


def derive(
    raw: np.ndarray,
    fs: float,
    name: str = "ppg",
    t: np.ndarray | None = None,
    config: PipelineConfig | None = None,
) -> Derivation:
    """Run the full chain on one channel.

    Parameters
    ----------
    raw : raw ADC counts
    fs : output sample rate in Hz (from the firmware header, not assumed)
    """
    cfg = config or PipelineConfig()
    raw = np.asarray(raw, dtype=float)
    n = raw.size
    if t is None:
        t = np.arange(n) / fs

    nyq = fs / 2.0
    if n < 32:
        raise ValueError(f"Need at least 32 samples to derive, got {n}")

    # -- 1. DC baseline ----------------------------------------------------
    # Everything below 0.4 Hz: contact pressure drift, venous pooling, thermal
    # wander. Also the denominator for normalisation.
    sos_lp = signal.butter(cfg.filter_order, cfg.dc_cutoff_hz / nyq, btype="low", output="sos")
    dc = _sos_zero_phase(sos_lp, raw)

    # -- 2. AC component ---------------------------------------------------
    ac = raw - dc

    # -- 3. Normalise ------------------------------------------------------
    # AC/DC removes the dependence on LED drive current, skin tone and contact
    # coupling, so green and IR become directly comparable. This ratio is the
    # perfusion index when taken as an RMS.
    with np.errstate(divide="ignore", invalid="ignore"):
        norm = np.where(np.abs(dc) > 1e-9, ac / dc, 0.0)

    # -- 4. Band-pass ------------------------------------------------------
    lo, hi = cfg.band_hz
    hi = min(hi, nyq * 0.95)  # keep the corner inside Nyquist at low fs
    sos_bp = signal.butter(cfg.filter_order, [lo / nyq, hi / nyq], btype="band", output="sos")
    filt = _sos_zero_phase(sos_bp, norm)

    if cfg.invert:
        filt = -filt

    # -- 5/6. Derivatives --------------------------------------------------
    win = min(cfg.sg_window(fs), n if n % 2 else n - 1)
    d1 = signal.savgol_filter(filt, win, cfg.sg_polyorder, deriv=1, delta=1.0 / fs)
    d2 = signal.savgol_filter(filt, win, cfg.sg_polyorder, deriv=2, delta=1.0 / fs)

    return Derivation(
        name=name, fs=fs, t=t, raw=raw, dc=dc, ac=ac,
        norm=norm, filt=filt, d1=d1, d2=d2, config=cfg,
    )


def quality_metrics(d: Derivation, full_scale: int = (1 << 18) - 1) -> dict[str, float]:
    """Numbers for deciding whether a recording -- or a channel -- is any good.

    See docs/led-selection-report.md §6: these are the metrics for settling
    green vs IR on evidence rather than on literature.
    """
    fs = d.fs

    dc_mean = float(np.mean(d.dc))
    ac_rms = float(np.sqrt(np.mean(d.ac**2)))

    # Perfusion index is computed on the BAND-LIMITED signal, not on raw AC.
    # Raw AC = raw - dc still contains the full broadband noise floor, so on a
    # noisy channel it reports noise as if it were perfusion -- which would
    # corrupt exactly the green-vs-IR comparison this metric exists to settle.
    # `filt` is already normalised by DC, so its rms *is* the ratio.
    pulsatile_rms = float(np.sqrt(np.mean(d.filt**2)))
    pulsatile_p2p = float(np.percentile(d.filt, 97.5) - np.percentile(d.filt, 2.5))

    # Headroom: are we clipping the ADC, or wasting its range?
    clipped = float(np.mean((d.raw >= full_scale) | (d.raw <= 0)) * 100.0)
    dc_utilisation = dc_mean / full_scale * 100.0

    # Derivative SNR: pulse band vs the noise floor above it, measured on d2
    # because that is where noise does the most damage.
    def band_snr(x: np.ndarray) -> float:
        nperseg = int(min(len(x), max(256, fs * 4)))
        f, pxx = signal.welch(x, fs=fs, nperseg=nperseg)
        sig_band = (f >= 0.5) & (f <= 8.0)
        noise_band = (f >= 15.0) & (f <= min(50.0, fs / 2 * 0.95))
        if not noise_band.any() or not sig_band.any():
            return float("nan")
        sig = float(_trapz(pxx[sig_band], f[sig_band]))
        noise = float(_trapz(pxx[noise_band], f[noise_band]))
        return 10.0 * np.log10(sig / noise) if noise > 0 else float("inf")

    # Dominant pulse frequency -> heart rate sanity check.
    nperseg = int(min(len(d.filt), max(256, fs * 8)))
    f, pxx = signal.welch(d.filt, fs=fs, nperseg=nperseg)
    hr_band = (f >= 0.7) & (f <= 3.5)  # 42-210 bpm
    hr_bpm = float(f[hr_band][np.argmax(pxx[hr_band])] * 60.0) if hr_band.any() else float("nan")

    return {
        "dc_mean": dc_mean,
        "ac_rms": ac_rms,
        "perfusion_index_pct": pulsatile_rms * 100.0,
        "perfusion_p2p_pct": pulsatile_p2p * 100.0,
        "dc_utilisation_pct": dc_utilisation,
        "clipped_pct": clipped,
        "snr_filt_db": band_snr(d.filt),
        "snr_d1_db": band_snr(d.d1),
        "snr_d2_db": band_snr(d.d2),
        "hr_bpm": hr_bpm,
    }


def format_metrics(name: str, m: dict[str, float]) -> str:
    """Human-readable metric block, with inline interpretation."""
    lines = [f"  {name}:"]
    lines.append(f"    DC level            {m['dc_mean']:>12,.0f} counts "
                 f"({m['dc_utilisation_pct']:.1f}% of full scale)")
    lines.append(f"    AC rms (broadband)  {m['ac_rms']:>12,.0f} counts")
    lines.append(f"    Perfusion index     {m['perfusion_index_pct']:>12.3f} % rms")
    lines.append(f"    Pulse amplitude     {m['perfusion_p2p_pct']:>12.3f} % p2p")
    lines.append(f"    Clipped samples     {m['clipped_pct']:>12.3f} %")
    lines.append(f"    SNR (filtered)      {m['snr_filt_db']:>12.1f} dB")
    lines.append(f"    SNR (dV/dt)         {m['snr_d1_db']:>12.1f} dB")
    lines.append(f"    SNR (d2V/dt2)       {m['snr_d2_db']:>12.1f} dB")
    lines.append(f"    Dominant rate       {m['hr_bpm']:>12.1f} bpm")

    warnings = []
    if m["clipped_pct"] > 0.1:
        warnings.append("ADC is clipping — reduce LED current")
    elif m["dc_utilisation_pct"] > 80:
        warnings.append("DC near full scale — reduce LED current for headroom")
    elif m["dc_utilisation_pct"] < 10:
        warnings.append("DC very low — raise LED current to use more ADC range")
    if m["perfusion_index_pct"] < 0.05:
        warnings.append("very weak pulsatile signal — check contact pressure and placement")
    if m["snr_d2_db"] < 0:
        warnings.append("2nd derivative is noise-dominated — "
                        "lengthen sg_window_s or raise the sample rate")
    if not (40 <= m["hr_bpm"] <= 180):
        warnings.append("dominant rate outside a plausible heart rate — likely artifact")

    for w in warnings:
        lines.append(f"    ! {w}")
    return "\n".join(lines)
