# SANE stage 3, hook 2: offset calibration — offline analysis

Written 2026-09-08, before any C++ for the hook exists. This is step 1
and step 2 of the hook-2 plan in `sane-port.md` ("Next: hook 2"): the
whole wire sequence with its wait points made explicit, the failure
rules, what the genesys core does around the hook, and how the hook is
tested offline before the single hardware run. Nothing here has been
run on hardware; every "verified" below refers to the Python driver's
hardware record (`test-log.md`), which replays this exact stream.

## 1. What the hook has to reproduce

In the Python driver a scan of frame 1 at 3600 dpi starts with
`initialize()` and then `_scan_plain()`. Everything up to and including
the second dark read is the hook's scope; from `cal_white` on, the gain
hook (not brought up) takes over.

| Step | Python | Wire content | Transfers |
|---|---|---|---|
| S0 | start-state check | read reg 0x01, accept 0x22 only (see §4) | 1 |
| S1 | `initialize()`, once per session | BASE_INIT (116 pairs, 4 batches of ≤32) + 8 AFE triples (0x51/0x5d/0x5e) | 12 writes, no ack reads (verified Test 43) |
| S2 | `PREP` phase (39 ops) | sensor reads, one 0x32 write-back, two 0x8c writes, 0x03 = 0x20/0x30, 0x31 write-back, two 0x8c writes | 12 writes + reads/polls |
| S3 | `AFE_BASE` phase (71 ops) | 4 batches of the per-scan register table (121 pairs), 8 AFE triples, 0x32 = 0x95, 3 × 0xd0–0xd2, 25 motor-slope pairs 0xe0–0xf8 in 13 transfers, the exposure/lamp batch (0x03 = 0x30, 0x2c/0x2d = 0x04b0, …), 0x27–0x2b, AFE 2/3/4 = 0 (gain 0) | 34 writes + reads/polls |
| S4 | `CAL_DARK_A` (29 ops) | AFE 5/6/7 = 0x0080; 0x0d = 05, 05, 07; 0x01 = 0x03; 0x0f = 0x01 (execute); **wait W1**; read 0x102–0x105; buffer read 3072 B; 0x01 = 0x02; read 0x100/0x101 | 10 writes (9 register, 1 descriptor), 1 bulk IN |
| S5 | `CAL_DARK_B` (29 ops) | same with AFE 5/6/7 = 0x00ff and 0x0d = 07, 07, 07 | 10 writes, 1 bulk IN |
| S6 | `offset_codes(dark_a, dark_b)` | pure computation, §5 | 0 |

The generated tables (`gl126_tables.cpp`, `PLAIN3600_PHASES`) carry
the register *pairs* of S2–S5 flattened, and they are byte-exact
against `tables.py` (`gen_sane_tables.py --check`). They do **not**
carry three things the hook needs, and that is the first design
finding:

1. **Transfer boundaries.** The capture writes each AFE triple as one
   6-byte control transfer and each single register (0x0d, 0x01, 0x0f)
   as its own 2-byte transfer, with an ack read (0x8e, wIndex 0x0020 →
   0x55) after every write. `write_pairs()` would merge the 15 pairs of
   a dark phase into one 30-byte batch. Whether the chip cares is
   unknown; the unit has only ever been driven transfer-by-transfer in
   these phases (the batch form is verified only for BASE_INIT, where
   the vendor batches too). The hook must reproduce the captured
   boundaries.
2. **Interleaving.** In S4/S5 the write 0x01 = 0x02 comes *after* the
   bulk read, and the 0x8c writes in S2 sit between register writes.
   A flat pair array cannot express that order.
3. **Read-modify-write sites.** S2 reads 0x32 and writes the value
   back (`8d` → `8d`); S3 reads 0x32 (`97`) and writes `95` (bit 0x02
   cleared); S2 reads 0x31 (`fe`) and writes `fe` back. The generator
   flattened these into constants. The Python replayer *also* writes
   the captured constants (that is the form verified on hardware,
   Tests 17–33, on a unit whose 0x32 is known to vary between
   sessions, Test 23), so the constant form is what the hook writes
   today. The RMW reading of those sites is recorded here as the
   likely vendor semantics and is NOT adopted without an A/B
   (`park_semantic` adopted it for PARK only after its own review).

Consequence: hook 2 is not "write a table"; it is "run an op program".
§6 makes that the structure.

## 2. Wire formats the hook uses (all already in the code base)

| Transfer | Setup | Notes |
|---|---|---|
| register batch | 0x40/0x04, wValue 0x0083, wIndex 0, data `[reg val]…` ≤ 64 B | `write_pairs()` / `usbio.write_regs()` |
| ack read after a write | 0xc0/0x0c, wValue 0x008e, wIndex 0x0020, 1 B → 0x55 | phase replays do it, `write_regs()` does not |
| register read | 0xc0/0x04, wValue 0x008e, wIndex `(reg<<8)\|0x22`, 2 B → `[val 0x55]` | `read_register()` GL124 path, already GL126-enabled |
| extended register read (0x100–0x105) | 0xc0/0x04, wValue 0x018e, wIndex `((reg&0xff)<<8)\|0x22`, 2 B | same path, bit 0x100 of wValue |
| 0x8c write | 0x40/0x0c, wValue 0x008c, wIndex 0x0010 / 0x0013, 1 B | `write_0x8c(index, value)` exists in `ScannerInterfaceUsb` |
| buffer-read descriptor | 0x40/0x04, wValue 0x0082, wIndex 0, 8 B `[00 00 00 10][len LE32]` | **identical to the GL124 branch of `bulk_read_data_send_header`** |
| bulk IN | EP 0x81, 3072 B in one transfer | GL124 max chunk 0xeff0 > 3072 |
| bulk-done read | 0xc0/0x0c, wValue 0x008e, wIndex 0x0018, 1 B → 0x02 | follows every bulk IN and bulk OUT in every phase (15/15 sites); not in genesys |

So the GL124 bulk-read path fits GL126 for the descriptor (`wIndex 0`,
hard-coded address 0x10000000, header before each chunk) — the risk
item "first image descriptor wIndex=8" belongs to the scan phase, not
here. What GL126 adds is the bulk-done read after the transfer. The
smallest change is adding GL126 to the GL124 lists in
`bulk_read_data()` / `bulk_read_data_send_header()` (the default
bulk max of 0xf000 already covers 3072 B) and doing the bulk-done read from
the hook (§6), so the core's bulk path stays untouched for the other
chips.

## 3. Wait points, as explicit conditions

Decision 3 of `sane-port.md`: the hook polls on a named condition with
a timeout and fails closed; it does not replay pacing. The captured
polls and what they settle on:

| Site | Captured poll | Settled value | Condition proposed for C++ | Timeout |
|---|---|---|---|---|
| S2 op 1 | reg 0x32, dur 4 ms | 0x8d | none — read once, log (sensor/session-variable, masked 0x18 in Python) | — |
| S2 op 17, 19, 31 | reg 0x101 / 0x32 | 0xe8 / 0x8d / 0xec | read once, log; the class-F/E idle values are session-variable (0xe8 vs 0xf8, Test 12–16 "benign") | — |
| S3 op 28 | reg 0x101, dur 8 ms | 0xdc | read once, log | — |
| **W1** S4/S5 after 0x0f = 0x01 | reg 0x101, dur 16 ms | **0xbd** | **poll until bit 0x01 (DATAENB) is set** — 0xbd = 1011 1101, and the vendor reads the 0x102–0x105 counters and the buffer immediately after | 2 s (captured 16 ms; Python's lenient budget is 1 s) |
| after bulk | reads 0x100 = 0xf0, 0x101 = 0xdc after 0x01 = 0x02 | — | read once, verify DATAENB clear, log; not a wait (the captured reads are single) | — |

W1 is the only real wait in the hook. Its condition is a semantic
reading of the settled value, not the value itself, which is exactly
what decision 3 asks for and also where the one known uncertainty
sits: the Python record lists "9c vs ad/bd" as a benign poll mismatch
(Tests 12–33), i.e. a status-word poll that settled at 0x9c (class 9,
DATAENB clear) or 0xad (DATAENB set) instead of 0xbd, after which the
replay continued — Python's leniency is upper-nibble only, so a 0x9c
poll timed out after 1 s and the bulk read still delivered a healthy
dark buffer. The record does not say which poll site produced 0x9c.
If it is W1, the DATAENB condition would fail closed on hardware where
the verbatim replay succeeded. That is acceptable for the single
hardware run (it produces evidence, not a stuck scanner: the sequence
stops before the bulk read with the engine idle; §7), and it is why
the alternative is written down now rather than after: **fallback
condition = upper nibble ∈ {0xA, 0xB} or DATAENB set**, adopted only
if the run shows W1 settling without DATAENB. The hook logs every
poll's first and last value so the run answers this.

Failure rules (plan step 2), each a named `SaneException` with zero
further writes and no recovery:

| Failure | Where | Status |
|---|---|---|
| start state ≠ 0x22 | S0 | `SANE_STATUS_INVAL`, message names 0x00 as "cold: run `of135i load` first" (the cold path is the load flow, not a hook) |
| ack read ≠ 0x55 after a write | any write in S2–S5 | `SANE_STATUS_IO_ERROR`; the Python driver only logs this, but a wrong ack is the first place a wedged chip shows |
| W1 timeout | S4/S5 | `SANE_STATUS_DEVICE_BUSY` ("data-ready never set"); the engine is not stopped by us (no 0x01 = 0x02 write: "zero further writes" wins over tidiness, same as Python's strict polls) |
| short bulk read (≠ 3072 B) | S4/S5 | `SANE_STATUS_IO_ERROR` |
| bulk-done read ≠ 0x02 | S4/S5 | log only (the Python driver has never enforced it; enforce after the run if it is always 0x02) |
| dark_b residual (Test 32 pattern) | S6 | `SANE_STATUS_IO_ERROR` — no healthy dark_b exists in a single sane_start to substitute; batch behaviour is a later hook's problem |
| slope < 1 count/code | S6 | falls back to the vendor's codes, logged at warning level, like the Python driver — but marked so the run's comparison catches it |

## 4. What the genesys core does around the hook — blockers

`sane_start` → `genesys_start_scan()` reaches `offset_calibration()`
through this path (genesys.cpp 3976–4034), with our model's flags
`UNTESTED | WARMUP` and source "Transparency Adapter":

1. `save_power(dev, false)` — gl126 hook: no-op. Fine.
2. `WARMUP` flag + `TRANSPARENCY` method → `scanner_move_to_ta()` →
   `scanner_move()` → core motor move through `init_regs_for_scan_session`
   / `begin_scan` → **our hooks refuse** (`not_brought_up`). Fail-closed,
   but hook 2 is never reached.
3. Non-sheetfed → `move_back_home(dev, true)` → **refuses**.
4. `TRANSPARENCY` → `scanner_move_to_ta()` again → **refuses**.
5. `send_gamma_table` — no-op. `genesys_restore_calibration()` — with an
   empty cache, false; with a cache file from an earlier run it could
   **skip calibration entirely** (the "Cannot open calibration for
   writing" message in Test 47 is `genesys_save_calibration` failing
   in the uninstalled setup, so today nothing is cached; the hardware
   run uses the `--force-calibration` button option to be sure).
6. `genesys_flatbed_calibration()` → `offset_calibration()` → hook 2 →
   `coarse_gain_calibration()` → refuses → exception → `sane_start`
   fails → scanimage calls `sane_cancel` → `end_scan` (refuses, logged),
   `move_back_home` (refuses, logged), `save_power` (no-op) → `sane_close`
   (writes nothing).

Required integration changes, all GL126-gated in `genesys.cpp`, to
be added to the patch before the run:

- Skip steps 2–4 for GL126: no warmup move, no home, no move-to-TA.
  The vendor's flow has none of them (protocol-notes pass 14:
  positioning is one absolute FEEDL move in the POSITION phase, and
  the lamp warm-up that exists is the driver's `_gain_with_warmup`
  retry inside the gain phase, not a pre-scan move). Cleanest form:
  drop `ModelFlag::WARMUP` from the model (removes 2) and gate 3–4 on
  `asic_type != GL126` with a comment, ~6 lines.
- `sane_cancel_impl`: gate `end_scan` and `move_back_home` for GL126
  on "a scan actually started" so a failed `sane_start` does not log
  two refusals. Cosmetic; can wait.

Not required: the calibration cache logic (forced off for the run),
`dev->initial_regs` (the `regs` argument the hook ignores),
`sanei_genesys_find_sensor_for_write` (placeholder sensor; nothing
from it reaches the wire in hook 2).

## 5. The computation, ported

`calibrate.offset_codes()` per channel, all in double:

    slope  = (mean_b - mean_a) / (0xff - 0x80)
    if slope < 1.0: code = default[ch]          # 0x010b, 0x010a, 0x010b
    else:           code = clamp(0xff + round(margin[ch] / slope), 0, 0xffff)
    margin = (211.0, 198.0, 215.0)

Dark buffers are 3072 B = 512 RGB16LE pixels, (512, 3) uint16; the
mean is over the 512 pixels of a channel. Residual detection
(`dark_is_residual`): `1 < unique(values) < 32` on the whole buffer.

Test vectors for the C++ port, from `tests/test_calibrate.py`:

| Input | Expected codes |
|---|---|
| means a = (21411, 27770, 24897), b = (23644, 30052, 27174), tiled | (0x010b, 0x010a, 0x010b) — the vendor's codes on the reference unit |
| all-zero buffers | (0x010b, 0x010a, 0x010b) via the slope fallback |

`round()` is Python's banker's rounding; `std::lround` differs at
exact .5. The reference vector does not hit a .5, but the port uses
round-half-even explicitly so the two implementations agree on any
input.

Where the result goes: `Genesys_Frontend::set_offset(ch, u16)` exists
(`sensor.h:164`, 16-bit values) and is the core's home for AFE
offsets, so the hook stores the codes there and the shading-measure
hook reads them back for its `offset_*_hi/lo` injections. No new
field on `Genesys_Device`. The hook also logs `dark_a_mean`,
`dark_b_mean`, slopes and codes at `DBG_info` in the same units as
`last_diag` in `device.py`, which is what the hardware run compares.

## 6. Structure: an op program with a wire interface

The Python driver's verified artefact is an op *stream*, and §1 shows
the flat register tables lose what the stream carries. Rather than
hand-writing S2–S5 as C++ statements (≈ 90 transfers, easy to get one
boundary wrong and impossible to diff against the capture), the hook
runs a generated op program:

    enum class OpKind { WriteRegs, Write8c, AckRead, ReadReg, ReadExtReg,
                        PollDataReady, BufRead, BulkDone };
    struct Op { OpKind kind; uint16_t a; uint16_t b; const uint8_t* data; uint16_t len; };

- `gen_sane_tables.py` gains a second output per phase: the ordered
  op list with transfer boundaries kept, reads kept as
  `ReadReg`/`ReadExtReg` (logged, provenance) or `AckRead` (verified),
  polls emitted as `PollDataReady` with the captured settled value and
  duration (for the log and the timeout scale), bulk sites as
  `BufRead(len)` + `BulkDone`. The existing pair tables stay for the
  hooks that already use them; `--check` covers both.
- A small runner in a new `gl126_ops.{h,cpp}` executes an op program
  against an abstract `Wire` (control write / control read / bulk
  read), with the wait policy of §3 and the failure rules of §3. It
  has **no genesys headers**, like `gl126_lock`, so it compiles and
  tests standalone.
- `gl126.cpp` provides the `Wire` over `UsbDevice` (three one-line
  forwards) and `offset_calibration()` becomes: S0, run S1 (existing
  `write_table`/`write_afe_base`), run the op programs of S2–S5,
  collect the two buffers, S6.

Offline tests (plan step 2), all without hardware:

1. **Wire-level equality with the Python replayer.** A Python test
   drives `Scanner.initialize()` + the two dark phases over the
   existing `FakeUsbDevice` (tests/test_safety.py) and records every
   transfer (setup + payload + reply). A C++ probe (built like
   `gl126_lock_probe`) runs the same op programs over a `Wire` fake
   fed with the same captured replies and prints its transfer log.
   The test asserts the two logs are identical byte for byte,
   including the bulk-done reads and the position of 0x01 = 0x02.
   This is the test the plan asks for; the fake replies come from the
   captured `resp` fields, so the fake needs no model of the chip.
2. **Wait policy.** The `Wire` fake returns 0x9c for W1 N times then
   0xbd: the runner polls N+1 times and continues; never sets DATAENB:
   the runner throws after the timeout with no further transfer
   (asserted by the log).
3. **Failure rules.** Ack ≠ 0x55, short bulk, bulk-done ≠ 0x02: each
   asserted for its status and for "no transfer after the failure".
4. **Computation.** The two vectors of §5 plus the residual pattern
   (a 24-value block repeated) → error.

## 7. The hardware run (plan step 3), scoped

State before: `of135i load` with the reference strip (0x22, latched),
then **a Python `scan --frame 1`** of the same strip, whose `last_diag`
gives `dark_a_mean`, `dark_b_mean` and the offset codes to compare
against; it ends in the post-PARK state, the verified start of a
next `initialize()` (batch frame 2+). Then, in a real terminal:

    scanimage -d genesys:libusb:… --force-calibration --resolution 3600 -o /dev/null

with the `sanei_usb`/genesys debug log on. Expected: hook 2 runs S0–S6
and logs its values; `coarse_gain_calibration` refuses; `sane_start`
fails with UNSUPPORTED; `sane_close` writes nothing. Compare: codes
equal to the Python run's (offset reproduces ±1 code step across runs
on this unit, Test 21), dark means within the run-to-run band, W1's
first/last poll values recorded.

State after: registers of S3 written, two sensor exposures done, no
motor move, 0x01 = 0x02 written, no PARK. That state is not one any
verified eject starts from (the eject guard on 0x3b/0x3c passes —
S3 writes 0x3b = 0x00, 0x3c = 0x01 — but passing the guard is not
verification, Test 44's lesson). The exit is therefore the verified
recovery path, not an eject: **power cycle → `of135i load` (jog
releases the latched magazine, Test 45) → `of135i eject`**. The
operator listens throughout as usual.

## 8. Decisions asked for before the code is written

1. Constant writes at the RMW sites (§1.3) — replay the constants
   (verified form) for the run; RMW stays a documented hypothesis.
2. W1 = DATAENB with the documented fallback (§3), fail-closed.
3. Op-program structure with a genesys-free runner (§6), generator
   extended, wire-equality test against the Python replayer.
4. Integration: drop `WARMUP`, gate home/move-to-TA for GL126 (§4).
5. Exit after the run by power cycle + load, not by eject (§7).

Work split once decided: generator extension and runner + tests are
mechanical against this document (Sonnet); the genesys.cpp gating and
the hook body are small; review and the hardware run stay with the
main session and Christian.
