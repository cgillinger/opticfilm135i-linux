# of135i driver — M2 design

Goal: turn the verified replay flow into a parameterized userspace
driver (Python/pyusb) that delivers **calibrated raw 16-bit RGB**.
Image processing (negative inversion, orange mask, color) is the
application's job — the driver is a data layer, SANE backend style.

## Module layout (`driver/of135i/`)

| Module | Responsibility |
|---|---|
| `usbio.py` | Transport primitives: `write_regs(pairs)`, `read_reg(reg)` (2 B `[value, 0x55]`), `read_reg_short(reg)`, `buf_read(addr, length)` (descriptor via 0x40/0x04/0x0082 wIndex 0, then bulk IN 16 KiB chunks), `buf_write(addr, data)` (descriptor wIndex 1, then bulk OUT), `end_access(which)` (0x40/0x0c/0x008c|d), device open/close, wake/standby detection |
| `tables.py` | Derived constants: base init table (116 pairs), AFE base, exposure block 0xd1–0xf7, scan-config batches per phase, slope tables (embedded binary), FEEDL formula. Provenance: derived from vendor config tables + captures — never ship vendor files |
| `device.py` | `Scanner` class: `initialize()`, `wait_ready()`, `home()`, `goto_frame(n)`, `eject()`, `load()` (loader feed), `scan(frame, dpi=3600, ir=False)` returning raw ndarray |
| `calibrate.py` | M2a: dark/offset measurement (AFE gain 0 / offset 0xff), white line, gain/offset computation, shading measurement + correction-data synthesis, upload to scanner RAM (addr 0x10014000) |
| `image.py` | Raw assembly (pixel-interleaved RGB16 LE, width 3762 @ 3600 dpi, lines = reg 0x26:0x27), 16-bit TIFF output |
| `cli.py` | `of135i scan --frame N --dpi 3600 [--ir] -o out.tiff`, `of135i eject`, `of135i preview`, `of135i status` |

## Scan sequence (from segment 03, frames → phases)

1. **prep**: sensor regs 36/3a/33/32, reg 0x03 variants, 0x31=fe,
   ENDACCESS ×2; write scan-config table (differs from base init).
2. **afe_base**: AFE 0–7 = f8 80 1c 1c 1c 80 80 80.
3. **cal_dark/offset**: exposure block; window config; AFE gains 2–4=0,
   offsets 5–7=80 → run (0f=01) → read 3072 B. Repeat with offsets
   5–7=ff → 3072 B.
4. **cal_white**: offsets 5–7 = computed 16-bit (hi=01); window
   84=23/86=14/87=63 → run → read 31104 B (10368 px × 3 ch, u8?).
5. **cal_gain_check**: AFE gains 2–4 = computed; re-run dark pair
   (3072 B ×2).
6. **cal_shading**: offsets final; 128-line window (reg 0x27=0x80) →
   run → read 2 889 216 B; compute + `buf_write(0x10014000, 45856 B)`;
   verification pass (second 2 889 216 B read).
7. **position**: slope tables → `02=18`, FEEDL=6743 (frame 1 incl.
   scan-window offset; generic formula 6548 + (n−1)×10760 verified via
   SilverFast), `0f=01`.
8. **scan**: three slope tables (0x10000000/4000/8000), line count
   0x26:0x27, motor mode 0x30, `01=23`, `0f=01` → read image
   (223 × 519 156 B @ 3600 dpi), ENDACCESS 0x8d.
9. **park**: shutter 0x15=80, sensor regs, idle.

Register semantics captured in trace batches; port values verbatim
from trace 03 for 3600 dpi first, then generalize dpi via vendor
profile tables.

## Open M2 items

- [ ] Calibration formulas (gain from white-line level, offset from
      dark level, shading from 128-line mean) — reverse from captured
      measurement→result pairs (we hold two sessions' worth).
- [ ] Motor-busy indicator (replace captured-poll targets with a real
      busy flag; candidates: reg 0x01 bits, interrupt EP).
- [x] Loader feed sequence extraction from segment 02 (driver-managed
      magazine insertion) → `load()`. Done: `tools/load_magazine.py`.
- [ ] dpi profiles beyond 3600 (vendor table-driven).
- [ ] IR pass (register deltas known from segment 03 vs 04).
- [x] Cold-start init (power-on with no prior vendor-software
      initialization). Done 2026-09-02 (pass 15): `Scanner.cold_init()`
      reproduces the vendor's power-on homing sequence from
      `01-init.pcap` and is auto-invoked by `initialize()` when reg
      0x01==0x00. Hardware-verified end to end (power cycle → cold_init
      → load → scan with IR → eject), no VM pre-init needed. See
      docs/protocol-notes.md pass 15 for details.

## Publication rules (M3)

Never publish: vendor ini/plist/framework files, pcaps, decoded
personal photos. Derived numeric constants in our own code are fine
(interoperability). License decision: Christian's.
