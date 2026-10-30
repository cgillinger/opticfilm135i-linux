# Changelog

Notable changes to the Plustek OpticFilm 135i Linux driver and its SANE
backend port. Entries name the hardware evidence (docs/test-log.md) where
one exists; "offline" means implemented and tested without the scanner.

## Unreleased

## v0.2.8 — 2026-10-30

- `digitize --sweep` (135, dual-light profiles): scan the whole strip in ONE
  pass — one calibration, one positioning move, one park — into
  `<roll>/sweep.raw` (streamed to disk chunk by chunk), then cut the frames
  from it in software through the unchanged per-frame writer, each judged by
  the same aperture coverage check. Aperture numbers come from the measured
  run edges plus the holder pitch relative to the sweep's commanded start; an
  inconsistent sweep is *unresolved* (no products, exit 4, `sweep.raw` kept).
  `--keep-sweep` keeps the raw file after a successful roll, `--from-sweep
  --roll N` rebuilds a roll offline from `sweep.raw` + `sweep.json`. The
  per-frame path's command stream is unchanged (the calibration block was
  extracted verbatim). **First hardware run Test 111 (2026-10-30, dual
  3600 with IR, one four-frame strip): 4 of 4 apertures verified, 192 s
  against 277 s per frame, eye-accepted by the owner.** The
  window finder's IR profile registers the colour-line stagger as
  `image.split_ir` does (the first run was unresolved without it and was
  rebuilt from the kept `sweep.raw`). Other profiles and plain 3600 remain
  unverified (docs/whole-strip-sweep-design.md).

## v0.2.7 — 2026-10-28

- Test 110 (2026-10-28, black-and-white 110, offline on the stored frames):
  **no more lost or unnumbered photographs from three gaps.** A hole's
  prediction past an aperture's end is carried to the next aperture (a
  strip's last picture, followed by fogged film); a pass-1 strip seated
  between the placement phases is numbered from its first whole image
  (placement "free", provisional) instead of refused; passes without IR are
  recognised in log intensity.

- Test 109 (2026-10-28, first black-and-white 110 strip, fixed offline on
  the stored frames): **dense black-and-white 110 is detected.** A dark run
  between other columns must be dark at the 95th percentile too to count as
  rail (dense pictures are as dark as the rail in the median); the hole
  search retries with an edge threshold of 0.9 x air when 0.6 finds nothing
  (a black-and-white clear base reads 0.78); side-band coverage decides on
  the bands that see the longest opening (a strip tip only shortens the
  bands it covers). Rebuild of the two stored passes: 4 of 4 delivered.

- Test 108 (2026-10-28, offline on production frames): **a thin, low-contrast
  110 photograph is no longer taken for a blank frame position.** A window
  that reads as clear film along transport must now also be flat across
  (`film110.BLANK_TEXTURE_MAX`); 138 of 142 stored apertures unchanged, the
  4 changes all corrections.
- **`digitize --film 110` scans all six apertures by default** (`--frames`
  default `1-6`; 135 stays `1-4`). A long 110 strip (up to ~8 images, 227 mm
  holder) reaches apertures 5-6; a short one costs nothing, the
  stop-at-empty rule ends the run after the first empty aperture that
  follows film. Offline test: an 8-image strip in both placements numbers
  all eight and leaves nothing missing.
- **110 on the plain path (`--no-ir`): coverage no longer depends on film
  density** (offline). When the central-band verdict fails (for 110 also when
  its opening is not the holder's length -- dense film at a bar), coverage is
  measured on the bare air beside the strip
  (`aperture_crop.measure_coverage_side_band`): column bands that verify
  and show the holder's own opening length (35.92 mm ± 1.0), agreeing
  within 0.3 mm. Manifest `coverage_source: "visible-side"`; otherwise the
  usual refusal, reason naming both failures. 135 and dual paths unchanged
  (docs/film-110.md §10).

## v0.2.6 — 2026-10-26

- Test 107 (2026-10-26, offline): **film in the white reference's light path
  no longer clips a pass.** A 110 strip whose front end lies a little into
  the holder's end at aperture 1 blocked the calibration's white measurement
  over the film band (2–3 % of the clear level over ~2240 columns); the gain
  table over-corrected those columns and visible R and IR clipped inside the
  photographs of every frame of the pass. `calibrate.guard_white_blockage`
  finds a wide region bounded by steep steps (20 of 389 stored calibrations,
  all from strips in that position, none of the clean ones: their steepest
  step is 0.87 against 0.03) and fills it from the newest clean profile of
  the same kind (`of135i/white_reference.py`, kept current by every clean
  calibration; `tools/white_reference_seed.py` seeds it), scaled on the clear
  columns either side; filled real measurements are flat to ±1 % across the
  former band. Sidecar field `white_blocked`. Owner's rule: where the strip
  lies is never the operator's problem. Needs one hardware pass to confirm.

- Test 107 (2026-10-26, first live 110 pass after b4e073b, offline fix): a
  whole photograph lying free in aperture 3 was dropped without a file. Three
  fixes (docs/film-110.md §4, §15.3, §15.7, §15.9): (1) **a hole is air across
  the zone**, not one bright pixel in it — the strip's unexposed edge rim read
  0.85–0.95 × air, rim noise made false 1.5 mm "holes" and the real hole was
  lost in one 4.7 mm air run, so the image window landed 8 mm off (on the
  printed border); (2) a printed-border gap extended past 9.5 mm (into a dense
  white fence) is no longer dropped; (3) a whole image the pass's numbering
  cannot place (off phase, between phases, off slot 2's grid) is written
  **unnumbered** with exit 5 instead of nothing. Old vs new detector over 123
  distinct stored real 110 frames: 119 identical or within 0.2 mm with the
  same classification, three corrected (Test 107 apertures 2 and 3, Testigen
  roll 3 pass 4 aperture 2 — the same failure on the same film, eye-checked),
  one split image's start moved 0.4 mm. The next-pass text now names **which
  photograph to move and why**, never a distance ("move image 3 (now split by
  bar 2) whole into an aperture"); the measured placement stays in
  `advice.next`.

- **Scanned and complete images are never discarded** (owner's founding
  requirement, docs/film-110.md §15.9): when numbering cannot be established
  for a pass, its whole and near-whole images are written unnumbered
  (`p<k>-a<aperture>-<i>.tiff`, manifest `number: null`, `identified: false`),
  the record is `ok` with `numbering: "unidentified"` and never a reference.
  Exit 5 now means "photographs written unnumbered, numbering not
  established", not "nothing written"; a rebuild supersedes them.
- Test 106 pass 3 (2026-10-23, offline): `decide_orientation` accepts a
  cluster on evidence (sum of each distinct new sighting's best correlation)
  as well as on the best-pair margin, so a strip of near-duplicate shots is
  recognised (+22.4 mm slide, 0.96 against 0.84); every earlier stored decision
  is unchanged (docs/film-110.md §15.8).
- Test 105/106 (2026-10-23, offline): gap detection robust to scratches and
  dust (median filter, level-step cuts, extension) and a clear frame is never
  an image when a prediction runs into the gap (it is a blank). A **near-whole**
  copy (one edge within 0.5 mm of the boundary, the other confirmed) is
  delivered and ranked: whole & supported > near-whole & supported > whole &
  free end > near-whole & free end > split. Report rows carry `hidden_mm` /
  `under` / `end_gap_mm`; a free-end rescue uses the measured gap and names the
  plastic the end must just reach under; the impossible case quotes the gap
  (docs/film-110.md §15.6, §15.7).
- Test 104 follow-up (2026-10-23, offline): **the pass report speaks the
  holder's language, with ONE next placement, and numbers count frame
  positions from the strip's front end** (docs/film-110.md §15). Records
  carry `pass_report` (per sighting: number, where = aperture / bar / end
  wall, state, outcome, located_by), `blanks`, `number_basis`
  (`"strip_start"` / `"provisional"`) and `advice.next` (one joint slide of
  the whole strip in quarter image lengths, "towards aperture 1/6"; null when
  nothing is needed or no placement helps) with `advice.not_done`,
  `advice.unseen`, `advice.frame_positions`; `advice.suggest` carries the
  text, the older keys stay one release. Every printed report starts with the
  one-line convention legend (the unconfirmed "aperture 1 = the end that
  enters first" is the single constant `film110_report.APERTURE_1_END`).
  Unexposed frame positions (`ApertureFind.blanks`) consume a number and make
  no file. When a later pass first sees the strip's front end the roll is
  renumbered in-process from its stored frames (the old photographs move to
  `superseded-*/`). The closing text never says "No image numbered" when
  images were numbered. `--from-frames` and the automatic renumbering are
  atomic for the roll (staging directory; a stop or an exception changes
  nothing but one failed record). Shifts are exact on a 0.5 mm grid and the
  text names the visual target (`targets[].end_on` for a free-end rescue); an
  impossible free-end rescue says why (`free_end_too_close`). A predicted image before a lone strip's film start
  (the leader) or past its end (the tail) is dropped and reported as such.
- Test 104 follow-up (2026-10-23, offline): **the holes are never a gate.**
  The printed border between pictures (a flat, dark ~8 mm gap along
  transport) now locates images independently of the perforation holes
  (`film110.detect`, docs/film-110.md §8 step 9): in every aperture that
  has a film band, also when no hole is readable or the band is "not
  adjacent to air". Hole-located frames are unchanged; frame-located ones
  go through the same refinement code and are added when no hole image
  matches (`located_by` `"holes"` / `"frame"` / `"holes+frame"`, in
  `FrameFind` and in every manifest `images` entry). Tuned on Testigen
  rolls 3-7: five apertures whose holes read nothing now yield images.
- `digitize --film 110 --roll N --from-frames` (offline, docs/film-110.md
  §14): rebuild a roll's numbering, photographs and manifest records from
  its stored `p<k>-f<N>.tiff` frames without a scanner, through the same
  code as a live pass; the old `image*.tiff` files are moved to
  `superseded-<UTC>/`, never deleted. Exit 5 when a pass cannot be
  recognised, 2 when refused (nothing touched).
- Test 104, roll 7 (2026-10-22 evening): a strip whose hole edge rests
  flush against the rail -- no air outside the film, the holes still
  reading as air inside the band -- was "no perforation found" on every
  aperture (the edge tracker needs an air->film step). The band edge is
  now the per-line edge on a side that abuts plastic, and both edges are
  searched. Roll 7's photographs 1-3 are found; Test 103's apertures with
  the holes against the rail (five of them, "no perforation found" on
  2026-10-21) now yield their images too. The operator rule "the hole
  edge must not be hidden by a rail" is thereby relaxed to "visible holes".
- Test 104, load 2 (2026-10-22): "the system serves the photographs, not
  the other way round" (Christian). A WHOLE image is never refused for
  its numbering: one found before the roll's first-seen image (an earlier
  pass missed it -- strip 2's first photograph lay under strip 1's
  cassette end) is numbered 0, -1, ... and delivered as `image0.tiff`;
  one that does not fit the roll's grid is delivered as
  `image<N>-uncertain.tiff` (`uncertain: true`, a problems line) and does
  not count as N. `advice.done` also requires that both ends of the
  strip were seen in some pass, or lie under a holder end wall -- a
  strip whose end was hidden is never done (three of four delivered had
  been declared done). A whole image with both edges confirmed is no
  longer dropped as "no strip lies there" in slot 1's last aperture (a
  turned strip 1 reaching into slot 2 lost its image 2), and a content
  cluster is scored by its best pair, not its mean (a 6 mm split
  sighting correlating with nothing had vetoed the right cluster).
  `ApertureFind.film_runs`; slot 1 hands strip 2's start seen in its
  last aperture to slot 2. A pass after a TURNED pass counts numbers
  along the reference's reversed grid (`direction` in `number_by_shift`;
  pass 3 had numbered a three-image strip's middle image "4" and another
  strip's "2" twice). Uncertain sightings never serve as a reference.
  `SHIFT_RESIDUAL_MM` 2 -> 5 (real pitch 24.85–25.5 mm against the
  model's 25.5: three pitches are 2 mm off by themselves). "Done" never
  refuses a run: a pass on a done roll keeps every copy and changes
  nothing ("roll 3 pass 4" must never stop a strip from being scanned).
  Hypothesis pruning by the slot's range tolerates `RANGE_SLACK_MM`
  (3 mm): a four-image strip turned about its own centre lands on the
  same positions, and the true turn had been pruned by 0.5 mm, letting a
  worse turn win and number the strip 5, 4, 3, 2 (roll 6).
- Test 104, first pass (2026-10-22, two strips through the application,
  seated free in the channel): three detector corrections, each validated
  on the pass's frames and on all 24 archived real 110 apertures (identical
  results except where a correction applies). (1) The edge tracker's zone
  now stops short of the rail (`RAIL_TRANSITION_MM`): a strip lying free
  with air on both sides has the rail within 4 mm of its edge, and the
  rail is as dark as film, so the tracker took the rail as the film edge
  and the hole zone fell in air ("no perforation found" on every
  aperture). (2) The plausibility test that drops a clear-film window
  measures the window's interior (`PLAUSIBILITY_TRIM_MM`): the aperture
  edge or the next frame's border at the window's ends made a clear
  leader look "varying" -- Test 103's "image 4 at +0.03 mm" was that
  strip's leader with its start marker, not a photograph (corrected in
  the test log). (3) A hole cut short by the strip's own cut end counts
  as a stub, as one cut by the aperture boundary already did.
  `advice.done` (bool) says whether the roll is done after this run; a
  run that numbered nothing prints "scanned, not done" and a suggestion
  to check the seating instead of "done, nothing missing".
- `digitize --film 110` runs until the roll is done, at any seating
  (hardware-verified 2026-10-22, Test 104: three passes, a turned strip,
  every photograph delivered): every run on an existing roll is a numbered
  pass (`p1-`, `p2-`, ... frame files, `pass` in the record) accepted
  unless the roll is done; the placement is recorded (A, B or `free`) and
  never refuses. From pass 2 the strip may be slid -- by any amount -- or
  turned 180 degrees in its plane: numbering waits for the end of the
  strip and is taken from the roll's own earlier images by RECOGNISING
  them (`film110.decide_orientation`): every sighting, whole or split,
  is compared on the part of the image it shows (`position_mm`, `crop`,
  `extent_mm` on the image entries) with the earlier pass's sightings
  re-read from its frame files, as slid and as turned; what is physically
  possible (the slot) and the strip's own free end seen in both passes
  (`film_start_mm` / `film_end_mm` on the record) prune the hypotheses
  first. Positions alone never decide (the film is periodic: a move of
  more than half a pitch would otherwise number every image one off,
  silently); when the pictures do not decide it, exit 5 and nothing is
  written. `orientation`, `numbering` in the record; products of a turned
  pass are rotated back. Image entries gain `kept` (a copy as good or
  better exists: information, no longer a problem); `advice` is `missing`
  / `free_end` / `suggest` plus the old `images` / `other_placement`; a
  roll is done when nothing is missing and no delivered copy has the strip
  end free. `--force` starts a new epoch of the roll's records (`forced`).
  Detector: `CLEAR_BASE_PERCENTILE` 90 -> 99 (the clear-film reference
  of the plausibility test is the brightest film, not the brightest
  picture: an aperture holding only printed border and pictures read a
  bright, flat picture as clear film; identical results on all 24
  archived real 110 apertures).
- Test 103 (2026-10-21), the first hardware pass of `digitize --film 110
  --strips 2` (two strips, placement A twice; placement B not yet run),
  and what it corrected: a film band narrower than 16 mm is accepted when
  its far edge lies under the rail (`film110.RAIL_HIDDEN_MAX_MM` 3.0,
  `ApertureFind.rail_hidden_mm`, the CLI warns when the picture is
  clipped by the rail); an image seated within `WHOLE_MARGIN_MM` of an
  aperture edge is a leading/trailing split (not "whole: false, split:
  none"); a leading split is counted unless the previous aperture already
  numbered that image (number-based, so image 1 at −0.1 mm and image 4
  at +0.03 mm are first sightings), and a leading prediction without a
  confirmed end edge is the leader's ghost, ignored; a roll without a
  single numbered image is never done (`digitize.roll_is_done`), so the
  other placement's run is accepted on a slot that read nothing. Operator
  rule recorded (`docs/film-110.md` §12): the edge with the small holes
  faces the aperture's open side; holes against the rail give nothing.
  Fixtures: holes model-consistent (2.6 mm long), leading images painted
  as images, a hole after the last image. `tests/test_film110_digitize.py`.
- `digitize --film 110 --strips 2` (offline; hardware check pending, Test
  103): two 110 strips of up to four images in one load, strip 1 against the
  holder's left end (apertures 1-3), strip 2 against its right end (4-6),
  written as two rolls N and N+1 (own directory, products, manifest record;
  record fields `strips`, `slot`, `companion_roll`, `placement_source`,
  `film_found`). Slot 1 numbers like a lone strip; slot 2 is anchored by its
  first detected image (`film110.image_number_anchored`). The empty-aperture
  stop rule is per slot, the guard covers both rolls, either rail works for
  either strip. `--strips 1` (default) is unchanged.
- `digitize --film 110` (offline; hardware check pending, Test 102): a 110
  strip is digitised as a two-run roll — one run per placement into the same
  roll directory. Frame files carry the placement (`a-f1.tiff`, `b-f1.tiff`;
  provisional `x-` stem until an inferred placement is decided), every whole
  photograph is written once as `image<N>.tiff` (+ `-ir.tiff`) by strip
  position, never overwritten except a free-end copy by the supported one
  (sag rule). The roll record gains `film`, `placement`, `placement_given`,
  `images`, `problems` and `advice`; the second run is accepted without
  `--force`, the same placement twice is refused before hardware,
  `roll_is_done` knows the protocol. `tests/test_film110_digitize.py`.
- 110 placement is inferred (offline; hardware check pending):
  `--placement` is optional with `--film 110` on `scan` and `digitize`; the
  first numbered image decides (`film110.infer_placement`, an image more than
  4.0 mm from both phases is refused with a request to give it); an explicit
  `--placement` still overrides and still refuses off-phase images.

## v0.2.5 — 2026-10-20

- Non-strict polls (production telemetry 2026-09/10, 652 frames;
  hardware-verified on one production strip, Test 100 PASS 2026-10-20:
  0 timeouts, every poll settled on the first read, about 9 s per strip
  saved, nothing else changed): at the extended-status sites
  whose captured settled word has the data-ready bit set (`ad55`, `bd55`,
  `a955`), a live word with that bit set is accepted, the criterion the
  SANE backend has used at the same sites since Test 48. The non-strict
  poll floor is 0.3 s (`of135i.device.NONSTRICT_POLL_FLOOR_S`, was 1.0 s;
  strict completion waits unchanged). Every non-strict poll's outcome and
  settle time is recorded in the diag sidecar (`poll_settle`,
  `poll_settle_max_s`). Telemetry: 93 % of production frames timed out one
  to seven of these polls at exactly the old floor, about 9 s per strip,
  never followed by a failure. `tests/test_poll.py`.

## v0.2.4 — 2026-10-20

- Dual (IR) paths measure aperture coverage on the IR channel first and on
  the visible frame only if IR does not verify
  (`aperture_crop.measure_coverage_dual`, `ApertureCoverage.source`);
  recorded as `coverage_source` in the manifest and `coverage.source` in
  the diag sidecar (offline; production telemetry 2026-09/10, 711 frames).
  A verified `digitize` frame whose margin exceeds 2.0 mm
  (`aperture_crop.MAX_PLAUSIBLE_MARGIN_MM`; telemetry maximum 1.10 mm) is
  flagged with `coverage_warning` (the opening found is shorter than the
  holder's, a film end inside it), never failed.
- `digitize` detects an empty aperture (`aperture_crop.film_presence`):
  only the overscan files are written, the entry records
  `film_present: false`, and the strip stops after an empty frame that
  follows film (`skipped_frames`, `stop_reason`; `--no-stop-at-empty`
  scans all) (offline; production telemetry 2026-09/10, 711 frames). An
  opening is empty at 95 % open light (`EMPTY_APERTURE_SHARE`), not 50 %:
  a strip end inside the opening is film and is written (first production
  series 2026-10-20, ten strips: a leader frame measured 0.54).
- The white measurement is guarded before the per-pixel gain table is
  computed (`calibrate.guard_white_dips`, `WHITE_DIP_*` constants): a
  narrow dip (≤ 40 columns, > 1.5 %) present in all three channels — a
  dust speck in the calibration light path, which over-corrected into a
  2–4 % bright line through 36 of 40 frames of the first production
  series (2026-10-20) — is replaced by the running median; the sensor's
  own single-channel dim pixels are never touched (validated on those 40
  exports: all speck dips guarded, no sensor pixel; a guarded dip grows
  over its soft shoulders, `WHITE_DIP_SHOULDER`). First hardware strip
  with the guard: residual at a 9 % speck under 1 %, no dark line in the
  image path (the speck is not in it). Diag `white_dips`;
  `tools/cal_dips.py` lists dips and the host-side correction factor for
  frames scanned before the guard. Python driver only; the SANE backend
  computes its tables in C++ without the guard (follow-up).
- Per-pixel calibration data is exported as `<stem>.cal.npz` next to the
  diag sidecar (`Scanner.last_cal_data`; `--no-cal-data` on `scan` and
  `digitize`) (offline; production telemetry 2026-09/10, 711 frames).
- Console output can no longer abort a command: stdout/stderr are wrapped
  so a closed pipe is swallowed instead of raising `BrokenPipeError`
  mid-scan (offline; production telemetry 2026-09/10, 711 frames).
- Docs: `docs/production-telemetry-2026-10.md` (what 193 strips / 711
  frames of production use say about the driver: the four changes above,
  the time budget behind whole-strip scanning, the per-frame calibration
  vindicated, the shading-noise hypothesis, three items parked);
  `docs/colour-rendering-analysis.md` §6 corrected (the band inside the
  aperture edge is the edge's penumbra, not film base); ROADMAP candidates;
  Test 100 planned (hardware check of the poll change on its own branch).

## v0.2.3 — 2026-10-16
Submission revision v8 on the merge request: the SANE backend no longer takes a process lock, bench-only names are gone from the backend, and the `.desc` status is `:basic`. No change to anything sent to the scanner. See [docs/release-notes-v0.2.3.md](docs/release-notes-v0.2.3.md).

- SANE submission revision v8 (prepared offline 2026-10-15, pushed to the
  merge request 2026-10-16 by the owner: pipeline green, no conflicts,
  description updated, reply posted in the maintainer's thread; not
  hardware-run): the maintainer asked why the backend takes a process lock
  shared with an external driver, so v8 drops the lock from the backend
  entirely. `sane/gl126_lock.{h,cpp}` is now `sane/gl126_mark.{h,cpp}` and
  holds only the magazine mark (same file hardening); the mark file moved
  to `/tmp/sane-gl126-07b3-1436.magazine` (override
  `SANE_GL126_MAGAZINE_MARK`, was `$OF135I_LOCK_FILE` + `.magazine`); the
  test-mode `OF135I_SANE_POLL_CAP_MS` is now `SANE_GL126_TEST_POLL_CAP_MS`.
  The `.desc` entry says `:basic` with "Experimental: developed and tested on
  a single unit". The Python driver is unchanged and keeps its own
  `ProcessLock`; do not run `of135i` commands while a SANE session is open.
  `tests/test_sane_lock.py` became `tests/test_sane_mark.py`. Package:
  `sane/wp3-package/`.
- The offline-checks CI workflow and the two symlink lists in
  `docs/sane-port.md` / `docs/sane-install.md` follow the
  `gl126_lock` -> `gl126_mark` rename (the first CI run of the v8 branch
  failed on the old test-file name).

## v0.2.2 — 2026-10-13
The cold-start fix (first finding admitted through the static-analysis barrier), the readable front-panel buttons, and submission revision v7 on the merge request. See [docs/release-notes-v0.2.2.md](docs/release-notes-v0.2.2.md).

- Cold-start fix (SA-001, 2026-10-12): the hand-written cold homing round
  dropped the vendor's reg 0x32 read-modify-write (set bit 0x02) between
  move 2 and move 3; it is restored in the driver and in the generated
  SANE program (cold_init 218 -> 224 transfers). Confirmed on hardware in
  both implementations (Test 98): the settle read gives 0x1f at once and
  the three per-round settle warnings are gone.
- Front-panel buttons measured (Test 97, read-only): the EP 0x83 interrupt
  byte is a button code (Eject 0x48, Positive 0x20, Negative 0x40, Custom
  0x28 = the bits a press clears in reg 0x31); a magazine insert produces
  no interrupt (reg 0x32 bit 0x40 falls). `tools/watch_events.py` added.
- Button API (2026-10-12): `of135i.buttons` (codes and names),
  `Scanner.poll_buttons()` (read-only, works on a cold scanner through
  reg 0x31 and on an initialised one through EP 0x83), `of135i buttons`
  (print presses) and `of135i watch` (ejects on the Eject button only,
  edge-detected, magazine present). Verified on hardware: all four
  buttons named correctly from a cold scanner (Test 99, 2026-10-13).
- Static-analysis isolation barrier added (docs/static-analysis.md,
  `tests/test_vendor_isolation.py`): findings from vendor-binary analysis
  are recorded separately and kept out of `vendor_derived.py` unless
  captures or our own measurement confirm them.

- SANE submission package revision v7 (2026-10-13, offline): v6 rebased onto upstream a8676a6f7 after the exposure-time revert removed a field gl126.cpp assigned (always 0, assignment dropped; likewise one DISABLE line in genesys.cpp), plus a fifth commit carrying the cold-start fix (Test 98). Built with 0 warnings, 111 symbols, suites 7/6/42 (two test-side changes: the probes get an rpath shim for upstream's `libsane.so.1` SONAME, and the magazine suite checks `scan-exposure-time` only where the option exists), CI replay make-dist 0 and clang BUILD_EXIT=0 with the known local escl_test stop. Pushed by the author to merge request !1032 on 2026-10-13 (fork branch still named `wp3-gl126-submission-v5`); the conflict is gone. The merge request's GitLab pipeline on v7 passed all eight jobs (2943836952) and the merge request is mergeable; still under review, not merged.
- SANE submission package revision v6 (2026-10-02, offline): v5 rebased
  onto upstream `7103e09b0`, after upstream merged a GL128 series
  (Plustek OpticFilm 8200i SE) that touched the same lines. Four commits;
  the `ImagePipelineNodeExtract` fix is dropped because upstream now
  carries an equivalent one. The GL126 source files are unchanged. Built
  standalone with 0 warnings and 111 gl126 symbols; the backend suites
  (7 / 6 / 42) and upstream's `genesys_unit_tests` pass; both recreation
  routes give tip tree `83568e8e…` (Test 96). Pushed by the author to
  merge request !1032 on 2026-10-02 (the fork's source branch keeps the
  name `wp3-gl126-submission-v5` but now holds the v6 commits); the
  merge request shows no conflicts and its pipeline passed all seven
  jobs. Not run on hardware.

## v0.2.1 — 2026-09-29
The state submitted to the SANE project; no functional change. See [docs/release-notes-v0.2.1.md](docs/release-notes-v0.2.1.md).

- The SANE backend was submitted to the SANE project on 2026-09-29 as
  merge request !1032
  (<https://gitlab.com/sane-project/backends/-/merge_requests/1032>),
  by the owner, from his own fork. It is open and under review, not
  merged.
- Dead code removed from `sane/gl126.cpp` (42 lines: an uncalled helper
  marked with the C++17 `[[maybe_unused]]` attribute and two unused
  constants) after the upstream project's clang CI job rejected it under
  `-Werror`; the submission package was re-exported as v5. The compiled
  code is identical (Test 95), and all seven upstream CI jobs pass.

## v0.2.0 — 2026-09-28
The SANE backend, one-button loading, next-strip loading, 110 film. See [docs/release-notes-v0.2.0.md](docs/release-notes-v0.2.0.md).

### Magazine handling
- **`of135i load --next-strip`** (2026-09-25): load the next strip after an
  eject in the same power-on with no jog and no remove-and-reseat step —
  swap the strip, push the magazine in to the stop, run the command. Vendor
  capture (Test 88) showed the vendor app doing exactly this; the driver
  path was hardware-verified the same day (Test 89: feed and traverse
  complete on the first poll, next frame positions identically). Saves
  three motor moves and one reseat per strip. Refused on a cold scanner.
- SANE backend: the same next-strip load happens automatically on the next
  scan after `eject-film` (swap the strip, push it in, scan); the magazine
  mark now records released vs ejected so it works across scanimage calls.
  Hardware-verified from scanimage 2026-09-27 (Test 90): open + load,
  no jog, feed and traverse on the first poll, the frame positioned like
  the one after a full load. The first run found and fixed a backend
  bug: the post-eject precondition required regs 0x3b/0x3c = 0x00/0x00
  (what the jog leaves), but an eject leaves the last scan profile's
  values, so every next-strip load after a scan would have been refused;
  now only the base-table 0xff/0xff is refused.
- Cold start shortened: the 15 s initial wait that could never succeed is
  gone (Test 78); the nine motor completions of the cold start fail closed
  in both the Python driver and the backend.
- SANE backend drives the magazine itself (WP-4): `load-film` /
  `eject-film` buttons and a `magazine` status line; a whole
  load → scan → eject cycle from scanimage and digiKam, including a
  power-cycled, latched magazine (Tests 75–77).

### SANE frontend surface
- **digiKam/KSane dialog cleanup** (offline, 2026-09-27, following the
  owner's report that the dialog was too cluttered to start a scan from):
  every genesys option that never had an effect on GL126 (exposure time,
  brightness/contrast, lamp timing, the whole calibration-cache family,
  colour filter) is now hidden; the magazine/frame controls sit in their
  own `Film` group between `Enhancement` and `Extras`, in the order
  `magazine`, `Load film`, `Eject film`, `Frame`; the backend's default
  mode is `Color` (colour filter defaults to `None`) instead of the
  generic Gray/Green combination this scanner refuses.
- **Two libksane display bugs found and worked around, both backend-side**
  (Test 91, 2026-09-27: a live session with a Swedish translation catalog
  installed failed at the feed): the seven `magazine` status values are
  now deliberately untranslated (plain English constants, `sane/gl126.cpp`)
  because KSaneWidgets' `LabeledCombo` matches a value-list option's
  internal value against a *translated* one, so a translated status never
  follows a backend-side change; and GL126's resolution word list is now
  ascending (600 first) in `genesys.cpp`'s `set_resolution_option_values`,
  because the same widget's constructor matches the bare default number
  against each item's unit-bearing text and always falls back to index 0.
  Following this, the backend's own strings are English-only by design —
  the Swedish catalog feature (`tools/sane_install.sh`, `po/sv.po`) that
  was added earlier the same evening was removed; other dialog strings
  still come from the distribution's own sane-backends catalog, so the
  dialog is mixed-language by design. See `docs/sane-install.md` §6 for
  the walkthrough and the mechanism, and `docs/ROADMAP.md` for what is
  still unverified in a live digiKam session.
- **Status line enabled, reworded to name "Scan"** (offline, 2026-09-27,
  following a second live digiKam session that passed on the mechanics
  but whose operator still needed outside guidance -- Test 91's second
  paragraph, owner's verdict: "no one can do this process without a
  written manual"): `magazine` gets `SANE_CAP_SOFT_SELECT` added
  (`SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT`) so KSaneWidgets renders
  its label and value enabled/black instead of the disabled grey it draws
  for a `SANE_CAP_SOFT_DETECT`-only option; its SET handler is a
  documented no-op (returns `SANE_INFO_RELOAD_OPTIONS`, changes nothing,
  a listed value is accepted and an unlisted one is rejected by SANE core
  before the handler runs). The option is retitled `Magazine -- next
  step`, its seven values now each name the frontend's own button
  ("... push in, Scan" instead of "... push in and scan"), and its
  tooltip (`desc`) spells out the whole load/scan/eject procedure. The
  README gained a matching six-line "digiKam cheat sheet". Seen live in
  Test 92.
- **One-button loading verified on hardware** (2026-09-28, Test 92): from
  digiKam and from `scanimage`, five loads on one power-on (one cold with
  jog, four without), feed and traverse on the first poll every time; the
  120 s timeout and the two refusals (Scan without a load, Load film while
  loaded) caught read-only; Check status correct after every step; the
  `ejected` / `loaded` marks carried the state across processes. The
  frontend limits of the design (instruction visible only for an instant,
  button errors invisible in digiKam, digiKam's own text for the NO_DOCS
  refusal) were all seen live.
- **One-button loading (WP-5)** (offline, 2026-09-27, following a live
  session that passed on the mechanics but whose operator still needed
  outside guidance, and a defect the same session hit for real: a second
  `Load film` press re-jogged and un-seated an already-released magazine,
  failing the following scan at the feed): `Load film` now runs the WHOLE
  flow in one press -- release (skipped when nothing needs releasing), a
  read-only wait (up to 120 s) for the loader sensor's present-clear-
  present edge, then the load. `Scan` never loads the magazine any more
  (`load_document()` is a pure checker of the in-process state and the
  on-disk mark); a new `Check status` button reads the hardware and
  reconciles the status line, without ever claiming Loaded on hardware
  evidence alone. A third mark kind, `loaded`, lets `scanimage -n
  --load-film` hand off to a separate scanning invocation. Status line
  values grew from seven to twelve (`docs/sane-wp5-load-button.md` §3.6).
  Design and implementation notes: `docs/sane-wp5-load-button.md`.
  Hardware-verified the next day (Test 92, above).
- **WP-5 review round two** (offline, 2026-09-27, later the same day --
  an independent reviewer plus the coordinator, nine findings): an
  Ejected-origin retry could lose its no-jog/lenient-regs treatment the
  moment a timeout made it look like an ordinary Released retry, and
  would then have refused Failed on real post-scan register values for a
  magazine that was never jogged loose; a magazine failure now WRITES a
  fourth mark kind (`failed`) instead of clearing the pending one, so a
  second process knows the transport's state was never established; a
  process killed while Load film waits now always leaves a mark that
  blocks the next scan; Check status no longer reports "unknown state --
  power-cycle" for a magazine that is actually loaded (reg 0x101 is not
  idle-class-shaped right after LOAD or during calibration); a
  cross-process `loaded` mark now blocks a second Load film press the
  same way the in-process state does; a cold reg 0x01 read inside one
  process now resets a stale Loaded/Failed claim instead of refusing
  forever; the sensor-clear debounce needs 2 consecutive reads, not one.
  Full list and rationale: `docs/sane-wp5-load-button.md` §9.5. 389
  offline tests pass (was 380). Hardware-verified the next day (Test
  92, above).

### Film and holders
- **110 (Pocket Instamatic) film in the 35 mm strip holder**: `--film 110`
  with a perforation-anchored frame detector, the two-placement protocol
  (`--placement A|B`, numbering by position on the strip), an independent
  edge check (`tools/film110_check.py`). Two strips verified (Tests 86–87).
- Mounted-slide holder characterised (empty-holder capture, then a real
  slide at 600 and 3600 dpi, Tests 84–85); slide support itself is still
  unscheduled.

### Scanning defaults
- Semantic PARK is the default and a per-session calibration cache skips
  repeated calibration on the plain 3600 profile.

### Colour
- The colour cast question closed by measurement: it varies between strips
  and shows in the vendor path too; the driver keeps delivering raw
  negatives, no colour change (docs/colour-rendering-analysis.md).

### Project
- Multi-licensed: driver GPL or MIT, docs CC BY 4.0; CONTRIBUTING and a
  pull-request template; hardware-free CI (offline checks).
- `tools/release_check.py` reports PASS / PARTIAL / SKIP / FAIL truthfully.
- SANE lock and magazine-mark files hardened against hard links and FIFOs.
- WP-3 submission package prepared and rebased onto current upstream —
  prepared only, nothing sent.
- WP-3 package re-exported 2026-09-28 (v3, `sane/wp3-package/`): rebased
  onto upstream `f8b5e16` and refreshed with everything since 2026-09-15
  (strict cold-start completions, the post-eject precondition fix, the
  dialog cleanup, English-only strings, WP-5 one-button loading); builds
  standalone with zero warnings; recreated identically from the bundle and
  from the patches. Still prepared only, nothing sent. The SANE
  project's `tstbackend -l 1` run against that build (Test 93): 22 965
  checks, 0 warnings, 0 errors, zero writes to the scanner.
- **Source comments cleaned for submission** (2026-09-28 evening, v4 of
  the package): every comment and log message in the nine GL126 files,
  the generated tables and the shared genesys hunks that cited the
  project's private documents, test numbers, work packages, review rounds,
  reviewers or dates now states the fact without the citation; one
  pointer to the public protocol documentation added to the headers.
  Compiled code identical (comment-stripped comparison of all 21 files);
  389 offline tests; `tstbackend -l 1` repeated against v4 (Test 94).

## v0.1.2 — 2026-09-12
IR channel alignment fix. See [docs/release-notes-v0.1.2.md](docs/release-notes-v0.1.2.md).

## v0.1.1 — 2026-09-06
Early driver release. See [docs/release-notes-v0.1.1.md](docs/release-notes-v0.1.1.md).

## v0.1.0 — 2026-09-06
First tag.
