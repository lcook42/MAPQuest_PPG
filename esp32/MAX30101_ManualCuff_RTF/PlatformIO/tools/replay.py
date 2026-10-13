"""Replay saved analysis windows through the exact detector; verify saved states."""
import argparse
import json
from pathlib import Path
from rtf_detector import Detector,VERSION,DETECTOR_REVISION

def replay(path):
    d=Detector(); windows=0
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        row=json.loads(line); kind=row['kind']
        if kind=='META':
            if row['version']!=VERSION: raise ValueError('Detector version mismatch')
            if row.get('detector_revision',DETECTOR_REVISION)!=DETECTOR_REVISION:
                raise ValueError('Host detector revision mismatch; use this session\'s source_snapshot/replay.py')
            d=Detector(row['sensitive'],row.get('sensor_site','unspecified'))
        elif kind=='ACTION':
            action=row['name']
            if action=='calibrate':
                d.channels=tuple(row.get('channels',('RED','IR')))
                d.calibrate()
            elif action=='arm': d.arm(row['now'])
            elif action=='deflate': d.deflate()
            elif action=='released': d.released()
            elif action=='stop': d.abort('Operator stopped the test.')
            elif action=='setup': d=Detector(d.sensitive,d.site)
            elif action=='calibration_rejected': d.abort(row['reason'])
        elif kind=='ISSUE':
            if row['fatal']: d.abort(row['reason'])
            else: d.invalidate(row['reason'])
        elif kind=='WINDOW':
            d.process(row['samples'],row['start'],row['end'],row['annotation']);windows+=1
            if d.state!=row['state_after']:
                raise ValueError(f'Replay disagreement at window {windows}: {d.state} vs {row["state_after"]}')
        d.events.clear()
    return {'windows':windows,'final_state':d.state,'result':d.result}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('log',type=Path)
    print(json.dumps(replay(p.parse_args().log),indent=2))
