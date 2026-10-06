# MAPQuest — Decision Log

Every non-obvious choice, why it was made, and what it costs. Entries are append-only:
when a decision changes, the old entry is marked **superseded** rather than edited, so the
reasoning that led there stays visible.

**Add an entry whenever:** a choice deviates from [`SPEC.md`](SPEC.md), a default is picked
that someone could reasonably pick differently, a bug reveals a wrong assumption, or an
experiment settles something.

| # | Decision | Status |
|---|---|---|
| [D1](#d1) | Red + IR, not green | accepted |
| [D2](#d2) | ~~Green + IR for best waveform quality~~ | **superseded by D1** |
| [D3](#d3) | SpO₂ mode rather than Multi-LED | accepted |
| [D4](#d4) | 400 sps internal + 4× averaging → 100 Hz | accepted |
| [D5](#d5) | Inline MAX30101 driver, no SparkFun library | accepted |
| [D6](#d6) | ASCII CSV over USB CDC, not binary | accepted |
| [D7](#d7) | `idx / fs` is the master clock, not `t_us` | accepted |
| [D8](#d8) | Zero-phase filtering (`filtfilt`) | accepted |
| [D9](#d9) | Savitzky–Golay derivatives, not `np.diff` | accepted |
| [D10](#d10) | Onset by ramp intersection, not threshold crossing | accepted — **deviates from SPEC §5.1** |
| [D11](#d11) | IR-only fallback converted to micrometres | accepted — bug fix |
| [D12](#d12) | `R` retry command added | accepted — **extends SPEC §3.4** |
| [D13](#d13) | `sensor_ok` reported in `INFO` | accepted — **extends SPEC §3.4** |
| [D14](#d14) | Pulse detection by spectral prominence | accepted — bug fix |
| [D15](#d15) | Arm-raise provocation is the viability gate | accepted — **not in SPEC** |
| [D16](#d16) | ABA design in the provocation protocol | accepted |
| [D17](#d17) | Typed link errors and partial-capture salvage | accepted |
| [D18](#d18) | **Measurement site: wrist, not ventral forearm** | accepted — **deviates from SPEC §2.1/§8** |
| [D19](#d19) | Pressure path compiled out behind a flag | accepted |
| [O1](#o1) | Red channel drift — cause and mitigation | **open** |
| [O2](#o2) | `synth.py` noise defaults do not match hardware | **open** |
| [O3](#o3) | Is the onset noise floor low enough to work? | **open** |

---

<a id="d1"></a>
## D1 — Red (660 nm) + IR (880 nm), not green

**Status:** accepted · 2026-10-05

**Context.** The MAX30101 carries green, red and IR. Only two channels are needed.

**Decision.** Red + IR. Green is left unused.

**Why.** SPEC §5.1 step 6 converts optical density into blood volume with
`ΔV·L = 229·ΔOD₆₆₀ + 1555·ΔOD₈₈₀`. Those coefficients are tied to haemoglobin extinction at
660 and 880 nm; there is no green coefficient to substitute. More importantly the Hb−HbO₂
extinction difference is **+2906 cm⁻¹/M at 660 nm and −488 cm⁻¹/M at 880 nm** — the sign flip
is what lets the pair cancel desaturation of trapped blood. Green sits where oxy and deoxy
absorb almost equally, so it carries nearly no saturation information and cannot play either
role. Green's 0.3–0.8 mm penetration would also largely miss the bulk filling being measured.

**Consequences.** Locked into SpO₂ mode (see D3). Green remains available for *diagnostics*
if coupling ever needs a more sensitive probe — that would not affect the measurement path.

Full reasoning and sources: [`led-selection-report.md`](led-selection-report.md).

---

<a id="d2"></a>
## D2 — ~~Green + IR, for best reflectance waveform quality~~

**Status:** **superseded by D1** · 2026-10-05

**What was decided.** Green + IR in Multi-LED mode, on the general reflectance-PPG literature:
green has the best AC/DC ratio, highest SNR, and sharpest dicrotic notch.

**Why it was wrong.** It optimised for *pulsatile waveform quality at a fingertip*, which was
the stated goal before `SPEC.md` arrived. The actual measurement is return-to-flow MAP built
on a two-wavelength Beer–Lambert solve, where green is not merely worse but unusable. The
research was sound; it answered the wrong question.

**Lesson.** Confirm the measurement model before optimising the front end. The literature's
"best" depends entirely on what is being measured.

---

<a id="d3"></a>
## D3 — SpO₂ mode rather than Multi-LED

**Status:** accepted · 2026-10-05

**Decision.** `MODE[2:0] = 0b011` (SpO₂), which drives exactly Red + IR.

**Why.** Follows from D1. Multi-LED would work but needs explicit slot configuration for no
benefit. One trap recorded for posterity: on the MAX30101, **"Heart Rate mode" is Red-only,
not green-only** — people arriving from the MAX30102 (which has no green) routinely assume
otherwise. Green is reachable *only* through Multi-LED.

**Consequences.** FIFO layout is fixed at Red-then-IR, 3 bytes each. Verified against the
datasheet (p.15: "3 bytes for RED, 3 bytes for IR") and SparkFun's reference implementation.

---

<a id="d4"></a>
## D4 — 400 sps internal with `SMP_AVE = 4`, giving 100 Hz output

**Status:** accepted · 2026-10-05

**Context.** SPEC §3.1 asks for 100 Hz. Datasheet Table 11 caps two-channel operation at
**400 sps** when the pulse width is 411 µs (the setting that gives full 18-bit resolution).

**Decision.** Run the ADC at its 400 sps ceiling and average 4 samples in hardware.

**Why.** Output rate and internal rate are separable. Running the ADC at 100 sps would meet
the spec while discarding the headroom; spending it on averaging buys roughly a 2× noise
reduction for free. The measurement is a slow DC trend whose detection threshold is set
directly by the noise floor, so this is the right place to spend it.

**Consequences.** Changing the output rate means changing `SAMPLE_AVERAGING`, not
`SAMPLE_RATE_SPS`. Both live in `config.h` and are echoed into every recording header.

---

<a id="d5"></a>
## D5 — Inline MAX30101 driver, not the SparkFun library

**Status:** accepted · 2026-10-05 *(rationale recovered 2026-10-06 — see note)*

**Decision.** The sketch talks to the sensor directly over `Wire`. No external libraries.

**Why.**
1. **No install step.** Open the `.ino` in Arduino IDE and upload.
2. **The SparkFun library names its FIFO getters by slot position, not by LED.**
   `getFIFORed()` returns whatever sits in slot 1. Put green in slot 1 and `getFIFORed()`
   returns green. For a project whose correctness depends on knowing which wavelength
   produced which number — the two-wavelength solve weights them 229 vs 1555 — that is a
   serious footgun.
3. Full control over the slot and averaging configuration D3/D4 depend on.

**Consequences.** ~200 lines of register code to maintain, and the datasheet is the only
reference. Compile-verified on every change via the `arduino-cli` bundled inside Arduino IDE.

> **Note.** This rationale was lost when `docs/design.md` was deleted during the restructure
> to match SPEC's layout, and recovered from git history (`782fbd5`) on 2026-10-06. It is the
> reason this decision log exists.

---

<a id="d6"></a>
## D6 — ASCII CSV over USB CDC, not a binary protocol

**Status:** accepted · 2026-10-05

**Decision.** `S,<idx>,<t_us>,<ir>,<red>` as text.

**Why.** At 100 Hz × 2 channels this is roughly 3 kB/s — nowhere near a USB CDC bottleneck.
Text is debuggable from any serial monitor, which matters a great deal during bring-up. The
bandwidth saved by a binary format buys nothing here.

**Revisit if** the sample rate rises substantially or more channels are added.

---

<a id="d7"></a>
## D7 — `idx / fs` is the master clock; `t_us` is diagnostics only

**Status:** accepted · 2026-10-05 (follows SPEC §3.3)

**Why.** `t_us` is `micros()` at the moment the host drained a FIFO *burst*, so every sample
in that burst shares a timestamp. It is not the conversion time. Using it as a timebase would
quantise time into bursts. The sample index divided by the configured rate is uniform and
correct.

**Consequences.** `t_us` is still recorded, and is what SPEC §3.5 uses to verify output rate
and detect jitter. Measured on hardware at **99.37 Hz vs 100 configured (−0.63 %)** — within
spec, and benign, because pressure is attached per-PPG-sample so the small scale error
cancels when reading pressure at the onset.

---

<a id="d8"></a>
## D8 — Zero-phase filtering (`sosfiltfilt`)

**Status:** accepted · 2026-10-05

**Why.** A causal filter imposes a frequency-dependent group delay, which would shift the
onset in time and therefore bias the reported pressure. Offline we can afford to be
non-causal and avoid the question entirely.

**Consequences.** The pipeline needs the whole buffer, so it is not real-time. SPEC §5.3's
real-time variant must instead subtract a known delay (≈1.75 s ≈ 3.5 mmHg at 2 mmHg/s).
**Zero-phase is not zero-smearing** — see D10, which exists because of exactly that
confusion.

---

<a id="d9"></a>
## D9 — Savitzky–Golay derivatives, not finite differencing

**Status:** accepted · 2026-10-05

**Why.** Finite differencing multiplies high-frequency noise by ω per derivative. SG fits a
local polynomial and differentiates it analytically, smoothing and differentiating in one
well-conditioned step. The 2.0 s window is one HeartMate-3 artificial-pulse period, so it
cancels that component and its harmonics — keeping the pipeline valid on LVAD data without
re-tuning (SPEC §5.1).

---

<a id="d10"></a>
## D10 — Onset by ramp intersection, not threshold crossing

**Status:** accepted · 2026-10-05 · **deviates from SPEC §5.1 step 10**

**Context.** The spec defines onset as the first sample of a sustained excursion above
`μ₀ + 3σ₀`, and SPEC §6 requires accuracy within **±1 mmHg**.

**Problem.** The spec's own method cannot meet the spec's own criterion. Measured at
**+2.6 mmHg** on synthetic ground truth. The cause is structural, not noise: the 0.3 Hz
zero-phase low-pass and the 2 s SG window are both *symmetric* kernels, so they smear the
ramp's corner backwards in time as well as forwards. The derivative starts climbing before
filling actually begins. Confirmed by sweeping the window: 0.5 s → +2.16, 2 s → +2.56,
4 s → +3.70 mmHg.

**Decision.** Default to `--onset-method intersect`: fit the established ramp and extrapolate
back to where it meets the occlusion baseline. Because it uses the ramp's slope and offset
rather than the location of its corner, smearing cancels. Measured error **−0.15 mmHg**.

**Consequences.** Both estimators are always computed and both printed, so the difference
stays visible. `--onset-method threshold` gives literal spec behaviour.
**This is a candidate spec amendment** — flagged for the user, not yet resolved.

---

<a id="d11"></a>
## D11 — IR-only fallback converted to micrometres

**Status:** accepted · 2026-10-05 · bug fix

**Problem.** SPEC §5.1 step 6 states the fallback as `ΔOD₈₈₀ / 6.13 cm⁻¹`, which yields
**centimetres**, while the two-wavelength branch yields **micrometres**. Implemented
literally, the two paths sat 10⁴ apart, making any shared threshold, comparison or plot
meaningless.

**Decision.** Convert the fallback to µm (`×1e4`).

**Sanity check.** `1e4 / 6.13 = 1631 µm/OD` lands close to the two-wavelength `COEFF_IR_880`
of 1555 — the fallback is the same formula with the red term dropped, which is the
confirmation the conversion is the right way round.

---

<a id="d12"></a>
## D12 — `R` retry command

**Status:** accepted · 2026-10-06 · **extends SPEC §3.4**

**Decision.** `R` (or `RETRY`) re-probes and reconfigures the sensor at runtime.

**Why.** Bring-up involves wiring mistakes. Without this, every fix costs a reflash. Cheap to
add, materially faster to iterate.

---

<a id="d13"></a>
## D13 — `sensor_ok` reported in `INFO`

**Status:** accepted · 2026-10-06 · **extends SPEC §3.4** · bug fix

**Problem.** The firmware printed `part_id=0x%02X` using `EXPECTED_PART_ID`, the compile-time
constant — so with **no sensor attached at all**, `INFO` still reported `part_id=0x15` and the
bench check passed "MAX30101 found at 0x57".

**Decision.** Report the id actually read, plus an explicit `sensor_ok` flag. `START` is
refused when it is false, rather than streaming a plausible-looking flat line.

**Host note.** A missing `sensor_ok` field parses as `None`, not `False` — older firmware
must not be mistaken for a failure.

---

<a id="d14"></a>
## D14 — Pulse detection by spectral prominence

**Status:** accepted · 2026-10-06 · bug fix

**Problem.** The detector took the largest in-band FFT bin and compared it to a distant
5–20 Hz noise floor. On a drift-dominated recording the spectrum falls monotonically, so the
argmax lands on the band's **lower edge** and the comparison reports a large SNR for a pulse
that does not exist. It confidently reported "45 bpm, 27 dB" on a recording with no pulse.

**Decision.** Require a genuine local peak with prominence, measured against its own
neighbourhood. A 1/f spectrum has no local maxima, so it correctly yields nothing.

**Verified.** Synthetic 1/f drift → 2.5 dB (rejected); real 0.15 % pulse → 18.4 dB (accepted).

**Consequence.** Conservative: a weak real pulse buried in drift is reported as absent. That
is the right failure direction — see D15, where the pulse is advisory anyway.

---

<a id="d15"></a>
## D15 — The arm-raise provocation test is the viability gate

**Status:** accepted · 2026-10-06 · **not in SPEC**

**Context.** Every SPEC §3.5 acceptance criterion passes with the photodiode staring at a
wall. The first wrist attempt met them all while showing a perfusion index of 0.024 % — no
usable signal.

**Decision.** Gate on whether DC responds to a deliberate blood-volume change: raise the arm
(venous drainage), lower it (pooling), with rests between.

**Why not the cardiac pulse.** The measurement detects slow venous filling, which is roughly
an order of magnitude larger than the cardiac AC. A site can be perfectly viable while
showing no resolvable heartbeat, so gating on a pulse would reject working setups. Borne out
on hardware: the pulse was undetectable, while the provocation response was **z = 37.8**.

**Sign convention.** More blood → more absorption → **fewer** counts. Arm up raises counts.
A response with the wrong sign is flagged as its own failure mode, not a pass.

---

<a id="d16"></a>
## D16 — ABA design in the provocation protocol

**Status:** accepted · 2026-10-06

**Decision.** Rest before *and* after each manoeuvre; compare each manoeuvre to the mean of
the two surrounding rests.

**Why.** Baseline drift at these timescales is large — ~1,400 counts over 60 s was measured.
Comparing a manoeuvre only to the rest preceding it would read that drift as a response.
Verified: 1,500 counts of pure linear drift with no real response classifies as **flat**
(z = 0.4).

**Also:** only the last 50 % of each phase is used, because venous volume settles over several
seconds and averaging the transient would understate the effect.

**Known limitation.** If the baseline does not recover after a manoeuvre, the correction
underestimates it. Observed on 2026-10-06: `rest3` never returned to baseline, so the
arm-down effect was reported as −1,769 counts where against `rest2` alone it is −3,528.

---

<a id="d17"></a>
## D17 — Typed link errors and partial-capture salvage

**Status:** accepted · 2026-10-06

**Problem.** Once streaming began, an empty read just continued the loop — a silent board
spun forever with no diagnosis. A disconnect raised a bare pyserial traceback and discarded
the whole capture.

**Decision.** `LinkLost` (port gone) and `LinkStalled` (port open, firmware silent) carry the
sample count and port diagnostics. Captures that die partway are kept and analysed, with the
failure reported alongside.

**Why distinguish them.** They point at different causes: `LinkLost` means cable or
re-enumeration; `LinkStalled` means the OS still has the device, so suspect the sketch.

---

<a id="d18"></a>
## D18 — Measurement site: wrist, not ventral forearm

**Status:** accepted · 2026-10-06 · **deviates from SPEC §2.1 and §8**

**Context.** SPEC §2.1 specifies "PPG on ventral forearm, 5–10 cm below cuff edge" and §8
repeats it in the trial procedure. The forearm attempt gave a perfusion index of 0.024 % and
no detectable response.

**Decision.** Use the wrist for the time being. **User's call**, made 2026-10-06.

**Concern raised before the decision.** The wrist is the site where Red/IR is weakest —
commercial wrist heart-rate sensors all use green, because green's AC/DC ratio and shallow
penetration suit the wrist's superficial capillary bed. Red/IR there is the hardest
combination.

**Outcome — the concern did not materialise.** The provocation test passed decisively:
arm-up **+3.31 % of DC, z = 37.8**; arm-down **−1.72 %, z = 19.2**; both correct sign. The
site responds to blood volume far more strongly than the forearm attempt did.

**Consequences and open risks.**
- Return-to-flow remains valid: the wrist is still distal to an upper-arm cuff, so it fills.
- Filling **volume and time course will differ** from the forearm — more tendon and bone, less
  muscle. Any filling-rate figure borrowed from forearm venous-occlusion literature does not
  transfer.
- SPEC §8's trial procedure needs updating if wrist becomes permanent.
- The red channel's drift (O1) was measured at the wrist and may be site-specific.

**Revisit if** the forearm is retried with better mechanical coupling, or if O1 traces the red
drift to something about this site.

---

<a id="d19"></a>
## D19 — Pressure path compiled out behind `PRESSURE_ENABLED`

**Status:** accepted · 2026-10-05

**Decision.** HX710B support is written but `#if`-d out, defaulting off. With it off the
firmware omits the pressure fields from `S` lines and the host records PPG only.

**Why.** The sensor is not owned yet. Writing it now while the datasheet details are fresh —
the 60 µs SCK ceiling, the 27-pulse mode select — is cheaper than returning later. Compiling
both paths on every change keeps the disabled branch from rotting.

**Caveat.** Compiled, never executed. The timing-critical parts are exactly the ones a
compiler cannot check.

---

<a id="o1"></a>
## O1 — Red channel drift: cause and mitigation

**Status:** **open** · raised 2026-10-06

**Observation.** With the arm still, red drifts ~90× more than IR:

| Phase | IR | Red |
|---|---|---|
| rest1 | −3.2 ct/s | −290 ct/s |
| rest2 | +178 ct/s | −1,064 ct/s (−0.73 %/s) |
| rest3 | −34 ct/s | −1,167 ct/s (−0.95 %/s) |

**Why it matters.** It enters ΔV·L through the `229·ΔOD₆₆₀` term and inflates the onset noise
floor. On `rest3`, dropping red halves the threshold (16.8 vs 34.3 µm/s) — but on `rest1`
two-wavelength is slightly *better* (7.65 vs 9.05), so there is no clean case for removing it.

**Leading hypothesis.** Red LED self-heating. Auto-tune set red to 19.8 mA against IR's
8.4 mA, and datasheet Table 13 gives up to 8 °C rise at high current and duty cycle.

**Next test.** Lower red current, allow a warm-up before recording, re-run the provocation and
compare drift. LED amplitudes are now recorded in every capture header, so runs are
comparable.

---

<a id="o2"></a>
## O2 — `synth.py` noise defaults do not match hardware

**Status:** **open** · raised 2026-10-06

**Problem.** `synth.py` defaults to a 3 µm/s filling ramp. The measured onset threshold in the
cleanest rest period is **7.65 µm/s** — the simulator's signal is below the real hardware's
noise floor. The SPEC §6 tests have been passing against conditions easier than reality.

**Impact.** False confidence. A green test suite currently does not imply the measurement
will work.

**Options.** Re-anchor the simulator's noise on measured per-phase figures; or treat the
synthetic tests as checking algorithm correctness only, and add a separate hardware-anchored
test. Not yet decided.

---

<a id="o3"></a>
## O3 — Is the onset noise floor low enough?

**Status:** **open** · raised 2026-10-06

**Measured.** Threshold `μ₀ + 3σ₀` = **+7.65 µm/s** in the cleanest rest phase; +34 and
+113 µm/s in the two contaminated ones.

**Unknown.** The real venous filling rate under cuff occlusion at the wrist. The arm-raise
produced ~70 µm over ~5 s settling (≈14 µm/s), which clears the threshold roughly 2×. If cuff
filling is comparable it will work; if it is slower it will not.

**Resolves when** the HX710B arrives and a real deflation can be recorded. Until then this is
the single largest unknown in whether the project works.
