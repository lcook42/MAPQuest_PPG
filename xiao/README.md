# MAX30101 + XIAO ESP32-S3: VS Code projects

Two independent PlatformIO projects, plus Arduino sketches and an optional desktop graph helper. Open either project folder in VS Code with the PlatformIO IDE extension. Each contains `platformio.ini`, `src/main.cpp`, its own README and an Arduino IDE sketch.

| Project | Setup | Output |
|---|---|---|
| `MAX30101_Guided_MAP` | 12 reference MAP points, then 3 validation points | Subject-specific research MAP prediction in mmHg; original behavior preserved |
| `MAX30101_NoReference` | 6 accepted optical baseline windows; no pressure inputs | Dimensionless optical distance and signed feature changes; experimental, relative, uncalibrated |

The reference project preserves the supplied MAP workflow and adds a continuous visualization command. Both runnable Arduino sketches match their PlatformIO sources. An unchanged copy of the uploaded sketch is preserved in `original/MAX30101_Guided_MAP/`. Both modes are research prototypes. A firmware `COUNT ... YES` means that its configured rules passed; it does not establish pressure accuracy, clinical validity or suitability for LVAD care. No human or hardware validation was performed here.

## Wiring

Use a MAX30101 **breakout** compatible with 3.3 V power and I²C logic. A bare MAX30101 IC requires its specified separate supplies; do not treat it as a breakout.

| MAX30101 breakout | XIAO ESP32-S3 |
|---|---|
| SDA | D4 / GPIO5 |
| SCL | D5 / GPIO6 |
| GND | GND |
| Power input rated for 3.3 V | 3V3 |

Sensor address: `0x57`. The MAX30105-named SparkFun class is also used for MAX30101. This code requires red, IR and green channels; a MAX30102 is not a drop-in substitute for this workflow.

## Build and run in VS Code

1. Install VS Code and the PlatformIO IDE extension.
2. Open **one** project folder, not `src` or just the package root.
3. Connect the XIAO by a data-capable USB cable. Click PlatformIO **Build**, then **Upload**.
4. Open PlatformIO **Serial Monitor**, 115200 baud. Commands and prompt replies must end with a newline. The configuration uses `send_on_enter` so typing is buffered until Enter; Enter alone answers a readiness prompt.
5. Enter `HELP`, then follow the project README. Capture windows take about six seconds, with additional time for retries and tuning. These workflows block until prompt input arrives. There is no in-workflow cancel command; reset returns to the menu.

Equivalent terminal commands, from the selected project folder:

```text
pio run
pio run --target upload
pio device monitor --baud 115200
```

Automatic port detection is enabled. If needed, add `upload_port = COM5` and `monitor_port = COM5` under the environment, using your actual port. If upload cannot connect, hold BOOT, tap RESET, release BOOT, then upload; reopen the monitor on the application USB port after reset. USB CDC is explicitly enabled in both projects.

The build pins Espressif32 platform 6.9.0 (Arduino ESP32 2.0.17) and SparkFun library 1.1.2. The initial build downloads dependencies. The board ID is documented by [PlatformIO](https://docs.platformio.org/en/latest/boards/espressif32/seeed_xiao_esp32s3.html); the sensor API is documented in [SparkFun's MAX30101 guide](https://learn.sparkfun.com/tutorials/sparkfun-photodetector-max30101-hookup-guide/all).

## Graphs entirely from VS Code

The PlatformIO monitor displays text; it does not itself render the original Arduino Serial Plotter traces. This package includes `tools/serial_plot.py` to capture the same labeled values and plot them on the desktop.

Install Python and run these commands from the package root:

```text
python -m pip install -r tools/requirements.txt
python tools/serial_plot.py --port COM5 --command LIVEPLOT --csv live_ppg.csv
python tools/serial_plot.py --port COM5 --command PLOT --csv ppg.csv
python tools/serial_plot.py --port COM5 --command CALPLOT --csv calibration.csv
```

Close the serial monitor first; only one program can own the port. Keep the sensor still and ensure the firmware is at its command menu before starting the helper. It sends the command and answers the PLOT readiness prompt, captures the finite stream, closes the port, then opens a graph. `CALPLOT` is only meaningful in the reference project after a saved calibration. Training predictions and held-out validation predictions are labeled in the graph's horizontal axis; the training fit is not independent validation. Graphing requires a graphical desktop and has not been tested against connected hardware here.

You can also use Arduino Serial Plotter at 115200 after issuing `PLOT` or `CALPLOT`. Close one serial client before opening another. The original five-second handover delay is retained.

## Arduino IDE

Open the `.ino` inside the selected project's `arduino/<sketch-name>/` folder. Install the Espressif ESP32 board package (2.0.17 for the tested PlatformIO core equivalent) and SparkFun MAX3010x library 1.1.2. Select XIAO ESP32S3, enable USB CDC on boot, and upload. Do not add the PlatformIO `src/main.cpp` to the Arduino sketch: it would duplicate setup, loop and globals.

See `VALIDATION.md` for build results and known limits.

## Continuous plot menu option

Both firmware menus now include `LIVEPLOT` (alias `CONTINUOUS`). This streams live normalized red/IR/green PPG until you send `STOP` followed by Enter, then returns to the command menu. It uses current LED settings and does not retune, change a saved baseline/model, or produce an accepted MAP measurement. If the signal is poor, stop first, adjust placement, then use RETUNE (and START afterward in no-reference mode).

For a live graph from VS Code, close the serial monitor and run `python tools/serial_plot.py --port COM5 --command LIVEPLOT`. The graph shows the most recent 1,000 samples (about 10 seconds). Click **Stop / Menu**, close the window or press Ctrl+C to send STOP. Optional `--csv live_ppg.csv` records the entire stream to disk without growing the graph's memory indefinitely. Horizontal times are desktop reception times, not synchronized sensor timestamps. With Arduino Serial Plotter, issue LIVEPLOT and switch within the five-second handover delay; send STOP with a newline to finish.

The live filter uses the same nominal 0.35 Hz high-pass / 5 Hz low-pass settings as the captured PLOT mode, with a rolling DC normalization and a one-second startup warmup. It is a visualization: contact, saturation and motion can still appear in the traces without a rejection label. Disconnected/stalled sensors produce no rows; STOP remains polled by the firmware, and the desktop view closes after a no-data timeout.
