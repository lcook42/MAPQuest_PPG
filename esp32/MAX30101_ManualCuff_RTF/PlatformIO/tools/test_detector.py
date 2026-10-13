"""Synthetic challenge tests. These do NOT measure physiological accuracy."""
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from rtf_detector import Detector, WindowBuffer, extract, parse_sample, VERSION
from monitor import Monitor

def signal(amplitude=300, noise=3, hz=1.2, seed=0, dc=(100000,110000), phase=0):
    rng=np.random.default_rng(seed)
    t=np.arange(400)/100
    # Asymmetric pulse with harmonic content, independent channel noise.
    wave=np.sin(2*np.pi*hz*t+phase)+.32*np.sin(4*np.pi*hz*t+phase-.5)
    return np.column_stack((dc[0]+amplitude*wave,dc[1]+.8*amplitude*wave)) + rng.normal(0,noise,(400,2))

def calibrated(sensitive=False,amp=300,hz=1.2):
    d=Detector(sensitive); d.calibrate()
    for i in range(3): d.process(signal(amp,hz=hz,seed=i,phase=.7*i),i*4,(i+1)*4)
    assert d.state=='READY', (d.state,d.events)
    d.arm(12)
    return d

def occluded(d,noise=3):
    for i in range(2): d.process(signal(0,noise,seed=40+i),12+i*4,16+i*4)
    assert d.state=='OCCLUDED', (d.state,d.events)
    d.deflate()

class DetectorTests(unittest.TestCase):
    def test_return_without_dc_contrast(self):
        d=calibrated(); occluded(d)
        d.process(signal(35,seed=70),20,24)
        self.assertEqual(d.state,'CANDIDATE')
        d.process(signal(40,seed=71,phase=.7),24,28)
        self.assertEqual(d.state,'RTF'); self.assertIsNone(d.result['map_mmHg'])

    def test_weak_signal_noise_aware_return(self):
        d=calibrated(amp=45); occluded(d,noise=1)
        for i in range(2): d.process(signal(10,noise=1,seed=60+i),20+i*4,24+i*4)
        self.assertEqual(d.state,'RTF')

    def test_healthy_pulse_never_occluded(self):
        d=calibrated()
        for i in range(12): d.process(signal(280,seed=i,dc=(101000,109000)),12+i*4,16+i*4)
        self.assertEqual(d.state,'INFLATING')

    def test_partial_suppression_is_not_occlusion(self):
        d=calibrated()
        for i in range(4): d.process(signal(35,seed=i),12+i*4,16+i*4)
        self.assertEqual(d.state,'INFLATING')

    def test_nonpulsatile_baseline_rejected(self):
        d=Detector(); d.calibrate()
        for i in range(10): d.process(signal(0,seed=i),i*4,i*4+4)
        self.assertEqual(d.state,'BASELINE')

    def test_one_window_not_confirmed(self):
        d=calibrated(); occluded(d)
        d.process(signal(45),20,24); self.assertEqual(d.state,'CANDIDATE')
        d.process(signal(0),24,28); self.assertEqual(d.state,'DEFLATING')
        self.assertIsNone(d.result)

    def test_motion_breaks_confirmation(self):
        d=calibrated(); occluded(d); d.process(signal(50),20,24)
        moved=signal(60); moved[160:]+=10000
        d.process(moved,24,28)
        self.assertEqual(d.state,'DEFLATING'); self.assertIsNone(d.pending)

    def test_contact_loss_never_occlusion(self):
        d=calibrated()
        for i in range(3): d.process(signal(0,dc=(100,100)),12+i*4,16+i*4)
        self.assertEqual(d.state,'INFLATING')

    def test_flatline_never_occlusion(self):
        d=calibrated()
        for i in range(3): d.process(np.full((400,2),100000.),12+i*4,16+i*4)
        self.assertEqual(d.state,'INFLATING')

    def test_noise_never_return_monte_carlo(self):
        d=calibrated(); occluded(d)
        for i in range(80):
            d.process(signal(0,noise=5+(i%10)*3,seed=100+i),20+i*4,24+i*4)
            self.assertNotEqual(d.state,'RTF')

    def test_one_channel_pulse_rejected(self):
        d=calibrated(); occluded(d)
        for i in range(5):
            a=signal(50,seed=i); a[:,1]=signal(0,seed=100+i)[:,1]
            d.process(a,20+i*4,24+i*4)
        self.assertNotEqual(d.state,'RTF')

    def test_different_channel_frequencies_rejected(self):
        d=calibrated(); occluded(d)
        for i in range(3):
            a=signal(80,seed=i); a[:,1]=signal(80,hz=1.6,seed=i)[:,1]
            d.process(a,20+i*4,24+i*4)
        self.assertNotEqual(d.state,'RTF')

    def test_impulse_and_ramp_not_return(self):
        for kind in ('impulse','ramp','burst'):
            d=calibrated(); occluded(d)
            for i in range(3):
                a=signal(0,seed=i)
                if kind=='impulse': a[180:183]+=150
                elif kind=='ramp': a+=np.linspace(0,180,400)[:,None]
                else: a[100:140]+=signal(150)[100:140]-np.array([100000,110000])
                d.process(a,20+i*4,24+i*4)
            self.assertNotEqual(d.state,'RTF',kind)

    def test_rate_variation(self):
        for hz in (.7,1,1.6,2.5):
            d=calibrated(hz=hz); occluded(d)
            for i in range(2): d.process(signal(55,hz=hz*1.08,seed=i),20+i*4,24+i*4)
            self.assertEqual(d.state,'RTF',hz)

    def test_cannot_skip_occlusion(self):
        d=calibrated()
        with self.assertRaises(ValueError): d.deflate()

    def test_invalid_window_clears_baseline_progress(self):
        d=Detector(); d.calibrate(); d.process(signal(),0,4)
        d.process(np.zeros((400,2)),4,8)
        self.assertEqual(len(d.baselines),0)

    def test_stale_baseline_blocks_arm(self):
        d=Detector(); d.calibrate()
        for i in range(3): d.process(signal(seed=i),i*4,4+i*4)
        with self.assertRaises(ValueError): d.arm(30)

class TransportTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_sample('S,1,10,2,10000,20000,32,32')[3],10000)
        for s in ('S,1,2,3,nan,5,32,32','S,1,2,3,300000,5,32,32','S,1,2','S,1,2,3,1000,1000,0,0'):
            with self.assertRaises(ValueError): parse_sample(s)

    def test_sequence_gap(self):
        b=WindowBuffer(); b.push((1,10,0,10000,10000,32,32))
        _,issue=b.push((3,30,0,10000,10000,32,32)); self.assertIn('gap',issue)

    def test_epoch_change(self):
        b=WindowBuffer(); b.push((1,10,0,10000,10000,32,32))
        _,issue=b.push((2,20,1,10000,10000,32,32)); self.assertIn('configuration',issue)

    def test_cadence_and_batching(self):
        for step,valid in ((10,True),(5,False),(20,False)):
            b=WindowBuffer(); window=None
            for i in range(400): window,issue=b.push((i,(i//3)*3*step,0,10000,10000,32,32))
            self.assertEqual(window is not None,valid)

    def test_no_overlap(self):
        b=WindowBuffer(); windows=[]
        for i in range(800):
            w,issue=b.push((i,i*10,0,10000,10000,32,32))
            if w: windows.append(w)
        self.assertEqual(len(windows),2); self.assertLess(windows[0][-1][0],windows[1][0][0])

class FakeLink:
    def __init__(self): self.sent=[]
    def send(self,s): self.sent.append(s)
    def close(self): pass

class IntegrationTests(unittest.TestCase):
    def test_handshake_and_fault_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=Monitor(FakeLink(),Path(tmp)/'session')
            try:
                m.receive(f'HELLO,{VERSION},100,RED_IR,60,90,ESP32_DEVKIT_V1'); m.receive('ACK,START')
                self.assertTrue(m.connected)
                m.receive('S,1,10,0,100000,100000,32,32')
                m.receive('S,3,30,0,100000,100000,32,32')
                self.assertTrue(m.failed); self.assertEqual(m.detector.state,'ABORTED')
                self.assertIn('STOP',m.link.sent)
            finally: m.wire.close();m.raw.close();m.trace.close()

    def test_no_map_dependency_and_annotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=Monitor(FakeLink(),Path(tmp)/'session')
            try:
                m.connected=True
                m.act('pressure',120)
                self.assertEqual(m.annotation,120)
                with self.assertRaises(ValueError): m.act('pressure',float('nan'))
            finally: m.wire.close();m.raw.close();m.trace.close()

if __name__=='__main__': unittest.main(verbosity=2)
