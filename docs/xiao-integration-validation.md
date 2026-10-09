# Integration verification — October 9, 2026

## New projects

| Check | Result |
|---|---|
| Guided reference project: PlatformIO XIAO ESP32-S3 build | PASS: 34,744 bytes RAM; 326,325 bytes flash |
| No-reference project: PlatformIO XIAO ESP32-S3 build | PASS: 33,456 bytes RAM; 310,249 bytes flash |
| Desktop optical-plot parser tests | 3 passed |
| Passive pressure-kit acquisition/calibration/marker tests | 6 passed |
| Source independence | All added .cpp/.ino/.py/.h files match earlier chat-created/user-supplied project files byte-for-byte; hashes in xiao-source-provenance.json |
| Passive-kit two pressure-profile builds | Source unchanged from the successful October 7 builds; recorded in its VALIDATION.md |

Platform: espressif32@6.9.0; Arduino ESP32 2.0.17; SparkFun MAX3010x@1.1.2. The upstream library emits an I2C_BUFFER_LENGTH redefinition warning; both new optical builds succeeded without modifying it.

The teammate's upstream implementation was not used or executed to validate our versions. Its firmware, host and tests are untouched. The fork relationship preserves upstream history; it does not make the new implementations derived from upstream code.

No board upload, live hardware acquisition, independent pressure accuracy test or clinical validation was performed for this integration. No-reference output is dimensionless relative optics, never absolute MAP. Guided reference predictions are research outputs.
