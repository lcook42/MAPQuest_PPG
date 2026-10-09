"""Live passive acquisition viewer and loss-preserving CSV/raw-log writer."""
import argparse
import csv
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import queue
import threading
import time
from records import FIELDS, OrderCheck, parse, synthetic

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', help='e.g. COM3; close other serial monitors first')
    parser.add_argument('--simulate', action='store_true')
    parser.add_argument('--headless', action='store_true', help='Instant simulation to files; no GUI')
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--out', default='session.csv')
    args = parser.parse_args()
    if bool(args.port) == args.simulate or (args.headless and not args.simulate):
        parser.error('Choose --port COMx or --simulate; --headless requires simulation')
    if not 1 <= args.seconds <= 3600:
        parser.error('--seconds must be between 1 and 3600')
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = path.with_suffix('.serial.log')
    if path.exists() or raw_path.exists():
        parser.error('Output exists: use a new filename')
    order = OrderCheck()
    messages = queue.SimpleQueue()  # Reader never waits for graph redraw.
    halt = threading.Event()
    serial_port = None
    if args.port:
        import serial
        serial_port = serial.Serial(args.port, 115200, timeout=0.2, write_timeout=1)
    with path.open('x', newline='', encoding='utf-8') as output, raw_path.open('x', encoding='utf-8') as raw:
        writer = csv.DictWriter(output, fieldnames=['host_received_utc'] + FIELDS)
        writer.writeheader()
        raw.write('# SIMULATED — NOT PHYSIOLOGY\n' if args.simulate else '# Passive bench session\n')
        def receive(line, stamp):
            raw.write(f'{stamp} {line}\n')
            try:
                row = parse(line)
            except ValueError as exc:
                return None, f'Invalid row retained in raw log: {exc}'
            if row is None:
                return None, line
            order.accept(row)  # Reset is fatal; do not splice clock domains.
            writer.writerow({'host_received_utc': stamp, **row})
            return row, ''
        if args.headless:
            for line in synthetic(args.seconds):
                receive(line, datetime.now(timezone.utc).isoformat())
            print(f'SIMULATION saved to {path}; no measured data or MAP values.')
            return
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation
        from matplotlib.widgets import Button, TextBox
        fig, (ax_p, ax_c) = plt.subplots(2, 1, sharex=True, figsize=(11, 7))
        fig.subplots_adjust(bottom=.26, top=.88, hspace=.18)
        fig.suptitle('SIMULATION — arbitrary signals, NOT patient physiology' if args.simulate
                     else 'Passive bench logger — pneumatic pressure is NOT MAP')
        ax_p.set_ylabel('Raw optical ADC counts'); ax_c.set_ylabel('Gauge pressure (mmHg)')
        ax_c.set_xlabel('Device acquisition-service time (s)')
        p = deque(maxlen=3000); c = deque(maxlen=1000)
        lines = [ax_p.plot([], [], label=label)[0] for label in ('RED', 'IR', 'GREEN')]
        pressure_line, = ax_c.plot([], [], color='black', label='Pneumatic pressure')
        ax_p.legend(loc='upper right'); ax_c.legend(loc='upper right')
        status = fig.text(.08, .20, 'Vent port → ZERO → set gain/LED if needed → START. STOP does not vent.', fontsize=9)
        command_box = TextBox(fig.add_axes([.15, .10, .48, .05]), 'Command ', initial='MARK FLOW_SEEN')
        buttons = []
        def send(command):
            if args.simulate:
                status.set_text('Simulation ignores hardware commands; press close to finish.'); return
            try:
                serial_port.write((command.strip() + '\n').encode('ascii'))
                status.set_text('Sent ' + command + '; read board reply below. STOP does not vent.')
            except (UnicodeError, OSError) as exc:
                status.set_text(str(exc))
        for i, label in enumerate(('ZERO', 'START', 'STOP', 'SEND')):
            button = Button(fig.add_axes([.08 + i*.20, .03, .16, .045]), label)
            button.on_clicked(lambda event, name=label: send(command_box.text if name == 'SEND' else name))
            buttons.append(button)
        def reader():
            try:
                if args.simulate:
                    start = time.monotonic()
                    for line in synthetic(args.seconds):
                        target = int(line.split(',')[3]) / 1e6
                        if halt.wait(max(0, start + target - time.monotonic())): break
                        messages.put((line, datetime.now(timezone.utc).isoformat()))
                    messages.put(('# Simulation complete. Close window to finish.', ''))
                else:
                    pending = b''
                    while not halt.is_set():
                        pending += serial_port.read(max(1, serial_port.in_waiting))
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            messages.put((line.decode('utf-8', errors='replace').strip(), datetime.now(timezone.utc).isoformat()))
                        if len(pending) > 4096:
                            raise ValueError('Serial frame exceeds 4096 bytes; check firmware/port')
            except Exception as exc:
                messages.put(('# Serial reader error: ' + str(exc), ''))
                halt.set()
        thread = threading.Thread(target=reader, daemon=True); thread.start()
        fatal = False
        def drain():
            nonlocal fatal
            while not messages.empty():
                line, stamp = messages.get()
                try:
                    row, notice = receive(line, stamp)
                except ValueError as exc:
                    fatal = True; halt.set(); status.set_text(str(exc)); break
                if notice:
                    print(notice)
                    if notice.strip(): status.set_text(notice[:150])
                if row:
                    if row['kind'] == 'P': p.append(row)
                    elif row['kind'] == 'C': c.append(row)
                    else: status.set_text('Marker: ' + row['label'])
            output.flush(); raw.flush()
        def update(_):
            if fatal: return
            drain()
            for j, line in enumerate(lines):
                line.set_data([r['t_end_us']/1e6 for r in p], [r[('a','b','c')[j]] if r['flags']==0 else float('nan') for r in p])
            pressure_line.set_data([r['t_end_us']/1e6 for r in c], [r['a'] if r['flags']==0 else float('nan') for r in c])
            for ax in (ax_p, ax_c): ax.relim(); ax.autoscale_view()
            times = [r['t_end_us']/1e6 for r in list(p)[-1:] + list(c)[-1:]]
            if times: ax_c.set_xlim(max(0, max(times)-30), max(30, max(times)))
        animation = FuncAnimation(fig, update, interval=100, cache_frame_data=False)
        try:
            plt.show()
        finally:
            if serial_port and not fatal:
                try:
                    serial_port.write(b'STOP\n'); time.sleep(.3)
                except OSError: pass
            halt.set(); thread.join(timeout=1)
            if not fatal: drain()
            if serial_port: serial_port.close()
    print(f'Saved {path} and {raw_path}. Manually release pneumatic pressure.')

if __name__ == '__main__': main()
