"""Protocol tests against a mock firmware.

The host half of the SPEC 3.4 protocol can be proven without hardware, which
is worth doing: a protocol bug otherwise shows up as a mysterious hang with
the board plugged in, when it is hardest to debug.

``FakeSerial`` emulates the sketch's command handling and S-line output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
HOST = ROOT / "host"
sys.path.insert(0, str(HOST))

import mapquest.io as io_mod  # noqa: E402
from mapquest.io import Device, check_continuity  # noqa: E402

FULL_SCALE = (1 << 18) - 1


class FakeSerial:
    """Minimal stand-in for the firmware over serial.

    Emits t_us consistent with the configured fs regardless of how fast the
    host reads, so device-time checks behave as they would on hardware.
    """

    def __init__(self, port="FAKE", baud=115200, timeout=1.0,
                 fs=100.0, ir_dc=130_000, red_dc=110_000,
                 dark_ir=95, dark_red=88, drop_at=None, boot_error=None,
                 sensor_ok=True, part_id=0x15):
        self.port = port
        self.fs = fs
        self.ir_dc = ir_dc
        self.red_dc = red_dc
        self.dark_ir = dark_ir
        self.dark_red = dark_red
        self.drop_at = drop_at            # inject a gap at this sample index
        self.sensor_ok = sensor_ok
        self.part_id = part_id if sensor_ok else 0x00
        self.ir_amp = 31
        self.red_amp = 31

        self.streaming = False
        self.idx = 0
        self.t_us = 0
        self.closed = False
        self._out: list[bytes] = []
        self._in = b""
        self._rng = np.random.default_rng(0)

        if boot_error:
            self._emit(f"E,{boot_error}")
        else:
            self._info()
            self._emit("# ready — send START to stream, INFO for settings")

    # -- helpers -----------------------------------------------------------

    def _emit(self, line: str) -> None:
        self._out.append((line + "\n").encode())

    def _info(self) -> None:
        self._emit("# firmware=mapquest-fw-0.2")
        self._emit(f"# part_id=0x{self.part_id:02X} rev=0x03 sensor_ok={1 if self.sensor_ok else 0}")
        if not self.sensor_ok:
            self._emit("E,sensor not initialised — check SDA=A4, SCL=A5, 3V3, GND, "
                       "then send R to retry")
        self._emit("# mode=spo2 channels=ir,red")
        self._emit(f"# fs={self.fs:.2f}")
        self._emit("# internal_sps=400 smp_ave=4")
        self._emit("# pulse_width_us=411 resolution_bits=18 adc_range_na=4096")
        self._emit(f"# ir_amp={self.ir_amp} red_amp={self.red_amp}")
        self._emit(f"# ir_amp_ma={self.ir_amp * 0.2:.1f} "
                   f"red_amp_ma={self.red_amp * 0.2:.1f}")
        self._emit("# pressure=none")
        self._emit("# columns=idx,t_us,ir,red")

    def _dc_for(self, amp: int, base_amp: int, base_dc: int, dark: int) -> int:
        """DC scales with LED current above the dark floor, and saturates."""
        value = dark + (base_dc - dark) * (amp / max(base_amp, 1))
        return int(min(value, FULL_SCALE))

    def _sample(self) -> str:
        ir = self._dc_for(self.ir_amp, 31, self.ir_dc, self.dark_ir)
        red = self._dc_for(self.red_amp, 31, self.red_dc, self.dark_red)
        ir += int(self._rng.normal(0, 40))
        red += int(self._rng.normal(0, 40))
        line = f"S,{self.idx},{self.t_us},{max(ir, 0)},{max(red, 0)}"
        self.idx += 1
        if self.drop_at is not None and self.idx == self.drop_at:
            self.idx += 7  # lose 7 samples
        self.t_us = (self.t_us + int(round(1e6 / self.fs))) % (1 << 32)
        return line

    # -- pyserial surface --------------------------------------------------

    def write(self, data: bytes) -> int:
        self._in += data
        while b"\n" in self._in:
            raw, _, self._in = self._in.partition(b"\n")
            self._handle(raw.decode().strip())
        return len(data)

    def _handle(self, cmd: str) -> None:
        verb, _, arg = cmd.partition(" ")
        arg = arg.strip()
        if verb == "START":
            if not self.sensor_ok:
                self._emit("E,cannot START — sensor not initialised; send R to retry")
                return
            self.idx = 0
            self.streaming = True
            self._info()
            self._emit("# streaming=1")
        elif verb == "STOP":
            self.streaming = False
            self._emit("# streaming=0")
        elif verb == "INFO":
            self._info()
        elif verb == "DARK":
            if self.streaming:
                self._emit("E,DARK refused while streaming — send STOP first")
            else:
                self._emit(f"# dark_ir={self.dark_ir} dark_red={self.dark_red} dark_n=200")
        elif verb in ("IRAMP", "REDAMP"):
            if self.streaming:
                self._emit("E,LED amplitude locked while streaming — send STOP first")
                return
            try:
                v = int(arg)
            except ValueError:
                self._emit("E,amplitude must be 0-255")
                return
            if not (0 <= v <= 255):
                self._emit("E,amplitude must be 0-255")
                return
            if verb == "IRAMP":
                self.ir_amp = v
                self._emit(f"# ir_amp={v} ir_amp_ma={v * 0.2:.1f}")
            else:
                self.red_amp = v
                self._emit(f"# red_amp={v} red_amp_ma={v * 0.2:.1f}")
        elif verb == "TEMP":
            self._emit("# temp_c=28.5000")
        elif verb:
            self._emit(f"E,unknown command: {verb}")

    def readline(self) -> bytes:
        if self._out:
            return self._out.pop(0)
        if self.streaming:
            return (self._sample() + "\n").encode()
        return b""  # timeout

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    """Patch serial.Serial so Device talks to a FakeSerial."""
    holder = {}

    def factory(port, baud, timeout=1.0, **kw):
        dev = FakeSerial(port, baud, timeout, **holder.get("kwargs", {}))
        holder["serial"] = dev
        return dev

    monkeypatch.setattr(io_mod.serial, "Serial", factory)
    return holder


def open_device(fake, **kwargs) -> Device:
    fake["kwargs"] = kwargs
    return Device("FAKE", settle=0.0)


# ---------------------------------------------------------------------------
# Metadata and commands
# ---------------------------------------------------------------------------

def test_info_parsed(fake):
    with open_device(fake) as dev:
        meta = dev.info()
    assert meta.fs == 100.0
    assert meta.channels == ["ir", "red"]
    assert meta.columns == ["idx", "t_us", "ir", "red"]
    assert meta.firmware == "mapquest-fw-0.2"
    assert meta.resolution_bits == 18
    assert meta.has_pressure is False
    assert meta.raw["part_id"] == "0x15"


def test_dark_measurement(fake):
    with open_device(fake) as dev:
        dev.info()
        dark = dev.measure_dark()
    assert dark == {"ir": 95.0, "red": 88.0}


def test_set_amp_changes_dc(fake):
    """Doubling the amplitude should roughly double the DC above the dark floor."""
    with open_device(fake) as dev:
        dev.info()
        dev.set_amp("ir", 62)
        got = []
        for fields, _ in dev.stream():
            got.append(fields[2])
            if len(got) >= 50:
                break
        dev.stop()
    expected = 95 + (130_000 - 95) * 2
    assert abs(np.mean(got) - expected) < 2_000


def test_amp_refused_while_streaming(fake):
    """The firmware locks LED settings during a run (SPEC 9)."""
    with open_device(fake) as dev:
        dev.info()
        for i, _ in enumerate(dev.stream()):
            if i >= 5:
                break
        dev.command("IRAMP 50")
        dev.drain(0.05)
        assert any("locked while streaming" in e for e in dev.errors)


def test_stop_clears_pending_and_allows_dark(fake):
    """DARK after streaming must work — this is what Device.stop() is for."""
    with open_device(fake) as dev:
        dev.info()
        for i, _ in enumerate(dev.stream()):
            if i >= 20:
                break
        dev.stop()
        dark = dev.measure_dark()
    assert dark == {"ir": 95.0, "red": 88.0}
    assert not any("refused" in e for e in dev.errors)


# ---------------------------------------------------------------------------
# Stream integrity
# ---------------------------------------------------------------------------

def test_stream_indices_continuous(fake):
    with open_device(fake) as dev:
        dev.info()
        idx = []
        for fields, _ in dev.stream():
            idx.append(fields[0])
            if len(idx) >= 500:
                break
        dev.stop()
    n_gaps, n_missing = check_continuity(np.array(idx))
    assert (n_gaps, n_missing) == (0, 0)


def test_dropped_samples_are_detected(fake):
    """An injected FIFO gap must be caught, not silently spliced shut."""
    with open_device(fake, drop_at=100) as dev:
        dev.info()
        idx = []
        for fields, _ in dev.stream():
            idx.append(fields[0])
            if len(idx) >= 300:
                break
        dev.stop()
    n_gaps, n_missing = check_continuity(np.array(idx))
    assert n_gaps == 1
    assert n_missing == 7


def test_rate_from_t_us_matches_configured_fs(fake):
    """SPEC 3.5 measures output rate from t_us."""
    with open_device(fake) as dev:
        dev.info()
        rows = []
        for fields, _ in dev.stream():
            rows.append(fields)
            if len(rows) >= 1000:
                break
        dev.stop()
        fs_cfg = dev.meta.fs

    arr = np.asarray(rows)
    span_us = int((int(arr[-1, 1]) - int(arr[0, 1])) % (1 << 32))
    measured = (len(arr) - 1) / (span_us / 1e6)
    assert abs(measured - fs_cfg) / fs_cfg < 0.01


def test_t_us_wraparound_is_handled():
    """micros() wraps at 2^32; the masked difference must stay positive."""
    first, last = (1 << 32) - 1000, 500  # wrapped
    span = int((last - first) % (1 << 32))
    assert span == 1500


def test_missing_sensor_is_reported_by_info(fake):
    """A missing sensor must NOT look present.

    The firmware used to echo EXPECTED_PART_ID in its header, so INFO claimed
    part_id=0x15 with nothing on the bus. It now reports the id it actually
    read plus an explicit sensor_ok flag.
    """
    with open_device(fake, sensor_ok=False) as dev:
        meta = dev.info()
    assert meta.sensor_ok is False
    assert meta.raw["part_id"] != "0x15"
    assert any("not initialised" in e for e in dev.errors)


def test_start_refused_without_sensor(fake):
    """Streaming an unconfigured sensor would emit a plausible flat line."""
    with open_device(fake, sensor_ok=False) as dev:
        dev.info()
        dev.command("START")
        dev.drain(0.05)
    assert any("cannot START" in e for e in dev.errors)


def test_sensor_ok_true_when_present(fake):
    with open_device(fake) as dev:
        assert dev.info().sensor_ok is True


def test_sensor_ok_none_on_older_firmware():
    """A firmware without the flag must not be mistaken for a failure."""
    from mapquest.io import Meta
    m = Meta()
    m.apply("# part_id=0x15 rev=0x03")
    assert m.sensor_ok is None


# ---------------------------------------------------------------------------
# End to end into the analysis
# ---------------------------------------------------------------------------

def test_captured_stream_loads_and_derives(fake, tmp_path):
    """A capture written to CSV must load and run through the pipeline."""
    from mapquest.io import load_csv
    from mapquest.pipeline import derive

    with open_device(fake) as dev:
        dev.info()
        rows = []
        for fields, _ in dev.stream():
            rows.append(fields)
            if len(rows) >= 800:
                break
        dev.stop()
        meta = dev.meta

    csv = tmp_path / "cap.csv"
    with csv.open("w") as fh:
        fh.write(f"# fs={meta.fs:.2f}\n")
        fh.write("# columns=idx,t_us,ir,red\n")
        fh.write(f"# dark_ir={meta.dark.get('ir', 0)} dark_red={meta.dark.get('red', 0)}\n")
        fh.write("idx,t_us,ir,red\n")
        for r in rows:
            fh.write(",".join(str(v) for v in r) + "\n")

    loaded_meta, t, channels, extra = load_csv(csv)
    assert loaded_meta.fs == 100.0
    assert set(channels) == {"ir", "red"}
    assert len(t) == 800

    d = derive(channels, loaded_meta.fs, dark=loaded_meta.dark, t=t)
    assert np.all(np.isfinite(d.dv_lp))
    # Flat input: no filling, so no onset.
    assert d.onset_time is None


# ---------------------------------------------------------------------------
# bench_check end to end
# ---------------------------------------------------------------------------

def run_bench(fake, argv, **kwargs):
    """Run bench_check.main() against the mock, returning (exit_code, stdout)."""
    import io as _io
    from contextlib import redirect_stdout

    import bench_check

    fake["kwargs"] = kwargs
    old_argv = sys.argv
    sys.argv = ["bench_check.py", "--port", "FAKE"] + argv
    buf = _io.StringIO()
    try:
        with redirect_stdout(buf):
            code = bench_check.main()
    finally:
        sys.argv = old_argv
    return code, buf.getvalue()


def test_bench_check_passes_on_healthy_sensor(fake):
    code, out = run_bench(fake, ["--seconds", "1"])
    assert code == 0, out
    assert "M1 hardware criteria met" in out
    assert "zero dropped samples" in out


def test_bench_check_fails_without_sensor(fake):
    """The regression that matters: a missing sensor must FAIL, not pass."""
    code, out = run_bench(fake, ["--seconds", "1"], sensor_ok=False)
    assert code == 1, out
    assert "sensor_ok=0" in out
    assert "M1 hardware criteria met" not in out


def test_bench_check_fails_on_dropped_samples(fake):
    code, out = run_bench(fake, ["--seconds", "1", "--no-dark"], drop_at=500)
    assert code == 1, out
    assert "samples missing" in out


def test_bench_check_warns_on_out_of_range_dc(fake):
    code, out = run_bench(fake, ["--seconds", "1", "--no-dark"], ir_dc=40_000)
    assert code == 0, out          # a warning, not a failure
    assert "warn" in out
    assert "raise IR current" in out


def test_bench_check_auto_tune_reaches_target(fake):
    code, out = run_bench(
        fake, ["--seconds", "1", "--no-dark", "--auto-tune"], ir_dc=40_000
    )
    assert code == 0, out
    assert "auto-tune ir" in out
    # Tuned DC must land inside the SPEC 3.1 window.
    import re
    m = re.search(r"IR DC in [\d,]+-[\d,]+\n\s+mean=([\d,]+)", out)
    assert m, out
    assert 105_000 <= int(m.group(1).replace(",", "")) <= 157_000
