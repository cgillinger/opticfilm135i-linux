---
Last-Updated: 2026-08-30
---

# IR-enabled scan (segment 04) — line structure analysis

## Summary

Line period = **5184 px × 3 channels (RGB, pixel-interleaved) = 15552
u16 samples = 31104 bytes/line**. Lines **alternate** line-by-line:

- **even line index (0, 2, 4, …) = IR pass**: R, G, B samples are
  (near-)identical to each other — the raw pipe broadcasts the single
  IR photodiode reading into all three channel slots. Image is bright
  and almost flat except for small dark specks (dust/scratches).
- **odd line index (1, 3, 5, …) = visible pass**: normal RGB negative
  image, R/G/B clearly separated (orange-mask-like channel offset).

This is candidate (a) from the task (alternating exposure), but at a
pixel width of **5184**, not 3762 as in the plain visible-only scan
(segment 03). Why the width differs is not established (same DPI
setting; possibly VueScan requests a wider capture area in "64 bit
RGBi" mode) — noted as open question below.

## Extraction

`cal-data/ir/04-image.raw` (327,960,576 bytes) was built from segment
04's bulk-IN (EP 0x81) `usb.capdata`, starting at frame 4340 — the
first `0000001000980700` control descriptor (target `0x10000000`,
length `0x00079800` = 497,664 B). Extraction script:
`cal-data/ir/extract_raw.py` (streams tshark's hex-field output
straight to bytes, never holding the full hex text in RAM).

There are 660 such descriptor reads total in segment 04, but only
**659 completed**: the 660th (frame 47834) is immediately followed by
a `0x40/0x0c/0x008d` cancel-style command with no bulk-IN data at all
— the scan legitimately ends after 659 chunks, it isn't a capture
truncation. `659 × 497,664 = 327,960,576` matches the extracted file
size exactly, confirming the full (complete) image payload was
captured.

## How the period was found

The naive assumption (width 3762, same as segment 03, either
alternating lines, sequential visible/IR blocks, or 4-channel RGBI —
task candidates a/b/c) all produced pure diagonal-banded noise when
reshaped: the byte-count math also rules them out. Segment 03's chunk
size (519,156 B) factors as `2² · 3³ · 11 · 19 · 23` — an exact
multiple of `22572 B = 3762 px × 3 ch × 2 B` (23 lines/chunk),
confirming the driver reads whole lines per bulk descriptor. Segment
04's chunk size (497,664 B) factors as **`2¹¹ · 3⁵` only** — it has
*no* factor of 11 or 19, so it can never be an exact multiple of any
line size that includes a factor of 3762. The true width could
therefore not be 3762 (or any multiple of it) in this capture.

A brute-force line-period search (Pearson correlation between rows *i*
and *i+2*, skipping the alternating parity, maximized over candidate
pixel widths, RGB-interleaved) peaked broadly around width ≈ 2600 in
several regions of the file. Refining against the exact-divisor
constraint of the chunk size (`2¹¹·3⁵`) pinpointed **width = 5184**
(`= 2⁶·3⁴`, line = 31,104 B) as the only nearby value giving:

- **zero remainder**: `327,960,576 / 31104 = 10544` lines exactly.
- **clean chunk framing**: `497,664 / 31104 = 16` lines/chunk exactly,
  and `659 chunks × 16 = 10544` — consistent with the observed file
  size to the byte.
- a reshaped image with **no diagonal shear and no tiling artifact**.
- near-perfect even/odd line separation into IR vs. visible content
  (see below) — the smoking gun.

Register 0x26:0x27 showed 0x297e = 10622 total lines for the IR pass
(vs. 0x1411 = 5137 for the single visible pass in segment 03); 10622
vs. our 10544 captured lines differs by 78 (~0.7%), plausibly a few
extra calibration/overscan lines the register counts that aren't part
of the 659 completed bulk reads — the same kind of small slack seen in
segment 03 (5137 registered vs. 5129 lines that evenly divide the
captured bytes).

## Evidence

- `preview_even_IR.png` — even lines only (downsampled 8×): flat,
  near-white, sprocket-hole margins dark on both sides, a handful of
  small dust/scratch specks. Matches the task's expected IR look.
- `preview_odd_visible.png` — odd lines only (downsampled 8×):
  recognizable normal color-negative photo (child on a street, poles,
  tree) — visually the same frame as
  `../../decoded-frame1-positive-preview.png` from segment 03.
- `line-means.csv` — per-line R/G/B mean & std for the first 200
  physical lines (both parities interleaved), with a `parity` column.
- Full-dataset per-channel stats (not just first 200 lines, which are
  still in the film-leader transient):

  | | mean R | mean G | mean B | std R | std G | std B |
  |---|---|---|---|---|---|---|
  | even (IR) | 33864 | 33983 | 33928 | 24623 | 24561 | 24666 |
  | odd (visible) | 13963 | 6989 | 5576 | 11893 | 5190 | 3680 |

  Even lines: R≈G≈B (IR broadcast into all 3 slots). Odd lines: clear
  R>G>B separation typical of a color negative under visible light.

## Byte-count confirmation

- `327,960,576 bytes = 10544 lines × 31104 bytes/line` exactly (no
  remainder).
- `10544 lines / 2 = 5272` IR lines + `5272` visible lines.
- `10544 = 659 chunks × 16 lines/chunk` exactly.

## Open questions / unexpected findings

- **Width changed from 3762 (segment 03) to 5184 (segment 04)**, a
  factor of ~1.378×, for nominally the same 3600 dpi setting. Not yet
  explained — could be a wider capture area requested by VueScan for
  the combined RGBI/dust-removal pass, extra dummy columns, or a
  genuinely different pixel clock/binning in this mode. Needs a
  register diff (0x26/0x27 pairs with a *width* register, if any) to
  pin down further.
- The 660th bulk descriptor request is issued but cancelled
  (`0x40/0x0c/0x008d`) rather than completed — worth keeping in mind
  for the driver: always be ready for a final speculative/read-ahead
  request that yields no data.
- Register-implied total lines (10622) vs. actually captured (10544)
  differ by 78; not investigated further here.
