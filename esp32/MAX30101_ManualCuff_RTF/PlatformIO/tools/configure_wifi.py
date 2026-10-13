"""Local-only USB Wi-Fi provisioning. Never log or save the router password on PC."""
import argparse,importlib,json,secrets,threading,time,socket,subprocess,sys,webbrowser
import urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
CONFIG=ROOT/'Data/wifi-device.json'

def main():
    try:
        serial=importlib.import_module('serial')
        list_ports=importlib.import_module('serial.tools.list_ports')
    except ImportError as exc:
        raise SystemExit('PySerial is required. Install it with: python -m pip install pyserial') from exc
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port');p.add_argument('--ssid',default='');p.add_argument('--no-browser',action='store_true')
    p.add_argument('--site',choices=('forearm','upper_arm'),default='forearm')
    p.add_argument('--monitor-port',type=int,default=8766)
    p.add_argument('--monitor-token')
    args=p.parse_args(); token=secrets.token_urlsafe(24)
    state={'status':'Enter your 2.4 GHz home Wi-Fi details. Keep cuff deflated.','busy':False,'connected':False}
    def connect(ssid,password,port):
        try:
            with serial.Serial(port,115200,timeout=.1,write_timeout=2) as link:
                time.sleep(3);link.reset_input_buffer()
                link.write(('WIFI,'+ssid.encode().hex()+','+password.encode().hex()+'\n').encode())
                password=None
                deadline=time.monotonic()+45;poll=0;ip=None
                while time.monotonic()<deadline:
                    if time.monotonic()-poll>1:link.write(b'NET\n');poll=time.monotonic()
                    row=link.readline().decode(errors='replace').strip()
                    if row.startswith('ERROR,'):raise RuntimeError('Device refused setup. Stop monitoring and keep cuff deflated.')
                    if row.startswith('NET,') and row!='NET,DISCONNECTED':
                        ip=row.split(',',1)[1];socket.inet_aton(ip);break
                if not ip:raise RuntimeError('Could not join. Check password and that this network has 2.4 GHz enabled; then retry.')
            # Verify the laptop can reach the actual recorder before saving its address.
            deadline=time.monotonic()+20
            while True:
                try:
                    with socket.create_connection((ip,8765),timeout=3) as probe:
                        probe.settimeout(3);probe.sendall(b'HELLO\n');reply=b''
                        while b'\n' not in reply and len(reply)<512:
                            part=probe.recv(256)
                            if not part:raise OSError('Disconnected')
                            reply+=part
                        if b'ESP32_DEVKIT_V1' not in reply:raise OSError('Identity not ready')
                    break
                except OSError:
                    if time.monotonic()>deadline:raise RuntimeError('ESP32 joined Wi-Fi but laptop cannot reach it. Check router client isolation or retry setup.')
                    time.sleep(1)
            CONFIG.parent.mkdir(exist_ok=True)
            CONFIG.write_text(json.dumps({'host':ip,'hostname':'rtf-esp32.local'},indent=2))
            if args.monitor_token:
                request=urllib.request.Request(f'http://127.0.0.1:{args.monitor_port}/command',
                    data=json.dumps({'name':'connect_wifi','value':ip}).encode(),
                    headers={'Content-Type':'application/json','X-RTF-Token':args.monitor_token})
                with urllib.request.urlopen(request,timeout=15) as response:response.read()
            else:
                subprocess.Popen([sys.executable,str(Path(__file__).with_name('monitor.py')),'--wifi',ip,'--site',args.site],
                                 cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            state.update(status='Connected. Opening the live Wi-Fi dashboard.',connected=True,host=ip,
                         dashboard=f'http://127.0.0.1:{args.monitor_port}/')
        except Exception as exc:
            # Do not include serial payloads or credentials in errors.
            state['status']=str(exc) if isinstance(exc,RuntimeError) else 'Connection failed. Close Serial Monitor/USB dashboard and check the cable and network.'
        finally:state['busy']=False
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def reply(self,code,body,kind='application/json'):
            b=body.encode();self.send_response(code);self.send_header('Content-Type',kind)
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(b)))
            self.end_headers();self.wfile.write(b)
        def do_GET(self):
            if self.path=='/state':self.reply(200,json.dumps(state));return
            if self.path!='/':self.reply(404,'{}');return
            import html
            ports=list(list_ports.comports())
            options=''.join('<option '+('selected ' if x.device==args.port else '')+'value="'+html.escape(x.device)+'">'+html.escape(x.device+' — '+x.description)+'</option>' for x in ports)
            page='''<!doctype html><meta charset="utf-8"><title>ESP32 Home Wi-Fi Setup</title>
<style>body{font:18px system-ui;max-width:620px;margin:45px auto;padding:20px;background:#102030;color:#eee}label{display:block;margin:20px 0}input,select,button{display:block;font:inherit;padding:10px;width:95%;margin-top:8px}button{background:#367eac;color:white;border:0;cursor:pointer}</style>
<h1>Connect ESP32 to home Wi-Fi</h1><p>Your laptop stays on its normal internet connection. Keep the ESP32 connected by USB for this one-time setup and keep the cuff deflated.</p>
<p>The ESP32 needs a <b>2.4 GHz</b> network. The laptop may remain on the same router's 5 GHz network.</p>
<form id="f"><label>ESP32 USB port<select id="port">PORTS</select></label><label>Network name<input id="ssid" value="SSID" required maxlength="32"></label>
<label>Wi-Fi password<input id="pw" type="password" autocomplete="off" required minlength="8" maxlength="63"></label><button id="go">Connect and start monitoring</button></form>
<p>The password is sent locally over USB and stored on the ESP32. It is not written to this project's source or computer logs.</p><p id="status"></p>
<script>f.onsubmit=async e=>{e.preventDefault();go.disabled=true;try{let r=await fetch('/connect',{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Token':'TOKEN'},body:JSON.stringify({ssid:ssid.value,password:pw.value,port:port.value})});pw.value='';if(!r.ok)document.getElementById('status').textContent='Invalid details or setup already busy.';}catch(e){document.getElementById('status').textContent='Setup service unavailable';}};async function poll(){try{let s=await(await fetch('/state')).json();document.getElementById('status').textContent=s.status;go.disabled=s.busy||s.connected;}finally{setTimeout(poll,1000)}}poll();</script>'''
            page=page.replace('go.disabled=s.busy||s.connected;', 'go.disabled=s.busy||s.connected;if(s.connected&&s.dashboard)location.href=s.dashboard;')
            page=page.replace('PORTS',options).replace('SSID',html.escape(args.ssid,quote=True)).replace('TOKEN',token)
            page+=f'<p><a style="color:#9ddaff" href="http://127.0.0.1:{args.monitor_port}/">Back to monitor (Reset session reconnects USB if setup was cancelled)</a></p>'
            self.reply(200,page,'text/html; charset=utf-8')
        def do_POST(self):
            if self.path!='/connect' or self.headers.get('X-Setup-Token')!=token:self.reply(403,'{}');return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<2048:raise ValueError()
                d=json.loads(self.rfile.read(size));ssid=d['ssid'];password=d['password'];port=d['port']
                if not isinstance(ssid,str) or not isinstance(password,str):raise ValueError()
                if not 1<=len(ssid.encode())<=32 or not 8<=len(password.encode())<=63 or '\x00' in ssid+password:raise ValueError()
                if port not in [x.device for x in list_ports.comports()] or state['busy'] or state['connected']:raise ValueError()
                state.update(busy=True,status='Connecting ESP32… this can take up to 45 seconds.')
                threading.Thread(target=connect,args=(ssid,password,port),daemon=True).start()
                self.reply(200,'{}')
            except (ValueError,KeyError,TypeError):self.reply(400,'{}')
    server=ThreadingHTTPServer(('127.0.0.1',8768),Handler)
    print('Local Wi-Fi setup: http://127.0.0.1:8768',flush=True)
    if not args.no_browser:webbrowser.open('http://127.0.0.1:8768')
    server.serve_forever()
if __name__=='__main__':main()
