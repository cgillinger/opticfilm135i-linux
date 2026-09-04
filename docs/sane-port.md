# SANE genesys port — design and status

Goal: an upstreamable `sane-backends` genesys backend for the OpticFilm
135i (GL126). This document maps the verified Python driver onto the
genesys `CommandSet` framework and records the decisions taken. It is
the working plan; `protocol-notes.md` remains the protocol truth.

Working tree: a clone of sane-backends on branch `gl126-opticfilm135i`.
Precedent for a whole new chip family: MR !418 (GL842 / OpticFilm 7200).

## Stage plan

| Stage | Content | Needs hardware |
|-------|---------|----------------|
| 1 | Skeleton: `gl126.{cpp,h,_registers.h}` cloned from gl124, `AsicType::GL126` wired everywhere GL124 is special-cased, model/sensor/motor/gpo/adc/memory-layout placeholders, `.desc`, `genesys.conf.in`, `Makefile.am`. Compiles, model shows in `scanimage -L`, flagged UNTESTED. | no |
| 2 | Table generator: emit the base register table and the per-DPI phase register sets from `of135i/tables*.py` as C++ (`gl126_tables.cpp`). Replace gl124 placeholder bodies with the 135i flow (below). | no (compile only) |
| 3 | Bring-up against hardware, one hook at a time: boot/status → offset → gain → shading → position → scan → park → eject. | yes |
| 4 | IR (dual-light) output, dust removal stays host-side (frontend), `.desc` status → `:good`, man page, sane-devel announcement, MR. | yes |

## Why genesys fits

The 135i's transport is GL124-identical, so the shared USB layer needs
no new code beyond adding `GL126` to the GL124 branches:

| Operation | genesys GL124 path | 135i (protocol-notes.md) |
|-----------|--------------------|---------------------------|
| register write | `0x40/0x04 wValue 0x83`, 2 B `[reg,val]` | same |
| register read | `0xc0/0x04 wValue 0x8e`, wIndex `(reg<<8)|0x22`, reply `[val,0x55]` | same, incl. `0x018e` for regs > 0xff |
| bulk read | header `0x40/0x04 wValue 0x82`, 8 B `[0x10000000 LE][len LE]`, then EP 0x81 in ≤0xeff0 chunks | same descriptor; we use 16384 B chunks and wIndex=8 on the first image descriptor (meaning unknown, harmless to keep) |
| bulk write | descriptor `[addr][len]` then EP 0x02 | same (wIndex=1) |
| end access | `0x40/0x0c wValue 0x8c` | same (wIndex 16/19), plus `0x8d` |
| status | reg 0x101 | same (bit map partial, pass 16) |
| valid words / scan count | 0x102–0x105 / 0x10b–0x10d | untested on GL126 — see risks |

The scan flow also lines up with the genesys core's own sequence
(`genesys_start_scan`): power → home/load → calibration → register
setup → `begin_scan` → data → `end_scan`/eject.

## Phase → hook mapping

The Python driver replays verbatim captured phases with injection
points. In genesys each phase becomes a hook body that writes the same
registers from generated tables and does the same computation.

| Python (`device.py` / `tables.py`) | genesys hook | Notes |
|-----------------------------------|--------------|-------|
| `cold_init()` (chip handshake, COLD_INIT_PAIRS, AFE bring-up, 3 loader-homing rounds) | `asic_boot(dev, cold=true)` | Triggered when reg 0x01 lacks the ready bit 0x20 (never homed). Motor moves are the vendor's own sequence — safe from power-on. |
| `initialize()` (BASE_INIT_PAIRS + AFE base, PREP, AFE_BASE) | `asic_boot(cold=false)` + `init()` | Base table goes in `dev->reg` via `init_reg`. |
| CAL_DARK_A / CAL_DARK_B + `calibrate.offset_codes()` | `offset_calibration()` | Two dark reads at offset 0x80 / 0xff, slope-extrapolated codes → AFE regs 5/6/7 via 0x5d/0x5e. |
| CAL_WHITE + `_gain_with_warmup()` + `gain_codes()` + CAL_GAIN_CHECK_A/B | `coarse_gain_calibration()` | Keep the 3×5 s warmup retry on gain 0x3F. `ModelFlag::WARMUP` also enables the core's `genesys_warmup_lamp`; decide in stage 3 whether one of the two is enough. |
| CAL_SHADING_MEASURE → `shading_table()` → CAL_SHADING_UPLOAD → CAL_SHADING_VERIFY (re-measure, `shading_table2()`, re-upload) | inside `coarse_gain_calibration()`, with `ModelFlag::DISABLE_SHADING_CALIBRATION` | **Decision:** keep the vendor's hardware-shading flow (512 B blocks of u16 offset/gain pairs uploaded to scanner RAM, vendor gain formula, verify pass) self-contained in our hook, exactly as verified in Python. The core's host-side shading (`compute_coefficients` + `send_shading_data`) targets a different data model; adapting to it is a later refactor if the maintainer asks. `has_send_shading_data()` returns false. |
| POSITION (mode 0x18 absolute FEEDL, `feedl_for_frame`) | `init_regs_for_scan_session()` computes FEEDL from `settings.tl_y`; `begin_scan()` runs the feed, then the scan pulse | No homing between frames (pass 14). `needs_home_before_init_regs_for_scan()` → false. |
| SCAN (slope tables, line count 0x25–0x27, execute, 223 chunk reads, drain) | `begin_scan()` + core `genesys_read_ordered_data` | Chunked reads are the core's job; our fixed chunk plan (LINES_PER_CHUNK × width × 6 B) becomes `ScanSession.output_line_bytes` etc. The trailing 180 576 B drain is chip-specific: do it in `end_scan()`. |
| PARK | `end_scan()` | Includes the 0x8d end-of-access write. |
| `eject()` (loaded-magazine jog, FEEDL 3090, loader slope tables) | `eject_document()` | Guards: loader sensor bit 0x08 on reg 0x101, cold state → `asic_boot(cold)` first. Exposed only through the sheetfed path or a backend option — see open questions. |
| `tools/load_magazine.py` (ack sensor, mode 0x18 feed 0x1a22, mode 0x1c traverse 71490) | `load_document()` | Called by the core only for `is_sheetfed` models. |
| `home()` (mode 0x30, FEEDL=1) | `move_back_home()` | **Do not** use for the scan flow (it is the scan pass, pass 14). Only meaningful after `cold_init`. |
| `is_magazine_loaded()` | `update_hardware_sensors()` / `load_document()` precheck | Reliable only before the base table is written. |

## Geometry model (`calculate_scan_session`)

genesys needs a `ScanSession` per scan so its image pipeline can size
buffers. From `tables_dpi*.py`:

| dpi | px/line | lines/chunk | default lines (dual-light) |
|-----|---------|-------------|----------------------------|
| 600 | 876 | 98 | 1764 |
| 1200 | 1752 | 48 | 3552 |
| 2400 | 5256 | 16 | 7088 |
| 3600 (plain) | 3762 (windowed) | — | 5137 |
| 3600 (dual) | 5184 | 16 | 10622 |
| 7200 | 10512 | 8 | 21248 |

- Pixel format on the wire: pixel-interleaved RGB, 16-bit LE. Maps to
  `ScanColorMode::COLOR_SINGLE_PASS`, depth 16, `ColorOrder::RGB`.
- Dual-light captures alternate IR/visible lines (even/odd). genesys has
  `ScanMethod::TRANSPARENCY_INFRARED` (7200i, gl843) but no notion of an
  interleaved IR line stream. Plan: a small pipeline node (or a
  `ScanSession` line-count doubling + host-side split in
  `genesys_read_ordered_data`) that drops or keeps the IR lines.
  First target is TRANSPARENCY only from the plain 3600 table and the
  dual tables with IR lines dropped; TRANSPARENCY_INFRARED (IR as gray)
  comes in stage 4.
- Colour-line stagger in dual-light mode (pass 18, `image.align_channels`)
  maps onto `ScanSession.color_shift_lines_{r,g,b}`.
- Frame selection: model `y_size` = the 4-frame strip; frame *n* is
  `tl_y = (n-1) × pitch`. FEEDL = `FEEDL_FRAME1 + (n-1) × FEEDL_PITCH`.
  The core's own `scanner_move` (motor tables) is bypassed for positioning.

## Decisions taken

1. **Replay-with-tables, not a motor/sensor model.** The genesys motor
   and sensor tables (`tables_motor.cpp` slope generation, sensor
   exposure/timing) would require register-level semantics we only
   partly have (pass 17: DPISET, STEPSEL, LAMPPWM, sensor clock phases
   at 7200). Stage 2 emits the captured per-DPI register sets verbatim
   as C++ tables; the motor/sensor table entries stay placeholders that
   the hooks do not consult. This is what makes the port pure code
   until stage 3.
2. **Shading stays vendor-style** (see mapping). Rationale: the shading
   swap bug (2026-09-03) showed how sensitive this is; port the
   verified algorithm, do not redesign it.
3. **No captured pacing.** The Python executor sleeps by captured `dt`
   and waits on the engine-busy bit after execute pulses. In C++ the
   waits become explicit polls (reg 0x01 engine bit, status word
   0xF000 mask, 0x35/0x32 settle) with timeouts. If a capture pacing
   turns out to matter, add a named sleep, never a blind delay.
4. **`is_sheetfed = false`.** The sheetfed path ejects after every scan
   and reloads before calibration, which breaks batch (`--frames 1-4`).
   Eject/load are exposed differently — open question below.
5. **Stagger, dust removal, positive inversion stay out of the backend.**
   Dust removal (`image.remove_dust`) is a frontend feature; SANE
   delivers the IR channel as a separate gray scan the way gl843 does.

## Risks and open questions

- **Valid-words / scan-count registers** (0x102–0x105, 0x10b–0x10d):
  the core's read loop (`wait_until_buffer_non_empty`,
  `sanei_genesys_read_valid_words`) depends on them and they are
  unverified on GL126 (the Python driver reads a fixed chunk plan).
  Stage 3 first test: read them during a scan and compare with the
  chunk plan. Fallback: override the read path for GL126.
- **First image descriptor wIndex=8** — unknown meaning; genesys sends
  wIndex 0. Test whether 0 works; otherwise add a GL126 branch.
- **Poll mismatches** on reg 0x01/0x32 (~10 s, open since pass 14).
- **Eject / load in SANE terms.** Options: (a) backend-private options
  (`--eject`, `--load`) like other backends' button/lamp options,
  (b) eject on `sane_close`, (c) leave load/eject to `tools/` until the
  maintainer weighs in. Recommendation: (a), decided at stage 3.
- **Standby**: the scanner drops USB ~5 min after release and never
  returns by itself. `sane_close` must not rely on a later reopen.
- **Position drift between DPIs** (~7.5 mm, open) — a homing fix in the
  Python driver first, then port.

## Delivery checklist (from the SANE requirements survey)

- `doc/descriptions/genesys.desc` entry (mandatory from day one, status
  `:untested` → `:good`), `backend/genesys.conf.in` USB id,
  man page chip list.
- `scanimage -T`, `tstbackend`, `saned` remote test, `nm` export check.
- Announce on sane-devel before opening the MR; open the MR only when
  preview + scan + calibration are correct at all five resolutions
  (the !418 shape, not the stalled !35 shape).
