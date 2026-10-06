"""Serial transport and recording I/O for the MAPQuest firmware.

Wire format is SPEC 3.4: ``#`` metadata lines, ``S,`` data lines, ``E,``
errors. Recordings keep the metadata verbatim so they are self-describing and
no host constant has to be kept in sync by hand.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
import serial
from serial.tools import list_ports

_LIKELY_VIDS = {0x2341, 0x2A03, 0x303A, 0x10C4, 0x1A86}
_LIKELY_HINTS = ("nano esp32", "esp32", "arduino", "usb serial", "cp210", "ch340")

DEFAULT_COLUMNS = ["idx", "t_us", "ir", "red"]


@dataclass
class Meta:
    """Metadata parsed from ``#`` lines."""

    fs: float = 100.0
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    channels: list[str] = field(default_factory=lambda: ["ir", "red"])
    resolution_bits: int = 18
    firmware: str = "unknown"
    dark: dict[str, float] = field(default_factory=dict)
    has_pressure: bool = False
    sensor_ok: bool | None = None
    """Whether the firmware actually found and configured the MAX30101.

    None means the firmware predates the flag and did not report it, which is
    not the same as False -- do not treat a missing field as a failure.
    """
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def full_scale(self) -> int:
        return (1 << self.resolution_bits) - 1

    def apply(self, line: str) -> None:
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
                elif key == "columns":
                    self.columns = [c for c in value.split(",") if c]
                    self.channels = [c for c in self.columns if c in ("ir", "red", "green")]
                    self.has_pressure = "p_raw" in self.columns
                elif key == "channels":
                    self.channels = [c for c in value.split(",") if c]
                elif key == "resolution_bits":
                    self.resolution_bits = int(value)
                elif key == "firmware":
                    self.firmware = value
                elif key == "dark_ir":
                    self.dark["ir"] = float(value)
                elif key == "dark_red":
                    self.dark["red"] = float(value)
                elif key == "pressure":
                    self.has_pressure = value != "none"
                elif key == "sensor_ok":
                    self.sensor_ok = value not in ("0", "false", "False")
            except ValueError:
                pass  # tolerate one malformed field rather than abort

    def describe(self) -> str:
        pressure = "with pressure" if self.has_pressure else "PPG only"
        dark = f", dark {self.dark}" if self.dark else ""
        return f"{'/'.join(self.channels)} @ {self.fs:g} Hz, {pressure}{dark}"


def find_port(explicit: str | None = None) -> str:
    """Pick a serial port, preferring one that looks like the board."""
    if explicit:
        return explicit
    ports = list(list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found. Is the board plugged in?")

    def score(p) -> int:
        s = 10 if p.vid in _LIKELY_VIDS else 0
        blob = f"{p.description} {p.manufacturer or ''} {p.product or ''}".lower()
        return s + sum(2 for h in _LIKELY_HINTS if h in blob)

    best = max(ports, key=score)
    if score(best) == 0 and len(ports) > 1:
        listing = "\n".join(f"  {p.device} — {p.description}" for p in ports)
        raise RuntimeError(
            f"Could not identify the board. Pass --port explicitly.\nPorts:\n{listing}"
        )
    return best.device


class Device:
    """Serial link to the firmware, speaking the SPEC 3.4 protocol."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0,
                 settle: float = 2.0) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.settle = settle
        self.meta = Meta()
        self.errors: list[str] = []
        self._ser: serial.Serial | None = None

    def __enter__(self) -> "Device":
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(self.settle)
        self._ser.reset_input_buffer()
        self.command("STOP")       # known state regardless of how we found it
        self.drain(0.3)
        return self

    def __exit__(self, *exc) -> None:
        if self._ser is None:
            return
        try:
            self.command("STOP")
            self._ser.flush()
        except serial.SerialException:
            pass
        self._ser.close()
        self._ser = None

    # -- primitives --------------------------------------------------------

    def command(self, text: str) -> None:
        assert self._ser is not None
        self._ser.write((text.strip() + "\n").encode())
        self._ser.flush()

    def readline(self) -> str:
        assert self._ser is not None
        return self._ser.readline().decode("utf-8", errors="replace").strip()

    def stop(self, settle: float = 0.3) -> None:
        """Stop streaming and discard anything still in flight.

        After breaking out of ``stream()`` the input buffer still holds S
        lines, and the firmware refuses DARK and the amplitude commands while
        streaming. Clearing both is the difference between the next command
        working and it appearing to hang.
        """
        assert self._ser is not None
        self.command("STOP")
        self.drain(settle)
        self._ser.reset_input_buffer()

    def drain(self, seconds: float) -> list[str]:
        """Read whatever arrives for a while, absorbing metadata."""
        out: list[str] = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            line = self.readline()
            if not line:
                continue
            out.append(line)
            self._absorb(line)
        return out

    def _absorb(self, line: str) -> None:
        if line.startswith("#"):
            self.meta.apply(line)
        elif line.startswith("E,"):
            self.errors.append(line[2:])

    # -- high level --------------------------------------------------------

    def info(self, wait: float = 1.0) -> Meta:
        self.command("INFO")
        self.drain(wait)
        return self.meta

    def measure_dark(self, wait: float = 8.0) -> dict[str, float]:
        """Run the DARK calibration (SPEC 3.4) and return the offsets."""
        self.command("DARK")
        deadline = time.time() + wait
        while time.time() < deadline:
            line = self.readline()
            if not line:
                continue
            self._absorb(line)
            if "dark_ir" in line or line.startswith("E,"):
                break
        return dict(self.meta.dark)

    def set_amp(self, channel: str, value: int) -> None:
        verb = {"ir": "IRAMP", "red": "REDAMP"}[channel]
        self.command(f"{verb} {int(value)}")
        self.drain(0.3)

    def stream(self) -> Iterator[tuple[list[int], str]]:
        """Send START and yield ``(fields, raw_line)`` for each S line."""
        self.command("START")
        started = False
        deadline = time.time() + 5.0

        while True:
            line = self.readline()
            if not line:
                if not started and time.time() > deadline:
                    raise RuntimeError(
                        "Timed out waiting for stream. Is the sketch uploaded?"
                    )
                continue
            if line.startswith("S,"):
                started = True
                try:
                    yield [int(v) for v in line[2:].split(",")], line
                except ValueError:
                    continue
            else:
                self._absorb(line)


def load_csv(path: str | Path) -> tuple[Meta, np.ndarray, dict[str, np.ndarray], dict]:
    """Load a recording written by ``logger.py``.

    Returns ``(meta, t, channels, extra)``. ``t`` comes from the sample index
    and the header's fs -- the master clock per SPEC 3.3 -- not from ``t_us``,
    which is the host read time of a FIFO burst. ``extra`` carries ``t_us`` and
    any pressure columns.
    """
    path = Path(path)
    meta = Meta()
    columns: list[str] = []
    rows: list[list[int]] = []

    with path.open("r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                meta.apply(line)
                continue
            if line.startswith("E,"):
                continue
            if not columns:
                # The first non-comment line is the column header.
                columns = [c.strip() for c in line.split(",")]
                if columns and columns[0] == "S":
                    columns = columns[1:]
                meta.columns = columns
                continue
            parts = line.split(",")
            if parts and parts[0] == "S":
                parts = parts[1:]
            if len(parts) != len(columns):
                continue
            try:
                rows.append([int(v) for v in parts])
            except ValueError:
                continue

    if not rows:
        raise ValueError(f"No samples found in {path}")

    arr = np.asarray(rows, dtype=np.int64)
    col = {name: arr[:, i] for i, name in enumerate(columns)}

    idx = col.get("idx", np.arange(len(arr)))
    t = (idx - idx[0]) / meta.fs

    channels = {n: col[n].astype(float) for n in ("ir", "red", "green") if n in col}
    extra = {k: v for k, v in col.items() if k in ("t_us", "p_idx", "p_raw")}
    extra["idx"] = idx
    return meta, t, channels, extra


def check_continuity(idx: np.ndarray) -> tuple[int, int]:
    """Count dropped samples (SPEC 3.5 requires zero over 5 minutes).

    Returns ``(n_gaps, n_missing_samples)``.
    """
    d = np.diff(np.asarray(idx, dtype=np.int64))
    gaps = d[d != 1]
    return int(gaps.size), int(np.sum(gaps[gaps > 1] - 1)) if gaps.size else 0


def load_calibration(path: str | Path) -> dict | None:
    path = Path(path)
    if not path.exists():
        return None
    with path.open() as fh:
        return json.load(fh)


def save_calibration(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(data, fh, indent=2)
