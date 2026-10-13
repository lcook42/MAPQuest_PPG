"""Windows-friendly setup, explicit upload, or Wi-Fi/USB monitoring launcher."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import json

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--setup',action='store_true')
    p.add_argument('--upload',action='store_true')
    p.add_argument('--usb',action='store_true')
    p.add_argument('--wifi',action='store_true',help='Start with the saved Wi-Fi address instead of USB')
    p.add_argument('--port')
    p.add_argument('--sensitive',action='store_true')
    p.add_argument('--site',choices=('upper_arm','forearm'),default='upper_arm')
    p.add_argument('--configure-wifi',action='store_true')
    p.add_argument('--host',help='Override ESP32 Wi-Fi address')
    args=p.parse_args()
    project=Path(__file__).resolve().parents[1]
    pio=Path.home()/'.platformio/penv/Scripts/platformio.exe'
    env=os.environ.copy(); env.pop('PLATFORMIO_CORE_DIR',None)
    if args.configure_wifi:
        subprocess.run([sys.executable,str(project/'tools/configure_wifi.py'),'--site',args.site],check=True)
        return
    if args.setup:
        subprocess.run([sys.executable,'-m','pip','install','-r',str(project/'tools/requirements.txt')],check=True)
        if not pio.exists(): raise SystemExit('Install PlatformIO IDE in VS Code, then run setup again.')
        subprocess.run([str(pio),'run','-d',str(project)],env=env,check=True)
        subprocess.run([sys.executable,'-m','unittest','discover','-s',str(project/'tools'),'-p','test_*.py'],check=True)
        print('Setup/build/tests complete. Firmware has NOT been uploaded.')
        return
    if args.upload or args.usb or not (args.wifi or args.host):
        # pyserial is installed by the setup command and may be unavailable
        # in the editor's analysis environment.
        from serial.tools import list_ports  # type: ignore[reportMissingImports]
        ports=list(list_ports.comports())
        for port in ports: print(f'{port.device}: {port.description}')
        selected=args.port
        boards=[v for v in ports if (v.vid,v.pid)==(0x10c4,0xea60)]
        if not selected and len(boards)==1:selected=boards[0].device
        if not selected:
            if not ports: raise SystemExit('No serial ports found. Connect ESP32 DevKit V1 by USB.')
            selected=input('Enter the ESP32 DevKit V1 COM port: ').strip()
        if selected.upper() not in {v.device.upper() for v in ports}: raise SystemExit('Port not in the connected port list.')
        if args.upload:
            if not pio.exists(): raise SystemExit('PlatformIO is missing; run Setup_Windows first.')
            print('Keep cuff fully deflated. Uploading ESP32 DevKit V1 firmware; close other serial monitors.')
            subprocess.run([str(pio),'run','-d',str(project),'-t','upload','--upload-port',selected],env=env,check=True)
            print('Upload complete. Use Start_USB, or connect laptop to RTF-ESP32 and use Start_WiFi.')
            return
        transport=['--usb',selected]
    else:
        config=project.parent/'Data/wifi-device.json'
        host=args.host
        if not host and config.exists():host=json.loads(config.read_text())['host']
        transport=['--wifi',host or '192.168.4.1']
    if args.sensitive: transport+=['--sensitive']
    transport+=['--site',args.site]
    subprocess.run([sys.executable,str(project/'tools/monitor.py'),*transport],check=True)

if __name__=='__main__':
    try: main()
    except (ImportError,OSError,subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Unable to proceed: {exc}. Run Setup_Windows while connected to the internet.')
