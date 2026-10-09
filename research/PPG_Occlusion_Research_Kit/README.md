# PPG + occlusion-pressure research build kit

Start with **[BUILD_GUIDE.md](BUILD_GUIDE.md)**. It contains the BOM, wiring, numbered build steps, pressure-calibration procedure and the path to supervised clinical validation.

This kit makes a **passive bench data-acquisition system**, not a validated blood-pressure device. It records MAX30101 optical signals and pneumatic pressure. No automatic occlusion, patient safety controller or MAP estimator is provided. An optical baseline without an external reference remains a relative signal.

Included:

- `firmware/`: two PlatformIO pressure profiles for the XIAO ESP32-S3.
- `arduino/Passive_PPG_Pressure/`: matching Arduino IDE sketch, default absolute-pressure profile.
- `tools/acquire.py`: continuous live plots, commands, event markers, CSV and complete received serial log.
- `tools/analyze.py`: offline plots and pneumatic pressure near operator markers, explicitly **not MAP**.
- `tools/calibrate.py`: bench pressure-gain fit and independent residual checks.
- `templates/`: session notes and calibration input example.
- `examples/`: synthetic demonstration and plot, not patient data.
- `tests/`, `VALIDATION.md`: software checks and remaining hardware checks.

Open `PPG_Occlusion_Research.code-workspace` in VS Code. PlatformIO installs firmware libraries from `platformio.ini`. Python tools require a separate environment:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r tools\requirements.txt
.\.venv\Scripts\python.exe tools\acquire.py --simulate --out recordings\simulation.csv
```

For hardware, build/upload the correct firmware profile first. Close PlatformIO's Serial Monitor before running:

```powershell
.\.venv\Scripts\python.exe tools\acquire.py --port COM3 --out recordings\bench_001.csv
```

Replace `COM3` with your board's port. Physically vent the pressure sensor, click **ZERO**, wait for `ZERO set`, then click **START**. **STOP stops recording only; release pressure manually.** Firmware `GAIN` and `LED` commands go through the command box and SEND while idle. See the guide before assembling pneumatics.

This is a separate project from the earlier guided/reference-calibrated and no-reference MAX30101 projects. Its purpose is to collect the additional pressure-and-flow evidence needed to develop an occlusion method.
