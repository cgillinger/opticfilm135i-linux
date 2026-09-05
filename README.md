# opticfilm135i-linux — Linux driver for the Plustek OpticFilm 135i film scanner

![License: GPL-2.0-or-later](https://img.shields.io/badge/License-GPL--2.0--or--later-blue.svg)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Status: working prototype](https://img.shields.io/badge/Status-working%20prototype-green.svg)

**Unofficial, community-built Linux driver for the Plustek OpticFilm 135i**
(USB `07b3:1436`, Genesys Logic GL126) — a 35 mm film scanner with motorized
batch feeding that officially supports only Windows and macOS. This project
makes it scan **natively on Linux**: no VM, no vendor driver, no SANE backend
required (yet).

Keywords: *Plustek OpticFilm 135i Linux driver, film scanner Linux, 35mm
negative scanner Linux, SANE OpticFilm 135i, GL126, pyusb scanner driver*.

## What works today

- Full scan flow over raw USB (pyusb): initialization, magazine handling,
  homing, per-frame positioning, **self-computed calibration** (AFE gain from
  live white measurements, AFE offset from a two-point dark bracket,
  two-stage per-pixel shading correction) and 3600 dpi 48-bit RGB
  scanning. The dark-bracket offset reproduces the vendor's codes on the
  reference unit; it is implemented but not yet hardware-verified.
- Color-line (staggered CCD) channel alignment — no RGB fringing.
- Output as 16-bit TIFF or PNM: the raw linear negative (the driver's
  actual product: calibrated, channel-aligned, unclipped), or a preview
  positive (`--positive`, sRGB-tagged) by per-frame density inversion
  with automatic black/white points per channel. The preview is a
  convenience; colour interpretation (inversion, mask, white balance,
  tone) belongs in the application — darktable's negadoctor, VueScan, a
  SANE frontend — working from the raw negative.
- IR scanning (`--ir`): dual-light alternating capture — visible image plus an
  IR channel where only dust and scratches are visible, with per-light
  two-stage shading calibration.
- Verified against hardware: calibration values reproduce the vendor
  driver's within ±1 gain code / 0.03 % shading gain on the same unit;
  rendered output judged on par with the vendor app.
- IR-based automatic dust/scratch removal (multi-scale inpainting), on by
  default with `--ir`.
- A udev rule for running without root (`udev/60-of135i.rules`).
- A fail-closed hardware-safety layer (`of135i/safety.py`) that refuses
  every writing operation unless the scanner is in a known start state
  — verified before the USB open sequence, so a refusal sends nothing
  at all, not even `SET_CONFIGURATION` — treats a short OUT transfer as
  an unknown hardware state, prevents two processes from driving the
  scanner at once (a Linux process lock), and keeps `doctor`/`status`
  strictly read-only. The guard has held on hardware through five
  failed magazine loads (it stopped each one before the next motor
  move) and every motor completion is verified against the vendor
  capture's state class. See
  [`docs/hardware-safety.md`](docs/hardware-safety.md).

## Development status

**Phase: working prototype. Magazine loading, single-frame and
whole-strip batch scanning, and eject are the verified paths.** Scan,
calibration, IR and dust removal are stable and hardware-verified, per
frame and across a 4-frame strip in one invocation, frame for frame
against the vendor application's output of the same strip (2026-09-05).
The rough edges you should know about:

- **Eject depends on how the magazine was loaded.** Load with
  `tools/load_magazine.py` (the vendor's insert flow) and `eject` /
  `--eject` works, before or after scanning. Ejecting from other
  transport states (e.g. after the older `--full` load flow) stalled
  the mechanism with the magazine stuck part-way; recovery was a power
  cycle plus an initialization with the vendor software.
- **Speed: not tuned yet.** The driver replays the vendor's complete
  captured command stream, including every status read and the
  captured pacing between commands, because slimmed-down variants
  produced wrong calibration (see docs/protocol-notes.md). A 3600 dpi
  frame therefore takes noticeably longer than in the vendor software.
  Slow and correct first. The stream is now classified op by op in
  [`docs/replay-analysis.md`](docs/replay-analysis.md) and the first
  phase (PARK) has a semantic implementation behind `--park semantic`
  (real read-modify-write and condition waits instead of captured
  pacing); it is off by default until it has been A/B-tested on
  hardware.
- **Cold-start initialization is handled.** The driver detects a freshly
  power-cycled scanner (reg 0x01 = 0x00) and runs the vendor's cold-start
  homing sequence automatically — no VM or vendor software needed.
  Scanning immediately after a cold start used to produce flat images
  (lamp not yet warmed up, AFE gain clipped at maximum). The driver now
  waits for the lamp when the first white-line measurement says it is
  not ready: it re-measures every 5 s until two consecutive
  measurements are usable and agree within 3 %, within a bounded budget
  (60 s by default, `--warmup-budget` to change), and **fails the scan
  instead of scanning flat** if the lamp never gets there. Every
  measurement and its time go into the scan's `.diag.json`, so a scan
  from bare power-on with a generous budget is also the measurement of
  the real warmup curve. The bounded wait has not yet been exercised on
  hardware; every verified scan so far started with a warm lamp (the
  magazine load flow alone gives it about a minute).
- **After an interrupted scan, power-cycle before anything else.** A
  session that is aborted inside a phase (Ctrl-C, a crash, a USB
  timeout) can leave the scan engine running (reg 0x01 reads 0x23
  rather than the idle 0x22). Starting a new session on top of that
  state produced a motor event and a firmware hang on 2026-09-04; the
  scanner stayed on the bus but answered nothing until power was cut.
  Every command — not just `hwblock.py` — now refuses to write to a
  scanner that is not in a known start state (idle-homed `0x22`, or
  `0x00` for the cold-init path only): the state is read before the
  device is even configured, so a refusal means zero USB writes and
  zero state-changing requests, and no automatic recovery is ever
  attempted. A USB transfer that completes short is treated the same
  way as a failed one. The only fix is a physical power cycle;
  restarting the program is not sufficient. See
  [`docs/hardware-safety.md`](docs/hardware-safety.md).
- **The magazine must be loaded through the driver**
  (`tools/load_magazine.py`; the tool verifies each motor completion
  against the vendor capture's state class and loader-sensor bit and
  fails, requiring a power cycle, if the scanner answers anything else.
  The flow replays the vendor's own session from device open — its
  register table, app-start jog, a fresh insert to the stop by the
  operator, then the feed and traverse — and latched the magazine on
  hardware three times out of three on 2026-09-05, see the test log.
  The tool is interactive: run it in a terminal, it asks you to take
  the magazine out and reinsert it half-way through) — the
  autoloader is driver-managed and the
  hardware buttons are dead without a driver process. The loader sensor
  reports magazine *presence*, not that it is mechanically locked — see
  [`docs/hardware-safety.md`](docs/hardware-safety.md).
- `of135i status` reports the loader sensor (magazine present/absent)
  and button state.
- **Multiple resolutions:** `--dpi 600|1200|2400|3600|7200` — all five
  hardware-verified. All non-3600 resolutions are dual-light (IR +
  visible) passes, so `--ir` decides whether the IR channel is written
  out and used for dust removal. 7200 dpi produces a 10512 px wide,
  ~1.3 GB raw frame — expect a long scan and a lot of RAM.

## Requirements

- Linux, Python 3.10+
- `pyusb`, `numpy`
- The scanner connected via USB

## Install

```bash
git clone https://github.com/cgillinger/opticfilm135i-linux.git
cd opticfilm135i-linux
python3 -m venv .venv
.venv/bin/pip install pyusb numpy
```

## Usage

```bash
# scan frame 1 at 3600 dpi to a raw 16-bit negative TIFF (needs USB access)
sudo .venv/bin/python -m of135i scan --frame 1 -o frame1.tiff

# display-ready positive with vendor-like colors, correctly oriented
sudo .venv/bin/python -m of135i scan --frame 1 --positive -o frame1-positive.tiff

# batch: scan a whole strip in one go (rulle-f1.tiff ... rulle-f4.tiff)
sudo .venv/bin/python -m of135i scan --frames 1-4 --ir --positive --rotate 90 -o rulle.tiff

# other resolutions
sudo .venv/bin/python -m of135i scan --frame 1 --dpi 2400 --positive -o frame1-2400.tiff
```

Add `--park semantic` to any of the above for an experimental faster park phase (see docs/replay-analysis.md); the default remains the captured replay.

```bash
# eject the film magazine / check device status
sudo .venv/bin/python -m of135i eject
sudo .venv/bin/python -m of135i status

# watch mode: ejects on hardware button press, reports magazine events
sudo .venv/bin/python -m of135i watch

# read-only hardware health report
sudo .venv/bin/python -m of135i doctor --json doctor-report.json
```

`doctor` is strictly read-only on the wire (USB control reads and
descriptor queries only — no register writes, no motor commands, no
`initialize()`/`cold_init()`): it prints USB descriptor info, the
GL chip id, the derived engine state (idle-homed / cold-never-homed /
unknown from register 0x01), a dump of every register the vendor
driver itself is observed reading, magazine/button status, and host
info (Python/pyusb versions, driver git revision). `--json PATH` also
saves the report as JSON. Every `scan` additionally writes a
`<output>.diag.json` sidecar alongside each frame with the computed
calibration values (gain/offset/shading), phase timings, and poll/
mismatch counters for that frame — disable it with `--no-diag`.

To run without `sudo`, install the udev rule (see the file for the
commands): [`udev/60-of135i.rules`](udev/60-of135i.rules).

### Autonomous hardware test block

```bash
# magazine already loaded, scanner idle/homed
sudo .venv/bin/python tools/hwblock.py warm --out results/warm-01

# after a power cycle (verifies cold-start lamp warmup + AFE offset)
sudo .venv/bin/python tools/hwblock.py cold --out results/cold-01
```

`tools/hwblock.py` runs a long, unattended block of already-verified
`scan`/`eject` operations (reproducibility, batch, DPI-change, and
cold-start checks) behind a single human confirmation, recording every
image, `.diag.json` sidecar, and a `summary.json`/`report.md` report —
and stops immediately on the first anomaly.

Troubleshooting: if the device is not found, check that nothing else
holds it (a virtual machine's USB passthrough, another scanning
application) and that it has not entered standby — the scanner drops
off the USB bus after ~5 minutes of inactivity and needs a physical
power cycle to reappear. Run the offline test suite
(no hardware needed):

```bash
.venv/bin/python tests/test_offline.py
.venv/bin/python tests/test_calibrate.py
```

## How it works

The GL126 speaks the Genesys vendor protocol over USB control transfers.
The protocol was reverse-engineered from USB captures of the Windows
driver and is documented in [`docs/protocol-notes.md`](docs/protocol-notes.md)
— transport layer, register semantics, motor/positioning model (relative
FEEDL stepping from the carriage's current position, 38.0 mm frame pitch), calibration data flow, image format
(pixel-interleaved RGB16, 3762 px/line @ 3600 dpi) and the two-stage
shading correction. [`docs/driver-design.md`](docs/driver-design.md)
describes the architecture; [`docs/cal-analysis.md`](docs/cal-analysis.md)
the calibration formula reverse-engineering; [`docs/replay-analysis.md`](docs/replay-analysis.md)
classifies the replayed command stream and sets the order for turning
it into semantic register sets and explicit waits.

The driver executes the captured command stream as data tables
(`of135i/tables.py`, regenerable via `tools/gen_tables.py`) with live
calibration values injected at well-defined points. This repository
contains **no vendor code or vendor configuration files** — only derived
interoperability constants and our own code.

## Roadmap

- [x] IR channel capture (`--ir`): separate visible + IR output, dual-light calibration
- [x] IR-based automatic dust/scratch removal (multi-scale inpainting, on by default with `--ir`)
- [x] Resolution profiles: all five DPIs (600/1200/2400/3600/7200) hardware-verified
- [x] Whole-strip batch scanning (`--frames 1-4`)
- [x] Eject from a loaded magazine, before or after scanning
- [x] Magazine loading through the driver (the vendor's insert flow replayed whole; latched 3/3, 2026-09-05)
- [x] Per-frame positioning wait scaled with the move length (frame 4 of a batch was scanned mid-move before; fixed and verified against the vendor's output, 2026-09-05)
- [ ] Speed tuning (trim the replayed command stream)
- [x] Cold-start initialization from a bare power-on
- [x] udev rule for rootless operation
- [x] Loader sensor and button event reading
- [x] ICC-tagged output (`--positive` TIFFs carry an sRGB profile; raw negatives are untagged linear data)
- [ ] SANE genesys backend support for GL126 (upstream goal) — plan and hook mapping in [`docs/sane-port.md`](docs/sane-port.md); skeleton in progress

## Status & disclaimer

Working prototype, developed and tested against a single OpticFilm 135i
unit. This project is not affiliated with, endorsed by, or supported by
Plustek Inc. Use at your own risk. Plustek and OpticFilm are trademarks
of their respective owner; they are used here only to identify the
hardware this software interoperates with.

## License

GPL-2.0-or-later — see [LICENSE](LICENSE).
