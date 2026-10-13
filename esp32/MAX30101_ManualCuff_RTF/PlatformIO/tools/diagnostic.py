"""Three-channel optical diagnostic recorder. No cuff test or baseline gate."""
import csv,json,time,threading,secrets,argparse,webbrowser
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import numpy as np
from serial.tools import list_ports
from monitor import Connection,ROOT

def choose_connection(host):
    ports=[p for p in list_ports.comports() if (p.vid,p.pid)==(0x10c4,0xea60)]
    if len(ports)>1:raise RuntimeError('Multiple CP210x boards connected; disconnect other boards before auto-selection.')
    if ports:
        try:return Connection(port=ports[0].device),'USB '+ports[0].device
        except OSError:pass
    return Connection(host=host),'Wi-Fi '+host

def parse(line):
    v=line.split(',')
    if len(v)!=10 or v[0]!='D':raise ValueError('Invalid three-channel sample')
    q=list(map(int,v[1:]))
    if any(x<0 or x>0xffffffff for x in q[:3]) or any(x<0 or x>262143 for x in q[3:6]) or any(x<1 or x>200 for x in q[6:]):raise ValueError('Sample out of range')
    return q

def main():
    p=argparse.ArgumentParser();p.add_argument('--host');p.add_argument('--no-browser',action='store_true');args=p.parse_args()
    saved=ROOT/'Data/wifi-device.json'
    host=args.host or (json.loads(saved.read_text())['host'] if saved.exists() else '192.168.4.1')
    link,transport=choose_connection(host)
    folder=ROOT/'Data'/('DIAGNOSTIC_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    folder.mkdir(parents=True);token=secrets.token_urlsafe(24)
    state={'connected':False,'transport':transport,'status':'Starting three-channel recorder','rows':[],'metrics':[],'leds':[32,32,32],'logs':str(folder)}
    import queue
    commands=queue.Queue();closed=threading.Event()
    def worker():
        last=time.monotonic();ping=0;previous=None;rows=[];started=False
        try:
            with (folder/'samples.csv').open('w',newline='') as f:
                writer=csv.writer(f);writer.writerow(['host_monotonic','seq','device_ms','epoch','red','ir','green','red_led','ir_led','green_led'])
                (folder/'session.json').write_text(json.dumps({'mode':'DIAGNOSTIC_ONLY','channels':['red','ir','green'],'transport':transport,'site':'forearm','cuff':'must remain deflated'},indent=2))
                link.send('HELLO')
                while not closed.is_set():
                    now=time.monotonic()
                    if now-ping>1:
                        if not started:link.send('HELLO')
                        link.send('PING');ping=now
                    if now-last>5:raise RuntimeError('Recorder stopped sending data. Reconnect and restart diagnostic mode.')
                    while not commands.empty():link.send(commands.get_nowait())
                    for line in link.read():
                        if started and not line.isascii():raise RuntimeError('Invalid bytes in recorder stream; restart diagnostic session')
                        if line.startswith('HELLO,') and not started:
                            link.send('STOP');link.send('DIAG');started=True
                        elif line=='ACK,DIAG,100,RED_IR_GREEN':
                            state.update(connected=True,status='Recording RED, IR and GREEN — diagnostic only');last=now
                        elif line.startswith(('FAULT,','ERROR,')):raise RuntimeError(line)
                        elif line.startswith('D,'):
                            q=parse(line);last=now
                            if previous and ((q[0]-previous[0])&0xffffffff)!=1:raise RuntimeError('Sample gap: recording stopped')
                            if previous and q[2]!=previous[2]:rows=[]
                            previous=q;writer.writerow([now,*q]);rows.append(q);rows=rows[-1000:]
                            state['rows']=[r[3:6] for r in rows];state['leds']=q[6:]
                            if q[0]%100==0 and len(rows)>=400:
                                a=np.array([r[3:6] for r in rows[-400:]],float);t=np.arange(400)
                                design=np.column_stack((np.ones(400),t));x=a-design@np.linalg.lstsq(design,a,rcond=None)[0]
                                state['metrics']=[{'channel':name,'dc':round(float(np.median(a[:,i])),1),'ac_rms':round(float(np.sqrt(np.mean(x[:,i]**2))),2),'clipped':bool(np.any((a[:,i]<500)|(a[:,i]>250000)))} for i,name in enumerate(('RED','IR','GREEN'))]
                                f.flush()
        except Exception as e:state.update(connected=False,status=str(e))
        finally:
            try:link.send('STOP');link.close()
            except Exception:pass
            state['connected']=False
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def respond(self,n,data,kind='application/json'):
            b=data.encode();self.send_response(n);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
        def do_GET(self):
            if self.path=='/state':self.respond(200,json.dumps(state));return
            if self.path!='/':self.respond(404,'{}');return
            self.respond(200,Path(__file__).with_name('diagnostic.html').read_text().replace('__TOKEN__',token),'text/html; charset=utf-8')
        def do_POST(self):
            if self.path!='/command' or self.headers.get('X-RTF-Token')!=token:self.respond(403,'{}');return
            try:
                n=int(self.headers.get('Content-Length',0))
                if not 0<n<512:raise ValueError()
                d=json.loads(self.rfile.read(n))
                if d.get('stop'):
                    state.update(connected=False,status='Recording stopped. Restart Start_Diagnostics.cmd for a new session.')
                    closed.set()
                else:
                    v=d['leds']
                    if not state['connected'] or len(v)!=3 or any(type(x)!=int or not 1<=x<=200 for x in v):raise ValueError()
                    commands.put('LED3,'+','.join(map(str,v)))
                self.respond(200,'{}')
            except (ValueError,KeyError,TypeError):self.respond(400,'{}')
    server=ThreadingHTTPServer(('127.0.0.1',8766),Handler)
    threading.Thread(target=worker,daemon=True).start()
    print('Diagnostic dashboard: http://127.0.0.1:8766 — '+transport,flush=True)
    if not args.no_browser:webbrowser.open('http://127.0.0.1:8766')
    try:server.serve_forever()
    finally:closed.set();server.server_close()
if __name__=='__main__':main()
