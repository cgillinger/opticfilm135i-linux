# Proposal: 110 (Pocket Instamatic) support in the strip holder

Status: PROPOSAL for review, 2026-09-19. Nothing here is implemented.
The measurements are from one evening with one strip (n = 1) and are
marked as such. No motor sequence, wait, profile or calibration changes
are proposed; everything below is image-side and workflow.

## 1. What was established on hardware (2026-09-19, Tests 86a-c)

A four-frame 110 colour-negative strip was scanned in the standard 35 mm
strip holder, three placements, one load each, all coverage checks
verified (14 apertures at 600 dpi, 8 at 3600 dpi). Four finished images
were produced by hand-cropping. Raw data: private analysis area,
`instamatic-20260919/{placering-a-forsta-laget,placering-a-regel,placering-b}`.

### 1.1 Film geometry (measured, n = 1 strip)

| Quantity | Value | How measured |
|---|---|---|
| Film width | 16.0 mm (15.7-16.0 across 3 loads) | lateral edge of the film band in the 600 dpi survey |
| Image, along transport | 17.2 mm (17.0-17.5, four images) | 1 mm rulers on the 3600 dpi frames |
| Image, lateral | 13.0 mm (one image read 13.7; see §4.3) | same |
| Frame pitch | 25.5 mm (25.0 / 25.8 / 25.7 between consecutive images) | 600 dpi survey, first placement |
| Perforation | one rectangular hole per frame, ~1.5 x 2 mm, on the film edge facing the open side of the aperture; its trailing edge lies 2.3 mm before the image start (consistent on all four) | 600 dpi rulers |
| Lateral position | the film lies against the lower rail; its far edge sits ~8 mm inside the 24 mm aperture; stable to ±0.1 mm across three loads | column profiles |
| Image lateral offset | image begins 2.4 mm inside the perforated edge; rail-side edge within ~0.5 mm of the rail | 3600 dpi rulers |

Nominal 110 figures (13 x 17 mm image, 16 mm film) agree; the pitch and
the perforation offset are this strip's and need a second strip before
they are called general (project rule: never build on one reference).

### 1.2 Why one placement is not enough

The aperture pitch is 38 mm (36 mm opening + 2 mm bar); the film pitch
is 25.5 mm. With image 1 edge-aligned to aperture 1, successive images
sit at phases 0, 25.5, 13, 0.5, 26, 13.5, 1, ... mm inside their
apertures. An image needs phase ≤ 18.8 mm to be whole; every third image
(2, 5, 8, ...) lands under a bar. The bar is opaque, so that image cannot
be recovered from that placement.

### 1.3 The operator protocol (Christian's rule, verified)

Two placements, both visual, no measuring:

- **Placement A** - image 1's left edge at the left edge of aperture 1,
  with a hair of clear film visible (exactly like a 35 mm strip). Yields
  images 1, 3, 4 (and 6, 7, ...) whole; bar 1/2 splits image 2.
- **Placement B** - bar 1 falls between images 1 and 2, bar 3 falls to
  the right of image 4 (a shift of 12.5 mm = half a film pitch). Yields
  images 2, 4 (and 5, 8, ...) whole.

An earlier spacer-based rule (shift 19 mm) was rejected as fiddly; the
visual rule is what the documentation should carry.

### 1.4 The sag finding (affects which placement takes which frame)

A 16 mm strip is held only where it passes under plastic. A free strip
end inside an aperture sags out of the focal plane:

| Image 4 (same frame, same day) | Laplacian sharpness, three bands along the frame |
|---|---|
| Placement A, strip end free in aperture 3 | 2004 / 1775 / 1556 (softening toward the end) |
| Placement B, strip end under bar 3/4 | 2780 / 3765 / 3399 (grain resolved) |

So the last image is taken in B, where the end is held, and image 1 is
taken in A, where the start is held. The driver should know this rule
and warn (see §3.4); it cannot fix it.

## 2. Design principle

**110 support is a film model plus an image-side detector on top of the
existing aperture pipeline.** The motor side is untouched: positioning
stays on the strip holder's fiducial grid (`holder.STRIP_FIDUCIAL` via
`overscan_geometry`), the overscan window, the coverage check
(`aperture_crop.measure_coverage`) and the aperture crop
(`crop_to_aperture`) remain exactly as today and remain the fail-closed
geometry gate. A 110 frame is always smaller than an aperture, apertures
are separated by opaque bars, and no holder-ID register exists, so a
110-specific FEEDL grid would gain nothing. Whole-strip sweeping (roadmap
candidate) is independent of this and not required.

Products stay raw: the 110 crop is the raw, channel-aligned negative,
cut to the image; the IR channel is cut with the same indices (as the
dual path already does), dust removal runs before the crop on the shared
grid. `--positive` remains a preview; colour is the application's job.

## 3. Proposed changes

### 3.1 `of135i/holder.py` - a `Film` model beside `Holder`

```python
@dataclass(frozen=True)
class Film:
    name: str
    width_mm: float
    image_mm: tuple[float, float]        # (along transport, lateral)
    pitch_mm: float
    perforation_lead_mm: float           # hole trailing edge -> image start
    image_lateral_offset_mm: float       # perforated film edge -> image edge
    geometry_source: str

FILM_135 = Film("35 mm", 35.0, (36.0, 24.0), 38.0, ...)   # fills the aperture: today's behaviour
FILM_110 = Film("110 Pocket Instamatic", 16.0, (17.2, 13.0), 25.5, 2.3, 2.4,
                geometry_source="one strip, 2026-09-19 (Test 86); n=1")
```

`Holder` is not changed; the holder is still `STRIP`. `Film` only
parameterises the image-side detector. All constants are in millimetres
so the detector works at 600 (survey) and 3600 (production) alike.

### 3.2 `of135i/film110.py` - the frame detector (pure numpy, offline-testable)

Input: an aperture-registered image (output of `crop_to_aperture`), its
dpi, and a `Film`. Output: a list of `FrameFind` records plus an
aperture verdict.

1. **Empty test.** Column medians; if no column is below 0.8 x the air
   level, the aperture is empty (survey prints "empty").
2. **Film band.** Longest run of columns with median below 0.8 x air and
   above the plastic level; width must be `width_mm ± 0.5`, else
   "no 110 film band" (the frame is left as today's aperture product).
3. **Perforations as the primary fiducial.** Inside the film band, the
   only air-level pixels (≥ 0.98 x air) are the holes. Connected runs
   of air-level lines near the perforated edge give the holes; each
   hole's trailing edge + `perforation_lead_mm` predicts an image start,
   + `image_mm[0]` an image end. This is what makes the detector robust:
   the holes are unambiguous, whereas thresholding the image against the
   clear base fails on thin (shadow) regions and on skies - both of my
   own threshold/gradient attempts on 2026-09-19 failed for exactly that
   reason, and the four finished images were cut by eye from 1 mm
   rulers.
4. **Local refinement.** Each predicted edge is refined within ±0.5 mm
   by the strongest gradient of the line-median profile over the film
   interior (excluding 1.5 mm at each film edge, where the hole and the
   frame-number ink live). Lateral edges: predicted from the perforated
   film edge + `image_lateral_offset_mm` and + `image_mm[1]`, refined
   the same way. Refinement may move an edge at most 0.5 mm; otherwise
   the prediction stands and the record says so.
5. **Classification per predicted image:**
   - `whole`: both along-transport edges ≥ 0.3 mm inside the aperture.
   - `split`: an edge lies beyond the aperture (the image runs under a
     bar). Reported with which side, so the survey can say "take this
     one in the other placement".
   - `free_end_near`: a film end (the film band ends inside the
     aperture) lies within one pitch of the image - sharpness untrusted
     (§1.4). Warning, not a failure.
6. **Numbering.** Images are numbered by counting perforations from the
   first frame detected in aperture 1. Under the protocol of §1.3, image
   1 is in aperture 1 in both placements, so A and B yield the same
   numbers by construction. If the strip does not start in aperture 1
   the survey says "numbering relative to the first visible frame".

Never raises on image content; a frame that cannot be resolved returns a
verdict with a reason, and the aperture product is still written.

### 3.3 CLI: `scan --film 110` (and later `digitize --film 110`)

- `--film {135,110}`, default 135 = unchanged behaviour. With `110`,
  after each aperture's coverage-verified crop (`_finish_plain_scan` /
  `_finish_dual_scan`): run the detector, and for every `whole` image
  write `<stem>-f<aperture>-image<N>.tiff` (+ `-ir.tiff` with `--ir`,
  same indices). The aperture product and the overscan frame are still
  written as today, so nothing is lost if the detector is wrong.
- One line per aperture on stdout, e.g.
  `aperture 2: image 2 whole (17.2 x 13.0 mm); image 3 split by the
  trailing bar -> take it in the other placement`,
  `aperture 3: image 4 whole; film end at 25.5 mm -> sharpness
  untrusted, take image 4 in placement B`, `aperture 4: empty`.
- **Survey is the existing 600 dpi run, not a new mode:**
  `scan --frames 1-4 --dpi 600 --film 110 --ir --no-clean` prints the
  verdicts above. A `--survey` alias can come later if wanted.
- `digitize` integration is deferred: its roll/manifest model assumes
  one frame per aperture and one load per strip; 110 needs two loads
  per strip. Proposal: a follow-up once `scan --film 110` is verified.

### 3.4 Documentation

- `docs/film-110.md`: the measurements (§1.1), the placement protocol
  with a drawing (§1.3), the sag rule (§1.4), the numbering rule, the
  command lines, and the limitations (§5).
- README: one paragraph under supported holders/films; ROADMAP: a new
  milestone with the acceptance criteria in §6.
- `docs/test-log.md`: Test 86 (tonight's three placements) written up
  with the sharpness table.

### 3.5 Tests (offline, in `release_check`)

- **Synthetic fixtures** in the repo: generated 110 frames (clear base
  with orange-mask levels, perforations at the measured geometry, dark
  image rectangles with sky-like and shadow-like regions, noise) at 600
  and 3600 dpi, placed at each of the three phases of §1.2 plus a
  split case, an empty aperture and a free-end case. Assert
  classification, image size within ±0.3 mm, numbering, and that a
  bright-sky or dark-shadow region does not move an edge.
- **Private fixture runner**: when tonight's real frames are present on
  disk (they are private photographs and never enter the repo), run the
  detector over all 22 apertures and compare with the hand-read edges
  (expected: whole/split/empty verdicts as in §1.3, edges within
  ±0.3 mm). Skipped, not failed, when the files are absent.
- The existing suites are untouched; `--film 135` must be byte-identical
  to today (regression on a saved 35 mm frame).

## 4. Risks and open questions for the reviewer

1. **n = 1.** Pitch 25.5 and the 2.3 mm perforation lead are one strip's.
   Different labs cut 110 in 3-5 frame strips; some cameras' gates differ
   slightly. The detector refines edges locally, so ±0.3 mm of model error
   is absorbed; ±1 mm is not. A second strip is required before "support"
   is claimed (§6).
2. **Rail-side lateral edge.** The image's rail-side edge lies within
   ~0.5 mm of the rail's shadow. If the refinement cannot find it, the
   crop uses the model prediction. Acceptable? Alternative: crop 0.15 mm
   inside the prediction and say so.
3. **Image 1 read 13.7 mm wide, the others 13.0.** Probably my ruler
   reading (aperture 1's lateral position differs slightly), but it is
   unexplained. The detector's fixture run will settle it.
4. **Numbering across placements** relies on the protocol (image 1 in
   aperture 1 in both). Should the driver verify this (e.g. by matching
   the perforation phase between A and B) or just document it?
5. **Warn vs. fail on a free end.** §3.2 warns. Should `free_end_near`
   suppress the product instead, to keep "a written product is a good
   product"? My view: warn, because the operator may accept softness for
   a frame that exists in no other placement (a 3-frame strip has no B
   for its last frame's end).
6. **Where does `--positive` orientation go?** 110 images are landscape
   or portrait at the photographer's whim; no auto-rotate is proposed,
   `--rotate` stays manual.
7. **Positive 110 film** (110 slide film exists) and **B&W 110** (no IR
   dust map): out of scope for the first cut; the detector does not care,
   the IR/clean path does.

## 5. Explicitly out of scope

- Any change to POSITION, PARK, load flow, waits, calibration or the
  FEEDL/overscan geometry.
- A 110-pitch FEEDL grid (nothing to gain, §2).
- 126 Instamatic (35 mm wide, 28 x 28 mm image): the `Film` model could
  describe it, but the 24 mm aperture clips it laterally by 2 mm on each
  side; a separate decision.
- Holder-type detection (no register reports one; documented limitation).
- SANE backend changes (a frontend can already scan every aperture; the
  110 crop is host-side post-processing and can live in the frontend or
  in a later `--film` option once the Python path is proven).

## 6. Verification plan and acceptance

1. Offline: §3.5 green; the private fixture run reproduces tonight's
   hand crops within ±0.3 mm on all eight 3600 dpi frames and gives the
   right whole/split/empty verdict on all 14 survey apertures.
2. Hardware, **Test 87**: a *second* 110 strip through the full protocol
   with no manual cropping - placement A: `scan --frames 1-4 --dpi 600
   --film 110` (survey) then `scan --frames <whole> --dpi 3600 --ir
   --film 110 --eject`; placement B likewise. Acceptance: every image on
   the strip delivered once as a `whole` product, sizes 17.2 x 13.0
   ±0.3 mm, no image missing an edge under eye inspection, the survey's
   A/B advice correct, and the sag warning raised exactly where a free
   end is.
3. Only then: README/ROADMAP say "110 in the strip holder: supported,
   two placements", with n = 2 stated.

## 7. Work split (for Christian's model rule)

- Spec, detector design, review, hardware runs, docs: main model.
- `Film` model, `film110.py`, synthetic fixtures, CLI wiring, private
  fixture runner: Sonnet against this document.
- Estimated size: ~400 lines of code, ~300 lines of tests, one hardware
  evening for Test 87.
