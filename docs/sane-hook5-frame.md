# SANE stage 3, hooks 5–7: the frame — POSITION, SCAN, PARK (offline analysis)

Written 2026-09-08, after hook 4 (Test 50). Hook 5 was scoped as
"POSITION" on its own. This analysis argues that it cannot be brought
up on its own, and lays out hooks 5, 6 and 7 as one unit — the
vendor's frame: position → scan pass → park — with one hardware run
that scans frame 1 end to end. Every decision at the end is asked for
explicitly; nothing here is taken under the earlier hooks' principles,
because this is the first motor movement of the port.

## Status (2026-09-08, same day)

All six decisions of §8 taken by Christian ("hela konkarongen"). Implemented
offline: `PollMasked` / `ReadModifyWrite` ops, the `position` and
`scan_setup` programs, the `park` program built by the generator from
`park_semantic()`'s step list, `read_image_chunk()` for the image path,
`begin_scan()` (POSITION with the FEEDL-scaled budget + the scan
setup), `end_scan()` (the semantic park), the session pinned to
3762 × 5137 px with 519156-byte requests, the GL126 branch of
`bulk_read_data`, the core's post-`begin_scan` wait loops and the
`wait_for_home` at `sane_start` gated. `tests/test_sane_ops.py` 28/28:
wire equality for `position` (48 transfers) and `scan_setup` (321) with
the Python replayer, the 224 image chunks (672 transfers) against the
replayer's SCAN tail, the `park` program against a live `park_semantic()`
run (21 transfers), the masked-poll waits and timeouts, the FEEDL and
budget values. 179 tests green, build clean.

Two corrections to the analysis below, found by the wire-equality
tests: the image chunks carry **no bulk-done read** (descriptor, ack,
bulk IN — the 0x0018 read follows only the calibration reads), and the
semantic park has **four** read-modify-write sites (0x15, 0x32 twice,
0x35 — whose write-back reuses Wait A's last polled value, as
`park_semantic()` does) and no ack reads (the driver's `write_regs()`
does none). One accepted equivalence: a chunk is read with one 519156-
byte bulk request where the capture shows ~33 USB fragments — the same
bulk stream on the wire. **The hardware run of §7 happened 2026-09-08
(Test 52, attempt 3): complete.** Frame selection followed (§10,
hardware-verified Test 53) and the colour-line shift found by Test 53's
image check was corrected the same day (Test 54; decision 5 in
docs/sane-port.md revised).

## 1. Why POSITION cannot be tested alone

POSITION is one absolute feed (mode 0x18, FEEDL from home) that leaves
the transport at the frame. Every verified flow continues with the
scan pass and PARK; the driver never leaves the unit positioned, and
no verified sequence starts from that state:

- `eject` from a positioned, unparked transport: never done (Test
  44's lesson — an unverified eject origin stalled the motor).
- PARK straight after POSITION, without the scan pass: never done;
  the carriage return is the scan pass's end plus PARK's `0x02 = 0x30`
  write, and whether PARK alone returns a merely positioned carriage
  is unknown.
- Power cycle with the transport at the frame: cold_init's homing
  rounds have only been run with the transport at home (post-PARK or
  post-load). A vendor power-on from mid-frame is a normal scenario
  for the vendor firmware, but not one this project has observed.

So the smallest hardware step with a verified exit is the whole frame:
POSITION → SCAN → PARK, after which the unit is in the post-PARK state
every driver `eject` starts from. That is the run proposed in §7.

## 2. The sequence

After the shading calibration (post-verify state, reg 0x01 = 0x22),
`_scan_plain()` runs:

| Step | Python | Wire content | Transfers |
|---|---|---|---|
| P1 | `POSITION` (48 ops), injections `feedl_{hi,mid,lo}` (op 35 bytes 7/9/11) | 0x0a = 0x48; 3 × 0xd0–0xd2; the 25 slope pairs; 0xf8 = 05; read 0x101; the mode batch (0x01 = 0x22, 0x04 = 0x42, 0x05 = 0x48, **0x3d–0x3f = FEEDL**, 0xa6–0xa9, 0x7d–0x7f = 00 36 b0, 0x80–0x87, 0x2c/0x2d = 0x04b0, 0x1d, 0x1c, 0xa4/0xa5, 0xaa/0xab, **0x02 = 0x18**, 0xae/0xaf); slope table → 0x1000c000 (512 B), slope table → 0x10010000 (the same 512 B); 0x0f = 0x01; **W3** | 21 writes, 2 bulk OUT |
| S1 | `SCAN` ops 0–320 | slope table → 0x10000000 / 0x10004000 / 0x10008000 (the scan table, 512 B ×3); 0x03 = 0x30; the scan batch (0x1c, **0x02 = 0x30**, 0x3d–0x3f = 1, 0x7d–0x7f, 0x8a–0x92, 0xae/0xaf, 0xac/0xad, 0x0d = 07, 0x28–0x2b, **0x25–0x27 = line count** (injections `lines_{hi,lo}`, op 14 bytes 55/57), 0x05 = 0x40); 0x01 = 0x23; **a read-back of every register 0x00–0xff and 0x100–0x120** (288 reads, provenance); 0x0f = 0x01; 4 × (read 0x06 → f8, read 0x101 → c5, c5, e5, a5); read 0x102–0x105 | 6 writes, 3 bulk OUT, 288 + 13 reads |
| S2 | `SCAN` ops 321–8139 | **224 image chunks**: descriptor (wIndex **8** for the first, 0 after) of 519156 B (= 23 lines × 22572 B) followed by the bulk INs (no bulk-done read here); the last descriptor 180576 B (= 8 lines); 223 × 519156 + 180576 = 115,952,364 B = **5137 lines** exactly | 224 descriptors, 7371 bulk IN |
| K1 | `PARK` (135 ops), verbatim | `0x8d` end-of-access; read 0x101 (d5); 0x03 = 0x30, 0x03 = 0x20, 0x01 = 0x22, 0x3a = 0x00; read 0x15 → 0x15 = 0x80; read 0x06; **0x02 = 0x30**; 0x36/0x3a/0x36/0x33; reads; 0x03 = 0x10, 0x03 = 0x00 (lamp off); two `0x8b` control writes (wIndex 0x0b: 0c000100, 0x0f: e0ff); read 0x32 → write back; reads; **0.74 s + 2.06 s pauses**; read 0x35 → 0x35 = 0xbb; then five idle-loop rounds (0x36/0x3a/0x36/0x33, read 0x32 → write, 2 s pause, read 0x35, poll 0x32) | 42 writes, 6 lenient polls, 13 paced ops |

Two things the Python driver does here that the C++ machinery does
not yet have: **pacing** (13 sleeps in PARK, one 1.6 s sleep before
POSITION's completion poll, two ~80 ms sleeps in SCAN — the replayer
sleeps the captured `dt` when it exceeds 50 ms, capped at 2 s) and
**lenient polls** (PARK's six 0x32 polls wait up to 1 s for the
captured value under a mask, then continue). Decision 3 of the port
says neither is replayed as such; §4 says what replaces them.

The Python driver's own frame quirk: it keeps 223 chunks (5129 lines)
as the image and discards the 180576 B chunk as a "drain of unclear
purpose". It is not a drain — it is the last 8 of the 5137 lines the
register holds. The backend reads all 5137 and, since the colour-line
fix (Test 53 follow-up), delivers 5113: the core's channel-shift node
consumes 24 (section 4 of docs/sane-port.md, decision 5); the wire is
identical either way.

## 3. Wait points

| | Captured | Condition for C++ | Timeout | Evidence |
|---|---|---|---|---|
| **W3** POSITION completion (op 47) | poll reg 0x101, settled 0xf4 after 1.61 s for FEEDL 6743; the driver polls strictly under mask 0xF0 (class only) | **class 0xF** on reg 0x101 (`PollClass`) | 3 × 1.61 s × max(1, FEEDL / 6743) — frame 1: 4.8 s, frame 4: 28 s (Test 28) | Tests 17–33: frame 1 settles f455, longer moves settle **f555** (bit 0x01 set, Test 31), so a DATAENB condition would be wrong here and the class mask is the verified one |
| SCAN start (ops 308–316) | 4 alternating reads of 0x06 (f8) and 0x101: c5, c5, e5, **a5** — class C → E → A with DATAENB set — then the counters | read as captured (`Read` ×8), then let the first bulk IN block: the data arrives when the sensor streams (the driver reads verbatim, 60 s USB timeout) | bulk timeout | every scan to date |
| image chunks | none between descriptors | USB flow control — a 519156 B read blocks until 23 lines are in the buffer (~0.18 s at 3600 dpi) | bulk timeout per chunk | every scan to date |
| **PARK Wait A** (after 0x02 = 0x30, before 0x35's RMW) | 0.74 + 2.06 s of pacing, then read 0x35 = 0xfb → write 0xbb | **reg 0x35 bit 0x40 set** (`park_semantic` Wait A) | 15 s | Test 23: Wait A completed on hardware; the pacing it replaces is the vendor's own delay |
| **PARK Wait B** (end of park) | five idle-loop rounds with 2 s pauses, polls on 0x32 that settle on session-variable values (0x81 / 0x95 / 0xb5 …) | **`park_complete_status_matches()`** on reg 0x101: bits 0x80/0x40/0x20 set, 0x01 and 0x02 clear, 0x10/0x04/0x08 ignored (park-completion-analysis.md) | 15 s | Test 23 stopped on the *old* Wait B (0x32 = 0x95); the status-word rule that replaced it is derived from every captured park end (e8 / ec / f8) and has **not** run on hardware yet |

## 4. Structure

- **`PollClass` is already there** (W2 of hook 4); W3 reuses it with
  the FEEDL-scaled timeout passed per program (`RunPolicy` override).
- **`PollBit`**: poll a register until a bit mask is set (Wait A: reg
  0x35 & 0x40). A generic form of `PollDataReady`.
- **`PollStatusPark`**: Wait B, the three-part rule above; or a
  general "poll until (v & mask) == want" op — `PollMasked {reg, mask,
  want}` covers PollDataReady, PollClass, PollBit and Wait B alike.
  Recommendation: add `PollMasked` and express all four through it;
  the generator's rules map each captured poll site to its mask/want.
- **RMW ops**: PARK reads 0x15 and writes it back with bit 0x10
  cleared, reads 0x32 and writes it back, reads 0x35 and clears bit
  0x40. `park_semantic` does real RMW; the verbatim replay writes the
  captured constants. The captured 0x32 constant is session-variable
  (Test 23), so this is the one place where the constant form is
  *known* to write a value the unit did not have. `OpKind::ReadModifyWrite
  {reg, and_mask, or_mask}` — three sites.
- **Pacing**: none. The two PARK pauses become Wait A; the idle-loop
  pauses go with the loop (one round, no pause, as `park_semantic`);
  POSITION's 1.6 s pre-sleep becomes polling from t = 0 (W3 reads the
  moving classes 9/D until F — harmless, and it is what `park_semantic`'s
  waits do too); SCAN's two 80 ms sleeps before register reads are
  dropped (reads).
- **PARK program = `park_semantic`'s op list**, not the verbatim
  capture: the same writes in the same order, real RMW, Wait A, Wait
  B, one idle round. Emitted by the generator from `tables.PARK` with
  the same reduction `park_semantic()` applies (it takes its
  constants — the two `0x8b` payloads, the optional 0x19 write — from
  the captured phase), so the two stay in step.
- **Injections**: FEEDL (three bytes) from the frame number; line
  count (two bytes) from the profile; both computed, never captured.
- **Slope tables**: the captured 512 B payloads, uploaded as constants
  — the one `BulkOut` case that keeps its captured data (they are
  motor profiles, not calibration; A10 classifies them green).

The genesys side — where the three hooks live and what the core does
around them:

| Core call | GL126 hook | Content |
|---|---|---|
| `init_regs_for_scan()` → `init_regs_for_scan_session()` | no wire; fills `dev->reg` with nothing the core writes (the core writes `dev->reg` only through `begin_scan` on this path — verified: `init_regs_for_scan` computes, `begin_scan` is the hook) | sets `session.buffer_size_read = 519156` so the image pipeline requests exactly the captured chunk size; reports 3762 × 5137 px, 3 × 16 bit |
| `begin_scan()` | **hooks 5 + 6a**: P1 (with FEEDL for the frame), W3, S1 through the settle reads | after it the core runs three wait loops on GL124 registers (feed steps 0x108–0x10a, valid words 0x102–0x105) whose GL126 semantics are unknown (the vendor reads 0x102–0x105 once, values that match neither "words" nor "lines") — **gated for GL126**, our begin_scan has already waited |
| image pipeline → `bulk_read_data(0x45, data, 519156)` per chunk | **hook 6b**: a GL126 branch of `bulk_read_data`: one descriptor (wIndex 8 for the first chunk of a scan, 0 after — a flag `begin_scan` arms), one bulk IN of the full request | the core's total is 5137 lines × 22572 B, so the last request is 180576 B, as captured |
| EOF in `genesys_read_ordered_data` → `end_scan()` | **hook 7**: K1 as the semantic park program | then the core sets `parking` via `move_back_home(false)` — gated for GL126, `dev->parking = true` set directly so `sane_cancel` does not run `end_scan` a second time; `sanei_genesys_wait_for_home` at the next `sane_start` (reads GL124 home regs) gated too |
| `wait_for_motor_stop()` before `init_regs_for_scan` | no-op for GL126 (the vendor has no such wait; hook 4's run ended on its refusal) | |

Failure rules as before: any poll timeout, short bulk (IN or OUT) or
missing injection ends the hook with zero further writes and a named
error. A failure *after* POSITION leaves the transport positioned —
the exit is then the power cycle + `load --double-jog` route (§7), the
same as after any failed session today.

## 5. What the run verifies, and against what

- The image: the backend's PNM (3762 × 5137, 16-bit RGB) against the
  driver's raw scan of the same strip and load (`scan --frame 1`,
  5129 lines): pixel statistics per channel, and the film-edge rows
  from `hwblock.film_rows()` within Test 21's band (±4 rows). A
  visual check by Christian of both images.
- W3's settled value and time (f455 expected for frame 1).
- Wait A's and Wait B's first/last values and times — Wait B's first
  hardware evidence.
- The state after: reg 0x01 = 0x22, 0x101 in the park-complete class,
  0x32 / 0x35 as after a driver park; then a driver `eject` — from the
  post-PARK state, the verified origin — closes the run.

## 6. Offline tests (before any hardware)

1. Wire equality with the Python replayer for POSITION and SCAN's
   setup (S1) — injections on both sides.
2. The image path: a C++ probe driving `bulk_read_data`'s GL126 branch
   through the `Wire` fake with 519156-byte requests must emit the
   captured descriptor/bulk/bulk-done sequence for the 224 chunks
   (wIndex 8 then 0, 180576 last) — compared with the Python
   replayer's S2 transfer list.
3. PARK: the semantic program compared with `park_semantic()`'s
   transfers over the fake (tests/test_park.py drives it), op for op,
   including the RMW values and the two waits' read sequences.
4. `PollMasked` timeout/continue cases for W3, Wait A, Wait B; the
   FEEDL-scaled budget; the frame → FEEDL mapping against
   `tables.feedl_for_frame`.

## 7. The hardware run

Power cycle → `of135i load` with the reference strip → the driver's
`scan --frame 1` (reference image + diag) → `scanimage
--force-calibration --resolution 3600 --format pnm -o frame1-sane.pnm`
with the debug log. Expected: hooks 2–4 (≈ 4 s), POSITION (1.6 s),
the scan pass (≈ 40 s, the driver's time), PARK (≈ 5 s), `sane_read`
delivers 5137 lines, `scanimage` exits 0. Christian listens through the
whole run; the motor sounds are the driver's (position move, scan
pass, carriage return). Then `of135i status` (0x22, park-complete
status) and `of135i eject` — no power cycle.

If the run stops before PARK completes: power cycle → `load
--double-jog` → `eject`, and the log says where.

## 8. Decisions — each needs an explicit go

1. **Bundle hooks 5, 6, 7 into one hardware run** (the full frame),
   because POSITION alone has no verified exit (§1).
2. **W3 = class-F poll on reg 0x101** with the FEEDL-scaled budget —
   the driver's verified rule (Test 31 rules out the bit-0x01
   variant); polling from t = 0 instead of the 1.6 s pre-sleep.
3. **PARK as the semantic program** (real RMW, Wait A on 0x35 bit
   0x40, Wait B on the status-word class rule, one idle round), whose
   Wait B has not run on hardware since it was rewritten after Test
   23. The alternative — verbatim PARK with replayed pacing and
   lenient polls — is the driver's verified form, but replaying
   pacing and tolerated timeouts is what decision 3 rejects. If the
   semantic park fails closed, the unit is mid-return with nothing
   further written, and the exit is the power cycle route.
4. **Image path through the core's pipeline** with 519156-byte
   requests and a GL126 branch of `bulk_read_data` (descriptor with
   wIndex 8 first / 0 after, bulk-done read), the core's GL124 wait
   loops gated; 5137 raw lines read, the driver's "drain" included as
   image. *(Amended after Test 53: 5113 lines reported and delivered —
   the core's colour-line shift node consumes 24, as `align_channels`
   crops them in the driver.)*
5. **Frame 1 fixed** for this run; a `--frame` backend option comes
   with batch support later. *(The option came the same day, §10;
   hardware-verified Test 53.)*
6. **Exit by `eject` from the post-PARK state** if the run completes;
   power cycle + `load --double-jog` otherwise.

## 9. After the hardware run — the scan-pass state machine (2026-09-08)

Test 52 attempt 3 verified the happy path. An external review of that
commit pointed out that the bookkeeping did not distinguish it from the
unhappy ones: the "first chunk pending" flag stayed set after a failed
chunk read, `end_scan` only checked that the flag existed, and a PARK
that threw left the flag in place for a second attempt. So `sane_cancel`
after a failed read, a frontend cancel after N chunks, or a second
`end_scan` after a PARK timeout could all run the park sequence from a
state it has never been run from. No such event has happened on the
unit; the fix is offline and closes the code paths.

`gl126_ops.h` now carries `ScanPass`, genesys-free like the runner:

    Idle -arm-> Armed -chunk_begin-> Streaming -chunk_done (bytes >=
    output_total_bytes_raw)-> Complete -parked-> Parked -arm-> Armed
    any -fail-> Failed (terminal until the next sane_open)

- `begin_scan` arms it (after the setup program, with the session's raw
  byte total) and refuses a new pass from Armed/Streaming/Complete/Failed
  before writing anything.
- `read_image_chunk_usb` takes the "first chunk" fact from it, marks
  Failed on a short read or bad ack, and counts full chunks.
- `end_scan` asks `park_decision()`: **Run** only from Complete;
  **NoPass** (Idle) and **AlreadyParked** are silent no-ops; **Failed**
  is a no-op with a log line; **AbortedPass** (Armed/Streaming) refuses,
  closes the pass and reports `SANE_STATUS_IO_ERROR` naming the bytes
  read, so the frontend shows the operator that a power cycle is due.
  A PARK that throws marks Failed before rethrowing.
- `init()` (a new `sane_open`, gated by the hardware check that reg 0x01
  reads idle) resets the bookkeeping. A power cycle is what puts the
  unit back there; the code never tries.

Offline tests (`tests/test_sane_ops.py`, probe command `scanpass`): the
verified 224-chunk path parks once and is re-armable; a failed chunk, a
cancel after 12 chunks, a cancel before the first chunk and a failed
`sane_start` never reach PARK and never retry; a PARK failure is
terminal. 31/31 op tests, backend build clean.

### 9.1 The tail the pipeline never pulls (Test 68, 2026-09-12)

The first ir3600 run through the backend stopped one chunk short: the
core's `genesys_read_ordered_data` stops pulling raw chunks once the
pipeline has produced the last DELIVERED line, and the IR pipeline crops
12 IR lines (24 raw lines) at the far end, so the 670th 16-line chunk was
never requested. `end_scan` found the pass Streaming, 332,937,216 of
333,434,880 bytes read, and refused PARK exactly as designed — nothing
written, power cycle. The visible profiles are unaffected: their
colour-shift tail makes the core read to the end (Tests 62–67). The
driver reads every chunk and crops host-side.

Rule added, wire-faithful to the driver (every chunk read, then PARK):

- `begin_scan` arms the pass with the tail the pipeline is known to leave
  (`unconsumed_tail_bytes()` in `gl126_ops.h`: pure arithmetic on
  read_lines, crop, raw line bytes and chunk length; 0 when there is no
  crop).
- `end_scan` first asks `drain_pending()` — Streaming **and** the
  shortfall is exactly the armed tail — and only then reads the remaining
  chunk(s) through `read_image_chunk_usb` (same descriptor/ack/bulk
  sequence, data discarded), which takes the pass to Complete; then the
  PARK decision as before. Any other shortfall is still **AbortedPass**.
  A chunk failure during the drain marks Failed and throws as it does
  mid-image.

Why the op-tests did not catch it: they counted 670 ir3600 chunks by
driving `read_image_chunk()` directly, not through the core's pull. The
gap is closed by `tests/gl126_session_probe.cpp`'s `pull` mode
(`test_sane_open_params.py::test_pipeline_pull_plus_tail_equals_wire`):
the real `build_image_pipeline` on a counting mock interface, every
delivered row pulled; for each profile `pulled + tail == wire total`, the
five visible profiles pull every chunk, ir3600 pulls 669 and arms 497664.
Plus `test_unconsumed_tail_bytes` and
`test_scan_pass_drains_exact_tail_then_parks` in `test_sane_ops.py`.

Hardware-confirmed 2026-09-12 (Test 69): 669 chunks pulled, one drained,
"scan pass complete", PARK normal.

### 9.2 The IR crop copied one third of each row (Test 69, 2026-09-12)

The same run delivered rows with data in the first 1728 of 5184 pixels and
zeros after. The core's `ImagePipelineNodeExtract` (the IR crop node)
copies `get_pixel_format_depth(format) / 8` bytes per pixel, and that depth
is per CHANNEL (RGB161616 = 16), so it copied 2 bytes per pixel instead
of 6 — an upstream bug that nothing else in the core exercised (Extract had
no multi-channel user). Fixed in `image_pipeline.cpp` via the integration
patch: `bpp = get_pixel_row_bytes(format, 1)`. Worth an upstream fix on
its own, separate from the GL126 backend.

The `pull` probe now fills its mock wire with a never-zero position
pattern and checks content: no zero byte in any delivered row (every
profile) and, for ir3600, each delivered row byte-equal to raw row
2·(k+12). With the bug put back the probe reports two thirds of the
delivered bytes zero and every IR row mismatched; with the fix, none.

### 9.3 The IR channels are staggered like the visible ones (Test 70)

The full-width IR image (Test 70) showed every dust speck as a triplet
in the channel mean: a 2-D cross-correlation on a dust-rich patch put R
12 IR lines before G and B 12 after — the model's ld_shift, in IR lines.
Hook 8's R = G = B assumption (docs/sane-hook8-dual.md §3, decision 3)
was wrong: the three CCD rows each see the IR light from their own
position. The IR session therefore no longer sets `IGNORE_COLOR_OFFSET`
and the Extract crop is gone; the core's `ComponentShiftLines` aligns
the infrared like the visible image, taking the same 24 lines off the
ends the crop did (delivered 5184×5336 unchanged). Consequences: the
core now reads the IR wire to its end, so no profile has an unconsumed
tail (the drain of §9.1 stays as the safety net, tail 0), and the core
Extract fix of §9.2 has no GL126 user left (kept in the patch as an
upstream bug fix). The `pull` probe checks content per channel for every
same-width profile: delivered row k, channel c == the raw line the shift
and parity select (plain k + shift_c; dual 2·(k + shift_c) + parity).

## 10. Frame selection (2026-09-08, offline)

Decision 5 pinned frame 1 for the first run. The driver's `scan
--frame N` differs from frame 1 in exactly two things, both already
ported for hook 5: the absolute FEEDL from home (`feedl_for_frame`,
6743 / 17503 / 28263 / 39023) and the FEEDL-scaled completion budget
for W3 (`position_timeout_ms`: 4.8 / 12.6 / 20.3 / 28.0 s). Everything
else -- the calibration, the POSITION program, the scan pass, PARK -- is
the same bytes.

The backend exposes it as a `--frame` option (1-4, integer range),
active for GL126 only (`SANE_CAP_INACTIVE` elsewhere), flowing
`Genesys_Scanner::frame` → `Genesys_Settings::frame` → `begin_scan`,
which checks the range itself before writing anything. The scan area
options do not apply: the frame's geometry is fixed and
`init_regs_for_scan_session` still refuses any other session. Batch
(`scanimage --batch` with a changing frame) is not addressed here: each
`sane_start` calibrates and parks as its own pass, which is the
driver's single-frame flow, not its batch flow (the residual-dark_b
substitution of Test 32/33 belongs to a later frame in the same
session; it is ported in `gl126_ops` but not exercised by this option).

Offline evidence: `test_position_frames_2_to_4_match_python_replayer`
-- the driver's actual POSITION transfers for `scan(frame=2..4)` over
the fake device equal the C++ program's with the same FEEDL (48
transfers each), and the budgets equal `position_timeout_scale()`.
`scanimage -A` lists `--frame 1..4 (in steps of 1) [1]`.

Hardware run — **done (Test 53, 2026-09-08)**: one load of the
reference strip, driver `scan --frame 2` as the reference, `scanimage
--frame 2`, then driver `scan --frame 4` and `scanimage --frame 4` (the
longest move, 28 s budget, completed in 8.0 s), exit by `eject` from
post-PARK. Frame 3 follows the same rule and was not run separately.
The image check of that run found the colour-line shift uncorrected;
fixed and hardware-verified in Test 54 (docs/sane-port.md decision 5).
