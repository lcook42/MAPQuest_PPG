# Recorder transport recovery — r3.1 update

During the final live check, the user identified a detached sensor power wire as
the cause of an I2C_READ fault. Secure that connection before resetting. Reset
reinitializes the sensor, but it cannot correct a disconnected wire.

The message **Invalid bytes in recorder stream** concerns damaged protocol data,
not weak PPG or a failed physiological baseline. The monitor stops the session so
those samples cannot establish suppression or pulse return.

Use **Reset session**. In automatic mode, a reset after a failed USB session now
tries your saved Wi-Fi device address and starts an entirely new recording and
calibration. The dashboard names the active transport. Wi-Fi remains selected for
the rest of that app run; restarting the app tries USB first again.

If a cuff phase had been active, physically release the cuff and acknowledge
**Cuff is fully released** before reset. The monitor does not change transports
or reuse a baseline during an active trial.

The firmware now also performs a MAX30101 software reset and full configuration
at the start of each new recording. Previously, a fresh host session only cleared
the FIFO and selected LED slots; it could retain an unexpected sensor register
configuration. Configuration faults during acquisition still stop the recorder
and now log the observed register values for diagnosis.

USB port opening leaves DTR/RTS inactive. The host ignores boot chatter only before
a compatible firmware handshake. Any invalid bytes after handshake remain an error.

After this update, independent 30-second USB and Wi-Fi checks each received 2,983
three-channel samples, with zero corrupted lines, sequence gaps, or sensor faults.
These short checks do not identify the physical cause of earlier intermittent
USB corruption. If it recurs on USB, the fresh-session Wi-Fi fallback avoids that
data path; persistent faults on both connections still need hardware investigation.

See [validation details](VALIDATION_R3.md) and [calibration and controls](THREE_CHANNEL_RTF.md).
