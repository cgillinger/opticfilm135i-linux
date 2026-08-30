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
  homing, per-frame positioning, **self-computed calibration** (AFE gain and
  offset from live dark/white measurements, two-stage per-pixel shading
  correction) and 3600 dpi 48-bit RGB scanning.
- Color-line (staggered CCD) channel alignment — no RGB fringing.
- Output as 16-bit TIFF or PNM: raw negative, or a display-ready positive
  (`--positive`) via a learned tone-curve LUT that matches the vendor
  application's color rendering.
- IR scanning (`--ir`): dual-light alternating capture — visible image plus an
  IR channel where only dust and scratches are visible, with per-light
  two-stage shading calibration.
- Verified against hardware: calibration values reproduce the vendor
  driver's within ±1 gain code / 0.03 % shading gain on the same unit;
  rendered output judged on par with the vendor app.

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

# eject the film magazine / check device status
sudo .venv/bin/python -m of135i eject
sudo .venv/bin/python -m of135i status
```

A udev rule to avoid `sudo` is on the roadmap.

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
— transport layer, register semantics, motor/positioning model (absolute
FEEDL stepping, 38.0 mm frame pitch), calibration data flow, image format
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
- [ ] IR-based automatic dust/scratch removal (cleanup algorithm)
- [ ] Resolution profiles beyond 3600 dpi
- [ ] Whole-strip batch scanning; verified positioning for frames > 1
- [ ] Loader/button event handling, udev rule, ICC-tagged output
- [ ] SANE genesys backend support for GL126 (upstream goal)
- [ ] VueScan support via a protocol dossier to Hamrick

## Status & disclaimer

Working prototype, developed and tested against a single OpticFilm 135i
unit. This project is not affiliated with, endorsed by, or supported by
Plustek Inc. Use at your own risk. Plustek and OpticFilm are trademarks
of their respective owner; they are used here only to identify the
hardware this software interoperates with.

## License

GPL-2.0-or-later — see [LICENSE](LICENSE).
