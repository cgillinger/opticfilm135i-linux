# 110 (Pocket Instamatic) support in the strip holder

Status: **offline-implemented**, 2026-09-19, **reviewed and corrected
2026-09-20** (§9.1: the crop cut 0.1–0.4 mm of picture on the trailing
edges because the edge refinement assumed a clear surround; §5: image
identity across placements was not implemented). `--film 110` runs on
saved data and on synthetic fixtures (`tests/test_film110.py`, in
`tools/release_check.py`'s core group) and has been checked against one
real hardware strip's saved scans (`tools/film110_check.py`, private
fixtures). **Hardware verification (Test 87, a second strip, run end to
end with no manual cropping) is pending** — see
docs/film-110-proposal.md §6 for the acceptance plan. Nothing here
changes the motor side: positioning, waits, calibration and the FEEDL/
overscan geometry are exactly as for 35 mm (docs/film-110-proposal.md
§2/§5).

## 1. What was measured (n = 1 strip, 2026-09-19, Test 86)

A four-frame 110 colour-negative strip scanned in the standard 35 mm
strip holder, three placements, one load each (docs/test-log.md Test 86).

| Quantity | Value | How measured |
|---|---|---|
| Film width | 16.0 mm (15.7–16.0 across 3 loads) | lateral edge of the film band, 600 dpi survey |
| Image, along transport | 17.2 mm (17.0–17.5, four images) | 1 mm rulers, 3600 dpi frames |
| Image, lateral | 13.0 mm (one image read 13.7; unexplained, see proposal §4.3) | same |
| Frame pitch | 25.5 mm (25.0/25.8/25.7 between consecutive images) | 600 dpi survey |
| Perforation | one rectangular hole per frame, ~1.5 x 2 mm, punched 0.6–2.1 mm inside the film edge that faces the open side of the aperture (the edge itself stays continuous through the hole); hole trailing edge 2.3 mm before the next image start, previous image end 3.4 mm before the hole leading edge | 600 dpi rulers, column profiles |
| Dark printed border | ~1.2 mm wide along the perforated edge, 0.8–2.0 mm inside it, reading 0.066 x air (the rail plastic reads 0.033 x air) | column profiles |
| Lateral position | film against the lower rail; far edge ~8 mm inside the 24 mm aperture; stable to ±0.1 mm across three loads | column profiles |
| Image lateral offset | image begins 2.0 mm inside the perforated edge (2.4 read by eye on the rulers; 2.0 fits the detector's tracked edge against the hand-read image edges on all four 3600 dpi frames) | 3600 dpi rulers + detector |

These agree with nominal 110 figures (13 x 17 mm image, 16 mm film); the
pitch and perforation lead are **this strip's own measurements** and are
not yet confirmed general (project rule: never build on one reference —
see Limitations below).

`of135i/holder.py` carries this as `FILM_110`, a `Film` record consumed
only by the detector (`of135i/film110.py`); it is image-side geometry,
not holder/positioning geometry, and never feeds a FEEDL, a wait or a
calibration step.

## 2. Why one placement is not enough

The aperture pitch is 38 mm (36 mm opening + ~2 mm bar); the film pitch
is 25.5 mm. With image 1 aligned to aperture 1, successive images sit at
phases 0, 25.5, 13, 0.5, 26, 13.5, 1, … mm inside their apertures. An
image needs a phase under about 18.8 mm to be whole; every third image
lands under a bar and cannot be recovered from that placement — the bar
is opaque.

## 3. The two-placement protocol (visual, no measuring)

- **Placement A** — image 1's left edge at aperture 1's left edge, a
  hair of clear film visible (exactly like loading a 35 mm strip).
  Yields images 1, 3, 4 (and 6, 7, …) whole; the bar between images 1
  and 2 splits image 2.
- **Placement B** — shift the strip half a film pitch (12.5 mm) so bar 1
  falls between images 1 and 2. Yields images 2, 4 (and 5, 8, …) whole.

## 4. The sag rule

A 16 mm strip is held only where it passes under plastic. A strip end
left free inside an aperture sags slightly out of the focal plane —
measured on image 4 of the test strip (Laplacian sharpness, three bands
along the frame):

| Placement | End's support | Sharpness (three bands) |
|---|---|---|
| A — end free in the aperture | unsupported | 2004 / 1775 / 1556 (softening) |
| B — end under the next bar | supported | 2780 / 3765 / 3399 (grain resolved) |

So take the strip's **last** image in the placement where its end sits
under a bar, and the **first** image in the placement where its start
does. The detector flags this (`FrameFind.free_end_near`) but cannot fix
it — it is a physical fact about the holder, not something software
corrects.

## 5. Numbering and image identity

An image's number is its **position on the strip**, derived from where
it was found and which placement the operator declared with
`--placement A|B` (required with `--film 110`):

    position  = (aperture − 1) × 37.947 mm + image start inside the aperture
    number    = round((position − phase) / 25.5 mm) + 1
    phase(A)  = 3.5 mm      (image 1's start inside aperture 1, §3)
    phase(B)  = 3.5 + 12.75 mm

So the same photograph gets the same number whichever placement,
resolution (the arithmetic is in millimetres) or subset of apertures it
was scanned in — that is what makes `a-image4.tiff` and `b-image4.tiff`
the *same* picture. Worked example from the 2026-09-19 strip (positions
as the detector measured them after the 2026-09-20 correction):

| Placement | Aperture | Image start | Position | Number | Residual |
|---|---|---|---|---|---|
| A | 1 | 3.53 mm | 3.5 mm | 1 | 0.0 mm |
| A | 1 | 28.85 mm (split by bar 1/2) | 28.9 mm | 2 | −0.2 mm |
| A | 2 | 16.17 mm | 54.1 mm | 3 | −0.4 mm |
| A | 3 | 3.70 mm | 79.6 mm | 4 | −0.4 mm |
| B | 1 | 15.73 mm | 15.7 mm | 1 | −0.5 mm |
| B | 2 | 3.26 mm | 41.2 mm | 2 | −0.6 mm |
| B | 2 | 28.74 mm (split by bar 2/3) | 66.7 mm | 3 | −0.6 mm |
| B | 3 | 16.32 mm | 92.3 mm | 4 | −0.5 mm |

The evening's first, unruled placement (strip 13 mm into aperture 1,
image 1 at 17.0 mm) is **not** at either phase (residual 13.5 mm against
A) and is refused — see below. The rounding tolerates ±6 mm, so the
visual placement rule of §3 has ample slack, and a 0.3 mm pitch error
per frame (this strip's pitch is n = 1) would take ~20 frames to reach
it.

Rules, all enforced by the CLI (`_film110_hook`):

- A whole image and a trailing split (an image running under the next
  bar) are numbered; a leading split (the tail of the previous aperture's
  trailing split, predicted from the hole after it) is reported but
  never numbered — it was numbered where it started, if that aperture
  was scanned.
- **Phase check.** An image more than 6 mm off the declared placement's
  grid means the strip is not where the operator said: the aperture line
  says so, no 110 product is written for it (the aperture product and the
  overscan are, as always), and the command exits 5 after finishing the
  other apertures. Re-seat the strip or declare the right placement.
- **No overwrite.** A 110 product that already exists is never
  overwritten: the aperture line says so, nothing is written for that
  image (neither the visible crop nor its IR sidecar), and the command
  exits 5. Use one `-o` stem per placement (§6). This is what keeps
  `a-image4.tiff` (the free-end, softer copy) and `b-image4.tiff` (under
  the bar, sharp) both on disk so the operator can choose per photograph
  (§4).
- The detector's per-aperture `FrameFind.index` stays what it is — the
  order within that aperture — and is not the strip number.

**What is not implemented and is not planned:** no session or manifest
ties A and B together; the identity is the number, the pairing is the
operator's two stems, and choosing the final product per photograph is
the operator's step (sag rule, §4). `digitize --film 110` (§10) would be
the place for that, once 110 has passed Test 87.

## 6. Commands

Survey (existing 600 dpi run, not a new mode — prints the per-aperture
verdicts, writes only the aperture/overscan products as today, since
`--overscan` is on by default):

```
of135i scan --frames 1-6 --dpi 600 --film 110 --placement A --ir --no-clean -o a-svep600.tiff
```

Production, once the survey says which apertures/placement to use — one
`-o` stem per placement:

```
of135i scan --frames <whole-image-apertures> --dpi 3600 --ir --film 110 --placement A --eject -o a.tiff
# re-seat the strip (§3, placement B), load, then
of135i scan --frames <whole-image-apertures> --dpi 3600 --ir --film 110 --placement B --eject -o b.tiff
```

`--placement` is required with `--film 110` and refused without it. The
products are then `a-image1.tiff`, `a-image3.tiff`, `a-image4.tiff`,
`b-image2.tiff`, `b-image4.tiff`, … — the number is the photograph (§5),
the stem is the placement.

## 7. Products written

`--film 110` never changes what the aperture pipeline already writes:
the full overscan frame(s) and the aperture-registered product (`out`,
`<stem>-ir.tiff`) are written exactly as for `--film 135`. In addition,
for every 110 image the detector finds **whole** inside a verified
aperture, it writes:

- `<stem>-image<N>.tiff` — the 110 image, visible, oriented the same way
  as the aperture product (`--positive`/`--rotate` applied identically).
- `<stem>-image<N>-ir.tiff` — the same crop of the IR channel, with
  `--ir` (same line/column indices as the visible crop, so the two stay
  registered — the dual scan path already guarantees this on the shared
  grid dust removal runs on).

A **split** image is reported (which bar, and "take it in placement B"
/ "A") but no file is written for it — the aperture product and
overscan remain the only record, exactly as an unverified-coverage frame
today writes no aperture-registered product either.

An existing `<stem>-image<N>.tiff` is **never overwritten** and an image
off the declared placement's phase is **never numbered** — both are
reported on the aperture line and make the command exit 5 (§5). The
aperture product and the overscan are written regardless, so a refused
110 product loses nothing that was scanned.

## 8. How the detector reads an aperture

`of135i/film110.py`, pure numpy, in millimetres so it runs on the 600 dpi
survey and the 3600 dpi production frames alike:

1. Column classes from column medians: air (≥ 0.95 x air), plastic
   (≤ 0.05 x air, but only in runs ≥ 2 mm wide, so the film's own dark
   printed border stays inside the band), film between. No film columns:
   the aperture is empty.
2. The film band is the longest run of film columns, 16 ± 1 mm wide,
   adjacent to air on the perforated side.
3. Film presence per line (median over the band interior below 0.85 x
   air) gives a strip end inside the aperture, if any.
4. The film's air-facing edge is located on **every line** (first column
   below 0.6 x air, scanning in from the air side) — the free edge of a
   16 mm strip wandered 1–2 mm along one aperture on this strip — and a
   hole is a line whose zone 0.3–3.0 mm inside *that line's* edge reaches
   air level. Hole runs of 0.8–4 mm (0.3 mm when cut by the aperture
   boundary) are perforations.
5. Every hole predicts the image after it (hole end + 2.3 mm) and the
   image before it (hole start − 3.4 mm); predictions closer than half a
   pitch are the same image and are merged. A window that is neither
   denser than clear film nor varying along its length is clear film, not
   an image, and is dropped.
6. The strip may be inserted either way round, which swaps the 2.3/3.4
   mm figures; both assignments are refined and the one the data confirms
   on more along-transport edges wins (a tie keeps the normal one).
7. Each edge is refined within ±0.5 mm of its prediction. The
   **direction of the step is read from the data, not assumed**: the
   median level 0.3–1.0 mm inside the predicted edge against the same
   band outside it (no refinement below 8 % contrast). On 110 film the
   picture is surrounded on all four sides by the pre-exposed dark
   printed border, so leaving the picture gets *darker* — the opposite
   of a 35 mm-like clear surround. The strongest step in that direction
   wins if it is ≥ 3 x the neighbourhood's median step, and the edge is
   then walked outward to the foot of the transition (≤ 0.6 mm, while
   the step stays ≥ 10 % of the peak), so the soft camera-gate ramp is
   inside the crop. Along transport this is done on the per-line median
   of the band interior; laterally on the per-column median over the
   image's own lines, on **both** sides — the rail side stopping 0.4 mm
   short of the rail's bright rim, the perforated side kept 1.0 mm inside
   the tracked film edge so the film's own rim and the hole never enter
   the window.
8. A find is *whole* with ≥ 0.3 mm inside both aperture boundaries,
   *split* (leading/trailing) otherwise; *free_end_near* when a strip end
   lies within one pitch. The CLI grows a whole image's crop by 0.25 mm
   on every side before writing it — a safety margin of dark border
   around a correctly found edge (§9.1), not a correction for a biased
   one.

## 9. Results on the real strip (offline, 22 apertures, 2026-09-19)

`tools/film110_check.py` over every aperture-registered frame of Test
86: 14 at 600 dpi (three placements) and 8 at 3600 dpi.

- **Verdicts: 22 of 22 as read by eye.** Every whole image is found
  whole, every bar-split image is reported split on the right side, the
  four empty apertures are empty, both strip ends inside an aperture are
  reported (13.2 / 12.0 mm starts, 25.4 mm end) with the free-end flag on
  the adjacent image, and every aperture resolved to the normal
  orientation.
- **Edges**: see §9.1 — the 2026-09-19 figures ("0.2–0.5 mm inside
  the eye reading, a soft gate edge") were the symptom of a detector
  defect, not a property of the film.

### 9.1 Does the crop lose picture? (Astra's review, measured 2026-09-20)

The review asked for proof that no visible part of the photograph is
cut, not just a `whole` verdict. Measured offline on the four finished
images of Test 86: each hand-cropped image was located inside its source
frame by normalised cross-correlation (0.97–0.99 at 1/8 scale), and a
density profile was taken across every edge (mean over the middle 80 %
of the perpendicular extent, 0.1 mm steps).

**Finding: the crop did cut picture.** On image 1's trailing edge the
product ended 0.15 mm before the picture's transition began, with
picture texture in the lost band (visible in the snippet); image 3's
trailing edge lost ≤ 0.08 mm, image 4's leading edge sat mid-ramp. The
cause was not a soft gate edge: `_refine_edge` assumed the surround is
*brighter* than the picture (true for clear 35 mm film, and for the
synthetic fixtures) and searched for a step of that sign. On the real
110 negative the picture (density 0.3–0.8) sits in a dark printed
border (density 1.1) on all four sides, so the only brightening step
near an edge is the fall-off of the thin bright gate-edge halo just
*inside* the picture — the refinement locked onto that, 0.1–0.4 mm
inside the true edge, on every along-transport edge of every frame, and
the 3 x median-step test did not catch it because a near-flat
neighbourhood has a tiny median. The 0.25 mm pad hid most of it.

**Fix** (§8 step 7): the step direction is read from the levels either
side of the predicted edge; the perforated-side lateral edge is refined
too (its 2.0 mm model offset sat 0.2 mm inside the picture's foot on all
four frames). **Result**, `tools/film110_check.py --edge-check`, which
re-measures every edge independently of the detector (the foot of the
density ramp = where its slope falls below 10 % of the peak, so the
rail's second step is never mistaken for the surround):

| Frame | line0 | line1 | col0 | col1 | Size (mm) |
|---|---|---|---|---|---|
| image 1 (A, aperture 1) | +0.19 | +0.24 | +0.26 | +0.25 | 17.07 x 13.11 |
| image 3 (A, aperture 2) | +0.24 | +0.25 | +0.25 | +0.24 | 17.19 x 13.09 |
| image 2 (B, aperture 2) | +0.24 | +0.26 | +0.25 | +0.24 | 17.26 x 13.12 |
| image 4 (B, aperture 3) | +0.24 | +0.26 | +0.22 | +0.26 | 16.80 x 13.12 |

Margins are how far the padded product edge lies *outside* the
independently measured foot: every edge is within 0.06 mm of its foot
before the pad, so the 0.25 mm pad is now pure safety (dark border, no
picture). Sizes match the nominal 17.2 x 13.0 mm; image 4 reads 16.8 mm
in all three placements it was scanned in, so that is the frame. The 22
aperture verdicts of Test 86 are unchanged by the fix, and the same
aperture at 600 and 3600 dpi agrees within 0.05 mm.
- **What failed on the way, and why it matters for a second strip:** a
  hole search fixed next to the aperture-wide median film edge failed at
  3600 dpi (the edge wanders); a hole modelled as a break in the film
  edge failed everywhere (the hole is inside the edge, behind a rim);
  a clear-base level taken from the 90th percentile of the film failed on
  an aperture with a fogged leader and one image (little clear film to
  estimate from). All three are structural and are what steps 4, 5 and
  8 above encode.

## 10. Limitations

- **n = 1 strip.** Pitch 25.5, the 2.3/3.4 mm hole offsets, the 2.0 mm
  lateral offset and the 0.6 mm rim are one strip's. Refinement absorbs
  ±0.3 mm of model error, not ±1 mm. Test 87 — a second strip through
  the full protocol with no manual cropping — is the acceptance step
  before "110 support" is claimed anywhere public.
- **The perforated edge must face the open side of the aperture** (it
  does when the strip lies against the lower rail as in Test 86). With
  the holes under the rail the detector reports "perforations hidden"
  and writes no 110 products; the aperture products are unaffected.
- **Edge safety is 0.25 mm of border, n = 1 strip.** After the
  2026-09-20 fix the refined edge lies within 0.06 mm of the measured
  foot on all 16 production edges; the pad is a margin, not a
  correction. Test 87 re-measures this with `film110_check.py
  --edge-check` on the second strip: any negative margin is a defect to
  fix in the detector, never by widening the pad.
- **Placement phases are one operator's, n = 1 strip** (3.5 mm for A,
  measured 3.53/3.58; B derived as A + 12.75, measured 15.7). The ±6 mm
  rounding tolerance covers the visual rule's slack; a strip that reads
  off-phase is refused, not misnumbered.
- **`leading_continuation` over-reports on 110.** It flags an aperture
  whose first millimetre is darker than clear film as "continuing a
  split image"; on this film the dark printed border does that too, so
  placement A's aperture 1 (image 1 at 3.5 mm) reads as continuing.
  The line is informational only — numbering and products never use it
  (a real continuation is the `leading` split predicted from the hole).
- **Positive (slide) 110 film and B&W 110** are untested; the detector
  works on light levels, the IR clean path assumes a dye-image negative
  as for 135.
- **`digitize` integration is deferred** (its roll/manifest model assumes
  one frame per aperture and one load per strip; 110 needs two loads per
  strip) — docs/film-110-proposal.md §3.3.
