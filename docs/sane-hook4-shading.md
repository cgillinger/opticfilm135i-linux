# SANE stage 3, hook 4: shading calibration — offline analysis

Written 2026-09-08, after hooks 2 and 3 (Tests 48, 49). Same method
and machinery. Hook 4 is the largest of the calibration hooks: two
128-line measurements of 2.9 MB each, the first bulk **write** of the
port (the shading table to scanner RAM), and a verify pass that
re-measures and re-uploads. Scope here is the **plain 3600 dpi**
profile; the dual-light profiles use two tables and a different gain
formula (`shading_table2_dual`) and are a later step.

## Status (2026-09-08, same day)

Implemented offline: `BulkOut` / `PollClass` ops, bulk injections with
the driver's padding rule, the verify pass split into two programs,
`shading_table()` / `shading_table2()` / `pack_shading()` in
`gl126_ops`, `run_shading_calibration()` in `gl126.cpp` at the end of
`coarse_gain_calibration()` (plain 3600 only), `has_send_shading_data()`
→ true and `DISABLE_SHADING_CALIBRATION` on the model. `tests/test_sane_ops.py`
21/21: wire equality with the Python replayer for the four programs
(450 / 6 / 445 / 10 transfers, offset bytes and both table payloads
injected on both sides, bulk-OUT payloads compared by digest), the
class poll, short bulk OUT, missing/oversized bulk injection, and the
tables: `shading_table` on the vendor's measurement byte-identical to
the driver's and within the driver's tolerance of the vendor's upload
(100 % of offsets within ±8), `shading_table2` byte-identical to the
driver's on real and synthetic inputs. Build clean. **The hardware run
of §7 has not happened yet.**

## 1. Scope

After the gain checks, `_scan_plain()` runs:

| Step | Python | Wire content | Transfers |
|---|---|---|---|
| H1 | `CAL_SHADING_MEASURE` (450 ops), the **dark** 128-line measurement | AFE 5/6/7 = 0x011a / 0x0120 / 0x011d (§2); 0x03 = 0x20; 3 × 0xd0–0xd2; the 25 slope pairs (13 transfers); the 32-pair exposure batch (0x01 = 0x02, 0x02 = 0x00, 0x80–0x87 …); 0x28–0x2b; **AFE 5/6/7 = the computed offset codes** (injections `offset_{r,g,b}_{hi,lo}`, ops 45/47/49 bytes 3/5); 0x01 = 0x03; 0x0f = 0x01; **W1**; counters; descriptor 2,889,216 B; **383 bulk INs** (16384 / 5632 or 6144 / 512 pattern); bulk-done; 0x01 = 0x02; **W2** (poll reg 0x100 → 0xf0); read 0x101 | 29 writes, 383 bulk IN |
| H2 | `shading_table(meas)` | per-pixel mean over the 128 lines → u16 offsets, gain 0x4000, packed (§4) | 0 |
| H3 | `CAL_SHADING_UPLOAD` (6 ops) | descriptor wIndex **1**, addr 0x10014000, len 45856; ack; **4 bulk OUTs** 16384 + 16384 + 12800 + 512 (= 46080, the payload zero-padded to the captured chunk shape) | 1 write, 4 bulk OUT |
| H4 | `CAL_SHADING_VERIFY` ops 0–444, the **white** 128-line measurement | bulk-done (of H3's upload; the capture's phase boundary splits the transfer); 0x03 = 0x30; AFE 5/6/7 = 0x0101 / 0x0005 / 0x0001 (§2); slope pairs; the exposure batch with 0x01 = 0x22, line count 0x25–0x27 = 128, 0x3d–0x3f = 1; 0x01 = 0x23; 0x0f = 0x01; W1; counters; descriptor 2,889,216 B; 383 bulk INs; bulk-done; 0x01 = 0x22; read 0x100 / 0x101 | 26 writes, 383 bulk IN |
| H5 | `shading_table2(white, dark)` | same offsets, gain = T·0x4000 / (white − offset) per channel, T = (81752, 83490, 87083) | 0 |
| H6 | `CAL_SHADING_VERIFY` ops 445–454 (`split_at` = 445) | descriptor wIndex 1, 0x10014000, 45856; ack; 4 bulk OUTs; bulk-done; reads 0x101 / 0x100 / 0x101 | 1 write, 4 bulk OUT |

Neither measurement moves the transport: motor mode 0x02 = 0x00 in
H1's batch, no FEEDL/mode change in H4's, MOTMFLG clear in the settled
status (0xa9 / 0xad). They are 128 exposures of the same line — which
is why the per-pixel mean is the table. The exit rule after the
hardware run is the one of hooks 2–3 (§7).

## 2. Values the hook writes that are not computed

- AFE 5/6/7 = 0x011a / 0x0120 / 0x011d at the top of H1 and 0x0101 /
  0x0005 / 0x0001 at the top of H4: captured constants with no
  injection in the driver, replayed on every verified scan. Yellow, as
  hook 3's 0x018f set. Only the H1 block at ops 45/47/49 carries the
  computed codes.
- 0x03 = 0x20 (H1) vs 0x30 (H4): lamp off for the dark map, on for the
  white map — the pair the two tables need, replayed as captured.
- The exposure batches (0x80–0x87, 0x2c/0x2d …) as captured.

## 3. Wait points and failure rules

- **W1** after each execute pulse: as before (reg 0x101 bit 0x01,
  2 s). Captured settled values 0xa9 (H1) and 0xad (H4) both have the
  bit set; Test 48/49 saw 0xcd on the other phases.
- **W2**, new: H1 op 448 is a captured *poll* on reg 0x100 (wValue
  0x018e, wIndex 0x0022) settling at 0xf0 after 15 ms, right after
  0x01 = 0x02 — the only place the vendor polls that register (every
  other phase reads it once, always 0xf0, Tests 48/49). Explicit
  condition: **upper nibble 0xF** (done class), timeout 5 s,
  fail-closed. Generator rule: a captured poll on reg 0x100 →
  `PollClass` (expected class from the captured settled value); the
  0x101 idle polls stay `Read` (their classes vary by session, Test
  48/49).
- Bulk IN: 383 chunks per measurement, each must return its captured
  length (2,889,216 B total); short → `ShortBulk`, nothing further.
- Bulk OUT: each chunk must be accepted in full (`sanei_usb_write_bulk`
  reports the written size); short → `ShortBulkOut`, nothing further.
- Missing injection (offset bytes, table payload) → error before any
  transfer of that program.
- Measurement shape ≠ (128, 3762, 3) → `IO_ERROR`.

## 4. The computation, ported

    # shading_table(meas): meas is (128 lines, 3762 px, 3 ch) u16
    offset[p, c] = round_half_even(mean over lines of meas[:, p, c]) as u16
    gain[p, c]   = 0x4000
    pack: pairs (offset u16 LE, gain u16 LE) in pixel-interleaved order (R,G,B,R,G,B …),
          126 pairs + 2 zero pairs per 512 B block, the last block partial and unpadded
          → 45856 B for width 3762 (3762·3 = 11286 pairs = 89 full blocks + 72 pairs)

    # shading_table2(white, dark): both (128, 3762, 3)
    f0 = round_half_even(mean over lines of dark)
    w  = mean over lines of white
    gain = clip(round_half_even(T[c] · 0x4000 / max(w − f0, 1.0)), 1, 65535), T = (81752, 83490, 87083)
    pack(f0 as u16, gain as u16)

The upload payload (45856 B) is zero-padded to the captured chunk
shape (46080 B) before the four bulk OUTs — `Phase.patched()` does the
same; the extra 224 B are USB padding the capture shows as zeros
(cal-analysis.md §4).

Reference vectors (in the repo, `cal-data/capture/`):

| Input | Expected |
|---|---|
| `cal-frame00797-len2889216.bin` → `shading_table` | vs `shading-upload-len45856.bin`: every gain 0x4000; ≥ 99 % of offsets within ±8 (the driver's own tolerance — the capture's table has a different rounding/reference than a plain mean) |
| the same, C++ vs Python | byte-identical |
| `shading_table2` on synthetic and on real inputs, C++ vs Python | byte-identical (the vendor's upload #2 is only in the private analysis dir; the driver reproduces it with cv 0.0003) |

## 5. What the genesys core does around the hook — three gates become one decision

`genesys_flatbed_calibration()` after hook 3, with our current flags
(`UNTESTED`) and `has_send_shading_data() == false`:

1. `sanei_genesys_init_shading_data` — gated for GL126 (hook 3).
2. `scanner_move_to_ta` — gated (hook 3).
3. The shading branch (`genesys_white_shading_calibration` → `init_regs_for_shading`
   refuses before `write_registers`) — today's stop point.
4. **New finding:** the function ends with
   `if (!has_send_shading_data()) genesys_send_shading_coefficient()`,
   *outside* the `DISABLE_SHADING_CALIBRATION` block → `write_buffer`
   → "Unsupported transfer type" for GL126. Fail-closed by accident
   again, and it would be reached the moment the shading branch is
   disabled.

All four exist because `has_send_shading_data()` is false: the core
then believes it must push shading data to scanner RAM itself. The
honest description of GL126 is the opposite — the backend uploads its
own tables, in the vendor's format, from its own hook. So the decision
is:

- `has_send_shading_data()` → **true**, `send_shading_data()` stays a
  no-op (documented: the core's coefficients are never used).
- `ModelFlag::DISABLE_SHADING_CALIBRATION` set: the core runs no
  shading pass, no repark, no coefficient send.
- Hook 4 runs **inside `coarse_gain_calibration()` after the gain
  checks**, as `sane-port.md` decision 2 originally placed it. The
  three gates of hooks 2–3 in `genesys_flatbed_calibration` stay (the
  move-to-TA one is still needed; the default-upload one becomes
  redundant but harmless).
- The core's calibration-cache logic (`genesys_save_calibration`,
  `sanei_genesys_is_compatible_calibration`) still runs; it caches
  `dev->frontend` and the sensor, which now carry our codes. Whether a
  cache hit may skip our calibration on a later `sane_start` is a
  hook-5+ question (the vendor calibrates every frame); the hardware
  run uses `--force-calibration` as before.

After `coarse_gain_calibration` returns, `genesys_start_scan` calls
`wait_for_motor_stop` → refuses (`not_brought_up`) — the stop point of
hook 4's hardware run, with zero writes after the hook.

Also: 2.9 MB × 2 in memory, and the whole hook takes the driver about
6 s (two 128-line reads); no SANE-side timeout applies inside
`sane_start`.

## 6. Structure: what hook 4 adds to the machinery

- **`OpKind::BulkOut`** (len = captured chunk length) and
  **`OpKind::PollClass`** (poll until `(reply[0] & 0xF0) == (captured & 0xF0)`,
  timeout `RunPolicy::class_timeout_ms` = 5000).
- **Bulk injections**: per program a table `{name, first_op, last_op}`
  from the phase's `("bo", (op_indices…))` spec; the runner takes a
  second map name → byte vector, zero-pads to the chunks' total and
  slices across the `BulkOut` ops in order; missing → error before any
  transfer; longer than the chunk total → error.
- **Split programs**: `CAL_SHADING_VERIFY` with `split_at` = 445 is
  emitted as two programs, `cal_shading_verify` (ops 0–444) and
  `cal_shading_verify_upload` (ops 445–454), so the hook computes
  between them exactly as `_scan_plain()` does. The bulk-done of the
  H3 upload is verify's op 0 and stays there (faithful boundaries; the
  validator no longer insists on one `BulkDone` per program).
- Programs for `cal_shading_measure`, `cal_shading_upload`,
  `cal_shading_verify`, `cal_shading_verify_upload` of every profile;
  the hook refuses non-plain profiles ("dual-light shading is a later
  step").
- Pure functions in `gl126_ops`: `shading_table()`, `shading_table2()`,
  `pack_shading()`, `mean_over_lines()`; the upload-length function.
- `Wire::bulk_write(data, len)` → returns bytes written; `UsbWire`
  forwards to `UsbDevice::bulk_write`.

Offline tests (`tests/test_sane_ops.py`, extended): wire equality with
the Python replayer for the four programs (offset bytes injected on
both sides; the table payloads computed by the *Python* functions on
the fake's buffers and injected on both sides — the C++ computation is
compared separately, so the wire test isolates the transfer stream);
`PollClass` waits then continues / times out; `BulkOut` short →
`ShortBulkOut` with no transfer after; missing bulk injection → error
before any transfer; the computation against the reference vectors and
against Python byte for byte (including the 46080 padding).

## 7. Hardware run

As Tests 48/49: power cycle → `of135i load` with the reference strip →
Python `scan --frame 1` (its `last_diag` has nothing for shading; the
comparison is C++ vs the Python functions on the C++'s own buffers,
logged as checksums, plus the table statistics: offset mean/range and
gain mean/range per channel against cal-analysis.md §4's ranges
93–344 / ≈0x4000) → `scanimage --force-calibration --resolution 3600
--format pnm -o /dev/null` with the debug log → hooks 2, 3, 4 run;
`wait_for_motor_stop` refuses. Listen: the two 128-line measurements
sound like the driver's calibration (lamp, no transport). Exit: the
two-cycle procedure — or the `--double-jog` A/B, which this run is the
natural occasion for.

## 8. Decisions (under the hook-2 principles; listed for the record)

1. H1/H4's leading AFE constants and lamp writes replayed as captured
   (yellow, with hook 3's set).
2. W2 = class-F poll on reg 0x100 with a 5 s timeout, fail-closed;
   generator rule "captured poll on reg 0x100 → PollClass".
3. `has_send_shading_data()` → true with a no-op send;
   `DISABLE_SHADING_CALIBRATION` set; hook 4 inside
   `coarse_gain_calibration()` after the gain checks. `sane-port.md`
   decision 2 is updated to say so.
4. Plain 3600 only; dual-light profiles refuse with a message.
5. Upload payload padded to the captured 46080 B chunk shape, as the
   driver does.
6. Exit after the run by power cycle; the `--double-jog` A/B may be
   tried on that exit.
