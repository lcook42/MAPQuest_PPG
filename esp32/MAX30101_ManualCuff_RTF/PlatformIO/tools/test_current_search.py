import tempfile,unittest
from pathlib import Path
from monitor import Monitor
from test_detector import signal

class Link:
    def __init__(self):self.sent=[]
    def send(self,s):self.sent.append(s)

class CurrentSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.m=Monitor(Link(),Path(self.tmp.name)/'test')
        self.m.connected=True;self.m.act('calibrate');self.seq=0;self.epoch=0
    def tearDown(self):
        for f in (self.m.wire,self.m.trace,self.m.raw):f.close()
        self.tmp.cleanup()
    def acknowledge(self):
        self.epoch+=1;r,i=self.m.leds
        self.m.receive(f'ACK,LED,{r},{i},{self.epoch}');self.m.tune_skip=0
    def feed(self,values):
        for r,i in values:
            self.seq+=1;self.m.tune((self.seq,self.seq*10,self.epoch,int(r),int(i),*self.m.leds))
    def prepare(self):
        self.acknowledge();self.feed(signal()[:100]);self.assertEqual(self.m.tune_phase,'quality')
    def test_adjusts_current_for_quality_then_requires_new_baseline(self):
        self.prepare();self.acknowledge()
        self.feed(signal(0,noise=5,seed=20));self.feed(signal(0,noise=5,seed=21))
        self.assertEqual(self.m.leds,[24,32]);self.acknowledge()
        self.feed(signal(seed=1));self.feed(signal(seed=2))
        self.assertEqual(self.m.tune_phase,'freeze');self.assertEqual(self.m.leds,[24,32])
        self.acknowledge();self.feed(signal()[:1])
        self.assertFalse(self.m.tuning);self.assertEqual(self.m.detector.state,'BASELINE')
        self.assertEqual(len(self.m.detector.baselines),0)
    def test_flatline_at_all_currents_cannot_pass(self):
        self.prepare()
        for _ in range(5):
            self.acknowledge();self.feed([[100000,110000]]*800)
        self.assertEqual(self.m.detector.state,'ABORTED')
    def test_gap_during_quality_search_aborts(self):
        self.prepare();self.acknowledge();self.feed(signal()[:1]);self.seq+=1
        self.feed(signal()[:1]);self.assertTrue(self.m.failed)

if __name__=='__main__':unittest.main()
