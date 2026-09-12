# v0.1.1 — Early Linux driver release (unofficial)

Prepared release notes for the existing `v0.1.1` tag (2026-09-06). **Not
published as a GitHub Release** — this file is the draft text for one. It
describes the software as tagged at `v0.1.1`; later work on `master` (the SANE
backend hardware runs, the 2400 dpi proportion fix, the calibration-cache fix)
is **not** part of this tag and is deliberately not claimed here.

---

An early, experimental, community-built **Linux driver for the Plustek
OpticFilm 135i** 35 mm film scanner (USB `07b3:1436`, Genesys Logic GL126). It
is **unofficial** — not affiliated with, endorsed by, or supported by Plustek.

## Scope of this release

`v0.1.1` is the standalone **Python / pyusb command-line driver** at its first
functional milestone (own driver complete within its frozen scope), plus
bulk-digitisation file-bookkeeping fixes over `v0.1.0`. There is **no SANE
backend** in this tag; SANE support is later, in-progress work.

## What works on the developer's test unit

- Native USB scanning with **no Windows, no virtual machine, and no vendor
  driver**.
- Magazine loading (the vendor insert flow), single-frame and **whole-strip
  batch** scanning (frames 1–4), and eject.
- **All five resolutions** — 600 / 1200 / 2400 / 3600 / 7200 dpi.
- Self-computed calibration (AFE gain, AFE offset, two-stage per-pixel shading)
  and colour-line (staggered CCD) channel alignment.
- **Infrared** dual-light capture (`--ir`) with IR-based automatic dust/scratch
  removal.
- Output as raw 16-bit linear negative (the driver's actual product), or an
  optional preview positive (`--positive`). The preview is a convenience;
  colour interpretation belongs in your application working from the raw
  negative — so the raw product and the preview have different purposes and
  different limitations.
- A fail-closed hardware-safety layer and a udev rule for rootless use.

## Test base — please read

Development and hardware testing were done by **one person on a single
OpticFilm 135i unit** in a limited Linux environment. Working results on that
unit are **not** proof of compatibility with other OpticFilm 135i scanners or
other Linux systems. Results on other units are not yet known, and additional
hardware testing is welcome. Treat this as **early, experimental software**.

## Known limitations (see the README for the full list)

- Eject depends on how the magazine was loaded (use the driver's `load` flow).
- Colour rendering of the `--positive` preview is a convenience, not a colour
  pipeline; the archival product is the raw negative.
- Cross-unit behaviour is a documented limitation, not a verified feature.

## Install and usage

Requirements, install steps (including the udev rule) and the normal workflow
are in the project **[README](../README.md)** (“Install” and “Usage” sections).
The roadmap and acceptance criteria are in
**[docs/ROADMAP.md](ROADMAP.md)**.
