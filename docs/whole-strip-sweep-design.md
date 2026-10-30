# Whole-strip sweep: one calibration, one position, one pass, one park

Status: design and implementation spec, 2026-10-30. **Hardware-run once**
the same day (docs/test-log.md Test 111: dual 3600 with IR, one four-frame
strip, 4 of 4 apertures verified, 192 s against 277 s per frame,
eye-accepted by the owner). The other dual
profiles and plain 3600 are not run. Section 5's IR profile registers the
colour-line stagger — the first run's post-processing was unresolved
without it.

## 1. Why

Production telemetry (`docs/production-telemetry-2026-10.md` §2.4, 652
frames, `digitize` on dual 3600 with IR) puts a four-frame strip at about
240 s: 160 s of scan passes, 32 s of calibration (four of them), 28 s of
park returns (four, growing with the frame number) and 20 s of
positioning. Every vendor application parks after every frame
(`docs/protocol-notes.md` pass 14). The driver does the same today, so the
carriage makes four full return trips per strip.

A whole-strip sweep replaces the per-frame cycle with: one calibration,
one POSITION to the first aperture's leading edge, one scan pass over
all the apertures, one PARK. On the telemetry numbers that removes about
70 s per four-frame strip and three carriage returns. It is the first
step of the motor-sequence saving decided 2026-10-22 (whole-strip sweep
first; frame-to-frame dead reckoning as a later refinement). The frames
are cut from the sweep in software, each judged by the same aperture
coverage check as today.

## 2. What the hardware has already demonstrated, and what it has not

The scan pass is a chunked read whose length is one 24-bit register:
`lines=` already drives it (the dual path's documented diagnostic /
capture-replay path), and the vendor's own per-resolution captures are
whole-strip sweeps on this unit: 12378 programmed lines at 600 dpi
(`tables_dpi600.CAPTURED_LINES`), 48390 at 2400 dpi (~254 mm of travel,
mode 0x30, the same per-dpi tables). For the 600 and 2400 dual profiles a
sweep over four or six apertures is a SHORTER window than the vendor's
own.

**Dual 3600 with IR, the production profile, has no such capture:**
`tables_ir.CAPTURED_LINES` is 10622 (one frame). The mechanism is
identical (same registers, same 16-line chunks, the engine completes when
every chunk has been read) but a long dual-3600 pass is a new data point,
as plain 3600 is. It gets one hardware A/B before it is relied on. Plain
3600 is out of scope here (the sweep refuses it; section 6).

Sizes at 3600 dpi dual (W = 5184 px, 6 bytes/px, two wire lines per
visible line):

| frames | start (motor units) | end (motor units) | span | wire lines | chunks | raw bytes |
|---|---|---|---|---|---|---|
| 1–4 | 1227 | 44089 | 151.2 mm | ~42900 | ~2680 | 1.34 GB |
| 1–6 | 1227 | 65554 | 226.9 mm | ~64400 | ~4020 | 2.00 GB |

Both ends stay under the load traverse (`holder.FEEDL_CEILING`, 71490),
which `holder.check_feedl` guards before any write. The raw buffer is far
too large for the per-frame flow's `b"".join` + ndarray copies, so the
sweep streams every image chunk to a file as it arrives and post-processes
from a read-only memory map (section 4).

## 3. Geometry

`holder.dual_sweep_geometry(first, last, *, dpi, lines_per_chunk,
colour_crop_lines, default_wire_lines, overscan_mm)` is
`dual_overscan_geometry` for a window that starts `overscan_mm` before
aperture `first`'s leading edge and ends `overscan_mm` after aperture
`last`'s trailing edge, on the same STRIP_FIDUCIAL mean mapping:

- `want_start = leading_hwdpi(first) - overscan`, `want_end =
  trailing_hwdpi(last) + overscan`, both from `STRIP_FIDUCIAL`.
- Wire line count = chunks x `lines_per_chunk`, chunks rounded up, the
  visible count is the wire count / 2; the delivered window must carry
  the two colour-crop strips (`image.align_shift(dpi)` per side) in
  addition to the span, exactly as `overscan_geometry` does.
- FEEDL is anchored the dual way: `want_start + K`, K = `default_wire_lines
  x units_per_wire_line / 2` (the empirical per-profile constant of
  `dual_overscan_geometry`, dual passes 1–2 of 2026-09-10). Positioning is
  therefore exactly the move frame `first` gets today.
- `end_hwdpi` is checked against `FEEDL_CEILING`; `check_frame` on both
  ends; `first <= last`.
- The result also carries, for every aperture k in first..last, its
  predicted leading and trailing edge in VISIBLE line coordinates of the
  delivered sweep (`predicted_edges[k] = (lead_line, trail_line)`), so the
  window finder can relate measured edges to aperture numbers.

Returns `(SweepGeometry, wire_lines)`; `SweepGeometry` extends the
`OverscanGeometry` fields with `first`, `last`, `predicted_edges`.

## 4. The device side: `Scanner._sweep_dual`

The dual calibration block of `_scan_dual` (dark pair, white/gain,
gain-check pair, shading measure, upload, verify) is extracted unchanged
into `_calibrate_dual(self, t)` returning a small record (dark_a, dark_b,
dark_b_substituted, gain codes, offset codes, shading tables A/B and
2A/2B, verify_meas, dips, blocks). `_scan_dual` calls it and continues as
before. **The frame path's op stream must stay byte-identical**: the
existing wire-equality tests (`tests/test_dpi.py`, `tests/test_ir.py`,
`tests/test_dual_overscan.py`) pass unchanged, and `_RecordingDualScanner`
sees the same phase order with the same injections.

`_sweep_dual(self, t, *, first, last, overscan_mm, sink)`:

1. Resets the per-scan diagnostics like `_scan_dual`; `_cal_cache = None`.
2. `_calibrate_dual(t)`.
3. `geom, wire_lines = holder.dual_sweep_geometry(...)`;
   `feedl = holder.check_feedl(geom.feedl)`; logs the window.
4. POSITION exactly as `_scan_dual` (strict polls, `POSITION_STATUS_MASK`,
   `position_timeout_scale(t, feedl)`, FEEDL injected).
5. **Park budget from the furthest position, not from FEEDL**:
   `self._park_scale = position_timeout_scale(t, round(geom.end_hwdpi))`.
   The semantic PARK's two waits are stretched by `_park_scale`; after a
   sweep the carriage returns from aperture `last`'s far edge, which is
   where frame `last` alone returns from today (wait B 10.06 s on frame 4,
   budget never approached).
6. SCAN: `self._run_phase(t.scan_phase(n_chunks), lines_top/hi/lo=...,
   chunk_sink=sink)`. `_run_phase` and `_exec_ops` get a new keyword-only
   `chunk_sink: Callable[[bytes], None] | None = None`; when set,
   `flush()` hands each completed read buffer to the sink instead of
   appending it to the returned list. Nothing else in `_exec_ops`
   changes. The sink is the only place the image data goes; the sweep
   never holds more than one chunk (486 KB) of image data in memory.
   The line count goes through the same 24-bit check as today.
7. `self._park(t, ir=True)`.
8. `last_cal_data` as `_scan_dual`; `last_diag` as `_scan_dual` with
   `frame=None`, `sweep={"first", "last", "feedl", "end_hwdpi",
   "wire_lines", "chunk_count", "raw_bytes" (counted in the sink),
   "overscan_mm"}`, `phase_seconds`, `total_seconds`, etc.
9. Returns `(W, {"width": W, "alternating": True, "dpi": dpi,
   "geometry": geom, "wire_lines": wire_lines})` — no raw bytes.

Public entry: `Scanner.sweep(first, last, *, dpi, overscan_mm=None,
sink)`, with the same preconditions as `scan()` (`initialize()` first,
loaded this session, `park_mode` valid), dual profiles only: `dpi == 3600`
without IR is not a dual profile — the public method always runs the
dual table of `dpi` (`ir=True` semantics), and the CLI refuses plain
(section 6). Runs inside `self._operation("sweep")`.

## 5. Post-processing: `of135i/sweep.py` (pure, offline-testable)

The sweep file is the raw wire buffer: pixel-interleaved RGB16LE,
alternating lines, even = IR, odd = visible, `wire_lines x W x 3` uint16.

- `open_raw(path, width) -> np.memmap` (read-only, shape `(lines, W, 3)`,
  dtype `<u2`); a trailing partial line is dropped as `image.assemble`
  does.
- `ir_profile(mm, *, band=0.5, block_lines=2048) -> np.ndarray`: the
  along-strip profile of the IR lines (even wire lines), mean over the
  central `band` of the width and over the three channels, computed block
  by block so no whole-sweep array is ever materialised. Index = visible
  line index (wire line // 2). The 12-line colour stagger of the IR
  channels is irrelevant at this stage (it only matters at the
  per-window coverage step, which runs the exact per-frame pipeline).
- `find_windows(profile, geom, *, dpi) -> SweepResolution`:
  1. Bin the profile to ~600 dpi (`factor = max(1, round(dpi/600))`) and
     run `aperture.apertures(binned, lines_per_mm / factor)` exactly as
     `aperture_crop.measure_coverage` does; scale the runs back.
  2. A run is *plausible* when its length is within
     `aperture_crop.SIDE_BAND_LENGTH_TOL_MM` (1.0 mm) of the holder's
     aperture length (`holder.STRIP_APERTURE_MM`, use the mean; the six
     differ by 0.3 mm). A shorter run (a film end opaque in IR, the
     telemetry's 2.54 mm case) is kept as a *candidate*, never used to
     set the strip offset.
  3. Each plausible run is assigned to the aperture k whose predicted
     leading edge (`geom.predicted_edges`) is nearest its start; the
     residual is the run's offset. All plausible runs must agree on one
     offset (spread <= `SWEEP_OFFSET_SPREAD_MM` = 1.0 mm; the load-to-load
     mapping varies ±0.24 mm and the pitch residual is 0.022 mm) and must
     map to distinct, increasing apertures; the strip offset is their
     median. If they do not agree, or no run is plausible, the sweep is
     **unresolved**: no window is cut, the reason names what was seen.
     The offset itself must stay within `SWEEP_MAX_OFFSET_MM` (12 mm):
     beyond about half a pitch (19 mm) the nearest-aperture assignment
     would be off by a whole aperture and no run could reveal it, so a
     larger offset also leaves the sweep unresolved (the known DPI-change
     shift is ~7.5 mm and passes).
  4. With the offset known, every aperture first..last gets a window:
     `located_by = "edges"` when a plausible run was assigned to it
     (window = that run's edges ± `overscan_mm`), else `"pitch"` (window =
     predicted edges + offset ± `overscan_mm`; this covers the film-end
     case and a run the IR did not show). The window is widened by
     `image.align_shift(dpi)` visible lines on each side so the colour
     alignment's crop lands outside the overscan, converted to WIRE lines
     (x2), start rounded DOWN to an even line, end rounded UP to an even
     line, clamped to the sweep.
  5. Returns `SweepResolution(resolved: bool, reason: str, offset_mm:
     float | None, runs: list[(start_line, end_line, length_mm)],
     windows: dict[int, SweepWindow])`, `SweepWindow(aperture, wire_start,
     wire_end, located_by)`.
- `window_bytes(mm, w) -> memoryview`: `memoryview(mm[w.wire_start:
  w.wire_end]).cast("B")` — a flat bytes-like view that `image.assemble`
  (and therefore `image.split_ir`) accepts unchanged, paging in lazily.

**The invariant that makes the sweep safe:** every window goes through
the EXISTING per-frame writer `_finish_digitize_frame(args, raw, width,
out, dual=True, progress=entry)` with `raw = window_bytes(...)`. That
function re-measures coverage on the window (IR first, visible second),
checks film presence, crops to the edges it finds, and writes the
overscan files, the main negative, the IR file and the preview exactly as
today. A window whose coverage does not verify yields no main negative,
as today. The window finder only decides WHERE to cut the sweep and WHICH
aperture number the cut carries; it never produces a product by itself.
Consequence (tested): the products of a window are byte-identical to the
per-frame writer run on the same bytes.

Aperture numbering: the aperture index comes from the measured edges'
order along the sweep and the holder's known pitch relative to the
sweep's own commanded start. It is never a bare position guess: a run
that does not fit the grid, or two runs claiming one aperture, leaves
the sweep unresolved rather than misnumbered. (135 frames ARE the holder's
apertures; photograph identity by content is the 110 problem and stays in
`film110`.)

## 6. CLI: `digitize --sweep`

- `--sweep` on `digitize`: 135 only (`--film 110` → error), dual profiles
  only (`--no-ir` with `--dpi 3600` → error "the sweep is implemented for
  the dual-light profiles; plain 3600 keeps the per-frame cycle"),
  `--strips 2` → error, `--frames` must be a contiguous range `a-b`
  (default `1-4`). All validated before any hardware step (exit 2).
- Flow: load (as today) → `scanner.initialize(ir=True, dpi)` once →
  `scanner.sweep(first, last, dpi=..., sink=file writer)` into
  `<rdir>/sweep.raw` → `sweep.json` (geometry + device diag) → **eject**
  (the carriage is parked and the raw file whole; the post-processing
  below takes minutes and the scanner drops USB after ~5 min of standby,
  so the eject comes first — the eject from the post-PARK state is the
  verified one) → the writing session closes → `sweep.ir_profile` →
  `find_windows` → per aperture k in order: `out = frame_path(k)`, `entry`
  as today, `_finish_digitize_frame(...)`, per-frame manifest entry filled
  EXACTLY as today
  (`coverage_verified`, `coverage_reason`, `coverage_source`,
  `film_present`, `saturated_share`, `coverage_warning`, `main`, `ir`,
  `preview`, `dust_cleaned`, `calibration`, `gain_codes`, `offset_codes`,
  `dark_b_substituted`) plus `located_by` → diag sidecar per frame →
  manifest record (the eject already happened, see above).
- Diag sidecar per frame: `_write_diag_sidecar` gets an optional
  `extra: dict | None` merged into the sidecar; the sweep passes
  `{"sweep_window": {"aperture", "wire_start", "wire_end", "located_by"},
  "sweep": last_diag["sweep"]}`. The calibration data file (`.cal.npz`)
  is written once as `f<first>.cal.npz` and then HARD-LINKED (`os.link`,
  falling back to a copy if linking fails) as `fN.cal.npz` for every other
  frame of the sweep, so each frame's sidecar names its own `fN.cal.npz`
  exactly as today. Rationale (workflow feedback 2026-10-30): the
  workflow's readers open `<dir>/fN.cal.npz` by name, follow no pointer,
  silently skip a correction when the file is missing, and treat
  `fN.cal.npz` as part of frame N (deleting frame 1 must not strand the
  others). Hard links cost no disk. The sidecar additionally records
  `"cal_data_shared": true`. Every frame's sidecar carries the full device
  diag (including `white_dips`), since the one calibration applies to all.
- Unresolved sweep: no products, every frame entry `coverage_verified:
  false`, `coverage_reason: "sweep unresolved: <reason>"`, exit 4, the
  roll recorded failed, `sweep.raw` and `sweep.json` KEPT. Nothing is
  lost: the strip's whole sweep is on disk and `--from-sweep` can rebuild
  it once the cause is clear.
- `sweep.json` (always written, next to `sweep.raw`): the geometry
  (first, last, dpi, feedl, end_hwdpi, wire_lines, chunk_count,
  overscan_mm, width), the resolution (offset_mm, runs, windows,
  located_by, reason) and timing (`phase_seconds`, `total_seconds`,
  started/finished UTC).
- `sweep.raw` is removed after a fully successful roll (every window
  verified or recorded empty) unless `--keep-sweep`; it is KEPT on any
  failure, on an unresolved sweep and on any frame whose coverage did not
  verify. Whenever `sweep.raw` is left on disk the CLI says so in
  plain words on stderr, with its size and path ("sweep.raw kept (1.3 GB):
  …"), so the operator can decide what to do with it. Rationale: 1.3–2 GB per strip is not archival material once the
  per-frame overscan files (the raw-data contract) are written, but no
  failed strip ever loses data.
- `digitize --sweep --from-sweep --roll N`: offline rebuild from
  `sweep.raw` + `sweep.json` (no hardware, no load/eject): rerun the window
  finder and the per-frame writer, append a manifest record with
  `rebuilt_from_sweep: true`. Refuses if either file is missing. Clears
  the roll's frame products first (as `--force` does), never the sweep
  files.
- `digitize.clear_roll_outputs` also removes `sweep.raw` and `sweep.json`
  (exact names) on `--force`; `--from-sweep` uses a variant that keeps
  them.
- Manifest record additions (top level): `"sweep": {"first", "last",
  "offset_mm", "located_by": {k: ...}, "seconds": {"position", "scan",
  "park", "calibration" (sum of the cal_* phases), "total"}, "raw_kept":
  bool}`. `started_utc`/`finished_utc`/`per_frame` keep their meaning, so
  the workflow's per-strip time (finished − started) compares directly
  with the per-frame cycle.
- Stop-at-empty does not apply: every aperture in the range is in the
  sweep already; an empty aperture gets no product (as today) and no
  frames are "skipped".

## 7. Tests (offline, `tests/test_sweep.py`, added to
`tools/release_check.py` SUITES as core)

1. Geometry: for every dual profile and (first,last) in {(1,4),(1,6),
   (2,3)}: `end_hwdpi < FEEDL_CEILING`, wire count even and 24-bit, wire =
   chunks x lines_per_chunk, FEEDL for (first, …) equals
   `dual_overscan_geometry(first)`'s FEEDL (same anchoring), predicted
   edges are monotonic and one pitch apart, (1,7) and (0,4) refused.
2. Device wiring with a `_RecordingDualScanner`-style harness: the sweep's
   phase order is cal_* (same as the frame path) → position → scan → park;
   the POSITION injection equals the frame-`first` FEEDL; the scan
   injection equals the geometry's wire count; the sink receives exactly
   `chunks` buffers of `IMAGE_CHUNK_LEN` bytes and the returned list is
   empty; `_park_scale` equals `position_timeout_scale(t, round(end_hwdpi))`
   (greater than frame `first`'s); `last_diag["sweep"]` filled.
3. Frame-path regression: `tests/test_dual_overscan.py`, `tests/test_dpi.py`,
   `tests/test_ir.py` pass unchanged after the `_calibrate_dual` extraction.
4. Window finder on synthetic profiles (3600 dpi, 1–4): (a) four
   plausible runs at the predicted positions shifted by +0.3 mm → resolved,
   offset 0.3, all `edges`; (b) run 1 shortened by 8 mm at its start (film
   end) → resolved on runs 2–4, aperture 1 `pitch`; (c) run 3 missing
   (dark) → aperture 3 `pitch`; (d) all runs shifted by +7 mm (the DPI
   change case) → resolved, offset 7; (e) two runs 0.5 pitch apart →
   unresolved, reason names the spread; (f) no run ≥ MIN_APERTURE_MM →
   unresolved; (g) window parity: every `wire_start` is even, every
   `wire_end` even, within the sweep.
5. Byte-identity: a synthetic 1–4 sweep raw (small W to keep the test
   fast, e.g. W=64, but real line counts at 600 dpi) written to a temp
   file; `digitize --sweep` offline through a fake scanner whose `sweep()`
   streams the file's chunks into the sink → four products; then
   `_finish_digitize_frame` run directly on the bytes of each window →
   identical TIFF bytes (main, ir, overscan). Manifest record has
   `sweep`, per-frame entries have the same keys as a per-frame run plus
   `located_by`.
6. Failure paths: unresolved sweep → exit 4, no `fN.tiff`, `sweep.raw`
   kept, record failed with the reason; a window that does not verify →
   `sweep.raw` kept, exit 4; `--from-sweep` rebuild reproduces the
   products byte for byte and appends a record with `rebuilt_from_sweep`.
7. CLI validation: `--sweep` with `--film 110`, with `--no-ir --dpi 3600`,
   with `--strips 2`, with `--frames 1,3` → exit 2 before any hardware.
8. `test_release_check` keeps passing (SUITES changed → whatever pins it
   is updated honestly).

## 8. Hardware plan (Test 111) — needs the owner's go, in a scanning pause

Preconditions: VueScan/OpticFilm.exe dead, `of135i status` healthy (reg
0x01 = 0x22), the A/B strip identified by the workflow (a four-frame dual
3600 IR strip whose per-frame products are verified and whose path is
known to the owner; it must NOT be named in this repo).

1. Normal load (`of135i digitize --sweep` runs the load flow itself); the
   owner listens as always — a scrape means cut the power.
2. The sweep: one POSITION (the frame-1 move he hears every strip), one
   long pass (~160 s of continuous motion instead of four 40 s passes; the
   ONLY new sound is the pass continuing over the crossbars), one PARK
   from aperture 4's far edge (the frame-4 return he already hears), eject.
3. Judge: every window `coverage_verified`; each product compared with
   the per-frame product of the same aperture — same aperture length
   within a line or two, visual identity (eye check per the milestone
   rule, `docs/production-telemetry-2026-10.md`'s acceptance), and the
   crossbar gaps present in `sweep.raw`.
4. Time: `finished_utc − started_utc` of the sweep record against the
   strip's per-frame record (the workflow measures exactly this); the
   `sweep.seconds` breakdown against §2.4's medians.
5. Only then: dual 3600 sweep marked hardware-verified in README/CHANGELOG;
   plain 3600 and the other dual profiles stay unverified until their own
   runs.

A failure of any kind: power-cycle, read the log and `sweep.json`, no
retry the same evening without a cause.

## 9. Out of scope here

Plain 3600 sweep; frame-to-frame dead reckoning (step 2 of the saving);
the inter-aperture film base as a manifest field (candidate, owner's
veto on new fields; the data is in `sweep.raw` when kept); the SANE
backend (per-frame by design, `docs/sane-batch-scanning-design.md`).
