"""Display captured PLOT/CALPLOT data or a continuous LIVEPLOT view."""
import argparse
import csv
import re
import time
from pathlib import Path
from collections import deque


def live_view(device, csv_path=None):
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Button

    labels = ['RedPPG', 'IRPPG', 'GreenPPG']
    samples = deque(maxlen=1000)  # Bounded display, approximately 10 seconds.
    pending = bytearray()
    started = time.monotonic()
    last_sample = started
    fig, axis = plt.subplots()
    fig.subplots_adjust(bottom=0.2)
    lines = [axis.plot([], [], label=label)[0] for label in labels]
    axis.set_xlabel('Elapsed reception time (seconds)')
    axis.set_ylabel('Rolling normalized AC/DC × 1000')
    axis.set_title('Continuous PPG — visualization only')
    axis.legend()
    axis.grid(alpha=0.3)
    button = Button(fig.add_axes([0.4, 0.04, 0.2, 0.07]), 'Stop / Menu')
    button.on_clicked(lambda event: plt.close(fig))
    csv_file = None
    writer = None
    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_file = csv_path.open('w', newline='', encoding='utf-8')
        writer = csv.writer(csv_file)
        writer.writerow(['ReceptionSeconds'] + labels)

    def update(_):
        nonlocal last_sample
        try:
            pending.extend(device.read(min(device.in_waiting, 65536)))
            while b'\n' in pending:
                raw, _, remainder = pending.partition(b'\n')
                pending[:] = remainder
                line = raw.decode('utf-8', errors='replace').strip()
                row = parse_row(line)
                if row and all(label in row for label in labels):
                    last_sample = time.monotonic()
                    sample = [last_sample - started] + [row[label] for label in labels]
                    samples.append(sample)
                    if writer:
                        writer.writerow(sample)
                elif line:
                    print(line)
                    if 'LIVEPLOT_COMPLETE' in line:
                        plt.close(fig)
            if len(pending) > 65536:
                raise RuntimeError('Serial input has no complete lines.')
            if time.monotonic() - last_sample > (20 if not samples else 10):
                raise RuntimeError('No live samples received. Check sensor and firmware menu.')
            if samples:
                times = [sample[0] for sample in samples]
                for j, plot_line in enumerate(lines):
                    plot_line.set_data(times, [sample[j + 1] for sample in samples])
                axis.relim()
                axis.autoscale_view()
        except Exception as error:
            print(f'Live view stopped: {error}')
            plt.close(fig)
        return lines

    device.timeout = 0
    animation = FuncAnimation(fig, update, interval=50, cache_frame_data=False)
    try:
        plt.show()
    finally:
        if animation.event_source is not None:
            animation.event_source.stop()
        if csv_file:
            csv_file.close()

def parse_row(line):
    pairs = re.findall(r'(\w+):(-?\d+(?:\.\d+)?)', line)
    values = dict((k, float(v)) for k, v in pairs)
    if {'RedPPG', 'IRPPG', 'GreenPPG'} <= values.keys():
        return values
    if {'ReferenceMAP', 'PredictedMAP'} <= values.keys():
        return values
    return None


def main():
    import serial
    import matplotlib.pyplot as plt
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, help='e.g. COM5')
    parser.add_argument('--command', choices=['PLOT', 'CALPLOT', 'LIVEPLOT'], default='PLOT')
    parser.add_argument('--csv', type=Path, help='optional captured samples')
    args = parser.parse_args()
    rows = []
    # Firmware must be at its command menu, not inside a calibration prompt.
    with serial.Serial(args.port, 115200, timeout=1) as device:
        time.sleep(2)  # Allow USB reconnect/reset messages to settle.
        device.reset_input_buffer()
        device.write((args.command + '\n').encode('ascii'))
        if args.command == 'LIVEPLOT':
            try:
                live_view(device, args.csv)
            finally:
                device.write(b'STOP\n')
                device.flush()
            return
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            line = device.readline().decode('utf-8', errors='replace').strip()
            if not line:
                continue
            row = parse_row(line)
            if row is not None:
                rows.append(row)
            else:
                print(line)
            if 'press enter' in line.lower():
                device.write(b'\n')
            if 'PLOT_COMPLETE' in line:
                break
            if 'cancelled' in line.lower() or 'No calibration graph' in line or 'No reference-vs-prediction' in line:
                break
        else:
            raise SystemExit('Capture timed out. Return firmware to its command menu and retry.')
    if not rows:
        raise SystemExit('No graph samples captured. Check placement/calibration and command.')
    labels = list(rows[0])
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=labels)
            writer.writeheader()
            writer.writerows(rows)
    for label in labels:
        x = [i / 100 for i in range(len(rows))] if args.command == 'PLOT' else list(range(1, len(rows) + 1))
        plt.plot(x, [row[label] for row in rows], label=label, marker=None if args.command == 'PLOT' else 'o')
    plt.xlabel('Seconds after discarded first second' if args.command == 'PLOT' else 'Point (1–12 training, 13–15 validation)')
    plt.ylabel('Normalized AC/DC × 1000' if args.command == 'PLOT' else 'MAP (mmHg), research prototype')
    plt.title('Captured optical waveform' if args.command == 'PLOT' else 'Training fit and three held-out validation points')
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.show()


if __name__ == '__main__':
    main()
