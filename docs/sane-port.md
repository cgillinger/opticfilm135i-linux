# SANE genesys port — design and status

Goal: an upstreamable `sane-backends` genesys backend for the OpticFilm
135i (GL126). This document maps the verified Python driver onto the
genesys `CommandSet` framework and records the decisions taken. It is
the working plan; `protocol-notes.md` remains the protocol truth.

Working tree: a clone of sane-backends on branch `gl126-opticfilm135i`.
Precedent for a whole new chip family: MR !418 (GL842 / OpticFilm 7200).

## Where the sources live

The port's own sources are version-controlled HERE, in `sane/`, not in the
sane-backends clone -- so they are reviewed, regenerated and released with
the driver whose tables they carry. To build, symlink them into a
sane-backends checkout's `backend/genesys/`:

```
cd /path/to/sane-backends/backend/genesys
for f in gl126.h gl126.cpp gl126_registers.h gl126_tables.h gl126_tables.cpp; do
    ln -sf /path/to/opticfilm135i-linux/sane/$f $f
done
```

Symlinks rather than copies: an edit here is picked up by the next build
with nothing to re-sync. The eventual merge request takes copies.

## Status (2026-09-07, evening)

- **Stage 2 — done (offline).** `tools/gen_sane_tables.py` emits
  `sane/gl126_tables.{h,cpp}` from `of135i/tables*.py`: the base, AFE,
  cold-init and loader-speed tables, the deduplicated motor slope tables,
  and for all six scan profiles (3600 plain, 3600 IR, 600/1200/2400/7200)
  every phase's register writes in capture order, its buffer transfers,
  and the indices of the bytes that carry computed values. Reads and polls
  are deliberately not emitted (decision 3). Two offline tests guard it:
  one regenerates and fails if the checked-in output is stale, one asserts
  each injection lands on the value byte of the expected register (gain on
  0x5e, offsets on 0x5d/0x5e, FEEDL on 0x3d-0x3f, line count on
  0x26/0x27) -- a mis-indexed injection would scan with the reference
  unit's calibration and look like a working scan.
- **Stage 1 — done.** The `scanimage -L` check passed on the reference
  host 2026-09-07 (Test 41): the built backend lists the unit as
  `genesys:libusb:…` / `PLUSTEK OpticFilm 135i`, with zero writes. `sane/gl126.{h,cpp}` and `sane/gl126_registers.h` declare the
  full `CommandSet` surface. The table-driven hooks (`init`, `asic_boot`'s
  register phase) are implemented; every hook that would move the motor
  throws `SANE_STATUS_UNSUPPORTED` naming itself, rather than issuing a
  sequence that has never been executed. `check_start_state()` mirrors
  `of135i/safety.py`: reg 0x01 must read 0x22 or 0x00 or the call fails
  having written nothing, and no recovery is attempted.

  The integration into the sane-backends tree is `sane/gl126-integration.patch`
  (against sane-backends master 1d47d7c): `AsicType::GL126` and
  `ModelId::PLUSTEK_OPTICFILM_135I` in `enums.{h,cpp}`, the command-set
  factory and the 0x101 extended-register address in `low.cpp`, the model
  entry in `tables_model.cpp`, `Makefile.am`, `genesys.conf.in` and the
  `.desc` entry (`:status :untested`).

  **Built and linked**, 2026-09-06 on B5 and 2026-09-07 on the reference
  host: `libgenesys_la-gl126.o` and `libgenesys_la-gl126_tables.o` are in
  `libsane-genesys.so` (229 gl126 symbols), no warnings from our files.
  To run the built backend without installing it: `LD_LIBRARY_PATH` at the
  clone's `backend/.libs` plus a private `SANE_CONFIG_DIR` whose
  `dll.conf` holds only `genesys` (and a copy of the built `genesys.conf`).

  The model's sensor/adc/gpio/motor ids are the OpticFilm 7200's, used as
  placeholders so the model registers: the 135i's own tables are not
  written yet. Nothing can reach the wire through them, because every scan
  hook refuses first -- but they are wrong values and stage 3 replaces
  them.
- **Stage 3 pre-flight (2026-09-07, Test 42, offline).** Walking the
  `sane_open` → `init()` → `sane_close` path against the Python driver
  before the first hardware run found four deviations, all fixed:
  1. `scanner_interface_usb.cpp` took the GL646-style path for GL126
     register reads/writes (`0x40/0x0c`, one byte at a time). The 135i
     wire is the GL124 one -- read `0xc0/0x04 0x8e` with wIndex
     `(reg<<8)|0x22` and `0x018e` above 0xff, write `0x40/0x04 0x83`
     `[reg,val]` -- so GL126 joins those two branches and the
     `write_fe_register` one (AFE via 0x5d/0x5e, not 0x3a/0x3b).
     **Still open**: the bulk-read header, `bulk_read_data`, `write_ahb`
     and the `low.cpp` sites (valid words, scan count, feed steps, bulk
     max size). Each is decided when its hook is brought up.
  2. `AFE_BASE` was written as chip registers 0x00-0x07 (reg 0x01 among
     them). The table holds AFE addresses; each value goes through
     0x51 / 0x5d / 0x5e as one three-pair batch, as `initialize()` does.
  3. Register tables went out one pair per control transfer. The vendor
     and the driver send 0x83 batches of up to 32 pairs; `write_pairs()`
     now does the same, straight to the USB device (`Genesys_Register_Set`
     de-duplicates by address and cannot carry the AFE sequence).
  4. `init()` wrote the base table from the cold state (0x00). The
     Python driver runs the vendor cold-start sequence first, which has
     motor moves; `init()`/`asic_boot()` were changed to refuse a cold
     scanner before the first write. *Superseded by Test 46:* `init()`
     and `asic_boot()` now write nothing in any state, so a cold scanner
     (0x00) opens like an idle one; the cold path is a matter for the
     hooks that write, none of which is enabled yet.
  Also guarded: `sane_close` issues an endpoint clear-halt and a USB port
  reset for every model. The unit has never been driven with either, so
  the integration patch skips both for GL126 until a directed check says
  they are safe. The lamp-off write at close (0x03 = 0x00) was kept at
  this point; *superseded by Test 46:* the integration patch now skips
  it for GL126 too, so `sane_close` writes nothing (the driver writes
  nothing at session close either; 0x03 = 0x00 only appears inside PARK
  and cold-init).

- **Stage 3, hook 1 — done (Test 43, hardware).** `sane_open` from
  reg 0x01 = 0x22 writes BASE_INIT (four 0x83 batches) and the AFE base
  (eight 0x51/0x5d/0x5e triples), `sane_close` writes 0x03 = 0x00; the
  unit reads 0x22 afterwards with 0x32/0x35 at the base-table values.
  Getting there added a real `SensorId::CCD_PLUSTEK_OPTICFILM_135I`
  (five resolutions, TRANSPARENCY, other fields default and labelled),
  the gl124-shaped `calculate_scan_session` (geometry only), and
  `exposure_lperiod = 0x3ffb` on the sensor (the core seeds an option
  from it; GL126 never reads it). Next: hook 2, offset calibration (scoped
  under "Decisions taken") --
  the first bulk read, which is where the bulk-path GL124 sites get
  decided against the captures.
- **Test 44 → resolved by design (Test 46).** The stalls came from a
  state only the backend's hook-1 `init()` produced: the base table
  written at open and left there. The vendor never writes the base
  table at app open (capture 20260907-vendor-open-with-latched-magazine:
  OPEN table + jog, even with a latched magazine), and the driver's
  `open()` writes nothing. `init()`/`asic_boot()` now read reg 0x01 and
  write nothing; the base table belongs to the scan-session hooks. Why
  eject stalls from the base-table-only state is recorded as a
  hypothesis (0x3b/0x3c/0x4f), not established, and not pursued.
- **Hook 1, redefined:** `sane_open` = open + start-state read, zero
  writes. The Test 43 run remains the wire-format verification (0x8e
  read, 0x83 batches, AFE via 0x51/0x5d/0x5e all reach the chip as the
  driver's do).

### Stage 3, hook 1 — what `sane_open` does now (verified, Test 46)

Precondition: reg 0x01 = 0x22 or 0x00. Trigger: `scanimage -d genesys:… -A`.

| Step | Wire | Same as the driver? |
|---|---|---|
| process lock | `gl126::process_lock_acquire()` (`sane/gl126_lock.{h,cpp}`), no wire traffic; `EWOULDBLOCK` refuses with `SANE_STATUS_DEVICE_BUSY` before `sanei_usb_open` | yes (same lock file/path as `ProcessLock`, see "Mutual exclusion with the driver" below) |
| `sanei_usb_open` | `libusb_open`, read the current configuration, claim interface 0 -- no SET_CONFIGURATION when the kernel has already configured the unit (it has: one configuration, value 1), no kernel-driver detach | driver: `set_configuration` after the check (step 6 of `UsbIo.open`) -- see decision 6 |
| `check_start_state` | read reg 0x01 (`0xc0/0x04 0x8e`) | yes (`safety.py`) |
| `init` / `asic_boot` | nothing | yes (`Scanner.open()` writes nothing) |
| `sane_close` | release interface; no register write, no clear-halt, no reset; then `gl126::process_lock_release()` | yes |

Measured 2026-09-07 (Test 46): exactly one control transfer, the 0x8e
read of reg 0x01, zero writes; a driver eject afterwards was normal.
The process-lock row was added 2026-09-08, offline-verified only (see
below) -- Test 46 predates it and did not exercise the lock.

### HISTORICAL — hook 1 as first implemented (Test 43), superseded by Test 46

This table describes what `sane_open` wrote on 2026-09-07 morning. It is
**no longer what the code does**: writing the base table at open created
the state the driver's eject stalled from (Test 44), and Test 46 showed
the vendor never writes it at app open. Kept as the wire-format record
(0x83 batches, AFE via 0x51/0x5d/0x5e reach the chip as the driver's do).

Precondition: reg 0x01 = 0x22 (idle-homed; a driver-loaded magazine is the
state every scan starts from). Trigger: `scanimage -d genesys:… -A`
(open, list options, close -- no `sane_start`).

| Step | Wire | Same as the driver? |
|---|---|---|
| `sanei_usb_open` | set configuration, claim interface 0 | yes (`usbio.py` open) |
| `check_start_state` | read reg 0x01 (`0xc0/0x04 0x8e`) | yes (`safety.py`) |
| `BASE_INIT` | 116 pairs, four 0x83 batches of ≤32 pairs | yes (`initialize()`) |
| `write_afe_base` | 8 × `[0x51,a 0x5d,0 0x5e,v]` batches | yes (`initialize()`) |
| `sane_close` | write 0x03 = 0x00; release interface | 0x03 = 0x00 is PARK's write; no clear-halt, no reset. *Removed for GL126 after Test 46.* |

No 0x0f = 0x01 execute pulse in any of it; no motor register is touched.
Result then: `scanimage -A` printed the option list and `of135i status`
read 0x22 -- but the driver's `eject` from that state stalled (Test 44).

**Stage 3 is the first step that touches the scanner**, and it happens on
the machine the unit is attached to, with the operator listening. Nothing
in stages 1-2 may be "completed" by inferring a motor sequence from the
tables.

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
| `cold_init()` (chip handshake, COLD_INIT_PAIRS, AFE bring-up, 3 loader-homing rounds) | `asic_boot(dev, cold=true)` -- **not enabled**: `asic_boot` currently reads reg 0x01 and writes nothing | When brought up: triggered when reg 0x01 reads 0x00 (never homed). Motor moves are the vendor's own sequence — safe from power-on. Until then a cold scanner opens but no writing hook accepts it. |
| `initialize()` (BASE_INIT_PAIRS + AFE base, PREP, AFE_BASE) | scan-session hooks (`init_regs_for_scan_session` and the calibration hooks), NOT `init()` | `sane_open` writes nothing (Test 46); the base table is per-scan, as in the vendor's per-frame re-init. |
| CAL_DARK_A / CAL_DARK_B + `calibrate.offset_codes()` | `offset_calibration()` | Two dark reads at offset 0x80 / 0xff, slope-extrapolated codes → AFE regs 5/6/7 via 0x5d/0x5e. |
| CAL_WHITE + `_gain_with_warmup()` + `gain_codes()` + CAL_GAIN_CHECK_A/B | `coarse_gain_calibration()` | Keep the 3×5 s warmup retry on gain 0x3F. `ModelFlag::WARMUP` also enables the core's `genesys_warmup_lamp`; decide in stage 3 whether one of the two is enough. |
| CAL_SHADING_MEASURE → `shading_table()` → CAL_SHADING_UPLOAD → CAL_SHADING_VERIFY (re-measure, `shading_table2()`, re-upload) | inside `coarse_gain_calibration()`, with `ModelFlag::DISABLE_SHADING_CALIBRATION` | **Decision:** keep the vendor's hardware-shading flow (512 B blocks of u16 offset/gain pairs uploaded to scanner RAM, vendor gain formula, verify pass) self-contained in our hook, exactly as verified in Python. The core's host-side shading (`compute_coefficients` + `send_shading_data`) targets a different data model; adapting to it is a later refactor if the maintainer asks. *Revised 2026-09-08 (hook 4):* `has_send_shading_data()` returns **true** with a no-op `send_shading_data()` — with false the core pushes a default table and its coefficients to scanner RAM through `write_buffer`; true plus `DISABLE_SHADING_CALIBRATION` keeps the core off the wire entirely (`docs/sane-hook4-shading.md` §5). |
| POSITION (mode 0x18 absolute FEEDL, `feedl_for_frame`) | `init_regs_for_scan_session()` computes FEEDL from `settings.tl_y`; `begin_scan()` runs the feed, then the scan pulse | No homing between frames (pass 14). `needs_home_before_init_regs_for_scan()` → false. |
| SCAN (slope tables, line count 0x25–0x27, execute, 223 chunk reads, drain) | `begin_scan()` + core `genesys_read_ordered_data` | Chunked reads are the core's job; our fixed chunk plan (LINES_PER_CHUNK × width × 6 B) becomes `ScanSession.output_line_bytes` etc. The trailing 180 576 B drain is chip-specific: do it in `end_scan()`. |
| PARK | `end_scan()` | Includes the 0x8d end-of-access write. |
| `eject()` (loaded-magazine jog, FEEDL 3090, loader slope tables) | `eject_document()` | Guards: loader sensor bit 0x08 on reg 0x101, cold state → `asic_boot(cold)` first. Exposed only through the sheetfed path or a backend option — see open questions. |
| `tools/load_magazine.py` (ack sensor, mode 0x18 feed 0x1a22, mode 0x1c traverse 71490) | `load_document()` | Called by the core only for `is_sheetfed` models. |
| `home()` (mode 0x30, FEEDL=1) | `move_back_home()` | **Do not** use for the scan flow (it is the scan pass, pass 14). Only meaningful after `cold_init`. |
| `is_magazine_present()` | `update_hardware_sensors()` / `load_document()` precheck | Reliable only before the base table is written. |

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
6. **Safety model: the C++ mirrors `of135i.safety`, it cannot reuse it.**
   (Revised 2026-09-07 evening, after Test 46 and a review of the whole
   `sane_open` → hook → `sane_close` chain against docs/hardware-safety.md.)

   *Implemented:* `check_start_state()` in `gl126.cpp` reads reg 0x01
   and fails with zero writes unless it reads 0x22 or 0x00; it is the
   first thing `init()`/`asic_boot()` do, and every hook that will write
   calls it before its first write. No recovery is attempted anywhere.
   `sane_open` and `sane_close` write nothing (Test 46: one control
   transfer, the reg 0x01 read). Every hook that would write
   beyond that throws `SANE_STATUS_UNSUPPORTED` naming itself.

   *Verification before configuration* (hardware-safety.md, "Opening a
   session"): the driver reads reg 0x01 before any standard request and
   only then detaches/`set_configuration`s. The backend cannot reorder
   `sanei_usb_open`, but on this unit `sanei_usb_open` issues **no**
   device-facing standard request: the unit has one configuration, the
   kernel selects it at enumeration, and `sanei_usb` calls
   `SET_CONFIGURATION` only when the device reports configuration 0 or
   has several; `claim_interface` is a host-side operation and there is
   no kernel-driver detach. So the reg 0x01 read is the first transfer
   on the wire (measured, Test 46). Residual, accepted and documented:
   if the unit ever reports configuration 0 (never observed on Linux),
   `sanei_usb` would send `SET_CONFIGURATION` before our read -- the same
   request the driver sends on every session after its check, so its
   effect on this unit in the 0x22 and 0x00 states is verified benign;
   only the engine-running states are then unprotected by that one
   request, and no write follows a failed check either way.

   *Mutual exclusion with the driver -- decided 2026-09-07, implemented
   2026-09-08:* before this it rested on the interface claim alone. That
   fails closed in both directions (the driver's `set_configuration`
   gets `EBUSY` after its check and refuses with zero writes, Test 45;
   the backend's `claim_interface` gets `EBUSY` while the driver holds
   the unit and `sane_open` fails before any transfer), but the driver
   then reports a half-configured session and asks for a power cycle
   that is not needed, and the driver's read-only sessions (`status`,
   `doctor`) hold the lock without claiming the interface, so the claim
   does not see them. Implemented: a new standalone pair,
   `sane/gl126_lock.{h,cpp}` (no genesys headers, POSIX `flock` only, so
   it compiles and tests on its own), mirrors the driver's `ProcessLock`
   byte-for-byte -- same path (`/tmp/of135i-07b3-1436.lock`, or
   `OF135I_LOCK_FILE`), same holder-line format. The GL126 branch of
   `sane_open_impl` in `genesys.cpp` takes the lock non-blocking before
   the USB open; `EWOULDBLOCK` → `SANE_STATUS_DEVICE_BUSY`, zero
   transfers. Offline tests in both directions:
   `tests/test_sane_lock.py` (driver holding the lock refuses the
   backend, backend holding it refuses the driver, holder-line format,
   read-only-lock-file fallback) -- standalone build of `gl126_lock.cpp`
   with a tiny probe, zero new compiler warnings on the full
   `libsane-genesys.la` build. Hardware check done 2026-09-08 (Test
   47): with the driver holding the lock, `scanimage -A` fails with
   `Device busy` naming the holder's pid, and the `sanei_usb` debug log
   shows no open and zero transfers in `sane_open`; with the lock free
   the same command opens, reads reg 0x01 once, closes and releases
   the lock (a driver `status` succeeds right after). Note: the
   genesys-wide device probe in `sane_init` opens/closes the device
   (interface claim, no wire transfer) before `sane_open` and is not
   under the lock -- it never was under the interface claim either
   when the driver's session is read-only.

   *Reference-counted ownership -- fixed 2026-09-08, external review:*
   the first version released the lock from an unconditional
   `catch (...)`, so a second, failing GL126 open in the same
   long-lived process (saned, xsane) could drop an already-open first
   session's lock. `gl126_lock.{h,cpp}` now counts references
   (`process_lock_acquire`/`process_lock_release` are paired calls, an
   acquire while already held just adds a reference), and
   `sane_open_impl` ties ownership of one reference to one successful
   open via a small RAII guard that is armed right after acquiring and
   disarmed only once the open is about to return successfully --
   `sane_close_impl` releases from there, also through a scope guard,
   so a throw before its USB close (sheetfed eject, park wait) cannot
   leave the reference held. A throw anywhere in between
   (USB open, `cmd_set->init`, `update_hardware_sensors`) now releases
   exactly the reference this open took, never another session's; a
   non-GL126 open never touches the lock at all. Covered by
   `test_failed_second_open_keeps_first_sessions_lock` and
   `test_release_without_acquire_is_noop` in `test_sane_lock.py`
   (6/6 passing); hardware side in Test 47.

   Documentation and code are kept in step: a hook that writes is
   enabled only together with the note here that says what guards it.

### Next: hook 2, offset calibration (scoped 2026-09-07)

Offline analysis (plan steps 1–2) written 2026-09-08:
`docs/sane-hook2-offset.md` — the full wire sequence with its one real
wait point, the failure rules, the genesys core blockers around the
hook (warmup move, home, move-to-TA) and the op-program structure with
an offline wire-equality test against the Python replayer. Five
decisions are listed at its end; they were taken the same day and the
hook is implemented offline (see the Status section of that document):
op-program generator, genesys-free runner `gl126_ops.{h,cpp}` with the
wire-equality test against the Python replayer, `offset_calibration()`
running the session preamble and the dark bracket, core gating in
`genesys_start_scan` (no home / move-to-TA for GL126, `WARMUP` flag
dropped). Hardware run done (Test 48): hook 2 complete on the unit,
offset codes within one step of the driver's, W1 = DATAENB confirmed.
Hook 3, coarse gain: offline analysis in `docs/sane-hook3-gain.md`
(2026-09-08) — same machinery, plus injections in op programs,
multi-chunk bulk reads, the warmup retry, and two more core gates
(default shading upload, second move-to-TA). Implemented offline the same
day (14/14 op tests, 163 total). Hardware run done (Test 49): gain codes
identical to the driver's. Hook 4, shading: offline analysis in
`docs/sane-hook4-shading.md` (2026-09-08) — bulk OUT, a second explicit
wait on reg 0x100, split programs for the verify pass, and the
decision that turns decision 2 around: `has_send_shading_data()` true
with a no-op send + `DISABLE_SHADING_CALIBRATION`, the vendor's shading
inside `coarse_gain_calibration()`. Implemented offline the same day
(21/21 op tests); hardware run pending.

Hook 2 is `offset_calibration()` and nothing else: the driver's
CAL_DARK_A / CAL_DARK_B phases (two dark reads at AFE offset 0x80 and
0xff) feeding `calibrate.offset_codes()` → AFE regs 5/6/7 via
0x5d/0x5e. It is the first bulk read, so it decides the GL124 bulk
sites (`scanner_interface_usb.cpp` bulk-read header, `bulk_read_data`,
the `low.cpp` valid-words / scan-count reads) for GL126. Order of work:

1. **Offline, from the captures and `tables.py`:** write down the whole
   sequence -- every register batch in order, the buffer descriptor
   (wValue 0x82, wIndex, length), the bulk-IN size, the completion
   condition -- and the driver's waits (engine-busy bit after the
   execute pulse, status word) as **explicit poll conditions with
   timeouts** (decision 3). The Python results verify the *values*
   (the driver's AFE codes and dark means for the same strip), not the
   C++ pacing: a C++ poll that returns on a different condition than the
   captured one is a new sequence and is treated as such.
2. **Offline:** the failure handling per step -- a poll timeout, a short
   bulk read, a dark level outside the bracket -- each ends the hook
   with zero further writes and a named error; no retry, no recovery.
   Offline tests against the Python driver's fake device model where the
   wire format can be compared byte for byte.
3. **Hardware, one run, operator listening:** enable the hook, run
   `scanimage` far enough to trigger calibration and stop after offset
   (the gain/shading hooks still refuse), compare the AFE codes and the
   dark means with the driver's for the same strip, then a driver `eject`.

Prerequisite: the shared lock above, implemented and checked.

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
