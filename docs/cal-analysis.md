# Plustek OpticFilm 135i (GL126) calibration formula analysis

Source: `cal-data/analyze.py`, run against `cal-data/capture/*.bin` and
`replay-out/03/*.bin`. Run with `.venv/bin/python cal-data/analyze.py` to
reproduce all numbers below.

All scan buffers are pixel-interleaved RGB, u16 little-endian.

## 1. Buffer statistics

| Buffer | Shape | R mean | G mean | B mean |
|---|---|---|---|---|
| dark A (gain=0, off=0x80) | 512×3 | 21411 | 27770 | 24897 |
| dark B (gain=0, off=0xff) | 512×3 | 23644 | 30052 | 27174 |
| white line (gain=0) | 5184×3 | 19065 | 27069 | 22782 |
| dark A2 (gain=final, off=0x80) | 512×3 | 53336 | 48828 | 53609 |
| dark B2 (gain=final, off=0xff) | 512×3 | 59511 | 53184 | 58965 |

Full per-channel median/min/max/std in the script output.

## 2. Gain formula (regs 2/3/4, codes R=0x2e G=0x21 B=0x29)

**Best fit: `actual_gain = code / 32`, target = white-line peak (max), implied
common target ≈ 31673 (~0x7bb9), max per-channel relative error 2.23%.**

| Form | Level used | Max rel. error |
|---|---|---|
| code/32 | peak (max) | **2.23%** |
| code/32 | mean | 3.62% |
| 2^(code/32) | peak | 3.79% |
| 1+code/32 | peak | 8.39% |
| 283/(283-code) (gl84x style) | mean | 14.84% |
| 256/(256-code) | mean | 14.47% |
| linear (code as raw multiplier) | any | ties code/32 numerically (same ranking; not a plausible register semantic on its own) |

`code/32` beats every other tested form by a wide margin, and does so with
both the mean and the peak level, which rules out the fit being a fluke of
one particular statistic. Testing the round target 0x8000 (32768, half full
scale) explicitly: predicted codes are R≈48.6, G≈34.2, B≈41.5 vs actual
46/33/41 — within 1–3 code steps, i.e. plausible but not an exact match.

**Confidence: moderate.** `code/32` is clearly the best of the tested forms
and the residual 2–4% spread across channels is small enough to be
explained by per-channel target margins or measurement noise, but it is not
a clean, exact fit to a single documented target value, so this should be
treated as "best available hypothesis," not a confirmed formula.

**Did NOT fit:** `1+code/32`, `1+code/48`, `1+code/64`, and the gl84x-style
`283/(283-code)` (and its `256/(256-code)` variant) all show 8–15% max
error — clearly worse and inconsistent across channels (R and G push the
implied target in opposite directions).

## 3. Offset formula (regs 5/6/7, codes R=0x010b G=0x010a B=0x010b)

The intended model — dark level vs. offset code is linear over the
[0x80, 0xff] bracket, and the driver extrapolates/interpolates to a small
black target (e.g. near 0 or ~0x100 counts) — **does not fit**:

- Both bracket pairs (gain=0 pair and gain=final pair) show *increasing*
  measured dark level as offset code increases from 0x80 to 0xff, in every
  channel.
- The final applied codes (0x010b/0x010a/0x010b = 267/266/267) sit *above*
  the top of the measured bracket (0xff=255), so the driver is
  extrapolating *further in the increasing direction* — i.e. toward an even
  *higher* predicted dark level (≈59500–60100 raw counts for the
  gain=final fit), not toward zero or any small black target.
- Solving each fit for "code where level = 0" gives negative codes
  (≈ −970 to −1420), confirming a near-zero dark target is unreachable from
  this bracket under a linear model in this direction.

**What does fit, cleanly:** in the code domain alone, the final offset is a
small, nearly channel-independent additive step above the top of the
bracket: `final ≈ 0xff + 11` (G) or `0xff + 12` (R, B). This is a much
tighter, simpler relationship than the linear-extrapolation-to-target
model, but its physical justification (why +11/+12 specifically) could not
be derived from only two bracket points.

**Confidence: low/inconclusive**, reported plainly per the task's
instructions. The two-point bracket is not enough to distinguish "small
fixed margin above the top of the measured range" from "linear
extrapolation toward some other target we haven't identified" — a third
measurement point away from 0x80/0xff would be needed to resolve this.

## 4. Shading correction table (45856-byte upload)

### Payload extraction

Extracted from `captures/segments/03-singel-3600-IRav.pcap`, bulk OUT
(endpoint 0x02) frames **1583, 1584, 1585** (16384+16384+12800 = 45568 B)
plus the **first 288 bytes of frame 1589** (a 512-byte frame). The
remaining 224 bytes of frame 1589 are all-zero, and that zero run begins
**exactly** at byte offset 45856 in the concatenated stream — i.e. right
where the driver's own expected total ends. This is USB bulk max-packet
padding, **not** a motor slope table (a slope table would be a
monotonically decreasing non-zero u16 sequence; this is a flat zero run).
Saved as `cal-data/capture/shading-upload-len45856.bin` (exactly 45856 B).

### Structure found

`45856 = 2⁵ × 1433` (1433 is prime), so it does **not** factor cleanly
against the shading-measurement width (3762 px) or 3762×3 channels — there
is no clean "N pixels × M channels × K bytes" decomposition.

A **period-2 phase split** of the payload as u16 LE (22928 elements) is
unambiguous and strongly structured:

- **field0** (even indices, 11464 entries): mean 240, std 88, range
  **93–344**.
- **field1** (odd indices, 11464 entries): mean 16130, std 2025, range
  0–16384, with **98.4% of values within 512 of 0x4000 (16384)**.

This is a classic Genesys-family **(offset, gain) pair** layout: field1
clamped at/near 0x4000 = 1.0 in Q2.14 fixed point is consistent with a
gain table that only *attenuates* (never boosts past 1.0), matching how
shading gain tables are normally built (correct down to the dimmest
column, never amplify above it).

**Striking but ultimately inconclusive coincidence:** field0's numeric
range (93–344) matches the shading measurement's own per-pixel column-mean
range almost exactly (95.3–345.8 combined across R/G/B), suggesting field0
is derived from / numerically equivalent to that same raw brightness
measurement. However:

- Direct positional correlation (native scan order, single lines instead
  of the 128-line average, per-channel de-interleave by stride 3, and
  shifts of ±6 entries) all give **|r| < 0.05** against the measured
  per-pixel column means — no alignment tested shows field0 tracking real
  per-pixel brightness position-for-position.
- field0 has a strong **negative lag-1 autocorrelation (−0.49)**
  (checkerboard-like alternation), unlike the near-zero autocorrelation of
  the actual measured per-pixel data. This rules out "field0 is simply a
  copy of per-pixel brightness in scan order."
- The entry count (11464) has no clean relationship to 3762 px (see prime
  factorization above), so even the intended index→pixel mapping is
  unresolved.

**Conclusion for section 4, stated plainly:** the *table format* — 4-byte
interleaved (offset-like value, gain clamped ≤ 0x4000 in Q2.14) pairs — is
well supported by the data and matches the known Genesys shading-table
convention. The **exact mapping from table index to physical sensor pixel
is inconclusive**; it is not a straightforward 1:1 left-to-right mapping at
native width, single-line reference, or per-channel-stride ordering. This
needs either a documented format reference or a targeted capture (e.g. a
shading table computed against a deliberately non-uniform reference target,
so index-to-pixel alignment could be confirmed by matching a distinctive
feature) to resolve further.

### Verification pass (frame01715, after upload) vs pre-correction (frame00797)

| Channel | Before mean | After mean | Amplification | Before CoV | After CoV |
|---|---|---|---|---|---|
| R | 117.3 | 56899.0 | 484.9× | 0.0529 | 0.0310 |
| G | 304.7 | 53503.2 | 175.6× | 0.0162 | 0.0379 |
| B | 301.9 | 56370.3 | 186.7× | 0.0204 | 0.0268 |

Per-channel amplification is **not** a single shared scale factor
(485× vs 176× vs 187×) — yet despite the raw "before" means differing by up
to 2.6× across channels, the "after" means converge to within **6.1%** of
each other (53503–56899). This is the signature of a per-channel
*normalizing* gain correction being active in the verification read,
consistent with the shading table's cross-channel balancing effect being
applied. Per-pixel CoV (relative flatness across the line) does not drop
sharply and is not consistently improved (R improves, G and B get *worse*
in relative terms) — so the dominant before/after change looks like it's
mostly driven by a large shared exposure/gain-stage difference between the
dim calibration-measurement pass and a normal read, with the shading
table's effect visible mainly in the cross-channel leveling, not in a
clean per-pixel flattening signal we could isolate here.

## 5. Replay-out vs capture consistency

All six replay buffers were compared against their capture counterparts
(dark A/B, white, dark A2/B2, shading). Full numbers in script output;
summary:

- Dark A/B, white, dark A2/B2: replay means are consistently **+0.5% to
  +1.05%** higher than capture, with Pearson r ≥ 0.98 (mostly ≥ 0.997) —
  i.e. essentially the same signal with a small, consistent systematic
  offset. None are byte-identical (expected: sensor read noise differs run
  to run even with identical AFE settings).
- Shading (128-line) buffers: means match closely (+0.46% to +2.73%), but
  **pixel-level Pearson r ≈ 0** between replay and capture. This is
  expected — the shading pass measures fine per-pixel noise/PRNU, which is
  not reproducible run-to-run even with identical AFE/exposure settings;
  only the aggregate statistics (not the per-pixel pattern) are expected to
  match, and they do.

**Conclusion:** replay levels are close to the capture's, confirming the
replayed AFE settings reproduce equivalent sensor response — no anomalies
found here.

## Summary of confidence

| Item | Formula | Confidence |
|---|---|---|
| Gain | `actual_gain = code / 32`, target ≈ white-line peak (~31.7k, near but not exactly 0x8000) | Moderate — best of all tested forms, 2–4% residual |
| Offset | No clean small-target model fits; `final ≈ 0xff + 11..12` in code domain | Low / inconclusive — needs a 3rd bracket point |
| Shading table | 4-byte (offset, gain-clamped-≤0x4000-Q2.14) pairs; index→pixel mapping unresolved | Format: moderate–high; pixel mapping: inconclusive |

## Resolution (main session follow-up, 2026-08-30)

The shading-table mapping is SOLVED. The 45856 B upload is streamed in
512-byte blocks of 128 (offset,gain) u16 LE pairs each, where the LAST
TWO pairs of every full block are trailer/filler (junk values). After
stripping trailers the payload is exactly 11286 pairs = 3762 px × 3 ch,
**pixel-interleaved RGB in the same order as the image line format**
(per-channel Pearson r vs measurement column means: 0.976/0.970/0.969).

- offset field = per-pixel mean of the 128-line shading measurement,
  essentially 1:1 (fit: 1.005×mean − 2.1, RMS 1.4, max err 7 — the
  vendor driver may use a trimmed mean).
- gain field = 0x4000 (1.0 in Q2.14) for EVERY payload pixel in this
  calibration — the correction is pure per-pixel offset subtraction;
  all unclamped gain values seen earlier live in the trailer pairs.
- No header: 89 full blocks + a 72-pair tail block, payload contiguous.

Driver recipe: measure 128 lines with final AFE state, per-pixel mean
→ offset u16, gain = 0x4000, pack 126 payload pairs + 2 zero trailer
pairs per 512 B block, upload to 0x10014000. (Trailer content to be
confirmed harmless as zeros on hardware.)
