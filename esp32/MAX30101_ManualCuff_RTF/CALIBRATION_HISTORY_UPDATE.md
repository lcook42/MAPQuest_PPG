# Calibration history and failure help update

Host revision: `2026-10-12-r3-host-history1`. Firmware protocol remains `2026-10-12-r3-three`.

- GREEN optical-baseline deviation limit is now 40% in either direction. RED/IR remain 25%. Only the selected pair gates decisions. Drift within a window, jumps, clipping, AC suppression/return gates and cuff timers are unchanged.
- Three-channel calibration keeps completed settings from successful, failed and interrupted attempts in this session. Each retained attempt contributes its best pair score for each setting; repeated observations are averaged. The highest-ranked setting seeds the next brightness search. Up to three historical settings are added to the freshly generated candidates, with duplicates removed. Every setting still tests all three pairs using two new windows. Three new baseline windows are still required.
- Dashboard: Use retained attempts checkbox, Remove attempt and Clear calibration history. These affect the next search without resetting recording or the current baseline. Removal excludes data from ranking but preserves original records for audit. History is session-local; Reset session starts a new history.
- Brightness observations and scored settings are saved in calibration_history.json; original CHANNEL_QUALITY samples remain in replay.jsonl. A brightness-only failure has no pair score to rank; the next search starts from the default unless other scored settings are retained.
- Failure help uses rejected checks and pair feedback to suggest relevant contact, shielding, drift, placement or connection checks. Possible causes are not treated as proven diagnoses. A changed placement can make prior settings inappropriate; remove those attempts or disable history.
- Earlier audit fixes label the unused channel diagnostic-only, stop inactive-state error spam, reserve the HTTP port before opening acquisition, and save source snapshots/hashes for replay.

## Verification

87 unit/integration tests passed. This includes HTTP delivery of history controls, full pair search after an early success, fresh-data qualification after historical success, failed-attempt reuse, history removal, threshold checks and failure suggestions. Headless Chrome UI check passed: controls render, removal reaches the command endpoint and updates the row, GREEN 40% and failure guidance display, no JavaScript errors. UI check used a synthetic state, not the recorder.

## Loading the changes

Changes are saved in the active source and this curated source copy. They have not been loaded into the running monitor. A new Python process is required; refreshing the browser or Reset session does not reload code. No firmware upload is needed for these host-only changes.

After fully releasing the cuff and closing the old monitor process, run from this project folder:

```powershell
& "$env:USERPROFILE/.platformio/penv/Scripts/python.exe" PlatformIO/tools/monitor.py --usb COM4 --site upper_arm
```

Use the actual USB port if different. The existing process must release port 8766 first. Connect wirelessly in the dashboard opens the Wi-Fi setup flow.

Historical companion documents may describe older thresholds. This update and the latest concise notes describe the current host revision. No improvement in physiological measurement accuracy is established by the software tests.

## Exact stream-fault evidence (host-capture1)

The research audit found that replacement decoding discarded the original invalid byte values. The host now writes RX_FAULT_BYTES records containing the complete offending line as hexadecimal, byte count, reason and transport before decoding/rejection. Framing-overflow bytes are also retained. Normal ASCII traffic does not create extra fault-byte records. Existing rejection rules and detector calculations are unchanged. Earlier recordings cannot recover the original corrupted bytes from replacement characters.

Host revision: 2026-10-12-r3-host-capture1; detector revision remains host-history1. All 90 tests passed, including split invalid lines, unchanged rejection, normal ASCII behavior and framing overflow. A monitor process restart is required to load this logging fix.
