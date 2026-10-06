"""Serial transport for the MAPQuest PPG firmware.

The firmware emits a ``#``-prefixed metadata header followed by CSV samples. We
parse the header rather than hardcoding sample rate or channel names, so
changing the firmware config needs no change here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
import serial
from serial.tools import list_ports

# USB VIDs that plausibly belong to the target board. Used only to rank
# candidate ports; an explicit --port always wins.
_LIKELY_VIDS = {0x2341, 0x2A03, 0x303A, 0x10C4, 0x1A86}  # Arduino, Espressif, SiLabs, CH34x
_LIKELY_HINTS = ("nano esp32", "esp32", "arduino", "usb serial", "cp210", "ch340")


@dataclass
class StreamHeader:
    """Metadata parsed from the firmware's ``#`` header lines."""

    fs: float = 200.0
    channels: list[str] = field(default_factory=lambda: ["green", "ir"])
    columns: list[str] = field(default_factory=lambda: ["idx", "us", "green", "ir"])
    resolution_bits: int = 18
    adc_range_na: int = 4096
    pulse_width_us: int = 411
    internal_sps: int = 400
    smp_ave: int = 2
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def full_scale(self) -> int:
        """Largest value the ADC can report, for clipping checks."""
        return (1 << self.resolution_bits) - 1

    def apply(self, line: str) -> None:
        """Absorb one ``# key=value ...`` metadata line."""
        body = line.lstrip("#").strip()
        if not body or "=" not in body:
            return
        for token in body.split():
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            self.raw[key] = value
            try:
                if key == "fs":
                    self.fs = float(value)
                elif key == "channels":
                    self.channels = [c for c in value.split(",") if c]
                elif key == "resolution_bits":
                    self.resolution_bits = int(value)
                elif key == "adc_range_na":
                    self.adc_range_na = int(value)
                elif key == "pulse_width_us":
                    self.pulse_width_us = int(value)
                elif key == "internal_sps":
                    self.internal_sps = int(value)
                elif key == "smp_ave":
                    self.smp_ave = int(value)
            except ValueError:
                pass  # tolerate a malformed field rather than abort the session

    def describe(self) -> str:
        return (
            f"{'/'.join(self.channels)} @ {self.fs:g} Hz, "
            f"{self.resolution_bits}-bit, {self.pulse_width_us} us pulse, "
            f"{self.internal_sps} sps / {self.smp_ave}x avg"
        )


def find_port(explicit: str | None = None) -> str:
    """Pick a serial port, preferring one that looks like the board."""
    if explicit:
        return explicit

    ports = list(list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found. Is the board plugged in?")

    def score(p) -> int:
        s = 0
        if p.vid in _LIKELY_VIDS:
            s += 10
        blob = f"{p.description} {p.manufacturer or ''} {p.product or ''}".lower()
        s += sum(2 for hint in _LIKELY_HINTS if hint in blob)
        return s

    ranked = sorted(ports, key=score, reverse=True)
    best = ranked[0]
    if score(best) == 0 and len(ports) > 1:
        listing = "\n".join(f"  {p.device} — {p.description}" for p in ports)
        raise RuntimeError(
            "Could not identify the board automatically. Pass --port explicitly.\n"
            f"Available ports:\n{listing}"
        )
    return best.device


class PPGStream:
    """Context manager that starts the firmware streaming and yields samples.

    Usage::

        with PPGStream(port) as stream:
            print(stream.header.describe())
            for idx, us, values in stream:
                ...
    """

    def __init__(
        self,
        port: str,
        baud: int = 921600,
        timeout: float = 1.0,
        settle: float = 2.0,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.settle = settle
        self.header = StreamHeader()
        self.overflows = 0
        self._ser: serial.Serial | None = None
        self._expected_cols = 0

    def __enter__(self) -> "PPGStream":
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        # Some boards reset on port open; give the sketch time to come up.
        time.sleep(self.settle)
        self._ser.reset_input_buffer()
        self._start()
        return self

    def __exit__(self, *exc) -> None:
        if self._ser is None:
            return
        try:
            self._ser.write(b"s")  # ask the firmware to stop streaming
            self._ser.flush()
        except serial.SerialException:
            pass
        self._ser.close()
        self._ser = None

    # -- internals ---------------------------------------------------------

    def _readline(self) -> str:
        assert self._ser is not None
        return self._ser.readline().decode("utf-8", errors="replace").strip()

    def _start(self) -> None:
        """Toggle streaming on and consume the header up to the column row."""
        assert self._ser is not None

        for attempt in range(2):
            self._ser.write(b"s")
            self._ser.flush()
            deadline = time.time() + 5.0
            already_running = False

            while time.time() < deadline:
                line = self._readline()
                if not line:
                    continue
                if line.startswith("#"):
                    if "stopped" in line:
                        # Streaming was already on from a previous session and we
                        # just turned it off. Toggle again.
                        already_running = True
                        break
                    if "ERROR" in line:
                        raise RuntimeError(f"Firmware reported: {line}")
                    self.header.apply(line)
                    continue
                if line.startswith("idx"):
                    self.header.columns = [c.strip() for c in line.split(",")]
                    data_cols = [c for c in self.header.columns if c not in ("idx", "us")]
                    if data_cols:
                        self.header.channels = data_cols
                    self._expected_cols = len(self.header.columns)
                    return

            if not already_running and attempt == 1:
                break

        raise RuntimeError(
            "Timed out waiting for the firmware header. "
            "Check the sketch is uploaded and the port is correct."
        )

    # -- iteration ---------------------------------------------------------

    def __iter__(self) -> Iterator[tuple[int, int, list[int]]]:
        assert self._ser is not None
        while True:
            line = self._readline()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith("# OVF"):
                    self.overflows += 1
                else:
                    self.header.apply(line)
                continue
            parts = line.split(",")
            if self._expected_cols and len(parts) != self._expected_cols:
                continue  # partial line from a mid-stream connect; drop it
            try:
                nums = [int(p) for p in parts]
            except ValueError:
                continue
            yield nums[0], nums[1], nums[2:]


def load_csv(path: str | Path) -> tuple[StreamHeader, np.ndarray, dict[str, np.ndarray]]:
    """Load a recording written by ``record.py``.

    Returns ``(header, t, channels)`` where ``t`` is seconds derived from the
    sample index and the header's sample rate -- not from the ``us`` column,
    which is the host read time of a FIFO burst rather than the per-sample
    conversion time.
    """
    path = Path(path)
    header = StreamHeader()
    rows: list[list[int]] = []
    columns: list[str] = []

    with path.open("r", newline="") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                header.apply(line)
                continue
            if not columns:
                columns = [c.strip() for c in line.split(",")]
                header.columns = columns
                data_cols = [c for c in columns if c not in ("idx", "us")]
                if data_cols:
                    header.channels = data_cols
                continue
            try:
                rows.append([int(v) for v in line.split(",")])
            except ValueError:
                continue

    if not rows:
        raise ValueError(f"No samples found in {path}")

    arr = np.asarray(rows, dtype=np.int64)
    idx = arr[:, columns.index("idx")]
    t = (idx - idx[0]) / header.fs

    channels = {name: arr[:, columns.index(name)].astype(float) for name in header.channels}
    return header, t, channels

