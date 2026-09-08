# SANE stage 3, hook 3: coarse gain calibration — offline analysis

Written 2026-09-08, after hook 2 (`sane-hook2-offset.md`, Test 48).
Same method: the wire sequence with its wait points made explicit, the
failure rules, what the genesys core does around the hook, the offline
tests, and the single hardware run. The structure of hook 2 (op
programs, the genesys-free runner, the `Wire` over `UsbDevice`) is
reused unchanged; what hook 3 adds is listed in §6.

## 1. Scope

In the Python driver, after the dark bracket, `_scan_plain()` runs:

| Step | Python | Wire content | Transfers |
|---|---|---|---|
| G1 | `CAL_WHITE` (72 ops) | AFE 5/6/7 = 0x018f / 0x0171 / 0x0183 (§2); 3 × 0xd0–0xd2; the 25 motor-slope pairs 0xe0–0xf8 (13 transfers); the 32-pair exposure/sensor batch (0x01 = 0x02, 0x04 = 0x42, 0x05 = 0x40, 0x2c/0x2d = 0x04b0, 0x02 = 0x00, …); 0x29–0x2b; AFE 2/3/4 = 0 (gain 0); 0x0d = 07, 07; 0x01 = 0x03; 0x0f = 0x01; **wait W1**; read 0x102–0x105; descriptor 31104 B; bulk IN 16384 + 14336 + 384; bulk-done; 0x01 = 0x02; read 0x100/0x101 | 30 writes, 3 bulk IN |
| G2 | `_gain_with_warmup()` | pure computation + retry policy (§4) — repeats G1 while the lamp is not ready | 0 or n × G1 |
| G3 | `CAL_GAIN_CHECK_A` (37 ops) | AFE 2/3/4 = **computed gain codes** (injections `gain_r/g/b`, op 0/2/4 byte 5); regs 0x82–0x87 = 00 00 23 00 02 23; AFE 5/6/7 = 0x0080; 0x0d = 07 ×3; 0x01 = 0x03; 0x0f = 0x01; W1; counters; descriptor 3072; bulk IN 3072; bulk-done; 0x01 = 0x02; reads | 14 writes, 1 bulk IN |
| G4 | `CAL_GAIN_CHECK_B` (29 ops) | AFE 5/6/7 = 0x00ff, otherwise as G3 | 10 writes, 1 bulk IN |

G3/G4 are a dark bracket at the computed gain. The driver runs them
and **discards** their buffers (`_scan_plain` ignores the return
values); the vendor does the same reads, so they are replayed for
fidelity. The hook logs their means as free evidence.

None of the four steps writes a motor mode or FEEDL that moves the
transport (G1 writes 0x02 = 0x00), and the settled status after each
execute pulse has MOTMFLG clear: sensor-only exposures, like the dark
phases. The exit rule after the hardware run is therefore the same as
hook 2's (§7).

## 2. Values the hook writes that are not computed

- **AFE 5/6/7 = 0x018f / 0x0171 / 0x0183 in G1.** The offset codes the
  vendor programs for the white measurement. They are *not* the
  computed offset codes (those go into `CAL_SHADING_MEASURE`, hook 4)
  and the Python driver's `CAL_WHITE` has no injection for them: every
  verified scan has replayed these constants (Tests 17–33, 48). Their
  relation to the computed codes is not established (0x018f − 0x010b =
  0x84 on the reference capture; could be "final + 0x84", could be a
  fixed white-measurement offset). Classified yellow: replayed verbatim
  in the hook, recorded as an open question for hook 4, where the
  computed codes and this constant meet.
- **Regs 0x82–0x87 = 00 00 23 00 02 23 in G3** — per-channel
  exposure-related (replay-analysis.md), yellow, replayed verbatim.
- **0x0d = 07 twice in G1, three times in G3/G4** — counter clears,
  replayed as captured (replay-analysis.md keeps them yellow until an
  A/B nobody has asked for).

## 3. Wait points and failure rules

Identical to hook 2 §3: the only real wait is W1 after each execute
pulse (reg 0x101 bit 0x01, 2 s timeout, fail-closed), confirmed on
hardware in Test 48 (settled 0xcd on the first poll). Everything else
is a single read, logged. The bulk read of G1 is three transfers
(16384 + 14336 + 384 = 31104 B = 5184 RGB16 pixels); each must return
its full length, and one bulk-done read follows the third.

Failure rules, each a named `SaneException`, zero further writes:

| Failure | Status |
|---|---|
| start state ≠ 0x22 at the hook's entry (§5: hook 3 does not re-run the preamble; it requires hook 2 to have run in this `sane_start`) | `SANE_STATUS_INVAL` |
| ack ≠ 0x55, W1 timeout, short bulk | as hook 2 (`IO_ERROR` / `DEVICE_BUSY` / `IO_ERROR`) |
| white line malformed (length ≠ 31104) | `IO_ERROR` |
| white line saturated (any channel's 99.9th percentile = 65535 at gain 0) | `IO_ERROR` — "implausible AFE state", the driver's rule |
| lamp never ready (§4 budget exhausted) | `IO_ERROR`, message names the peaks seen and says to run the load flow first — the driver's `LampWarmupError` |
| missing injection value | programming error → `INVAL` (the captured gain byte is the reference unit's and must never be written) |

## 4. The computation and the retry, ported

`calibrate.gain_codes(white)` per channel over the (5184, 3) uint16
white line:

    peak = percentile(channel, 99.9)          # numpy 'linear': pos = 0.999·(N−1),
                                              # v = x[⌊pos⌋] + frac·(x[⌊pos⌋+1] − x[⌊pos⌋]) on the sorted values
    if peak <= 0: code = 63 (clamp_nonpositive, the warmup path)
    else:         code = clamp(round_half_even(32 · 31673 / peak), 0, 63)

`_gain_with_warmup()`:

    codes = measure()                          # one CAL_WHITE run
    if not all(code == 63): return codes       # the verified single-measurement path (every scan to date)
    loop: at most 1 + ⌊60 / 5⌋ = 13 measurements, 5 s apart, 60 s budget
        codes = measure()
        accept when this AND the previous measurement are non-maxed and
        every channel's peak agrees with the previous one within 3 %
    else: fail (LampWarmup)

Reference vectors (all in the repo, `cal-data/capture/`):

| Input | Expected |
|---|---|
| `cal-frame00501-len31104.bin` (the vendor's white line) | (0x2e, 0x21, 0x29) ± 1 per channel |
| a white line with every pixel at `_peak_for_gain_code(0x21)` | (0x21, 0x21, 0x21) exactly |
| all-zero white line | (63, 63, 63) with clamp → warmup path |
| retry sequences from `tests/test_calibrate.py::_WarmupHarness`: [0, 0, p(0x21), p(0x21)] → (0x21 ×3) after 4 measurements; 15 × 0 → fail at the cap | same |

The codes go to `dev->frontend.regs` under AFE addresses 2/3/4, next
to the offsets under 5/6/7 from hook 2.

## 5. What the genesys core does around the hook

`genesys_flatbed_calibration()` after `offset_calibration()`:

1. `coarse_gain_calibration(dev, sensor, local_reg, coarse_res)` — the
   hook. `coarse_res` and `local_reg` are GL124 concepts, ignored.
2. not CIS → no `led_calibration`.
3. `sanei_genesys_init_shading_data()` — because `has_send_shading_data()`
   is false for GL126, the core does **not** return early: it builds a
   default shading table and calls `genesys_send_offset_and_shading()` →
   `dev->interface->write_buffer(0x3c, …)`, a bulk write to scanner RAM.
   `ScannerInterfaceUsb::write_buffer` throws "Unsupported transfer
   type" for every ASIC but GL646/841/842/843 before any transfer — so
   today the flow fails closed here by accident, with a misleading
   message. **Gate it for GL126** (the vendor uploads its own shading
   table in hook 4's phases; a default table has no place on the wire).
4. `scanner_move_to_ta()` again (TRANSPARENCY) → `scanner_move()` →
   `init_regs_for_scan_session` refuses before the core's
   `write_registers` — verified in the source: the refusal is thrown
   before the try block whose catch would write `dev.reg`. Fail-closed,
   zero writes, but **gate it too** (the vendor has no such move).
5. Shading: `genesys_white_shading_calibration()` → impl →
   `init_regs_for_shading` refuses before `write_registers`. This is
   where the hook-3 hardware run stops: `sane_start` fails with
   UNSUPPORTED, `sane_close` writes nothing. (Hook 4 decides whether
   the vendor's shading lives here or under `DISABLE_SHADING_CALIBRATION`
   inside our own flow; not this hook's question.)

Also: hook 3 runs in the same `sane_start` as hook 2, right after it,
and the unit is then in the post-dark_b state (0x01 = 0x02, verified
Test 48). Hook 3 must therefore **not** re-run the start-state check
against 0x22 — it checks instead that hook 2 ran in this `sane_start`
(a per-device flag set by `offset_calibration()` and cleared in
`sane_close`/at the next `sane_start`), and refuses otherwise. This is
what the driver enforces too (`scan()` requires `initialize()` in the
same session; the white measurement never runs stand-alone).

Required patch changes: two GL126 gates in `genesys_flatbed_calibration`
(steps 3 and 4), ~6 lines, same style as the `genesys_start_scan` gates.

## 6. Structure: what hook 3 adds to the hook-2 machinery

- **Injections in op programs.** `CAL_GAIN_CHECK_A` patches three bytes
  (op 0/2/4, byte 5) with the gain codes. The generator emits, per
  program, an injection table `{name, op_index, byte_offset}` from the
  phase's `injections` (the same specs that feed the pair tables), and
  the runner takes a name → byte map, copies the payload and patches
  it before the write. A missing name is an error before any transfer.
- **Several `BulkIn` ops per program.** G1 has three; the validator
  accepts ≥ 1 and the hook concatenates `RunResult::buffers` in order.
- **Programs for `cal_white`, `cal_gain_check_a`, `cal_gain_check_b`**
  of every profile, same mapping rules as hook 2.
- `gain_codes()` / `percentile_linear()` / the warmup policy as pure
  functions in `gl126_ops` (the policy takes a `measure` callback and
  a `sleep` so the test drives it without time passing), plus the
  hook body in `gl126.cpp`.

Offline tests (`tests/test_sane_ops.py`, extended):

1. Wire equality with the Python replayer for the three phases, with
   the gain injection set to the fake's codes on both sides (the
   Python side patches through `_run_phase(..., gain_r=…)`; the C++
   side through the runner's map).
2. Injection: missing name → error before any transfer; wrong name →
   error.
3. Three-chunk bulk: a short second chunk → `ShortBulk` after two bulk
   transfers, no further transfer.
4. `gain_codes` on the three reference vectors; `percentile_linear`
   against `numpy.percentile` on random data (seeded).
5. Warmup policy: the two `_WarmupHarness` sequences, plus "stable
   check fails once then passes" and "saturated → fail at once".

## 7. Hardware run

As Test 48: power cycle → `of135i load` with the reference strip →
Python `scan --frame 1` (reference gain codes; Test 48 gave 0x2e / 0x20
/ 0x27) → `scanimage --force-calibration --resolution 3600 --format pnm
-o /dev/null` with the debug log → hooks 2 and 3 run, the shading hook
refuses. Compare: gain codes equal to the Python run's (Test 21: gain
identical over ten runs; ±1 at a rounding boundary is the band), white
peaks in the run-to-run band, the gain-check dark means logged. Exit:
power cycle → `load` (magazine fully out and back to the stop at the
prompt) → `eject`.

## 8. Decisions (taken under the hook-2 principles; listed for the record)

1. G1's AFE offsets 0x018f/0x0171/0x0183 and the 0x82–0x87 block are
   replayed as captured; both yellow, revisited with hook 4.
2. G3/G4 run for fidelity; their buffers are logged, not used.
3. The warmup retry is ported 1:1 (5 s / 60 s / 3 %), fail-closed; it
   has never triggered on hardware in the driver either.
4. Core gating: `init_shading_data` and the second `move_to_ta` skipped
   for GL126; the shading path stays as the refusing stop.
5. Hook 3 requires hook 2 in the same `sane_start` (flag), no second
   start-state check.
6. Exit after the run by power cycle + load, not by eject.
