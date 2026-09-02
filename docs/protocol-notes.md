# OpticFilm 135i (GL126) — protocol analysis

Status 2026-08-29: command encoding CRACKED and verified against
`Scanapi_07b3_1436.ini`. Based on `captures/segments/01-init.pcap`.

## Transport layer

The GL126 speaks the **Genesys vendor protocol over control endpoint 0**,
the same constant family as SANE's genesys backend (gl84x/gl124):

| Operation | bmRequestType | bRequest | wValue | wIndex | Payload |
|---|---|---|---|---|---|
| Register write (batch) | 0x40 (OUT) | 0x04 | 0x0083 | 0 | (reg,val) byte pairs, up to 64 B = 32 pairs |
| Register read | 0xc0 (IN) | 0x04 | 0x008e | (reg<<8)\|0x22 | 2 B response |
| Register access (short) | 0xc0 (IN) | 0x0c | 0x008e | reg | 1 B response |
| Buffer write | 0x40 (OUT) | 0x04 | 0x0082 | 1 | data (e.g. 8 B) |
| Image/table data | bulk | — | — | — | EP 0x81 IN / EP 0x02 OUT |
| Status/buttons | interrupt | — | — | — | EP 0x83 IN, 1 B |

## Cross-checks verified against the ini

- **Init register dump**: frames 130/136/140/144 in 01-init write the
  ini's `"0"` table verbatim (`01=22, 03=20, 04=02, 05=48, 06=18,
  07=00 … bf=00`) in four 64/58-byte batches. 1:1 match.
- **AFE programming**: frames 196–224 = sequence `51 <adr> 5d <hi>
  5e <lo>` — exactly `AfeWriteReg="0x0051,0x005d,0x005e"` from the ini,
  and the values match the ini's `"@"` table (AFE reg 0=0x00f8,
  1=0x0080, 2..7=0). AFE is read via 0x0050/0x0107/0x0108
  (`AfeReadReg`).
- **Status polling**: 862 reads of reg 0x01 during init (wIndex 0x0122);
  the idle loop also reads 0x31/0x32/0x35 (wIndex 0x3122/0x3222/0x3522)
  — 0x31 is the standby register (`StandbyReg=0x31,0x80`); writes
  `31 7e`/`31 fe` appear in init.
- **Motor curves**: the only bulk OUT packets with payload during init
  are identical 512 B tables of decreasing 16-bit LE step times —
  classic Genesys slope tables (cf. `Curve="30,2500,180"` in the ini).
- **Scan-start candidate**: frame 290 writes reg `0x0f=01` — in gl84x,
  0x0f is scan/motor start. To be verified against the scan segments.

## Pass 2 (2026-08-29): image data, IR, motor — SOLVED

### Image data request (question 1 — solved)
A bulk read is initiated with control `0x40/0x04/0x0082` + an **8-byte
descriptor** `[target/address u32 LE][length u32 LE]`, after which the
length is read from bulk EP 0x81 in 16384-byte chunks (last chunk
smaller). In segment 03: 223 × `00000010 f4eb0700` = read 0x7ebf4
(519,156 B) from target 0x10000000 → ≈115 MB of image data in total,
matching the segment size. Small variants for calibration data
(0xc00 = 3,072 B). Completion is marked with `0x40/0x0c/0x008c`
(BUF_ENDACCESS, wIndex 16/19).

### IR toggling (question 4 — solved)
Diff of 03 (IR off) vs 04 (IR on); registers with differing sequences:
- **reg 0x19 toggles 08/00/08/00** only in IR mode = light source
  toggle (visible light ↔ IR LED) per pass; reg 0x32 gets bit 0x20
  variants (b5/bd) = `IRMASK 0x32,0x20` confirmed.
- reg 0x0c: 0→1 (extra channel/mode bit); reg 0x26:0x27 (16-bit line
  count) doubles: 0x1411→0x297e — two full passes (MultiExposure).
- The exposure block 0xd1–0xf7 (per-channel timing) shifts
  systematically (68→a8, 99→c3, 32→51 …) = longer exposure for the
  IR pass.
- NOTE: the register space extends past 0xbf (d1–f7 are used) — the
  init table's 0x01–0xbf is only the base plate.

### Motor/feed (question 3 — solved for the eject case)
06-eject is a complete minimal command sequence:
sensor prep (36/3a/33/32 writes) → `09=08` → batch
`02=18, ae=00, af=ff, 3d=00, 3e=0c, 3f=12` → **`0f=01` = GO** →
motor runs → `09=00` (finish). Interpretation per gl84x layout:
**0x3d–0x3f = FEEDL (24-bit step count)**, 0x02 = motor mode/direction,
0x0f = execute. Init frame 270 uses the same pattern with
`3e=1a, 3f=22` (different feed length). 9 × `0f=01` in segment 03 =
calibration passes + scan pass.

## Pass 3 (2026-08-29 evening): RAM ADDRESSING — SOLVED

Source: `captures/20260829-silverfast-ramval.pcap` (48 MB; SilverFast 9
demo, prescan + scan of frames 3, 4, 1 + eject; log in
quickscan-log.md).

**The positioning model is absolute, from the home position:**

- ~~Homing after each scan: motor mode `02=0x30`, FEEDL=1, `0f=01`.~~
  CORRECTED in pass 14: mode 0x30 is the scan pass itself; there is
  no homing command, positioning is absolute.
- Positioning to frame n: `02=0x18`,
  **FEEDL = 6548 + (n−1) × 10760**, `0f=01`.
  Measured: frame 1=6548, frame 3=28052 (=6548+2×10752…10760),
  frame 4=38828 (=6548+3×10760). 10760 steps / 7200 dpi = **38.0 mm**
  = film frame pitch → FEEDL counts in 1/7200 inch (HWDPI).
  Base offset 6548 ≈ 23.1 mm (home sensor → frame 1).
- Prescan sweep: its own mode `02=0x1c`, FEEDL=71490 (long traverse).
- Eject: `02=0x18`, FEEDL=3090 (confirms the 06-eject sequence).
- Small FEEDL=1 pulses with `02=0x00` around home = sensor adjustment.
- "Reverse" does not exist as a motor command — backwards motion is
  done as homing + a new absolute feed. In practice the transport is
  still bidirectional and fast (~14 s for 4→1 incl. scan).

With this, every operation a driver needs is mapped: init, calibration
(AFE/gain framework), per-frame positioning, scan (RGB ± IR), image
data readout, homing, eject, wake/release behavior.

### Remaining open questions
1. ~~16-bit register addresses (0x101/0x107/0x108): are they read via
   the bRequest 0x0c variant or AFE-indirect?~~ — **Resolved (pass 16):**
   same wire format as normal registers but with wValue bit 0x100 set
   (0x018E instead of 0x008E); wIndex uses only the low 8 bits of the
   register address. This is the SANE GL124 extended register scheme
   (`scanner_interface_usb.cpp:52`). Reg 0x101 = the "status word" our
   `read_status()` has been reading all along (wValue=0x018E,
   wIndex=0x0122). The AfeReadReg path (0x50/0x107/0x108) is separate
   — AFE-internal, not ASIC registers.
2. ~~The calibration phase's data flow in detail~~ — mapped in pass 5.
3. ~~The magazine sensor's insertion event in 02-preview (interrupt EP +
   status bits in the reg 0x01/0x35 reads).~~ — **Resolved (pass 16):**
   see "Loader sensor and button events" section below.
4. The descriptor format's target address field (0x10000000 vs the
   0x10010400 variant) — buffer address or type flag?
5. ~~Where is motor-busy visible?~~ — **Partially resolved:** reg 0x01
   bit 0x20 clears during engine execution (0x22→0x02) and sets on
   completion (pass 4). Interrupt EP 0x83 delivers button and sensor
   events but not a motor-done signal.

## Working method (repeatable)

```bash
# register writes in a segment:
tshark -r SEG.pcap -Y "usb.bmRequestType == 0x40 && usb.setup.bRequest == 4 && usb.setup.wValue == 0x0083" \
  -T fields -e frame.number -e usb.setup.wLength -e usb.data_fragment
# register reads (reg = wIndex >> 8):
tshark -r SEG.pcap -Y "usb.bmRequestType == 0xc0 && usb.setup.bRequest == 4" \
  -T fields -e frame.number -e usb.setup.wIndex -e usb.data_fragment
```

## Pass 4 (2026-08-30): REPLAY PoC — VERIFIED AGAINST HARDWARE

`of135i_poc.py` (pyusb, venv `.venv/`) run on the mintuu host with the
scanner released from the VM. Results:

- **Read format confirmed**: `0xc0/0x04/0x008e`, wIndex `(reg<<8)|0x22`
  gives a 2 B response `[value, 0x55]` — the second byte is a constant
  ack (0x55).
- **probe**: reg 0x01=0x22 (the init table value), 0x31=0xfc,
  0x32=0x95, 0x35=0xbb in the post-VM state.
- **init**: the 116-pair table + the AFE sequence (51/5d/5e) written;
  readback exact for 0x03/0x05/0x06/0x1e/0xb9. Reg 0x31: wrote 0x80,
  read 0xfe (=0x7e|0x80) — the low bits are hardware/driver managed,
  matching the init capture's `31 7e`/`31 fe`. Not a read error.
- **home** (0x30, FEEDL=1): inaudible, reg 0x01 unchanged at 0x22 —
  no motor-busy bit identified in 0x01 yet (open question 5).
- **eject** (0x18, FEEDL=3090, 0f=01): the magazine was physically
  ejected. Motor protocol proven.

### Button finding (2026-08-30)
The physical eject button is DEAD without a driver — the magazine could
not be ejected with the button while no ScanApi/PoC process held the
device, but the PoC's eject command worked immediately. Conclusion: the
buttons are handled by the driver (polling of the ButtonID registers /
interrupt EP 0x83), not autonomously by firmware. Requirement for a
future Linux driver: button polling. Must be included in the Hamrick
material.

### Loader state without a driver (2026-08-30)
A cassette inserted without a driver is NOT pulled in (the autoloader
is driver-managed). Blind home/eject via the PoC does not recover from
an undefined mechanical state — **a power cycle is the safe reset**:
firmware runs its own transport init at power-on and ejected the
cassette. A future driver must handle insertion events
(LoaderSensorReg/LoaderInterruptReg + interrupt EP) for the autoloader
to work.

## Pass 5 (2026-08-30): IMAGE FORMAT CRACKED + replay tooling built

**Image format** (verified by decoding segment 03's captured image data
into a recognizable photo, `decoded-frame1-positive-preview.png`):
- Pixel-interleaved RGB, 16-bit little-endian, straight up — no planar
  split, no visible stagger correction needed.
- 3762 px/line × 5137 lines @ 3600 dpi = 26.5 × 36.2 mm.
- Line count = reg 0x26:0x27 (0x1411 = 5137). Line length 22572 B.
- Total 115,952,364 B = exactly 5137 × 22572.

**Calibration flow** (open question 2 — now mapped from the 03
sequence): dark/offset passes with AFE gain 0 and offset 0xff
(2×3072 B), white line 31104 B (=10368 px × 3 channels = PageSize),
computed gains (AFE 2-4) + offsets (AFE 5-7, 16-bit with hi byte),
2×2.89 MB shading measurement, and the driver writes back 45,856 B of
shading correction data to scanner RAM (bulk OUT, addr 0x10014000).

**Tools** (in the analysis directory):
- `compile_trace.py` — pcap segment → replayable command trace
  (JSON.gz; coalesces status polling into poll ops with target state).
  Compiled traces in `traces/`: 01-init (573 ops), 02-preview-magasin
  (3469 ops, incl. driver-managed magazine feed), 03-singel-3600
  (9540 ops, 121.8 MB bulk-in).
- `replay_trace.py` — executes a trace against the scanner (pyusb):
  cw/cr/poll/bo/bi, buffers saved per descriptor, mismatch log.
- `decode_image.py` — raw → PNG (negative/positive, normalization).

## Pass 6 (2026-08-30): FULL SCAN FLOW NATIVE ON LINUX — GOAL REACHED

Hardware replay on mintuu (scanner disconnected from the VM, power
cycled, magazine with negatives loaded):

- `of135i_poc.py init` (base register table) replaces the 01-init
  trace (that trace contains enumeration debris; unnecessary — 03
  rewrites the config anyway).
- Replay 02-preview: 100 s, 24 MB, 38 benign mismatches (status bits;
  cassette already inserted). The magazine WAS FED IN and the preview
  sweep produced real images of the strip (line period 5184 u16 =
  1728 px RGB16).
- Replay 03-scan: complete — 223×519,156 B = 115,771,788 B exactly,
  0 bulk timeouts. Decoded into a full photo (frame 1), saved as
  `replay-frame1-fullres-positive.png` + `replay-frame1-raw.bin`.

Quality note: the calibration values are replayed from the previous
day's session (AFE gains/offsets + shading data) — works, but a real
driver needs its own calibration computed from the measurement passes
for optimal image quality.

Publication rules: pcaps and decoded images are PRIVATE (personal
photos). Plustek's ini/plist/framework must not be published — derived
constants in our own code are fine (interoperability).

**Architecture decision (2026-08-30):** the driver is a middle layer
that delivers calibrated raw data (16-bit RGB); image processing
(negative inversion, orange mask, color) belongs in the application
layer (VueScan, darktable, digiKam). Same model as SANE
backend/frontend. All comments and documentation in this project are
in English (GitHub publication ahead).

## Pass 7 (2026-08-30): segment 02 motor map (loader + preview)

Motor executions in the 02-preview trace (op indices in
`traces/02-preview-magasin.trace.json.gz`):

- op 27→43: mode `02=0x18`, FEEDL=0x1a22 (6690) → run. **Magazine
  feed-in** (~23.6 mm; same FEEDL as init frame 270 — the load op).
- op 46→56: mode `02=0x1c`, FEEDL=0x011742 (71490) → run. Full-strip
  **preview sweep**; data arrives as 42 × 518,400 B chunks
  (line = 5184 u16 = 1728 px RGB16).
- mode `02=0x78`: appears around loader events (op 103, 2160) with
  FEEDL=1 — loader-specific mode (sensor-gated?), plus small
  mode-0x00 FEEDL=1 pulses = sensor adjustment.
- op 1010: mode 0x18 FEEDL=0x190e (6414) → run; op 1037: home 0x30.
- op 2233: mode 0x18 FEEDL=0x430e (17166) → run; op 2260: home —
  likely the mid-segment magazine reload the capture contains.

For `Scanner.load()`: watch loader sensor (ini: LoaderSensorReg=
0x101,0x08; LoaderInterruptReg=0x32,0x01), then feed mode 0x18
FEEDL≈0x1a22, home. To be refined when wiring device.py.

## Pass 8 (2026-08-30 afternoon): M2 hardware bring-up findings

- **Motor/engine busy bit FOUND (open question 5 resolved)**: bit 0x20
  of reg 0x01 clears while the scan/motor engine executes (0x22 → 0x02
  observed during homing) and sets again on completion. A driver can
  wait on this instead of mimicking captured poll timing.
- **Session state matters**: after a VM/vendor-driver session the
  scanner keeps its register state; a driver must program the full
  base init table + AFE base at open (of135i_poc-style), not just the
  pre-scan phases, or calibration runs in an undefined state.
- **Canonical start position**: every captured flow starts from a homed
  transport. A firmware-side magazine feed parks elsewhere → home
  before scanning.
- **Slimmed-down phase execution is NOT sufficient**: executing only
  the register batches + poll subset yields saturated white cal and
  wrong shading levels, while the verbatim replayer (full op stream:
  all single reads, exact ordering, captured dt pacing) reproduces
  capture-identical calibration levels on the same night/film. Some of
  the ~900 "status" reads and/or the pacing carry required state
  transitions (lamp settling is the prime suspect). Driver decision:
  phases carry the full op stream; the executor uses replayer
  semantics + the real engine-busy wait. Isolating WHICH ops matter is
  future work (bisection), not needed for correctness.

## Pass 9 (2026-08-30): SECOND SHADING UPLOAD DECODED — M2 complete

Root cause of the all-zero images from the driver's own calibration:
the vendor flow uploads shading TWICE, and upload #2 was being built
with upload #1's recipe (white means as offsets → the scanner
subtracted away the whole signal).

**Upload #2 format** (frames 2499-2505, 46080 B wire = 45856 B data +
USB 512-padding; same block format as upload #1):
- offset field = IDENTICAL to upload #1 (the dark map, 100% equal)
- gain field = per-pixel white uniformity:
  **gain = T_c × 0x4000 / (white_mean − offset)**, per-channel targets
  T = (81752, 83490, 87083); reproduces the vendor table with
  cv 0.0003-0.0004 (our implementation: offsets ±7, gains ±37 ≈ 0.03%).

**Other execution lessons (full-stream driver bring-up):**
- The engine-idle wait (reg 0x01 bit 0x20) must NOT be applied inside
  the trace stream: the trace's own polls carry the timing, and during
  the image pass the scanner streams while bulk reads drain it —
  waiting for idle first overflows the buffer and yields stale RAM.
  Keep the wait only for out-of-trace motor moves (home/eject).
- Verified end-to-end 2026-08-30: `of135i scan --frame 1` produces a
  clean, correctly calibrated 3600 dpi image with ALL calibration
  computed from live measurements (gains 0x2f/0x21/0x28 vs vendor's
  0x2e/0x21/0x29 on the same hardware).

## Pass 10 (2026-08-30): color pipeline — learned LUT against vendor rendering

- **Orientation**: the raw sensor image is MIRRORED relative to vendor
  output (vendor ini `HorizontalMirror=1`) and rotated; vendor-matching
  orientation = rot270 + horizontal mirror. Applied by the CLI's
  `--positive` path.
- **Color rendering**: fitted per-channel LUTs (raw u16 → display u8)
  against the vendor app's (QuickScan) rendering of the SAME frame:
  1:1 scale (both 3600 dpi), alignment by NCC search (best offset
  NCC 0.952), then quantile-binned median mapping, monotone-enforced.
  Captures inversion + orange mask + tone curve in one mapping;
  visually indistinguishable from the vendor rendering. Shipped as
  `driver/of135i/data/negative-color-lut.npy` (tone curves only, no
  image content); `image.to_positive()` uses it when present, generic
  density inversion as fallback. Fitting tooling lives in session
  history / cal-data (fit-inputs, align.npz).
- Serious color work remains app-layer (darktable etc.) — this is the
  everyday convenience path.

### Channel alignment (pass 10 addendum, 2026-08-30)
The raw image has the color channels offset along the scan axis:
R lags G by 12 lines and B leads G by 12 lines at 3600 dpi (= vendor
ini LineSpace=-24 at the 7200 dpi base; measured empirically, residual
0 with corr 0.94-0.98 after correction). Without correction: strong
RGB fringing ("double exposure" look), dust specks appear as three
colored blobs. No even/odd pixel stagger at 3600 dpi (StaggeredLine
applies at 7200). Implemented as image.align_channels(dpi), applied by
the CLI after assemble. Remaining cosmetic gap vs vendor apps: IR
dust removal (not yet implemented).

## Pass 11 (2026-08-30): frame selection verified + IR data structure

- **Frame selection VERIFIED on hardware**: `scan --frame 2` (FEEDL
  17503) and `--frame 4` (FEEDL 39023) returned the correct motifs on
  a 4-frame strip (user-confirmed against known frame contents). The
  FEEDL formula 6743 + (n−1)×10760 holds across the strip.
- **IR scan data structure** (analysis of segment 04, details in
  cal-data/ir/ir-analysis.md): visible and IR are captured as
  ALTERNATING LINES — even index IR (R≈G≈B, flat + dust specks), odd
  index visible. Line width 5184 px (31104 B, raw sensor width; the
  non-IR scan windows to 3762 px). 659 chunks × 497664 B = 16
  lines/chunk = 10544 lines exactly (5272 + 5272). A 660th descriptor
  is issued but cancelled by the vendor driver. Open question: why the
  IR mode scans at full sensor width while plain mode windows.

## Pass 12 (2026-08-30): IR MODE WORKING — dual-light scanning verified

`scan --ir` verified on hardware: visible pass with correct orange-mask
channel separation, IR pass at mean ~50k = a near-uniform bright field
where ONLY dust and scratches are visible (image dyes transparent to
IR). Three findings on the way:

1. **IR-mode initialization matters**: trace 04's own PREP/AFE_BASE
   phases carry the IR-LED setup. Running only the plain (trace 03)
   prep gives an IR pass ~7x too dark with residual image structure.
   `initialize(ir=True)` runs the IR phase set.
2. **Dual shading tables**: IR mode calibrates BOTH light sources —
   256-line alternating dark + white measurements, two uploads per
   pass (0x10014000 = visible, 0x10034000 = IR), 63,192 B each
   (15,552 pairs = full 5184-px sensor width × RGB). Upload-2 gain
   targets derived from the vendor payloads:
   visible T=(53928, 74096, 61728), IR T=(100287, 72574, 87480)
   (gain = T×0x4000/(white−offset), cv 0.034-0.043).
3. IR mode scans at full sensor width (5184) with alternating
   visible/IR lines; frame-1 FEEDL=6746 (vs 6743 in plain mode).

## Pass 13 (2026-08-30 evening): THE LOAD PROTOCOL — scan-refusal root cause

Symptom: scans from a self-loaded magazine ran the motor but never
streamed image data (bulk-in timeout), plain and IR alike, while the
identical driver code worked on a vendor-loaded magazine.

Root cause, established with a targeted capture of the vendor app's
insert flow: loading is a three-stage, sensor-gated protocol, and a
bare mode-0x18 feed (our first loader) establishes none of the state:

1. **The user must push the cassette in** — the vendor driver idles
   until the loader sensor fires (reg 0x32 flips 0x1f→0x5b in the
   vendor session's register state; the driver acks with 0x32=0x1d).
   At app start the vendor also probes with feed+eject (the "jog").
2. **Then**: feed (0x18, FEEDL 0x1a22, speed regs 0x7d-0x7f + two
   slope tables — a naked register poke without them stalls the motor
   with a grinding noise), slow prescan traverse (0x1c, FEEDL 71490),
   six loader pulses (0x78, FEEDL 1), and draining the ~16 MB preview
   the traverse produced.
3. Only after this does the scan engine stream data.

Implemented as a verbatim replay (load_magazine.py, ops 790-3280 of
the load-only capture trace) with user-confirmed start. OPEN: reading
the loader sensor in OUR register state (0x32 reads 0x9d raw / 0x1d
with sensor-prep writes, and never changes on insertion — the real
sensor is likely 16-bit reg 0x101 bit 0x08 per the vendor config,
i.e. open question 1). Verified end-to-end: full IR scan with all-own
calibration and processing produced a clean final image immediately
after this load.

## Pass 14 (2026-09-02): INTER-FRAME SEQUENCE — batch root-cause analysis (offline)

Source: `captures/20260829-silverfast-ramval.pcap` compiled with
`tools/compile_trace.py` (28,086 ops). SilverFast scans frames 3, 4, 1
IN SEQUENCE without ejecting — the vendor reference for what a driver
must do between two frames. Pure offline analysis; no hardware touched.

**Correction to pass 3/4: mode 0x30 is the SCAN PASS, not a homing
command.** The batch `1c=00 02=30 3d/3e/3f=1 7d-7f 8a-92 ae/af ac/ad
0d=07 28-2b 25-27=<line count> 05=40` + `01=23` + `0f=01` is exactly
the batch that starts image streaming (tables.SCAN op 14, where the
line count is injected). FEEDL=1 there means one motor step per line.
"Homing after each scan" in pass 3 was this scan pass misread. The
lone `02=30` inside PARK is a plain mode-register write with no
execute pulse.

**There is NO standalone homing command anywhere in the vendor flow.**
Positioning is one mode-0x18 batch with the absolute FEEDL, issued from
wherever the carriage currently is, forwards or backwards:

| move | from | FEEDL | busy-poll duration |
|---|---|---|---|
| frame 3 | home (after load) | 28052 | 5.9 s |
| frame 4 | end of frame 3 | 38828 | 8.1 s |
| frame 1 | end of frame 4 (backwards) | 6548 | 1.6 s |

The firmware resolves the move itself (home sensor and/or tracked
position). Our POSITION phase is this batch verbatim (speed regs
0x7d-0x7f, two slope tables, `0f=01`), so it is valid from any start
position.

**The vendor inter-frame sequence (identical between 3→4 and 4→1):**

1. Scan pass ends → buffer drain (5 × 521,856 B + remainder) →
   PARK: `wv=0x8d`, `03=30`, `03=20`, `01=22`, `15=80`, `02=30`,
   `03=10`, `03=00` (lamp off), two `wv=0x8b` writes, `32=81`, `35=bb`.
   Byte-for-byte our tables.PARK (the `0x8b` payloads vary per scan:
   `16000100`/`80ff` here vs `0c000100`/`e0ff` in trace 03).
2. Idle loop (`36=fc 3a=00 36=fc 33=0e 32=8d` + read 0x32) until the
   user starts the next frame.
3. Re-init: `32=8f`, read 0x01, ENDACCESS 16/19, `03=20`/`03=30`
   twice, read 0x01, `31=fe`, ENDACCESS 16/19 (= our PREP, which has
   the 03 toggle once) → the full base register table incl. `03=30`
   (lamp on) + AFE base regs (= our AFE_BASE table; only 0x3b/0x3c
   differ, 02/00 vs 00/01, a SilverFast-vs-QuickScan scan parameter,
   not an inter-frame thing) → ~1.25 s of reads of 0x01 (0x22 → 0xdc,
   as in our AFE_BASE poll) → `03=20`, `03=30` → exposure block
   0xd0-0xf8.
4. Calibration — frames 3 and 1 run the full dark/white/gain/shading
   flow; **frame 4 skips it entirely** and just rewrites the AFE gain/
   offset codes from frame 3 (`2e/22/29`) before positioning. Shading
   data in scanner RAM survives PARK. Calibration per frame is a
   driver policy, not a hardware requirement.
5. Position (mode 0x18, absolute FEEDL) → scan pass (mode 0x30) →
   drain → PARK. No motor command other than these two.

The vendor does NOT rewrite the power-on table (tables_base.
BASE_INIT_PAIRS) between frames; that table only appears at device
open (01-init).

**Diff against our `--frames` loop (cli.py + device.py.scan()):**

1. **`Scanner.home()` before calibration has no vendor counterpart.**
   It executes mode 0x30 with FEEDL=1 — i.e. a scan pass — with
   whatever regs 0x25-0x27/0x0d hold at the time. After frame 1's
   SCAN those are still the frame's line count (5137) and 0d=07, and
   PARK does not reset them, so frame 2's "home" is a full-length
   engine run that leaves undrained data in the buffer before the
   dark/white calibration reads. On frame 1 the same call is harmless
   only because the preceding load/preview flow leaves 0x25-0x27 = 1.
   The observed frame-2 symptom (gain codes 0x3f = the white
   measurement read as dark) is consistent with the calibration
   reading that stale buffer, but this is the diff, not a hardware-
   verified cause.
2. `initialize()` per frame writes BASE_INIT_PAIRS (power-on table:
   02=78, 3b/3c=ff, 7e/7f=15/7c, 32=1f, 33=8e ...) before PREP/
   AFE_BASE. Every one of those registers is rewritten by the
   AFE_BASE table 0.1 s later, so the end state matches the vendor's,
   but the vendor never issues these transient values between frames.
   Lower-ranked candidate for the grinding noise in the init-per-frame
   attempt (2026-09-01); the extra 0x30 execute (item 1) ran in that
   attempt too.
3. Our PREP toggles `03=20/30` once, the vendor twice, and the vendor
   precedes PREP with `32=8f` from its idle loop. Cosmetic.

**Implication for the driver (next iteration, needs hardware):** drop
the home() call from scan() (or reduce it to what the vendor does:
nothing), keep initialize() per frame but without BASE_INIT_PAIRS, and
let POSITION move the carriage from wherever the previous frame left
it. Optionally reuse calibration across frames as SilverFast does.

### Pass 14 addendum (2026-09-02): batch VERIFIED on hardware, eject fixed

- `scan --frames 1-2` and then `--frames 1-4 --eject` with the pass-14
  changes (no home() before calibration, power-on table once per
  session): all frames calibrated normally (gain codes 0x2e-0x2f/0x21/
  0x28 on every frame, vs 0x3f on frame 2 the day before), four clean,
  distinct images. Batch scanning works.
- **Eject stalled** with a grinding noise after the 4-frame batch and
  left the magazine locked (power cycle recovered it: the firmware's
  power-on transport init ejected the cassette). Cause, from the
  capture: the vendor's eject is `33=8e`, `32=8f`, `09=08`, batch
  `02=18 ae=00 af=ff 3d/3e/3f=3090`, **the positioning slope table
  uploaded to 0x1000c000 and 0x10010000**, `0f=01`, busy-poll, `09=00`.
  Our _motor_run() skipped the two table uploads. It worked on earlier
  days only because a positioning move had left the same table in RAM;
  after a scan pass (which uploads its own table to other addresses
  and streams through the buffer) it was gone. _motor_run() now
  uploads the tables.
- **Re-test with the tables (load_magazine.py, then `eject`, same
  session): STILL STALLS.** Feed-in, a short reverse move, then a
  short harsh motor sound; the button went orange (firmware: ejected)
  but the magazine was mechanically stuck. Registers read healthy
  afterwards (0x01=0x22). Power cycle alone did not free it either
  time; QuickScan's init in the VM did. So the slope tables were not
  the (only) missing piece. Facts to work from: both stalls happened
  with the carriage at a position the vendor never ejected from
  (after a 4-frame batch at frame 4; right after a load at home),
  while the ramval eject ran after a frame-1 scan + PARK, and the
  06-eject segment after a single scan. Mode 0x18 is absolute (pass
  14), so FEEDL 3090 is a target position, not a distance — from the
  loaded/home position that may be a move INTO the film path.
- **Resolution (offline, same day): the vendor has TWO eject variants
  and we had copied the wrong one for our situation.**
  - Post-scan eject (06-eject, ramval end): `09=08`, batch
    `02=18 ae/af FEEDL=3090`, positioning slope table (f5d7...), fast
    speed regs left over from the scan flow (7e/7f=36/b0), `0f=01`.
  - Loaded-magazine eject — the vendor app's start-up "jog" (feed 6690,
    then eject 3090; load-only capture ops 100-169, ramval ops 736-758,
    26 identical occurrences): `32=<ack>`, `09=08`, batch
    `1c=00 02=18 FEEDL=3090 7d=00 7e=75 7f=30 8a-92=00 ae=00 af=ff`, a
    DIFFERENT 512 B slope table (c7e5ef43...) to 0x1000c000 and
    0x10010000, `0f=01`, busy-poll (~0.9 s), `09=00`. Slower speed
    profile, and the same table/profile the magazine feed itself uses.
    This is what freed the stuck magazine three times today (vendor
    init in the VM). Now shipped as tables_base.SLOPE_TABLE_LOADER +
    LOADER_SPEED_PAIRS and Scanner.eject(); wire sequence verified
    byte-identical against the capture offline.
- Also corrected the positioning model: the busy-poll durations of the
  three ramval positioning moves (28052: 5.9 s, 38828: 8.1 s,
  6548: 1.6 s) scale linearly with FEEDL, i.e. every move starts from
  the same place. Together with frame 1 being scanned after frame 4
  with a plain forward feed, this means the transport is back at home
  after each scan pass (firmware-side; no command for it in the
  capture). "Absolute FEEDL" in practice = relative feed from home.

### Pass 14 addendum 2 (2026-09-02, afternoon): EJECT SOLVED — it is the start state

Targeted capture `20260902-vendor-eject-from-loaded.pcap` (usbmon on
the Linux host while the vendor app ran in the VM): app init (jog:
feed 6690, feed 6690, eject 3090), then a user insert -> the vendor's
load = `32=1d` ack, feed 6690, prescan traverse 71490 (loader speed
profile + loader slope table), then ONLY its idle loop (`32=05` acks),
then the physical eject button -> the same 3090 eject batch as above.
No loader pulses, no calibration pulses, no preview pass.

So the 2026-08-30 "load-only" capture that load_magazine.py replayed
(ops 790-3280) was the vendor's load PLUS its preview preparation (six
mode-0x78 pulses, calibration pulses) minus the preview pass. All
three stalled ejects were issued from that end state, which the vendor
never ejects from. The eject batch itself was never the problem: the
loaded-state variant is byte-identical to ours.

Verified on hardware, same day:
- load (feed + traverse only) -> eject: magazine out, orange button.
- load -> `scan --frame 1 --eject`: correct frame, normal calibration
  (gains 0x2f/0x21/0x28), eject completed in <1 s, registers healthy
  (0x01=0x22) afterwards.

Completion poll for the eject: the vendor reads a status word via
wValue 0x018e / wIndex 0x0122 (NOT the 0x008e register read) and
continues at 0xf8 after 0xd9 -> 0xf9 (~0.9 s). The 0x008e reg-0x01
engine bit does not set after an eject (0x02 for 90 s on a successful
eject), so eject() now polls the 0x018e word for (v & 0x21) == 0x20.
Open: what the 0x018e read space is (it also carries the 0xdc/0xec
values the calibration polls wait for).

load_magazine.py now defaults to the feed+traverse flow (`--full` keeps
the old one for reference).

### Pass 15 (2026-09-02, evening): cold-start init SOLVED — motor homing from power-on

**Problem:** after a fresh power cycle (reg 0x01=0x00, 0x32=0xc2), our
init assumed an already-homed scanner (0x01=0x22, 0x32=0x1f). Every
successful run up to this point required vendor-software pre-init in a
Windows VM.

**Source:** vendor cold-start sequence from USB capture `01-init.pcap`
(segment from session 1, captured directly after power-on).

**Vendor's cold-start structure** (573 ops total):

1. GL chip handshake: read 0x008a at wIndex=0x26fe (chip ID, returns
   0x01), write 0x008b at same wIndex.
2. Status word (0x018e) poll: initial 0x4855, settles to 0xf055.
3. Cold register table: 125 pairs, same as BASE_INIT_PAIRS except 5
   value differences (0x7e=0x75, 0x7f=0x30 = loader motor speed;
   0x4f=0x63 vs 0x03; 0x3b=0x00, 0x3c=0x00 vs 0xff) plus 9 extra regs
   0x8a-0x92 (motor slope params, 0x91=0x2a, 0x92=0xf8). Shipped as
   `COLD_INIT_PAIRS` in tables_base.py.
4. end_access(0x8c, 16), end_access(0x8c, 19).
5. AFE access enable (0x0b=0x64, 0x13=0x0f, 0x0b=0x6c), EEPROM read
   (3 B: c84013; 19 B: mostly ff), AFE reset (0x03 toggle 0x10->0x00),
   AFE base values (regs 0-7 via SPI).
6. Interrupt/sensor register toggle: reg 0x31 bit 0x80 clear then set.
7. Motor homing: 3 rounds x 3 moves each (9 GO commands total; vendor
   capture shows 10 -- an extra short move between rounds 2->3 which
   we omit). Each round: pre-move sensor prep (0x32 bit manipulation,
   0x36=0xfc, 0x33=0x8e), then:
   - Move 1: feedl=8730 (0x3e=0x1a, 0x3f=0x22), loader slope table to
     0x1000c000 + 0x10010000, GO, poll ~1.6 s
   - Resync: 0x09=0x00, 0x35 bit 0x40 clear (0xfb->0xbb), 0x32 bit 1
     dance
   - Move 2: same feedl=8730, full 19-reg speed profile rewrite
   - Move 3: feedl=4620 (0x3e=0x0c, 0x3f=0x12), poll ~0.9 s
   - Settle: motor disable, poll 0x35/0x32 alternating until 0xbb/0x1f
     (~30 iterations)
   Between rounds: full register table + AFE sequence rewritten.
8. End state: 0x01=0x22, 0x35=0xbb -- scanner homed, ready for
   magazine load + scan.

**Implementation:** `Scanner.cold_init()` in device.py. Auto-detected
by `initialize()`: reads reg 0x01, calls `cold_init()` when value is
0x00 (fresh power-on, never initialized).

**Hardware-verified 2026-09-02:** power cycle -> cold_init() ->
load_magazine -> scan frame 1 with IR + dust removal -> correct TIFF
output -> eject. No VM init needed. Button went orange (ready) after
eject.

**Known deviations from vendor capture:**
(a) 9 homing moves instead of 10 (missing extra short move between
rounds 2-3 -- scanner homes correctly regardless);
(b) LOADER_SPEED_PAIRS writes 0x91=0x00/0x92=0x00 in move 2's
speed-profile rewrite where the vendor's rounds 1-2 use
0x91=0x2a/0x92=0xf8 (COLD_INIT_PAIRS sets them at round start; no
observed impact);
(c) several poll timeouts on status word (0xf055 vs expected 0xf855)
and settle register (0x32=0x5d vs 0x1f) during the no-magazine cold
test -- the subsequent with-magazine scan succeeded regardless.

## Pass 16 (2026-09-02): LOADER SENSOR + BUTTON EVENTS — hardware-verified

### Extended register addressing (GL124/GL126)

Registers above 0xFF use the same control transfer as normal reads but
with wValue bit 0x100 set. For reading reg N (N > 0xFF):

    bmRequestType = 0xC0 (IN)
    bRequest      = 0x04
    wValue        = 0x018E (= 0x008E | 0x100)
    wIndex        = ((N & 0xFF) << 8) | 0x22
    wLength       = 2
    reply         = [value, 0x55]

For writing: wValue = 0x0183 (= 0x0083 | 0x100), data = [N & 0xFF, value].

This is the same scheme SANE's GL124 backend uses
(`scanner_interface_usb.cpp`). Our `read_status()` / `read_status_word()`
already read reg 0x101 via this path (wValue=0x018E, wIndex=0x0122).
Generalised as `read_ext_reg(reg)` in usbio.py.

### Loader sensor (reg 0x101 bit 0x08)

Vendor config: `LoaderSensorReg=0x101,0x08`.

| State              | reg 0x101 | bit 0x08 |
|--------------------|-----------|----------|
| No magazine        | 0xe0      | 0        |
| Magazine at stop   | 0xe8      | 1        |
| After eject        | 0xe0      | 0        |

Hardware-verified 2026-09-02. Polarity: bit set = magazine physically
present in the slot.

Reg 0x32 also reflects magazine state (0xdb without, 0x9f with magazine)
but with many co-varying bits. The vendor config names it
`LoaderInterruptReg=0x32,0x01` (bit 0). Bits 3-4 (0x18) of reg 0x32
are sensor-dependent and must be masked in trace-replay polls to avoid
timeouts.

### Reg 0x101 bit map (GL126, partial)

From SANE GL124 register definitions and hardware observation:

| Bit  | Mask | Name (GL124)  | Observed role (GL126)        |
|------|------|---------------|------------------------------|
| 7    | 0x80 |               | set in idle states           |
| 6    | 0x40 |               | set in idle states           |
| 5    | 0x20 |               | set in idle/done states      |
| 4    | 0x10 | CHKVER        | state-class marker           |
| 3    | 0x08 | DOCJAM        | **loader sensor** (magazine) |
| 2    | 0x04 | HISPDFLG      |                              |
| 1    | 0x02 | MOTMFLG       | motor move in progress       |
| 0    | 0x01 | DATAENB       | data ready                   |

Upper nibble state classes (observed in status-word polls):
- 0x9x: busy (scanning)
- 0xDx: transitioning (motor starting/stopping)
- 0xEx: transitioning (late phase)
- 0xFx: done/idle

### Button / interrupt endpoint (EP 0x83)

The scanner has one interrupt IN endpoint (EP 0x83, 1 byte). The
vendor driver polls it in a loop; when no event is pending, the read
times out with 0 bytes. Known event codes:

| Code | Event                         | Observed in          |
|------|-------------------------------|----------------------|
| 0x48 | Eject button pressed          | 06-eject, silverfast, vendor-eject captures |
| 0x04 | Loader sensor state change    | 01-init (during homing with magazine)       |

The eject button is DEAD without a driver process holding the USB
interface — buttons are software-handled, not firmware-autonomous
(confirmed pass 4). The `of135i watch` command polls EP 0x83 and
performs an eject on 0x48 events.

Stale events can accumulate in the endpoint's queue (e.g. a 0x04 left
over from cold_init homing); the first `read_button()` drains them.
