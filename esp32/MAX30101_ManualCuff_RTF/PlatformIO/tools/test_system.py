"""End-to-end synthetic stream, dashboard API, logs, and deterministic replay."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from monitor import Monitor,serve
from demo import SyntheticConnection
from replay import replay

class SystemTests(unittest.TestCase):
    three=False
    def test_complete_pipeline_and_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=Monitor(SyntheticConnection(accelerated=True),Path(tmp)/'SYNTHETIC',synthetic=True,site='forearm',three=self.three)
            server=serve(m,0,'test-token')
            root=f'http://127.0.0.1:{server.server_address[1]}'
            worker=threading.Thread(target=m.run,daemon=True);worker.start()
            http=threading.Thread(target=server.serve_forever,daemon=True);http.start()
            def command(name,value=None):
                req=Request(root+'/command',json.dumps({'name':name,'value':value}).encode(),
                            headers={'X-RTF-Token':'test-token','Content-Type':'application/json'})
                return json.load(urlopen(req,timeout=2))
            def wait_state(state,timeout=12):
                until=time.monotonic()+timeout
                while time.monotonic()<until:
                    s=json.load(urlopen(root+'/state',timeout=2))
                    if s['state']==state:return s
                    if s['failed']:self.fail(s['messages'])
                    time.sleep(.02)
                self.fail(f'Never reached {state}: {s}')
            try:
                self.assertIn('PPG Return to Flow',urlopen(root).read().decode())
                with self.assertRaises(HTTPError):urlopen(Request(root+'/command',b'{"name":"arm"}'))
                until=time.monotonic()+3
                while not m.connected and time.monotonic()<until:time.sleep(.02)
                command('calibrate');wait_state('READY')
                command('pressure',130);command('arm');wait_state('OCCLUDED')
                command('pressure',110);command('deflate');s=wait_state('RTF')
                self.assertTrue(s['synthetic']);self.assertIsNone(s['result']['map_mmHg'])
                self.assertEqual(s['sensor_site'],'forearm')
                self.assertEqual(s['result']['sensor_site'],'forearm')
                if self.three:
                    self.assertEqual(s['result']['channels'],['IR','GREEN'])
                    self.assertEqual(len(m.tune_results),21)
                self.assertEqual(s['result']['candidate_pressure_annotation']['mmHg'],110)
                command('released');wait_state('COMPLETE')
            finally:
                m.closed.set();worker.join(timeout=3);server.shutdown();server.server_close();http.join(timeout=2)
            replayed=replay(Path(tmp)/'SYNTHETIC/replay.jsonl')
            self.assertEqual(replayed['final_state'],'COMPLETE');self.assertGreaterEqual(replayed['windows'],8)
            self.assertIsNotNone(replayed['result'])
            self.assertEqual(replayed['result']['sensor_site'],'forearm')
            if self.three:self.assertEqual(replayed['result']['channels'],['IR','GREEN'])

class ThreeChannelSystemTests(SystemTests):
    three=True

if __name__=='__main__':unittest.main(verbosity=2)
