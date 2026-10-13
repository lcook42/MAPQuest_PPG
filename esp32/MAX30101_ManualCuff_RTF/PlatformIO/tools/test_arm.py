"""Engineering signal challenges for arm use; no anatomical validation data."""
import unittest
from rtf_detector import Detector
from test_detector import signal

class ArmChallenges(unittest.TestCase):
    def baseline(self,sensitive=True):
        d=Detector(sensitive,site='forearm');d.calibrate()
        for k in range(3):d.process(signal(8,noise=.4,seed=k),k*4,k*4+4)
        return d

    def test_weak_clean_pulse_accepted_only_experimental(self):
        self.assertEqual(self.baseline().state,'READY')
        self.assertEqual(self.baseline(False).state,'BASELINE')

    def test_weak_return_complete_and_site_retained(self):
        d=self.baseline();d.arm(12)
        for k in range(2):d.process(signal(0,noise=.4,seed=40+k),12+k*4,16+k*4)
        self.assertEqual(d.state,'OCCLUDED');d.deflate()
        for k in range(2):d.process(signal(4,noise=.4,seed=60+k),20+k*4,24+k*4)
        self.assertEqual(d.state,'RTF');self.assertEqual(d.result['sensor_site'],'forearm')

    def test_experimental_noise_does_not_calibrate(self):
        d=Detector(True,site='forearm');d.calibrate()
        for k in range(80):d.process(signal(0,noise=1+k%12,seed=k),k*4,k*4+4)
        self.assertEqual(d.state,'BASELINE')

    def test_experimental_noise_does_not_return(self):
        d=self.baseline();d.arm(12)
        for k in range(2):d.process(signal(0,noise=.4,seed=40+k),12+k*4,16+k*4)
        self.assertEqual(d.state,'OCCLUDED');d.deflate()
        for k in range(80):
            d.process(signal(0,noise=.4+k%5,seed=100+k),20+k*4,24+k*4)
            self.assertNotEqual(d.state,'RTF')

    def test_recalibration_retains_site(self):
        d=self.baseline();d.calibrate()
        self.assertEqual(d.site,'forearm')

if __name__=='__main__':unittest.main(verbosity=2)
