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
| Black-and-white strip, per-frame overscan windows | plain 3600, `--no-ir` | 6 | Test 60 (2026-09-10) |
| Production black-and-white strips, per-frame overscan windows and diag sidecars | plain 3600, `--no-ir` | 16 strips, 7 refused apertures among them | Test 111 session 2 (2026-10-31) |

For scale: the driver's hardware log (`docs/test-log.md`) holds 111
numbered tests since 2026-08-30; the production telemetry study
(`docs/production-telemetry-2026-10.md`) analysed 711 delivered frames;
the whole-strip sweep has run on 10 production colour strips plus the A/B
pair as of this date.

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
3. **Step 2b, a synthetic black-and-white sweep.** The six Test 59
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

*Pending: the seven apertures refused on 2026-10-31 (two on real
pictures — a dense sky against the leading edge; a bright sky with white
foam hiding both edges — and five on one strip lying crooked in the
holder) are being re-evaluated against the edge finder's criteria. This
section is filled in when that run is done.*

## 4. What this says about a black-and-white sweep

1. **The window finder does not need IR.** On the one real sweep the
   visible channels locate the apertures to within 0.014 mm of IR, and on
   real black-and-white content a six-aperture synthetic sweep resolves
   with four of six runs and cuts all six. Luminance is the natural
   channel for silver film (no colour information to prefer); red, the
   per-frame default, behaves identically here.
2. **The sweep is more forgiving than the per-frame cycle, not less.**
   Per frame, a lost edge refuses the frame: nothing but that window
   exists. In a sweep the siblings' edges give the strip offset, a lost
   aperture is still cut at the right place (by pitch, flagged as such in
   `sweep.json` and the diag sidecar), and the per-frame check then runs
   on the cut window with its own local range — which is exactly the
   check that passed these frames in September. The numbering stays
   content-based: an aperture number comes from measured runs and the
   holder pitch relative to the sweep's own start, never from position
   alone (`of135i/sweep.py`, numbering rule), and a sweep with no
   plausible run stays unresolved.
3. **The absolute step rule is the weak point, and it is a known one.**
   `EDGE_MIN_STEP` exists to keep noise inside the plastic from
   qualifying as an edge; measured against a whole strip's range it also
   discards real edges next to dense black-and-white pictures. Two
   options, to decide before building: evaluate the finder per predicted
   aperture segment (local range, as the per-frame check effectively
   does), or leave the finder alone and rely on the pitch cut plus the
   local re-check, as 3.3 shows works. The second changes nothing in a
   shared detector and is the conservative choice.
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

1. Finish 3.4 (offline).
2. Decide point 4.3 (owner's call; recommendation: conservative).
3. Implement the plain-3600 sweep offline with fixture tests: the
   synthetic sweep of 3.3 and the production windows of 3.4 become the
   fixtures; invariants "content-based numbering, never positions alone"
   and "a pitch-located window is never counted as verified coverage by
   itself" written as tests.
4. One hardware A/B on a black-and-white strip already scanned per frame
   on 2026-10-31 (its per-frame products are the A side): the strip
   through `digitize --sweep --no-ir`, products compared frame by frame,
   eye check, and the operator listening — the plain-3600 sweep moves the
   carriage at a cadence the unit has not run before.

Analysis scripts and profiles stay in the private analysis area; this
document carries every figure they produced.
