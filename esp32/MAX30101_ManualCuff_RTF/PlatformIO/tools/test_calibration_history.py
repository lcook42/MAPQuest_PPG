import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from calibration_history import CalibrationHistory
from channel_calibration import compare
from monitor import Monitor,serve
from test_detector import FakeLink,calibrated,signal
from test_three_channel import triple
from troubleshooting import suggestions


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.m=Monitor(FakeLink(),Path(self.tmp.name)/'session',three=True)
        self.m.connected=True

    def tearDown(self):
        for stream in (self.m.wire,self.m.trace,self.m.raw):stream.close()
        self.tmp.cleanup()

    def add_results(self,leds=(70,80,90),good=True):
        windows=[triple(1),triple(2)] if good else [np.full((400,3),100000.)]*2
        results=compare(windows,leds)
        self.m.calibration_history.add(results,'search_passed' if good else 'no_pair')
        return results

    def test_failed_search_guides_retry_but_cannot_qualify_it(self):
        self.add_results(good=False)
        self.m.act('calibrate')
        self.assertEqual(self.m.leds,[70,80,90])
        self.assertEqual(self.m.detector.state,'IDLE')
        self.assertIsNone(self.m.selection)
        self.assertEqual(self.m.tune_results,[])
        self.assertEqual(self.m.detector.baselines,[])
        with self.assertRaises(ValueError):self.m.act('arm')

    def test_repeated_results_are_aggregated_and_removal_recomputes(self):
        h=CalibrationHistory()
        def result(score,leds):return dict(leds=leds,score=score,qualified=score>=2)
        h.add([result(2.8,[50]*3),result(.7,[60]*3)],'search_passed')
        h.add([result(-1,[50]*3),result(2.5,[60]*3)],'search_passed')
        ranked=h.ranked_settings()
        self.assertEqual(ranked[0]['leds'],[60]*3)
        self.assertAlmostEqual(ranked[0]['score'],1.6)
        h.remove(2)
        self.assertEqual(h.ranked_settings()[0]['leds'],[50]*3)
        self.assertEqual(len(h.attempts),2)

    def test_remove_and_clear_preserve_session_and_current_baseline(self):
        self.add_results();self.add_results((100,110,120))
        detector=self.m.detector;directory=self.m.directory
        self.m.wave=[[1,2,3,4]]
        self.m.act('remove_calibration',1)
        self.assertIs(self.m.detector,detector);self.assertEqual(self.m.directory,directory)
        self.assertEqual(self.m.wave,[[1,2,3,4]])
        self.assertEqual(self.m.calibration_history.ranked_settings()[0]['leds'],[100,110,120])
        self.m.act('clear_calibrations')
        self.assertEqual(self.m.calibration_history.ranked_settings(),[])
        saved=json.loads((directory/'calibration_history.json').read_text())
        self.assertEqual(len(saved['attempts']),2)
        self.assertFalse(any(a['included'] for a in saved['attempts']))

    def test_disabled_history_starts_from_default(self):
        self.add_results();self.m.act('use_calibration_history',False)
        self.m.act('calibrate');self.assertEqual(self.m.leds,[32]*3)

    def test_cannot_edit_history_during_search_or_cuff(self):
        self.add_results();self.m.act('calibrate')
        for name,value in [('remove_calibration',1),('clear_calibrations',None),('use_calibration_history',False)]:
            with self.assertRaises(ValueError):self.m.act(name,value)
        self.m.tuning=False;self.m.arm_at=1
        with self.assertRaises(ValueError):self.m.act('remove_calibration',1)

    def test_interrupted_attempt_preserves_completed_settings(self):
        self.m.act('calibrate');self.m.tune_results=self.add_results()
        self.m.issue('transport failed',fatal=True)
        attempts=self.m.calibration_history.attempts
        self.assertEqual(attempts[-1]['status'],'interrupted')
        self.assertEqual(len(attempts[-1]['results']),3)
        self.m.finish_calibration_attempt('interrupted')
        self.assertEqual(len(attempts),2)

    def test_fresh_failure_overrides_qualified_history_and_all_pairs_run(self):
        self.add_results()
        self.m.act('calibrate');self.m.tune_phase='quality'
        self.m.tune_candidates=[[70,80,90],[60,80,90]]
        self.m.tune_wait=False;self.m.tune_skip=0
        for setting in range(2):
            self.m.tune_wait=False
            for i in range(800):
                self.m.tune((800*setting+i,i*10,setting,100000,100000,100000,*self.m.leds))
        self.assertEqual(len(self.m.tune_results),6)
        self.assertEqual(self.m.detector.state,'ABORTED')
        self.assertFalse(self.m.selection['qualified'])
        self.assertEqual(self.m.calibration_history.attempts[-1]['status'],'no_pair')

    def test_success_does_not_skip_remaining_settings_or_fresh_baseline(self):
        self.m.act('calibrate');self.m.tune_phase='quality'
        self.m.tune_candidates=[[32]*3,[24,32,32]]
        for setting in range(2):
            self.m.tune_wait=False;self.m.tune_skip=0
            values=np.concatenate([triple(1),triple(2)])
            for i,row in enumerate(values):
                self.m.tune((setting*800+i,i*10,setting,*map(int,row),*self.m.leds))
            if setting==0:self.assertEqual(self.m.tune_phase,'quality')
        self.assertEqual(len(self.m.tune_results),6)
        self.assertEqual(self.m.tune_phase,'freeze')
        self.m.tune_wait=False;self.m.tune_skip=0
        self.m.tune((1600,16000,3,100000,100000,100000,*self.m.leds))
        self.assertEqual(self.m.detector.state,'BASELINE')
        self.assertEqual(self.m.detector.baselines,[])

    def test_history_adds_settings_after_fresh_brightness_adjustment(self):
        self.add_results();self.m.act('calibrate')
        self.m.leds=[40]*3;self.m.tune_wait=False;self.m.tune_skip=0
        for i in range(100):self.m.tune((i,i*10,1,100000,100000,100000,40,40,40))
        self.assertIn([70,80,90],self.m.tune_candidates)
        self.assertIn([40]*3,self.m.tune_candidates)
        self.assertLessEqual(len(self.m.tune_candidates),10)

    def test_failure_help_uses_failed_pair_evidence(self):
        results=compare([np.full((400,3),100000.)]*2,[32]*3)
        tips=suggestions('No pair passed both windows',results)
        self.assertTrue(any('flat' in tip for tip in tips))
        self.assertTrue(any('remove' in tip for tip in tips))
        self.assertEqual(suggestions(''),[])
        self.assertTrue(any('USB' in tip for tip in suggestions('Invalid bytes in recorder stream')))
        self.assertTrue(any('Do not increase pressure' in tip for tip in suggestions('90-second cuff-phase limit reached')))

    def test_history_commands_reach_monitor_over_http(self):
        import threading
        from urllib.request import Request,urlopen
        server=serve(self.m,0,'test-token')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for name,value in [('remove_calibration',1),('clear_calibrations',None),('use_calibration_history',False)]:
                request=Request(f'http://127.0.0.1:{server.server_address[1]}/command',
                                data=json.dumps(dict(name=name,value=value)).encode(),
                                headers={'X-RTF-Token':'test-token','Content-Type':'application/json'})
                with urlopen(request,timeout=2) as response:self.assertEqual(response.status,200)
                self.assertEqual(self.m.commands.get_nowait(),dict(name=name,value=value))
        finally:server.shutdown();server.server_close();thread.join(2)


class GreenLimitTests(unittest.TestCase):
    def test_green_39_percent_accepted_and_41_rejected_both_directions(self):
        for green_index in (0,1):
            for shift in (.39,-.39,.41,-.41):
                d=calibrated();d.channels=('GREEN','IR') if green_index==0 else ('IR','GREEN')
                dc=[100000,110000];dc[green_index]*=1+shift
                d.process(signal(0,dc=dc),12,16)
                self.assertEqual(d.state=='ABORTED',abs(shift)>.4)

    def test_ir_still_rejects_26_percent_with_green_in_pair(self):
        d=calibrated();d.channels=('IR','GREEN')
        d.process(signal(0,dc=(126000,110000)),12,16)
        self.assertEqual(d.state,'ABORTED')

    def test_green_within_window_drift_guard_unchanged(self):
        d=calibrated();d.channels=('IR','GREEN')
        a=signal(0);a[:,1]+=np.linspace(0,10000,400)
        f=d.process(a,12,16)
        self.assertFalse(f.valid);self.assertIn('drift',f.reason)
        self.assertNotEqual(d.state,'OCCLUDED')

if __name__=='__main__':unittest.main()
