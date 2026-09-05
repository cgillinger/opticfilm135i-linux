# Replay minimization — op classification (offline, 2026-09-04)

Goal: the driver currently replays the vendor's captured command
stream verbatim (`of135i/tables*.py`, executor in `device.py`). That
makes it a faithful reproduction of one capture on one unit. For
cross-unit compatibility the flow should be expressed as *meaning*
(register sets, explicit wait conditions) rather than *recording*
(byte-exact op order, captured pacing, captured poll responses). This
document classifies every op class in the 3600 dpi plain flow so the
conversion can proceed phase by phase, with an A/B against hardware
after each step. Numbers below come from `tables.PHASES`; the dual-
light tables (`tables_ir.py`, `tables_dpi*.py`) have the same
structure with more bulk-in ops.

## Op classes

| Class | Wire shape | Count (plain flow) | Meaning | Keep? |
|-------|-----------|--------------------|---------|-------|
| **A. Register write** | `cw 0x40/0x04 wValue 0x83`, pairs `[reg, val]` | 689 pairs in 13 phases | Chip configuration. The payload. | Yes — as named register sets with injection slots. |
| **B. Ack read** | `cr wValue 0x8e wIndex 0x0022 len 1` → `0x55` | one after almost every write batch (≈480 total; 231 in SCAN alone) | The vendor driver reads register 0x00 for 1 byte after each write and gets the constant ack byte. Never branches on it. | Optional. Cheap, harmless, and it is what `read_reg`'s 0x55 check already verifies. Drop from replay; keep one after each *batch* as a link-health check if desired. |
| **C. Status read** | `cr 0x8e` on 0x31/0x32/0x35, `cr 0x18e` on 0x100/0x101, and 0x102–0x105 after buffer reads | ≈ 6–8 per calibration phase, 87 in PARK | The vendor logs state; our executor logs a mismatch and continues. 0x102–0x105 are the GL124-style valid-words counters read after each measurement. | Informational. Drop from the flow; expose via `doctor`/diag instead. Exception: the read-modify-write pairs (D). |
| **D. Read-modify-write** | `cr reg` immediately followed by `cw` of the same reg | PREP 2 (0x32, 0x31), PARK 8 (0x32 ×6, 0x35, ...), cold_init several | Real RMW: the vendor preserves unrelated bits. Our replay writes the *captured* value, which is only correct if the register reads the same on every unit/session. | Yes — convert to genuine RMW (`val = read(reg); write(reg, (val & ~mask) | bits)`). This is the single most important compatibility change: today a different loader/lamp bit pattern is silently overwritten. |
| **E. Poll** | coalesced repeated `cr`, settles on a captured value | 24 total; PREP 4, cal phases 1–3 each, POSITION 1 (1.6 s), PARK 6 | Wait conditions. `_poll_one` already relaxes 0x018e polls to the state-class nibble and masks loader bits on 0x32. | Yes — as explicit `wait(reg, mask, value, timeout)` with the mask made explicit per call, not inferred. |
| **F. Buffer descriptor + bulk** | `cw 0x82` `[addr u32][len u32]` + `bi`/`bo` chunks | 383 bi per shading phase, 7371 in SCAN; 4 bo (shading), 3 bo (slope tables) | Data transfer. | Yes — as `buf_read(addr, len)` / `buf_write(addr, data)` with the length computed from geometry (width × lines × 6), not from the capture. |
| **G. End-access / misc control** | `cw wValue 0x8c` (wIndex 16/19), `0x8d`, `0x8b` | PREP 4, PARK 3 | GL124-family "end of buffer access" and two `0x8b` commands (`0c000100`, `e0ff`) in PARK whose meaning is unknown. | Yes, verbatim; document as constants. `0x8b` payloads stay yellow. |
| **H. Captured pacing** | `dt` sleeps above 50 ms (capped 2 s) | 18.0 s per plain scan, 21.8 s dual | Mostly *the vendor's own poll interval*, not a hardware requirement: PARK's five identical 2 s rounds are one polling loop waiting for 0x35=0xbb / 0x32=0x95; PREP's 2.08 s is a poll on 0x101. | No — replace with condition waits (E). Where a delay has no observable condition (e.g. 0.31 s before shading measurement, 0.12 s before the first 0x0d write in CAL_DARK_A), keep it as a *named* constant with a comment, never as anonymous `dt`. |
| **I. Register dump** | 256 × `cr 0x8e len 2` (0x00–0xff) + 31 × `cr 0x18e` | once, inside SCAN before the image reads | The vendor dumps every register (diagnostic logging in its driver). | Drop from the flow. `doctor` now does the same dump on demand. |

## Phase-by-phase

| Phase | Ops | A writes (distinct) | D RMW | E polls | H pacing | Verdict |
|-------|-----|---------------------|-------|---------|----------|---------|
| prep | 39 | 8 (6) | 2 | 4 | 2.1 s | Small. Convert first: 6 registers, 2 RMW (0x32, 0x31), 4 waits, 4 end-access writes. |
| afe_base | 71 | 220 (158) | 1 | 1 | 0.1 s | The base register table + AFE bring-up. Already a table (`tables_base.AFE_BASE_PAIRS`, `BASE_INIT_PAIRS`); keep as data, drop the 34 ack reads. |
| cal_dark_a / _b | 29 | 15 (6) | 0 | 1 | 0.1 s | Textbook: AFE offset via 0x51/0x5d/0x5e (regs 5/6/7 ← 0x0080 / 0x00ff), 0x0d ×3, 0x01=03, 0x0f=01, wait 0x101=0xbd, read 0xc00 B, 0x01=02. Fully semantic already. |
| cal_white | 72 | 86 (68) | 0 | 2 | 0 | Same skeleton plus a 68-register exposure/sensor block. |
| cal_gain_check_a / _b | 37 / 29 | 30 (12) / 15 (6) | 0 | 1 | 0 | Same skeleton; gain via 0x51=02/03/04, plus 0x82–0x87 (per-channel exposure-related, values 0x23/0x02/0x23 — yellow). |
| cal_shading_measure / _verify | 450 / 455 | 86 (69) / 80 (72) | 0 | 3 / 2 | 0.3 s / 0.1 s | Skeleton + 128-line read (383 chunks). Chunking is derivable from geometry. |
| cal_shading_upload | 6 | 0 | 0 | 0 | 0 | Pure buffer write (F). |
| position | 48 | 62 (62) | 0 | 1 (1.6 s) | 1.6 s | Motor block: mode 0x18, FEEDL 0x3d–0x3f, speed regs, 2 slope tables, 0x0f=01, wait 0x101=0xf4. The wait is real motor time. |
| scan | 8140 | 33 (33) | 0 | 0 | 0.15 s | 3 slope tables (bo), one 31-register block (mode 0x30, FEEDL=1, speed regs, LAMPPWM 0x29, DPISET 0x2a/0x2b, line count 0x25–0x27), 0x01=23, execute, then 223 chunk reads + drain. Everything else is ack reads and the register dump (I). |
| park | 135 | 39 (9) | 8 | 6 | 11.2 s | Five identical 2 s rounds = one polling loop. Convert to: the 0x8d end-access, `0x8b` ×2, lamp/motor off writes, then `wait(0x35 == 0xbb and 0x32 == 0x95, timeout 15 s)` with the RMW on 0x32 done properly. Biggest speed win and biggest RMW risk in one phase. |

Bulk-in op counts per phase in the dual tables are larger (SCAN 20 429
chunks) but the control skeleton is identical.

## What is genuinely unit-specific vs. captured noise

- **Captured RMW values (D)** — the concrete risk. Registers 0x31, 0x32,
  0x35 carry loader-sensor, lamp and interrupt bits that differ by
  session (protocol-notes pass 16). Every place the capture wrote back
  a read value must become a real read-modify-write.
- **Poll targets (E)** — already partially relaxed in `_poll_one`; the
  masks should move into the tables as data, so a reviewer can see
  which bits a wait depends on.
- **Pacing (H)** — not unit-specific, but hides the real conditions.
  Converting PARK and PREP alone removes 13 s of the 18 s budget and
  turns two "it worked on this unit" sleeps into checked conditions.
- **Register values (A)** — model constants by default (green) except
  the yellow set from test-log Test 3 (AFE base values, exposure block
  0x82–0x87 / 0xe0–0xf7, LAMPPWM, cold-start 0x4F/0x3B/0x3C).

## Conversion order (each step A/B-tested against the verbatim flow)

1. **PARK** → semantic (writes + RMW + condition wait). Largest pacing,
   most RMW, runs at the end of every scan so a regression is visible
   immediately and recoverable (a failed park leaves the lamp on, not
   the motor moving). **Implemented** behind `--park semantic`
   (device.py `Scanner.park_semantic()`; default stays `verbatim`,
   the byte-exact replay) -- A/B on hardware pending. The two condition
   waits it replaces the captured pacing with:
     - Wait A: poll reg 0x35 until bit `0x40` is set (replaces a
       captured 0.74 + 2.06 s pause before the RMW clear of that bit).
     - Wait B: poll reg 0x32 until `(v & ~0x18) == (0x95 & ~0x18)`
       -- bits `0x18` are loader-sensor/transport bits that legitimately
       differ between sessions, the same mask `_poll_one()` already
       applies to 0x32 polls elsewhere (replaces a captured poll + a
       2.07 s pause + a second poll).
     The table-specific constants are read from each table module's own
     `PARK` at run time: the two `0x8b` control-write payloads (wIndex
     0x0b: `0c000100` in the 3600 dpi captures, `22000100` in the
     600–7200 dpi ones; wIndex 0x0f: `e0ff`/`c0ff`/`f8ff`/`feff`/`fcff`/
     `f0ff` — per dpi and IR, meaning unknown, kept verbatim) and the
     `0x19=0x00` write present in every dual-light table. The 7200 dpi
     capture wrote 0x32 back as `0x01` where the others wrote `0x81` —
     direct evidence that this register must be read live, not replayed.
     `tests/test_park.py` proves pair-for-pair equivalence against all
     six tables' captured PARK, truncated after the first idle-loop
     round. Since 2026-09-05 (P3 review) both waits FAIL CLOSED on
     timeout (`StrictPollTimeoutError` inside the park operation, no
     further write, session FAILED) instead of logging and continuing:
     Wait A follows the carriage-return write, and a transport not home
     must never be handed to the next absolute POSITION move. Test 23
     (hardware) then showed Wait B's target `0x32 == 0x95` is a session-
     variable value, not a completion signal; the rule now is the status
     word reading like every observed park end (class E/F, busy bit
     clear) — see `park-completion-analysis.md`.
2. **PREP** → semantic (RMW 0x31/0x32, waits on 0x101/0x32).
3. **Calibration skeleton** → one helper
   `measure(afe_writes, extra_regs, read_len)` used by dark A/B, white,
   gain-check A/B and shading measure/verify; the per-phase register
   blocks become named tables.
4. **POSITION / SCAN** → keep the register blocks as data; replace the
   ack reads and the register dump with nothing; compute chunk plans
   from geometry.
5. Drop `dt` pacing entirely once 1–4 are verified; remaining named
   delays are listed here.

A/B method for each step: same frame, verbatim flow vs. converted
flow, compare `.diag.json` sidecars (gain/offset codes identical,
poll timeouts 0) and the images (pair RMS on the 8-bit downscale, as
in `tools/hwblock.py`), plus the register dump from `doctor` before
and after each flow to confirm the end state is the same.

## Open items found during this pass

- `0x8b` control writes in PARK (`0c000100` at wIndex 0x0b, `e0ff` at
  wIndex 0x0f): meaning unknown; keep verbatim.
- SCAN's first image descriptor uses wIndex 8, later ones 0 (already
  noted in device.py); test with 0 during step 4.
- The `0x0d` write is issued three times in a row in every calibration
  phase (0x05, 0x05, 0x07 in dark; 0x07 ×3 in gain check). Likely
  clear-counter pulses; one of each value is probably enough. Yellow
  until A/B-tested.
