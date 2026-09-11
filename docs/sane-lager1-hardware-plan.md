# SANE stage-1 (Lager 1): minimal hardware-verification plan

Written offline; revised after external review, command corrections
2026-09-11. **Do not run until the plan is explicitly approved.** This
closes ONE thing: that the SANE backend's separate C++ implementation, on
real hardware, positions to the A+C ledger's FEEDL and scans the correct
overscan window (full transfer, correct line count), then PARK/eject —
for the holder positions actually run. The offline wire-equivalence proves
the commands are identical to the CLI driver's; the hardware confirms the
C++ actually drives the unit that way (the first SANE motor run on the new
geometry).

Interim contract: overscan is delivered in FULL (no crop in the backend).
Coverage is checked host-side afterwards with `tools/sane_coverage.py`
(`measure_coverage` on the delivered full image), exactly as the driver's
overscan runs (Test 57).

## 0. Environment lock BEFORE the first motor write (mandatory, logged)
It is the separate C++ implementation that is verified — the test log must
prove the RIGHT binary ran:
1. `git rev-parse HEAD` == the approved stage-1 commit (stage 1 + the
   2026-09-10/11 correction rounds). `git status` clean.
2. `.venv/bin/python tools/gen_sane_tables.py --check` → "up to date".
3. Rebuild: `make -j8 -C <sane-backends>/backend libsane-genesys.la`
   → 0 warnings, links.
4. Confirm scanimage loads the **scratch build's** libsane-genesys via the
   intended `LD_LIBRARY_PATH` + `SANE_CONFIG_DIR` (the scratch SANE config
   dir): `scanimage -L` shows the genesys backend from the scratch path
   (not a system-installed one). Verify the path before any scan.
5. Take the fresh device string from `scanimage -L` at run time; do NOT
   reuse an old USB bus/device number (re-enumeration changes it).
6. The coverage command was already exercised offline: it parses 16-bit
   PNM correctly; the old fixed-window scans are correctly reported "no
   aperture found" (they scanned inside the aperture with no margin —
   expected). `sane_coverage.py` accepts ONLY P6/RGB PNM (`read_pnm`
   rejects anything not starting with `P6`) — so the scan MUST be run with
   `--mode Color`, otherwise Genesys defaults to Gray (P5) and a fully
   successful scan looks like a failure. Command per frame after a scan:
   `.venv/bin/python tools/sane_coverage.py <file.pnm> --dpi <dpi>`
   (exit 0 = verified, 1 = not, 2 = read error).

## Preconditions (hardware protocol)
- Scanner healthy (`reg 0x01 = 0x22`), any VM detached.
- Power-cycle → `of135i load` in its own terminal window.
- **Low debug level** (NEVER 255 — it hexdumps the image and falsifies
  timings).
- Listen: any scraping sound → cut power immediately.

## Mandatory scanimage flags in ALL runs
- `--force-calibration` — makes the test deterministic. Genesys otherwise
  tries to read and reuse its calibration cache; an old compatible entry
  can make SANE skip exactly the calibration hooks GL126's `begin_scan()`
  requires (it requires `CalStage::ShadingDone` from the SAME `sane_start`
  — the vendor calibrates every frame) and stall before the motor. All
  bring-up runs (hooks 2–7) used `--force-calibration` for the same reason.
  That a finished backend must not REQUIRE this flag for every ordinary
  scan is a separate open item — see `docs/sane-port.md`, "Risks and open
  questions" — which does NOT block this test.
- `--mode Color` — see §0.6 (the coverage tool requires P6/RGB).

## Runs

Two loads (plain and dual do not share resolution → a DPI change shifts
the position → new load). External-review recommendation adopted: Load A
runs ALL six frames so the SANE part of Milestone C (frames 1–6) is
actually closed, not just the endpoints.

### Load A — plain3600, frame 1→6 on the SAME load

No home between frames — the scan pass IS the transport. **No eject until
after frame 6.** The holder stays in the known post-PARK state between
frames. If SANE unexpectedly ejects mid-sequence: treat it as an ANOMALY
(STOP, see recovery), not "reload and continue".

Command per frame (low debug):
`scanimage -d genesys:libusb:<fresh-from-scanimage-L> --force-calibration --mode Color --resolution 3600 --frame N --format pnm -o <out-dir>/sane-l1-plain-fN.pnm`

Expected geometry (from the ledger) and timings:

| frame | FEEDL | line_register | chunks | delivered | end_hwdpi | expected observed POSITION | hard timeout |
|---|---|---|---|---|---|---|---|
| 1 | 6562 | 5367 | 233 | 5335 | 11897 | ~1.8 s | 4.8 s |
| 2 | 17315 | 5367 | 233 | 5335 | 22650 | ~4 s | 12.4 s |
| 3 | 28051 | 5344 | 232 | 5312 | 33363 | ~6 s | 20.1 s |
| 4 | 38806 | 5321 | 231 | 5289 | 44095 | ~8 s | 27.9 s |
| 5 | 49538 | 5321 | 231 | 5289 | 54827 | ~10 s | 35.6 s |
| 6 | 60276 | 5344 | 232 | 5312 | 65588 | ~12.4 s | 43.3 s |

**Expected observed time ≠ timeout budget.** The "expected observed"
column is the approximate completion time the CLI regression (Test 59) saw
(1.8→12.4 s over f1→f6). "Hard timeout" is `position_timeout_ms()` =
3 × 1.6141 s × FEEDL scale (scale reference still the legacy
`kFeedlFrame1` = 6743, conservative — the f1 scale clamps to 1×). **Mild
timing variation is to be LOGGED, not to trigger STOP, as long as class F
is reached within the actual hard timeout.** After each frame: expected
full transfer (chunks/chunks), PARK normal (no eject).

After frame 6: `of135i eject` from post-PARK.

Host-check Load A: run `sane_coverage.py` on all six PNM → aperture
captured with a leading + trailing margin (the same verdict the driver's
overscan gives). Visual check in two steps:
1. Quick identity check on ALL six: right image in the right order, whole
   subject — closes "right frame" visually (nearly free since 1–6 are run
   anyway).
2. Careful quality check (image-path change) on a representative
   production image, preferably frame 6: whole frame, no skew/banding,
   compare against the driver's overscan image.

### Load B — dpi2400, frame 1 (dual: fixed-K anchoring + parity)

Power-cycle → `of135i load` (new load, DPI change).

`scanimage -d genesys:libusb:<fresh-from-scanimage-L> --force-calibration --mode Color --resolution 2400 --frame 1 --format pnm -o <out-dir>/sane-l1-dual2400-f1.pnm`

Expected: FEEDL **6543** (re-anchor want_start+K, NOT plain-centre),
line_register **7152** (interleaved, even), chunks **447**, delivered
**3560**, hard timeout **4.8 s** (expected observed ~1.8 s). Full transfer
447/447. PARK (dual PARK is longer, ~65 s observed). Eject from post-PARK.
Host-check: `sane_coverage.py ... --dpi 2400`. Visual check on the dual
image too.

### Optional follow-up (only if A+B are green)
**ir3600 frame 1** (`--force-calibration --source "Transparency Adapter Infrared"`):
FEEDL 6538, line_register 10720, 670 chunks. Confirms IR interleave. Not
required for stage 1's core requirement. (`--force-calibration` for the
same determinism reason; `--mode` is governed by the IR source, not by
`--mode Color` — do not run `sane_coverage.py` on the IR image, it assumes
visible-light RGB.)

## Recovery after an anomalous/failed pass (less automatic)
On an anomaly (wrong FEEDL/chunk/line, POSITION > hard timeout, short
transfer, unexpected eject, PARK error, unusual sound):
1. Power-cycle.
2. **Read-only state check** (`of135i status`, `lsusb | grep 07b3`) — no
   motor command.
3. Run NO further motor sequences until a DOCUMENTED known start state for
   the recovery procedure is established.
4. `load --double-jog` → eject may be used ONLY if its already-documented
   precondition holds (power-cycled, latched magazine in the well), not as
   a general blind recovery.

## Acceptance
- If Load A (frames 1–6) + Load B (dual2400 f1) all give the expected
  FEEDL/line/chunk geometry, full transfer, normal PARK/eject, and
  `sane_coverage.py` verifies the aperture with a margin: **SANE stage 1
  frames 1–6 (plain) + the dual path (representative, dpi2400 f1) are
  hardware-verified.** Then — and only then — may the docs say so (not
  offline alone).
- If only three runs are done (f1/f6 + dual2400 f1): the docs may ONLY say
  "hardware-verified on representative SANE geometry paths: plain f1/f6 and
  dual2400 f1" — NOT that SANE frames 1–6 or all profiles are verified.
- Do not mark the other SANE dual profiles hardware-verified. The Python
  driver's verification does not transfer automatically to the C++ backend.
</content>
</invoke>
