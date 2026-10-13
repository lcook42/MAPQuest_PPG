"""Live local dashboard and single-owner USB/TCP acquisition for pulsatile RTF."""
import argparse
import csv
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import sys
import ipaddress
import hashlib
import threading
import time
import webbrowser

import numpy as np
from rtf_detector import Detector, WindowBuffer, VERSION, DETECTOR_REVISION, parse_sample, extract
from channel_calibration import CHANNELS, parse_three, candidates, compare, noise_profile
from calibration_history import CalibrationHistory
from troubleshooting import suggestions

ROOT=Path(__file__).resolve().parents[2]
HOST_REVISION='2026-10-12-r3-host-capture1'
SOURCE_SNAPSHOT={name:Path(__file__).with_name(name).read_bytes() for name in
                 ('monitor.py','rtf_detector.py','channel_calibration.py','calibration_history.py','troubleshooting.py','replay.py')}
SOURCE_HASHES={name:hashlib.sha256(data).hexdigest() for name,data in SOURCE_SNAPSHOT.items()}

def choose_connection(host):
    from serial.tools import list_ports
    ports=[p for p in list_ports.comports() if (p.vid,p.pid)==(0x10c4,0xea60)]
    if len(ports)>1:raise RuntimeError('Multiple CP210x boards connected; use --usb COM_PORT to select the recorder')
    if ports:
        try:return Connection(port=ports[0].device),'USB '+ports[0].device
        except OSError:pass
    return Connection(host=host),'Wi-Fi '+host

class Connection:
    def __init__(self, host=None, port=None):
        self.capture_rx_fault=None
        self.tcp=bool(host)
        if host:
            self.io=socket.create_connection((host,8765),timeout=5)
            self.io.settimeout(.1)
            self.io.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        else:
            import serial
            self.io=serial.Serial(port=None,baudrate=115200,timeout=.1,write_timeout=1)
            # Avoid asserting the ESP32 auto-reset/boot control lines on open.
            self.io.dtr=False;self.io.rts=False;self.io.port=port;self.io.open()
            time.sleep(1)
            self.io.reset_input_buffer()
        self.pending=b''

    def send(self,s):
        b=(s+'\n').encode('ascii')
        if self.tcp: self.io.sendall(b)
        else: self.io.write(b)

    def read(self):
        try:
            b=self.io.recv(8192) if self.tcp else self.io.read(self.io.in_waiting or 1)
        except socket.timeout:
            return []
        if self.tcp and not b: raise ConnectionError('Wi-Fi disconnected')
        self.pending+=b
        if len(self.pending)>65536:
            if self.capture_rx_fault:self.capture_rx_fault(self.pending,'framing_overflow')
            raise ConnectionError('serial/network framing overflow')
        lines=self.pending.split(b'\n'); self.pending=lines.pop()
        # Boot chatter is tolerated only before a verified HELLO by the host.
        # Invalid bytes after handshake remain a fatal acquisition error.
        decoded=[]
        for s in lines:
            # Preserve the actual offending bytes before replacement decoding.
            # Previously wire.txt retained only U+FFFD, losing diagnostic evidence.
            if not s.isascii() and self.capture_rx_fault:
                self.capture_rx_fault(s+b'\n','invalid_ascii')
            decoded.append(s.decode('ascii',errors='replace').strip())
        return decoded

    def close(self): self.io.close()

class Monitor:
    def __init__(self, connection, directory, sensitive=False, synthetic=False, site='upper_arm', three=False, transport=None):
        self.link=connection
        self.three=three; self.nchannels=3 if three else 2
        self.selected_pair=(0,1); self.selection=None; self.channel_metrics=[]
        self.transport=transport or ('Wi-Fi' if getattr(connection,'tcp',False) else 'USB')
        self.detector=Detector(sensitive,site)
        self.buffer=WindowBuffer(self.nchannels)
        self.commands=queue.Queue()
        self.view_lock=threading.Lock()
        self.view={}
        self.closed=threading.Event()
        self.directory=directory
        self.synthetic=synthetic
        self.directory.mkdir(parents=True,exist_ok=False)
        self.wire=(directory/'wire.txt').open('w',encoding='utf-8',buffering=1)
        self.trace=(directory/'replay.jsonl').open('w',encoding='utf-8',buffering=1)
        self.raw=(directory/'samples.csv').open('w',newline='',encoding='utf-8',buffering=1)
        self.writer=csv.writer(self.raw)
        self.link.capture_rx_fault=lambda data,reason:self.record('RX_FAULT_BYTES',
            reason=reason,byte_count=len(data),hex=data.hex(),transport=self.transport)
        self.writer.writerow(['host_monotonic','sequence','device_read_ms','epoch']+
                             (['red','ir','green','red_led','ir_led','green_led'] if three else ['red','ir','red_led','ir_led']))
        self.messages=[]; self.wave=[]; self.annotation=None; self.annotated_at=None
        self.hello=False; self.connected=False; self.tuning=False; self.tune_rows=[]
        self.tune_wait=False; self.tune_skip=0; self.tune_round=0; self.leds=[32]*self.nchannels
        self.tune_phase='brightness'; self.tune_candidates=[]; self.tune_results=[]
        self.tune_quality=[]; self.tune_buffer=WindowBuffer(); self.tune_index=0
        self.calibration_history=CalibrationHistory();self.use_calibration_history=True
        self.history_attempt_active=False;self.brightness_history=[]
        self.last_problem=''
        self.last_rx=time.monotonic(); self.last_sample=self.last_rx; self.last_ping=0
        self.started=self.last_rx; self.arm_at=None; self.release_at=None; self.warned=False
        self.recovery_skipped=False;self.reset_connection=False
        self.last_processed=None; self.failed=False
        self.led_ack_at=None; self.expected_epoch=None
        snapshot=self.directory/'source_snapshot';snapshot.mkdir(exist_ok=True)
        for name,data in SOURCE_SNAPSHOT.items():(snapshot/name).write_bytes(data)
        self.record('META',version=VERSION,detector_revision=DETECTOR_REVISION,source_sha256=SOURCE_HASHES,
                    host_revision=HOST_REVISION,
                    sensitive=sensitive,synthetic=synthetic,sensor_site=site,
                    acquisition_channels=list(CHANNELS[:self.nchannels]),transport=self.transport)
        self.message('Sensor site: '+site.replace('_',' ')+'. Support the arm; mount distal to the cuff, outside its compression area. Keep cuff fully deflated.')
        self.publish()

    def record(self,kind,**data):
        line=json.dumps({'kind':kind,'host_time':time.monotonic(),**data},allow_nan=False)+'\n'
        if self.trace.closed:
            with (self.directory/'replay.jsonl').open('a',encoding='utf-8') as trace:trace.write(line)
        else:self.trace.write(line)

    def message(self,text):
        self.messages.append(text); self.messages=self.messages[-14:]
        print(text,flush=True)

    def drain_events(self):
        while self.detector.events:
            event=self.detector.events.pop(0)
            if event['state']=='ABORTED' or event['message'].startswith(('Unusable window:','Baseline not ready:','Baseline varied','Suppression unconfirmed:','Not suppressed:')):
                self.last_problem=event['message']
            self.record('EVENT',**event); self.message(event['message'])
        if self.detector.result:
            (self.directory/'result.json').write_text(json.dumps(self.detector.result,indent=2),encoding='utf-8')

    def publish(self):
        snap=self.detector.snapshot()
        decision_channels=list(self.detector.channels) if self.selection and self.selection.get('qualified') and not self.tuning else []
        snap.update(connected=self.connected,tuning=self.tuning,failed=self.failed,synthetic=self.synthetic,messages=self.messages.copy(),
                    transport=self.transport,acquisition_channels=list(CHANNELS[:self.nchannels]),
                    selection=self.selection,channel_metrics=[dict(v,used_for_detection=v['channel'] in decision_channels) for v in self.channel_metrics],leds=self.leds,
                    decision_channels=decision_channels,detector_revision=DETECTOR_REVISION,
                    host_revision=HOST_REVISION,
                    calibration_history=self.calibration_history.summaries(),use_calibration_history=self.use_calibration_history,
                    failure_reason=self.last_problem,failure_suggestions=suggestions(self.last_problem,self.tune_results if self.three and self.selection and not self.selection.get('qualified') else []),
                    tune_phase=self.tune_phase,tune_trial=self.tune_index+1,tune_total=len(self.tune_candidates),
                    waveform=self.wave[-1000:],pressure_annotation=self.annotation,
                    pressure_age=None if self.annotated_at is None else round(time.monotonic()-self.annotated_at,1),
                    cuff_seconds=None if self.arm_at is None else round(time.monotonic()-self.arm_at,1),
                    rest_remaining=max(0,round(60-(time.monotonic()-self.release_at))) if self.release_at and not self.recovery_skipped else 0,
                    recovery_skipped=self.recovery_skipped,
                    logs=str(self.directory),version=VERSION)
        with self.view_lock: self.view=snap

    def issue(self,text, fatal=False):
        self.last_problem=text
        self.buffer.clear()
        self.record('ISSUE',reason=text,fatal=fatal)
        if fatal:
            self.finish_calibration_attempt('interrupted',text)
            self.failed=True; self.connected=False; self.tuning=False
            self.detector.abort(text)
            try: self.link.send('STOP')
            except Exception: pass
        else: self.detector.invalidate(text)

    def act(self,name,value=None):
        now=time.monotonic()
        if name=='stop':
            self.finish_calibration_attempt('interrupted','Operator stopped calibration')
            self.record('ACTION',name='stop',now=now)
            self.detector.abort('Operator stopped the test.'); self.tuning=False
            self.link.send('STOP'); self.connected=False; self.buffer.clear()
            self.message('After physically releasing the cuff, press Cuff fully released. Restart the app for a new session.')
            return
        if name=='released':
            if self.tuning: raise ValueError('Calibration is running; cuff must already be fully deflated')
            self.record('ACTION',name='released',now=now)
            if self.connected:self.link.send('RELEASED')
            self.arm_at=None; self.release_at=now; self.recovery_skipped=False
            self.detector.released(); self.buffer.clear()
            return
        if name=='skip_recovery':
            if self.arm_at is not None or self.detector.state in Detector.ACTIVE or self.release_at is None:
                raise ValueError('Acknowledge that the cuff is fully released before skipping the recovery wait')
            self.recovery_skipped=True
            self.record('ACTION',name='skip_recovery',now=now)
            self.message('Recovery wait skipped by operator. A new calibration is still required.')
            return
        if name in ('remove_calibration','clear_calibrations','use_calibration_history'):
            if self.tuning or self.arm_at is not None or self.detector.state in Detector.ACTIVE:
                raise ValueError('Finish calibration and release cuff before editing calibration history')
            if name=='remove_calibration':
                if type(value) is not int:raise ValueError('Calibration attempt ID must be an integer')
                self.calibration_history.remove(value)
            elif name=='clear_calibrations':
                for attempt in self.calibration_history.attempts:attempt['included']=False
            else:
                if type(value) is not bool:raise ValueError('History option must be true or false')
                self.use_calibration_history=value
            self.record('ACTION',name=name,value=value,now=now)
            self.save_calibration_history()
            self.message('Calibration history updated for the next search. Current baseline and recording are unchanged.')
            return
        if not self.connected or self.failed: raise ValueError('No live recorder connection; restart the app after releasing cuff')
        if name=='calibrate':
            if self.tuning: raise ValueError('Calibration is already running')
            if self.arm_at is not None or self.detector.state in Detector.ACTIVE:
                raise ValueError('Release cuff before calibration')
            if self.release_at and not self.recovery_skipped and now-self.release_at<60:
                raise ValueError('Allow the 60-second recovery interval before starting again')
            self.detector=Detector(self.detector.sensitive,self.detector.site)
            self.record('ACTION',name='setup',now=now)
            self.tuning=True; self.tune_round=0; self.tune_rows=[]; self.buffer=WindowBuffer(self.nchannels)
            self.tune_phase='brightness'; self.tune_candidates=[]; self.tune_results=[]
            self.tune_quality=[]; self.tune_buffer=WindowBuffer(self.nchannels); self.tune_index=0
            self.selection=None; self.selected_pair=(0,1)
            self.last_problem=''
            self.history_attempt_active=self.three;self.brightness_history=[]
            hints=self.calibration_history.ranked_settings() if self.three and self.use_calibration_history else []
            self.set_tune_leds(hints[0]['leds'] if hints else [32]*self.nchannels)
            self.record('CALIBRATION_START',attempt=len(self.calibration_history.attempts)+1,
                        use_history=self.use_calibration_history,seed=self.leds.copy(),hints=hints[:3])
            if hints:self.message('Using retained calibration results to seed the current search. All pairs must pass fresh checks.')
            self.message('Automatic LED setup: keep cuff DEFLATED and sensor still. Checking brightness, then comparing pulse quality and channel pairs (about 1–2 minutes).')
        elif name=='arm':
            if self.tuning: raise ValueError('Wait for optical setup to finish')
            self.detector.arm(now)
            self.record('ACTION',name='arm',now=now)
            self.link.send('ARM'); self.arm_at=now; self.warned=False
            self.buffer.clear(); self.annotation=None; self.annotated_at=None
        elif name=='deflate':
            self.detector.deflate(); self.record('ACTION',name='deflate',now=now)
            self.buffer.clear()
        elif name=='pressure':
            p=float(value)
            if not math.isfinite(p) or not 0<=p<=250: raise ValueError('Gauge annotation must be between 0 and 250 mmHg')
            self.annotation=p; self.annotated_at=now
            self.record('ANNOTATION',pressure=p)
            self.message(f'Gauge annotation {p:g} mmHg recorded. This is not continuous pressure measurement.')
        else: raise ValueError('Unknown command')

    def tune(self,row):
        if self.three: return self.tune_three(row)
        if self.tune_wait: return
        if self.tune_skip:
            self.tune_skip-=1; return
        if self.tune_phase=='freeze':
            self.tuning=False; self.buffer=WindowBuffer()
            self.detector.calibrate(); self.record('ACTION',name='calibrate',now=time.monotonic())
            self.message(f'LEDs frozen at RED {self.leds[0]}, IR {self.leds[1]}. Now requiring three NEW stable baseline windows.')
            return
        if self.tune_phase=='quality':
            rows,issue=self.tune_buffer.push(row)
            if issue: self.issue('LED search: '+issue,fatal=True); return
            if rows is None:return
            samples=[list(r[3:5]) for r in rows]; f=extract(samples)
            self.tune_quality.append(f)
            self.record('LED_QUALITY',leds=self.leds.copy(),samples=samples,features=asdict(f))
            self.message(f'Current trial {self.tune_index+1}/{len(self.tune_candidates)} RED {self.leds[0]}, IR {self.leds[1]}: '+self.detector.baseline_feedback(f))
            if len(self.tune_quality)<2:return
            good=all(self.detector._baseline_good(v) for v in self.tune_quality)
            score=min(self.quality_score(v) for v in self.tune_quality)
            self.tune_results.append((score,self.leds.copy()))
            if good or self.tune_index+1>=len(self.tune_candidates):
                best=max(self.tune_results,key=lambda item:item[0])
                if best[0]<0:
                    self.tuning=False;self.detector.abort('No usable optical window during current search. Keep cuff deflated; check attachment and light shielding.');return
                self.tune_phase='freeze';self.set_tune_leds(best[1])
                self.message('Current search complete. Best observed setting selected; calibration is NOT passed yet.')
            else:
                self.tune_index+=1;self.tune_quality=[]
                self.set_tune_leds(self.tune_candidates[self.tune_index])
            return
        self.tune_rows.append(row[3:5])
        if len(self.tune_rows)<100: return
        dc=np.median(self.tune_rows,axis=0); self.tune_rows=[]; self.tune_round+=1
        if np.all((dc>=65000)&(dc<=160000)):
            self.tune_candidates=[]
            for factors in ((1,1),(.75,1),(1,.75),(1.2,1),(1,1.2)):
                candidate=np.clip(np.rint(np.array(self.leds)*factors),1,200).astype(int).tolist()
                if candidate not in self.tune_candidates:self.tune_candidates.append(candidate)
            self.tune_phase='quality';self.tune_index=0;self.tune_quality=[]
            self.set_tune_leds(self.tune_candidates[0])
            self.message('Brightness is usable. Comparing pulse quality across bounded RED/IR current settings; cuff must stay deflated.')
            return
        if self.tune_round>=8:
            self.tuning=False; self.detector.abort('Optical setup failed: check placement, light shielding and wiring. Cuff must remain deflated.')
            return
        next_led=np.clip(np.rint(np.array(self.leds)*np.clip(110000/np.maximum(dc,1),.5,2)),1,200).astype(int).tolist()
        if next_led==self.leds:
            self.tuning=False; self.detector.abort('LED limit reached without suitable light level. Reposition sensor.'); return
        self.leds=next_led; self.tune_wait=True
        self.link.send(f'LED,{next_led[0]},{next_led[1]}')

    def quality_score(self,f):
        if not f.valid:return -1.
        # Prefer actual baseline-qualified pulses; DC brightness earns no points.
        timing=max(0.,1-abs(f.hz[0]-f.hz[1])/.12)
        return float(self.detector._baseline_good(f))+min(f.fraction)*max(0.,min(f.periodicity))*max(0.,f.corr)*timing

    def set_tune_leds(self,leds):
        self.leds=list(leds);self.tune_wait=True;self.tune_rows=[];self.tune_buffer=WindowBuffer(self.nchannels)
        self.wave=[];self.channel_metrics=[]
        self.led_ack_at=time.monotonic(); self.expected_epoch=None
        self.link.send(('LED3,' if self.three else 'LED,')+','.join(map(str,leds)))

    def tune_three(self,row):
        if self.tune_wait:return
        if self.tune_skip:
            self.tune_skip-=1;return
        if self.tune_phase=='freeze':
            self.tuning=False;self.buffer=WindowBuffer(3)
            self.detector.channels=tuple(CHANNELS[i] for i in self.selected_pair)
            self.detector.calibrate()
            self.record('ACTION',name='calibrate',now=time.monotonic(),channels=list(self.detector.channels))
            self.message('Selected '+ '/'.join(self.detector.channels)+'. All LED settings frozen. Collecting three NEW stable baseline windows.')
            return
        if self.tune_phase=='quality':
            rows,issue=self.tune_buffer.push(row)
            if issue:self.issue('LED search: '+issue,fatal=True);return
            if rows is None:return
            samples=[list(r[3:6]) for r in rows]
            self.update_channel_metrics(np.array(samples))
            self.tune_quality.append(samples)
            if len(self.tune_quality)<2:return
            results=compare(self.tune_quality,self.leds,self.detector.sensitive)
            self.tune_results.extend(results)
            self.record('CHANNEL_QUALITY',trial=self.tune_index+1,samples=self.tune_quality,results=results)
            best=max(results,key=lambda r:r['score'])
            self.message(f'Current trial {self.tune_index+1}/{len(self.tune_candidates)}: best pair '+
                         '/'.join(best['channels'])+(' passed both search windows.' if best['qualified'] else ': '+best['feedback'][-1]))
            if self.tune_index+1<len(self.tune_candidates):
                self.tune_index+=1;self.tune_quality=[]
                self.set_tune_leds(self.tune_candidates[self.tune_index]);return
            best=max(self.tune_results,key=lambda r:r['score'])
            self.selection=best
            self.finish_calibration_attempt('search_passed' if best['qualified'] else 'no_pair',
                                            'Fresh baseline still required' if best['qualified'] else 'No pair qualified')
            (self.directory/'calibration.json').write_text(json.dumps({'selected':best,'trials':self.tune_results},indent=2),encoding='utf-8')
            self.record('SELECTION',**best)
            if not best['qualified']:
                self.reject_calibration('No pair passed both independent search windows. Keep cuff deflated; improve contact/placement and Calibrate again.')
                return
            self.selected_pair=tuple(best['pair']);self.tune_phase='freeze'
            self.set_tune_leds(best['leds'])
            return
        self.tune_rows.append(row[3:6])
        if len(self.tune_rows)<100:return
        dc=np.median(self.tune_rows,axis=0);self.tune_rows=[];self.tune_round+=1
        self.brightness_history.append(dict(leds=self.leds.copy(),dc=dc.tolist()))
        usable=(dc>=65000)&(dc<=160000)
        headroom=(dc>=500)&(dc<=250000)
        next_led=np.clip(np.rint(np.array(self.leds)*np.clip(110000/np.maximum(dc,1),.5,2)),1,200).astype(int)
        next_led[usable]=np.array(self.leds)[usable]
        exhausted=self.tune_round>=8 or next_led.tolist()==self.leds
        if np.all(usable) or (exhausted and np.count_nonzero(headroom)>=2):
            self.tune_candidates=candidates(self.leds);self.tune_index=0;self.tune_quality=[]
            if self.use_calibration_history:
                for hint in self.calibration_history.ranked_settings()[:3]:
                    if hint['leds'] not in self.tune_candidates:self.tune_candidates.append(hint['leds'])
            self.tune_phase='quality';self.set_tune_leds(self.tune_candidates[0])
            self.message('Brightness setup complete. Comparing RED/IR, RED/GREEN and IR/GREEN at every current setting.')
        elif exhausted:
            self.reject_calibration('Fewer than two channels have usable brightness. Keep cuff deflated and check placement.')
        else:self.set_tune_leds(next_led.tolist())

    def reject_calibration(self,reason):
        self.last_problem=reason
        self.finish_calibration_attempt('failed',reason)
        self.tuning=False;self.detector.abort(reason)
        self.record('ACTION',name='calibration_rejected',reason=reason,now=time.monotonic())

    def save_calibration_history(self):
        payload=dict(use_history=self.use_calibration_history,attempts=self.calibration_history.attempts,
                     ranked_settings=self.calibration_history.ranked_settings())
        target=self.directory/'calibration_history.json'
        temporary=target.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload,indent=2),encoding='utf-8');temporary.replace(target)

    def finish_calibration_attempt(self,status,reason=''):
        if not self.history_attempt_active:return
        self.history_attempt_active=False
        attempt=self.calibration_history.add(self.tune_results,status,reason,self.brightness_history)
        self.record('CALIBRATION_ATTEMPT',**attempt)
        self.save_calibration_history()

    def update_channel_metrics(self,samples):
        self.channel_metrics=[]
        spectrum=noise_profile(samples)
        for i,label in enumerate(CHANNELS):
            cf=extract(samples[:,[i,i]])
            self.channel_metrics.append(dict(channel=label,dc=cf.dc[0],pulse=cf.pulse[0],
                fit=cf.fraction[0],repeat=cf.periodicity[0],valid=cf.valid,reason=cf.reason,
                slow_percent=spectrum['slow_percent'][i],high_frequency_percent=spectrum['high_frequency_percent'][i]))

    def receive(self,line):
        now=time.monotonic(); self.last_rx=now
        self.wire.write(line+'\n')
        if not line.isascii():
            if self.hello:raise ConnectionError('Invalid bytes in recorder stream. Use Reset session; if repeated, check USB connection/power.')
            return
        if line.startswith('HELLO,'):
            if line != f'HELLO,{VERSION},100,RED_IR,60,90,ESP32_DEVKIT_V1':
                raise ConnectionError('Firmware/protocol mismatch. Upload this release before testing.')
            if not self.hello:
                self.hello=True
                if self.reset_connection:
                    self.link.send('STOP');self.link.send('RELEASED')
                self.link.send('RTF3' if self.three else 'START')
            return
        if line==('ACK,RTF3,100,RED_IR_GREEN' if self.three else 'ACK,START'):
            if not self.hello: raise ConnectionError('Recorder started without compatible handshake')
            self.connected=True; self.last_sample=now
            self.message('Recorder connected. Press Calibrate with the cuff fully deflated.'); return
        if line.startswith('ACK,LED3,') and self.three:
            parts=line.split(',')
            if not self.tune_wait or len(parts)!=6 or list(map(int,parts[2:5]))!=self.leds:
                raise ConnectionError('Unexpected three-channel LED acknowledgement')
            self.expected_epoch=int(parts[5]);self.led_ack_at=None
            self.tune_wait=False;self.tune_skip=50;self.tune_rows=[]
            self.buffer=WindowBuffer(3);self.tune_buffer=WindowBuffer(3);return
        if line.startswith('ACK,LED,'):
            parts=line.split(',')
            if len(parts)!=5 or list(map(int,parts[2:4]))!=self.leds:
                raise ConnectionError('Unexpected LED acknowledgement')
            self.tune_wait=False; self.tune_skip=50; self.tune_rows=[]; self.buffer=WindowBuffer(); self.tune_buffer=WindowBuffer(); return
        if line.startswith('FAULT,') or line.startswith('ERROR,'):
            raise ConnectionError(line)
        if line.startswith('WARN,'): self.message('CUFF TIMER WARNING: fully release cuff if the test cannot be completed promptly.'); return
        if line.startswith(('S,','D,')) and not line.startswith('D,' if self.three else 'S,'):
            raise ConnectionError('Unexpected acquisition channel count')
        if not line.startswith('D,' if self.three else 'S,'): return
        row=parse_three(line) if self.three else parse_sample(line)
        if self.three and self.expected_epoch is not None and (row[2]!=self.expected_epoch or list(row[6:])!=self.leds):
            raise ConnectionError('Samples disagree with frozen LED settings / configuration epoch')
        self.last_sample=now; self.writer.writerow([now,*row])
        # A current change creates a real DC step, not a pulse. Keep every raw
        # sample in the CSV, but do not join different settings in the live plot.
        if not self.tuning or (not self.tune_wait and not self.tune_skip):
            self.wave.append([row[0]/100,*row[3:3+self.nchannels]]); self.wave=self.wave[-1200:]
        if not self.connected: return
        if self.tuning: self.tune(row); return
        rows,issue=self.buffer.push(row)
        if issue:
            # Never allow a sample transport/configuration fault to establish occlusion.
            self.issue(issue,fatal=True); return
        if rows is None: return
        start=now-((rows[-1][1]-rows[0][1])&0xFFFFFFFF)/1000
        annotation=None if self.annotation is None else {'mmHg':self.annotation,
                      'entered_host_time':self.annotated_at,'age_seconds':now-self.annotated_at,
                      'meaning':'manual annotation only; not pressure at event'}
        raw_samples=np.array([r[3:3+self.nchannels] for r in rows])
        if self.three:
            self.update_channel_metrics(raw_samples)
        samples=raw_samples[:,self.selected_pair].tolist()
        before=self.detector.state
        f=self.detector.process(samples,start,now,annotation)
        if not f.valid:self.last_problem=f.reason
        elif self.detector.state in ('READY','OCCLUDED','CANDIDATE','RTF','COMPLETE'):self.last_problem=''
        self.record('WINDOW',samples=samples,start=start,end=now,annotation=annotation,channels=list(self.detector.channels),
                    state_before=before,state_after=self.detector.state,features=asdict(f))
        self.last_processed=now

    def run(self):
        try:
            self.link.send('HELLO')
            while not self.closed.is_set() and not self.failed:
                now=time.monotonic()
                while True:
                    try: cmd=self.commands.get_nowait()
                    except queue.Empty: break
                    try: self.act(cmd['name'],cmd.get('value'))
                    except (ValueError,TypeError) as exc: self.message(str(exc))
                for line in self.link.read():
                    self.receive(line)
                    if self.failed: break
                if not self.hello and now-self.started>8: raise ConnectionError('No compatible firmware response')
                if self.connected and now-self.last_sample>2: raise ConnectionError('No fresh PPG samples for 2 seconds')
                if self.three and self.led_ack_at is not None and now-self.led_ack_at>3:
                    raise ConnectionError('LED configuration acknowledgement timed out')
                if self.arm_at is not None:
                    elapsed=now-self.arm_at
                    if elapsed>=90: raise ConnectionError('90-second cuff-phase limit reached')
                    if elapsed>=60 and not self.warned:
                        self.warned=True; self.message('60-second cuff warning. Release cuff if the measurement cannot finish promptly.')
                if now-self.last_ping>=1:
                    self.last_ping=now
                    if not self.hello:
                        self.link.send('HELLO')
                    self.link.send('PING')
                    snap=self.detector.snapshot()
                    detail='Baseline / pulse analysis'
                    if snap['features'] and self.detector.base:
                        ratio=max(np.array(snap['features']['pulse'])/self.detector.base['pulse'])*100
                        detail=f'Pulse {ratio:.0f}% of base'
                    if self.detector.state=='RTF': detail='Return detected. Release cuff'
                    if self.detector.state=='ABORTED': detail='Fully release cuff'
                    self.link.send('STATUS,'+('OPTICAL SETUP' if self.tuning else self.detector.state)+','+detail)
                self.drain_events(); self.publish()
        except Exception as exc:
            self.issue(str(exc),fatal=True)
        finally:
            self.finish_calibration_attempt('interrupted','Recorder worker stopped')
            self.connected=False;self.tuning=False
            self.drain_events(); self.publish()
            try: self.link.send('STOP')
            except Exception: pass
            self.link.close()
            self.wire.close(); self.trace.close(); self.raw.close()

class SessionManager:
    """Replace the live recorder, buffers and calibration; preserve session files."""
    def __init__(self,connection_factory,directory_factory,fallback_factory=None,**options):
        self.connection_factory=connection_factory;self.directory_factory=directory_factory
        self.fallback_factory=fallback_factory
        self.options=options;self.lock=threading.RLock();self.monitor=None;self.worker=None
        self.start()

    def start(self,reset=False,use_fallback=False):
        link,label=(self.fallback_factory if use_fallback else self.connection_factory)()
        try:self.monitor=Monitor(link,self.directory_factory(),transport=label,**self.options)
        except Exception:
            link.close();raise
        self.monitor.reset_connection=reset
        self.worker=threading.Thread(target=self.monitor.run,daemon=True);self.worker.start()

    def reset(self):
        old=self.monitor
        if old.arm_at is not None or old.detector.state in Detector.ACTIVE:
            raise ValueError('Fully release the cuff and press Cuff is fully released before resetting')
        while not old.commands.empty():
            try:old.commands.get_nowait()
            except queue.Empty:break
        old.closed.set();self.worker.join(timeout=3)
        if self.worker.is_alive():raise ValueError('Recorder is still stopping; try Reset session again shortly')
        if old.arm_at is not None:
            old.detector.abort('Reset interrupted: acknowledge full cuff release before restarting')
            old.drain_events();old.publish()
            raise ValueError('Cuff phase started before reset completed; fully release cuff and acknowledge release')
        old.record('SESSION_RESET',saved_directory=str(old.directory))
        fallback=bool(old.failed and old.transport.startswith('USB') and self.fallback_factory)
        try:self.start(reset=True,use_fallback=fallback)
        except Exception as exc:
            self.monitor=old
            old.failed=True;old.message('Reset could not reconnect: '+str(exc)+'. Reconnect recorder and retry Reset session.')
            old.publish();raise ValueError('Could not reconnect the recorder; saved data is preserved') from exc
        self.monitor.message('New session. Calibration, plots and live data cleared; previous recordings kept at '+str(old.directory))
        if fallback:self.monitor.message('Previous USB session failed. Reconnected through Wi-Fi; a fresh calibration is required.')
        self.monitor.publish()

    def dispatch(self,name,value=None):
        with self.lock:
            if name in ('wifi_setup','connect_wifi'):
                if self.monitor.arm_at is not None or self.monitor.detector.state in Detector.ACTIVE or self.monitor.tuning:
                    raise ValueError('Finish setup or fully release the cuff before changing connection')
                if name=='wifi_setup':
                    if getattr(self,'setup_process',None) and self.setup_process.poll() is None:
                        self.setup_process.terminate();self.setup_process.wait(timeout=3)
                    try:
                        with socket.socket() as probe:probe.bind(('127.0.0.1',8768))
                    except OSError:raise ValueError('Wi-Fi setup port 8768 is already in use. Close the previous setup service and retry.')
                    self.setup_process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('configure_wifi.py')),
                            '--no-browser','--site',self.monitor.detector.site,'--monitor-port',str(self.http_port),
                            '--monitor-token',self.http_token],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    deadline=time.monotonic()+5
                    while True:
                        if self.setup_process.poll() is not None:raise ValueError('Wi-Fi setup could not start')
                        try:
                            with socket.create_connection(('127.0.0.1',8768),timeout=.2):break
                        except OSError:
                            if time.monotonic()>deadline:raise ValueError('Wi-Fi setup did not become available')
                            time.sleep(.1)
                    self.close()
                    self.monitor.connected=False
                    self.monitor.message('USB acquisition paused for Wi-Fi setup. Reset session reconnects if setup is cancelled.')
                    self.monitor.publish()
                    return
                host=str(ipaddress.IPv4Address(value))
                self.connection_factory=lambda:(Connection(host=host),'Wi-Fi '+host)
                self.fallback_factory=None
                self.reset();return
            if name=='reset':self.reset();return
            if not self.worker.is_alive():
                if name not in ('released','skip_recovery'):
                    raise ValueError('Recorder stopped. Use Reset session to reconnect')
                self.monitor.act(name,value);self.monitor.drain_events();self.monitor.publish()
            else:self.monitor.commands.put({'name':name,'value':value})

    def close(self):
        self.monitor.closed.set();self.worker.join(timeout=3)

class LocalDashboardServer(ThreadingHTTPServer):
    allow_reuse_address=sys.platform!='win32'

    def server_bind(self):
        if sys.platform=='win32':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

def serve(monitor,port,token,sessions=None):
    if sessions:sessions.http_port=port;sessions.http_token=token
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def respond(self,status,data,kind='application/json'):
            body=data.encode('utf-8')
            self.send_response(status); self.send_header('Content-Type',kind)
            self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path=='/':
                html=Path(__file__).with_name('dashboard.html').read_text(encoding='utf-8').replace('__TOKEN__',token)
                self.respond(200,html,'text/html; charset=utf-8')
            elif self.path=='/state':
                current=sessions.monitor if sessions else monitor
                with current.view_lock: data=json.dumps(current.view,allow_nan=False)
                self.respond(200,data)
            else: self.respond(404,'{}')
        def do_POST(self):
            if self.path!='/command' or self.headers.get('X-RTF-Token')!=token:
                self.respond(403,'{}'); return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<1024: raise ValueError('Invalid request size')
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict) or data.get('name') not in ('calibrate','arm','deflate','pressure','released','stop','skip_recovery','reset','wifi_setup','connect_wifi','remove_calibration','clear_calibrations','use_calibration_history'):
                    raise ValueError('Invalid command')
                if sessions:sessions.dispatch(data['name'],data.get('value'))
                else:
                    if monitor.failed or data['name']=='reset':raise ValueError('Reset is available in the live monitor launcher')
                    monitor.commands.put(data)
                self.respond(200,'{"queued":true}')
            except (ValueError,TypeError) as exc: self.respond(400,json.dumps({'error':str(exc)}))
    server=LocalDashboardServer(('127.0.0.1',port),Handler)
    def attach_sessions(manager):
        nonlocal sessions,monitor
        sessions=manager;monitor=manager.monitor
        manager.http_port=server.server_address[1];manager.http_token=token
    server.attach_sessions=attach_sessions
    return server

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    transport=parser.add_mutually_exclusive_group(required=True)
    transport.add_argument('--wifi',nargs='?',const='192.168.4.1')
    transport.add_argument('--usb',metavar='COM_PORT')
    transport.add_argument('--auto',metavar='WIFI_HOST',help='Prefer USB; use this Wi-Fi host if USB is unavailable at startup')
    parser.add_argument('--sensitive',action='store_true',help='Experimental low-amplitude candidate mode; not greater accuracy')
    parser.add_argument('--site',choices=('upper_arm','forearm'),default='upper_arm',help='Actual sensor location; recalibrate after repositioning')
    parser.add_argument('--http-port',type=int,default=8766)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'Data')
    args=parser.parse_args()
    avoid_usb=False
    def fallback_factory():
        nonlocal avoid_usb
        avoid_usb=True
        return Connection(host=args.auto),'Wi-Fi '+args.auto
    def connection_factory():
        if args.auto:
            if avoid_usb:return fallback_factory()
            return choose_connection(args.auto)
        return Connection(host=args.wifi,port=args.usb),('Wi-Fi '+args.wifi if args.wifi else 'USB '+args.usb)
    def directory_factory():return args.data_dir/('session_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    # Reserve the dashboard before opening USB/TCP. A second launcher must not
    # touch the recorder before discovering that an existing app owns the port.
    server=serve(None,args.http_port,secrets.token_urlsafe(24))
    sessions=None
    try:
        sessions=SessionManager(connection_factory,directory_factory,
                                fallback_factory=fallback_factory if args.auto else None,
                                sensitive=args.sensitive,site=args.site,three=True)
        server.attach_sessions(sessions)
        print('Logs:',sessions.monitor.directory)
        print(f'Dashboard: http://127.0.0.1:{args.http_port}')
        if not args.no_browser: webbrowser.open(f'http://127.0.0.1:{args.http_port}')
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt: pass
    finally:
        if sessions:sessions.close()
        server.server_close()
        print('Stopped. Fully release the manual cuff.')

if __name__=='__main__':
    try: main()
    except Exception as exc: raise SystemExit(f'Cannot start: {exc}. Keep cuff fully deflated.')
