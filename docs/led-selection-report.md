# LED Channel Selection for the MAX30101

**Question:** which LED channels give the most accurate starting point for measuring change in
blood volume on this hardware?

**Answer: Red (660 nm) + IR (880 nm), in SpO2 mode.** Not green.

> **Correction.** An earlier version of this report recommended **Green + IR**, on the general
> reflectance-PPG literature. That recommendation was answering the wrong question — it optimised
> for pulsatile-waveform signal quality at a fingertip, before `SPEC.md` established that the
> measurement is a **return-to-flow MAP estimate on the forearm** built on a two-wavelength
> Beer–Lambert solve. Under the actual requirement, green is not merely worse, it is unusable.
> Section 2 explains why. The datasheet findings in sections 3–5 are unchanged and still govern.

---

## 1. The requirement that settles it

`SPEC.md` §5.1 step 6 converts optical density change into blood volume with:

```
ΔV·L [µm] = 229·ΔOD₆₆₀ + 1555·ΔOD₈₈₀
```

This is a two-wavelength inversion against **haemoglobin extinction coefficients at 660 nm and
880 nm specifically**. The two wavelengths are not interchangeable inputs — the whole construction
depends on the pair.

## 2. Why green cannot be substituted

**It would break the solve.** The coefficients 229 and 1555 are tied to the Hb/HbO₂ extinction
values at 660 and 880 nm. There is no green coefficient to put in their place, and no reason a
green-plus-IR pair would invert to total haemoglobin at all.

**It would destroy the desaturation rejection, which is the entire point of using two
wavelengths.** The Hb − HbO₂ extinction difference is about **+2906 cm⁻¹/M at 660 nm** and about
**−488 cm⁻¹/M at 880 nm**. The sign flip is what lets the combination cancel: as trapped blood
desaturates under the cuff, absorption rises at 660 and falls at 880, and the weighted sum stays
near zero while a genuine volume change does not. Green sits where oxy and deoxy haemoglobin
absorb almost equally — it carries nearly no saturation information, so it cannot play either
role in that cancellation.

This is tested directly: `tests/test_pipeline.py::test_two_wavelength_rejects_desaturation`
desaturates trapped blood at constant volume and asserts the two-wavelength result stays flat
while the IR-only fallback drifts. Measured on synthetic data, IR-only drifts **~8.5×** more.

**It would miss the tissue being measured.** Green penetrates roughly **0.3–0.8 mm** — superficial
dermis and capillary bed. The measurement is forearm filling under an occluding cuff, a bulk
vascular event in deeper tissue. IR reaches **1.5–2.5 mm**. Green's shallow penetration, which is
an advantage for a motion-robust wrist heart-rate signal, is a liability here.

**The thing green is actually good at is not wanted.** The green literature's headline results are
better AC/DC ratio, higher SNR and motion robustness — all about the **pulsatile AC waveform**. In
this measurement the pulsatile component is a *nuisance*: the signal of interest is the slow DC
trend as the forearm refills, and §5.1 step 12 treats pulsatility purely as a flag that onset will
read near systolic rather than MAP.

## 3. The mode this forces

| Mode | `MODE[2:0]` | Active LEDs |
|---|---|---|
| Heart Rate | `0b010` | **Red only** |
| SpO2 | `0b011` | **Red + IR** ← |
| Multi-LED | `0b111` | Any of Green / Red / IR in up to 4 slots |

**SpO2 mode is exactly Red + IR**, so it is the natural fit and no slot configuration is needed.

One trap worth recording: on the MAX30101, **"Heart Rate mode" is Red-only, not Green-only.**
People coming from the MAX30102 (which has no green LED) often assume HR mode is the green
channel. It is not. Green is reachable only through Multi-LED mode — which this project does not
use.

Board wavelengths (SparkFun SEN-16474): Green 530–545 nm, Red 650–670 nm, IR 870–900 nm. Note the
spec quotes 660 nm and 880 nm; the actual emitters are centres of those ranges, and the
coefficients assume the nominal values. Part-to-part wavelength variation is a real source of
systematic error in the solve, and the die temperature sensor exists partly to track it — the
datasheet notes red wavelength shifts measurably with LED temperature while IR is relatively
insensitive. The firmware exposes a `TEMP` command for this.

## 4. Timing: what the hardware permits

Maximum sample rate is limited by pulse width, because pulse width sets ADC resolution and so
conversion time. From the datasheet, **Table 11, SpO2 Mode (Allowed Settings)**, p.24 — which is
precisely our two-LED case:

| sps | 69 µs | 118 µs | 215 µs | 411 µs |
|---|---|---|---|---|
| 50 | ✓ | ✓ | ✓ | ✓ |
| 100 | ✓ | ✓ | ✓ | ✓ |
| 200 | ✓ | ✓ | ✓ | ✓ |
| **400** | ✓ | ✓ | ✓ | **✓** |
| 800 | ✓ | ✓ | ✓ | — |
| 1000 | ✓ | ✓ | — | — |
| 1600 | ✓ | — | — | — |
| **Resolution** | 15 bit | 16 bit | 17 bit | **18 bit** |

So at full 18-bit resolution, **400 sps is the ceiling** for two channels. The spec asks for
100 Hz output, which leaves that ceiling spendable on hardware averaging:

> **400 sps internal + `SMP_AVE = 4` → 100 Hz output per channel, 18-bit.**

That is what `config.h` does. It buys a ~2× noise reduction over running the ADC at 100 sps and
discarding the headroom — worth having, because the measurement is a slow DC trend where the
noise floor directly sets the onset threshold `μ₀ + 3σ₀`.

## 5. Chosen configuration

| Parameter | Value | Reason |
|---|---|---|
| Mode | SpO2 (`0b011`) | Exactly Red + IR; no slot setup needed |
| Pulse width | 411 µs | 18-bit, the maximum available |
| Internal rate | 400 sps | Hardware ceiling at 411 µs with 2 LEDs |
| `SMP_AVE` | 4 | → 100 Hz output per SPEC 3.1, with free noise reduction |
| ADC range | 4096 nA | 15.63 pA LSB, per SPEC 3.1 |
| LED current | `0x1F` (6.2 mA) both | Tune so IR DC lands 105k–157k counts; `IRAMP`/`REDAMP` |
| Green | unused | Section 2 |

## 6. What to watch on real data

- **IR DC in 105k–157k counts** (40–60 % of the 262 143 full scale). Headroom both ways matters
  here: the DC *moves* as the forearm fills, so a level that starts near the rail will clip
  mid-measurement.
- **Dark offsets ≤ ~128 counts** (SPEC 3.5). Higher means ambient light is leaking in, which adds
  a bias that `ΔOD` turns into a spurious volume reading. The logger warns automatically.
- **Pulsatility RMS during the reference window.** On a healthy teammate this will be clearly
  non-zero, which is the expected flag that onset reads near systolic, not MAP (SPEC §1).
- **IR-only vs two-wavelength on the same recording.** If they disagree substantially, suspect
  desaturation of the trapped blood — and trust the two-wavelength result.

---

## Sources

- [MAX30101 Datasheet (SparkFun mirror)](https://cdn.sparkfun.com/assets/8/1/c/9/0/MAX30101_Datasheet.pdf) — Table 11/12 allowed settings (p.24), mode config, FIFO format, red LED wavelength vs temperature (Table 13)
- [MAX30101 Register Map](https://psoc-components-library.readthedocs.io/en/latest/max30101_reg.html) — mode/LED mapping, ADC ranges, averaging
- [SparkFun Photodetector MAX30101 Hookup Guide](https://learn.sparkfun.com/tutorials/sparkfun-photodetector-max30101-hookup-guide/all) — board wavelengths, 0x57, onboard pullups, reflectance mode
- [Haemoglobin extinction coefficients — Prahl, OMLC](https://omlc.org/spectra/hemoglobin/summary.html) — the 660/880 nm values behind the two-wavelength solve
- [Modified Beer–Lambert law primer](https://fnirs.org/courses/introduction-to-fnirs/lessons/modified-beer-lambert-law-mbll/)
- [Analog Devices KB: multi-LED FIFO reads](https://support.analog.com/en-us/knowledgebase/article/000102527) — 3 bytes per active slot
- [Origin of Infrared Light Modulation in Reflectance-Mode PPG (PMC5074491)](https://pmc.ncbi.nlm.nih.gov/articles/PMC5074491/) — green 0.3–0.8 mm vs NIR 1.5–2.5 mm penetration depths
- [The Advantages of Wearable Green Reflected Photoplethysmography](https://www.researchgate.net/publication/45630585_The_Advantages_of_Wearable_Green_Reflected_Photoplethysmography) — the green AC/DC advantage that does *not* apply here
