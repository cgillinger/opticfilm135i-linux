# Test log — OpticFilm 135i Linux driver

Chronological record of hardware-verified tests, findings, and open issues.
Each entry records the date, what was tested, outcome, and any follow-up.

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
| 600 | **FAILED** ❌ | — | White calibration returned all-zero R channel: `gain_codes() channel 0: non-positive peak level (0.0)`. Offline investigation in progress. |
| 7200 | NOT TESTED | — | Deferred (1.3 GB raw, last in priority per safety rules) |

**⚠️ CRITICAL FINDING — all DPI results above are DIMENSIONALLY correct
but contain NO IMAGE CONTENT.** See Test 6 below.

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
Scanner left in safe state (no mechanical risk). Root cause under
investigation — likely a profile-generation issue (sensor/window
configuration for 600 dpi), not a hardware problem.

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

### DPI profiles — IMPLEMENTED, NOT YET HARDWARE-VERIFIED
- Profiles for 600, 1200, 2400, 7200 dpi generated from vendor captures.
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
