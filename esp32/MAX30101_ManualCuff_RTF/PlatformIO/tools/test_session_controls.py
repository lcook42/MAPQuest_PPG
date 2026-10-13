import itertools,json,tempfile,time,unittest
from unittest.mock import patch
from pathlib import Path
from monitor import Monitor,SessionManager
from demo import SyntheticConnection
from test_detector import FakeLink

class RecoveryControls(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.m=Monitor(FakeLink(),Path(self.tmp.name)/'session',three=True)
        self.m.connected=True
    def tearDown(self):
        for f in (self.m.wire,self.m.trace,self.m.raw):f.close()
        self.tmp.cleanup()
    def test_skip_allows_recalibration_but_not_arming(self):
        self.m.act('released')
        with self.assertRaises(ValueError):self.m.act('calibrate')
        self.m.act('skip_recovery');self.m.publish()
        self.assertEqual(self.m.view['rest_remaining'],0)
        with self.assertRaises(ValueError):self.m.act('arm')
        self.m.act('calibrate');self.assertTrue(self.m.tuning)
        rows=[json.loads(s) for s in (self.m.directory/'replay.jsonl').read_text().splitlines()]
        self.assertTrue(any(r.get('name')=='skip_recovery' for r in rows))
    def test_cannot_skip_before_release(self):
        with self.assertRaises(ValueError):self.m.act('skip_recovery')
        self.m.release_at=time.monotonic();self.m.arm_at=time.monotonic()
        with self.assertRaises(ValueError):self.m.act('skip_recovery')
    def test_new_release_restores_wait(self):
        self.m.act('released');self.m.act('skip_recovery');self.m.act('released');self.m.publish()
        self.assertFalse(self.m.recovery_skipped);self.assertGreater(self.m.view['rest_remaining'],0)

class ResetControls(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.seq=itertools.count()
        self.sm=SessionManager(lambda:(SyntheticConnection(accelerated=True),'Synthetic'),
            lambda:Path(self.tmp.name)/str(next(self.seq)),three=True,synthetic=True)
    def tearDown(self):self.sm.close();self.tmp.cleanup()
    def test_reset_clears_session_and_preserves_previous_files(self):
        old=self.sm.monitor;old.annotation=123;old.selection={'qualified':False}
        self.sm.dispatch('reset');new=self.sm.monitor
        self.assertIsNot(old,new);self.assertTrue(old.trace.closed)
        self.assertIsNone(new.annotation);self.assertIsNone(new.selection);self.assertIsNone(new.detector.base)
        self.assertNotEqual(old.directory,new.directory);self.assertTrue((old.directory/'samples.csv').exists())
        self.assertTrue(new.reset_connection)
        saved=(old.directory/'samples.csv').read_bytes()
        self.sm.dispatch('reset');self.assertEqual((old.directory/'samples.csv').read_bytes(),saved)
    def test_reset_blocked_during_cuff_phase(self):
        self.sm.monitor.arm_at=time.monotonic()
        with self.assertRaises(ValueError):self.sm.dispatch('reset')
        self.assertTrue(self.sm.worker.is_alive());self.sm.monitor.arm_at=None

    def test_wireless_switch_blocked_during_cuff_phase_or_tuning(self):
        self.sm.monitor.arm_at=time.monotonic()
        for command in ('wifi_setup','connect_wifi'):
            with self.assertRaises(ValueError):self.sm.dispatch(command,'192.168.1.122')
        self.sm.monitor.arm_at=None;self.sm.monitor.tuning=True
        with self.assertRaises(ValueError):self.sm.dispatch('wifi_setup')
        self.sm.monitor.tuning=False

    def test_wireless_handoff_reuses_session_manager(self):
        old=self.sm.monitor
        with patch('monitor.Connection',return_value=SyntheticConnection(accelerated=True)):
            self.sm.dispatch('connect_wifi','192.168.1.122')
        self.assertIsNot(self.sm.monitor,old)
        self.assertEqual(self.sm.monitor.transport,'Wi-Fi 192.168.1.122')
        self.assertTrue(old.trace.closed)
        self.assertIsNone(self.sm.monitor.detector.base)

    def test_invalid_wireless_address_keeps_connection(self):
        old=self.sm.monitor
        with self.assertRaises(ValueError):self.sm.dispatch('connect_wifi','not-an-address')
        self.assertIs(self.sm.monitor,old)
    def test_reconnect_after_failed_worker_and_offline_release(self):
        self.sm.close();old=self.sm.monitor;old.failed=True;old.arm_at=time.monotonic()
        with self.assertRaises(ValueError):self.sm.dispatch('reset')
        self.sm.dispatch('released');self.sm.dispatch('skip_recovery');self.sm.dispatch('reset')
        self.assertIsNot(self.sm.monitor,old);self.assertTrue(self.sm.worker.is_alive())
    def test_failed_reconnect_keeps_old_logs_and_can_retry(self):
        old=self.sm.monitor;factory=self.sm.connection_factory
        def unavailable():raise OSError('test disconnected')
        self.sm.connection_factory=unavailable
        with self.assertRaises(ValueError):self.sm.dispatch('reset')
        self.assertIs(self.sm.monitor,old);self.assertTrue(old.failed)
        self.assertTrue((old.directory/'samples.csv').exists())
        self.sm.connection_factory=factory;self.sm.dispatch('reset')
        self.assertIsNot(self.sm.monitor,old)
    def test_failed_usb_uses_wifi_only_on_explicit_reset(self):
        self.sm.close();old=self.sm.monitor;old.failed=True;old.transport='USB COM4'
        calls=[]
        def wifi():
            calls.append('wifi');return SyntheticConnection(accelerated=True),'Wi-Fi test'
        self.sm.fallback_factory=wifi
        self.assertEqual(calls,[])
        self.sm.dispatch('reset')
        self.assertEqual(calls,['wifi']);self.assertEqual(self.sm.monitor.transport,'Wi-Fi test')
        self.assertIsNone(self.sm.monitor.detector.base)

if __name__=='__main__':unittest.main()
