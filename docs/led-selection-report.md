# LED Channel Selection for the MAX30101 — Research Report

**Question asked:** which LED channel(s) on the MAX30101 give the most accurate starting point for
measuring change in blood volume?

**Short answer:** run **Green (SLOT1) + IR (SLOT2)** in Multi-LED mode, at 411 µs pulse width
(18-bit), 400 sps internal with 2× hardware averaging → **200 Hz output per channel**.

Green is very likely to be your best single channel, but the evidence for that is strong in
*reflectance/wrist/HR* contexts and weaker in the *waveform-morphology* context you actually care
about. Running both costs essentially nothing here, so the design measures the answer instead of
assuming it. Section 6 says how to decide empirically after one session.

---

## 1. The constraint that rules out the obvious choice

The MAX30101 has three operating modes, and **which LEDs are available is a property of the mode**:

| Mode | `MODE[2:0]` | Active LEDs |
|---|---|---|
| Heart Rate | `0b010` | **Red only** |
| SpO2 | `0b011` | Red + IR |
| Multi-LED | `0b111` | Any of Green / Red / IR, in up to 4 time slots |

The trap: on the MAX30101, **"Heart Rate mode" is Red-only, not Green-only.** People coming from
the MAX30102 (which has no green LED at all) routinely assume HR mode is the green channel. It is
not. Red is the *worst* standalone choice for a blood-volume waveform — it sits on the steep part
of the haemoglobin absorption curve, so it is highly sensitive to SpO2 and perfusion changes that
have nothing to do with the volume signal you want.

**Therefore: any design that uses green must use Multi-LED mode.** That is the mode this project
uses, and it is why the firmware configures slots explicitly rather than calling a convenience
`setup()`.

In Multi-LED mode each enabled slot emits 3 bytes into the FIFO, and disabled slots emit nothing —
no dummy bytes. Two active slots = 6 bytes per sample.

## 2. Wavelengths on this specific board

SparkFun SEN-16474 (Photodetector Breakout - MAX30101, Qwiic):

| Channel | Wavelength | Register for current |
|---|---|---|
| Green | 530–545 nm | `LED3_PA` (0x0E), also `LED4_PA` (0x0F) |
| Red | 650–670 nm | `LED1_PA` (0x0C) |
| IR | 870–900 nm | `LED2_PA` (0x0D) |

Note LED3 and LED4 are **both wired to green** internally.

**The sensor is reflectance-mode, always.** The LEDs and the photodiode are co-planar on the same
face of the package. Even with a fingertip pressed onto it you are *not* doing transmissive pulse
oximetry — light goes in, scatters, and comes back out the same surface. This matters, because most
of the "IR is the gold standard" intuition comes from transmissive fingertip pulse oximeters, and
that intuition does not transfer cleanly.

## 3. The case for Green

- **Best AC/DC ratio.** Green yields a higher AC/DC ratio (≈ perfusion index) than blue, red, or
  IR. More pulsatile signal per unit of DC offset means more of your 18-bit ADC range is spent on
  the part you care about.
- **Best SNR in reflectance.** Reflectance-mode green/blue has been measured at roughly **+8 dB
  SNR** over transmission-mode PPG. Green shows the highest agreement with simultaneous ECG.
- **Shallow penetration is a feature, not a bug.** Green reaches ~0.3–0.8 mm — superficial dermis.
  It therefore does not integrate signal from deep tissue, large veins, or bone, all of which add
  low-frequency wander and respiratory coupling without adding pulse information.
- **Robust to confounders.** Less affected by temperature extremes and by oxygenation changes than
  IR, and markedly less motion-sensitive.
- **Sharper dicrotic notch.** Notch definition degrades as wavelength increases — which directly
  helps the `d²V/dt²` (APG) analysis, where the notch shows up as the e-wave.

## 4. The case for IR — and the caveats on the Green evidence

Being honest about where the green literature overreaches:

- **The "notch disappears in IR" result is at 1064 nm, not 880 nm.** That is a much longer
  wavelength than this part's IR LED. The trend is real but you cannot read the magnitude of that
  finding across to the MAX30101's 870–900 nm emitter.
- **Most green-superiority work targets HR/HRV extraction at the wrist under motion.** Your use
  case is a stationary fingertip measuring waveform shape. That is the scenario where green's
  headline advantage — motion robustness — is worth the least.
- **IR samples the vasculature people mean when they say "arterial."** IR penetrates ~1.5–2.5 mm
  versus green's ~0.3–0.8 mm. Arteries typically sit deeper than 3 mm, so *neither* reaches them
  directly, but IR integrates a larger and deeper vascular bed.
- **IR dominates the cuffless-BP and morphology literature.** If this prototype ever grows toward
  MAP estimation, IR is the channel most published feature sets were derived on.

**An important mechanistic wrinkle:** recent work argues that neither green nor IR primarily
measures arterial blood volume directly. Instead both appear to track *elastic deformation of the
dermis* driven by transmural arterial pressure, which compresses the capillary bed and modulates
absorption. Green and IR responses correlated at **r = 0.85**, suggesting "the same signal read
independently of penetration depth." If that holds, the depth argument for IR is substantially
weaker than the textbook account implies — and the practical win goes to whichever channel has
better SNR, which is green.

This is also a quiet caution about the project's framing: what you are recording is a robust
*correlate* of pulsatile blood volume, not a calibrated volume measurement.

## 5. Timing: what the hardware actually permits

From the datasheet (Table 11 / Table 12, p.24), the maximum sample rate is limited by pulse width,
because pulse width sets ADC resolution and therefore conversion time:

**SpO2 mode (2 active LEDs) — the correct proxy for our 2-slot Multi-LED config:**

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

So with two channels at full 18-bit resolution, **400 sps is the ceiling.** That lands exactly on a
useful configuration:

> **400 sps internal + `SMP_AVE = 2` → 200 Hz output per channel, 18-bit, two channels.**

This gives the 200 Hz you asked for *and* buys a free √2 noise reduction from hardware averaging,
rather than simply running the ADC at 200 sps and discarding the headroom. The datasheet ceiling is
being spent on averaging instead of on an output rate we do not need.

Adding a third slot (Red) would tighten timing further and double nothing useful — Red is only
worth enabling if SpO2 becomes a goal.

## 6. How to settle it empirically

The design records both channels simultaneously through an identical pipeline, so after one good
session you can decide on measured evidence rather than literature. Compare:

1. **Perfusion index**, `AC_rms / DC_mean`, per channel. Higher is better. Expect green to win.
2. **Derivative SNR** — band power in 0.5–8 Hz over band power in 15–50 Hz, computed on `d²V/dt²`.
   This is the metric that actually matters, because the second derivative is where noise hurts.
3. **Dicrotic notch visibility** on the ensemble-averaged beat in `d²V/dt²`. Can you see a clean
   e-wave?
4. **Beat-to-beat consistency** of fiducial point timing.

`analyze.py` prints metrics 1 and 2 per channel. If green wins on all four, drop IR and spend the
timing budget on 400 Hz single-channel for sharper derivatives.

## 7. Chosen configuration

| Parameter | Value | Reason |
|---|---|---|
| Mode | Multi-LED (`0b111`) | Only mode that exposes green |
| SLOT1 | Green (LED3) | Best expected AC/DC and SNR |
| SLOT2 | IR (LED2) | Deeper bed; comparison channel; morphology literature |
| SLOT3/4 | disabled | Red adds no value without an SpO2 goal |
| Pulse width | 411 µs | 18-bit ADC resolution — max available |
| Internal rate | 400 sps | Hardware ceiling at 411 µs with 2 LEDs |
| `SMP_AVE` | 2 | → 200 Hz output, √2 noise reduction for free |
| ADC range | 4096 nA | 15.63 pA LSB; headroom for fingertip DC without clipping |
| LED current | 7.2 mA both (`0x24`) | Tunable live over serial; see design doc |

---

## Sources

- [MAX30101 Datasheet (SparkFun mirror)](https://cdn.sparkfun.com/assets/8/1/c/9/0/MAX30101_Datasheet.pdf) — Tables 11/12 allowed settings, mode config, FIFO format
- [MAX30101 Register Map](https://psoc-components-library.readthedocs.io/en/latest/max30101_reg.html) — mode/LED mapping, ADC ranges, slot encoding
- [SparkFun Photodetector MAX30101 Hookup Guide](https://learn.sparkfun.com/tutorials/sparkfun-photodetector-max30101-hookup-guide/all) — board wavelengths, 0x57, pullups, reflectance mode
- [Analog Devices KB: multi-LED FIFO reads](https://support.analog.com/en-us/knowledgebase/article/000102527) — 3 bytes per active slot
- [EngineerZone: disabled slots emit no dummy bytes](https://ez.analog.com/optical_sensing/w/documents/22169/in-multi-led-mode-if-slotx-in-the-max30101-is-disabled-will-i-receive-dummy-bytes)
- [Origin of Infrared Light Modulation in Reflectance-Mode PPG (PMC5074491)](https://pmc.ncbi.nlm.nih.gov/articles/PMC5074491/) — penetration depths, r=0.85 green/NIR correlation, dermal-deformation mechanism
- [The Advantages of Wearable Green Reflected Photoplethysmography](https://www.researchgate.net/publication/45630585_The_Advantages_of_Wearable_Green_Reflected_Photoplethysmography) — AC/DC ratio by wavelength
- [Comparison of Reflected Green Light and Infrared Photoplethysmography](https://www.researchgate.net/publication/23932320_Comparison_of_Reflected_Green_Light_and_Infrared_Photoplethysmography)
- [Wearable Photoplethysmography Devices — Charlton et al.](https://peterhcharlton.github.io/publication/wearable_ppg_chapter/Wear_PPG_Chapter_20210323.pdf)
- [PPG Signal Processing and Synthesis — Charlton et al.](https://peterhcharlton.github.io/publication/ppg_sig_proc_chapter/PPG_sig_proc_Chapter_20210612.pdf) — APG fiducial points
- [Determinants of PPG signal quality at the wrist (PMC12204546)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12204546/)
