# Test log — OpticFilm 135i Linux driver

Chronological record of hardware-verified tests, findings, and open issues.
Each entry records the date, what was tested, outcome, and any follow-up.

---

## Hardware test coverage at a glance

Summary of actual hardware testing performed, as documented in this log.
All testing has been performed on a single OpticFilm 135i unit by a
single developer on one Linux host.

**Cross-unit verified: NO — all hardware testing has been performed on a single OpticFilm 135i unit.**

### Status term definitions

| Term | Meaning |
|---|---|
| **IMPLEMENTED** | Code exists, but physical function is not verified. |
| **OFFLINE VERIFIED** | Code, tables, captures or calculations verified without a physical scanner. |
| **HARDWARE VERIFIED** | At least one successful physical run on the existing OpticFilm 135i unit. |
| **REPEATED** | Same function has been run multiple times with consistent results. |
| **STRESS-TESTED** | Function has been subjected to repeated runs, longer sequences, or multiple relevant states. |
| **ROBUSTNESS-TESTED** | Function tested under variations such as cold/warm state, different prior scanner states, recovery, or similar. |
| **CROSS-UNIT VERIFIED** | Verified on more than one physical scanner unit. |

### Test volume summary

Total hardware scan count has not been systematically tracked. Offline
suite as of 2026-09-05 evening: 105 tests (test_safety 47, test_hwblock
16, test_calibrate 14, test_park 12, test_offline 6, test_diag 4, test_dpi
3, test_ir 3).

| Test area | Volume | Status |
|---|---|---|
| Physical OpticFilm 135i units | 1 | Single-unit only |
| 3600 dpi scans | >=25 (10+10 reproducibility, cold-start, DPI reference, numerous development scans) | Stress-tested |
| 2400 dpi scans | At least 2 | Hardware-verified |
| 1200 dpi scans | At least 2 | Hardware-verified |
| 600 dpi scans | At least 1 (after 0x2b fix) | Hardware-verified |
| 7200 dpi scans | At least 1 | Hardware-verified |
| A3 reproducibility | 2 rounds × 10 scans (6 warm + 4 cold) | Repeated |
| Whole-strip batches (4 frames) | 4 documented (1 on 09-02, 3 on 09-05 incl. 1 raw), the last two frame-for-frame against the vendor app's output | Repeated |
| Cold-start initializations | >=13 documented (cold eject 09-02, Test 1, Test 9, Tests 15-23) | Repeated — a cold-started session must load before it scans (Test 22); the 'bare cold scan' path is retired |
| Post-cold-start scans | >=5 (Test 1 + Test 9 scans 6–9) | Partial — gain clips to 0x3F without warmup retry |
| Eject cycles | Multiple, incl. 4 from driver-loaded magazines on 09-05 | Repeated |
| Magazine load cycles (driver, vendor flow replayed whole) | 7/7 latched from power-on (Tests 17-23); 5 earlier attempts with out-of-context tables and 1 operator-error attempt failed safely | Repeated |
| IR scans (dual-light) | Standard mode in nearly all scans | Repeated |
| Dust removal (IR inpainting) | Used in most scans with `--ir` | Repeated |
| USB hosts / controllers | 1 (a second Fedora laptop is set up for B5 — same xHCI controller class, different chipset/kernel — not yet run) | Limited |
| Linux hosts | 1 | Limited |
| Film strips / film types | Not systematically counted | Limited |
| Cross-unit testing | 0 additional units | **NOT VERIFIED** |

---

## 2026-09-03 — Cold-start scan + reproducibility + offline audit

### Context

Scanner state at session start: freshly power-cycled (reg 0x01=0x00),
magazine loaded with colour negative strip, orange indicator lamp on.
No vendor software (Win11 VM offline). All tests run from Linux host
with the userspace pyusb driver (`of135i`).

### Test 1: Cold-start → full scan (first ever)

**Goal:** Verify that `cold_init()` (the vendor's own cold-start sequence
reverse-engineered from `01-init.pcap`) brings the scanner to a state
where the full calibration + scan pipeline works — not just eject
(verified 2026-09-02).

**Command:** `of135i scan --frame 1 --ir --positive --rotate 90 -o cold-test-f1.tiff`

**What happened:**
1. `initialize()` detected reg 0x01=0x00 → auto-called `cold_init()`
2. `cold_init()` ran: chip handshake, cold register table, AFE bring-up,
   3 homing rounds (9 motor moves). Initial status word 0x4855 (stale
   from power-on, poll to 0xF000 timed out — benign, continues).
3. Settle poll reached 0x35=0xBB / 0x32=0x1D (expected 0x1F — bit 1
   differs, likely magazine-sensor state; benign).
4. `BASE_INIT_PAIRS` + `AFE_BASE_PAIRS` written (normal power-on table).
5. PREP + AFE_BASE phases ran (dual-light IR mode, 3600 dpi).
6. Full calibration pipeline completed: dark pair, white line, gain
   codes (R=0x3F, G=0x3F, B=0x3F), gain check, shading measurement,
   shading upload + verify.
7. Positioned to frame 1 (FEEDL=6746) and scanned.
8. Output: 5184×5248 px, 16-bit RGB visible + IR channel.

**Result:** ✅ **PASSED.** Cold-start → scan works without vendor init.
This resolves the primary cold-start open issue (TODO #2 from the
project roadmap). No VM-based workaround needed for normal operation.

**Image quality:** Reasonable channel levels (R mean=199, G=89, B=82 in
positive; center std=54.7 — real image content). IR channel: mean=37.5,
std=1.5 (expected: low and flat). Compared against the pre-shading-fix
reference (`rulle-f1.tiff`, 2026-09-01): height differs by 12 lines
(doubled stagger 6→12 from the 2026-09-02 shading fix), colour balance
shifted (different shading table pairing) — noted for the IR-regression
comparison (TODO #2b).

**Known warnings during cold_init (all benign):**
- Initial status word poll timeout (0x4855 → wants 0xF000) — scanner
  hasn't completed its own power-on housekeeping; cold_init continues.
- Settle poll 0x32=0x1D instead of 0x1F — bit 1 (loader sensor
  interaction); cold_init warns and continues.
- Resync status 0xF855 instead of 0xF055 — sequence-variable; benign.

### Test 2: Reproducibility (A3) — 10× same frame

**Goal:** Verify that repeated scans of the same frame produce
reproducible results (same calibration, same image, no drift).

**Status:** ✅ COMPLETE — all 10 scans successful.

**Method:** 10 consecutive scans of frame 1 at 3600 dpi, dual-light
(IR) mode, single session (no re-init between scans). For each scan:
record per-channel means/std/percentiles, compute pixel-level RMS
difference between consecutive scans and first-vs-last.

**Results:**

| Metric | Value | Assessment |
|---|---|---|
| Channel mean spread (R) | 14.3 DN / 0.15% | Excellent |
| Channel mean spread (G) | 36.4 DN / 0.33% | Excellent |
| Channel mean spread (B) | 37.5 DN / 0.43% | Excellent |
| Channel mean spread (IR) | 25.9 DN / 0.27% | Excellent |
| Drift scan 0→9 | +0.09–0.20% all channels | Lamp warmup, normal |
| Pair-to-pair pixel RMS | 912–917 DN (mean 915) | Sensor temporal noise |
| Per-scan noise (RMS/√2) | ~647 DN | ~10 useful bits at 16-bit |
| SNR | 24.6 dB per single scan | Typical consumer film scanner |
| Noise floor σ (per-pixel) | 600–672 DN, stable ±5 DN | No flickering |
| Scan timing | 72–77 s per frame | Consistent |

**Verdict:** HIGHLY REPRODUCIBLE. Calibration is stable across all 10
scans. The small upward drift across all channels is consistent with
lamp warmup (thermal effect). Pixel-level variation is dominated by
sensor temporal noise, not calibration instability.

### Test 3: Offline static analysis (A1) — hardcoded value audit

**Goal:** Classify every hardcoded value in the driver by risk of being
unit-dependent.

**Method:** Systematic review of `tables_base.py`, `calibrate.py`, and
`device.py`.

**Key findings:**

| Classification | Count | Notable items |
|---|---|---|
| 🟢 GREEN (model constant) | ~15 | BASE_INIT_PAIRS, slope tables, loader speed, poll masks/timeouts, wire-format constants |
| 🟡 YELLOW (possibly unit-dependent) | ~8 | AFE_BASE_PAIRS (until EEPROM decoded), gain target (31673), shading2 targets, cold-start settle values, eject feedl |
| 🔴 RED (insufficient understanding) | 2 | **`_OFFSET_DEFAULT` (0x010B/0x010A/0x010B)** — placeholder, dark measurements discarded; **cold-start regs 0x4F/0x3B/0x3C** — cause unknown |

**Most critical finding:** `calibrate._OFFSET_DEFAULT` is documented in
the code itself as a low-confidence stand-in. The function `offset_codes()`
accepts dark-frame measurements but returns this constant unconditionally.
If AFE offset needs differ by unit, temperature, or lamp age, every scan
uses this one capture's value with zero feedback. This is the single
highest-priority item for robustness improvement.

**Second tier:** Cold-start homing feed lengths (`FEEDL_1_2=8730`,
`FEEDL_3=4620`) are captured byte-exact from one unit. If another unit's
home switch is positioned differently, these could over/under-travel.
No adaptive homing (sensor-based stop) is implemented yet.

### Test 4: DPI offline verification (A8) — 2400 dpi profile

**Goal:** Verify `tables_dpi2400.py` against the vendor capture and the
3600 dpi reference before hardware testing.

**Checkpoints:**

| Check | Result | Notes |
|---|---|---|
| IMAGE_WIDTH | ✅ 5256 px | Correct: vendor reads sensor at full 3600-dpi rate, resamples in software; our driver delivers raw sensor data (no resampling step) |
| SHADING_LINES | ✅ 256 | Matches all dual-light modules |
| feedl_for_frame() | ✅ FEEDL₁=6746, pitch=10760 | Identical to 3600 dpi dual reference |
| DEFAULT_LINES / chunks | ✅ 7088 = 443×16 | Arithmetic correct |
| Phase structure | ✅ Complete dual-light set | All phases present with correct injections |

**Verdict:** 2400 dpi profile passes offline verification. Ready for
hardware test (next priority after reproducibility).

**Note:** `--dpi 2400` outputs a 5256 px wide image (3600-dpi-equivalent
sensor data), not a resampled 3504 px image. This is by design — the
driver delivers raw data; resampling is left to the user's workflow.

### Test 5: DPI hardware verification (B7)

**Goal:** First hardware test of each non-3600 DPI profile (single frame,
no modifications, abort on anomaly per safety rules).

**Results:**

| DPI | Status | Output | Notes |
|---|---|---|---|
| 3600 | **VERIFIED** (reference) | 5184×5248 | Dozens of successful scans |
| 2400 | **VERIFIED** ✅ | 5256×3528 | Correct dimensions, real image content, same FEEDL as 3600 |
| 1200 | **VERIFIED** ✅ | 1752×1768 | Correct dimensions, real image content |
| 600 | **VERIFIED** ✅ | 876×878 | Profile fix confirmed (see Test 8). Correct dimensions, real image content (41–51% dynamic range). |
| 7200 | **VERIFIED** ✅ | 10512×10576 | Correct dimensions, real image content (40–50% dynamic range). 637 MB TIFF output. |

**Note:** The initial DPI tests (2400, 1200) in this session ran while
the shading A/B swap regression (Test 6) was active — those scans had
correct dimensions but no image content. After the shading fix, all five
DPIs were re-tested and verified with real image content (Test 5 table
updated, Tests 8–9 below).

**2400 dpi detail:** Init + calibration completed normally. Gain codes
R=G=B=0x3F (same as 3600). Numerous benign poll timeouts on reg 0x32
(0x9555 vs 0x8155) during the scan phase — same pattern as 3600 dpi.
Scan completed, PARK phase ran normally. Scanner returned to safe state.

**1200 dpi detail:** Init + calibration completed normally. Gain codes
R=G=B=0x3F. Same poll timeout pattern. Scan and PARK completed normally.

**600 dpi detail:** Init and PREP phases completed. CAL_DARK_A/B ran.
CAL_WHITE phase ran but the bulk-in data contained all zeros in channel 0,
causing `gain_codes()` to raise ValueError. No motor commands had been
issued yet (failure was in the calibration phase, before POSITION).
Scanner left in safe state (no mechanical risk). Root cause found — see
Test 8 below. Fix applied, awaiting hardware verification.

### Test 6: Image content diagnostic — shading table A/B swap regression

**Goal:** Investigate why all dual-light scans from this session produce
technically correct files but with no visible image content (flat noise).

**Discovery:** Maximum-stretch analysis of the raw 16-bit scan data
(2400 dpi retry, 3600 dpi cold-start, all 10 reproducibility scans)
showed zero spatial structure. Per-channel std/mean ratio ~6% — pure
sensor noise, no film modulation.

**Root cause found:** The pass 18 commit (09f3ca1, 2026-09-02 evening)
swapped the shading table A/B address assignment. The pass 18 analysis
correctly identified that the vendor computes table A (address
0x10014000) from EVEN (IR) measurement lines and table B (0x10034000)
from ODD (visible) lines. But it incorrectly assumed the scanner applies
each table to the SAME line type it was computed from. Empirical evidence
proves the opposite:

| Mapping | Code version | Dynamic range (positive) | Image content |
|---|---|---|---|
| visible→A, IR→B | Pre pass-18 (f84fd70) | 171% | ✅ Real images |
| IR→A, visible→B | Pass 18 (09f3ca1) | 22% | ❌ Flat noise |

The scanner hardware cross-connects: address A is applied to ODD
(visible) scan lines, address B to EVEN (IR) lines. The pre-pass-18
code accidentally had the correct mapping; pass 18 "corrected" it to
match the vendor's source-data assignment (even→A, odd→B), breaking the
cross-connection.

**Fix applied:** Restored the pre-pass-18 measurement-to-address
mapping (visible measurement → address A, IR measurement → address B)
while keeping the pass 18 formula improvements (shading_table2_dual
with per-table targets, no double offset subtraction).

**Status:** ✅ **FIX VERIFIED** (2026-09-03 hardware test). Scan after
fix: dynamic range 152–208 %, differentiated gain codes (R=0x2D,
G=0x21, B=0x28), natural colours in positive conversion. All offline
tests updated and passing (`test_ir.py`, `test_dpi.py`,
`test_calibrate.py`, `test_offline.py` — 16 tests total).

### Test 7: Frame position stability after DPI change

**Goal:** Verify that the full negative is always captured regardless of
which DPI the previous scan session used.

**Method:** Three scans at 3600 dpi, dual-light:
1. **Scan 1** — first 3600 scan after a session that ran 2400 dpi
2. **Scan 2** — same session, positive conversion (`--positive`)
3. **Scan 3** — new session after Scan 2's 3600 dpi PARK

**Results:**

| Scan | Film start (row) | Film end (row) | Bottom cut off? | Position correct? |
|---|---|---|---|---|
| 1 (after 2400) | 1085 | 5247 | YES (0 margin) | ❌ shifted +1059 rows (7.5 mm) |
| 2 (positive) | ~0 | ~5230 | no | ✅ |
| 3 (after 3600) | 26 | ~5220 | no | ✅ |

**Root cause:** POSITION uses mode 0x18 (relative feed from current
carriage position). After PARK, different DPIs leave the carriage at
slightly different positions. Within the same DPI, PARK consistently
returns to the same offset, so batch scans and same-DPI repeat scans
are unaffected. The 1059-row shift occurred because the 2400 dpi PARK
left the carriage ~7.5 mm offset from the 3600 dpi reference position.

**Mitigation:** Re-loading the magazine (via `tools/load_magazine.py`)
resets the carriage to the known load-position reference. A proper
homing command (GL126 home-sensor seek) would fix this permanently but
requires hardware testing.

**Status:** DOCUMENTED. Code comment updated. No code fix applied — the
issue only occurs when changing DPI between sessions without re-loading.
The vendor's workflow (always re-loads between DPI changes) avoids it.
A homing fix is planned for the next hardware session.

---

## 2026-09-02 — Batch scanning, eject, DPI profiles

*(Summary of prior session — see protocol-notes.md pass 14-18 for
detailed protocol analysis.)*

### Batch scanning — VERIFIED
- `--frames 1-4 --eject` produces four clean frames.
- Fix: `home()` removed from `scan()`, `BASE_INIT` written once per session.

### Eject — VERIFIED
- Root cause found: old `load_magazine.py` replayed vendor preview-prep
  sequence, leaving transport in a state the vendor never ejects from.
- New default load = insert + sweep only. Eject works from this state.
- Eject polls vendor status word (wValue 0x018E) to 0xF8 completion.

### Magazine sensor + button — VERIFIED
- `of135i watch` polls loader sensor and eject button.
- Sensor: ext reg 0x101 bit 0x08 (0xE0 = empty, 0xE8 = loaded).

### DPI profiles — IMPLEMENTED (hardware-verified 2026-09-03, see above)
- Profiles for 600, 1200, 2400, 7200 dpi generated from vendor captures.
  *(All five DPIs subsequently hardware-verified — see 2026-09-03 Tests 5, 8–9.)*
- All captures turned out to be IR-mode (dual-light) — every non-3600
  resolution always runs dual-light; `--ir` flag controls only whether
  the IR channel is used/output.
- Shading table pairing corrected (A@0x14000 = even/IR lines,
  B@0x34000 = odd/visible lines).
- Channel stagger in IR mode doubled (6→12 lines at 3600 dpi).

### Cold-start (cold_init) — PARTIAL
- Verified for eject (power cycle with magazine in → cold_init → eject).
- **Full scan from cold state: not tested until 2026-09-03 (see above).**

---

## 2026-09-01 — First working scans

*(Summary — see protocol-notes.md pass 1-13.)*

### Single-frame 3600 dpi — VERIFIED
- First successful scan from the Linux driver.
- Verbatim op-stream replay (not register-only) required for correct
  calibration levels.

### IR + dust removal — VERIFIED
- Dual-light pass captures alternating IR/visible lines.
- `image.remove_dust()` inpaints visible-channel defects using IR map.

### Positive conversion — VERIFIED
- LUT-based negative→positive fitted to vendor app's sRGB output.
- sRGB ICC profile embedded in TIFF output.

### Motor stall incident
- Blind motor command from undefined mechanical state → grinding noise.
- Christian cut power immediately (correct response).
- Recovery: power cycle + vendor QuickScan in VM.
- **Lesson:** Never issue motor commands without verified mechanical state.
  This incident led to the creation of the hardware safety rules document.

---

## 2026-09-03 (session 2) — 600 dpi CAL_WHITE fix, design review

### Test 8: 600 dpi CAL_WHITE crash — root cause analysis and fix

**Goal:** Find and fix why 600 dpi white calibration returns all-zero R
channel data.

**Method:** Cross-DPI register comparison of all vendor captures
(600/1200/2400/3600/7200 dpi).

**Root cause:** Register 0x2b in the CAL_WHITE phase. The five DPI
profiles group into three sensor-mode families by registers 0x29/0x2a:

| Group | 0x29/0x2a | DPIs | 0x2b (vendor capture) |
|---|---|---|---|
| A | 0x2f / 0x47 | 600, 1200 | 600: **0x1f** ❌, 1200: 0x04 ✅ |
| B | 0x34 / 0x57 | 2400, 3600 | 0x1f |
| C | 0x3e / 0x77 | 7200 | 0x3d |

600 dpi's 0x2b=0x1f is the Group B value (2400/3600 dpi), not the Group A
value used by 1200 dpi (0x04). The vendor captures were recorded
sequentially (likely 3600→2400→1200→600→7200); the 0x1f was a stale
register value left over from the preceding 2400/3600 session that the
vendor software didn't explicitly reset.

With 0x29=0x2f and 0x2a=0x47 (Group A sensor mode), 0x2b=0x1f
misconfigures the sensor timing, resulting in all-zero readout on
channel 0 (R). The same 0x2b=0x1f works correctly with Group B's
0x29=0x34 / 0x2a=0x57.

**Fix:** Changed `tables_dpi600.py` CAL_WHITE register 0x2b from 0x1f
to 0x04 (matching 1200 dpi, which shares the same Group A sensor mode
and works correctly).

**Additional finding:** AFE_BASE also has a 0x2b discrepancy (600:
0x03, 1200: 0x01) with the same 0x29/0x2a=0x2a/0xb7. This doesn't
cause a crash (AFE_BASE runs before CAL_WHITE reconfigures), but may
affect calibration quality. Noted for hardware testing.

**Status:** 🔧 FIX APPLIED, awaiting hardware verification. All 16
offline tests pass.

**Design note:** This bug is a textbook example of why verbatim vendor
replay is fragile — the driver faithfully replayed a capture artifact
(a stale register from a different DPI session) as if it were an
intentional configuration. Understanding what each register does and
setting values from first principles makes the driver robust against
this class of bug.

### Test 9: A3 reproducibility retest — corrected shading pipeline

**Goal:** Re-run the A3 reproducibility test (Test 2) now that the
shading A/B swap (Test 6) is fixed, to confirm the *corrected*
image pipeline's stability rather than just the USB/calibration layer.

**Method:** 10 consecutive scans of frame 1 at 3600 dpi, dual-light
(IR) mode. Scans 0–5 ran in a warm session (scanner already active
from prior 600/7200 dpi testing); scans 6–9 ran after a power cycle
(cold start, cold_init auto-triggered).

**Results (scans 0–5, warm start):**

| Metric | Value | Assessment |
|---|---|---|
| Dynamic range (std/mean) | R=81%, G=75%, B=67% | ✅ Real image content |
| Channel mean spread (R) | 0.37 DN / 0.83% | Excellent |
| Channel mean spread (G) | 0.24 DN / 0.82% | Excellent |
| Channel mean spread (B) | 0.23 DN / 1.13% | Excellent |
| Drift scan 0→5 | R=+0.83%, G=+0.82%, B=+1.14% | Lamp warmup, normal |
| Pair-to-pair pixel RMS | 1.6 DN (all pairs identical) | Extremely stable |
| Gain codes (all 6 scans) | R=0x2D, G=0x21, B=0x27 | Consistent, not clipped |

**Scans 6–9 (cold start): ❌ FLAT IMAGES.**
After a power cycle (caused by USB timeout during scan 6 in the first
batch), cold_init ran and all four scans produced gain codes R=G=B=0x3F
(clipped maximum) and flat output (std/mean ~6%, no spatial structure).
The lamp was insufficiently warmed after cold_init — the white
calibration measured very low light levels, maxing the AFE gain.
Identical behaviour across all four cold-start scans confirms it is
systematic, not random.

**Comparison with original A3 (Test 2):**

| | Test 2 (broken shading) | Test 9 (corrected) |
|---|---|---|
| Image content | ❌ None (6% dynamic range) | ✅ Real (67–81%) |
| Channel spread | 0.15–0.43% | 0.82–1.13% |
| Pair RMS | 912–917 DN (16-bit) | 1.6 DN (8-bit = ~410 DN at 16-bit) |
| Gain codes | R=0x3F, G=0x3F, B=0x3F | R=0x2D, G=0x21, B=0x27 |

The original A3 measured the stability of a *broken* pipeline where the
shading correction flattened the signal — very reproducible because there
was nothing to modulate. The retest confirms the corrected pipeline is
equally stable but now produces actual images.

**New finding — cold-start lamp warmup:** Immediate scanning after
cold_init produces maxed gain (0x3F) and flat images. The vendor's
workflow likely includes a warm-up period (preview pass, loading
animation). A warm-up delay or gain-level retry loop after cold_init
is needed.

**Status:** ✅ A3 VERIFIED (warm start), ❌ cold-start scanning needs
lamp warmup mitigation.

---

## 2026-09-04 — AFE offset, lamp warmup, IR regression analysis

### Test 10: 3600 dpi IR regression — offline comparison

**Goal:** Compare the IR channel from a post-shading-fix scan
(`fix-test-f1-ir.tiff`, 2026-09-03) against the pre-fix reference
(`rulle-f1-ir.tiff`, 2026-09-01) to check for IR quality regression.

**Method:** 16-bit channel statistics comparison using tifffile
(Pillow's reader truncates to 8-bit).

**Results:**

| Metric | Pre-fix (rulle-f1-ir) | Post-fix (fix-test-f1-ir) |
|---|---|---|
| Shape | 5260×5184 | 5248×5184 |
| IR mean | 32514 | 39918 |
| IR std | 23397 | 27026 |
| IR min | 985 | 7768 |
| IR max | 55866 | **65535 (saturated)** |
| p50 (median) | 48380 | **65535 (saturated)** |
| p1 | 1212 | 9179 |
| Dark pixels (<~10k) | ~36% (film area) | ~40% (film area) |

**Finding:** The post-fix IR channel saturates at 65535 in bright
(clear film / no dust) areas. More than 50% of pixels are clipped.
The pre-fix version had no saturation (max=55866).

**Root cause:** The pre-fix version (2026-09-01) used the wrong
shading formula for the dual-light mode — a single-table formula
that coincidentally produced non-saturating IR values. The current
code uses the vendor-derived per-address shading targets
(SHADING2_TARGET_B = 90112), which produce higher gain in the IR
shading correction.

**Impact assessment:** The saturation does NOT affect dust detection
usability. The IR channel's purpose is binary discrimination between
"dust/scratch" (dark defect) and "clean film" (bright background).
With the post-fix data, dark defects sit at ~8k–10k counts vs bright
at 65535 — ample contrast (>6:1 ratio). The `remove_dust()` function
operates on this contrast and continues to work correctly (verified
by the synthetic dust removal test in test_ir.py, and by visual
inspection of fix-test-f1-pos-ir.tiff which shows clean dust removal
results).

**Height difference:** 12 lines (5260 vs 5248), consistent with the
doubled channel stagger (6→12 lines) introduced in the 2026-09-02
shading fix. Expected and correct.

**Status:** ✅ NOT A REGRESSION. IR saturation in bright areas is
the expected result of using the vendor's correct shading targets.
Dust detection function is unaffected.

### Implementation: AFE offset from dark measurements

**Change:** `calibrate.offset_codes()` now computes the per-channel
AFE offset code from the two-point dark bracket (offset=0x80 and
offset=0xff measurements), instead of returning a hardcoded constant.

**Formula:** Per channel, the slope of dark level vs offset code is
measured from the bracket. The final code is `0xff + round(margin /
slope)`, where the margin in dark-level space (211/198/215 counts
for R/G/B) is derived from the reference unit's vendor capture. On
the reference unit this reproduces the vendor's codes exactly
(R=0x010b, G=0x010a, B=0x010b). On a unit with a different AFE
slope, the code count adapts proportionally.

**Fallback:** If the bracket slope is abnormal (< 1 count per code
step — e.g. zero-filled mock data), the hardcoded default is used.

**Tests:** 3 new offline tests (zero-dark fallback, reference bracket
round-trip, slope adaptation). All 18 offline tests pass. Sequence
test unchanged (mock's zero dark data triggers the fallback path).

**Status:** OFFLINE VERIFIED. Needs hardware verification.

### Implementation: Lamp warmup retry

**Change:** `Scanner._gain_with_warmup()` wraps the white-line
measurement + gain computation. If all three gain codes are at
maximum (0x3F), the method waits 5 seconds and re-runs the
CAL_WHITE phase, up to 3 retries (15 seconds total maximum).

**Rationale:** After `cold_init()`, the lamp has not warmed up,
causing the white measurement to return very low levels. The AFE
gain clips to 0x3F (maximum), producing flat/underexposed images.
The vendor likely avoids this via a preview pass that doubles as
warmup time. The retry loop achieves the same effect without
requiring a full preview implementation.

**Behavior:** Both `scan()` and `_scan_dual()` use the retry.
If gain stabilizes below 0x3F, scanning proceeds normally with
a log message. If still maxed after 3 retries, scanning proceeds
with a warning (does not abort — the user may still want the data).

**Status:** IMPLEMENTED. Needs hardware verification (cold-start
scan after power cycle).

---

## 2026-09-04 (evening) — First hardware run of doctor/hwblock, firmware hang

### Context

First hardware session with the new diagnostics (`of135i doctor`,
`.diag.json` sidecars, `tools/hwblock.py`). Scanner powered on from
cold, cassette inserted but not pushed to the stop ("in, not locked",
eject button orange).

### Test 11a: `doctor` on a cold scanner — PASSED (read-only)

`doctor` ran to completion in a few seconds: USB descriptors, chip id
`01`, reg 0x01 = 0x00 (cold-never-homed), status word 0x4855, 288
registers dumped, loader sensor "loaded", button event "sensor".
No writes issued. Report saved as `doctor-0.json` in the private
analysis directory.

### Test 11b: cold_init + load_magazine.py — PASSED mechanically, state open

`tools/load_magazine.py` ran cold_init (3 homing rounds) and the
default load flow. Same benign warnings as Test 1 (initial status
word 0x4855 poll timeout, settle 0x32=0x1D, resync 0xF855), plus
`poll ... last cc55 want d855` at the end of the load. Christian:
"everything sounds normal". Button steady BLUE. `doctor` afterwards:
reg 0x01 = **0x02** (not 0x22), status word 0xcc55, sensor loaded.

**Open finding (TODO 9b):** the magazine sat LOOSE but in place after
the load while the LED showed blue; Christian states that state should
show ORANGE. Our load flow sets the "loaded" indication without the
magazine being latched. Not yet analysed. Christian then seated and
locked the magazine by hand (LED still blue).

### Test 11c: hwblock warm, first attempt — ABORTED by operator decision

Started `hwblock.py warm --repeat 10`. While the first scan was in
CAL_WHITE (warmup retry had triggered: gain 0x3F on a lamp only
minutes from cold start), Christian reported the magazine loose. I
stopped the process with SIGINT (safety rule B6) — inside the bulk
read of the white measurement.

State left behind (doctor): reg 0x01 = **0x23** (scan bit set, engine
running), 0x03 = 0x30 (lamp on), status word 0xa555, sensor bit clear.

### Test 11d: new session on top of the aborted state — FAILED, FIRMWARE HANG

Second `hwblock warm` exited at W0 because `is_magazine_loaded()`
read false (sensor bit is unreliable once a session has written the
base table — TODO 9c). Added `--assume-loaded` and started a third
run. `initialize()` (base table 0x01=0x22, 0x02=0x78 … + PREP +
AFE_BASE) ran on top of the still-running engine; polls showed
0x32=0x99 (want 0x95) and status classes 0xB1/0xB5 (want 0xF8/0xFC).
CAL_DARK_A's execute pulse followed; Christian heard a **loud two-tone
sound** that stopped after a moment; the next control write timed out
(`USBTimeoutError`). Afterwards the device stayed enumerated but every
control read timed out (2 s). Kernel log: nothing.

**Root cause (assessment):** re-initializing a scanner whose scan
engine was left running by an aborted session. The vendor's base
table assumes an idle engine; writing it plus an execute pulse into a
running engine produced a motor event and a firmware lock-up. The
A9 recovery premise "a new process against an *idle* scanner" does
not extend to a scanner with the engine running.

**Recovery:** power off (done by Christian). Session paused there.

**Changes made:** `hwblock` W0 now refuses to start unless reg 0x01
is 0x22 (idle-homed) or 0x00 (cold) and tells the operator to
power-cycle; `--assume-loaded` documented as "human confirmed locked
magazine" only. TODOs 9b/9c/9d recorded (CLAUDE.md).

**Rules confirmed/added:**
- After ANY abort inside a phase, the only recovery is a power cycle.
  Never start a new session when reg 0x01 is neither 0x22 nor 0x00.
- The loader-sensor precheck is only meaningful before the first
  `initialize()` of the scanner's power cycle.

**Status:** doctor HARDWARE VERIFIED (read-only path). hwblock W0 +
first-scan calibration reached hardware; W1–W6 NOT RUN. Warmup retry
observed triggering on hardware (gain 0x3F, retry 1/3 logged) but the
scan never completed, so its effect is still UNVERIFIED. AFE offset
codes: no completed scan, UNVERIFIED. Semantic PARK: not exercised.

### Test 11e: cold block — warmup insufficient, crash on zero white line

After the power cycle, `doctor` confirmed a clean cold state (reg 0x01
= 0x00, sensor loaded). `hwblock.py cold` ran cold_init to completion
(no abnormal sound reported), then the first scan's warmup retry
triggered as designed: attempt 1 and 2 both read maxed gain
(R=G=B=0x3F, lamp dim), 5 s apart. Attempt 3 read an **all-zero**
white line, and `calibrate.gain_codes()` raised `ValueError` on the
zero peak, crashing the scan at C3.

**Two findings:**

1. **Crash bug (fixed, commit 7bbbf95):** a zero white line is a
   valid cold-lamp reading, not an error. `gain_codes(clamp_nonpositive=
   True)` now maps a non-positive peak to the max gain code, so the
   warmup loop treats it as "not ready", retries, and gives up
   gracefully. Regression test added.

2. **Warmup budget too short (open):** 15 s (3 x 5 s) is not enough
   for the lamp after a cold start — gain stayed maxed/zero across all
   three attempts. The vendor's preview pass gives the lamp much
   longer. Re-running as-is would give up after 15 s and produce a
   flat image, so cold-start scanning is still NOT verified. Next step
   is a read-only warmup-timing probe (re-read CAL_WHITE every few
   seconds, log the white level until it stabilises) to measure the
   real warmup time before choosing a budget — not blind tuning.

**Scanner state:** responsive throughout (control reads worked after
the crash: reg 0x01 = 0x02, 0x35 = 0xfb). NOT hung — unlike Test 11d,
the crash was in a calibration read before any scan motor command, so
no motor event. Left powered on, lamp likely on (reg 0x03 = 0x30);
a power cycle before the next session is cleanest.

**Status:** warmup retry MECHANISM hardware-verified (it triggered and
looped correctly); cold-start image UNVERIFIED (lamp not warm within
budget); crash fixed offline.


## 2026-09-05 — Hardware-safety pass (offline)

### Context

Following the 2026-09-04 firmware hang (Test 11d) and the still-open
warmup-budget question (Test 11e), the ad-hoc start-state check that
`hwblock.py` had grown into was generalised into one authoritative
mechanism: `of135i/safety.py`. Full model in docs/hardware-safety.md.

### What changed

- **Centralized start-state guard.** Every writing entry point —
  `scan`, `eject`, `initialize`, `cold_init`, `load_magazine`, `home`,
  `park_semantic`, `watch`, and `hwblock.py`/`replay_trace.py` — now
  goes through the same guard, not just `hwblock.py` as before.
  Accepted start states: reg 0x01 == 0x22 (idle-homed, normal
  operations) and reg 0x01 == 0x00 (cold, cold-init path only). Every
  other value, and every failure to read the register (USB error,
  timeout, short/malformed reply), is refused with zero USB writes and
  no automatic recovery — a dedicated exception tells the user to
  power-cycle.
- **`GuardedDevice`** wraps the pyusb device so every control-OUT and
  bulk-OUT transfer, from anywhere in the driver or tools, passes
  through one gate that is asked permission before the transfer and
  counted afterwards.
- **Per-session model**, not per-write: the check runs once, before a
  session's first write. A batch scan is one session; the transient
  engine states between phases inside it (0x02/0x03/0x23) are expected
  and not re-checked. A new process is a new session and is validated
  again.
- **No automatic recovery, ever.** No PARK, home, eject, or
  re-initialization runs in any `finally`, on `KeyboardInterrupt`, or
  on any other failure. The only recovery is a physical power cycle;
  restarting the process is explicitly not sufficient.
- **Process lock.** An exclusive `flock` on
  `/tmp/of135i-07b3-1436.lock` (override `OF135I_LOCK_FILE`) refuses a
  second of135i process — writing or read-only `doctor` — before it
  touches USB.
- **`doctor`/`status` proven strictly read-only.** Offline tests show
  zero OUT transfers, no `set_configuration`, no `initialize()`/
  `cold_init()`, and no recovery attempt, even against an interrupted
  scanner reading 0x23.
- **Magazine sensor explicitly not treated as lock proof.** The loader
  sensor bit is documented as presence-only, unreliable after the
  first `initialize()`, and not a substitute for a person confirming
  the magazine is seated and locked (Test 11b). `--assume-loaded`
  remains controlled-development-only and does not bypass the guard.
- **Unguarded motor-write paths removed.** `tools/load_magazine.py`'s
  `--full` flow was removed (its end state stalled the transport);
  `tools/of135i_poc.py` (raw home/eject writes, no guard) was deleted.
  `tools/replay_trace.py` now runs over the guarded transport and
  aborts on the first USB error instead of clearing stalls.
- **New modules:** `of135i/safety.py`, `of135i/errors.py` (shared
  `Of135iError` base), `of135i/tables_load.py` (vendor magazine-load
  sequence compiled from a trace, driven by `Scanner.load_magazine()`),
  `tools/gen_load_table.py` (generator for the above).

### Verification status

**OFFLINE VERIFIED ONLY.** `tests/test_safety.py` (27 new tests) plus
the existing offline suite all pass. NOTHING new is hardware-verified —
the guard was deliberately not tested by recreating an unsafe physical
state (that is what bricked the scanner on 2026-09-04, Test 11d). The
only permitted next hardware step is a single conservative normal-path
scan from a power-cycled, known-good scanner — not a repeat run, a DPI
sweep, or a full `hwblock` run.

See docs/hardware-safety.md for the full model, the accepted
start-state table, and what remains unverified.

## 2026-09-04 — Safety follow-up: verify before configure, short OUT transfers (offline)

### Context

Code review of the safety pass found two violations of its fail-closed
claims: (1) `UsbIo.open()` issued kernel-driver detach and
`SET_CONFIGURATION` on the raw pyusb handle *before* the start state
was read, i.e. state-changing requests reached an unverified scanner
and bypassed `GuardedDevice`; (2) `GuardedDevice` treated any
non-exception return from an OUT transfer as complete, so a short
transfer (pyusb reporting fewer bytes than requested) was counted as
a successful write and the sequence continued.

### What changed

- **Open order.** lock → find → one `HardwareSession` + proxy →
  strict reg 0x01 read through the proxy → classify → *only then*
  detach/`SET_CONFIGURATION` on the local raw handle. A refusal
  releases handle and lock with zero OUT transfers and zero
  state-changing calls; a configuration failure after acceptance
  marks the same session failed. The kernel driver is never detached
  to make the check possible. `UsbIo` no longer stores the raw handle.
- **Short transfers.** The proxy compares the reported length with the
  actual payload length (0 for the verified zero-length requests).
  A mismatch fails the session, raises `ShortTransferError` with the
  lengths, operation, phase and execute-pulse flag on record, counts
  the transfer as attempted but not completed, sends nothing further,
  and requires a power cycle.
- **Three functional-test fakes** (`test_calibrate`, `test_dpi`,
  `test_ir`) returned nothing from `write()`; they now return the
  length, as pyusb does — the guard had correctly flagged them.

### Verification status

**OFFLINE VERIFIED ONLY.** `tests/test_safety.py` grew from 27 to 38
tests (5 exercising the real `UsbIo.open()`/`Scanner.open()` over a
fake device with ordered event logging, 7 short-transfer fault
injections); the whole offline suite passes (38 + 10 + 14 + 6 + 6 + 4 +
3 + 3). **No physical scanner operation was performed.** New
hardware-side caveat: reading reg 0x01 before `SET_CONFIGURATION` has
never been exercised on this scanner; if it fails, the driver refuses
rather than configuring first (see docs/hardware-safety.md).

## 2026-09-04 (late evening) — First hardware run after the safety pass: guard holds, load ends unsafe

### Context

Driver at 9ddfa08 (safety pass + review fixes). Plan: the single
permitted normal-path scan (doctor → 0x00/0x22 → magazine locked by
hand → one scan → doctor). Scanner power-cycled before start.

### Test 12a: `doctor` before `SET_CONFIGURATION` — PASSED (read-only)

Fresh power-on, magazine inserted loose (orange LED). `doctor` read
everything through the read-only open (no `SET_CONFIGURATION`, no
kernel-driver detach): reg 0x01 = 0x00 (cold), status word 0x4855,
chip id 01, reg 0x101 = 0x48, sensor "loaded". First hardware
evidence that a device-recipient control-IN works before
configuration. The writing open (`load_magazine.py`, next test) also
verified 0x00 through the proxy before configuring — the reordered
open sequence works on hardware.

**Sensor finding (TODO 9b):** the loader sensor reports "loaded" for
a magazine that is merely inserted (orange LED, not fed, not locked)
— before any register table has been written. It is a presence
sensor, nothing more. Confirmed again after the second power cycle
(`doctor-3`: 0x00, 0x101 = 0x48, "loaded", magazine loose).

### Test 12b: `load_magazine.py` from 0x00 — completes, end state 0x02 (×2)

cold_init (three audible homing rounds, same benign settle warnings
as Tests 1/11b) then the vendor insert flow. LED: orange off/on/off
in step with motor sounds, then steady blue, power LED blinking then
steady. Exit 0. `doctor` afterwards: reg 0x01 = **0x02**, status
word **0xcc55** (the load's last poll wanted 0xd855 and timed out at
0xcc55, as on 2026-09-04), 0x32 = 0x15, 0x35 = 0xfb, 0x101 = 0xcc.
Unchanged minutes later (`doctor-2`). Repeated in full after a power
cycle (Test 12d): byte-identical register dump except 0x2e (0x0b vs
0x07). So 0x02 is the deterministic end state of our LOAD replay, not
a transient, and the guard refuses every writing operation from it.

**Offline analysis of 0x02 (done during the session):**
- Our LOAD = ops 291-640 of `20260902-vendor-eject-from-loaded`. In
  that capture the vendor never reads reg 0x01 at all; it writes
  0x01=0x22 in its base table at session start and proceeds. Its
  status word after the load is 0xd855; ours is 0xcc55 (bits 0x10 and
  0x04 differ in the high byte, reg 0x101).
- In `20260830-184448-vendor-load-only` the vendor polls reg 0x01 and
  sees 0x22 (×100) after its (longer) load flow; 0x02 appears only
  while the engine executes a pass (12 631 fast reads during the
  traverse), consistent with Pass 8 (bit 0x20 clears while the engine
  runs, sets on completion).
- So our replay leaves the engine with bit 0x20 clear and a status
  word the capture never shows at that point: an op whose completion
  we time out on and skip. Every earlier verified scan from a
  driver-loaded magazine started from exactly this 0x02, via
  `initialize()`'s base table (0x01=0x22) — it worked, but was never
  explained. **No override was added.** Decision deferred to the
  offline analysis of which LOAD op is left incomplete.

### Test 12c: magazine does NOT lock by hand after the load — STOPPED

After the second load (blue LED) Christian pushed the magazine to the
stop: it does not latch. On 2026-09-04 (Test 11b) it did. New
observation; cause unknown (mechanical, or the load end state). With
an unlocked magazine no scan was attempted. Session ended with a
power cycle (0x02 state left behind, no writes after the refusal).

### Result

- Safety guard hardware-verified on the normal path: read-only doctor
  before configuration, verify-before-configure on the writing open,
  cold path (0x00 → cold_init → armed) — all as designed. Refusal on
  0x02 exercised read-only via `doctor` (no writing entry point was
  invoked against it).
- The one planned normal-path scan was **not** performed: no
  accepted state with a locked magazine was reachable (load ends in
  0x02; cold path needs a loaded magazine; magazine would not lock).
- Artefacts: `hw-2026-09-04-verify/doctor-{0..4}.json` (analysis
  area).

### Next (offline, before any hardware)

1. Find which LOAD op's completion poll fails (want 0xd855, last
   0xcc55) and what the vendor does right after op 640 that we do
   not; decide whether the LOAD replay is incomplete or the hardware
   differs. Only then decide how the loaded state is reached safely.
2. Magazine latch: compare 12c with 11b (what differed: hand-seating
   before load? the second load on the same insertion?).

### Addendum (same night): analysis, review, decisions

**0x02 analysed offline.** Reg 0x01 is a driver-written register; the
hardware clears bit 0x20 while the engine runs (Pass 8) and the load
flow never rewrites 0x22. Both complete vendor load flows end with reg
0x01 = 0x02 written by the vendor itself (`vendor-coldload` op 3189,
`load-only-fixed` op 2353) and a status word of 0xd855 → 0xdc55; the
vendor's next action from the loaded state is always its eject batch
or a scan session starting with the base table (0x01=0x22). So 0x02
is a normal possible end state — **but only together with the rest of
the vendor's end signature**. Our post-load state (0x02 + 0xcc55, no
loader pulses) is known-incomplete.

**Review outcome (Christian + external review):** a rule accepting
0x02 on the motor flag alone would also accept the incomplete state
and is therefore not merged. It is parked, inactive, on branch
`wip/loaded-idle-start-state`. Future acceptance must be a named
composite classification (e.g. `LOADED_READY`) from several
independent register values, coded only after the exact end signature
is established from the captures (0xd855 vs 0xdc55 to be settled, no
convenience range) and the load flow itself is complete and verified.

**Done tonight (offline, tests green: 39 in test_safety.py):**
- False success fixed: `load_magazine()` reads the status word after
  the replay and fails — `LoadIncompleteError`, session FAILED, power-
  cycle instruction, tool exit 1 — unless it equals the capture's
  completion value (0xd855, derived from the table's final poll). Both
  of tonight's loads would have failed.
- Sensor semantics: `is_magazine_loaded()` → `is_magazine_present()`,
  doctor key `magazine_present`, `--assume-loaded` → `--assume-locked`.

**Register note:** the vendor's post-load status word is 0xd855 in
the eject-from-loaded loop and 0xd855→0xdc55 in both complete loads;
0x32 reads 0x05 for the vendor, 0x15 for us; our 0xcc55 differs in
0x101 bits 0x10 and 0x04. These bits were identical on 2026-09-04 when
the magazine *could* be latched by hand, so they do not track the
latch.

**Christian's QuickScan observation (TODO 9b):** the vendor app does
not accept a magazine that is already partly inserted at start
("Please insert the film holder"); it must be taken COMPLETELY out of
the slot and inserted afresh, and only that insertion triggers the
full load that pulls the magazine in and latches it (blue LED). The
vendor's load therefore runs from the sensor-trigger position of a
fresh insertion; `load_magazine.py` asks for the cassette "to the
stop" first and then feeds the same distance — a plausible cause of
the loose-magazine-with-blue-LED result. Next hardware check, no
motor: read reg 0x101 bit 0x08 (and the interrupt endpoint's 0x04
event) while inserting slowly; note where the sensor trips relative
to the stop.

**Load-flow finding for the offline comparison:** both complete
vendor loads contain six mode-0x78 loader pulses (FEEDL 1, with
0x01=0x03/0x02 toggles); `vendor-coldload` consists of those pulses
only; our LOAD (eject-from-loaded ops 291-640) has none. Pass 13
called them loading, Pass 14 preview preparation. Whether they latch
the magazine is the question the op-by-op comparison must answer.

### Addendum 2 (same night): load analysis done offline — docs/load-analysis.md

Op-by-op comparison of eject-from-loaded, load-only and coldload:
the mechanical load is exactly feed 6690 + traverse 71490, our LOAD is
byte-identical and **not truncated**; the "six loader pulses" are the
preview preparation's six line reads (FEEDL 1, bulk-IN data, no
movement) and `vendor-coldload` contains no load at all. d855 vs dc55
are both vendor loaded-idle values (session-stable, bit 0x04); the
table's own completion value d855 is what is required, not a range.
Decisive bit: after the vendor's feed the loader-sensor bit 0x08 is
**clear** (cassette pulled past the sensor); after ours it stays
**set** — the transport ran, the cassette did not follow. Consistent
with the cassette having been pushed past the engagement point ("to
the stop") before the load. Driver: both LOAD completion polls are
now strict (flow stops after an unengaged feed, session FAILED, power
cycle); `tools/sensor_probe.py` added (read-only, zero writes proven).
Next hardware step in load-analysis.md §4. 40 safety tests green.

## 2026-09-05 — Test 13: load from the sensor trigger point (guard worked, table suspect)

### Setup
Power-cycled, magazine fully out. `tools/sensor_probe.py` (read-only,
0 writes) run twice: the loader sensor trips (reg 0x101 0x40→0x48,
reg 0x32 0xc2→0xc6, orange LED) partway in, roughly a cm short of the
mechanical stop, with NO driver activity — presence sensor confirmed
independent of the driver. Magazine left at the trigger point.

### Test 13: `load_magazine.py` from the trigger point — FAILED (as designed)
cold_init (3 homing rounds) → base table → the LOAD feed. The first
strict completion poll refused: status word **0xec55** after the feed,
capture wants **0xf055**. `StrictPollTimeoutError`, session FAILED, 224
writes / 10 pulses, no traverse, power-cycle demanded, tool exit 1.
`doctor` afterwards: reg 0x01 = 0x02, status word 0xec55, reg 0x101 =
0xec, reg 0x02 = 0x18. Blue LED came on and there was motor noise, but
Christian saw no magazine movement and confirmed it sat loose, exactly
where he left it. So the blue LED is set by the feed op, not by the
magazine being drawn in — and the feed does NOT engage the cassette.

Bit reading holds: after our feed 0xec55 has loader-sensor bit 0x08
**set** (cassette still at the sensor); the vendor's f055 has it
**clear** (cassette pulled past). Position was not the variable —
11b/12b (to the stop) and 13 (trigger point) all give the unengaged
result. The new strict-poll guard caught it cleanly and stopped before
the traverse.

### Finding: the LOAD table was regenerated from the wrong context
`of135i/tables_load.py` is AUTO-GENERATED at commit 3dd825e (the safety
pass, 2026-09-05) from `20260902-vendor-eject-from-loaded` ops 291-640
— a capture whose purpose was *eject*, where the load ran after the
app-start jog with the session's registers already programmed. The
2026-09-02 hardware-verified load used the *old* load_magazine.py off a
different source (the load-only capture). In the `load-only` capture
the feed is programmed with the FULL register block right at the feed
(op 794: 32 regs incl. 0x03=0x30, 0x15=0x90, 0x35=0xbb, plus op 796's
32 more), whereas our LOAD (from eject-from-loaded) writes only the
FEEDL + slope regs (19), relying on prior session state. Our
`initialize()` (BASE_INIT_PAIRS) does set all those registers, but a
few VALUES differ from what load-only programs at feed time —
0x03 (0x20 vs 0x30), 0x15 (0x80 vs 0x90), 0x35 (0xfb vs 0xbb). 0x35 is
motor-related (the cold_init settle poll waits on 0x35=0xbb). Whether
those deltas are why the feed does not engage is unproven, but the
table's provenance matches the review's "wrong context" suspicion: the
currently-shipped LOAD has NEVER been hardware-verified as a load —
only the eject cut from the same capture has.

### Decision / next step
No more Linux motor runs from this table. The authoritative fix is a
fresh Win11/QuickScan capture of a clean standalone load (magazine
fully OUT → reinsert → load → confirm latched → stop), usbmon on the
Linux host, then regenerate LOAD from THAT and diff. That capture also
records the working status word (f055 after feed, sensor bit going
clear) and settles d855-vs-dc55 (Test 12 addendum) from a known-good
load. Scanner read 0x00 cold, magazine out, after the Test 13 power
cycle.

## 2026-09-05 — Test 14: clean vendor load captured — root cause of the loose magazine

`captures/20260905-vendor-clean-load.pcap` (usbmon1 on the Linux host,
QuickScan in the Win11 VM driven by the VM Claude session; magazine
taken fully out on QuickScan's prompt and reinserted fresh; it latched
the instant it reached the stop). 442 ops. The decisive comparison:

### What the vendor's engaging load feed actually is
The sequence: register setup (op 40, mode 0x78 loader profile) → an
app-start JOG (op 109 feed 6690, op 137 feed 6690, op 155 eject 3090)
→ a ~25 s idle-poll GAP where the operator removed and reinserted the
magazine → **the engaging load feed at op 199**, which is `mode 0x18
FEEDL 6690` immediately followed by a FULL 47-register reprogram
(op 201: 0x14-0x3a incl. **0x35=0xbb, 0x15=0x90, 0x03=0x30**) BEFORE the
GO → traverse (op 218, mode 0x1c FEEDL 71490) → locked, idle at dc55.

Our `tables_load.LOAD` (from eject-from-loaded ops 291-640) runs the
feed as a **19-register** batch with NONE of that block: it executes
with whatever the base table left (0x35=0xfb, not 0xbb). The engaging
feed here programs **64 registers vs our 19**; the 47 it adds are
exactly the motor/exposure/timing set our feed omits. 0x35 (motor) at
feed time is 0xbb in every engaging load, 0xfb in ours. **That is why
the transport runs but the cassette does not follow** — confirmed
across Tests 11b/12b/13 (to-the-stop and trigger-point both failed
identically) and now explained.

This matches `load-only-fixed` (feed op 794 n=32 + op 796 reg block)
and is the "wrong context" the review suspected: LOAD was regenerated
at 3dd825e from an EJECT capture whose load ran with registers already
set, so the extracted feed is stripped of the setup a standalone load
needs.

### Completion values (fix the strict polls)
Feed completion here: **f455**; traverse: **dc55**. Our strict targets
(from eject-from-loaded) are f055 / d855 — i.e. the strict poll added
2026-09-05 would REJECT even this correct vendor load. The session-
invariant signal is the loader-sensor bit **0x08 going CLEAR** after
the feed (cassette pulled past the sensor: f0/f4 both have it clear;
our failed ec55 had it set). The completion check must key on that bit
(high nibble = done AND bit 0x08 clear), not on an exact status-word
value or a convenience range. d855 vs dc55 is thereby settled: both
are done-class, bit 0x04 is session-variable; the fresh-insert load
settles at dc55.

### Next session (offline first, then one verify)
1. Regenerate `tables_load.LOAD` from `20260905-vendor-clean-load`
   (the engaging feed with its 47-reg block + the traverse), via
   tools/gen_load_table.py; do NOT hand-edit.
2. Replace the exact-match strict completion polls with the sensor-bit
   test: after the feed, require reg 0x101 bit 0x08 CLEAR (done class);
   fail closed otherwise. This is the composite LOADED-check the review
   asked for, grounded in the loader sensor, not a status-word range.
3. Offline tests, then ONE hardware load from a fresh insert to verify
   the cassette engages (sensor bit clears, magazine latches). Only
   then revisit the 0x02 loaded-idle start state (still parked).

Mechanical note (Christian, Test 14): QuickScan required the magazine
taken FULLY out and reinserted; it latched the moment it hit the stop.
So full insertion to the stop IS correct — the earlier trigger-point
idea was wrong; the missing piece was always the feed's register block.

## 2026-09-05 — Test 15: LOAD with the full register block, no jog — feed done, cassette not engaged

Scanner power-cycled overnight, found cold (0x01=0x00, status 0x4055,
magazine absent). Magazine inserted fresh to the stop, orange LED
(`hw-2026-09-05-load2/doctor-1-magazine-in.json`: 0x4855, sensor set).
`tools/load_magazine.py` with the LOAD table regenerated from the
clean-load capture (commit 80e76c9: feed with the vendor's full
126-register block, masked completion test).

Sequence: cold_init (3 homing rounds, magazine in the slot, no
abnormal sound), base table, armed 0x22; LED went blue with no motor
sound at the feed's register write, then motor sound (the feed). The
feed's completion poll ended **0xfc55**: done class, loader-sensor bit
0x08 still SET (capture: 0xf455, clear). The strict poll stopped the
flow before the traverse, session FAILED, nothing further sent, exit 1
(`load-1.log`). Registers afterwards (`doctor-2-after-failed-feed.json`):
0x01=0x22, 0x35=0xbb, 0x32=0x1d, status 0xfc55. Magazine LOOSE, LED
went out. Power-cycled.

**Conclusion: the register block was not the missing piece.** With the
feed programmed exactly as the vendor programs it, the transport still
ran without taking the cassette (same symptom as Tests 11b/12b/13).
The masked completion test and the fail-closed poll worked as designed.

### What all three engaging vendor loads have in common
Re-reading the captures (eject-from-loaded 2026-09-02 ops 137-193 →
load 292; load-only 2026-08-30 ops 108-169 → load 794; clean-load ops
107-168 → load 199): **every engaging feed is preceded by the vendor's
app-start jog** — feed 6690 (short batch), 0x35=0xbb, feed 6690
(19-register batch), eject 3090, all with the loader profile — and
then either the operator's reinsert (clean-load, load-only) or nothing
(eject-from-loaded, magazine already in: its feed had only the
19-register batch and still engaged, 0xf055). The jog's own feeds
never clear the sensor bit (0xf855 after each). None of our loads ever
ran the jog: cold_init's homing rounds (8730/8730/4620) and the base
table are what preceded our feeds (Test 12: 0xec55; Test 15: 0xfc55).
Working hypothesis: the eject 3090 positions the loader mechanism so
that the next feed catches the cassette.

### Driver change (offline, 89 tests green, NOT hardware-verified)
- `tables_load.JOG` generated from clean-load ops 88-170 (acks, motor
  enable, feed, 0x35=0xbb, feed, eject 3090, motor disable), replayed
  by `Scanner.jog_magazine()` with four strict masked polls (0xf855:
  done, sensor SET, busy clear).
- `initialize(prep=False)`: base table + AFE values only — the vendor's
  device-open state; its load flow never runs the scan preparation.
- `tools/load_magazine.py` now runs the vendor's order: initialize
  (prep=False) → JOG → operator takes the magazine fully out and
  reinserts it to the stop (Enter) → LOAD. reg 0x32 is logged before
  and after the reinsert (clean-load showed 0x1f → 0x5b → 0x1f).

### Next (Test 16): one run of that flow from a power-cycled scanner
with the magazine loose in the slot at the stop. Expect the jog to
move the magazine, 0xf455 after the engaging feed, latched magazine.

## 2026-09-05 — Test 16: JOG on top of BASE_INIT_PAIRS — a harsh noise; aborted at the prompt (stdin)

Scanner still cold from the Test 15 power cycle, magazine loose at the
stop. `tools/load_magazine.py` (commit 1d38cf5): cold_init (three
rounds, normal sound), base table + AFE, then the JOG. Christian: a
rather loud, "nasty" sound that stopped abruptly, unlike QuickScan's
start. The jog's four polls all settled at 0xf855 (as captured), so the
guard let it through. The reinsert prompt then got EOF (the tool was
run through the session's `!` prefix, which gives no stdin): abort,
exit 130, nothing sent after the jog, session armed. Doctor: 0x01=0x22,
status 0xf855. Magazine loose the whole time. Scanner off, magazine out.

### Cause of the noise (offline)
`BASE_INIT_PAIRS` differs from the vendor's app-start table in five
registers: 0x3b/0x3c, 0x4f (0x03 vs 0x63) and the motor speed profile
**0x7e/0x7f = 0x15/0x7c (scan) vs 0x75/0x30 (loader)**. The jog's
first move (capture op 109) is a 6-register batch that takes 0x7e/0x7f
from the table — on top of BASE_INIT_PAIRS it ran 6690 steps at scan
speed. The LOAD feed and the jog's later moves set the profile
explicitly, which is why Test 15 sounded normal. Fix: the load flow now
replays the vendor's device-open sequence verbatim (`tables_load.OPEN`,
clean-load ops 37-88: chip-id ack, app-start table with the loader
profile, acks, AFE bring-up) via `initialize(prep=False)`, instead of
BASE_INIT_PAIRS. The flow is then byte-identical to the vendor's
clean-load session from device open to loaded, minus the operator gap.

### Second finding
cold_init's three "homing rounds" are, on the wire, the same jog as the
vendor's app-start jog (feed 6690, feed 6690, eject 3090; the 8730/4620
in the code are the same FEEDL bytes read the other way round). So Test
15 had three jogs before its feed and still did not engage; the jog
alone is not the explanation. What remains different from Test 14 is
the register context (now fixed) and that in Test 14 the magazine
latched at insertion, before any feed.

Per-move logging added: every strict completion poll logs its settled
value and elapsed time. Test 17 = the full OPEN → JOG → reinsert → LOAD
flow from a power-cycled scanner, run from a real terminal.

## 2026-09-05 — Test 17: OPEN → JOG → reinsert → LOAD — the magazine LATCHED (first driver load that engaged)

Scanner power-cycled, cold (0x4855), magazine loose at the stop.
`tools/load_magazine.py` (commit a679224) from a real terminal, log
`hw-2026-09-05-load2/load-4-open-jog.log`:

- cold_init: three rounds, normal sound. OPEN (vendor device-open
  sequence, loader profile in the table). JOG: four polls settled at
  0xf855, sound normal this time ("lät bra") — the Test 16 noise was
  the scan profile, as analysed.
- Prompt: magazine fully out, reinserted to the stop, Enter. reg 0x32
  read 0x1f before and after.
- LOAD: engaging feed settled **0xf455** (done, loader-sensor bit
  CLEAR — the cassette was pulled past the sensor), traverse **0xdc55**,
  final read 0xdc55. Exit 0.
- Christian: **magazine latched, button blue.**

Doctor afterwards (`doctor-5-after-load-ok.json`, read-only): reg
0x01=0x22, status word 0xdc55, 0x32=0x05, 0x35=0xbb, 0x31=0xfc — the
same values the vendor's clean-load capture shows in its idle loop
after the load (0x0555 / 0xbb55 / 0xdc55). This is the observed
**LOADED_READY signature**: 0x01=0x22 (a normal start state), status
class 0xD with the sensor bit set, 0x32=0x05. The 0x02 loaded-idle
value from Tests 12/15 was an artefact of the unengaged loads; the
parked `wip/loaded-idle-start-state` branch is moot.

What made the difference, in the end: replaying the vendor's session
byte for byte from device open — its own register table (loader motor
profile), the jog, the operator's fresh insert to the stop, the feed
with its full register block — instead of our scan-session base table
followed by a feed cut out of context. Which single element is
necessary was NOT isolated (Test 15 lacked the jog and the OPEN table,
Test 16 lacked the OPEN table); the working flow is the vendor's, kept
whole.

Next: one scan from this state (frame 1, 3600 dpi) and an eject, both
already hardware-verified from a vendor-loaded magazine.

### Test 17, continued: scan and eject from the driver-loaded magazine
- `scan --frame 1 --ir --positive --rotate 90` (new session, start
  state 0x22): gain R=0x2d G=0x21 B=0x27, offsets 0x010a/0x0109/0x010a,
  warmup_attempts=1, poll_timeouts=0, cr_mismatches=15. Image
  5184x5248 visually correct and well exposed (`test17-frame1.tiff`,
  `-ir.tiff`, `.diag.json`, preview `-view.jpg`).
- `eject` (new session, 0x22): "ejected", exit 0. Doctor afterwards:
  0x01=0x22, status 0xe855 (class E, sensor bit set: magazine still in
  the slot), 0x32=0x9f.

Full chain from power-on in one sitting: cold_init → vendor open → jog →
fresh insert → load (latched) → scan → eject.

## 2026-09-05 — Test 18: the load flow repeated from power-on — identical result

Power cycle, magazine loose at the stop, `tools/load_magazine.py` from
a terminal (`hw-2026-09-05-load2/load-5-repeat.log`): cold_init, OPEN,
jog (four polls 0xf855), reinsert, engaging feed **0xf455**, traverse
**0xdc55**, final 0xdc55, exit 0. Doctor (`doctor-8-after-load-2.json`):
0x01=0x22, status 0xdc55, 0x32=0x05 — the same LOADED_READY signature
as Test 17. Two for two from power-on.

### Test 18, continued: batch 1-4 with IR and eject from the driver-loaded magazine
`scan --frames 1-4 --ir --positive --rotate 90 --eject` (start state
0x22; `hw-2026-09-05-load2/scan-2-batch.log`, `test18-f1..4.tiff`,
sidecars). Gain/offset identical for all four frames (0x2d/0x21/0x27,
0x010a/0x0109/0x010a), warmup 1. Frames 1-3 visually correct. Eject at
the end: "ejected", doctor 0x22 / 0xe855 / 0x32=0x9f.

**Frame 4 shows a frame edge across the middle of the image** (top
strip: a different picture; lower part: the building scene). In the
log, the POSITION completion poll for frame 4 (FEEDL 39026) timed out
after 4.9 s with the status still **0xd555 (busy)**, captured 0xf455 —
and the scan started anyway (the poll is not strict). Frames 1-3 had
no such timeout (their moves are shorter: 6746/17506/28266). The
POSITION poll's budget is 3x the captured duration of the frame-1 move
(1.61 s), which a 39026-step move exceeds. Hypothesis: the scan of
frame 4 began while the transport was still moving, hence the shifted
frame. Not yet checked: whether this strip's frame 4 is a whole
picture (Christian), and whether the earlier verified batch (b4,
another strip, no sidecars) had the same timeout. Candidate fix for a
clean session: scale the POSITION completion budget with FEEDL, or
wait for the busy bit to clear before SCAN (strict). Poll timeouts per
frame: 1/3/4/3, the others being the known benign state-class
mismatches (9c vs ad/bd, 8155 vs 9555 on reg 0x32).

**Vendor reference settles frame 4** (`batch-test/vendor/ref-20260905-f-0001..0005.tif`,
QuickScan on the same strip, 5 slots scanned, slot 5 empty): the
vendor's frame 4 is a whole picture (lion statue, street towards Big
Ben). Our frame 4 holds mostly frame 3's scene with only the top of
frame 4 — the transport had not reached the frame-4 position when the
scan began. **Positioning, not film.** Consistent with the POSITION
poll timing out busy (0xd555) after 4.9 s on the 39026-step move.
Fix for a clean session (offline first): the POSITION completion wait
must not be a fixed 3x of the frame-1 move; wait for the done class /
busy bit clear with a budget that scales with FEEDL, and treat a
timeout there as a failure (no scan on a moving transport). Also
visible in the comparison: a pink/magenta cast in our --positive
output against the vendor's neutral rendering — separate topic
(image processing), not part of this pass.

**Fix implemented (offline, 90 tests green, awaiting the hardware batch):**
the POSITION completion poll is now strict on the state class
(`POSITION_STATUS_MASK` 0xf0: 0xf455/0xf055 pass, 0xd555 fails) with a
budget scaled by FEEDL relative to the captured frame-1 move
(`position_timeout_scale`: frame 4 at 3600 dpi = 5.8x, ~28 s). A
transport that has not settled fails the scan before SCAN sends
anything (session FAILED, power cycle), instead of scanning a moving
frame. Verification: Test 19 = batch 1-4 from a fresh driver load,
compared frame by frame against `batch-test/vendor/ref-20260905-f-*`.

## 2026-09-05 — Test 19: batch 1-4 with the scaled POSITION wait — all four frames match the vendor reference

Power cycle, driver load (`load-6.log`: f455 / dc55, latched), then
`scan --frames 1-4 --ir --positive --rotate 90 --eject`
(`scan-3-batch.log`, `test19-f1..4.tiff`, `test19-vs-vendor.jpg`).
POSITION completion times: frame 1 settled within the paced 1.6 s,
frame 2 after 2.0 s, frame 3 after 4.2 s, **frame 4 after 6.3 s** —
frames 3 and 4 beyond the old fixed 4.9 s budget. All four frames
match `batch-test/vendor/ref-20260905-f-0001..0004` frame for frame;
frame 4 is the lion-statue street scene, whole. Gain 0x2c-2d/0x21/0x27,
warmup 1, eject fine, doctor 0x22 / 0xe855 / 0x32=0x9f afterwards.

Note: the polls for frames 2-4 settled at **0xf555** (class F, bit 0x01
still set) and were accepted under mask 0xf0; the images are correct,
so the class transition is a sufficient completion signal here. The
captured value is 0xf455; tightening to bit 0x01 clear (mask 0xf1)
would be an unverified change on top of a verified one — left as is,
noted for a future A/B. Remaining poll timeouts (22) are the known
benign state-class mismatches (9c vs ad/bd, 8155 vs 9555, e8/ec vs
f8/fc at session start).

## 2026-09-05 — Test 20: raw batch of the strip; interrupt-endpoint overflow explained

Power cycle → `doctor` BEFORE any load (`doctor-11-...json`): EP 0x83
healthy, one pending sensor event (0x04) read normally — **a power
cycle clears the overflow state.** Driver load #4 (`load-7.log`: f455 /
dc55, latched) with the tool now draining EP 0x83 after the jog, the
reinsert and the load (all three: no events, no overflow). `doctor`
after the load and again after the eject: button reads normally.
**The overflow state is caused by our not reading the interrupt
endpoint during the load; draining it as the vendor does prevents it.**

Raw batch (`scan --frames 1-4 --ir --eject`, no --positive; `raw20-f1..4.tiff`
+ IR + sidecars; positioning 2.0/4.2/6.3 s again, gain 0x2d/0x21/0x27):

| frame | p0.1 (R G B) | p50 | p99.9 | clipped |
|---|---|---|---|---|
| 1 | 1379 1541 1133 | 13375 9246 6926 | 40801 28807 18499 | 0 % |
| 2 | 1373 1614 1120 | 10465 7374 5395 | 42939 30237 19358 | 0 % |
| 3 | 1379 1549 1130 | 10241 6837 4725 | 20818 13563 8607 | 0 % |
| 4 | 1378 1553 1131 | 9016 6322 4716 | 34217 23139 13445 | 0 % |

(central 80 % of each frame, 16-bit linear.) No pixel at 0 or 65535
in any channel; the black floor (~1100-1600, from the AFE offset) is
identical across frames; the red channel carries the orange mask as
expected. The raw negative is what the driver promises: linear,
unclipped, reproducible.

Preview (`to_positive`, per-frame density inversion, gamma 2.2) against
the vendor references (`raw20-preview-vs-vendor.jpg`): all four frames
neutral (R/G 0.96, vendor 0.85-0.94), correctly oriented, composition
identical; ours flatter and less saturated than the vendor's rendering
— acceptable for a preview, and colour work starts from the raw file.


## 2026-09-05 — A10 revision: parameter classification after the load, positioning and colour work

Revision of the Test 3 table (2026-09-03) in the light of Tests 12-20.
Single unit, single host, as before.

| Parameter / mechanism | Then | Now | Why |
|---|---|---|---|
| AFE offset codes | 🔴 `_OFFSET_DEFAULT` placeholder | 🟡 middle stage dynamic, first and final stages replayed | The vendor writes offsets in three stages (bracket 1 result in cal_white, bracket 2 result in cal_shading_measure, small final codes at the end of cal_shading_measure / in cal_shading_verify). Only the middle stage is computed (`offset_codes`, stable across 13 frames on 09-05: 0x010a/0x0109-0x010a/0x010a); the first and final stages are captured constants. The formula's margins were fitted to the plain 3600 capture, which it reproduces within one code; in dual mode the vendor's own captures sit 5-12 codes higher, and the vendor recomputes the final small codes per pass (±2 codes). Images unaffected (black floor ~1400 counts, no clipping), but unit- and session-dependent by nature. |
| Cold-start regs 0x4f/0x3b/0x3c | 🔴 cause unknown | 🟢 explained | 0x4f=0x63 and 0x3b/0x3c=0x00 are the vendor's device-open table (loader context); 0x03/0xff/0xff are the scan-session base table (dpi-dependent 0x3b/0x3c). `tables_load.OPEN` carries the former verbatim. |
| Cold homing FEEDL (were "8730/4620") | 🟡 unit-dependent travel | 🟢 vendor constants | They are 0x1a22/0x0c12 = feed 6690 / eject 3090, the same jog the vendor runs at every app start on every unit; no adaptive homing exists on the vendor side either. Byte-identical in all captures. |
| Magazine load flow (OPEN, JOG, LOAD tables) | 🔴 (unengaged loads, Tests 11b-16) | 🟢 as a whole; 🟡 as parts | Byte-identical to the vendor's clean-load session; 4/4 latched. Which element is necessary is NOT isolated — do not vary parts without A/B. |
| Load completion masks (0xfb: class + sensor bit + busy) | — | 🟡 | Bit 0x04 varies between sessions (d8/dc, f0/f4) on this unit; the mask is derived from two captures and four runs, not from a bit-level spec. |
| POSITION completion (mask 0xf0, budget × FEEDL/FEEDL_frame1) | 🟡 fixed 3× frame-1 budget (failed at frame 4) | 🟢 | Move time is linear in FEEDL on hardware (2.0/4.2/6.3 s for 17506/28266/39026); budget now 5.8× at frame 4. Polls settle at f5 before f4; class transition proved sufficient (frames correct 8/8). Mask 0xf1 is a possible tightening, untested. |
| Interrupt endpoint handling | — | 🟢 | Overflow explained (unread events during the load) and prevented by draining as the vendor does; power cycle clears it. |
| Colour LUT (`negative-color-lut.npy`) | 🟢 (as tone curve) | removed | Fitted to one frame of one film; 🔴 for any other film. Replaced by per-frame density inversion (preview only). |
| Gain target 31673 | 🟡 | 🟡 | Unchanged: fitted with 2-4 % residual on one unit. Gain codes reproduced 0x2c-2d/0x21/0x27 across 13 frames on 09-05 (stable), but the target itself is still one-unit. |
| AFE_BASE_PAIRS / EEPROM | 🟡 | 🟡 | EEPROM bytes (c84013 / ff…) still read and logged, not decoded; whether the vendor derives AFE values from them is unknown. |
| Lamp warmup budget (3 × 5 s) | 🔴 open (Test 11: not enough after a bare cold start) | 🔴 open, lower urgency | With the load flow (cold_init + open + jog + load ≈ 60 s of lamp-on time) the first scan never needed a retry on 09-05. A bare cold-start scan without a load still has no measured warmup time. |
| Poll leniency: benign mismatches (9c vs ad/bd, 8155 vs 9555, e8/ec vs f8/fc) | 🟡 unexplained | 🟡 | Same values every session; no effect on output. Not understood, not harmful. |
| Cross-unit / cross-host | 🔴 | 🔴 (permanent for cross-unit) | One unit, one host, one USB controller. A second unit is not available to this project and will not be; cross-unit verification can only come from other people's units (SANE users, a vendor with a fleet). Mitigation: everything that can be measured on the device at run time is (gain, offset, shading, load/position completion by state, not by fixed values), and every one-unit constant is labelled as such. Another USB port and another Linux host (B4/B5) ARE feasible and still open. |

Safe to test automatically next: hwblock warm from a driver-loaded
magazine (repeatability of gain/offset/levels), semantic PARK A/B.
Not to be varied: anything in the OPEN/JOG/LOAD tables.

## 2026-09-05 — P0 review of the verified main flow (offline)

External review asked for a regression-protection pass over
`power-on → driver load (OPEN/JOG/LOAD) → batch 1-4 → eject`. Findings:

- Already covered by the suite: byte identity of OPEN/JOG/LOAD against the
  clean capture; the masked completion test with 0xec55/0xf855/0xf555/
  0xcc55/0xd455 rejected; stop after a failed feed (no traverse); freeze
  after a failed traverse; POSITION timeout before SCAN; Ctrl-C, EOF, USB
  errors, short OUT transfers and timeouts failing the session with zero
  recovery commands; doctor/status strictly read-only; the process lock
  covering read-only sessions; the load tool's order (open, jog, prompt,
  load).
- Gap 1 (fixed, adce3d6): a non-timeout, non-overflow USB error on the
  interrupt endpoint surfaced as an unhandled pyusb exception in status
  and in the load tool's drain step. Now `InterruptReadError`
  (Of135iError): uniform error path, nothing hidden, nothing retried.
- Gap 2 (fixed, e21ce67): the POSITION completion rule was only tested
  at 3600 dpi. Now tested for 3600 plain and the 600/1200/2400/7200
  dual-light profiles at frame 4: budget scaled from each profile's own
  frame-1 FEEDL, class F accepted, busy (0xd5) refused before SCAN's GO
  with the session frozen.
- Not changed, deliberately: Ctrl-C/EOF at the reinsert prompt leaves the
  session ARMED (the jog is complete, nothing is in progress) while the
  tool still demands a power cycle; marking that state FAILED would
  misdescribe it.

Offline suite after P0: test_safety 46, test_hwblock 14, test_calibrate
10, test_offline 6, test_park 6, test_diag 4, test_dpi 3, test_ir 3 —
**92 tests, all passing**, at e21ce67.

P1 preparation (hwblock reviewed against the current API): every scan
now keeps visible + IR TIFFs and its sidecar, the report records the
checkout's SHA and dirty flag, the precheck text describes the driver
load flow. No gate loosened; prechecks (start-state verdict via the
driver, loader sensor before initialize, --assume-locked only as a
documented override) unchanged.

## 2026-09-05 — Test 21: hwblock warm, 10× reproducibility + batch + eject from a driver-loaded magazine (P1)

Power cycle → driver load #5 (`load-8.log`: f455/dc55, latched, blue LED)
→ `tools/hwblock.py warm --repeat 10 --skip-dpi-change --eject` at
493ebb8 (`hwblock-20260905-warm/`, 18 min, status COMPLETED, findings:
none). A first attempt on the cold, unloaded scanner was refused at W0
(read-only) as designed.

**W1/W4, ten scans of frame 1 at 3600 dpi dual-light:**

| metric | result |
|---|---|
| film start row | 1856-1860 (spread 4 rows = 0.03 mm) |
| film end row | 5130-5134 |
| gain codes | 0x2d / 0x21 / 0x27 on all ten |
| offset codes | R 0x010a, G 0x0109-0x010a, B 0x010a-0x010b (vendor ref 0x010b/0x010a/0x010b) |
| channel mean drift first→last | +1.6 % R, +2.3 % G, +2.1 % B (monotonic; lamp/temperature) |
| pair RMS (8-bit) between consecutive scans | 0.22-0.45, except 2.67 and 2.17 at the two scans where the start row shifted by 3-4 rows |
| warmup retries | none; gain never clipped |
| time per frame | 63-67 s (scan 40.6 s, PARK 13.6 s, POSITION 1.8 s) |

**W5, batch 1-4:** start rows 1856 / 2159 / 6 / 0 — frames 1-4 correct
(visually: portrait, square, Trafalgar, lion; same as Test 19).
POSITION 1.8 / 3.8 / 6.0 / 8.1 s. **W7:** eject OK; doctor afterwards
0x22 / 0xe855. Session record: 13 196 writes, 127 execute pulses, no
failure. Remaining poll timeouts per scan 1-5 (the known benign
state-class mismatches), cr mismatches 7-33.

Verdict: the calibration and the geometry are reproducible to one code
and four rows over ten consecutive scans; the slow monotonic brightness
drift (~2 % over 11 minutes) is the one thing to keep an eye on (lamp
warming — consistent with the cold-start warmup topic, P2).

## 2026-09-05 — P2 (offline): lamp warmup is now bounded, measurement-based and fail-closed

Audit of `_gain_with_warmup` (was: up to 3 retries × 5 s, then
**proceed with maxed gain** — a flat image, as Test 11 showed after a
bare cold start). Findings and changes (96 offline tests green):

- "Lamp not ready" = all three gain codes at 0x3f on a measurement.
  An all-zero white line already mapped to that (clamp_nonpositive).
- New rule: a warm first measurement returns at once (the single-
  measurement path of every verified scan). Otherwise re-measure every
  5 s; accept only two consecutive non-maxed measurements whose peaks
  agree within 3 %. Budget `Scanner.warmup_budget_s` (default 60 s,
  `scan --warmup-budget`, `hwblock cold --warmup-budget`), enforced by
  the clock AND a measurement cap (13 at the default), so a stopped
  clock cannot extend it.
- Fail-closed: budget exhausted, saturated white (65535 at gain 0) or
  malformed buffer → `LampWarmupError` (SafetyError): no scan, no motor
  command, session FAILED, power cycle. USB errors, timeouts and Ctrl-C
  propagate untouched. Nothing is retried.
- Sidecar: `warmup_peak_history` and `warmup_measurement_times_s` —
  a scan from bare power-on with a generous budget IS the warmup
  probe; no separate tool.
- Tests: warm lamp (1 run, no sleep), gradual warmup (accepted on the
  second stable measurement), dark lamp (fails at the cap), smaller
  per-scanner budget, never-stable lamp, saturation, malformed data,
  Ctrl-C and USB errors. The fake device now models a lit lamp for
  unscripted reads and repeats the last scripted calibration buffer
  across batch frames (zeros modelled a dark lamp, which the driver now
  refuses).

Not run on hardware. Planned single test (P2, needs approval): bare
power-on, magazine in, `hwblock cold --warmup-budget 300` — the
cold-start scan records the warmup curve; the second scan in the same
session checks the lamp is then warm. Only after that is a real budget
chosen.

## 2026-09-05 — P3 (offline): semantic PARK reviewed; waits now fail closed

Review of `park_semantic` against the P3 list: real read-modify-write
on 0x15/0x32/0x35 (tested against scripted live values); explicit,
bounded waits (15 s each); runs inside the "park" operation with the
same session guard and bookkeeping as every other write path; the
0x8b payloads and the 0x19 write taken from each table's own PARK
(pair-for-pair equivalence proven against all six tables).

One gap, fixed: both waits used to log a timeout and CONTINUE. Wait A
follows the carriage-return write (`0x02=0x30`); a transport that has
not reported home must not be handed to the next frame's absolute
POSITION move — the frame-4 lesson of Test 18. Now a timeout records
the wait in the diagnostics and raises `StrictPollTimeoutError` inside
the park operation: nothing further is written (no RMW clear, no
heartbeat), session FAILED, power cycle. Test rewritten (Wait A and
Wait B cases, writes after the timeout asserted absent). `--park
semantic` stays off by default.

Not verified on hardware. Proposed A/B (needs approval): driver load,
then `hwblock warm --repeat 3 --skip-dpi-change --park semantic` against
Test 21's verbatim data — same magazine, film, frame 1 and start state:
compare gain/offset codes, film start row, park_waits (a/b seconds, no
timeout), phase seconds, the doctor register dump after the block, and
a following batch/eject. Not to become the default after one run.

Offline suite at this point: 96 tests, all passing.

## 2026-09-05 — Test 22: bare cold-start scan — the white line is DARK, not warming; a cold session must load first

Load #6 attempt (`load-9.log`) failed at the feed (fc55, sensor bit still
set) — operator error at the reinsert prompt (magazine not taken out);
the guard stopped before the traverse, power cycle. Then load #6 proper
(latched), power OFF with the magazine in, power ON: the Test 11
situation. `hwblock cold --warmup-budget 300 --eject` (5274963+, at
fceafb0): cold_init clean (three rounds, normal sound), base table,
PREP, calibration — and the first white line maxed the gain. The
bounded warmup loop then took **51 measurements over 295 s**:

per-channel white peaks (99.9th pct, of 65535), first / middle / last:
11.0 58.8 41.0 / 18.5 63.6 52.8 / 24.0 63.0 53.6 — every one of the 51
between 10 and 27 (R), 59 and 75 (G), 41 and 57 (B). **Flat. Not
rising. Dark.** LampWarmupError, no scan, no motor command, session
FAILED, block stopped at C3 (`hwblock-20260905-cold/`). Eject afterwards
from the power-cycled cold scanner: cold_init + eject, fine.

Reading: this is not a lamp warming up (a warming lamp rises; Test 21's
warm lamp drifted +2 % in 11 minutes, here nothing moved in 5). The
lamp register 0x03 is written 0x30 in PREP on this path exactly as on
the working path. What differs is where the transport stands: every
working scan ran from the position the load's 71490-step traverse
leaves the transport in (the frame FEEDLs are absolute from it, and a
re-load is what resets the DPI drift). After cold_init alone the
transport is at the jog's home, and the white-line read sees no light
at the sensor. The vendor never scans after a power-on without loading
(app-start jog, reinsert, load). Tests 1/9/11's "flat cold-start
images" had this cause; the "lamp warmup" reading was wrong.

Decisions (offline, 98 tests green):
- `scan()` refuses (`OperationNotAllowedError`, nothing sent) in a
  session that ran cold_init until `load_magazine()` has completed in
  it. Warm sessions unaffected.
- `hwblock cold` retired (exits 2, touches nothing); cold-start
  verification = power-cycle → load tool → warm block, i.e. Tests 17-21.
- The bounded warmup wait stays as the guard that produced this result
  instead of a flat image; the budget question is closed (never
  triggered in a load-first session).

## 2026-09-05 — Test 23: semantic PARK A/B — Wait B's condition is not a completion signal

Power cycle, driver load #7 (`load-11.log`: f455/dc55, latched), then
`hwblock warm --repeat 3 --skip-dpi-change --eject --park semantic`
(`hwblock-20260905-semantic/`, at 8007dcd). Scan 1 of frame 1 ran
normally: gain 0x2c/0x20/0x27, offsets 0x010a/0x0109/0x010a, POSITION
settled f455. Semantic PARK: Wait A (reg 0x35 bit 0x40 after the
carriage-return write) completed; **Wait B (reg 0x32 → 0x95 masked
0x18) timed out after 15 s with 0x32 = 0xb5** (bit 0x20 set where the
capture had it clear). The fail-closed rule stopped the park there:
StrictPollTimeoutError, nothing further written, session FAILED, block
FAILED at W1, power cycle, cold eject (fine). No abnormal sound.

Reading: the verbatim PARK never reaches 0x95 either — Test 21's
sidecars show the same poll as one of the benign timeouts ("8155 vs
9555", log-and-continue after 1 s) on every scan. Wait B's target was a
captured value that varies between sessions (0x81, 0x95, 0xb5 seen), so
it was never a completion signal; the verbatim path masked that by
tolerating the timeout, the semantic path exposed it by being strict.
Wait A is sound so far (1/1). What the park should wait for after the
carriage-return write is the status word settling in the idle class
with the busy bit clear (a1 → a9 → e8 in a private capture of the vendor
driver's own returns, e855 after every verbatim park on our hardware) —
an offline change and a new A/B. `--park semantic` stays off; the
fail-closed rule stays.

Loads today: 7/7 latched from power-on (one operator-error attempt in
between, stopped by the guard). Offline suite: 98 tests.


## 2026-09-05 — Semantic PARK Wait B: evidence analysis (offline)

`docs/park-completion-analysis.md`. From the six captured PARK phases,
the raw traces with timing, and the scan logs/sidecars of Tests 18-21:
every busy status during the return (d1, d5, a1, a5, a9, 81) has bit
0x01 set; every idle status after it (f8, e8, ec, and the loaded-idle
d8/dc) has bit 0x01 clear and bit 0x40 set. Bits 0x20/0x10/0x08/0x04
vary between sessions for the same physical idle state; bit 0x02 was
never seen set. Register 0x32's captured 0x95 is one of several
session/dpi-dependent values (81 → 85/95/8d/15, b5 on hardware); the
vendor app's own loop watches its bit 0x04 transition, not 0x95. Rule
derived: status word idle = (byte & 0xc3) == 0xc0 with a valid 2-byte
0x55-ack reply; Wait B budget 30 s. Not hardware-verified.

## 2026-09-05 — Semantic PARK Wait B: implementation and simulated tests (offline)

Clearly separated:

**Earlier hardware observations (real):** Tests 18-21 verbatim parks
(post-park status e8/ec on our unit, park phase 13.6-15.6 s, next frames
correct); Test 23 semantic park (Wait A completed, Wait B stopped on
0x32 = 0xb5, fail-closed, nothing written after).

**Today's offline analysis:** `docs/park-completion-analysis.md` — the
rule (status word idle: (byte & 0xc3) == 0xc0 on a valid 2-byte 0x55-ack
reply) derived from six captured PARK phases, the raw traces with timing
and the Tests 18-21 logs.

**Today's simulated tests (fake transport, scripted status replies,
controllable clock; `tests/test_park.py`, 12 tests, all passing):**
predicate truth table (every observed idle and busy value, cold values,
malformed replies, and that the mask is neither LOAD's nor POSITION's);
observed sequences complete (a1→a9→e8, immediate idle, repeated a1
before a9, the captured d1→f8 and 81→e8, the hardware variants ec/dc/f0)
with the idle round written after and no timeout recorded; stuck a1,
stuck a9, wrong class 9c, busy bit on an idle-looking value (e9), the
undocumented bit 0x02 (ea) and a cold 48 all fail closed with
StrictPollTimeoutError, operation park, session FAILED, no write after
the last status read, later writing operations refused; the total
budget ends after a bounded number of reads; short, empty, too-long,
wrong-ack and swapped replies are never accepted (and a malformed reply
followed by a real idle one completes); a USB timeout, another USB
error and KeyboardInterrupt during the wait propagate untouched, fail
the session and freeze it; the new Wait B completes for all six table
variants with their 0x8b payloads untouched; Wait A still fails closed on
its own before any status read; park_mode still defaults to verbatim;
the verbatim tables are byte-identical (A/B equivalence tests unchanged);
RMW still reads live values. The wait record (`park_waits`) now survives
every exit path for the sidecar.

**Still without hardware evidence:** a complete semantic PARK on the
scanner with the new rule; whether Wait B's 30 s budget is right in
practice; the status word's exact progression on our unit during the
return (the vendor captures read it sparsely; our verbatim parks never
read it). `--park semantic` remains off by default and must not be
described as hardware-verified.

Offline suite after this work: test_safety 47, test_hwblock 15,
test_calibrate 14, test_park 12, test_offline 6, test_diag 4, test_dpi 3,
test_ir 3 — **104 tests, all passing**.

## 2026-09-05 — Semantic PARK Wait B tightened to the observed park ends (offline)

Review finding, accepted: the first rule ((byte & 0xc3) == 0xc0) mixed
phases — it accepted class D (d8/dc, observed only after a LOAD) and
class C (c8/cc, never observed after a park) as "idle". PARK-specific
evidence only (docs/park-completion-analysis.md §2-3): every observed
successful park ends in e8, ec or f8 — class E/F, bit 0x20 set in all,
0x10 and 0x04 varying within that evidence, 0x08 the loader sensor
(ignored on its documented meaning). Is there evidence for a park
ending in class C or D? **No.** Rule now `park_complete_status_matches`:
(byte & 0xe3) == 0xe0 on a 2-byte 0x55-ack reply; d8, dc, c8, cc
rejected explicitly, a1 → a9 → e8, d1 → f8 and 81 → e8 accepted, the
busy/scanning/malformed/USB-error/Ctrl-C cases as before. A hardware
park ending in class D would now stop fail-closed as an unobserved
state. Offline-verified only; Test 23 verified safe rejection, not a
full semantic PARK; verbatim PARK remains the default.

## 2026-09-05 — P4 (offline): the CLI workflow productised

- `of135i load`: the magazine load flow is now a driver module
  (`of135i/loadflow.py`) shared by the CLI subcommand and
  `tools/load_magazine.py`; same order, same checks, interactive
  reinsert prompt (real terminal).
- `of135i version`: driver version (0.1.0, aligned with pyproject) and
  git revision without touching USB.
- `tools/release_check.py`: runs every offline test file, requires a
  clean checkout, prints version, revision and per-file counts.
- README: install with the udev rule first (no sudo anywhere else), the
  normal workflow load → check latch → scan → eject, a table of what
  the driver knows about the magazine (present / load completed /
  latched / start state), what refusals and failures mean and the exit
  codes (0/1/2/130), the retired cold block.
- Tests: `of135i load` order and exit codes on the fake, `version`
  touches no USB. Offline suite: 106 tests, all passing.


## 2026-09-05 — Calibration cross-check (offline) and A10 correction

Comparing the AFE codes across the QuickScan captures (plain 2026-08-30,
dual 2026-09-02), a second vendor-driver session captured today
(private) and our 13 frames today: gain codes agree within ±2 across
all of them (2c-2e / 20-23 / 27-29). Offsets are a three-stage vendor
sequence of which our driver computes only the middle stage; its
margins were fitted to the plain capture and land 5-12 codes below the
vendor's dual-mode values, and the final small codes are replayed from
the capture while the vendor recomputes them per pass (±2). A10's
offset row corrected from green to yellow accordingly. No code change;
images are unaffected (Test 20: no clipping, black floor identical
across frames).


## 2026-09-06 — Test 24: B5 (laptop host) — load and batch 1-4 reproduce mintuu, but dark_b collapses on even frames

Host B5: Fedora 44, kernel 7.1.6-201.fc44.x86_64, Intel i5-6300U, single
Intel Sunrise Point-LP xHCI (00:14.0), python 3.14.6, pyusb 1.3.1,
driver c1604de. The repo was fast-forwarded fceafb0 → c1604de before the
run (the checkout was 15 commits behind, including "a cold-started
session must load before it scans"); `tools/release_check.py` 106/106
green on a clean checkout.

Power on with the magazine out → magazine loose in the slot →
`of135i load` (cold path: reg 0x01 = 0x00, so cold_init, then the jog,
the operator's reinsert to the stop, and the load) → latched, blue LED,
drag test held → `of135i scan --frames 1-4 --ir --positive --rotate 90
--eject`. Loaded-idle before the batch: 0x01=0x22, status word 0xdc55,
0x32=0x05, 0x101=0xdc. After eject: 0x01=0x22, 0x32=0xdb, 0x101=0xf0,
magazine not detected.

**Batch 1-4, 3600 dpi dual-light:**

| metric | B5 | mintuu (Test 21) |
|---|---|---|
| gain codes | 0x2d / 0x21 / 0x27 on all four | 0x2d / 0x21 / 0x27 |
| offset codes | f1, f3: 0x010a / 0x0109 / 0x010a — f2: 0x0105 / 0x010d / 0x0109 — f4: 0x0105 / 0x010c / 0x0108 | R 0x010a, G 0x0109-0x010a, B 0x010a-0x010b |
| warmup attempts | 1 on all four, never exhausted | none needed |
| image dimensions | 5184x5248 on all four | 5184x5248 |
| POSITION | 1.66 / 3.67 / 5.83 / 7.99 s | 1.8 / 3.8 / 6.0 / 8.1 s |
| scan pass | 39.56 / 39.59 / 39.57 / 39.57 s | 40.6 s |
| PARK | 13.20 / 14.21 / 14.42 / 15.20 s | 13.6 s |
| poll timeouts | 1 / 5 / 4 / 6 | 1-5 per scan |
| cr mismatches | 26 / 49 / 42 / 51 | 7-33 |
| session record | writes 953/1894/2835/3776 cumulative, attempted = completed throughout; execute pulses 9/18/27/36; failure none, refusal none | 13 196 writes, 127 pulses, no failure |

Timing, gain and geometry reproduce mintuu: the POSITION ladder has the
same shape about 0.1-0.15 s faster per step, the scan pass is ~1 s
faster and flat to 0.03 s across four frames, the dimensions are exact.
PARK rises monotonically 13.20 → 15.20 s, ending 1.6 s above the mintuu
figure.

**The finding: the dark_b measurement collapses on even frames.**
[Corrected 2026-09-06, see Test 32: "even frames" is wrong as a rule. The
mechanism is now proven — dark_b on an affected frame is residual data
(that frame's own dark_a tail, repeated), not a measurement. Which frames
are affected VARIES between runs (this batch: f2/f4; the Test 32 batch:
f2/f3/f4). The only constant is that frame 1 is always healthy. Read the
"even frames" wording throughout this entry as "affected frames in this
particular run".]

    f1  dark_b_mean = [23898.75390625, 26731.1650390625, 25680.232421875]
    f2  dark_b_mean = [26177.375, 26177.375, 26177.375]
    f3  dark_b_mean = [23942.240234375, 26745.5048828125, 25722.7666015625]
    f4  dark_b_mean = [26194.375, 26194.375, 26194.375]

`dark_a` is per-channel on all four frames. Only `dark_b`, only the even
frames, and the three channel means are bit-identical. `device.py`
computes it as a plain `mean(axis=0)` over
`np.frombuffer(dark_b_raw, "<u2").reshape(-1, 3)`; no path in that mean
computation can turn a genuine per-channel measurement into three equal
means. [Corrected 2026-09-06, see Test 29: the earlier wording here —
"the dark_b read returned degenerate data rather than a measurement" —
overstated the evidence. The raw dark_b buffer is never retained (only
its per-channel mean is), so whether the buffer itself was degenerate,
and why, is NOT established from a mean of three equal values.]
`calibrate.offset_codes(dark_a, dark_b)` consumes it, which is why f2
and f4 are the frames whose offsets deviate — R five codes low on both,
against a reproducibility band of ±1.

The same odd/even signature appears independently in two other places:
the shading per-channel offsets (f1/f3 channel means ~130 / 341 / 339;
f2/f4 ~374 / 212 / 388 and ~380 / 243 / 425) and cr_mismatches, whose
two high values are exactly f2 and f4.

The mintuu baseline (`hwblock-20260905-warm/batch-frame-*`,
`hw-2026-09-05-load2/test18-f*`, `test19-f*`) has `dark_b_mean`
per-channel on every frame, even ones included — test18 f2
24016/26747/25790, f4 23850/26564/25648; hwblock f2 23705/26415/25555,
f4 23722/26388/25550 — and `offset_codes` 0x010a/0x0109/0x010a
throughout, R = 266 on all eight frames with no drift.

So the collapse and the R drift are seen only on B5 in this data. That
rules out a *deterministic* driver bug (one would show on mintuu's even
frames too) — but NOT a timing- or host-dependent driver bug that only
trips on B5's USB/xHCI behaviour. [Corrected 2026-09-06, see Test 29: the
earlier wording called this "a platform-dependent difference, not a
latent driver bug", which overstated it — a host-dependent *outcome* does
not exclude a driver bug, and the raw buffers needed to tell them apart
were not retained.]

**Not measurable from this run:** the film start row (1856-1860 for
frame 1 on mintuu). The outputs were written with `--positive`, and
`to_positive()` is a per-frame percentile/log-domain inversion that
cannot be inverted exactly; the `--rotate 90` alone would have been
reversible. Confirming the geometry to ±4 rows needs one frame scanned
without `--positive`.

**Poll pairs outside the documented benign set:** `cc55` vs `ad55` and
`ec55` vs `dc55` occurred on frames 2-4. The documented benign set is
9c vs ad/bd, 8155 vs 9555, and e8/ec vs f8/fc at session start; `9c55`
vs `bd55` and `8155` vs `9555` matched it, these two did not. They
should be verified rather than assumed benign.

Verdict: B5 reproduces mintuu on timing, gain and geometry within the
reproducibility bands. The dark_b collapse on even frames is a B5-only
*outcome* in this data that propagates into the AFE offsets; whether its
cause is host-specific or a timing-dependent driver bug is unresolved
(Test 29 — the raw buffers were not retained). The images themselves were
not assessed for visible impact.


## 2026-09-06 — Test 25: load from a magazine at the stop, and a same-strip geometry comparison across hosts

Two questions, both settled. (a) Does the load flow tolerate the
magazine already inserted to the mechanical stop at power-on, rather than
loose? (b) Is the ~16-row film-start offset seen on B5 (Test 24 left it
unmeasurable) a host difference or an artefact of a different film strip?

**(a) Load from a magazine at the stop — tolerated.**
Cold scanner (reg 0x01 = 0x00), magazine pushed all the way to the stop
before power-on. The presence sensor cannot tell "at the stop" from
"loose": both give the identical register picture (0x31=0xfe, 0x32=0xc6,
0x35=0x00, 0x101=0x48), confirmed against this session's earlier loose
reading. `of135i load` runs the cold path unchanged: cold_init (three
homing rounds → 0x01=0x22), then the jog. The pre-run analysis flagged
the jog as the risk — its feeds might grip and drag a cassette that has
no slack. They did not: four completion polls, all f855 exact, sensor
bit still set, over two independent power cycles. The first attempt
FAILED at the load feed (fc55, want f455) — but that was operator error
(Enter pressed before the magazine was taken fully out and reinserted),
confirmed by the operator, and is not a result about the start position.
The clean rerun, with the reinsert done deliberately, completed with
f455 then dc55 both exact. So the start position is fine for cold_init,
jog and load. Caveat: the flow still converges on the verified state at
the Enter prompt, because the operator takes the magazine fully out and
reinserts it to the stop regardless — what is verified is that cold_init
and jog tolerate the stop position, not that the reinsert step can be
skipped.

**The reinsert prompt has no machine verification.**
`reg 0x32` reads 0x1f before and after the reinsert, and the interrupt
event list is empty — identical in both the failed and the successful
run. The driver cannot tell whether the operator actually took the
magazine out and back to the stop; it is a pure trust step, which is why
a premature Enter yields a silent fc55 rather than a legible refusal.
This is a property of the flow, not of the host, and it mirrors the
sensor's blindness to insertion depth.

**(b) Same-strip geometry, both hosts.**
The vendor reference strip in batch-test/ (2026-09-01) turned out to be a
*different* physical strip (picnic scenes, not the boy-portrait / Big Ben
strip in the magazine), and Test 21 saved no images, so B5's film-start
number could not be checked against the same strip from the earlier data.
Resolved by scanning the *same* strip on both hosts, `scan --frame 1
--ir` (no --positive, so film_rows is measurable), film_rows verbatim
(green row-means, 21-wide edge-padded moving average, threshold
(p5+p95)/2):

| same physical strip | film_start | film_end | length |
|---|---|---|---|
| B5 (Lenovo laptop) | 1842 | 5106 | 3264 |
| mintuu | 1858 | 5114 | 3256 |
| Test 21 (different strip) | 1856-1860 | 5130-5134 | 3274 |

mintuu landed at 1858 — inside Test 21's 1856-1860 band —
even though Test 21 was a different strip. So film_start_row is
host/positioning-determined, not strip-determined (two different strips
give the same start row on the same host). That makes the ~16-row earlier
start on B5, on the identical strip, a genuine host difference, not the
strip confound the earlier data could not rule out. It is consistent with
B5's other signature: B5 ran the POSITION ladder 0.1-0.15 s and the scan
pass ~1 s faster (Test 24) and now also positions the film start ~16 rows
earlier — the same marginally-faster motor/USB profile on that host.
Caveat: one load cycle per host, so load-to-load variation is not
formally excluded; mintuu giving 1858 on two different strips
makes chance unlikely. The algorithm is bit-depth insensitive (Pillow
8-bit and 16-bit memmap gave identical rows), so the reading method is
not a confound.

**Sharpening of Test 24.**
The standalone frame 1 on both hosts is clean on everything the batch
even frames were not: dark_b per-channel (mintuu
[24129, 26873, 25859]), offsets 0x010a/0x0109/0x010a, cr_mismatches 9 (B5
19) — both inside the reference band. So Test 24's dark_b collapse is
"even frames within a batch", not "B5 is broken": a lone frame 1 is clean
every time it has been run, on either host.

**Still open, unchanged:** the cold_init settle poll reads 0x32 = 0x1d
instead of 0x1f, 6 of 6 homing rounds over two power cycles on B5 —
systematic and reproducible, but no mintuu settle-poll log was
saved to compare against, so it is noted, not concluded. The earlier
0x4855 flag is withdrawn: mintuu's cold doctor shows the same
0x4855 in the same cold-never-homed state, so it is the normal cold start
value.

Verdict: the load flow tolerates a magazine at the stop through cold_init
and jog; the reinsert prompt is an unverifiable trust step; and B5 has a
second host signature — film start ~16 rows earlier on the identical
strip — alongside the dark_b collapse, both pointing to the same faster
motor/USB profile.


## 2026-09-06 — Test 26: semantic PARK Wait B completes; Test 25's geometry host-difference is withdrawn

Semantic PARK, run on mintuu, `hwblock warm --repeat 3 --skip-dpi-change
--eject --park semantic`. Status COMPLETED, findings none, ejected.

**Semantic PARK Wait B now completes deterministically.**
All three scans: Wait A ~0.004 s (no timeout), Wait B ~3.78-3.79 s,
`b_timed_out` false, `b_last = e855` on all three (e8 & 0xe3 = 0xe0, so
it matches the redefined `park_complete_status_matches` rule; see
docs/park-completion-analysis.md). This is what Test 23 failed at — there
Wait B's condition was wrong and timed out. Contrast verbatim PARK
(Test 21), which does not wait on the status word at all: its 0x32 poll
times out after 1 s and continues. So semantic is the park mode that
actually verifies completion (waits ~3.8 s for the e8-class
PARK_COMPLETE), and verbatim is the one that assumes it. Semantic is a
working, verified alternative but is NOT made the default on one run.

Reproducibility was good: W1 length constant at 3290 across the three
scans, warmup 1/1/1 never exhausted, batch start rows 1838/2145/6/0 ≈
Test 21's 1856/2159/6/0.

**Test 25's geometry host-difference is withdrawn.**
W1's first scan gave film_start_row **1842** on mintuu — the same host
and the same physical strip that gave **1858** in Test 25. That is 16
rows of load-to-load variation on a single host, which is exactly the
confound Test 25 flagged but judged unlikely ("one load cycle per
host"). B5's 1842 therefore lies inside mintuu's own load-to-load range
(1842-1858), so the ~16-row "host difference" Test 25 reported cannot be
distinguished from load-to-load variation, and that finding is
withdrawn. Test 24's dark_b collapse is unaffected — it is independent
and was confirmed against the baseline. Within a single load the drift
stays small (W1 1842 → 1839 → 1838, ~4 rows over three scans, in line
with Test 21's 4 rows over ten), so the large frame-start jumps are
between loads, not within one.

Verdict: semantic PARK Wait B is a working, verified park mode (not the
default); and the load-to-load film-start spread is ~16 rows, which
subsumes the B5-vs-mintuu geometry gap Test 25 had read as a host
difference. The one open question this leaves is the DPI-change position
shift (Test 7, ~1059 rows) — an order of magnitude larger than
load-to-load, so still a real and separate effect to test.


## 2026-09-06 — Test 27: the DPI-change position shift (Test 7) does not reproduce on 058f3a8

The cheapest experiment from docs/dpi-drift-analysis.md, run on mintuu,
driver 058f3a8, one load, frame 1, default (verbatim) park throughout.
Two variants, separate `of135i scan` processes (each its own session, so
each 3600 scan is a fresh session after the previous DPI's park — Test 7's
condition).

**Variant a — 3600 → 2400 → doctor → 3600.**
film_start_row 3600a **1836**, 3600b (after the 2400 session) **1842** —
6 rows, within a single load's drift. The `doctor` read taken immediately
after the 2400 park: reg 0x01 = 0x22, **status word 0xf855** (f8 & 0xe3 =
0xe0 → the park-complete idle class), not busy. So the park had completed
before the next session opened.

**Variant b — 3600 → 2400 → 3600 → 2400 → 3600 (five scans, one load).**
The three 3600 scans: film_start **1841 → 1838 → 1833**, i.e. ~4 rows
per scan monotonic, the same magnitude as within-load drift (Test 26).
The 2400 scans were consistent (1223 / 1218). The DPI changes add no
extra position drift.

So Test 7's ~1059-row (7.5 mm) shift does NOT reproduce on the current
code, with either one DPI change or five. The doctor read shows the 2400
park reaches idle before the next session, which contradicts hypothesis 1
(verbatim park ends mid-return). The most likely explanation is that a
fix landed since Test 7 (2026-09-03, older code) — the POSITION-budget
and park work — and incidentally settled the DPI drift too. No firm
"fixed" claim from one day's data, but the shift is not observable now.

Consequence: the step-4 fix that dpi-drift-analysis.md sketched (wait for
PARK_COMPLETE at session start) is not warranted — there is no shift to
fix. The "re-load after a DPI change" workaround is no longer reproducibly
necessary; keep it noted but no longer required in practice.

Side observation: the 2400 park polls 0x32 far longer than the 3600 park
— about 50 iterations of the benign 9555-vs-8155 pair, ~1 s each, so a
2400 park runs ~50 s where a 3600 park is a few seconds. Benign (the pair
is in the documented benign set) but it dominates the 2400 scan time.

Verdict: DPI→DPI position is stable on 058f3a8; Test 7's shift is not
reproducible; no PARK_COMPLETE session-start fix is needed. TODO 9 can be
closed as "not reproducible on current code", pending re-check if it ever
resurfaces.


## 2026-09-06 — Test 28: POSITION completion — the 0xf1 mask adds nothing, and frame 4's budget is already sufficient (offline)

Both settled from collected diag (test18/19, all eight batch frames,
`poll_timeout_details` + `phase_seconds`), no new hardware.

**TODO 10 — POSITION mask 0xf1 vs 0xf0.** The question was whether the
POSITION completion should also require bit 0x01 (mask 0xf1) rather than
the state class alone (0xf0).
- Still-moving reads are always class 9 or D (`9c55`, `d555`), already
  rejected by the 0xf0 class mask.

[Corrected 2026-09-06, see Test 31: this entry then claimed "successful
POSITION completions are always f455 (bit 0x01 = 0)" and closed TODO 10 on
that basis. **That was wrong** — it was drawn from `poll_timeout_details`
(the 1 s intermediate polls that timed out), which does NOT contain the
final "settled" value. The scan logs show POSITION actually settled on
`f555` (class F, **bit 0x01 SET**) for every move longer than frame 1
(frames 2/3/4). So 0xf1 would NOT behave identically to 0xf0, and TODO 10
is reopened as the f555 question — see Test 31.]

**Frame 4's POSITION budget is already sufficient.** The earlier worry
(position-poll-budget-frame4, 2026-09-05: "frame 4 scanned while still
moving") predates the FEEDL-scaled budget. `position_timeout_scale` gives
frame 4 (FEEDL 39026) ~28 s, and test18-f4 / test19-f4 completed POSITION
in **6.66 s / 8.13 s** with `session.failure` None. The `d555` in the
details was an intermediate non-strict settle poll, not a completion
timeout. Resolved.

Verdict: no POSITION code change. The mask stays 0xf0 and the FEEDL-scaled
budget already covers the longest move — both closed offline.


## 2026-09-06 — Test 29: dark_b collapse on even batch frames — an evidence gap, plus diagnostics to close it (offline)

Full offline investigation of the B5 batch's dark_b collapse (Test 24).
No hardware. Result: **the cause cannot be established from existing data,
because the raw dark buffers are never retained** — so this delivers a
precise evidence gap and tested diagnostics to capture it next time,
rather than a proven cause.

**1. Inventory — raw data vs summaries.** dark_b is read as raw bytes
(`dark_b_raw`, device.py `_scan_plain`/`_scan_dual`) then immediately
reduced: only `dark_b_mean` (three floats) reaches `last_diag`/the
sidecar. Confirmed in the actual diag keys on mintuu (test18/19, hwblock:
`dark_a_mean`, `dark_b_mean`, no `_raw`). B5 runs the same code, so its
sidecars carry the same summaries and no raw buffer. **The raw dark_b
bytes exist nowhere, on any host.** Every artefact here is a summary.

**2. The mechanism is understood; the cause is not.** `offset_codes` is
`slope = (mean_b − mean_a)/127`, `code = 0xFF + round(margin/slope)`. When
`dark_b_R` collapses ~24000→26177 the R slope rises, `margin/slope`
shrinks, and the R code falls ~5 — exactly the observed drift. That is a
mechanical consequence of the collapse, not a second fault.
  What the summary *can* say: `26177.375` is not an integer, so the buffer
  is not trivially constant. [Corrected 2026-09-06, see Test 30: an earlier
  version here claimed three equal non-integer means "force the three
  channel columns to hold the same value multiset — every triplet
  `[v, v, v]`". That is FALSE. Three channels can average to the same
  non-integer value with different values and different value sets — e.g.
  `[[26177,26176,26175]] + [[26177,26177,26177]]*6 + [[26180,26181,26182]]`
  all three columns average to 26177.375. The mean says nothing about the
  buffer's structure; only a per-channel checksum decides bit-identity,
  which is why the diagnostic records one.]
  What it *cannot* say: whether that came from flat hardware data, a
  short/stale bulk transfer, buffer reuse, or channel replication. All
  four hypotheses predict three-equal-means and can only be separated by
  the raw bytes (whole- and per-channel checksums, exact transfer length,
  and comparison against the preceding buffer). The mean alone is
  insufficient. Note also `dev.read(EP_BULK_IN, op.length, …)` does not
  verify the actual transfer length, and dark_a and dark_b use the *same*
  `_run_phase` — so "only dark_b, only even frames" is state/timing
  dependent, not a reshape bug (that would hit dark_a and all frames).
  A host-dependent outcome does NOT exclude a timing-dependent driver bug.

**3–5. Diagnostics (implemented, offline-verified).** New env var
`OF135I_DUMP_CAL=<dir>` makes each scan persist the raw `dark_a` and
`dark_b` buffers it already read, plus metadata that would decide the
hypotheses: exact byte length, whole-buffer and **per-channel** sha256
(three equal → columns provably bit-identical, not merely equal-mean),
`triplets_rgb_equal` fraction, min/max/mean, and a `byte_identical_buffers`
map (dark_b == dark_a → direct buffer-reuse/stale-RAM signal). The dump
runs **after PARK on host memory**: it adds no USB transaction and does
not change the op sequence (verified — the hook touches no `io`; a stub
whose `io` access raises is left untouched). Measured write cost 1.6–12 ms
for 61 KiB–1 MiB, after the scan is mechanically done, so no timing-
sensitive sequence is affected.

**No validity check was added (step 4).** There is no *proven* criterion
for invalid calibration, and rejecting on "three equal means" is
explicitly disallowed without evidence. So no offset formula, wait, or
fallback was introduced to paper over the anomaly, and no data is
rejected. The FEEDL-scaled POSITION path, LOAD/scan/eject flow, and all
safety guards are unchanged; POSITION's f555 acceptance stays a separate
open question (Test 28); no POSITION/PARK change was made.

**Tests (112 total, +6):** test_calibrate +1 (SYNTHETIC: a collapsed
dark_b drives the R offset down, with the normal per-channel case pinned
so a future check can't reject it). test_diag +5 (dump flags bit-identical
channels, does not flag the normal reference, flags a short/truncated
read, detects buffer reuse, and is a no-op without the env var while
touching no USB). Synthetic fixtures are labelled SYNTHETIC and are not
presented as a reproduction of the B5 fault.

**Remaining uncertainty:** the next step toward the cause is a hardware
re-collection with `OF135I_DUMP_CAL` set on B5 — a 1–4 batch, then read
back the even-frame `dark_b` buffers' per-channel checksums, byte lengths
and byte-identity. [Corrected 2026-09-06, see Test 30: do NOT promise that
a single collection settles the root cause. It may not reproduce the
collapse at all, or may reproduce it ambiguously; a byte-identical dark_b
is consistent with reuse/stale data but is not by itself proof of the
mechanism. One collection is the next datum, not a guaranteed verdict.] It
needs hardware and is out of scope for this offline pass. Test 24's
"degenerate data" and "platform-dependent difference" wording is corrected
above to match the evidence.


## 2026-09-06 — Test 30: hardened the dark_b capture so it survives failure, and corrected an overstated claim (offline)

An external review of Test 29's diagnostic (`d3834d6`) found three real
weaknesses for the very collection it was built for, plus a logic error in
the prose. All fixed offline; no hardware.

**1. The capture now survives a failed scan.** Before, the dump ran only
after a successful scan + PARK, so a short `dark_b` that crashes
`frombuffer`/`reshape` — the case we most want to see — saved nothing. Now
the raw dark_a/dark_b are recorded the moment they are read, *before*
reshape, and an accumulated capture is flushed on `__exit__` whether the
block exits normally or by exception. A partial buffer from a bulk read
that raises mid-transfer is kept too. The flush is host I/O only: it sends
no USB, triggers no PARK/home/eject/init, and a flush error is logged
without masking the original scan error or the session's FAILED state.

**2. Disk and metadata work moved out of the batch's active run.** Before,
the dump wrote files between frames, so an unchanged USB *sequence* did not
prove unchanged *timing*. Now buffers + lightweight per-transfer records
are held in memory during the batch, and files + checksums are written
once, on `__exit__` — after the batch or after a failure stops the
hardware. No file I/O or subprocess runs between frames (test:
`no_disk_io_during_scan`). Memory is bounded: only dark_a/dark_b are kept
(not white/shading/image), capped at 64 MiB per session with a recorded
drop count. This is NOT claimed to be perfectly timing-neutral — the
in-memory `note_read`/`note_buffer` calls add a little host work per read;
the point is only that no disk or subprocess work happens mid-batch.

**3. Per-transfer lengths are recorded.** Total length + `reshape_ok`
cannot catch a 6-byte-short read (still a valid reshape). Each calibration
bulk read now records phase, frame, sequence number, requested vs actually
returned length, before/after timestamps, and any exception. A read whose
length is unknown after an exception is `returned: null`, kept distinct
from a genuine zero-byte read (`returned: 0`). No USB reads, retries or
status polls were added; the read call is byte-for-byte the same with the
diagnostic off or on (tests: `offon_identical_write_stream`, plain and
dual).

**4. Corrected conclusions.** The claim that three equal channel means
imply the same value set / `[v, v, v]` triplets is removed from Tests 24
and 29 (a counterexample averages to 26177.375 in all three channels with
different values — regression test
`test_equal_means_do_not_imply_bit_identical_channels`). Byte-identical
buffers are now described as *consistent with* reuse/stale data, not proof
of cause. The promise that a single hardware collection settles the root
cause is withdrawn.

**Constraints held:** no offset formula, register table, POSITION/PARK
predicate, or recovery path changed. No hardware run.

**Tests (offline suite, ordinary deps):**
- PASSED: full suite green (test_safety 48, test_calibrate 22, test_hwblock
  16, test_park 12, test_offline 6, test_diag 10, test_dpi 3, test_ir 4).
  New: 7 scan-path integration tests in test_calibrate (off/on identical
  stream, flush-after-success, 6-byte and 1-byte short dark_b, failure
  preserves data + zero writes after + FAILED + no recovery, flush error
  does not mask scan error, no disk during scan); 1 dual-path off/on in
  test_ir; 2 in test_diag (per-transfer reads, equal-means counterexample).
- SKIPPED/BLOCKED: reproducing the actual B5 collapse — blocked, needs
  hardware (`OF135I_DUMP_CAL` on B5); the short-read and failure cases are
  fault injection through the mock, labelled as such, not the real fault.
- The scan-path tests run on the plain path in test_calibrate and the dual
  path off/on in test_ir; both share the one `_exec_ops` read hook and the
  one `__exit__` flush, so the failure/short-read behaviour verified on the
  plain path holds for dual by construction.

**Limitations / open:** the diagnostic observes; it does not decide the
cause. dark_b's cause stays open until a hardware collection (Test 29).
POSITION's f555 acceptance remains a separate open safety question,
untouched.


## 2026-09-06 — Test 31: POSITION settles on f555 (bit 0x01 set) on every long move — corrects Test 28 (offline)

Offline log review (hw-2026-09-05-load2/scan-3-batch.log, scan-4-raw.log,
identical across both). The POSITION completion value scales with move
length:

| frame | FEEDL | settled | time |
|---|---|---|---|
| 1 | 6746 | `f455` (exact) | 0.00 s |
| 2 | 17506 | **`f555`** (captured f455) | 2.01 s |
| 3 | 28266 | **`f555`** | 4.17 s |
| 4 | 39026 | **`f555`** | 6.33 s |

So POSITION settles on `f555` — class F but **bit 0x01 set** — on every
move longer than frame 1, systematically and reproducibly, and the settle
time scales with FEEDL. The 0xf0 mask accepts it (class match). The vendor
capture's `f455` is from frame 1 only; there is no vendor capture for the
longer moves, so `f555` is not necessarily a deviation *from the vendor*.

This corrects Test 28's "successful completions are always f455": that was
read from `poll_timeout_details` (intermediate 1 s polls), which never
holds the final settled value. The settled value lives only in the scan
log's "completion poll settled" line.

**The open question (unchanged in kind, sharper in fact):** does bit 0x01
at a class-F completion mean the transport is still settling (so 0xf0
accepts a not-fully-stopped transport on long moves — a real safety
issue), or is it a benign status bit (class F is the done class per Test
27/28's still-moving = class D finding; the code comment notes bit 0x01 is
set in the 0xad scan state, i.e. not a motion flag there)? The functional
threshold is "scan never starts on a moving transport → the frame is
geometrically correct".

**Not resolvable offline** with the files on hand: the b4 batch
(2026-09-02) is an older format (different dimensions, f3 flat) and not
trustworthy; the actual f555 frames' TIFFs are not available here (the B5
batch's are on the laptop). So this stays open.

**Next step (hardware, folds into the laptop pass):** in the dark_b
collection batch (Test 29), also (a) log the POSITION completion value per
frame and (b) check frames 2–4's film geometry — if they are correctly
positioned, f555 is a functionally safe completion and the row closes 📄
(bit 0x01 benign, keep 0xf0); if shifted, it is motion and 0xf1 / a longer
wait is needed. Optionally an explicit 0xf1 A/B: do frames 2–4 then settle
to f455 given more time (bit 0x01 = motion) or time out (bit 0x01 never
clears = benign)? No mask change is made now (the external review said to
keep f555 an open question and not to change POSITION speculatively).


## 2026-09-06 — Test 32: dark_b's cause is PROVEN — residual data (the frame's own dark_a tail), not a measurement

Hardware collection on B5 with the hardened diagnostic (Test 30,
`OF135I_DUMP_CAL`), then offline analysis. The Test 29 evidence gap is
closed: the cause is established, and the diagnostic's per-transfer log was
what settled it.

**The mechanism.** On an affected frame, dark_b is not a measurement — it
is residual data: **that same frame's dark_a last 8 words (16 bytes),
repeated 384 times** to fill the 3072-word buffer. Identical on 3 of 3
affected frames (f2/f3/f4 this run):

    f2: dark_b's 8-word period == dark_a[-8:]  (29127,25860,22039,29418,26230,22131,28476,26323)
    f3: same, == dark_a[-8:]
    f4: same, == dark_a[-8:]

**The transfer is complete, not short.** The new per-transfer log:
requested = returned = 6144 on every read, exception None everywhere. No
truncation, no USB error. But the degenerate reads come back **3–4× too
fast**: cal_dark_b 0.5–0.7 ms on the affected frames vs 1.9 ms for a
healthy dark_b and 1.7–3.9 ms for dark_a. So the device returns a full,
error-free transfer whose content is stale — it did not perform the dark_b
exposure on those frames. **It is the device's response, not a driver bug
(reshape/mean) and not a USB transport fault.**

**Why the means looked identical, and why "collapse" was the wrong word.**
The period is 8 words, the reshape channel stride is 3, gcd(8,3)=1, so each
channel cycles through all 8 values equally often → identical per-channel
mean/min/max. But no triplet is R=G=B (`triplets_rgb_equal = 0`) and
`channels_bit_identical = False` (the three per-channel sha256 differ — the
channels see the 8 values in different rotations). This confirms Test 30's
correction: the mean lied; only the per-channel checksum told the truth. It
is a 16-byte repetition, not a collapse.

**Test 24's "even frames" is wrong.** This run degenerated f2, f3 AND f4;
the morning batch degenerated f2 and f4 (f3 healthy). The only constant is
that **frame 1 is always healthy**. Correct characterisation: frame 1's
dark_b is a real measurement; later frames in a batch may instead get back
the previous dark_a's tail; how many are affected varies between runs. A
reliable automatic discriminator is the read time (~1.9 ms real vs
~0.5–0.7 ms residual) or the content (very few unique values — ~8 vs
~2500 healthy).

**f555 confirmed on B5.** frames 2/3/4 settle `f555`, settle time scaling
with FEEDL (2.01/4.17/6.33 s); frame 1 is `f455` exact. So Test 31's f555
is host-independent (reference host and B5 both). Geometry (positive,
grov, not comparable): B5 1836/2141/6/0 ≈ Test 21 W5 1856/2159/6/0 — same
structure, logged as observation. f555's benignity (frame 2–4 geometry)
will be taken on the reference host without --positive, on the same strip.

Collection was complete: `dropped_buffers = 0`, all 8 buffers saved
(6144 B each), reshape_ok on all.

**Fix follows in Test 33:** detect a residual dark_b (few unique values)
and substitute the session's healthy dark_b before offset_codes — dark_b
is a frame-independent dark measurement, so this is a proven correction,
not a cover-up. offset_codes, register tables, POSITION/PARK and recovery
are untouched.


## 2026-09-06 — Test 33: fix — detect a residual dark_b and substitute the session's healthy one (offline)

Acts on Test 32's proven cause. Offline; no hardware.

**Detection (content-based, not timing).** `calibrate.dark_is_residual()`
flags a dark buffer whose distinct-value count is `1 < unique <
_DARK_MIN_UNIQUE` (32). The proven residual (Test 32: the dark_a tail
repeated) has ~8 distinct values; a healthy dark is sensor noise (~2500).
Timing (0.5–0.7 ms vs 1.9 ms) is recorded by the diagnostic but NOT used
to decide — it is host-dependent. A single distinct value (unique == 1: a
dead AFE or a zero-filled mock) is deliberately excluded; offset_codes()'s
slope<1 fallback already owns that case.

**Substitution.** `Scanner._healthy_dark_b()` remembers each session's
healthy dark_b and, when a later frame's dark_b is residual, substitutes
the remembered one before `offset_codes()`. A dark measurement (gain=0,
offset=0xff) is frame-independent — the reference data shows it stable to
<1 % across a batch — so reusing a healthy one is a proven correction, not
a cover-up. `last_diag["dark_b_substituted"]` records when it happened, and
the raw residual buffer is still captured (note_buffer runs before
substitution) so nothing is hidden.

**Fail-closed.** If dark_b is residual and no healthy one has been measured
this session (frame 1 is empirically always healthy, so this is a
defensive branch), it raises `safety.CalibrationError` — the scan operation
is FAILED, no motor command follows, and no recovery is attempted.

**Constraints held:** offset_codes, register tables, POSITION/PARK
predicates and recovery paths are untouched. The fix only replaces a
proven-invalid *input* (residual dark_b) with a healthy measurement before
the existing offset computation, or fails closed.

**Tests (offline suite):** test_calibrate +3 — dark_is_residual
classification (residual / healthy / unique==1 / constant / empty),
_healthy_dark_b (pass-through + remember, substitute, fail-closed), and an
integration test that a residual dark_b on frame 1 fails the scan closed
with the session FAILED. The scan-sequence mocks (zero-filled dark,
unique==1) are unaffected, confirming the unique>1 boundary. Full suite
green.

**Outcome for the dark_b row:** closed. The residual dark_b is now
detected and corrected (or fails closed if unrepairable), so it no longer
reaches the AFE offset as a silent error. Cause proven (Test 32), fix in
place (Test 33). The residual is the device's own response on later batch
frames; that behaviour is a documented property of the hardware, now
handled.
