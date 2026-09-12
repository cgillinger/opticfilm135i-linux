# B1 — remaining-profile hardware verification plan (offline prep)

Closes the four SANE profiles still without hardware evidence for B1's promised
function: **dpi600, dpi1200, dpi7200, ir3600**. plain3600 (frames 1–6) and
dpi2400 (frame 1) are already hardware-verified in SANE (Tests 62/63); the
calibration-cache fix is hardware-confirmed (Test 64) and the proportion fix is
closed (Test 63). This document is the offline preparation and the concrete
hardware plan; **Step 2 runs only after Christian's explicit go**. A reviewer's
technical GO is not itself permission to run hardware.

Scope excludes: install/packaging, frontend acceptance, lateral overscan,
Layer 2, upstream submission.

## Blocker check (raw delivery)

The image investigation (2026-09-12) found the mirror and yellow-green cast to
be **app-layer only** (preview transforms; the raw negative is a healthy C-41
orange mask, unclipped). No unresolved fault makes raw delivery unreliable for
any profile → **no blocker**; the profile runs may be planned.

## Status 2026-09-12 (hardware, Tests 65–68)

- dpi600 (Test 65), dpi1200 (Test 66), dpi7200 (Test 67): transport PASS on
  every ledger figure, coverage VERIFIED, owner's eye verdict ACCEPTED.
- ir3600 (Test 68): FAILED one chunk short — a backend bug (the core's
  pipeline never requests the far-end IR crop's chunk; docs/sane-hook5-frame.md
  §9.1), not hardware. Fixed offline the same day (end_scan drains the armed
  tail); the build below is therefore superseded — **the IR re-run must use the
  new build** (`libsane-genesys.so.1.4.0` sha256 `4dafc253...` after the fix,
  verify before the run) and needs a new explicit go. Everything else in this
  plan is unchanged.

## Fixed build for all runs

- **Code revision:** repo HEAD `ab0cb19` (no `sane/` source change since the
  Test 64 fix at `3086eda`; only docs/README followed).
- **Backend build:** `sane-backends` branch `gl126-opticfilm135i`,
  `backend/.libs/libsane-genesys.so.1.4.0`,
  sha256 `36d26634f37173770ba4e88a7477028d2c4c518b1958f2c1ea2b6bfa5113af42`
  (reproducible: a clean rebuild reproduced this hash). Rebuild:
  `make -j8 -C backend libsane-genesys.la` in the clone.
- **Offline gate (this build):** `release_check` 255 PASS / 0 FAIL / 0 SKIP,
  incl. the SANE-linked suites (`test_sane_ops` 38, `test_sane_geometry` 5,
  `test_sane_open_params` 6, `test_sane_calibration_cache` 6). The session probe
  reproduces every profile's delivered geometry below.
- **Backend selection & check:** run uninstalled with
  `LD_LIBRARY_PATH=<clone>/backend/.libs` and
  `SANE_CONFIG_DIR=~/Dokument/plustek-135i-analys/sane-config`. Before scanning,
  confirm the loaded library is this build (`ldd` on the probe, or the `.so`
  sha256), and take a fresh device string from `scanimage -L` (it re-enumerates
  on power-cycle; the examples below show `genesys:libusb:BBB:DDD`).

## Shared preconditions and rules

- **One fresh load per profile** (the documented DPI/profile-change rule:
  changing DPI between sessions shifts the frame; a reload resets it). Do **not**
  chain multiple resolutions on one load — that start state/positioning is not
  verified. So: power-cycle → `status && load` → scan that one profile → eject,
  then repeat for the next profile.
- **No `--force-calibration`** (ordinary calibration is verified, Test 64). Each
  run must show the calibration hooks ran (offset/gain/shading in the debug log).
- **Low USB debug** (e.g. `SANE_DEBUG_GENESYS=4`); **never level 255** (it
  hex-dumps the image and dominates timing).
- **Film:** the same colour negative and saved references where possible.
  **Never silver-based B&W** for IR/dust (opaque to IR). For IR use a colour
  (chromogenic-type) negative so the IR pass sees dust/scratches, not silver.
- **Interactive steps** (`status && load`, the load prompt) run in a real
  terminal (Christian). Christian listens; scraping/abnormal motor sound → cut
  power immediately.
- **Stop/recovery:** wrong geometry, short transfer, POSITION over budget,
  unexpected eject or PARK error → STOP, no blind retry, no new motor sequence
  from an unknown state; follow `docs/sane-lager1-hardware-plan.md` "Recovery".
  Normal exit is `of135i eject` only from confirmed post-PARK. Changed code
  voids a prior go for a new run.

## Per-profile ledger (frame 1, derived from the generated tables + probe)

All four are dual-light captures on the wire; the host keeps one line in two.
`raw bytes = chunks × chunk_len` (unchanged by any host scaling). Delivered =
what the frontend receives.

| Profile | Source | Res | Delivered px | Delivered lines | chan | max_shift | output_lines | chunks | chunk_len | Raw transfer (B) | Delivered PNM (B) | FEEDL | line_register | end_hwdpi |
|---------|--------|-----|--------------|-----------------|------|-----------|--------------|--------|-----------|------------------|-------------------|-------|---------------|-----------|
| dpi600  | Transparency Adapter | 600  | 876   | 927   | 3 | 4  | 931   | 19   | 515088 | 9,786,672     | 4,872,312   | 6519 | 1862  | 12351 |
| dpi1200 | Transparency Adapter | 1200 | 1752  | 1792  | 3 | 8  | 1800  | 75   | 504576 | 37,843,200    | 18,837,504  | 6555 | 3600  | 11979 |
| dpi7200 | Transparency Adapter | 7200 | 10512 | 10664 | 3 | 48 | 10712 | 2678 | 504576 | 1,351,254,528 | 672,599,808 | 6539 | 21424 | 21424(*) |
| ir3600  | Transparency Adapter Infrared | 3600 | 5184 | 5336 | 3 | 0 | 5336 | 670 | 497664 | 333,434,880 | 165,970,944 | 6538 | 10720 | 11899 |

Invariants checked offline (all hold): dual `delivered_lines + 2·align_shift(dpi)
== read_lines/2`; `output_lines == delivered_lines + max_shift`;
`raw_line_bytes == delivered_px × 3 × 2`; every `end_hwdpi ≤ FEEDL_CEILING`
(71490). (*) dpi7200 `end_hwdpi` prints as its line-register value in the
ledger; it is well under the ceiling.

Delivered width == raw sensor width for all four (only dpi2400 is anisotropic).

## Commands (no install; fresh device string each power-cycle)

```
export LD_LIBRARY_PATH=~/Dokument/Github/sane-backends/backend/.libs
export SANE_CONFIG_DIR=~/Dokument/plustek-135i-analys/sane-config
export SANE_DEBUG_GENESYS=4
DEV=$(scanimage -L | grep -o 'genesys:libusb:[0-9]*:[0-9]*')

# dpi600 / dpi1200 / dpi7200 (visible), one fresh load each:
scanimage -d "$DEV" --source "Transparency Adapter" --mode Color \
  --resolution <600|1200|7200> --frame 1 --format pnm \
  -o ~/Dokument/plustek-135i-analys/profiles-<date>/<profile>-f1.pnm 2> <profile>.log

# ir3600 (its own fresh load):
scanimage -d "$DEV" --source "Transparency Adapter Infrared" --mode Color \
  --resolution 3600 --frame 1 --format pnm \
  -o ~/Dokument/plustek-135i-analys/profiles-<date>/ir3600-f1.pnm 2> ir3600.log
```

## Per-profile acceptance and the file Christian judges

**dpi600 / dpi1200 / dpi7200 (visible).**
- Open B1 requirement: one owner-approved SANE image at this claimed resolution.
- Existing evidence: implemented + offline wire-equal to the driver; delivered
  geometry reproduced by the probe (above). NOT hardware-run in SANE yet.
- Bounded run: one frame-1 scan on its own load.
- Checks: PNM header = the delivered px×lines above; raw transfer = the byte
  count above (log "scan pass complete, N raw bytes"); calibration hooks ran;
  POSITION within the FEEDL-scaled budget (frame-1 FEEDL ≈ Test 62/63 band,
  ~4.8 s budget); normal semantic PARK; **coverage VERIFIED at that dpi** (RGB
  coverage is valid for these visible profiles); channel alignment (no RGB
  fringing); no banding/streaks; isotropic → square proportions.
- File to judge: `<profile>-f1.pnm` rendered to a positive preview (documented
  transform `rot90(3)[:, ::-1]` + `to_positive`, the vendor convention) plus a
  neutral per-channel white-balance diagnostic, in
  `~/Bilder/opticfilm-granskning/`. Colour is out of scope (app's job); the eye
  rule is geometry/integrity.

**ir3600 (infrared).**
- Open B1 requirement: one owner-approved IR image (IR/visible separation and
  IR geometry) at 3600 dpi.
- Existing evidence: implemented (hook 8) + offline wire-equal; probe delivers
  5184×5336, max_shift 0 (IR is cropped, not colour-shifted). NOT hardware-run
  in SANE yet.
- Bounded run: one frame-1 IR scan on its own load.
- Checks: PNM 5184×5336; raw transfer 333,434,880 B; calibration hooks ran;
  POSITION within budget; normal PARK. **Do NOT run RGB coverage on the IR
  file** (it assumes visible light). Assess the IR image *content* (dust and
  scratches stand out; the pictorial image is faint/absent, as IR expects) and
  *geometry* (aperture present, whole frame). IR is delivered as 3-channel with
  R=G=B (the IR pass broadcast into RGB slots).
- File to judge: `ir3600-f1.pnm` rendered as a gray image (documented: take one
  channel, or the mean; rotate `rot90(3)` to upright) in
  `~/Bilder/opticfilm-granskning/`.
- **IR-to-visible registration:** NOT a B1 backend requirement. SANE delivers
  one image per scan (the IR parity from one dual capture); it does not emit a
  registered visible+IR pair, and host-side dust removal is the frontend's job
  (delivery checklist). Two *separate* SANE scans (IR + a visible plain3600)
  would be at possibly different per-load positions, so they are not a true
  registration reference. If registered IR+visible dust removal is wanted later,
  the compatible reference is a **combined dual-output capture mode** — a
  separate design item, not a hardware test here. No extra run is planned for IR.

## Post-analysis (offline, after the runs)

- Parse each PNM (P6, 16-bit big-endian) to a uint16 array; compare header dims
  and byte counts to the ledger; grep each log for FEEDL, line_register, chunks,
  "scan pass complete … raw bytes", the calibration hooks, and PARK.
- Visible: `of135i.aperture_crop.measure_coverage(arr, dpi=<dpi>)`.
- **dpi7200 memory:** the delivered PNM is ~641 MiB and the raw transfer ~1.26
  GiB (streamed in chunks, not held whole). Keep the raw file; do analysis in
  **uint16** on **limited regions or a downscaled view**; avoid float64
  whole-image copies (`to_positive` triples memory). Run heavy renders under
  `systemd-run --user --scope -p MemoryMax=3G` (oomd rule). 31 GiB RAM / 406 GB
  free is ample, but the discipline still applies.
- Every diagnostic render states its transform in a README beside it; no
  generative processing; originals preserved.

## What this plan is NOT

- Not a multi-resolution series on one load (each profile is its own load).
- Not a re-run of already-passed profiles (plain3600, dpi2400) without a fault.
- Not `--force-calibration`.
- Not silver B&W film for IR.
- Not install/frontend/upstream (separate work packages).
