"""Compare fixed wavelength pairs using independent cuff-deflated windows.

Selection is an engineering heuristic, not an estimate of RTF accuracy.
All three pairs are tested; detection never switches pairs during a trial.
"""
from itertools import combinations
from dataclasses import asdict
import importlib

try:
    # Load this optional tool dependency at runtime so editors using the
    # PlatformIO interpreter do not report a missing static import.
    np = importlib.import_module('numpy')
except ModuleNotFoundError as exc:
    raise RuntimeError(
        'channel_calibration.py requires NumPy; install it with "python -m pip install numpy"'
    ) from exc
from rtf_detector import Detector, extract

CHANNELS = ('RED', 'IR', 'GREEN')
PAIRS = tuple(combinations(range(3), 2))

def noise_profile(samples):
    """Descriptive spectral content after linear detrending, not a validated SNR.

    Low-frequency and high-frequency energy can contain physiological signal;
    these fractions diagnose the recording and never override pulse gates.
    """
    a=np.asarray(samples,dtype=float)
    t=np.arange(len(a))/100
    trend=np.column_stack((np.ones(len(a)),t-t.mean()))
    x=a-trend@np.linalg.lstsq(trend,a,rcond=None)[0]
    freq=np.fft.rfftfreq(len(a),.01)
    power=np.abs(np.fft.rfft(x,axis=0))**2
    # One-sided Parseval weights: DC/Nyquist are not doubled.
    power[1:-1]*=2
    total=np.maximum(power.sum(axis=0),1e-12)
    return {'slow_percent':(100*power[freq<.5].sum(axis=0)/total).tolist(),
            'high_frequency_percent':(100*power[freq>8].sum(axis=0)/total).tolist()}

def parse_three(line):
    parts=line.strip().split(',')
    if len(parts)!=10 or parts[0]!='D': raise ValueError('Invalid three-channel sample')
    row=tuple(map(int,parts[1:]))
    if (any(not 0<=v<=0xffffffff for v in row[:3])
        or any(not 0<=v<=262143 for v in row[3:6])
        or any(not 1<=v<=200 for v in row[6:])):
        raise ValueError('Three-channel sample out of range')
    return row

def candidates(leds):
    result=[list(leds)]
    # Change one emitter at a time; preserve independently adjusted brightness.
    for factor in (.75, 1.2):
        for ch in range(3):
            v=list(leds);v[ch]=int(np.clip(round(v[ch]*factor),1,200))
            if v not in result:result.append(v)
    return result

def compare(windows, leds, sensitive=False):
    """Rank pairs by the worse of two windows, with qualified stable pairs first."""
    results=[]
    for pair in PAIRS:
        labels=[CHANNELS[i] for i in pair]
        detector=Detector(sensitive,channels=labels)
        features=[extract(np.asarray(w)[:,pair]) for w in windows]
        valid=all(f.valid for f in features)
        stable=False; quality=-1.
        if valid:
            pulses=np.array([f.pulse for f in features])
            rates=np.array([np.mean(f.hz) for f in features])
            stable=bool(np.all(np.max(pulses,axis=0)/np.maximum(np.min(pulses,axis=0),1e-9)<=1.8)
                        and np.ptp(rates)<=.25)
            quality=min(min(f.fraction)*max(0.,min(f.periodicity))*max(0.,f.corr)
                        *max(0.,1-abs(f.hz[0]-f.hz[1])/.12) for f in features)
        qualified=valid and stable and all(detector._baseline_good(f) for f in features)
        results.append(dict(pair=list(pair),channels=labels,leds=list(leds),qualified=bool(qualified),
                            stable=stable,score=float(quality)+(2. if qualified else 0.),
                            feedback=[detector.baseline_feedback(f) for f in features],
                            spectral_content=[noise_profile(np.asarray(w)[:,pair]) for w in windows],
                            features=[asdict(f) for f in features]))
    return results
