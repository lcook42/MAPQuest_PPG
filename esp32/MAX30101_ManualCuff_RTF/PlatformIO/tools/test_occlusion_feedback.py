import unittest
from test_detector import calibrated, signal

class OcclusionFeedbackTests(unittest.TestCase):
    def test_dc_shift_aborts_once_with_channel_detail(self):
        d=calibrated()
        d.channels=('IR','GREEN')
        d.events.clear()
        d.process(signal(0,dc=(100000,160000)),12,16)
        self.assertEqual(d.state,'ABORTED')
        self.assertIsNone(d.result)
        self.assertIsNone(d.last_feature)
        self.assertRegex(d.events[-1]['message'],r'GREEN \+45\.\d%')
        self.assertEqual(len(d.events),1)
        d.events.clear()
        d.process(signal(0,dc=(100000,160000)),16,20)
        self.assertEqual(d.events,[])
        self.assertEqual(d.state,'ABORTED')

    def test_noise_blocker_does_not_force_occlusion(self):
        d=calibrated(amp=45)
        for i in range(2):
            f=d.process(signal(0,noise=14,seed=200+i),12+4*i,16+4*i)
        self.assertEqual(d.state,'INFLATING')
        self.assertIn('total fluctuation exceeds',d.suppression_feedback(f))
        self.assertNotIn('coherent pulse remains',d.suppression_feedback(f))

    def test_remaining_pulse_identified(self):
        d=calibrated()
        f=d.process(signal(280),12,16)
        self.assertIn('coherent pulse remains',d.suppression_feedback(f))
        self.assertEqual(d.state,'INFLATING')
