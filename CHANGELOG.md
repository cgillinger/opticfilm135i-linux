# Changelog

Notable changes to the Plustek OpticFilm 135i Linux driver and its SANE
backend port. Entries name the hardware evidence (docs/test-log.md) where
one exists; "offline" means implemented and tested without the scanner.

## Unreleased

- **External report, slides on the second unit (test log, 2026-11-08).**
  Two trays of cardboard mounts through the released slide path: seven
  of seven occupied openings verified (margins 2.94–3.64 mm), the empty
  opening refused by the 37 mm bound as designed, 7200 dpi opening 1
  verified, `--overscan 0.5` verified on four openings (default kept).
  His mounts sat branding side up and read the right way round.
- **Slide insertion rule confirmed on both units (docs only).** The
  photographer of the owner's slide confirmed that the Test 124 preview
  is the right way round, and the contributor's mounts sat branding side
  up with pictures reading correctly, so README, ROADMAP and the test
  log now say "the branded or shiny side (the front) up toward the
  holder's open face" (issue #70).
- **Faster dust removal and TIFF writing, same output (PR #83,
  bearyjd).** `remove_dust`
  builds the fill plan once for all channels and reads the summed-area
  tables only at the masked pixels; `write_tiff16` streams the pixel
  block instead of joining a copy; `align_channels` copies once. Pixels
  and written bytes are identical to before (offline suite
  `test_postscan_perf`, compared against a frozen copy of the old code).
  Merged 2026-11-09 after the offline check (test log): 8 new tests,
  release check 756 passed, 54 skipped; the eye check on real frames from
  the owner's unit is still to come.
- **SANE backend: the slide-holder geometry tables (PR #86, bearyjd,
  offline).** `tools/gen_sane_tables.py` freezes
  `holder.slide_dual_geometry()` for openings 1-4 of each dual profile
  into `<KEY>_SLIDES[4]` next to the strip `<KEY>_FRAMES[6]`, and
  `Profile` gains a `slides` pointer (nullptr on plain3600).
  `test_sane_geometry` checks every table against the generator, the
  Profile wiring, and the slide path's FEEDL + wire-travel bound inside
  FEEDL_CEILING (opening 4 ends 175-311 steps inside it). Merged
  2026-11-09; nothing read the tables yet at that point.
- **SANE backend: the `holder` option and the slide travel bound
  (issue #70 step 2 of the v10 list, offline only, not run on
  hardware).** A new string option `holder` (`strip`, the default, or
  `slide`) in the Film group, right before `frame`. With `slide`, `frame`
  means the opening (its range swaps to 1-4 and a higher frame is pulled
  down to 4, with RELOAD_OPTIONS), and the backend positions and sizes
  the pass from the profile's `slides[]` ledger instead of `frames[]`;
  the same POSITION program and the same move, only the FEEDL target and
  the window differ. `gl126_ops` gains `HolderKind`, `kFeedlSlideMax` and
  `slide_travel_end()`; `feedl_for_frame()`/`frame_geometry()` take the
  holder (strip default, every existing caller unchanged) and, for the
  slide holder, also refuse an opening whose FEEDL + wire travel would
  pass FEEDL_CEILING, the driver's own `slide_dual_geometry()` bound.
  Refused before anything is written: opening 0 or 5, and the slide
  holder on plain 3600 dpi (no slide ledger; its window clips a mounted
  slide, Test 85). Parameter reporting stays tolerant of that last
  request (it answers with the strip geometry) so an option change never
  throws; the refusal is sane_start's. Verified offline against a build
  of sane-backends `1d47d7c` with the integration patch: 0 warnings,
  upstream's genesys testsuite PASS, `test_sane_magazine` 43 (one new,
  the range swap and clamp through the real option path),
  `test_sane_open_params` 8 (one new, the session sized from `slides[]`),
  `test_sane_geometry` 10 (two new, the compiled slide path against
  `slide_geom_entries()` and `holder.slide_travel_end()`),
  `test_sane_ops` 52, `test_sane_calibration_cache` 6. The hardware
  run (load, opening 1 and 4, eject through scanimage) is the next step
  and has not happened; the submission package in `sane/wp3-package/`
  is still v9 and untouched.

## v0.2.12 — Shiny Side Slides — 2026-11-08

- **README: a safety section before anything else.** "Read this before
  you connect a scanner": use at your own risk, the OpticFilm 135i
  (`07b3:1436`) is the only supported model, do not patch the product id
  to open another scanner (issue #72), the four rules that apply to every
  unit, and a pointer to docs/hardware-safety.md. Requirements and the
  disclaimer say the same; the SANE paragraph now mentions v9.

- **Capture guide (`docs/capture-guide.md`, issue #72).** How to record
  the manufacturer's software driving the scanner from a Windows virtual
  machine with usbmon, for a unit or model that behaves differently
  (the OpticFilm 135 without infrared). Safety rules first, at the
  reader's own risk; one program at a time, power cycle afterwards; what
  to note and send, and the size/privacy trade-off (`-s 0` vs `-s 256`).

- **SANE submission revision v9 pushed to merge request !1032 (2026-11-08).**
  v8 plus the calibration stop gate (co-authored by bearyjd) and a
  comments-only commit, nothing rebased, `94628f3f2..18bea9b32`; fork
  pipeline 8/8 green. Pushed after v9 had run on the owner's unit (Test
  118) and on bearyjd's unit through the whole test table, 600 dpi, plain
  3600 and 3600 dpi infrared (test log, external reports of 2026-11-07).
  The merge request's description now says two units. Under review.

- **`--slide-positive`: a positive slide written as a positive (issue
  #70 step 3, bearyjd). Run on one real slide.** The switch changes the
  ORIENTATION only: slides already come out uninverted. With `--holder
  slide`, the
  products and overscan frames (visible and IR alike) get the vendor
  orientation the `--positive` path uses (horizontal mirror +
  rot90), and **no inversion**: the values are the raw linear scan,
  written without the sRGB ICC tag (the tag describes a gamma-encoded
  image; this one is linear, so it looks dark in an ordinary viewer). The 2400 dpi per-axis resolution follows
  the turn. `--positive` (the negative's density inversion) stays
  refused with `--holder slide`, as the contributor proposed in #70;
  its message now points at `--slide-positive`. Without the switch a
  slide is written raw, as before, so a mounted negative is untouched.
  Strip output with and without `--positive` is unchanged
  (`tests/test_slide_positive.py`, 7 tests: no `to_positive` call on
  the slide path, the turned values equal the raw ones, no ICC, the
  swapped 2400 dpi resolution tags on all four files, strip
  orientation unchanged, the refusals before the device opens, the
  sidecar's `cli.slide_positive`). Nothing sent to the scanner changes.
  On hardware (opening 1, 3600 dpi dual, cardboard mount): the crop
  verified (margins lead 3.593 mm, trail 3.178 mm); the positive crop
  equals the plain crop of the same opening rotated 270 degrees and
  mirrored (correlation +1.000 on a 96x96 block-mean comparison), it is
  not inverted (mean 6484 against 6461 of 65535), and the slide is
  upright and not mirrored by the owner's eye. One slide, one mount.
  Run on the owner's unit as well (Test 124, 2026-11-08, one pass
  through all three steps: FEEDL 6255, crop verified on the IR channel,
  35.12 mm aperture, the slide whole and upright by eye); two units,
  two slides. Insertion rule from a second pass: the mount's white,
  shiny side (the film base) up toward the holder's open face; the
  picture then reads the right way round with `--slide-positive`.

- **Slide crop keyed on the IR channel (issue #70 step 2, bearyjd).
  Synthetic fixtures, plus one real mount (opening 1, verified).** `scan --holder
  slide` now checks each opening's coverage and crops, with the strips'
  contract: the full overscan frame(s) are always kept, the cropped
  pair (`-o`, with `--ir` also `-ir.tiff`) is written only when
  "coverage VERIFIED", otherwise "NOT verified", no product, exit 4.
  `aperture_crop.measure_slide_coverage()` looks for the slide mount's
  aperture in the IR channel only (the mount is opaque to IR, the film
  is not; visible light was fooled by dark picture content, Test 84),
  with the strip edge finder and binning, and accepts one lit run of
  33.8-37 mm (Tests 84/85: 35.06-35.1 mm; four cardboard mounts on 2026-11-07: 34.16-34.45 mm) whose two margins each hold
  the 1.5 mm crop pad inside the window; the crop is padded by 1.5 mm
  per side. An IR-dense picture band at the mount edge looks like mount
  and shortens the run: with the floor and the pad, apertures up to
  35.3 mm (floor + pad) are kept whole by a verified crop (tested with 0.5-5 mm
  bands). Above 35.3 mm a band can still be cut (a 37 mm aperture with
  a 3 mm band reads like a clean 34.0 mm mount; pinned by a test as the
  documented limit). On hardware (cardboard mounts of several brands,
  3600 dpi dual) the lit run measured 34.16-34.45 mm over openings 1-4,
  so the earlier 34.5 mm floor refused three of four; with the 33.8 mm
  floor opening 1 verified (margins lead 3.567 mm, trail 3.208 mm,
  5184x5289 pair) and the slide was complete by eye; only opening 1 was
  re-run. Shorter runs are NOT verified rather than cut
  (the strip's 25 mm floor would have cropped into the picture and
  called it verified). Also NOT verified, exit 4: a 33 mm mount, a mount
  turned 90 degrees (its ~23 mm side along the transport), the 38.05 mm
  opening itself (no mount, or an IR-translucent mount). The crop is
  along the transport, as for strips. No `--position-offset` hint for
  slides (the margins say where the slide sits in its mount). Sidecar
  `coverage.method: "slide-ir"`, `coverage.crop_pad_mm: 1.5`. `of135i
  recrop` refuses an overscan file whose sidecar says `holder: slide`
  (it would apply the strip check). Nothing sent to the scanner changes.
  `measure_coverage()` gains `min_aperture_mm` (strip default unchanged).
  New `tests/test_slide_crop.py` (14 tests: centred, off-centre and
  dark-grey mounts verified at 600/1200/3600 dpi; edge bands never cut
  by a verified crop; the 35.3 mm guarantee and its limit; the floor
  and the pad margin by behaviour; IR-dense
  edge, empty opening, IR-translucent mount, turned mount and a clipped
  window NOT verified; recrop's refusal; the strip default unchanged;
  the CLI's verified and NOT-verified outputs).

- **`scan --holder slide`: the mounted-slide holder, positioned per
  opening (issue #70 step 1, bearyjd). Run on hardware on one unit.**
  `--holder {strip,slide}`, default `strip` (unchanged, byte-identical
  on the wire). With `slide`, `--frame N` / `--frames` are slide
  openings 1-4 and each is positioned on the #67 grid through
  `holder.slide_dual_geometry()`, with `--position-offset` applied and
  the slide-only travel check; `Scanner.scan(slide=True)` checks that
  whole geometry before the operation, so a refusal sends nothing. What
  goes to the scanner per opening: the strip path's own phases
  (calibration, POSITION, scan, PARK); only the FEEDL (slide 1-4 at
  3600 dpi dual: 6255 / 24000 / 41745 / 59490) and the line count
  differ. Dual-light profiles only (plain 3600 clips a mounted slide
  across, Test 85), default `--overscan 1.5`; plain 3600, `--film 110`
  and `--positive` (a negative inversion, which would invert a positive
  slide) are refused before the device opens. Output: the full overscan
  frame(s) always (`.overscan.tiff`, with `--ir` also
  `-ir.overscan.tiff`), the cropped pair when the IR crop (entry above)
  verifies, sidecar `holder: "slide"`. `digitize` is unchanged (its roll,
  album and 110 workflow is strip-shaped). `tests/test_slides.py` grows
  from 5 to 11 tests (wire FEEDL and line count per opening with and
  without the offset, refusals with zero phases run, the CLI refusals
  before the device opens, one opening end to end off hardware).
  Hardware run on one unit with cardboard mounts of a few brands:
  openings 1-4 at 3600 dpi and opening 1 at 600 dpi, FEEDL as modelled,
  no poll timeouts, lit aperture centred within -0.30..+0.22 mm along
  the transport. Still experimental.

- **Mounted-slide geometry model, library only (PR #67, bearyjd).**
  `of135i/holder.py` carries the slide holder's measured grid from Test
  84 (`SLIDE_FIDUCIAL`: 38.05 mm openings on a 62.6 mm pitch, opening 1
  extrapolated at 23.5 mm) and `slide_dual_geometry()`, the strip path's
  dual geometry with that model, the `--position-offset` shift, and one
  stricter travel check for slides (FEEDL plus the wire's travel must
  stay inside the proven ceiling; slide 4 keeps 175 to 311 steps on every
  profile at the 1.5 mm default overscan). Nothing calls it yet: no
  option selects the slide holder, nothing sent to the scanner changes.
  `tests/test_slides.py` (5 tests) pins the numbers, the per-profile
  FEEDL grid, the refusals and the offset. The rest of C2 (holder
  option, IR-keyed crop, positive output, hardware runs with mounted
  slides) is being built by the contributor on his unit, issue #70.

- **IR dust cleaning scaled by resolution (PR #66, bearyjd).** The
  dust mask's background box, border halo and dilation and the inpaint
  footprints were fixed pixel counts tuned at 3600 dpi; at 600 dpi the
  same counts covered six times the film area, so specks merged into a
  mass mask (Test 84). `image.dust_mask()` and `image.remove_dust()` now
  take `dpi` and convert every footprint to the profile's physical
  sampling, per axis for the anisotropic 2400 dpi profile; `scan --ir`
  and `digitize` pass the scan's dpi. At 3600 dpi the output is
  unchanged: pinned hashes in `tests/test_ir.py`, and a 3000-line band
  of the Test 85 slide cleaned byte-identically before and after. At
  600 dpi on the Test 84 slide the mask fell from 28.8 % to 6.0 % of the
  frame and the owner's eye check found the cleaned picture clearly
  better, with the tree line and sky intact (test log, 2026-11-07,
  offline). Specks within the border halo of the mount edge stay, and a
  thin scratch is only partly removed at 600 dpi (the smallest inpaint
  kernel is one pixel there). Offline; no transfer to the scanner
  changes.

## v0.2.11 — Second Scanner Gamble — 2026-11-06

- **Release codename.** `of135i.__codename__` ("Second Scanner Gamble"
  for this release) is printed by `of135i version` and recorded in the
  sidecar's host block (`driver_codename`), so a report from another
  unit names the release it ran. `tests/test_release_check.py` holds the
  CHANGELOG heading, the version line and the sidecar to it.
- **External reports from the second unit (test log, 2026-11-06 and
  2026-11-07).** The cross-unit check, gated: 4 of 4 completed, exit 0,
  coverage verified at frame 1 with the slide holder. SANE v9 through its
  test table on that unit: load (7 of 10 cold loads over all runs, the
  misses with the known "transport ran, cassette not taken" signature;
  from the reg 0x32 write to the end of the load the backend's transfers
  match the Python driver's), scans at 600 dpi, plain 3600 and 3600 dpi
  infrared, and eject, all completed. Every one of the 21 reg 0x100 gate
  polls on that host found the engine still running and waited 9–26 ms;
  none timed out. Calibration offsets identical to the owner's unit,
  gains within two codes. v9 is ready to push.

- **Per-unit position offset: `--position-offset MM` on `scan` and
  `digitize` (issue #61).** The corrected mean mapping was fitted on one
  scanner. On a third unit (the Steam Deck of issue #35) every frame sat
  about 0.5 mm earlier in the scan window, on all six frames and both of
  that user's holders, so frames 4 and 5 sat at the 0.15 mm floor
  (leading margin 0.10–0.14 mm where refused) and were refused on some
  strips and passed on others (sidecars of 2026-11-05, the test log's
  external report). The flag moves every scan and sweep window
  along the strip by a per-unit amount (`holder.FiducialModel.shifted`;
  negative = earlier). Margins, window length and chunk count are
  unchanged; the transport ceiling is checked after the shift, before
  anything is sent; at most ±3 mm. The `OF135I_POSITION_OFFSET`
  environment variable holds a unit's value as the default; the flag
  overrides it. Recorded in the sidecar
  (`position_offset_mm`, `cli.position_offset`) and in `sweep.json`. A
  coverage failure whose two margins are known now ends with the value
  that would centre the opening (e.g. `--position-offset -0.6` for that
  unit's frame 4). Offline-verified (`tests/test_position_offset.py`,
  17 tests) and hardware-verified on the owner's unit (Test 120: three
  scans at 0, −0.3 and +0.3 mm, FEEDL moved by exactly the offset, the
  margins followed it); not yet run on the unit it is for. Reported by
  Krustenviech.
- **`digitize --album NAME`: one folder per film, photographs numbered
  01–36 through the strips (issue #65).** Each strip keeps its
  `roll-NNN/` with the holder's `f1`–`f6`; the album `<out>/NAME/` holds
  `NN.tiff` and `NN.diag.json` for every frame with film, numbered
  gap-free in strip order. An empty opening takes no number; a frame
  scanned but not delivered (coverage not verified) reserves its number
  and is listed as missing in `NAME/index.json` until `recrop` delivers
  it. The numbered files are hard links to the roll products; the album
  is a view rebuilt from the manifest after every strip and by the new
  `of135i album -o DIR NAME` command, so a `--force` re-run renumbers
  what follows. 135 only. Offline-verified (`tests/test_album.py`, 5
  tests). Requested by Krustenviech.
- **`of135i recrop FILE...`: the cropped products from a frame's preserved
  overscan files, without scanning again (issue #61).** A frame whose
  coverage did not verify keeps only `fN.overscan.tiff` (and
  `fN-ir.overscan.tiff`); most such frames hold the whole picture. The
  command un-rotates those files, measures coverage as the scan did (IR
  first on a dual frame), refuses an empty opening (the `digitize` rule;
  `scan` does not check for film), crops both channels
  to the same lines and writes `fN.tiff` and `fN-ir.tiff`, with a margin
  floor the operator chooses (`--min-margin MM`, default the scan
  commands' 0.15; `--force` accepts any located edge). Resolution and
  rotation come from `fN.diag.json` (which now also records the command
  that wrote it); a `scan --positive` frame is refused, a `digitize
  --positive` one is not (its overscan is the negative); an existing
  product needs `--overwrite`; `--dry-run` reports only. Exit 2 (a frame
  not processed) outranks exit 4 (a margin failure). The
  overscan files are never altered and dust removal is not repeated (the
  visible overscan frame of a dual scan is already the cleaned image). A
  `fN.recrop.json` records the run; the digitize manifest is not changed.
  Offline-verified (`tests/test_recrop.py`, 7 tests).
- **SANE backend: file headers credit the gl124 files they started from.**
  `gl126.cpp`, `gl126.h` and `gl126_registers.h` began as copies of the
  genesys backend's gl124 files (docs/sane-port.md, stage 1) and named
  only this project's author. They now carry "Portions derived from"
  lines with the original copyright holders, Stéphane Voltz and Povilas
  Kanapickas. The banner of the generated tables no longer says that
  reads and polls are not reproduced (the op programs carry them). Both
  are comment changes; no code differs. A stale docstring in
  `of135i/vendor_derived.py` is corrected.

## v0.2.10 — 2026-11-05

- **Production confirmation (Test 119).** One colour strip through the
  calling application with `digitize --sweep`: the first whole-strip
  sweep with the stop gate as default and the first hardware run of
  `digitize` after the two-strip removal. Status ok, 4 of 4 apertures by
  edges, seven gate waits cleared at the first read, device phases as in
  Test 111 (calibration 6.1 s, position 1.8 s, scan 161.4 s, park
  10.3 s).
- **SANE backend: calibration stop gate in the generated op programs.**
  After the SCAN-clear that ends each calibration acquisition, the
  backend's programs read reg 0x100 and reg 0x101 once and did not wait,
  so on a fast host the next acquisition could be armed while the
  previous one was still running; the backend would be expected to fail
  calibration there as the Python driver did (issue #35), though it was
  never run on such a host. The
  generator now emits those two reads in the seven acquisition programs
  as polls for bit 0 clear (300 ms each; the plain-3600
  `cal_shading_measure` site keeps its class-F match and 5 s), and a
  timeout reports "the previous acquisition did not stop". A control read
  that returns no data no longer counts as a match in any masked poll.
  Transfers are unchanged when the scanner has already stopped. Offline
  tests in `tests/test_sane_ops.py`. Hardware, the developer's unit
  (Test 118): frame 1 at 600 dpi and at plain 3600 dpi completed; at
  plain 3600 three of the fourteen polls waited 11-18 ms for reg 0x100,
  where earlier builds went on. Not run on a unit that shows the failure
  yet. Exported as revision v9 of the SANE submission series
  (`sane/wp3-package/`: v8 plus one commit, base unchanged; standalone
  gcc build and a clang 17 `-Werror` build of the genesys backend without
  warnings), not pushed to the merge request. The wait was designed by bearyjd for the Python
  driver; the failure was reported by Krustenviech (#35).

- **Removed: `digitize --film 110 --strips 2`.** The mode let two short 110
  strips share one load of the six-aperture holder (slot 1 = apertures 1-3
  as roll N, slot 2 = apertures 4-6 as roll N+1; docs/film-110.md §12). It
  ran on hardware in Tests 103 and 104; one strip per load is the routine,
  so the option, the per-slot code and its tests are gone. A one-strip run
  behaves exactly as before: 27 scenarios (110 colour and black-and-white
  passes, rebuilds, a turned strip, 135 per-frame and sweep, failures) gave
  the same exit codes, output and file hashes before and after. Offline,
  no hardware run. Three tests that used the two-strip harness for
  one-strip behaviour were kept as one-strip tests. A roll whose manifest records `"strips": 2` is still
  refused by `--from-frames`, with a message that no longer names the
  removed option.

- **CI runs the whole offline gate; version string corrected.** The
  GitHub Actions workflow now calls `tools/release_check.py --no-backend`
  instead of its own list of suites, which had fallen behind: ten core
  suites, among them the stop gate's and the cross-unit tool's, ran only
  locally. `of135i.__version__` said 0.2.7 while the package metadata
  said 0.2.9, so `of135i version` and the cross-unit report printed the
  older number; both now say 0.2.9 and a test holds them equal. Offline.

- **Watch eject failures exit 75** (breaking for wrappers: a failed watch
  eject used to exit 1): a raised exception or Ctrl+C inside `eject()` ends
  the watcher immediately with the session diagnostics. 75 is `EX_TEMPFAIL`
  ("retry later") but here means the opposite: power-cycle, do NOT restart
  automatically. Supervisor config is required (systemd:
  `RestartPreventExitStatus=75`). Idle Ctrl+C still exits 0; Ctrl+C in the
  read-only magazine-presence check exits 130; startup errors and eject
  refusals that sent nothing (`refused: ...`, e.g. the unejectable-state
  guard) still exit 1. Unexpected exception types also print a traceback;
  the error text is printed once. The README and `watch --help` carry the
  same exit-code text (test-enforced). CLI-only, offline-tested: no motor
  sequence, wait, or device API changes. Existing eject completion timeouts
  still warn and return success, so they do not produce 75. Adapted from
  bearyjd (https://github.com/bearyjd), commit 22c955b in
  bearyjd/opticfilm135i-linux.

- **`--cal-wait-scanstop enforce` is now the default** (`scan`, `digitize`,
  `Scanner.cal_wait_scanstop`). Evidence: Krustenviech's A/B on the Steam
  Deck, the unit that reported issue #35 (two ungated runs failed in
  `cal_dark_b`; six gated runs / seven frames completed, longest wait 29 ms),
  and bearyjd's A/B on a second unit (ungated 0 of 3, gated 7 of 7, longest
  wait 24 ms); the developer unit completed all 14 gated frames of Tests
  114-115, and in Test 116 a scan with no gate option ran gated (the default,
  on hardware). Across the three units ungated scans failed 5 of 5 on the two fast
  hosts; gated scans completed on all. `--cal-wait-scanstop off` stays and
  sends exactly the transfers of a driver without the gate. Not run with the
  gate on hardware: 1200/2400/7200 dpi, the whole-strip sweep, 110 film
  (docs/cal-scanstop-gate.md, "Decision"; the slide holder has run gated,
  bearyjd's seven scans, confirmed afterwards). A
  gate timeout is a fail-closed `CalibrationError` needing a power cycle. The
  residual-dark_b error now names the gate when it is off; the
  `_healthy_dark_b` docstring no longer claims frame 1 is always healthy.
  Tests that compare the wire with the vendor captures state `off`
  explicitly. Thanks to Krustenviech (reported the failure, ran the Deck A/B)
  and bearyjd (designed and wrote the gate, ran the first A/B).
- **Cross-unit protocol made gentler on other people's units.** The default
  sequence is now `on,on,on,on`: the ungated failure is already established
  on the affected units and repeating it only costs a power cycle. A sequence
  with `off` still works and prints a NOTE that the ungated scan is expected
  to fail on an affected unit (harmless, but the tool stops and the scanner
  needs a power cycle with the magazine loaded). The plan and the final
  printout now say what to do after the run: eject command on success; after
  a failure or interrupt send no command (a motor command from an unknown
  state), power-cycle, `load --release` if the feed does not engage, never pull
  a magazine by force. docs/cross-unit-protocol.md gets a Risks section split
  into what could damage the scanner and what only costs a scan or a power
  cycle. The new default sequence ran once on the developer unit (Test 116); the
  tool has not been run on a second unit.
- **Stop gate confirmed on a second unit (external report, issue #35).**
  bearyjd ran the A/B asked for on master: gate off 0 of 3 scans completed
  (all fail at `cal_dark_b`), `--cal-wait-scanstop enforce` 3 of 3 plus four
  warm-up scans, longest wait 24 ms against the 0.3 s budget. His sidecars
  compared with Test 114's: the two units calibrate to nearly the same offsets
  and gains; his host completes a control read about ten times faster (0.39 ms
  against 3.9 ms), which is why it reaches the next acquisition while the
  engine still runs. Reading, not proof: the failure follows the host's pace,
  and the developer unit is hidden by its slower host rather than immune.
  docs/cal-scanstop-gate.md ("Second unit, on master"), docs/test-log.md.
  (Superseded by the entry above on the default.)

- **Diag: cr mismatches are now locatable.** Each control-read reply that
  differs from the capture is recorded in the `.diag.json` as
  `cr_mismatch_details` (phase, op index, wv/wi, got/want; first 20 per
  scan; `cr_mismatches` still counts all). Reason: docs/status-read-inventory.md
  section 6 item 4. Offline, no wire change.

- **Cross-unit protocol (run on the developer unit, Test 115).** `tools/cross_unit_check.py` runs a fixed
  doctor / load / four-scan sequence (`--cal-wait-scanstop enforce,off,off,enforce`),
  stops on the first failure without sending any recovery command, and packs the
  sidecars, doctor reports and log into a tarball without images or personal paths.
  Protocol in docs/cross-unit-protocol.md; tests in tests/test_cross_unit_check.py.
  Review by bearyjd, who asked for each step to say what it sends, led to:
  the printed plan, `plan.txt` and a table in the protocol doc now state for
  every step whether it only reads, writes registers or moves the film holder
  (marked MOTOR), what the operator must do (`load` jogs, prompts you to take
  the magazine out and back in, and needs a real terminal) and what the step is
  for, with the scan broken down by phase and the code or doc each claim rests
  on; calibration acquisitions are listed as "may move the motor" because the
  repo does not establish otherwise. `--dry-run` now writes nothing, not even
  `plan.txt`. The default sequence was `on,off,off,on` here (changed to `on,on,on,on`, see above) (a gated run first, still
  symmetric). A scan that exits 4 but left its sidecar (the image check, not
  the transport or calibration) is recorded as `completed-coverage-unverified`
  and the run goes on; every run has an `outcome`. A failed scan whose output
  contains `dark_b is residual` gets `failure_class: dark_b_residual` and is
  reported as the issue #35 failure. Ctrl-C or an unexpected error still writes
  `summary.json`, `report.md` and the tarball, and sends nothing more to the
  scanner. The first hardware run found that the tool missed the sidecars
  (`--frames` makes the driver name outputs `-f<N>`) and packed the scanned
  images into the tarball; sidecars are now found by what is on disk, and the
  archive is built from an allowlist and verified after packing. A second
  hardware run with the fixed tool confirmed both (Test 115, developer unit).
- **Status-read inventory (offline).** `tools/status_read_inventory.py`
  lists every read of reg 0x100/0x101 in the op tables with the
  engine command before it; docs/status-read-inventory.md judges the
  142 "read once after a stop or start, no wait" cases: 111 are the
  calibration acquisitions of issue #35 (gated by
  `--cal-wait-scanstop`), 27 are the image-pass start where bulk IN
  does the waiting, 4 are a POSITION pre-move read behind the gate and
  a strict completion poll. Policy: no new lone read after a stop or
  start; per-phase mismatch records in the diag are the next step. No
  code change to the driver. Wording after review by bearyjd: the 142 is
  stated under its counting rule, "polled" is distinguished from "waited"
  (non-strict polls outside JOG/LOAD/POSITION only log on timeout), and
  categorical phrasings are hedged as our reading of the tables.
- **Opt-in transfer recorder** (`OF135I_TRACE=/new/path/run.jsonl`):
  records transfers passing through the guarded USB layer, with bounded
  control/OUT payloads and bulk-IN lengths only. Unset or empty leaves
  recording off; no command, retry or sleep is added. Files are created
  exclusively with mode 0600; recording failures warn once and disable
  recording without changing USB results. Trace files close before warning;
  recorder warnings remain best effort even if a logging handler fails.
  `tools/trace_to_compiled.py`
  converts the diagnostic JSONL to compiled traces for offline comparison
  (payloads beyond 64 bytes are incomplete; not a replay capture).
  Standalone tests cover failure handling, conversion and the upstream
  wire digests with recording on/off (offline). One hardware pair on the
  developer unit (Test 117): a 600 dpi scan with the recorder on matched
  the same scan with it off in calibration, timing and output size, and
  the 2336-record file was as documented; the failure paths ran offline
  only.
  Credit: adapted from bearyjd (https://github.com/bearyjd),
  commits a02b4b6 and 6c2be93 in bearyjd/opticfilm135i-linux.
  Review fixes (offline): Ctrl-C while recording disables the recorder
  and never replaces a failed transfer's exception; reads record the
  requested size and mark short reads; the converter validates the header
  and records, reports damaged lines (refusing unless `--skip-damaged`),
  emits `null` + `"partial": true` instead of padded/truncated payloads,
  follows compile_trace.py timestamps and writes atomically; and
  `tools/replay_trace.py` refuses partial or data-less write ops before
  opening the device. Round 2 (offline): record data must be strict hex
  of exactly `min(len, 64)` bytes (converter) and strict hex (replay);
  every converted op carries `"src": "of135i-trace/1"` and replay refuses
  it (converter output is diagnostic only; committed captures unaffected);
  the converter copies into place where hard links are unsupported;
  `gen_tables.py` refuses partial or null-data write ops; documented the
  converter exit codes 0/1/2 and that a Ctrl-C during recording of a
  failed transfer is only a note on the USB exception.
  Round 3 (offline): converter output is a dict
  `{"format": "of135i-trace-converted/1", "ops": [...]}`, which replay
  refuses as not a list (stripping `src` cannot undo it; committed bare-list
  captures unchanged); the failed-copy cleanup unlinks only the file it
  created (device/inode check) and cannot mask the original error; docs
  qualify atomicity (hard-link path atomic, copy fallback not under a hard
  kill).
- **Docs: the driver is no longer single-unit.** README, CONTRIBUTING and
  the ROADMAP now say what is true as of 2026-11-03: the driver has
  scanned on two units besides the maintainer's (a Steam Deck and a fork's
  unit, issue #35), both of which show an intermittent calibration failure
  this unit never has; the fix below came from one of those users. README
  gains an Acknowledgements section. No code change.
- **Calibration stop gate, opt-in** (`scan`/`digitize --cal-wait-scanstop
  {off,enforce}`, default `off`; `Scanner.cal_wait_scanstop`;
  docs/cal-scanstop-gate.md). Issue #35: on a Steam Deck the residual
  dark_b of Test 32 appears on frame 1, which Test 33's substitution
  cannot cover. Our tables read registers 0x100/0x101 once after every
  calibration SCAN-clear and move on; a fast host re-arms the next
  acquisition while the engine still runs. `enforce` polls reg 0x100 bit 0
  (data enable) and then reg 0x101 bit 0 (motor enable) clear before the
  next OUT transfer, within the existing 0.3 s non-strict poll floor, and
  fails closed (`CalibrationError`, power cycle) at the deadline; waits are
  recorded in the `.diag.json` as `cal_scanstop_waits`. Ported from
  bearyjd's fork (`feat/cal-wait-scanstop`, f462b0b) with its offline tests
  (`tests/test_cal_wait_scanstop.py`, which also pins the default wire
  against pre-gate digests). Hardware: Test 114 (owner's unit, 2026-11-02)
  ran it off/on/on/off over frames 1–4 at 3600 dpi plus gated 600 dpi and
  IR frame-1 runs: harmless (every wait ≤ 8 ms against 0.3 s) but the
  residual dark_b did not occur in any frame, so its effect is unproven;
  stays opt-in until the issue's reporter runs it. The SANE backend has no
  gate. (Superseded by the entry above on the default: the reporter and a
  second user have run it, and `enforce` is now the default.)
  Credit: the mechanism, the code and the offline tests are by bearyjd
  (https://github.com/bearyjd); the report and the Steam Deck logs are by
  Krustenviech (https://github.com/Krustenviech), issue #35.
- **Sweeps longer than four apertures are refused** (`sweep.SWEEP_MAX_APERTURES_VERIFIED`,
  checked in `digitize --sweep` before any hardware step, exit 2, and in
  `Scanner.sweep`). Every hardware-run sweep so far covered 1–4 or 1–2;
  after Test 112 the owner decided (2026-10-31) that a six-aperture colour
  sweep gets its own hardware test (Test 113 plan,
  docs/whole-strip-sweep-design.md) before it is allowed. Offline change;
  the per-frame cycle covers 1–6 as before.
- **Plain-3600 whole-strip sweep WITHDRAWN after Test 112** (2026-10-31,
  `docs/test-log.md`). `digitize --sweep --no-ir` exits 2 before any
  hardware step and `Scanner.sweep(ir=False)` raises, both naming the
  test; black-and-white film keeps the per-frame cycle. The first (and
  only) hardware run of the path completed its scan pass but the
  acquisition had started ≈ 19 mm later along the film than the geometry
  assumed, the magazine ended displaced against the slot's front edge and
  the park stalled (power cut by the operator). The dual sweep is
  unaffected. The code path (`holder.plain_sweep_geometry`,
  `Scanner._sweep_plain`, `sweep.visible_profile`,
  `find_windows(wire_per_vis=1)`, `sweep.json` `dual`/`wire_per_vis`) stays
  in the tree for the record, unreachable from the CLI.
- Recovery finding (vendor capture 2026-10-31, same entry): when the
  status word shows the loader bit 0x08 clear at app start, the vendor
  runs the LOAD traverse (mode 0x1c, 71 490 steps) BEFORE the app-start
  jog, which re-seats the cassette; our `cold_init` jogs directly and
  stopped fail-closed on the displaced magazine. Owner's decision: the
  driver gets this recovery (offline from the capture, then its own
  hardware test).
- **Cold start re-seats a displaced cassette first** (offline, from the
  2026-10-31 vendor capture; NOT hardware-verified, hardware test pending
  on the owner's go). `Scanner.cold_init` now runs the LOAD flow's
  traverse (`_cold_reseat_traverse`: mode 0x1c, 71 490 steps, loader slope
  tables, strict completion against `load_completion_target()` under
  `LOAD_STATUS_MASK`, 30 s budget, fail-closed) between the first
  pre-move status read of homing round 1 and the jog, only when a magazine
  was present at the opening status word (loader bit set, read before any
  table is written) AND the loader bit reads clear at that pre-move read.
  Every other cold start is byte-identical on the wire to before
  (golden op lists, `tests/test_cold_reseat.py`). The SANE backend's
  cold start does not have the step yet (`docs/sane-port.md`).
- Earlier the same day (PR #42, merged, now superseded by the
  withdrawal): the plain sweep had been implemented offline with an
  anchoring gate limiting it to four apertures; `_scan_plain`'s
  calibration block was extracted into `_plain_calibration` with the
  phase sequence unchanged (kept).

## v0.2.9 — 2026-10-30

- Progress lines during the sweep: `scanning frame N @ <dpi> dpi (sweep pass,
  aperture N of first–last, P%)` as the pass reaches each aperture's
  predicted leading edge, `cutting frame N from the sweep (...)` per cut
  (PR #37). Test 111 production confirmation, evening 1: seven colour-135
  strips through the calling application, all exit 0, 4/4 apertures by edges,
  device phases identical to the A/B run (`docs/test-log.md`). Every
  production strip counts as test data from here on.
- CLI exit code 3 when another of135i process holds the scanner lock
  (`ScannerBusyError`), distinct from 1 (a real failure), for every
  hardware command that goes through `_run_writing_session` or
  `loadflow.run` (including `digitize`). The message now says "No power
  cycle is needed: nothing was sent to the scanner", and `digitize` /
  `digitize --sweep` print "roll N not started", write no manifest record and
  do not consume the roll number (the old "Power-cycle before the next strip"
  line is gone for this case). Production observation 2026-10-30: a lock
  refusal during a next-strip load was read as a hardware failure by the
  calling application and the operator power-cycled needlessly; nothing had
  been sent. Offline only (no hardware evidence needed: no command stream
  changed).

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
