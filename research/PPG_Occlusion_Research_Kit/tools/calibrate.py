"""Fit/verify pressure gain against bench references; does not calibrate MAP."""
import argparse
import json
import math

def fit(points):
    if len(points)<3: raise ValueError('At least three calibration points required')
    for p in points:
        if not all(math.isfinite(p[k]) for k in ('measured_mmHg','reference_mmHg')):
            raise ValueError('Nonfinite calibration point')
        if not 0 <= p['measured_mmHg'] <= 300 or not 0 <= p['reference_mmHg'] <= 300:
            raise ValueError('Bench calibration range is 0..300 mmHg')
    denominator = sum(p['measured_mmHg']**2 for p in points)
    if denominator <= 0: raise ValueError('Calibration span is zero')
    return sum(p['measured_mmHg']*p['reference_mmHg'] for p in points)/denominator

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('json'); parser.add_argument('--out', default='pressure_calibration_result.json')
    args = parser.parse_args()
    data = json.loads(open(args.json, encoding='utf-8').read())
    gain = fit(data['fit_points'])
    if not .5 <= gain <= 1.5: raise ValueError('Gain outside firmware range: check hardware/profile')
    checks = data.get('independent_check_points', [])
    residuals = [gain*p['measured_mmHg']-p['reference_mmHg'] for p in checks]
    if checks: fit(checks)  # Validate inputs; do not use their fit.
    result = {'gain':gain, 'command':f'GAIN {gain:.8f}',
              'independent_residuals_mmHg':residuals,
              'max_absolute_independent_residual_mmHg':max(map(abs,residuals)) if residuals else None,
              'note':'Instrument gain only. Does not establish total uncertainty or accurate MAP.'}
    with open(args.out,'x',encoding='utf-8') as stream: json.dump(result,stream,indent=2)
    print(json.dumps(result,indent=2))

if __name__ == '__main__': main()
