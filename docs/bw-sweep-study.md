# Can black-and-white film go through the whole-strip sweep? An offline study

Status: offline study, 2026-10-31. **No hardware was run for it.** Every
number below comes from data the driver had already produced: the
whole-strip sweep A/B raw of 2026-10-30 (`docs/test-log.md`, Test 111),
the plain-3600 black-and-white strip of 2026-09-10 (Test 60), and the
production black-and-white strips of 2026-10-31 (Test 111, production
confirmation, session 2). Nothing in the driver was changed by this
study; it is the decision basis for whether to build a plain-3600 sweep.

## 1. The question

The whole-strip sweep (`docs/whole-strip-sweep-design.md`, released in
v0.2.8) scans a 135 strip in one pass instead of one pass per frame and
cuts the frames out afterwards. It is implemented for the dual-light
profiles only, because the window finder (`of135i/sweep.py`) locates the
apertures in the **infrared** lines of the sweep: the IR side channel
sees through a colour negative's dyes, so every aperture is a near-flat
lit run bounded by the holder's opaque crossbars, whatever the picture.

Black-and-white film is scanned without IR (`--no-ir`, the plain 3600
profile): silver blocks IR, so the side channel would show the picture
anyway, and the plain profile has no IR lines at all. The CLI therefore
refuses `--sweep` on plain 3600 and black-and-white strips still go
frame by frame. In production on 2026-10-31 a six-frame black-and-white
strip took 252 s wall clock (225 s on the device), against 199 s for a
four-frame colour sweep; each frame parks and repositions from home, so
park and position grow by about 2.1 s per aperture and frame 6 costs
47 s where frame 1 costs 31 s. A black-and-white sweep would remove five
carriage returns and five calibrations per six-frame strip.

The open question was whether the apertures can be found in the
**visible** lines alone, on real black-and-white content, well enough to
cut a sweep. This study answers it offline before any code is written.

## 2. What was tested, and how much

| Material | Profile | Frames | Source |
|---|---|---|---|
| A/B sweep raw, one colour-135 strip, apertures 1–4 | dual 3600, IR + visible interleaved, 42 912 wire lines, 1.33 GB | 4 | Test 111 (2026-10-30), kept on disk |
| Production sweep raw, one colour-135 strip whose film ends inside aperture 3 | dual 3600, same size | 4 (3 with film) | Test 111 session 2 (2026-10-31), kept because aperture 3 was refused |
| Black-and-white strip, per-frame overscan windows | plain 3600, `--no-ir` | 6 | Test 60 (2026-09-10) |
| Production black-and-white strips, per-frame overscan windows and diag sidecars | plain 3600, `--no-ir` | 16 strips, 63 windows on disk, 5 of them refused by the driver | Test 111 session 2 (2026-10-31) |

For scale: the driver's hardware log (`docs/test-log.md`) holds 111
numbered tests since 2026-08-30; the production telemetry study
(`docs/production-telemetry-2026-10.md`) analysed 711 delivered frames;
the whole-strip sweep has run on 10 production colour strips plus the A/B
pair as of this date; the black-and-white per-frame cycle on 16
production strips the same day.

Method, in the order it was run:

1. **Step 1, visible versus IR on the same bytes.** From the A/B sweep
   raw, two along-strip profiles were computed from the same memory map:
   the IR profile exactly as `sweep.ir_profile` does, and a visible
   profile from the odd wire lines, per channel (red, green, blue,
   luminance), with the same colour-line stagger registration. Each was
   fed to the unchanged `sweep.find_windows`. The IR result is the
   reference; the question is whether the visible result agrees.
2. **Step 2, black-and-white content against the plastic.** On every
   black-and-white overscan window: the production coverage check
   (`aperture_crop.measure_coverage`, red and luminance), and how close
   the picture gets to the plastic floor, expressed as a fraction of the
   window's lit range, since the edge finder's rules
   (`of135i/aperture.py`: `EDGE_GRADIENT`, `EDGE_CONTRAST`,
   `EDGE_MIN_STEP`, `PLASTIC_LEVEL`) are all relative.
3. **Step 2b, a synthetic black-and-white sweep.** The six Test 60
   windows stitched into one profile with the holder's 2.0 mm crossbar
   between them (plastic level taken from the windows' own plastic),
   handed to `find_windows` as a six-aperture sweep, then each resulting
   window re-checked by the per-frame rule as the sweep's cutter does.
4. **Step 2c, the refused production apertures.** For each 2026-10-31
   aperture the per-frame check refused with "no aperture found in
   profile", the edge finder's four criteria re-evaluated at the
   position where the edge physically is, to name the rule that dropped
   it.

Heavy array work ran on uint16 data, block-wise, under a 3 GB memory
cap; the raw files were opened read-only.

## 3. Results

### 3.1 Step 1: the visible lines find the same apertures as IR

All five profiles resolve the A/B sweep with four lit runs of plausible
length, one strip offset, and every aperture located by its own edges.

| Profile | Resolved | Offset vs predicted (mm) | Runs found | Run lengths (mm) |
|---|---|---|---|---|
| IR (reference) | yes | +0.072 | 4 | 36.05, 36.03, 36.05, 35.93 |
| visible red | yes | +0.071 | 4 | 36.05, 36.06, 36.06, 35.95 |
| visible green | yes | +0.069 | 4 | 36.05, 36.05, 36.06, 35.95 |
| visible blue | yes | +0.069 | 4 | 36.05, 36.04, 36.06, 35.95 |
| visible luminance | yes | +0.070 | 4 | 36.05, 36.05, 36.06, 35.95 |

The cut windows differ from the IR windows by at most 4 wire lines
(0.014 mm) at either end. On a colour negative the visible profile is
far from flat inside an aperture — the picture content dips to 31–47 %
of the strip's lit range in green and red — but the holder's edges are a
step from that level to the plastic floor, and the finder places them
where IR does.

The second real sweep, with the film ending inside aperture 3, repeats
the picture and adds a nuance. IR finds three runs (apertures 1, 2, 4;
35.91–36.01 mm), resolves at +0.23 mm and cuts aperture 3 by pitch.
Visible **red** finds the same three runs and the same windows (within
0.03 mm). Green and luminance find two (apertures 2 and 4), blue one
(aperture 4): on a colour negative aperture 1's leading edge is a step
from the dim clear rebate to the plastic, and the dimmer channels do not
clear the finder's absolute step rule there. All five still resolve on
the runs they have and cut every aperture within 0.2 mm of IR. For
colour film, red is the visible channel that matches IR; for silver film
the channels carry the same picture and the choice is moot.

### 3.2 Step 2: black-and-white content comes much closer to the plastic

Test 60's six frames all verify per frame (red and luminance alike),
with margins 0.49–0.70 mm leading and 0.85–1.05 mm trailing. But the
picture content is darker relative to the plastic than on colour film:

| Frame | Content minimum (fraction of window range) | Lit level next to the leading / trailing edge |
|---|---|---|
| 1 | 0.09 | 0.12 / 0.94 |
| 2 | 0.37 | 0.61 / 0.95 |
| 3 | 0.12 | 0.12 / 0.64 |
| 4 | 0.31 | 0.96 / 0.32 |
| 5 | 0.20 | 0.46 / 0.99 |
| 6 | 0.33 | 0.90 / 0.48 |

Two frames have a dense picture right against an aperture edge, where
the lit side of the edge is only 12 % of the window's range above the
plastic. The edge finder's absolute rule, `EDGE_MIN_STEP = 0.08` of the
profile's range, is what those edges pass — with a third of the
threshold to spare, not more. A colour negative's orange mask keeps the
clear rebate and the shadows well above that; silver film has no such
floor.

### 3.3 Step 2b: the synthetic black-and-white sweep resolves and cuts 6 of 6

Stitched into one six-aperture profile, the Test 60 strip resolves:
four runs found (apertures 1, 2, 5, 6), lengths 35.82–36.10 mm, one
strip offset (0.00 mm against the stitched prediction), and apertures 3
and 4 cut **by pitch** from the siblings' offset. They were lost because
the sweep's range is the whole strip's: their edge-adjacent content sits
at 4.7 % and 7.5 % of that range, under the 8 % absolute step rule that
the per-frame windows, with their own smaller range, had passed.

That loss does not reach the products. The sweep's cutter hands every
window to the per-frame writer, which measures coverage again on the
window's own profile: all six windows, the two pitch-located ones
included, verify there (one aperture each, 35.76–36.10 mm long, margins
0.60–0.84 mm). The sweep delivered 6 of 6 on this strip, offline.

### 3.4 Step 2c: the production refusals

Of the 63 black-and-white windows on disk from 2026-10-31, the driver
verified 58 (red and luminance agree on every one). Margins 0.42–0.90 mm
leading, 0.51–1.17 mm trailing. Over the 116 verified edges the lit
level next to the edge is a median 0.68 of the window's range, but 5
edges sit below 0.15 and 3 below 0.12 (minimum 0.098); 7 verified frames
have picture content below 0.10 somewhere inside the window. The five
refusals:

| Window | What was in it | Finder's verdict | Rule that failed, measured at the physical edge |
|---|---|---|---|
| strip 15, ap. 1 | roll start, fully blackened | no aperture | no lit run at all (content 0.002 of range) — correct |
| strip 18, ap. 1 | roll start, unexposed clear | no aperture | no plastic step: the window is lit end to end (0.997) — correct |
| strip 18, ap. 2 | roll start, blackened | no aperture | no lit run — correct |
| strip 16, ap. 3 | a picture; dense sky against the leading edge | no aperture | leading edge: gradient 0.60 (rule 0.25), contrast 1.01 (rule 0.50), plastic side at 0.00 — **step 0.076 of range, rule 0.08**. Trailing edge found at 0.886. |
| strip 19, ap. 1 | a picture; bright sky and white foam | no aperture | red: leading edge found (step 0.085) but the trailing run splits into two falling edges and no pair of length ≥ 25 mm survives; luminance: leading **step 0.079, rule 0.08** |

So on the two real pictures the edge is physically there, with a clean
gradient, full contrast and a plastic floor on one side; the one rule it
fails is the absolute step, by 0.001–0.004 of the range. The two refused
frames were later rebuilt by the owner's workflow outside the driver
(from the trailing edge plus the siblings' median length, and from the
same aperture's position on eight other strips, respectively); that is
recorded in the test log, not adopted here.

Five further refusals the same afternoon (strips 22 and 23, one strip
lying crooked in the holder, scanned three times) have no files left and
could not be re-measured; strip 21, the first scan of that strip,
verified 4 of 4 with pictures that contain black gap and a slice of the
neighbouring frame. The coverage rule measures the holder's edges, not
the film's straightness; a crooked strip is an operator matter.

### 3.5 Step 2d: synthetic sweeps from the production strips

Three of the day's strips stitched as in 3.3 (a refused frame placed at
the position its rebuilt sidecar gives, so the stitch has a geometry;
the finder is not told which frames were refused):

| Strip | Frames | Runs found | Resolved | Cut by edges / by pitch | Edge-adjacent content under 0.08 of the strip range |
|---|---|---|---|---|---|
| 16 | 6 | 5 | yes, offset 0.00 mm | 5 / 1 (aperture 3) | aperture 3 leading (0.056) |
| 19 | 4 | 3 | yes, offset 0.00 mm | 3 / 1 (aperture 1) | aperture 1 leading (0.079) |
| 12 | 6 | 5 | yes, offset 0.00 mm | 5 / 1 (aperture 6) | aperture 6 leading (0.049) |

Every sweep resolves, and every frame the per-frame cycle verified is
cut by its own edges or by pitch and re-verifies locally (strip 12's
aperture 6 is the 3.3 case again: lost to the strip-wide range, passed
by its own window's range). The two frames the per-frame cycle refused
are cut at the right place by pitch — but their local re-check fails on
the same 0.076/0.079 step, so the sweep as implemented today would
refuse them too. What the sweep adds for them is information the
per-frame cycle never has: the strip offset from the siblings, the
siblings' measured run lengths in the same pass, and one measured edge
of the frame itself (strip 16, aperture 3: trailing edge at 0.886 of
range). A content-based single-edge rule — one edge measured in the
frame, the length from the siblings of the same sweep, accepted only
when it lands within the load variation of the pitch prediction — would
have delivered both, flagged in the sidecar as what it is.

## 4. What this says about a black-and-white sweep

1. **The window finder does not need IR.** On two real sweeps the
   visible red channel locates the apertures to within 0.03 mm of IR,
   and on real black-and-white content four synthetic sweeps (September's
   six-frame strip and three production strips, 22 frames) all resolve,
   with 17 of 22 apertures on their own edges and the rest cut by pitch
   from the siblings' offset. Luminance is the natural
   channel for silver film (no colour information to prefer); red, the
   per-frame default, behaves identically here.
2. **The sweep is more forgiving than the per-frame cycle, not less.**
   Per frame, a lost edge refuses the frame: nothing but that window
   exists. In a sweep the siblings' edges give the strip offset, a lost
   aperture is still cut at the right place (by pitch, flagged as such in
   `sweep.json` and the diag sidecar), and the per-frame check then runs
   on the cut window with its own local range — which is exactly the
   check that passed these frames in September and on strip 12 today.
   The sweep never delivers less than the per-frame cycle. The numbering stays
   content-based: an aperture number comes from measured runs and the
   holder pitch relative to the sweep's own start, never from position
   alone (`of135i/sweep.py`, numbering rule), and a sweep with no
   plausible run stays unresolved.
3. **The absolute step rule is the weak point, and it is a known one.**
   `EDGE_MIN_STEP` exists to keep noise inside the plastic from
   qualifying as an edge. On silver film it is also what refuses real
   edges next to dense pictures: both production refusals on real
   pictures failed it by less than 0.005 of the range with every other
   rule passed cleanly (3.4), and 5 of 116 verified edges today sit
   within 0.07 of it. Three options, to decide before building: (a)
   leave the finder alone and rely on the pitch cut plus the local
   re-check — delivers exactly what the per-frame cycle delivers, never
   less (3.5); (b) add the content-based single-edge rule of 3.5 in the
   sweep's cutter, where the siblings' lengths and the strip offset are
   measured in the same pass — would have delivered both refused
   pictures, flagged; (c) lower or localise `EDGE_MIN_STEP` — touches a
   detector shared with the holder geometry measurement and every
   per-frame verdict since 2026-09-10, so it needs the 647-frame
   regression set, and is not recommended on five edges' evidence.
   Recommendation: (a) first, (b) as the follow-up once the plain sweep
   has run on hardware.
4. **What the plain path lacks.** Everything hardware-side: a plain-3600
   sweep table (`holder.dual_sweep_geometry` has a dual-only chunk/parity
   model; plain 3600 has one wire line per visible line and its own
   `LINES_PER_CHUNK`/`DEFAULT_LINES`), a `Device._sweep_plain` beside
   `_sweep_dual` (the plain calibration, which caches, instead of the
   dual one), a raw reader for a non-interleaved sweep, and the CLI
   refusal lifted for `--no-ir` with `--dpi 3600`. The long plain-3600
   pass is a new data point for the motor and the chunk engine, as the
   dual one was before Test 111.

## 5. Proposed next steps

1. Decide point 4.3 (owner's call; recommendation: (a), then (b)).
2. *Implemented offline (PR #<n>), not hardware-verified.* Implement the plain-3600 sweep offline with fixture tests: the
   synthetic sweep of 3.3 and the production windows of 3.4 become the
   fixtures; invariants "content-based numbering, never positions alone"
   and "a pitch-located window is never counted as verified coverage by
   itself" written as tests. Strip 16 (six frames, one refused) is the
   natural first fixture: five by edges, one by pitch, local re-check
   refusing it exactly as the per-frame cycle did.
3. One hardware A/B on a black-and-white strip already scanned per frame
   on 2026-10-31 (its per-frame products are the A side): the strip
   through `digitize --sweep --no-ir`, products compared frame by frame,
   eye check, and the operator listening — the plain-3600 sweep moves the
   carriage at a cadence the unit has not run before.

Analysis scripts and profiles stay in the private analysis area; this
document carries every figure they produced.
