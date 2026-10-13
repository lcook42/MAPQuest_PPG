import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from monitor import Monitor,serve,main,SOURCE_HASHES
from rtf_detector import Detector,DETECTOR_REVISION
from test_detector import FakeLink,calibrated,signal

class AuditGuards(unittest.TestCase):
    def test_unused_channel_extremes_do_not_block_cuff_pipeline(self):
        for pair in itertools.combinations(range(3),2):
            with self.subTest(pair=pair),tempfile.TemporaryDirectory() as td:
                m=Monitor(FakeLink(),Path(td)/'session',three=True)
                try:
                    m.connected=True;m.hello=True;m.selected_pair=pair
                    m.detector=calibrated();m.detector.channels=tuple(('RED','IR','GREEN')[i] for i in pair)
                    m.selection={'qualified':True,'channels':list(m.detector.channels)}
                    unused=next(i for i in range(3) if i not in pair)
                    seq=0
                    def feed(amp,seed):
                        nonlocal seq
                        rows=np.zeros((400,3));rows[:,pair]=signal(amp,seed=seed)
                        # The unused channel clips and jumps across the ADC range.
                        rows[:,unused]=np.where(np.arange(400)%2,262143,0)
                        for row in rows:
                            m.receive('D,'+','.join(map(str,[seq,seq*10,1,*map(int,row),32,32,32])))
                            seq+=1
                    feed(0,40);feed(0,41)
                    self.assertEqual(m.detector.state,'OCCLUDED')
                    m.detector.deflate()
                    feed(50,60);feed(50,61)
                    self.assertEqual(m.detector.state,'RTF')
                    m.publish()
                    self.assertEqual(m.view['decision_channels'],list(m.detector.channels))
                    self.assertFalse(m.view['channel_metrics'][unused]['used_for_detection'])
                    self.assertFalse(m.view['channel_metrics'][unused]['valid'])
                    meta=json.loads((m.directory/'replay.jsonl').read_text().splitlines()[0])
                    self.assertEqual(meta['detector_revision'],DETECTOR_REVISION)
                    self.assertEqual(meta['source_sha256'],SOURCE_HASHES)
                    self.assertTrue((m.directory/'source_snapshot/rtf_detector.py').exists())
                finally:
                    for f in (m.wire,m.trace,m.raw):f.close()

    def test_selected_channel_dc_shift_still_rejected(self):
        d=calibrated();d.process(signal(0,dc=(140000,110000)),12,16)
        self.assertEqual(d.state,'ABORTED');self.assertIsNone(d.result)

    def test_invalid_diagnostics_do_not_spam_inactive_trial(self):
        for state in ('IDLE','ABORTED','COMPLETE'):
            d=Detector();d.state=state
            for i in range(3):d.process(np.zeros((400,2)),i*4,i*4+4)
            self.assertEqual(d.state,state);self.assertEqual(d.events,[])
            self.assertFalse(d.last_feature.valid)

    def test_second_dashboard_cannot_open_recorder(self):
        server=serve(None,0,'test')
        try:
            port=server.server_address[1]
            with patch('sys.argv',['monitor.py','--usb','COM_FAKE','--http-port',str(port),'--no-browser']),patch('monitor.Connection') as connection:
                with self.assertRaises(OSError):main()
                connection.assert_not_called()
        finally:server.server_close()

if __name__=='__main__':unittest.main()
