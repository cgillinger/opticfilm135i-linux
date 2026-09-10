# Positioning after N2: the mapping, its variation, and how to make coverage robust

Status: offline analysis, 2026-09-10, written from the N2 empty-holder
runs (Test 56) before any positioning constant is changed.
`FEEDL_PITCH` is still 10760 and the base offset is unchanged; nothing
in this document is a decision, it is the ground for one.

The question N2 answered is not the one we set out to ask. We went in
to choose a pitch (10752? 10760? the measured ~10733?). The data says
the choice of an exact pitch is no longer the main problem: **the
transport lands differently on different loads, by more than the plain
3600 dpi window's whole margin**. The design question is therefore how
positioning becomes robust against that variation — and this document
keeps four things separate that the discussion so far has mixed:

1. the holder's fixed aperture geometry (plastic; does not vary),
2. the *mean* mapping from commanded FEEDL to where the aperture
   lands in the delivered image,
3. the *variation* of that mapping between separate loads,
4. the margin actually required for the real 35 mm image area.

## 1. The data

Three empty-holder runs (`empty-a/b/c`, 2026-09-10), each from its own
power cycle and its own load, holder taken out and reinserted, frames
1–6 at 600 dpi. Plus N1 (Test 55, same command, film in the holder) as
a fourth load for the mapping question, since the fiducial is the
plastic edge in both. Raw data and JSON reports: private analysis
area, `holder-20260910/` (N1: `holder-20260909/`).

All 18 empty frames delivered, aperture at saturation across the whole
window, one sharp plastic trailing edge in every frame. The tool's
fiducial is that trailing edge; the leading edge was outside the
window in all 18 frames (see §3), so the aperture's own length is
*still* unmeasured — the sweep's 35.80–36.12 mm (holder-geometry.md
§2) remains the source for it.

**Fiducial (aperture trailing edge) in motor units, per frame and load:**

| frame | N1 (film) | empty-a | empty-b | empty-c | range (mm) |
|---|---|---|---|---|---|
| 1 | 11698.1 | 11670.1 | 11660.7 | 11698.9 | 0.135 |
| 2 | 22394.1 | 22397.5 | 22407.5 | 22427.9 | 0.119 |
| 3 | 33118.1 | 33115.7 | 33160.4 | 33173.8 | 0.205 |
| 4 | 43848.4 | 43827.0 | 43874.9 | 43920.1 | 0.328 |
| 5 | 54600.3 | 54551.6 | 54593.9 | 54669.0 | 0.414 |
| 6 | 65377.3 | 65312.4 | 65309.3 | 65411.7 | 0.361 |

Free-pitch fits per run: 10725.3 / 10729.0 / 10743.8 (N1: 10735.6),
max residuals 0.029–0.084 mm. Neither candidate constant fits any run:
the better one (10752) leaves 0.10–0.25 mm where the free fit leaves
0.03–0.08 mm. Measurement resolution is ±0.025 mm per edge
(holder-geometry.md §1), an order below everything discussed here.

## 2. The four things, separated

### 2.1 The holder's geometry is fixed, regular — and now linear by construction

The sweep measured the apertures optically (35.80–36.12 mm, crossbars
1.90–2.00 mm, constant spacing to 0.13 mm). N2 adds the stronger
statement: **the per-frame means over the three empty loads lie on a
straight line to ≤ 0.022 mm.** Whatever looked like a bow or arc in
the residuals of any single run (including N1) is not the holder — the
per-load deviations below carry it. A per-frame position table
therefore has nothing to describe: the geometry is a base plus a
constant pitch, full stop.

One unresolved side observation: in the frames where the *next*
aperture's leading edge entered the window (empty-a f3–f5), the gap
between apertures reads 1.42–1.50 mm, where the sweep read the
crossbars as 1.90–2.00 mm. A ~0.5 mm disagreement between two optical
measurements of the same plastic is flagged, not explained; nothing
below turns on it.

### 2.2 The mean mapping: pitch 10732.7, and the window sits 0.6–1.0 mm late

Fitting one line to the per-frame means of the three empty runs:

    fiducial(n) = 11678.3 + (n−1) × 10732.7        [1/7200 in units]

Two properties of today's constants, measured against that:

- **Effective pitch ≈ 10733, not the commanded 10760** (or the
  vendor grid's 10752). Note what is and is not claimed: the vendor's
  *commands* step by exactly 10752 (holder-geometry.md §3); what N2
  measures is the end-to-end result — commanded FEEDL to aperture
  position *in the delivered image*. That end-to-end figure is the
  one coverage depends on, and it is 10733. Decomposing the 19-step
  gap into motor scale versus stopping behaviour is not needed for
  any decision here.
- **The window sits late on the aperture in every frame**: by
  0.57 mm at frame 1, growing linearly to 1.03 mm at frame 6 (the
  growth is exactly the pitch error accumulating). This is the
  N1 window-offset finding, now confirmed 18/18 and quantified. It
  is a *mean* error: a base-offset plus pitch correction removes it.

### 2.3 The variation between loads: ±0.24 mm observed, growing with travel, not the film

Deviation of each load from the per-frame mean mapping (mm):

| load | f1 | f2 | f3 | f4 | f5 | f6 | max |
|---|---|---|---|---|---|---|---|
| empty-a | −0.023 | −0.048 | −0.121 | −0.166 | −0.188 | −0.113 | 0.188 |
| empty-b | −0.056 | −0.012 | +0.037 | +0.003 | −0.039 | −0.124 | 0.124 |
| empty-c | +0.079 | +0.060 | +0.084 | +0.163 | +0.226 | +0.237 | 0.237 |
| N1 (film) | +0.076 | −0.060 | −0.112 | −0.090 | −0.016 | +0.116 | 0.116 |

- Each load has its own signature: a sits short, c long, and the
  deviation **grows with travel** — the per-load *pitch* varies
  (10725→10744), not only the base. So this is transport behaviour
  over distance, not just an imprecise starting reference.
- N1, with film, sits inside the same band: the variation is not
  film-related.
- The earlier "±4 lines = ±0.028 mm" repeatability figure
  (Tests 17–23) was repeats at frame 1; it is consistent with the
  f1 column here. What it never measured is the growth over travel,
  which is where the real budget goes.
- **n = 3 (4) loads. The observed ±0.24 mm is a floor, not a
  ceiling.** The design number used below is **±0.5 mm at frame 6**
  (roughly twice the observed worst deviation); every option is
  judged against that worst case, not against a mean or a best fit.

### 2.4 The margin the real image area needs

The visible image is the intersection of the film frame (nominally
36.0 mm along the strip for 24×36) and the aperture (35.80–36.12 mm,
mean 35.92). The film frame's position inside the aperture varies by
about a millimetre between strips (how the roll was shot and cut), so
the only *guarantee* that covers every strip is covering the aperture
itself; against a nominal, centred 24×36 frame the requirement is
0.1–0.2 mm milder. Both are shown; the practical acceptance is N3's
six-frame colour negative, judged by eye under the production-image
rule.

Required delivered window = target length + 2 × worst-case placement
error (±0.5 mm design):

| target | length | required window |
|---|---|---|
| nominal 24×36 frame | 36.0 mm | **37.0 mm** |
| mean aperture | 35.92 mm | 36.92 mm |
| longest aperture (f1) | 36.12 mm | **37.12 mm** |

Against what the profiles deliver today (holder-geometry.md §6),
*assuming the mean mapping is corrected first* (§2.2):

| profile | delivered | vs 37.12 mm required |
|---|---|---|
| plain 3600 | 36.08 mm | **−1.04 mm — cannot cover even at zero error** (it is already shorter than aperture 1) |
| dual 3600 (IR) | 37.03 mm | −0.09 mm |
| dual 600 | 37.17 mm | +0.05 mm |
| dual 1200/2400/7200 | 37.31–37.42 mm | +0.19…+0.30 mm |

Plain 3600 is not fixable by any positioning precision: the window is
smaller than the largest aperture. The dual profiles sit at ±0.1 mm of
the requirement — inside the observed variation, i.e. "it fits" would
be luck, exactly what the definition of done forbids.

## 3. The design options

Judged against worst-case load variation (±0.5 mm at frame 6) and
actual image coverage — not best fit or mean error.

### A. Corrected constant pitch + base offset

Set pitch ≈ 10733 and move the base ≈ 0.57 mm earlier (exact constants
to be decided from this document; the fitted line in §2.2 gives them).
This centres the *mean* mapping and stops the error growing along the
holder. It changes the wire for every frame (base) — frames 2–4's
hardware verification reopens, per the rule in holder-geometry.md §5.

**What it does not do:** absorb the load-to-load variation. After A,
the worst case is still ±0.5 mm of placement, and §2.4's table already
assumes A. **Necessary, not sufficient.**

### B. Explicit per-frame position table

Rejected on the data: the per-frame means are linear to 0.022 mm
(§2.1), so a table has no information to add over A — and, explicitly:
**a per-frame table does not touch load-to-load variation either**,
because the table would be a *mean* per frame and the variation is per
*load*. It would be six constants doing the work of two, with the same
residual risk.

### C. Overscan + host-side crop

Deliberately scan a longer window than the nominal image area and crop
on the host. This attacks the variation itself: if the window is
longer than target + 2×worst-case, the whole image area is inside the
scan on *every* load, and precision stops being the load-bearing wall.

**Protocol feasibility — derived, not guessed:**

- The line count is data length, not motor programming: registers
  0x25:0x26:0x27 in the scan batch plus the chunk count
  (protocol-notes.md; `of135i/device.py` already takes `lines=` as a
  parameter and rounds to whole chunks). The motor's slope tables
  (0x7e–0x92), exposure and mode bytes are per-dpi and are not
  functions of the line count.
- **The vendor has already run the dual profiles with far longer
  windows on this very unit**: every per-resolution capture is a
  whole-strip scan — 12378 programmed lines at 600 dpi
  (`tables_dpi600.CAPTURED_LINES`), 48390 at 2400, ~254 mm of travel
  in scan mode 0x30 with the same per-dpi tables. Overscan of a
  millimetre or two per side is a *shorter* window than the vendor's
  own, at the same profile. For the dual profiles this is
  vendor-demonstrated territory, not extrapolation.
- **Plain 3600 is the one exception**: 5137 lines is the only value
  ever observed for it (the vendor's long scans are dual). The
  mechanism is identical (same registers, chunked the same way, the
  engine completes when all chunks are read), but a longer plain-3600
  window is a new data point and gets **one hardware A/B** before it
  is relied on — not adopted blind.
- **Travel bound**: the scan pass moves while reading, so the end
  position is FEEDL + window travel. The bound is the load traverse,
  FEEDL 71490 (~252 mm), performed on every load; the vendor's strip
  scans end essentially there. Frame 6 with corrected base and
  +0.75 mm/side overscan ends at ≈ 71 000 in plain 3600 and
  ≈ 71 100 in dual 600 — inside the bound, checked before any run
  via the existing `holder.check_feedl` logic extended to the *end*
  position, not only the stop position.
- **Plastic in the raw image is a non-problem**: every correctly
  positioned frame already contains slivers of crossbar
  (holder-geometry.md §6); dark plastic photographs like dense film
  and touches neither calibration (which happens at the calibration
  position, before the move) nor exposure.

**Sizing**: +0.75 mm per side covers the ±0.5 mm design worst case
with 0.25 mm to spare on top of A's centring. That is +106 lines/side
at plain 3600 (wire 5137 → ~5350, +4 %) and +18 physical lines/side
at dual 600 — in practice rounded up to whole chunks, which at 600 dpi
makes the quantum ~1 mm/side. Cost: a few percent more data and scan
time.

**The crop**: with overscan, *both* aperture edges are inside every
delivered frame, at half-level crossings the geometry tool already
resolves to 0.025 mm. The host crops to the aperture (or to a fixed
24×36 inside it), keyed on the measured plastic edges — so the output
is registered to the holder itself, identical across loads *by
construction*. This is the "free registration reference" of
holder-geometry.md §6, made total. The crop is deterministic image
geometry on the host, not a motor behaviour, and the raw uncropped
data can always be kept — the raw-data principle is untouched.

### D. Dynamic per-load reference

The vendor's own answer is a variant of this: its scan-pass FEEDL
values are film-detected per frame from a preview pass, which is why
they scatter around the grid. Two shapes for us:

- **(i) Host-side, within a load**: measure the aperture edge in
  frame *k*'s delivered image, correct the FEEDL of the *later*
  frames of the same batch. No new protocol, no extra motor moves,
  FEEDL still bounds-checked. But N2 shows the per-load deviation has
  a slope (it grows with travel), so a frame-1-referenced correction
  removes only the base component (±0.08 mm at f1) and progressively
  refitting the slope mid-batch re-introduces exactly the
  chase-the-measurement complexity the holder model was built to
  avoid.
- **(ii) A vendor-style preview/detection pass**: real machinery
  (a new pass, new state), for a problem C solves statically.

Kept as a recorded option if more loads ever show variation beyond
the overscan budget; not proposed now.

## 4. Overscan geometry, derived (leading and trailing are separate controls)

Decision taken 2026-09-10: **A + C**. Before implementation, two things
the owner asked to be made explicit. This section is the first.

**The two controls are not the same axis.** The scan moves forward
(increasing motor position) while it reads. So the window is
`[start, end]` with:

- **`start` set by FEEDL** — where acquisition begins. Continuous in
  motor units. Moving `start` *earlier* (more leading coverage) means
  **lowering FEEDL**.
- **`end` set by the line count** — `end = start + delivered_lines ×
  (7200/dpi)`. Increasing the line count extends the window **forward
  only** (trailing side), and it is quantised to whole image chunks.

Therefore a longer line count **cannot** add leading coverage: that is
purely a FEEDL move. And option A's base correction is *also* a FEEDL
move (≈0.57 mm earlier at frame 1, plus the pitch change along the
strip). The two must be booked separately or A's centring gets
miscounted as overscan. The implementation books them as three explicit
terms:

    FEEDL(n)   = base_corrected + (n−1)·pitch_corrected   (A: centres the mean)
               − leading_overscan                          (C: leading slack)
    line_count = ceil( ( aperture_len + leading_overscan + trailing_overscan
                         + 2·colour_crop ) / chunk_lines ) · chunk_lines
                                                            (C: trailing slack, rounded UP)

Rounding the line count **up** can only *add* trailing margin, never
remove it. The colour-line crop (`image.align_channels`, `shift` lines
each side: 12 at 3600, 49 at 600) is removed from the delivered image,
so the wire count carries `2·colour_crop` extra that the crop then eats
— it is added before rounding so the *delivered* window still meets the
target.

**Worked ledger** (mean mapping from §2.2; aperture lengths from the
sweep; per-load spread = observed max deviation from §2.3; target
overscan 0.75 mm/side). Positions in motor units (1/7200 in) from the
load reference.

*dual 600 (the profile N2 ran):*

| | frame 1 | frame 6 |
|---|---|---|
| aperture (lead … trail) | 1438 … 11677 | 55179 … 65344 |
| aperture length | 10238 u (36.12 mm) | 10165 u (35.86 mm) |
| observed load spread | ±22 u (±0.079 mm) | ±67 u (±0.237 mm) |
| **A+C** window | 1226 … 12986 | 54967 … 66727 |
| wire lines (chunks) | 1078 (11) | 1078 (11) |
| delivered lines | 980 | 980 |
| mean-load margin lead / trail | +0.750 / +4.618 mm | +0.750 / +4.877 mm |
| worst observed-load lead | +0.671 mm | +0.513 mm |
| design −0.50 mm lead / trail | +0.250 / +4.118 mm | +0.250 / +4.377 mm |
| window end vs bound 71490 u | 12986 (206 mm spare) | 66727 (16.8 mm spare) |

*plain 3600 (the tight profile, and the one that needs C most):*

| | frame 1 | frame 6 |
|---|---|---|
| aperture (lead … trail) | 1438 … 11677 | 55179 … 65344 |
| aperture length | 10238 u (36.12 mm) | 10165 u (35.86 mm) |
| observed load spread | ±22 u (±0.079 mm) | ±67 u (±0.237 mm) |
| **A+C** window | 1226 … 11896 | 54967 … 65591 |
| wire lines (chunks) | 5359 (233) | 5336 (232) |
| delivered lines | 5335 | 5312 |
| mean-load margin lead / trail | +0.750 / +0.772 mm | +0.750 / +0.869 mm |
| worst observed-load lead | +0.671 mm | +0.513 mm |
| design −0.50 mm lead / trail | +0.250 / +0.272 mm | +0.250 / +0.369 mm |
| window end vs bound 71490 u | 11896 (210 mm spare) | 65591 (20.8 mm spare) |

Reading the ledger:

- **Both sides get ≥ 0.75 mm on the mean load, ≥ 0.51 mm on the worst
  observed load, ≥ 0.25 mm even at the ±0.5 mm design worst case** —
  at every frame, in both profiles, *after* chunk-rounding and *after*
  the colour crop. The leading 0.75 is exact (continuous FEEDL); the
  trailing is ≥ 0.75 (rounded up — huge on dual 600, whose chunk is
  ~2 mm, tight on plain 3600, whose chunk is 0.16 mm).
- **The end position stays inside the transport bound** (71490 u, the
  load traverse the machine performs every load): the worst case is
  frame 6, ending 16.8 mm (dual) / 20.8 mm (plain) short of it. The
  implementation range-checks the *end* position, not only the stop,
  extending the existing `holder.check_feedl` guard.
- Plain 3600's numbers confirm §2.4: its *current* window is shorter
  than aperture 1, so C is not an enhancement there, it is the only
  thing that makes plain-3600 whole-aperture delivery possible at all.

The illustrative constants used here (base 11678.3, pitch 10732.7) are
the §2.2 fit; the final adopted constants are the decision, and the
ledger is regenerated from them by `tools/holder_geometry.py overscan`
(added with the implementation) so the guarantee is checked against the
numbers actually shipped, not against these.

## 5. Edge verification: coverage is proven per scan, not assumed

The second thing the owner asked to be explicit, and the reason C is
robust rather than merely hopeful: **the ±0.5 mm design worst case is
an assumption from n=3 loads, not a measured ceiling.** Overscan sized
to it is a bet. The bet is made safe by *checking the aperture's own
edges in every delivered image* — the registration reference is in the
data, so each scan proves its own coverage instead of trusting the
budget.

**The rule.** After a scan, the host locates both plastic aperture
edges in the overscanned image (the half-level crossings the geometry
tool already resolves to 0.025 mm):

- **both edges found, each with ≥ `min_margin` of overscan beyond it**
  → coverage verified for *this* scan; crop to the aperture (or to a
  fixed 24×36 registered inside it) and deliver.
- **an edge missing, or found with less than `min_margin` slack** →
  the window did not contain the whole aperture on this load. Do
  **not** deliver a silently-clipped frame as if complete: flag it,
  and in a batch fail that frame closed (same discipline as the
  residual-dark_b substitution — a suspect frame is never dressed up
  as a good one). The raw overscan data is kept regardless, so a
  flagged frame can be inspected, not just discarded.

This is what turns C from "trust ±0.5 mm" into "verify per scan and
fail loudly if the transport ever exceeds the budget". If a real load
ever shifts more than the overscan, we find out from the image, not
from a customer.

**Does the edge survive a real colour negative?** The edge is
plastic-vs-open on the empty holder (≈780 vs ≈39800 counts — trivial),
but N3 puts film across the aperture, and plain 3600 has no IR pass to
fall back on. Assessment:

- **Dual / IR (600–7200):** trivial. The IR pass sees the near-
  transparent film base as bright and the plastic as dark regardless
  of the picture; the edge is a clean step in the IR channel, which is
  exactly the channel `holder_geometry.py` already measures. No image
  content can weaken it.
- **Plain 3600 (no IR):** still robust, for a specific physical
  reason. The aperture edge does not fall on picture content — it
  falls on the **inter-frame rebate**, the clear film base between
  photographed frames. Clear C-41 base passes strong light in the red
  channel (the orange mask is a red-pass filter); against the plastic
  floor (~780) that is a several-fold step in R at the very edge, with
  no image detail there to erode it. The detector uses the red channel
  (or luminance) with the per-scan adaptive threshold the tool already
  computes, at the outer overscan region where base — not image — is
  guaranteed to sit.
- **Evidence it already works on film:** N1 (Test 55) was run *with a
  negative in the holder* and the trailing plastic edge was still
  found in all six frames (that is where its fiducials came from); the
  lit level ran 7300–23300 counts for film against ≈780 for plastic, a
  ~10× step. Edge detection on film is not a hypothesis; it is
  demonstrated. What N3 adds is confirming the **leading** edge too
  (now that overscan brings it into frame) and confirming plain 3600
  specifically.
- **Failure handling covers the residual risk:** if a particular
  frame's edge is genuinely ambiguous (an unusually dense rebate, a
  scratch), the min-margin rule flags rather than mis-crops. The floor
  is set from the empty-holder and N1 contrast; N3 confirms it against
  the real negative and is where the threshold is finalised.

So edge verification is designed for both the empty holder and the
real negative, is already demonstrated on film for the trailing edge,
and fails safe where it cannot decide. Confirming it on N3 — both
edges, both profiles — is part of that milestone, not a precondition
that blocks implementation.

## 6. Recommendation

**A + C combined; B rejected; D parked.** Chosen by the owner
2026-09-10, with §4 (overscan geometry) and §5 (edge verification)
made explicit as conditions on the implementation.

1. **Correct the mean** (option A): pitch ≈ 10733, base ≈ 0.57 mm
   earlier — final constants read off §2.2's fit when the decision is
   taken.
2. **Make coverage robust with overscan + aperture-registered host
   crop** (option C): 0.75 mm/side (leading exact via FEEDL, trailing
   ≥ target chunk-rounded up — §4), both edges brought into every
   frame, deterministic crop registered on the plastic edges.
   Leading and trailing are booked as separate terms so A's centring
   is not miscounted as overscan (§4). For the dual profiles the
   longer window is inside vendor-demonstrated behaviour; plain 3600
   gets one hardware A/B for the longer window before it is promised.
3. **Verify coverage per scan** (§5): both aperture edges must be
   found with ≥ min_margin slack, else the frame is flagged / failed
   closed, never delivered as silently clipped. This is what makes the
   ±0.5 mm sizing safe against an unknown true worst case. The raw
   overscan image is always kept; crop is a deterministic host-side
   operation on top of it.
4. Plain 3600 **requires** C regardless of anything else: its window
   is smaller than aperture 1, so without overscan it cannot deliver
   the whole opening even on a perfect stop.

Why this and not "more precision": the transport's own load-to-load
behaviour is the noise floor, it is ±0.2 mm observed at n=3–4 and
grows with travel. No constant — one, or six — gets underneath it.
Widening the window and registering on the plastic makes the observed
variation irrelevant at 3× the observed worst case, with margin that
is *engineered* rather than lucky.

**What a decision triggers** (unchanged rules): changing pitch/base
reopens frames 2–4's hardware verification and re-verifies all six on
the empty holder (one load suffices — the variation is now
characterized); the overscan window gets its A/B at plain 3600; and
the acceptance remains N3, the six-frame colour negative, judged by
eye as a production image under the 2026-09-10 acceptance rule.

**Decided here: nothing.** FEEDL_PITCH stays 10760 and the base
stays until the owner has read this and chosen.
