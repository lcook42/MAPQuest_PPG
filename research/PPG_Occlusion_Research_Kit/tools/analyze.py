"""Plot recordings and report pressure near operator markers; never estimate MAP."""
import argparse
import csv
import math
from pathlib import Path
from records import FIELDS, OrderCheck, parse

def marker_pressure(rows, time_us, maximum_age_us=100000):
    good = [r for r in rows if r['kind']=='C' and r['flags']==0 and math.isfinite(r['a'])]
    before = [r for r in good if r['t_end_us'] <= time_us]
    after = [r for r in good if r['t_start_us'] >= time_us]
    if not before or not after: return None
    left, right = before[-1], after[0]
    if time_us-left['t_end_us'] > maximum_age_us or right['t_start_us']-time_us > maximum_age_us:
        return None
    # Reject interpolation across even one flagged pressure conversion.
    if any(r['kind']=='C' and r['flags'] and left['t_end_us'] <= r['t_end_us'] <= right['t_start_us'] for r in rows):
        return None
    t0 = (left['t_start_us']+left['t_end_us'])/2
    t1 = (right['t_start_us']+right['t_end_us'])/2
    if t1 == t0: return left['a']
    fraction = (time_us-t0)/(t1-t0)
    if not 0 <= fraction <= 1: return None
    return left['a'] + fraction*(right['a']-left['a'])

def load(path):
    rows = []; order = OrderCheck()
    with open(path, newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            parsed = parse(','.join(str(row[k]) for k in FIELDS))
            order.accept(parsed); rows.append(parsed)
    return rows

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv'); parser.add_argument('--out', default='review.png')
    args = parser.parse_args()
    rows = load(args.csv)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(11, 7))
    for key, color in zip(('a','b','c'), ('red','purple','green')):
        p = [r for r in rows if r['kind']=='P']
        axes[0].plot([r['t_end_us']/1e6 for r in p], [r[key] if r['flags']==0 else math.nan for r in p], color=color, label=key)
    c = [r for r in rows if r['kind']=='C']
    axes[1].plot([r['t_end_us']/1e6 for r in c], [r['a'] if r['flags']==0 else math.nan for r in c], color='black')
    report = ['Operator markers: nearby PNEUMATIC pressure only; NOT MAP.',
              'Host/operator reaction and Doppler delays are not corrected.',
              f'Flagged rows: {sum(bool(r["flags"]) for r in rows)} / {len(rows)}']
    for row in rows:
        if row['kind']=='E':
            t = row['t_end_us']
            value = marker_pressure(rows, t)
            report.append(f'{row["label"]}: t={t/1e6:.3f}s; ' + (f'{value:.2f} mmHg pneumatic' if value is not None else 'no valid bracketing pressure'))
            axes[1].axvline(t/1e6, alpha=.25, color='blue')
    axes[0].set_ylabel('Raw optical counts (a RED, b IR, c GREEN)'); axes[0].legend()
    axes[1].set_ylabel('Pneumatic pressure (mmHg)'); axes[1].set_xlabel('Device acquisition-service time (s)')
    simulated = any('SIMULATED' in r['label'] for r in rows)
    fig.suptitle('SIMULATION — arbitrary signals; NOT physiology' if simulated else 'Bench acquisition review — no MAP estimator')
    fig.tight_layout(); fig.savefig(args.out, dpi=160); plt.close(fig)
    text = '\n'.join(report) + '\n'
    Path(args.out).with_suffix('.txt').write_text(text, encoding='utf-8')
    print(text)

if __name__ == '__main__': main()
