# 110 (Pocket Instamatic) support in the strip holder

Status: **offline-implemented**, 2026-09-19, **reviewed and corrected
2026-09-20** (§9.1: the crop cut 0.1–0.4 mm of picture on the trailing
edges because the edge refinement assumed a clear surround; §5: image
identity across placements was not implemented). `--film 110` runs on
saved data and on synthetic fixtures (`tests/test_film110.py`, in
`tools/release_check.py`'s core group) and has been checked against one
real hardware strip's saved scans (`tools/film110_check.py`, private
fixtures). **Hardware verification passed 2026-09-20: Test 87, a second
strip from another camera, end to end with no manual cropping** (§9.2;
acceptance plan in docs/film-110-proposal.md §6). Supported on n = 2. Nothing here
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
| Image, lateral | 13.0 mm (one image read 13.7; unexplained, see proposal §4.3). **The second strip's gate is 13.3–13.7 mm wide with a 0.5 mm fogged margin on each side that is *lighter* than the picture (§9.2)** — the lateral edge is the camera's, not the format's | same |
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
   the window. **Laterally the step's direction is not assumed at all**
   (§9.2: the first strip's side border is darker than the picture, the
   second's is lighter): the candidates are the local maxima of |step|
   in the window that reach half the window's strongest step and 3 x the
   neighbourhood's median step; the **innermost** candidate whose levels
   0.1–0.4 mm on either side differ by ≥ 4 % wins — a border's outer
   edge (border → clear film) can be the stronger step, picture-content
   steps have picture on both sides and fail the contrast test — and is
   walked outward to its foot. `crop` never pads past the film band.
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
re-measures every edge independently of the detector (the foot = the
start of the first flat plateau beyond the picture, at another level or
reached through a step; a flat sky can trip it into a false LOSS, which
errs towards a human look, never towards a missed cut):

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

### 9.2 The second strip (Test 87, 2026-09-20): a lighter side border

A four-frame strip from another 110 camera, placed by the §3 rule with
image 1 starting 1.0 mm into aperture 1 (2.5 mm earlier than the first
strip's 3.5 mm — well inside the ±6 mm phase tolerance; the numbering
gave 1, 3, 4 directly). Survey verdicts A: image 1 whole, image 2 split
by bar 1/2, image 3 whole, image 4 whole with the strip end free at
25.4 mm (flagged), aperture 4 empty; B: image 1 whole with the free
start flagged, image 2 whole, image 3 split, image 4 whole with the end
under bar 3 — exactly the first strip's pattern, all 8 survey verdicts
and both productions (apertures 1–2 in A, 2–3 in B; coverage verified,
ejected) as the rule predicts, and the same photograph numbered the
same in `a-` and `b-` products.

**The first products cut 0.2–0.5 mm of picture on the rail side of
three images and 0.2 mm on the perforated side of one**, caught by
`--edge-check`. This film differs from the first in one thing a
one-strip model cannot know: beside the picture, on both sides, lies a
~0.5 mm fogged margin of density ~0.8 that is **lighter** than the
picture (density 0.9–1.05 on these dense frames), where the first
strip's printed border (1.13) is darker than its pictures (0.5–0.7).
The lateral refinement had assumed a fixed step direction and checked
contrast in bands around the *prediction*, which for a prediction 0.4 mm
off straddled border and picture: it then found nothing and fell back
to the 13.0 mm model, or locked onto a picture-content step. On the way
to the fix two wrong readings were made and are kept here: the check
tool's foot was walking through that light border to the rail (its
scatter threshold was inflated by the ramp itself), which made the
picture look 14 mm wide "with no side border", and a "picture runs to
the rail" rule built on that reading was implemented and then removed
once the profiles were read properly — a uniform strip of one density
on both sides of every frame is a border, and the close-ups agree.

Fix (§8 step 7): direction-free lateral refinement, innermost contrasted
step; the check tool's foot is now the first plateau beyond the picture.
Re-measured on all eight production frames of both strips: every edge at
or outside the foot (worst +0.08 mm, image 4's perforated side, where
the border and the picture share a density and only the step between
them marks the edge); the first strip's four frames unchanged. Sizes on
this strip: 17.3 x 13.3, 16.9 x 13.4, 17.1 x 13.7, 16.9 x 13.4 mm. The
products were re-cut from the saved aperture frames; both earlier cuts
are kept beside them as evidence.

## 10. Limitations

- **Lateral size and border polarity are the camera's and the film's,
  not the format's**: 13.1 mm inside a darker border on the first strip,
  13.3–13.7 mm inside a lighter one on the second. The model's 13.0 mm
  is only where the search starts; the crop follows the data (§8 step
  7). A fixed "13.0 ± 0.3 mm" is therefore not an acceptance criterion —
  the edge check is.

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

## 11. Two strips in one load (`--strips 2`)

§3's protocol costs two runs per strip. Two strips fit in the holder end
to end, so one load covers both and the pair costs two runs instead of
four — the same pictures for half the transport travel and half the lamp
time.

### 11.1 Why two, and why aperture 4

| Quantity | Value | Source |
|---|---|---|
| Aperture pitch | 37.86 mm | `STRIP_FIDUCIAL.pitch_hwdpi` (10732.7 / 7200 in) |
| Bar (pitch − aperture) | 1.74–2.07 mm | against `STRIP_APERTURE_MM` |
| Holder span, aperture 1 leading → 6 trailing | 225.4 mm | 5 × 37.86 + 36.12 |
| 110 strip, 4 images: pictures | 93.7 mm | 3 × 25.5 + 17.2 |
| 110 strip, 4 images: film | ~102 mm | 4 × `FILM_110.pitch_mm` |

Strip 2 must start past strip 1's film and still end inside aperture 6.
Aperture 3 (75.7 mm along) collides; **aperture 4** (113.6 mm) is the
first that clears, and leaves strip 2's end near 215.6 mm, inside
aperture 6. Two strips of 102 mm fit in 225.4 mm; three (306 mm) never do.
`STRIP2_ORIGIN_APERTURE = 4` and `film110.strip_origin` encode this, and
`test_strip2_starts_clear_of_strip1_and_fits_the_holder` asserts both
bounds so the constant cannot drift away from the geometry.

**The clearance is not generous, and placement B is the tight case.**
Strip 1 shifts half a pitch on in B, so its film end moves with it:

| Strip 1 film length | End in placement B | Against aperture 4's edge (113.59 mm) |
|---|---|---|
| 99 mm | 111.8 mm | 1.8 mm clear, under bar 3/4 |
| 100 mm | 112.8 mm | **0.8 mm clear**, under bar 3/4 |
| 101 mm | 113.8 mm | **crosses into aperture 4** |
| 102 mm | 114.8 mm | crosses into aperture 4 |

The margin is small enough that the *choice of pitch* moves it: the
measured grid (`STRIP_FIDUCIAL`, 37.863 mm) puts aperture 4's edge at
113.59 mm, the nominal one (`Holder.pitch_mm`, 37.947 mm) at 113.84 mm —
0.25 mm apart, a third of the clearance. The table uses the measured
grid, since this is a question about where the plastic is.

§4's measured behaviour (strip 1's end under bar 3/4 in B) puts the test
strip at 98.9–100.8 mm, so a four-image strip is expected to clear — but
by under a millimetre, and a strip cut with more leader will not. A
strip 1 that reaches into aperture 4 puts foreign film at the leading
edge of strip 2's first aperture. Test 88 criterion 8 checks exactly
this; if it fails, strip 2's origin aperture is the thing to change, not
the detector.

### 11.2 Both strips are in the same placement

The film pitch (25.5 mm) and the aperture pitch (37.86 mm) do not go
evenly into one another, so a **single continuous** strip drifts out of
phase against the apertures — 3 apertures along is 4.454 film pitches,
11.6 mm off. That drift is what §2 is about.

It does **not** bind two separate strips. Each is seated by hand against
its own aperture's leading edge, so each gets the phase the operator gives
it, and both sit at §3's placement A phase in their own first aperture.
One `--placement` per run stays correct for the pair:

| Run | Strip 1 | Strip 2 |
|---|---|---|
| 1 | A | A |
| 2 (re-seat both, half a pitch on) | B | B |

Per strip and placement, one image lands on a bar and the other placement
recovers it, exactly as §2–3 describe — the bar pattern at aperture 4 is
the same as at aperture 1.

### 11.3 Numbering and products

Each strip is numbered from **its own** first image, so strip 2's first
picture is image 1, not image 5: `image_number(..., origin_aperture=…)`
measures along the aperture grid from that strip's origin aperture rather
than from aperture 1. Because both strips then produce an image 1, the
product name carries the strip:

```
<stem>-s1-image<N>.tiff        <stem>-s2-image<N>.tiff
<stem>-s1-image<N>-ir.tiff     <stem>-s2-image<N>-ir.tiff
```

`--strips 1` (the default) keeps today's `<stem>-image<N>.tiff` names
byte-identical. The §5 phase check and the §7 overwrite refusal are
unchanged and now report which strip they are about.

### 11.4 Commands

```
of135i scan --frames 1-6 --dpi 3600 --ir --film 110 --strips 2 --placement A --eject -o a.tiff
# re-seat BOTH strips half a pitch on (§3, placement B), load, then
of135i scan --frames 1-6 --dpi 3600 --ir --film 110 --strips 2 --placement B --eject -o b.tiff
```

`--strips` is refused without `--film 110`, and only 1 or 2 are accepted.

### 11.5 Limitations

- **Sag.** Strip 2's free end falls inside aperture 6, not under a bar.
  §4's measurement (1556 against 3399 for a supported end) says its last
  image is the one at risk. The free-end warning already raised per image
  applies unchanged, but the two-strip layout cannot give strip 2's end
  bar support the way re-seating a single strip can.
- **Untested on hardware.** The geometry above is arithmetic on measured
  constants; no two-strip load has been scanned. Aperture 3 and 6 each
  carry only one image of their strip, so there is slack to shift the
  strips if a real load wants it.
- **The constants are n = 2** (Test 86 and Test 87, two strips from two
  cameras). Pitch, protocol, numbering and the sag rule generalised to
  the second strip; the *lateral* edge model did not and was fixed. The
  arithmetic above rests on `FILM_110.pitch_mm` and the aperture grid,
  both of which Test 87 exercised — but never with two strips in the
  holder at once, which is Test 88 (§12).

## 12. Test 88 — the hardware acceptance for `--strips 2`

§11 is arithmetic on measured constants. No two-strip load has ever been
in the scanner. Test 88 is the one hardware test that closes that gap,
and it is **closed by construction**: the criteria below are the whole
list, they are checked in the two runs the mode itself costs, and
nothing here re-validates what Test 86/87 already settled.

### 12.1 Setup

Two four-image 110 strips, the standard strip holder, film against the
lower rail (perforations toward the open side of the aperture, §10).
Strip 1 seated at aperture 1 by the §3 placement rule, strip 2 seated
the same way at **aperture 4**.

Prefer the Test 86 and Test 87 strips: their images are already known
good, so any new defect is the layout's, not the film's.

### 12.2 The runs — two, not more

```
# Run 1 — both strips in placement A
of135i scan --frames 1-6 --dpi 600 --film 110 --strips 2 --placement A --ir --no-clean -o t88-a600.tiff
of135i scan --frames <whole-image apertures> --dpi 3600 --ir --film 110 --strips 2 --placement A --eject -o t88-a.tiff

# re-seat BOTH strips half a pitch on (§3 placement B), status && load
# Run 2 — both strips in placement B
of135i scan --frames 1-6 --dpi 600 --film 110 --strips 2 --placement B --ir --no-clean -o t88-b600.tiff
of135i scan --frames <whole-image apertures> --dpi 3600 --ir --film 110 --strips 2 --placement B --eject -o t88-b.tiff

.venv/bin/python tools/film110_check.py --dpi 3600 --edge-check t88-*-s*-image*.tiff
```

### 12.3 Definition of done

Test 88 passes when **all eight** hold. Each is decided from the two runs
above; none needs a third load.

1. **The strips do not collide.** Strip 2 seats at aperture 4 with
   strip 1 in place, neither strip is bent or lifted by the other, and
   the holder closes normally. *Observation, before any scan.*
2. **Every aperture reads its own strip.** The 600 dpi survey finds
   images in apertures 1–3 attributed to strip 1 and in 4–6 attributed
   to strip 2, with no aperture reporting "no 110 film band" where a
   strip is.
3. **One `--placement` is right for both.** No image on either strip is
   refused as off-phase (§5's ±6 mm) in either run. A refusal here falsifies
   §11.2 and is the finding the test exists to catch.
4. **Numbering restarts per strip and is stable across placements.**
   Each strip's first picture is image 1, and the same photograph carries
   the same number in run 1 and run 2 — checked per strip, the §5 rule.
5. **Both strips' products are written and none collides.** The
   `-s1-` and `-s2-` products exist for every whole image found, nothing
   is refused as already existing, and the eight photographs are
   accounted for across the two runs.
6. **No picture is lost at an edge.** `film110_check.py --edge-check`
   reports no negative margin on any production edge of either strip —
   the same instrument and the same bar as Test 87, no new threshold.
7. **Both strips' last images are sharp in placement B.** §4's rule must
   carry to strip 2: its image 4 in run 2, whose film end lies past
   aperture 6's trailing edge, is judged against its own image 4 from
   run 1 (free end) — supported should be visibly the sharper, as
   2780/3765/3399 against 2004/1775/1556 was in Test 86. §12.4.
8. **Strip 1's tail does not reach into aperture 4.** In placement B
   strip 1's film end sits at ~112.8 mm against aperture 4's leading
   edge at 113.59 mm — **0.8 mm of margin on a ~100 mm strip**, and a
   strip 101 mm or longer crosses into strip 2's first aperture. Run 2's
   aperture 4 must show strip 2's image 1 and no foreign film at its
   leading edge. §11.1.

### 12.4 Sag is a gate, and the protocol is what clears it

An earlier revision of this section called sag "a finding, not a gate",
on the reasoning that `--strips 1` stays available. That was wrong, and
it misread §4. The two placements are not only about bars splitting
images — **the placement that gives a strip its end under plastic is the
placement that gives that strip a sharp last image.** Sag is what the
protocol is *for*. A layout that cannot clear it has not delivered the
strip.

Laid out, at the measured constants and a ~100 mm four-image strip:

| | image 1 | image 2 | image 3 | image 4 | film end |
|---|---|---|---|---|---|
| **A** | whole ap1 | **split** | whole ap2 | whole ap3 | free at 100.0 mm — **sags** |
| **B** | whole ap1 | whole ap2 | **split** | whole ap3 | 112.8 mm, under bar 3/4 — supported |

So B is the run that delivers both the recovered image 2 *and* a sharp
image 4; A contributes image 3. Strip 2 at aperture 4 repeats the
pattern one strip along (ap4/5/6, split at bar 4/5 in A and 5/6 in B).

**The open question Test 88 must answer** is strip 2's end in placement
B. It lands at 226.3 mm — 1.16 mm *past* aperture 6's trailing plastic
edge at 225.17 mm. There is no bar 6/7; what would support it is the
holder's own end plastic beyond the last aperture. That the holder body
continues there is near-certain physically, but *how far*, and whether
1.16 mm of overlap is enough to hold the film flat the way a 1.9 mm bar
does, is not something this repo has measured. It is a hardware
observation, and it is criterion 7 below — **a gate, not a note**.

If strip 2's end is not supported, the honest outcome is that the
two-strip layout delivers strip 1 fully and strip 2 with a soft last
image. That is a real cost to weigh against halving the runs, and it is
the owner's call, not a detail to bury.

What is still **not** part of this test: re-seating the strips to hunt
sharpness frame by frame. The protocol either clears sag at its two
declared placements or it does not, and that is what gets recorded.

### 12.5 Not in scope — do not add these to Test 88

- **Three strips.** 306 mm of film in a 225.4 mm holder. Settled by
  arithmetic (§11.1); no test can change it.
- **Re-validating the detector, the edge model, the crop or the sag
  rule.** Test 86 and Test 87 settled those at n = 2. Test 88 uses the
  edge check as a regression bar only (criterion 6).
- **New film types** (positive, B&W), a third camera, or more strips
  through the layout. The layout is geometry, not film chemistry; it
  does not inherit §10's per-film questions.
- **Image quality judgements** beyond "the whole picture is there".
  Colour and tone belong to the digitising workflow, as in Test 87.

If Test 88 passes, `--strips 2` is supported and README says so. If a
criterion fails, the failure names the fix — a collision means the origin
aperture is wrong, an off-phase refusal means §11.2 is wrong — and the
test is re-run once after that fix, not broadened.
