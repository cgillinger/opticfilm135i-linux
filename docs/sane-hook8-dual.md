# SANE port, hook 8 — the dual-light profiles: 600/1200/2400/7200 dpi and infrared

*Analysis and offline implementation, 2026-09-08 (evening). Written after
Test 54 closed the colour-line question for the plain 3600 dpi frame.
Hardware run pending; the decisions in section 8 were taken under the
hook 2 principles (docs/sane-port.md) and are listed for Christian's
review, not separately approved.*

## Status (2026-09-08, offline)

Implemented and built clean; 37/37 op tests (183 + 5 = 188 offline
tests total):

- every op program of the five dual profiles (`ir3600`, `dpi600`,
  `dpi1200`, `dpi2400`, `dpi7200`) is wire-equal to the driver's own
  transfers for that profile over the fake device — the calibration
  chain with its injections, POSITION with the profile's own FEEDL, the
  scan setup with the three-byte line count, the image chunks, and the
  semantic PARK (`test_dual_programs_match_python_replayer`,
  `test_dual_park_programs_match_park_semantic`, `test_dual_image_chunks`);
- the dual second shading table and the line split are byte-identical
  to `calibrate.shading_table2_dual` / numpy's `arr[p::2]`
  (`test_shading_table2_dual_reference_vectors`);
- the geometry of all six profiles is pinned by `frame_geometry()`
  against the Python tables (`test_frame_geometry_all_profiles`).

Not done, needs the scanner: one run per resolution and one infrared
run (section 7), then Christian's eye check of each image.

## 1. What the captures are

Every resolution except 3600 dpi exists only as the vendor's IR-enabled
scan (docs/protocol-notes.md pass 18): one motor pass in which the
sensor is read twice per line position, once under the infrared lamp and
once under the visible lamp. The stream therefore alternates — even
lines IR (R ≈ G ≈ B, dust and scratches dark), odd lines visible — at the
full sensor width, not the 3762-pixel window of the plain 3600 dpi scan.
3600 dpi has both: the plain scan (hooks 2–7, Tests 48–54) and the dual
one (`ir3600`).

| profile | dpi | width px | lines/chunk | chunk B | line register | chunks read | raw lines read | image lines per pass | colour shift | delivered lines |
|---|---|---|---|---|---|---|---|---|---|---|
| plain3600 | 3600 | 3762 | 23 | 519156 | 5137 | 224 | 5137 | 5137 | 24 | 5113 |
| ir3600 | 3600 | 5184 | 16 | 497664 | 10622 | 659 | 10544 | 5272 | 24 | 5248 |
| dpi600 | 600 | 876 | 98 | 515088 | 1764 | 18 | 1764 | 882 | 4 | 878 |
| dpi1200 | 1200 | 1752 | 48 | 504576 | 3552 | 74 | 3552 | 1776 | 8 | 1768 |
| dpi2400 | 2400 | 5256 | 16 | 504576 | 7088 | 443 | 7088 | 3544 | 16 | 3528 |
| dpi7200 | 7200 | 10512 | 8 | 504576 | 21248 | 2656 | 21248 | 10624 | 48 | 10576 |

`ir3600` is the odd one: the register holds 10622 lines but the vendor
read 659 full chunks (10544 lines) and cancelled the 660th descriptor
(pass 12). The backend does what the vendor did (decision 1). The
driver, for comparison, keeps 223 of the plain scan's 224 chunks; the
backend reads all 224 there (hook 5, "the drain"), so the plain and
dual conventions differ by design and both are the captured behaviour.

## 2. How the driver runs them (`Scanner._scan_dual`)

The same phase sequence as the plain scan, with these differences
(of135i/device.py, read for this analysis):

- **Dark pair, offsets:** the same two-point bracket; the doubled
  buffers (IR and visible dark lines) are flattened alike. Unchanged.
- **White line, gain:** two lines, IR first. There is one AFE gain
  register set, computed from the **visible line only** — the IR line's
  flat, bright values would skew the 99.9th-percentile peak.
- **Shading:** a 256-line alternating measurement at the full width.
  Two tables are uploaded: address A (0x10014000) built from the
  **visible (odd)** lines and applied by the scanner to the odd scan
  lines, address B (0x10034000) from the **IR (even)** lines for the even
  ones — the empirically corrected assignment (a swapped mapping gave
  flat images, 2026-09-03). The second upload uses the dual formula:
  gain = target · 0x4000 / mean(white), with the vendor's per-address
  targets 61440 (A) and 90112 (B); the plain profile's (white − f0)
  denominator is not used.
- **POSITION:** the same program; FEEDL frame 1 is 6746 in every dual
  capture against the plain capture's 6743, pitch 10760 in all.
- **Scan setup:** a three-byte line count (`lines_top/hi/lo`).
- **PARK:** `park_semantic()` with the profile's own two 0x8b payloads.
- **Host side:** `image.split_ir()` takes the odd lines as the visible
  image and the even ones as a single-channel IR image (mean of R, G,
  B); `align_channels` shifts and crops the visible image by
  2·round(24·dpi/7200) rows and crops the IR image by the same amount
  so both stay on one row grid; `--ir` writes the IR as a separate file
  and dust removal is a CLI step.

## 3. The backend's mapping

**Session geometry** (`calculate_scan_session`, generalised from the
plain pin): every captured profile is pinned. `frame_geometry(profile)`
(gl126_ops, pure arithmetic) gives the wire line count, the raw lines to
read, the lines of one pass, the colour shift at this dpi
(24 · dpi / 3600, the model's `ld_shift` scaled as the core scales it)
and the delivered count. The session's `params.lines` is the delivered
count, its `optical_line_count` (what the USB source node reads) the raw
lines of both passes, and `output_total_bytes_raw` (what the scan-pass
state machine expects before PARK) follows it. The model's ld_shift
24/12/0 gives 4/2/0 at 600 dpi and so on — the driver's
`round(24·dpi/7200)` per channel, checked by the geometry test.

**Pipeline:** the core's `build_image_pipeline` gets a GL126 branch right
after the USB source node: `push_dual_light_nodes()` inserts a
keep-one-line-in-two node (`ImagePipelineNodeGl126KeepParity`, height =
source / 2, row k = source row 2k + parity) — odd lines for the visible
image, even for the infrared — and, for the infrared, an `Extract` crop
of shift/2 rows at each end. The core's own `ComponentShiftLines` node
then re-aligns the visible image's channels exactly as for the plain
frame. For the infrared session `IGNORE_COLOR_OFFSET` is set: one line
per position, R = G = B, and a shift would only smear every dust speck
across the shift distance. `sane_get_parameters` reports the pipeline's
output, so the visible and the infrared image of one resolution have the
same width and the same height (table above, "delivered lines").

**Infrared as a scan method:** `ScanMethod::TRANSPARENCY_INFRARED`
(`--source "Transparency Adapter Infrared"`) is added to the model and
to every resolution's sensor entry. It is a separate `sane_start` that
runs the same dual pass and delivers the even lines; `--mode Gray`
gives the one-channel image (the core's host-side gray, a weighted mix
of the three near-equal channels), `--mode Color` the three channels as
read. This is how gl843 delivers the OpticFilm 7200i's infrared and how
SANE frontends expect it; a frontend that wants dust removal scans
twice — the price of SANE's one-frame model (decision 3).

**Calibration:** `offset_calibration` is unchanged (the dark buffers are
flattened alike). `coarse_gain_calibration` cuts the white buffer to its
second line for a dual profile before `gain_with_warmup`, so the warmup
retry and the gain codes see the visible line only.
`run_shading_calibration_dual` is the driver's step list: split the
measurement with `alternate_lines()`, table 1A/1B from the visible/IR
dark lines, upload, verify measurement, table 2A/2B with
`shading_table2_dual()` and the two targets, upload. The generated
programs already carried the four bulk injection names.

**Scan pass:** `begin_scan` no longer refuses dual profiles; FEEDL comes
from the profile (`feedl_for_frame(frame, profile)`), the line register
is written with the captured value (`lines_top/hi/lo`; the plain program
ignores `top`), and the pass is armed with the raw bytes of what is
read. `read_image_chunk` is generic in the chunk length. PARK is
generic.

## 4. Wire

Unchanged for the plain profile (Test 54). For a dual profile the wire
is the driver's for the same profile, program for program — that is
what the wire-equality tests assert — with one convention: the backend
reads `ir3600`'s 659 chunks like the vendor, and the other four profiles'
whole line count (equal to their chunks).

## 5. Verification offline

- `tests/test_sane_ops.py`: the five tests listed under Status.
- The build: clean, no warnings from our files.
- Not verifiable offline: the pipeline's behaviour end to end (the
  core's nodes on a real stream), the frontend's view of the option
  (`scanimage -A` needs an open device), and of course the images.

## 6. What the hardware run must show

Per resolution (600, 1200, 2400, 7200) and once for the infrared at
3600 dpi, one `scanimage` of frame 1 from a fresh load, low debug level
(the level-255 log slows the pass, docs/test-log.md 2026-09-08):

- the calibration chain completes with the profile's own waits
  (W1 first poll as before), gain and offset codes within the driver's
  bands for that dpi, two shading tables of the profile's length;
- POSITION W3 in budget; the scan pass complete (all chunks full, the
  last one as long as the geometry says); PARK Wait A/B; 0x22 / 0xf8
  after; `eject` from post-PARK;
- the image: width and height per the table, no colour fringing, the
  visible image equal to the driver's `scan --dpi N --ir` visible output
  within its run-to-run band, the infrared image equal to the driver's
  `-ir.tiff` (as gray) and on the same row grid;
- Christian's eye check of every image.

Order proposal: 2400 first (the same sensor width as `ir3600`, a short
pass), then 600 and 1200 (binned widths), then the infrared at 3600,
7200 last (1.3 GB raw, the longest pass). Exit after each: `eject` from
post-PARK, the verified route.

## 7. Risks

- **Reading past the vendor's chunks.** Avoided by decision 1; the
  ir3600 pass reads exactly the vendor's 659 chunks. If the scanner
  nevertheless streams the remaining 78 lines into a later read, the
  pass's byte count still ends where the state machine expects.
- **The core's other nodes.** With the parity node inserted before the
  format conversion and the colour shift, the rest of the pipeline sees
  a plain single-pass image; the calibration node and gamma are off for
  this model as before.
- **7200 dpi memory.** The pipeline works chunk by chunk; the frontend's
  file is the 10512 × 10576 × 6 B = 667 MB visible image. The driver
  handles the same.
- **Sensor entries per method.** Added for both methods at every
  resolution; the model's motor placeholder accepts any method
  (`VALUE_FILTER_ANY`). Checked only by the build.

## 8. Decisions taken (hook 2 principles; for review)

1. **Read what the vendor read.** ir3600: 659 chunks of a 10622-line
   register; every other dual profile: its whole count. The plain frame
   keeps hook 5's 224 chunks. Wire fidelity over symmetry.
2. **One line in two is dropped on the host, in the core's pipeline**,
   by a GL126 node right after the source; the wire carries both
   passes as captured. The alternative — a source-side split in the
   bulk read — would hide the raw stream from the pipeline's debug
   dumps and the state machine's byte count.
3. **Infrared is a separate scan method** (`TRANSPARENCY_INFRARED`), the
   same dual pass delivering the even lines, cropped to the visible
   image's row grid, with the colour shift disabled for it. Dust removal
   stays in the frontend (decision 5 of docs/sane-port.md). Two passes
   for colour + infrared, as gl843.
4. **At 3600 dpi the colour scan stays the plain profile** (3762 px, the
   vendor's own visible pass, Tests 52–54) and the infrared comes from
   the dual profile at 5184 px — different widths. The window's offset
   within the sensor is not measured; matching the two at 3600 dpi is a
   later step (crop the dual image to the plain window once the offset
   is known, or scan the colour from the dual pass). Documented as a
   limitation until then.
5. **Gain from the visible line only, shading tables with the
   empirically corrected assignment** — the driver's behaviour, not pass
   18's prose.
6. **The profile's own FEEDL** (6746 for the dual captures). The
   difference of three steps from the plain capture is carried as
   captured, not normalised.
