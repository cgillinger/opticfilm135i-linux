# v0.2.8 — the whole-strip sweep

Release notes for the `v0.2.8` tag (2026-10-30) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.7](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.8/docs/release-notes-v0.2.7.md).

## What this release is

The first motor-sequence saving. Until now the driver, like every vendor
application captured, parked the carriage after every frame and
repositioned from the park reference: four calibrations and four carriage
returns per four-frame strip, about 240 s of which 160 s is the scan
itself (`docs/production-telemetry-2026-10.md` §2.4).

**`digitize --sweep`** (135, dual-light profiles) scans the whole strip in
ONE pass: one calibration, one positioning move to the first aperture,
one scan pass over every aperture, one park. The raw pass (1.3 GB for
four apertures at 3600 dpi) is streamed to `<roll>/sweep.raw` chunk by
chunk and never held in memory; the frames are cut from it in software
through the unchanged per-frame writer, so each frame carries the same
aperture coverage verdict, the same overscan files and the same products
as the per-frame cycle. Aperture numbers come from the measured aperture
edges and the holder's pitch relative to the sweep's commanded start;
runs that do not agree leave the sweep *unresolved* (no numbered product,
`sweep.raw` kept, exit 4) rather than misnumbered. `--keep-sweep` keeps
the raw file after a successful roll; `--sweep --from-sweep --roll N`
rebuilds a roll offline from it. The calibration data file is written
once and hard-linked as `fN.cal.npz` for every frame. Design:
`docs/whole-strip-sweep-design.md`.

**Hardware evidence — Test 111 (2026-10-30).** Dual 3600 with IR, one
four-frame colour strip that had been scanned per frame earlier the same
day: 4 of 4 apertures verified by their IR edges (margins 0.67–0.76 mm,
strip offset +0.07 mm), aperture lengths within 0.06 mm of the per-frame
products, per-aperture correlation 0.997–0.999, eye-accepted by the owner.
Time: 180 s on the device (position 1.8 s, calibration 6.0 s, scan 161 s,
park 10.3 s) plus 12 s of offline cutting, about 192 s against 277 s for
the same strip per frame — 31 % less, with three carriage returns and
three calibrations fewer. The first run's post-processing was unresolved
because the window finder's IR profile averaged the three staggered IR
channels; fixed (the channels are registered as `image.split_ir` does),
regression-tested, and the roll rebuilt from the kept raw without a new
scan — the raw-data principle at work.

**Limits.** Run on one strip in one profile; the other dual profiles are
not run and plain 3600 (`--no-ir`) keeps the per-frame cycle (refused by
`--sweep`). `--frames` must be a contiguous range. The per-frame path's
command stream is unchanged (its calibration block was extracted
verbatim; the wire-equality tests pass untouched).

## Gate

`tools/release_check.py`: FULL (545 tests, 0 skipped). CI green on PR #36.
