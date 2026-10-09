# Verification and remaining checks

Prepared 2026-10-07. This is software verification, not measurement or clinical validation.

## Completed

- Both XIAO ESP32-S3 PlatformIO environments compiled successfully: `bench_absolute` and `bench_gauge300`, Espressif platform 6.9.0 / Arduino ESP32 2.0.17.
- SparkFun MAX3010x 1.1.2 and Adafruit MPRLS 1.2.2 used. Resolved dependencies: BusIO 1.17.4 and Unified Sensor 1.1.15.
- Six automated tests passed: pressure-fault preservation, malformed optical/time rejection, reset detection, ordered synthetic recording/marker association, rejection of pressure interpolation across a fault, and pressure-gain fit/zero-span rejection.
- Headless synthetic acquisition produced 2,503 ordered rows; offline analysis produced the included plot/report. The final marker correctly has no following pressure sample and receives no interpolated result.
- Example instrument-calibration JSON processed successfully. Its exact fit is arithmetic on invented numbers, not sensor performance.
- Arduino sketch source matches PlatformIO source. Arduino IDE compilation was not separately tested.

The upstream SparkFun library emits an `I2C_BUFFER_LENGTH` redefinition warning against the ESP32 Wire header; both builds succeeded. No library internals were modified.

## Not performed

Firmware upload; actual sensor communication; pressure calibration; live hardware capture; optical fixture tests; pressure leak/hysteresis/temperature tests; timing characterization; phantom experiments; patient testing; clinical validation or electrical-safety assessment. The GUI's physical serial connection and operator workflow require bench verification. No MAP accuracy claim is made.

## Suggested bench acceptance record

Before moving beyond the bench, document at least:

1. Correct profile, device addresses and sustained sensor communication.
2. Atmospheric zero, traceable pressure reference, span/linearity/hysteresis and independent check errors.
3. Drift across intended session duration and temperatures, including return to zero.
4. Actual optical/pressure sample rates, conversion intervals, backlog/fault rates and reset behavior.
5. Known pneumatic changes and response delay at the sensor/reference.
6. Dark, illumination, fixture/contact and remounting experiments without clipping.
7. Marker/independent-flow timing uncertainty and a predeclared handling rule for missing data.
8. Full raw logs, metadata, excluded sessions and measurement-failure counts.

Software tests from the kit root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tools\acquire.py --simulate --headless --out recordings\new_simulation.csv
.\.venv\Scripts\python.exe tools\analyze.py recordings\new_simulation.csv --out recordings\new_simulation_review.png
```
