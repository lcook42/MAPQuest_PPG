"""SYNTHETIC demonstration only. Does not connect to a sensor or measure a person."""
import argparse
from datetime import datetime
from pathlib import Path
import threading
import time
import webbrowser
import numpy as np
from monitor import Monitor,serve,ROOT
from rtf_detector import VERSION

class SyntheticConnection:
    def __init__(self,accelerated=False):
        self.pending=[]; self.streaming=False; self.seq=0; self.epoch=0
        self.amplitude=300; self.leds=[32,32]; self.rng=np.random.default_rng(48291)
        self.accelerated=accelerated
        self.three=False
    def send(self,s):
        if s=='HELLO': self.pending.append(f'HELLO,{VERSION},100,RED_IR,60,90,ESP32_DEVKIT_V1')
        elif s=='START': self.streaming=True;self.pending.append('ACK,START')
        elif s=='RTF3':
            self.three=True;self.leds=[32,32,32];self.streaming=True
            self.pending.append('ACK,RTF3,100,RED_IR_GREEN')
        elif s.startswith('LED3,'):
            self.leds=list(map(int,s.split(',')[1:]));self.epoch+=1
            self.pending.append('ACK,LED3,'+','.join(map(str,[*self.leds,self.epoch])))
        elif s.startswith('LED,'):
            self.leds=list(map(int,s.split(',')[1:]));self.epoch+=1
            self.pending.append(f'ACK,LED,{self.leds[0]},{self.leds[1]},{self.epoch}')
        elif s=='ARM': self.amplitude=0;self.pending.append('ACK,ARM')
        elif s.startswith('STATUS,DEFLATING'): self.amplitude=45
        elif s=='RELEASED': self.amplitude=300;self.pending.append('ACK,RELEASED')
        elif s=='STOP': self.streaming=False;self.pending.append('ACK,STOP')
    def read(self):
        time.sleep(.005 if self.accelerated else .1)
        if self.pending: p=self.pending;self.pending=[];return p
        if not self.streaming:return []
        lines=[]
        for _ in range(10):
            t=self.seq/100
            pulse=self.amplitude*(np.sin(2*np.pi*1.2*t)+.32*np.sin(4*np.pi*1.2*t-.5))
            r=int(100000+pulse+self.rng.normal(0,3));i=int(110000+.8*pulse+self.rng.normal(0,3))
            if self.three:
                # Deliberately unusable red; calibration must select IR/GREEN.
                r=int(100000+self.rng.normal(0,5));g=int(105000+.9*pulse+self.rng.normal(0,3))
                lines.append('D,'+','.join(map(str,[self.seq,self.seq*10,self.epoch,r,i,g,*self.leds])))
            else:lines.append(f'S,{self.seq},{self.seq*10},{self.epoch},{r},{i},{self.leds[0]},{self.leds[1]}')
            self.seq+=1
        return lines
    def close(self): pass

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--no-browser',action='store_true');args=p.parse_args()
    print('SYNTHETIC DEMO — NO SENSOR CONNECTED; DO NOT INFLATE A CUFF.')
    m=Monitor(SyntheticConnection(),ROOT/'Data'/('SYNTHETIC_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')),synthetic=True,three=True,transport='Synthetic')
    server=serve(m,8766,'synthetic-demo-local-token')
    worker=threading.Thread(target=m.run,daemon=True);worker.start()
    if not args.no_browser:webbrowser.open('http://127.0.0.1:8766')
    try:server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:m.closed.set();worker.join(timeout=3);server.server_close()
