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

Total hardware scan count has not been systematically tracked.

| Test area | Volume | Status |
|---|---|---|
| Physical OpticFilm 135i units | 1 | Single-unit only |
| 3600 dpi scans | >=25 (10+10 reproducibility, cold-start, DPI reference, numerous development scans) | Stress-tested |
| 2400 dpi scans | At least 2 | Hardware-verified |
| 1200 dpi scans | At least 2 | Hardware-verified |
| 600 dpi scans | At least 1 (after 0x2b fix) | Hardware-verified |
| 7200 dpi scans | At least 1 | Hardware-verified |
| A3 reproducibility | 2 rounds × 10 scans (6 warm + 4 cold) | Repeated |
| Whole-strip batches (4 frames) | At least 1 documented | Hardware-verified |
| Cold-start initializations | >=3 documented (cold eject 09-02, Test 1, Test 9) | Partial — warmup retry implemented, not yet hardware-verified |
| Post-cold-start scans | >=5 (Test 1 + Test 9 scans 6–9) | Partial — gain clips to 0x3F without warmup retry |
| Eject cycles | Multiple | Hardware-verified |
| Magazine load cycles | Multiple | Hardware-verified |
| IR scans (dual-light) | Standard mode in nearly all scans | Repeated |
| Dust removal (IR inpainting) | Used in most scans with `--ir` | Repeated |
| USB hosts / controllers | 1 | Limited |
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
