"""Acquisition schema and bench-only synthetic data; no MAP inference."""
import csv
import math

FIELDS = ['kind', 'seq', 't_start_us', 't_end_us', 'a', 'b', 'c', 'flags', 'label']

def parse(line):
    if line.startswith('#') or not line.strip():
        return None
    row = next(csv.reader([line]))
    if len(row) != 9 or row[0] not in ('P', 'C', 'E'):
        raise ValueError('Unrecognized acquisition row')
    result = dict(zip(FIELDS, row))
    for key in ('seq', 't_start_us', 't_end_us', 'flags'):
        result[key] = int(result[key])
        if result[key] < 0:
            raise ValueError('Negative counter/time/flags')
    if result['t_end_us'] < result['t_start_us']:
        raise ValueError('Reversed acquisition interval')
    for key in ('a', 'b', 'c'):
        result[key] = float(result[key])
    if result['kind'] == 'P' and any(not math.isfinite(result[k]) or
            not 0 <= result[k] <= 262143 for k in ('a', 'b', 'c')):
        raise ValueError('Invalid raw optical value')
    if result['kind'] == 'C':
        if result['flags'] & 8:
            if math.isfinite(result['a']):
                raise ValueError('Conversion-fault row contains finite pressure')
        elif any(not math.isfinite(result[k]) for k in ('a', 'b', 'c')):
            raise ValueError('Nonfinite pressure without conversion fault')
    return result

class OrderCheck:
    def __init__(self):
        self.seq = -1
        self.time = -1

    def accept(self, row):
        if row['seq'] <= self.seq or row['t_end_us'] < self.time:
            raise ValueError('Device restart or out-of-order stream: create a new session file')
        self.seq, self.time = row['seq'], row['t_end_us']

def synthetic(seconds=20):
    """Deterministic signals are illustrative, not an LVAD/vascular model."""
    seq = 0
    yield f'E,{seq},0,0,nan,nan,nan,0,SIMULATED_START'; seq += 1
    for i in range(int(seconds * 100)):
        t = i / 100
        pressure = max(0.0, 140 * math.sin(math.pi * t / seconds))
        optical = 80000 + 5000 * math.sin(t * 0.3) - pressure * 45
        us = int(t * 1e6)
        yield f'P,{seq},{us},{us},{optical:.3f},{optical*1.1:.3f},{optical*0.7:.3f},0,SIMULATED'; seq += 1
        if i % 4 == 0:
            yield f'C,{seq},{us},{us},{pressure:.6f},{1013.25+pressure/0.750061683:.6f},1013.25,0,SIMULATED'; seq += 1
        if i == int(seconds * 70):
            yield f'E,{seq},{us},{us},nan,nan,nan,0,SIMULATED_MARK'; seq += 1
    us = int(seconds * 1e6)
    yield f'E,{seq},{us},{us},nan,nan,nan,0,SIMULATED_STOP'
