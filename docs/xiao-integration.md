# XIAO fork integration

Prepared October 9, 2026 from upstream `akkhilmorkonda/MAPQuest_PPG` commit `767cc3b610cd0116227c23c47814dc9244689f37`.

No teammate implementation was used in the new versions. The upstream firmware, host and tests remain unchanged and separate in the fork tree/history. The new projects do not import, link or call upstream code. `xiao-source-provenance.json` records hashes checked against the earlier projects created here.

The additions are independent runnable projects in one fork, rather than two copies of the entire repository. Each optical mode has its own `platformio.ini`, `src/main.cpp`, README and matching Arduino sketch. Open the root workspace and select the appropriate PlatformIO project before uploading. Only one firmware can run on a given board at a time.

| Project | Hardware | Reference input | Result |
|---|---|---|---|
| Upstream `firmware/mapquest_ppg` | Nano ESP32, MAX30101, optional HX710B | Pressure-instrument calibration for pressure path | Experimental cuff/optical onset analysis; synthetic analysis tests are not clinical validation |
| `xiao/MAX30101_Guided_MAP` | XIAO ESP32-S3, MAX30101 | Patient/session reference MAP calibration points | Research regression predictions in mmHg |
| `xiao/MAX30101_NoReference` | XIAO ESP32-S3, MAX30101 | No MAP inputs; optical baseline only | Dimensionless relative optical distance/feature changes, no absolute MAP |
| `research/PPG_Occlusion_Research_Kit` | XIAO, MAX30101, supported MPRLS pressure breakout | Bench pressure zero/gain calibration; no patient MAP prompt | Raw PPG, measured pneumatic pressure and operator markers, no MAP estimator |

The upstream return-to-flow hypothesis and guided optical regression are distinct approaches. Adding the XIAO projects does not establish that either is accurate for a non-pulsatile patient. The no-reference optical tracker must not be presented as an uncalibrated absolute pressure device.

## Wiring and software pairing

XIAO projects use SDA GPIO5/D4 and SCL GPIO6/D5 with 3.3 V-compatible breakouts. Upstream Nano pin assignments differ. The passive pressure kit's default profile uses Adafruit MPRLS #3965; the upstream HX710B is not supported by that kit. Follow each project's own wiring and exact pressure profile.

Use `xiao/tools/serial_plot.py` with the two optical projects and their `LIVEPLOT` command. Use `research/PPG_Occlusion_Research_Kit/tools/acquire.py` with the passive kit. Upstream `host/logger.py` is for upstream firmware. CSV schemas and command protocols are not interchangeable.

Original supplied Arduino source is retained under `xiao/original`, with its source hash. Existing source provenance and README limitations are preserved. No new license is imposed on upstream or user-supplied code; this upstream snapshot does not include a license file.

## Validation

See `xiao/VALIDATION.md` and the research kit's `VALIDATION.md` for earlier build/test evidence and limitations. Current integration checks are recorded in `docs/xiao-integration-validation.md`. No hardware upload or human test is performed as part of this GitHub integration.
