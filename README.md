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
  live dark/white measurements, two-stage per-pixel shading correction;
  AFE offset is currently a hardcoded empirical value) and 3600 dpi
  48-bit RGB scanning.
- Color-line (staggered CCD) channel alignment — no RGB fringing.
- Output as 16-bit TIFF or PNM: raw negative, or a display-ready positive
  (`--positive`, sRGB-tagged) via a learned tone-curve LUT that matches
  the vendor application's color rendering.
- IR scanning (`--ir`): dual-light alternating capture — visible image plus an
  IR channel where only dust and scratches are visible, with per-light
  two-stage shading calibration.
- Verified against hardware: calibration values reproduce the vendor
  driver's within ±1 gain code / 0.03 % shading gain on the same unit;
  rendered output judged on par with the vendor app.
- IR-based automatic dust/scratch removal (multi-scale inpainting), on by
  default with `--ir`.
- A udev rule for running without root (`udev/60-of135i.rules`).

## Development status

**Phase: working prototype. Single-frame and whole-strip batch scanning
are the verified paths.** Scan, calibration, IR and dust removal are
stable and hardware-verified, per frame and across a 4-frame strip in
one invocation. The rough edges you should know about:

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
  Slow and correct first; trimming the stream is future work.
- **Cold-start initialization is handled.** The driver detects a freshly
  power-cycled scanner (reg 0x01 = 0x00) and runs the vendor's cold-start
  homing sequence automatically — no VM or vendor software needed.
  However, scanning immediately after a cold start may produce flat images
  (lamp not yet warmed up); a warm-up delay is planned but not yet
  implemented.
- **The magazine must be loaded through the driver**
  (`tools/load_magazine.py`) — the autoloader is driver-managed and the
  hardware buttons are dead without a driver process.
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

# eject the film magazine / check device status
sudo .venv/bin/python -m of135i eject
sudo .venv/bin/python -m of135i status

# watch mode: ejects on hardware button press, reports magazine events
sudo .venv/bin/python -m of135i watch
```

To run without `sudo`, install the udev rule (see the file for the
commands): [`udev/60-of135i.rules`](udev/60-of135i.rules).

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
the calibration formula reverse-engineering.

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
