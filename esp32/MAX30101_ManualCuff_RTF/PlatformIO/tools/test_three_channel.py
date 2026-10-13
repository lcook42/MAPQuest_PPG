import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from channel_calibration import compare,parse_three,noise_profile
from monitor import Monitor,choose_connection
from rtf_detector import WindowBuffer,VERSION
from test_detector import signal,FakeLink
from replay import replay

def triple(seed=1):
    a=signal(seed=seed)
    return np.column_stack((100000+np.random.default_rng(seed).normal(0,5,400),a[:,0],a[:,1]))

class PairTests(unittest.TestCase):
    def test_noise_content_separates_fast_and_slow_variation(self):
        t=np.arange(400)/100
        a=np.column_stack((100000+100*np.cos(2*np.pi*.25*t),100000+100*np.cos(2*np.pi*20*t)))
        f=noise_profile(a)
        self.assertGreater(f['slow_percent'][0],99)
        self.assertGreater(f['high_frequency_percent'][1],99)
    def test_selects_ir_green_when_red_has_no_pulse(self):
        results=compare([triple(1),triple(2)],[32]*3)
        best=max(results,key=lambda r:r['score'])
        self.assertEqual(best['channels'],['IR','GREEN']);self.assertTrue(best['qualified'])
        self.assertFalse(any(r['qualified'] for r in results if 0 in r['pair']))

    def test_one_good_window_cannot_qualify(self):
        a=triple(2);a[:,2]=100000+np.random.default_rng(9).normal(0,5,400)
        self.assertFalse(any(r['qualified'] for r in compare([triple(),a],[32]*3)))

    def test_varying_amplitude_cannot_qualify(self):
        a=triple();b=triple(2);b[:,1:]=np.array([100000,110000])+(b[:,1:]-[100000,110000])*.25
        self.assertFalse(any(r['qualified'] for r in compare([a,b],[32]*3)))

    def test_clipped_third_channel_does_not_veto_good_pair(self):
        a=triple();a[:,0]=262143
        b=triple(2);b[:,0]=262143
        best=max(compare([a,b],[32]*3),key=lambda r:r['score'])
        self.assertTrue(best['qualified']);self.assertEqual(best['pair'],[1,2])

    def test_noise_cannot_qualify_any_pair(self):
        for seed in range(12):
            rng=np.random.default_rng(seed)
            windows=[100000+rng.normal(0,5,(400,3)) for _ in range(2)]
            self.assertFalse(any(r['qualified'] for r in compare(windows,[32]*3)))

    def test_three_channel_buffer_ignores_changing_green_adc_but_checks_green_led(self):
        b=WindowBuffer(3)
        b.push((0,0,0,100000,100000,100000,32,32,32))
        _,issue=b.push((1,10,0,100000,100000,100050,32,32,32));self.assertIsNone(issue)
        _,issue=b.push((2,20,0,100000,100000,100100,32,32,33));self.assertIn('configuration',issue)

class MonitorThreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.m=Monitor(FakeLink(),Path(self.tmp.name)/'session',three=True)
        self.m.receive(f'HELLO,{VERSION},100,RED_IR,60,90,ESP32_DEVKIT_V1')
        self.assertEqual(self.m.link.sent[-1],'RTF3')
        self.m.receive('ACK,RTF3,100,RED_IR_GREEN')
    def tearDown(self):
        for f in (self.m.wire,self.m.trace,self.m.raw):f.close()
        self.tmp.cleanup()
    def test_current_ack_must_match(self):
        self.m.act('calibrate')
        with self.assertRaises(ConnectionError):self.m.receive('ACK,LED3,32,32,33,1')
    def test_invalid_bytes_after_handshake_abort_instead_of_being_filtered(self):
        with self.assertRaises(ConnectionError):self.m.receive('D,1,10,2,100000,100000,10000,32,32,\ufffd')
    def test_boot_chatter_before_handshake_cannot_create_samples(self):
        self.m.hello=False;self.m.connected=False
        self.m.receive('\ufffd startup')
        self.assertFalse(self.m.hello);self.assertEqual(self.m.wave,[])
    def test_plot_does_not_join_led_settings_or_settling_samples(self):
        self.m.receive('D,0,0,1,100000,100000,100000,32,32,32')
        self.assertEqual(len(self.m.wave),1)
        self.m.act('calibrate');self.assertEqual(self.m.wave,[])
        self.m.receive('D,1,10,1,100000,100000,100000,32,32,32')
        self.assertEqual(self.m.wave,[])
        self.m.receive('ACK,LED3,32,32,32,2')
        self.m.receive('D,2,20,2,110000,120000,100000,32,32,32')
        self.assertEqual(self.m.wave,[])
        self.m.tune_skip=0
        self.m.receive('D,3,30,2,110000,120000,100000,32,32,32')
        self.assertEqual(len(self.m.wave),1)
        self.assertEqual(len((self.m.directory/'samples.csv').read_text().splitlines()),5)
    def test_samples_must_match_ack(self):
        self.m.act('calibrate');self.m.receive('ACK,LED3,32,32,32,1')
        with self.assertRaises(ConnectionError):self.m.receive('D,0,0,1,100000,100000,100000,32,32,33')
    def test_config_change_even_on_unselected_channel_fails(self):
        self.m.selected_pair=(1,2);self.m.expected_epoch=1;self.m.leds=[32]*3
        with self.assertRaises(ConnectionError):self.m.receive('D,0,0,1,100000,100000,100000,33,32,32')
    def test_calibration_blocked_while_armed(self):
        self.m.arm_at=time.monotonic()
        with self.assertRaises(ValueError):self.m.act('calibrate')
        self.assertNotIn('LED3,32,32,32',self.m.link.sent)
    def test_failed_search_remains_unarmed_and_replays(self):
        self.m.act('calibrate');self.m.receive('ACK,LED3,32,32,32,1');self.m.tune_skip=0
        seq=0
        for _ in range(100):
            self.m.tune((seq,seq*10,1,100000,110000,105000,*self.m.leds));seq+=1
        rng=np.random.default_rng(18)
        for epoch in range(2,9):
            self.m.receive('ACK,LED3,'+','.join(map(str,[*self.m.leds,epoch])))
            self.m.tune_skip=0
            for values in 100000+rng.normal(0,5,(800,3)):
                self.m.tune((seq,seq*10,epoch,*map(int,values),*self.m.leds));seq+=1
        self.assertEqual(self.m.detector.state,'ABORTED');self.assertFalse(self.m.tuning)
        self.assertFalse(self.m.selection['qualified']);self.assertNotIn('ARM',self.m.link.sent)
        for values in 100000+rng.normal(0,5,(400,3)):
            self.m.receive('D,'+','.join(map(str,[seq,seq*10,8,*map(int,values),*self.m.leds])));seq+=1
        self.assertEqual(replay(self.m.directory/'replay.jsonl')['final_state'],'ABORTED')
    def test_dim_green_can_reach_quality_search_when_red_is_clipped(self):
        self.m.act('calibrate');self.m.receive('ACK,LED3,32,32,32,1');self.m.tune_skip=0
        self.m.tune_round=7
        for seq in range(100):self.m.tune((seq,seq*10,1,262143,110000,8000,*self.m.leds))
        self.assertEqual(self.m.tune_phase,'quality')
        self.assertTrue(self.m.tuning)
    def test_usb_first_and_wifi_fallback(self):
        with patch('serial.tools.list_ports.comports',return_value=[SimpleNamespace(vid=0x10c4,pid=0xea60,device='COM4')]),patch('monitor.Connection') as c:
            _,label=choose_connection('host');c.assert_called_once_with(port='COM4');self.assertEqual(label,'USB COM4')
        with patch('serial.tools.list_ports.comports',return_value=[]),patch('monitor.Connection') as c:
            choose_connection('host');c.assert_called_once_with(host='host')

if __name__=='__main__':unittest.main()
