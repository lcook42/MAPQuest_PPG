"""Transport-independent pulsatile RTF detector. No MAP regression or DC recovery.

All thresholds are research engineering settings, NOT clinical validation limits.
Non-overlapping four-second windows prevent counting the same beats twice.
"""
from dataclasses import dataclass, asdict
import math
import numpy as np

VERSION = '2026-10-12-r3-three'
DETECTOR_REVISION = '2026-10-12-r3-host-history1'
DC_SHIFT_LIMITS = {'RED': .25, 'IR': .25, 'GREEN': .40}
FS = 100
WINDOW = 400

@dataclass
class Features:
    valid: bool
    reason: str
    dc: tuple = (0., 0.)
    rms: tuple = (0., 0.)
    pulse: tuple = (0., 0.)
    fraction: tuple = (0., 0.)
    periodicity: tuple = (0., 0.)
    hz: tuple = (0., 0.)
    corr: float = 0.
    balance: tuple = (0., 0.)
    coherent: bool = False

def extract(samples, baseline_hz=None):
    """samples: N x 2 raw ADC counts, at 100 Hz, with gaps rejected upstream."""
    a = np.asarray(samples, dtype=float)
    if a.shape != (WINDOW, 2) or not np.isfinite(a).all():
        return Features(False, 'invalid sample window')
    dc = np.median(a, axis=0)
    if np.any(a < 500) or np.any(a > 250000):
        return Features(False, 'contact lost / clipped ADC', tuple(dc))
    if np.any(np.max(np.abs(np.diff(a, axis=0)), axis=0) > .012 * dc):
        return Features(False, 'abrupt optical jump / movement', tuple(dc))
    t = np.arange(WINDOW) / FS
    trend = np.column_stack((np.ones(WINDOW), t-t.mean()))
    beta = np.linalg.lstsq(trend, a, rcond=None)[0]
    if np.any(np.abs(beta[1] * 4) > .04 * dc):
        return Features(False, 'excessive optical drift', tuple(dc))
    x = a - trend @ beta
    rms = np.sqrt(np.mean(x*x, axis=0))
    if np.any(rms < .15):
        return Features(False, 'flatline / stuck ADC', tuple(dc), tuple(rms))
    # A zero-padded spectrum locates the fundamental; the fitted waveform is
    # evaluated on the original samples, not on interpolated artificial data.
    freq = np.fft.rfftfreq(8192, 1/FS)
    power = np.abs(np.fft.rfft(x*np.hanning(WINDOW)[:,None], n=8192, axis=0))**2
    lo, hi = (.65, 3.0) if baseline_hz is None else (max(.55,.70*baseline_hz), min(3.3,1.35*baseline_hz))
    band = (freq >= lo) & (freq <= hi)
    hz = freq[band][np.argmax(power[band], axis=0)]
    # Shared timing evidence matters: a pulse on only one wavelength is insufficient.
    pulse, fraction, acf, balance = [], [], [], []
    for ch in range(2):
        f = hz[ch]
        design = np.column_stack((trend, np.sin(2*np.pi*f*t), np.cos(2*np.pi*f*t),
                                  np.sin(4*np.pi*f*t), np.cos(4*np.pi*f*t)))
        coeff = np.linalg.lstsq(design, a[:,ch], rcond=None)[0]
        fitted = design[:,2:] @ coeff[2:]
        amp = float(np.sqrt(np.mean(fitted**2)))
        residual = a[:,ch] - design @ coeff
        fraction.append(float(np.clip(1-np.mean(residual**2)/max(rms[ch]**2,1e-9),0,1)))
        pulse.append(amp)
        # Lowpass solely for waveform repetition/correlation. No cross-window filter state.
        spectrum = np.fft.rfft(x[:,ch])
        spectrum[np.fft.rfftfreq(WINDOW,1/FS) > 8] = 0
        smooth = np.fft.irfft(spectrum,n=WINDOW)
        lag = int(round(FS/f))
        left,right = smooth[:-lag],smooth[lag:]
        den = np.linalg.norm(left)*np.linalg.norm(right)
        acf.append(float(np.dot(left,right)/den) if den>1e-9 else 0.)
        halves = np.sqrt(np.mean(x[:,ch].reshape(2,-1)**2,axis=1))
        balance.append(float(min(halves)/max(max(halves),1e-9)))
    corr = float(np.corrcoef(x.T)[0,1])
    coherent = bool(min(fraction)>=.40 and min(acf)>=.35 and corr>=.65
                    and abs(hz[0]-hz[1]) <= .12 and min(balance)>=.35)
    return Features(True, 'pulse' if coherent else 'no consistent pulse', tuple(dc), tuple(rms),
                    tuple(pulse), tuple(fraction), tuple(acf), tuple(hz), corr, tuple(balance), coherent)

class Detector:
    ACTIVE = ('INFLATING','OCCLUDED','DEFLATING','CANDIDATE','RTF')

    def __init__(self, sensitive=False, site='upper_arm', channels=('RED','IR')):
        if site not in ('upper_arm','forearm','unspecified'): raise ValueError('Unknown sensor site')
        self.site=site
        self.channels=tuple(channels)
        self.sensitive=sensitive
        self.state='IDLE'
        self.baselines=[]
        self.suppressed=[]
        self.base=None
        self.noise=None
        self.pending=None
        self.result=None
        self.last_feature=None
        self.last_time=None
        self.last_absent=None
        self.events=[]

    def event(self, text, **values):
        out={'state':self.state,'message':text,**values}
        self.events.append(out)
        return out

    def calibrate(self):
        if self.state in self.ACTIVE:
            raise ValueError('Release cuff before recalibrating')
        sensitive=self.sensitive
        self.__init__(sensitive,self.site,self.channels)
        self.state='BASELINE'
        return self.event('Keep cuff fully deflated and hold the sensor still: collecting 3 stable windows.')

    def arm(self, now):
        if self.state!='READY' or self.last_time is None or now-self.last_time>6:
            raise ValueError('A fresh, stable baseline is required before inflation')
        if not self.last_feature or not self._baseline_good(self.last_feature):
            raise ValueError('Pulse is not currently stable; keep cuff deflated and improve contact')
        self.state='INFLATING'; self.suppressed=[]; self.pending=None
        return self.event('Monitor cuff per your test protocol. Looking for sustained pulse suppression.')

    def deflate(self):
        if self.state!='OCCLUDED':
            raise ValueError('Sustained suppression must be detected before starting the return phase')
        self.state='DEFLATING'; self.pending=None
        return self.event('Release cuff gradually per protocol; enter gauge readings as annotations.')

    def abort(self, reason):
        self.state='ABORTED'; self.pending=None; self.suppressed=[]
        return self.event('STOP: fully release cuff. '+reason)

    def released(self):
        self.state='RECOVERY'; self.pending=None; self.suppressed=[]
        return self.event('Cuff release acknowledged. Checking pulse recovery; recalibrate before another trial.')

    def invalidate(self, reason):
        self.pending=None; self.suppressed=[]; self.last_absent=None
        if self.state=='BASELINE': self.baselines=[]
        if self.state=='CANDIDATE': self.state='DEFLATING'
        self.last_feature=None
        return self.event('Unusable window: '+reason)

    def _baseline_good(self, f):
        return f.valid and f.coherent and min(f.pulse)>=(4 if self.sensitive else 8) and min(f.fraction)>=.60 and min(f.periodicity)>=.5

    def baseline_feedback(self, f):
        if not f.valid: return f.reason
        reasons=[]
        for i,label in enumerate(self.channels):
            if f.pulse[i] < (4 if self.sensitive else 8):
                reasons.append(f'{label} pulse too small ({f.pulse[i]:.1f} counts)')
            if f.fraction[i] < .60:
                reasons.append(f'{label} pulse fit weak ({100*f.fraction[i]:.1f}%, need 60%)')
            if f.periodicity[i] < .5:
                reasons.append(f'{label} does not repeat reliably')
        if f.corr < .65: reasons.append('/'.join(self.channels)+' agreement too low')
        if abs(f.hz[0]-f.hz[1])>.12: reasons.append('/'.join(self.channels)+' timing differs')
        if min(f.balance)<.35: reasons.append('signal strength changes within window')
        return '; '.join(reasons) or 'collecting stable pulses'

    def suppression_feedback(self, f):
        """Describe rejection without treating lack of coherence as proof of occlusion."""
        if not f.valid or self.base is None:
            return 'Suppression unavailable: '+f.reason
        pulse=np.array(f.pulse)/self.base['pulse']
        total=np.array(f.rms)/self.base['rms']
        if f.coherent and np.any(pulse>.05):
            return 'Not suppressed: coherent pulse remains.'
        reasons=[]
        if np.any(pulse>.15):
            reasons.append('pulse-like amplitude exceeds 15% of baseline')
        if np.any(total>.25):
            detail=', '.join(f'{ch} {100*value:.0f}%' for ch,value in zip(self.channels,total) if value>.25)
            reasons.append('total fluctuation exceeds 25% of baseline ('+detail+'); drift/noise or residual signal prevents confirmation')
        return 'Suppression unconfirmed: '+'; '.join(reasons)+'.' if reasons else 'Suppression criteria met in this window; sustained confirmation still required.'

    def process(self, samples, start, end, annotation=None):
        f=extract(samples, self.base['hz'] if self.base else None)
        self.last_feature=f; self.last_time=end
        # Diagnostics may continue, but a stopped/inactive trial cannot generate
        # new analysis failures or accumulate evidence for a result.
        if self.state in ('IDLE','ABORTED','COMPLETE'):
            return f
        if not f.valid:
            self.invalidate(f.reason)
            return f
        if self.base and self.state not in ('BASELINE','ABORTED','RECOVERY','COMPLETE'):
            shift=np.array(f.dc)/self.base['dc']-1
            limits=np.array([DC_SHIFT_LIMITS[ch] for ch in self.channels])
            if np.any(np.abs(shift)>limits):
                detail=', '.join(f'{ch} {100*v:+.1f}%' for ch,v in zip(self.channels,shift))
                f.valid=False
                limit_detail=', '.join(f'{ch} {100*limit:.0f}%' for ch,limit in zip(self.channels,limits))
                f.reason=('Optical baseline changed beyond the comparison limits ['+limit_detail+'] ('+detail+
                          '). Cuff-related blood-volume changes or contact changes can cause this; '
                          'the recording cannot distinguish them. Release cuff and recalibrate.')
                self.last_feature=None; self.last_absent=None
                self.abort(f.reason)
                return f
        if self.state=='BASELINE':
            if not self._baseline_good(f):
                self.baselines=[]
                self.event('Baseline not ready: '+self.baseline_feedback(f)+'. Keep cuff deflated.')
                return f
            self.baselines.append(f)
            self.event('Stable baseline window '+str(len(self.baselines))+'/3')
            if len(self.baselines)>=3:
                pulses=np.array([v.pulse for v in self.baselines])
                rates=np.array([np.mean(v.hz) for v in self.baselines])
                if np.any(np.max(pulses,axis=0)/np.min(pulses,axis=0)>1.8) or np.ptp(rates)>.25:
                    self.baselines=[]; self.event('Baseline varied too much; collecting again.')
                else:
                    self.base={'pulse':np.median(pulses,axis=0),
                               'rms':np.median([v.rms for v in self.baselines],axis=0),
                               'dc':np.median([v.dc for v in self.baselines],axis=0),
                               'hz':float(np.median(rates))}
                    self.state='READY'
                    self.event('Baseline ready. Start inflation ONLY after pressing Start cuff phase.')
            return f
        if not self.base: return f
        ratio=np.array(f.pulse)/self.base['pulse']
        # A low DC difference is intentionally NOT a criterion. Suppression is AC-based.
        is_suppressed = bool(np.all(np.array(f.rms)/self.base['rms'] <= .25)
                             and np.all(ratio<=.15)
                             and not (f.coherent and np.any(ratio>.05)))
        if self.state=='INFLATING':
            if is_suppressed:
                self.suppressed.append(f)
                self.event('Pulse suppression candidate '+str(len(self.suppressed))+'/2')
                if len(self.suppressed)>=2:
                    # Upper observed pulse-like noise prevents a tiny denominator explosion.
                    self.noise=np.max([v.pulse for v in self.suppressed],axis=0)
                    self.state='OCCLUDED'; self.last_absent=(start,end,annotation)
                    self.event('Sustained pulse suppression detected. Consistent with occlusion; begin return phase.')
            else:
                self.suppressed=[]
                self.event(self.suppression_feedback(f), remaining=ratio.tolist())
        elif self.state=='OCCLUDED':
            if not is_suppressed:
                self.state='INFLATING'; self.suppressed=[]; self.noise=None
                self.event('Suppression no longer sustained. Recheck cuff/contact; do not force a pass.')
            else: self.last_absent=(start,end,annotation)
        elif self.state in ('DEFLATING','CANDIDATE'):
            floor=np.maximum(3*self.noise, np.maximum((.03 if self.sensitive else .06)*self.base['pulse'], 2. if self.sensitive else 4.))
            returned=f.coherent and bool(np.all(np.array(f.pulse)>floor))
            if returned:
                if self.pending is not None:
                    prev=self.pending
                    if abs(np.mean(f.hz)-prev['hz'])>.20:
                        self.pending=None; self.state='DEFLATING'
                        self.event('Candidate cleared: pulse timing changed between windows.')
                    else:
                        self.state='RTF'
                        self.result={'label':'EXPERIMENTAL_PULSE_RETURN' if self.sensitive else 'PPG_PULSE_RETURN',
                                     'candidate_window':[prev['start'],prev['end']],
                                     'confirmation_window':[start,end],
                                     'last_suppressed_window':self.last_absent,
                                     'candidate_pressure_annotation':prev['annotation'],
                                     'confirmation_pressure_annotation':annotation,
                                     'threshold_counts':floor.tolist(), 'map_mmHg':None,'sensor_site':self.site,
                                     'channels':list(self.channels)}
                        self.event('PPG RETURN DETECTED: consistent pulses in two separate windows. Fully release cuff.',result=self.result)
                else:
                    self.pending={'start':start,'end':end,'hz':float(np.mean(f.hz)),'annotation':annotation}
                    self.state='CANDIDATE'
                    self.event('Possible pulse return: checking a second independent window.')
            else:
                self.pending=None; self.state='DEFLATING'
                if is_suppressed: self.last_absent=(start,end,annotation)
                else: self.last_absent=None
                self.event('No confirmed return yet.',pulse_floor=floor.tolist())
        elif self.state=='RECOVERY' and self._baseline_good(f):
            self.state='COMPLETE'; self.event('Pulse recovery observed. Trial complete; allow recovery before another trial.')
        return f

    def snapshot(self):
        return {'state':self.state,'sensitive':self.sensitive,'sensor_site':self.site,'result':self.result,
                'selected_channels':list(self.channels),
                'dc_shift_limits':DC_SHIFT_LIMITS.copy(),
                'features':asdict(self.last_feature) if self.last_feature else None,
                'baseline_pulse':self.base['pulse'].tolist() if self.base else None}

def parse_sample(line):
    parts=line.strip().split(',')
    if len(parts)!=8 or parts[0]!='S': raise ValueError('invalid sample record')
    seq,ms,epoch,r,i,lr,li=map(int,parts[1:])
    if not (0<=r<=262143 and 0<=i<=262143 and 1<=lr<=200 and 1<=li<=200
            and 0<=seq<=0xFFFFFFFF and 0<=ms<=0xFFFFFFFF and 0<=epoch<=0xFFFFFFFF):
        raise ValueError('sample values out of range')
    return seq,ms,epoch,r,i,lr,li

class WindowBuffer:
    """Reject sequence gaps, configuration changes, and invalid cadence before DSP."""
    def __init__(self, channels=2):
        self.channels=channels
        self.rows=[]; self.previous=None

    def clear(self): self.rows=[]

    def push(self, row):
        issue=None
        if self.previous:
            p=self.previous
            if row[0] != (p[0]+1)&0xFFFFFFFF: issue='sample sequence gap / device reset'
            elif row[2]!=p[2] or row[3+self.channels:]!=p[3+self.channels:]: issue='optical configuration changed'
            elif (row[1]-p[1])&0xFFFFFFFF > 250: issue='acquisition timing gap'
        self.previous=row
        if issue: self.rows=[]
        self.rows.append(row)
        if len(self.rows)<WINDOW: return None,issue
        rows=self.rows; self.rows=[]
        duration=((rows[-1][1]-rows[0][1])&0xFFFFFFFF)/1000
        if not 3.55<=duration<=4.45: return None,'sample cadence outside tolerance'
        return rows,issue
