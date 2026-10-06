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
import serial

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
                 sensor_ok=True, part_id=0x15,
                 pulse_frac=0.0015, bpm=68.0,
                 die_at=None, stall_at=None, reset_at=None,
                 temp_c=28.5, temp_rise_per_sample=0.0):
        self.port = port
        self.fs = fs
        self.ir_dc = ir_dc
        self.red_dc = red_dc
        self.dark_ir = dark_ir
        self.dark_red = dark_red
        self.drop_at = drop_at            # inject a gap at this sample index
        self.sensor_ok = sensor_ok
        self.pulse_frac = pulse_frac   # cardiac AC as a fraction of DC
        self.bpm = bpm
        self.die_at = die_at      # raise SerialException after N samples
        self.stall_at = stall_at  # go silent after N samples
        self.reset_at = reset_at  # restart the sample index after N
        self.sent = 0
        self.temp_c = temp_c
        self.temp_rise_per_sample = temp_rise_per_sample
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
        # Cardiac modulation, so a "healthy" mock looks optically coupled to
        # blood rather than like a photodiode staring at a wall.
        if self.pulse_frac:
            phase = 2 * np.pi * (self.bpm / 60.0) * (self.idx / self.fs)
            beat = np.sin(phase)
            ir += int(ir * self.pulse_frac * beat)
            red += int(red * self.pulse_frac * 0.7 * beat)
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
            # Die warms with LED activity, like the real part.
            t = self.temp_c + self.temp_rise_per_sample * self.sent
            self._emit(f"# temp_c={t:.4f}")
        elif verb:
            self._emit(f"E,unknown command: {verb}")

    def readline(self) -> bytes:
        if self._out:
            return self._out.pop(0)
        if not self.streaming:
            return b""  # timeout
        if self.die_at is not None and self.sent >= self.die_at:
            # What a yanked cable or a USB re-enumeration looks like.
            raise serial.SerialException(
                "ClearCommError failed (PermissionError(13, "
                "'The device does not recognize the command.', None, 22))")
        if self.stall_at is not None and self.sent >= self.stall_at:
            return b""  # port still open, firmware silent
        if self.reset_at is not None and self.sent == self.reset_at:
            self.idx = 0  # board restarted
        self.sent += 1
        return (self._sample() + "\n").encode()

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
    assert "cardiac pulse detected" in out


def test_bench_check_warns_when_no_pulse(fake):
    """Every SPEC 3.5 criterion passes with the sensor pointed at nothing.

    The pulse check is the one that notices, so it must not be silent.
    """
    code, out = run_bench(fake, ["--seconds", "1"], pulse_frac=0.0)
    assert code == 0, out              # not a SPEC 3.5 failure
    assert "no periodic cardiac component" in out
    assert "FINGERTIP" in out          # the advice must point somewhere useful
    assert "M1 hardware criteria met" not in out


def test_pulse_detector_rejects_pure_drift():
    """The false positive that shipped: 1/f drift read as a confident pulse.

    The old detector took the largest in-band FFT bin and compared it to a
    distant high-frequency floor. On a drift-dominated recording the spectrum
    falls monotonically, so that lands on the band's lower edge and reports a
    large SNR for a pulse that is not there. Prominence requires an actual
    local peak, which drift does not have.
    """
    import bench_check as bc

    fs, n = 100.0, 6000
    t = np.arange(n) / fs
    rng = np.random.default_rng(1)

    def pink(n):
        w = rng.normal(0, 1, n)
        F = np.fft.rfft(w)
        freq = np.fft.rfftfreq(n, 1 / fs)
        F[1:] /= freq[1:]
        F[0] = 0
        x = np.fft.irfft(F, n)
        return x / np.std(x)

    drift_only = 109_000 + 550 * pink(n) + rng.normal(0, 40, n)
    m = bc.analyse_pulse({"ir": drift_only}, fs)["channels"]["ir"]
    assert m["prominence_db"] is None or m["prominence_db"] < 6.0, (
        f"drift-only signal reported a {m['prominence_db']} dB pulse at "
        f"{m['bpm']} bpm"
    )
    assert m["drift_ratio"] > 2.0

    real = (109_000 + 109_000 * 0.0015 * np.sin(2 * np.pi * 1.15 * t)
            + 300 * pink(n) + rng.normal(0, 40, n))
    m2 = bc.analyse_pulse({"ir": real}, fs)["channels"]["ir"]
    assert m2["prominence_db"] is not None and m2["prominence_db"] >= 6.0
    assert abs(m2["bpm"] - 69.0) < 5.0


def test_bench_check_reports_plausible_rate(fake):
    code, out = run_bench(fake, ["--seconds", "1"], bpm=72.0)
    assert code == 0, out
    import re
    m = re.search(r"IR (\d+) bpm at", out)
    assert m, out
    assert 60 <= int(m.group(1)) <= 85, out


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



# ---------------------------------------------------------------------------
# Link failure modes — "the run keeps stopping midway"
# ---------------------------------------------------------------------------

def test_disconnect_raises_link_lost(fake):
    """A yanked cable must raise LinkLost, not a bare pyserial traceback."""
    from mapquest.io import LinkLost

    with open_device(fake, die_at=50) as dev:
        dev.info()
        got = []
        with pytest.raises(LinkLost) as exc:
            for fields, _ in dev.stream():
                got.append(fields)
    assert len(got) == 50
    assert exc.value.samples == 50
    assert "50" in str(exc.value)


def test_silence_raises_link_stalled_not_hang(fake):
    """The regression that matters: silence used to spin forever.

    Before this, an empty read after the stream had started just continued the
    loop, so a wedged board looked like a hang with no diagnosis.
    """
    from mapquest.io import LinkStalled

    with open_device(fake, stall_at=40) as dev:
        dev.info()
        got = []
        with pytest.raises(LinkStalled) as exc:
            for fields, _ in dev.stream(stall_timeout=0.3):
                got.append(fields)
    assert len(got) == 40
    assert exc.value.samples == 40
    assert "no data for" in str(exc.value)


def test_link_errors_carry_partial_sample_count(fake):
    from mapquest.io import LinkLost

    with open_device(fake, die_at=123) as dev:
        dev.info()
        with pytest.raises(LinkLost) as exc:
            for _ in dev.stream():
                pass
    assert exc.value.samples == 123


def test_stop_survives_a_dead_port(fake):
    """Cleanup after a link failure must not raise a second exception."""
    from mapquest.io import LinkLost

    with open_device(fake, die_at=10) as dev:
        dev.info()
        with pytest.raises(LinkLost):
            for _ in dev.stream():
                pass
        dev.stop()          # must not raise
        dev._ser.die_at = None


def test_bench_check_salvages_a_dropped_capture(fake):
    """A capture that dies partway is kept and diagnosed, not discarded."""
    code, out = run_bench(fake, ["--seconds", "30", "--no-dark"], die_at=1200)
    assert code == 1, out
    assert "LINK LOST" in out
    assert "Partial capture retained" in out
    assert "Swap the USB cable" in out
    # The salvaged samples still went through the downstream checks.
    assert "IR DC in" in out


def test_bench_check_reports_a_stall(fake):
    code, out = run_bench(
        fake, ["--seconds", "30", "--no-dark", "--stall-timeout", "0.3"],
        stall_at=900,
    )
    assert code == 1, out
    assert "LINK STALLED" in out
    assert "board reset or a wedged sketch" in out


def test_bench_check_detects_board_reset(fake):
    """A sample index going backwards means settings were silently lost."""
    code, out = run_bench(fake, ["--seconds", "30", "--no-dark"], reset_at=800)
    assert code == 1, out
    assert "BOARD RESET" in out
    assert "Brown-out" in out


def test_healthy_link_reports_stable(fake):
    code, out = run_bench(fake, ["--seconds", "1", "--no-dark"])
    assert code == 0, out
    assert "serial link stable" in out


def test_read_temperature(fake):
    with open_device(fake, temp_c=31.25) as dev:
        dev.info()
        assert dev.read_temperature() == pytest.approx(31.25, abs=0.01)


def test_read_temperature_returns_none_when_unsupported(fake):
    """Older firmware without TEMP must not crash the caller."""
    with open_device(fake) as dev:
        dev.info()
        dev._ser._handle = lambda cmd: None      # swallow TEMP
        assert dev.read_temperature(wait=0.3) is None


def test_provocation_flags_die_heating(fake):
    """A warming die must be surfaced -- it is the O1 self-heating evidence."""
    code, out = run_bench(
        fake,
        ["--provocation", "--phase-seconds", "1", "--no-dark", "--red-amp", "20"],
        temp_c=28.0, temp_rise_per_sample=0.0005,
    )
    assert "Die temp" in out
    assert "die temperature stable" in out
    assert "red set to 20" in out


def test_explicit_amp_excluded_from_autotune(fake):
    """--red-amp must survive --auto-tune rather than being overwritten."""
    code, out = run_bench(
        fake, ["--seconds", "1", "--no-dark", "--auto-tune", "--red-amp", "20"],
        ir_dc=40_000,
    )
    assert "red set to 20" in out
    assert "auto-tune ir" in out          # IR still tuned
    assert "auto-tune red" not in out     # red left alone


def test_amp_out_of_range_rejected(fake):
    code, out = run_bench(fake, ["--seconds", "1", "--no-dark", "--red-amp", "999"])
    assert code == 1
    assert "must be 0-255" in out


def test_save_writes_a_console_log(fake, tmp_path):
    """--save must persist the report, not just the samples.

    The CSV holds numbers; the auto-tune steps, warnings and verdicts only
    exist in the console output, so a run is not reproducible without them.
    """
    log = tmp_path / "run.log.txt"
    code, out = run_bench(
        fake, ["--seconds", "1", "--no-dark", "--auto-tune", "--log", str(log)],
        ir_dc=40_000,
    )
    assert code == 0, out
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert "MAPQuest bench check" in text
    assert "Auto-tuning LED current" in text
    assert "RESULT:" in text
    # Progress frames rewrite one line with \r and must not bloat the log.
    assert "\r" not in text
    assert len(text.splitlines()) < 100


def test_autotune_trajectory_is_recorded(fake, tmp_path, monkeypatch):
    """The DC-vs-current curve is data and must reach the CSV header."""
    import bench_check

    monkeypatch.setattr(bench_check, "DATA_DIR", tmp_path)
    code, out = run_bench(
        fake, ["--seconds", "1", "--no-dark", "--auto-tune", "--save"],
        ir_dc=40_000,
    )
    assert code == 0, out
    csvs = list(tmp_path.glob("*.csv"))
    assert csvs, f"no CSV written to {tmp_path}"
    header = [ln for ln in csvs[0].read_text().splitlines() if ln.startswith("#")]
    traj = [ln for ln in header if "autotune_ir" in ln]
    assert traj, header
    # At least two points, so the curve has a slope.
    assert len(traj[0].split("=")[1].split(",")) >= 2
    assert any("ir_amp=" in ln for ln in header)


def test_save_implies_a_log(fake, tmp_path, monkeypatch):
    import bench_check

    monkeypatch.setattr(bench_check, "DATA_DIR", tmp_path)
    code, _out = run_bench(fake, ["--seconds", "1", "--no-dark", "--save"])
    assert code == 0
    assert list(tmp_path.glob("*.log.txt")), list(tmp_path.iterdir())


def test_stdout_restored_after_run(fake):
    """The tee must come off even when a run fails, or later output is lost."""
    import sys as _sys
    before = _sys.stdout
    run_bench(fake, ["--seconds", "1", "--no-dark"], sensor_ok=False)
    assert _sys.stdout is before
