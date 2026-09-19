# 110 (Pocket Instamatic) support in the strip holder

Status: **offline-implemented**, 2026-09-19 — `--film 110` runs on saved
data and on synthetic fixtures (`tests/test_film110.py`, in
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

## 5. Numbering

Images are numbered by a running counter across every aperture scanned
in one `scan` command, in ascending order, counting each detected image
once. A whole image and a trailing split (an image running under the
next bar) are counted; a leading split (the tail of the previous
aperture's trailing split, predicted from the hole after it) is reported
but **not** counted again. If the first aperture scanned is
not 1, the CLI prints `110: numbering is relative to the first scanned
aperture` once, since there is no way to know a strip's true frame 1 from
one aperture's data alone.

## 6. Commands

Survey (existing 600 dpi run, not a new mode — prints the per-aperture
verdicts, writes only the aperture/overscan products as today, since
`--overscan` is on by default):

```
of135i scan --frames 1-6 --dpi 600 --film 110 --ir --no-clean -o svep600.tiff
```

Production, once the survey says which apertures/placement to use:

```
of135i scan --frames <whole-image-apertures> --dpi 3600 --ir --film 110 --eject -o out.tiff
```

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

A **split** image is reported (which bar, and "take it in the other
placement") but no file is written for it — the aperture product and
overscan remain the only record, exactly as an unverified-coverage frame
today writes no aperture-registered product either.

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
7. Each along-transport edge is refined within ±0.5 mm to the strongest
   gradient of the expected sign, accepted only if it is ≥ 3 x the
   neighbourhood's median step, then walked outward to the foot of the
   transition (≤ 0.6 mm). Laterally the perforated-side edge is the
   tracked film edge + 2.0 mm (the profile there — dark border, a bright
   sliver of rebate, then the image — is too ambiguous to refine), and
   the rail-side edge is refined the same way, stopping 0.4 mm short of
   the rail's own bright rim.
8. A find is *whole* with ≥ 0.3 mm inside both aperture boundaries,
   *split* (leading/trailing) otherwise; *free_end_near* when a strip end
   lies within one pitch. The CLI grows a whole image's crop by 0.25 mm
   on every side before writing it.

## 9. Results on the real strip (offline, 22 apertures, 2026-09-19)

`tools/film110_check.py` over every aperture-registered frame of Test
86: 14 at 600 dpi (three placements) and 8 at 3600 dpi.

- **Verdicts: 22 of 22 as read by eye.** Every whole image is found
  whole, every bar-split image is reported split on the right side, the
  four empty apertures are empty, both strip ends inside an aperture are
  reported (13.2 / 12.0 mm starts, 25.4 mm end) with the free-end flag on
  the adjacent image, and every aperture resolved to the normal
  orientation.
- **Edges at 3600 dpi against the hand-read edges of the four finished
  images** (nominal mm): along transport the refined edges lie 0.2–0.5 mm
  *inside* the eye reading at both ends (a soft camera-gate edge; the
  0.25 mm product pad recovers half of it); laterally within 0.1 mm on
  three frames and 0.5 mm on the fourth (whose eye reading was itself the
  odd one out at 13.7 mm). Sizes 16.3–16.9 x 12.6–13.0 mm.
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
- **Edge bias.** Refined along-transport edges sit 0.2–0.5 mm inside the
  visible picture edge on this strip; the product pad is a fixed 0.25 mm.
  If a second strip shows a different soft-edge width, the foot walk's
  constants are the knob, not the pad.
- **Positive (slide) 110 film and B&W 110** are untested; the detector
  works on light levels, the IR clean path assumes a dye-image negative
  as for 135.
- **`digitize` integration is deferred** (its roll/manifest model assumes
  one frame per aperture and one load per strip; 110 needs two loads per
  strip) — docs/film-110-proposal.md §3.3.
