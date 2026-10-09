import math
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from records import parse, OrderCheck, synthetic
from analyze import marker_pressure
from calibrate import fit

class AcquisitionTests(unittest.TestCase):
    def test_conversion_fault_preserved(self):
        row=parse('C,1,10,20,nan,nan,1013.2,8,PNEUMATIC_NOT_MAP')
        self.assertTrue(math.isnan(row['a']))
        with self.assertRaises(ValueError): parse('C,1,10,20,60,1000,1013,8,BAD')

    def test_reject_bad_raw_and_time(self):
        for text in ('P,0,0,0,nan,2,3,0,BAD','P,0,10,0,1,2,3,0,BAD'):
            with self.assertRaises(ValueError): parse(text)

    def test_restart_detected(self):
        check=OrderCheck(); check.accept(parse('E,10,20,20,nan,nan,nan,0,START'))
        with self.assertRaises(ValueError): check.accept(parse('E,0,0,0,nan,nan,nan,0,START'))

    def test_simulated_stream_order_and_marker(self):
        check=OrderCheck()
        rows=[parse(line) for line in synthetic(2)]
        for row in rows: check.accept(row)
        marker=next(r for r in rows if r['label']=='SIMULATED_MARK')
        self.assertIsNotNone(marker_pressure(rows,marker['t_end_us']))
        self.assertIsNone(marker_pressure(rows,3000000))

    def test_marker_does_not_bridge_fault(self):
        rows=[parse('C,0,0,0,0,1013,1013,0,OK'),parse('C,1,40000,40000,nan,nan,1013,8,FAULT'),parse('C,2,80000,80000,80,1100,1013,0,OK')]
        self.assertIsNone(marker_pressure(rows,50000))

    def test_gain_and_invalid_span(self):
        points=[{'measured_mmHg':x,'reference_mmHg':x*1.02} for x in (20,80,140)]
        self.assertAlmostEqual(fit(points),1.02)
        with self.assertRaises(ValueError): fit([{'measured_mmHg':0,'reference_mmHg':0}]*3)

if __name__=='__main__': unittest.main()
