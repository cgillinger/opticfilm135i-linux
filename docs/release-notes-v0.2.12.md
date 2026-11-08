# v0.2.12 — Shiny Side Slides: mounted slides, and v9 on the merge request

Release notes for the `v0.2.12` tag (2026-11-08) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and is used at
your own risk. It is developed by one person on a single OpticFilm 135i
unit and has scanned on two other 135i units. Previous release notes:
[v0.2.11](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.12/docs/release-notes-v0.2.11.md).

## What this release is

The first release with mounted slides. The scanner ships with a
four-slide holder; until now the driver could load it but not position
on its openings or crop a slide. All three steps of that work were
written by **bearyjd** on his unit and his slides (issue #70, PRs
#73–#75), then run on the developer's unit with its one slide
(Test 124), so the slide path has run on two units with two slides.

`scan --holder slide --frame N --ir` (N = 1–4) positions on the
holder's measured grid, keeps the full overscan frame, finds the mount's
aperture in the infrared channel and crops to it when coverage
verifies, exactly as for strips; `--slide-positive` turns a positive
slide to the vendor orientation without inverting it. **Put the slide in
the holder with the mount's white, shiny side (the film base) up**: with
that, the picture reads the right way round. That rule is the codename.

The SANE backend's revision v9, the calibration stop gate, is now on
merge request !1032, after it had run through its whole test table on
bearyjd's unit (600, plain 3600 and infrared 3600 dpi, every gate wait
doing work, none timing out) and on the developer's.

The README now opens with a safety section. The OpticFilm 135i
(`07b3:1436`) is the only supported model; an OpticFilm 135 was tried
with a patched product id (issue #72) and its red channel came out
wrong. A capture guide (`docs/capture-guide.md`) describes how the
manufacturer's software can be recorded for such a case, at the
reader's own risk.

## Changes that can break a caller

None. `--holder` defaults to `strip`, and the strip path is unchanged
on the wire and in its output and sidecars (pinned by tests). The dust
cleaning at 3600 dpi is byte-identical to v0.2.11; only the lower
resolutions change (below).

## New

- **`scan --holder slide`** (bearyjd, #73): `--frame N` / `--frames`
  are the slide holder's openings 1–4, positioned on the grid measured
  in Test 84 with `--position-offset` applied and a stricter travel
  check; dual-light profiles only (`--ir`, or any `--dpi` but 3600),
  default `--overscan 1.5`; `--film 110` and `--positive` are refused
  before the device opens. Per opening the scanner receives the strip
  path's own phases; only the position and the line count differ.
  Run on bearyjd's unit (openings 1–4 at 3600 dpi, opening 1 at 600
  dpi, Test 121) and on the developer's (opening 1, Test 124).
- **Slide crop keyed on the infrared channel** (bearyjd, #74): the
  mount is opaque to infrared and the film is not, so the lit run in
  the IR channel is the mount's aperture (33.8–37 mm along the
  transport; cardboard mounts measured 34.16–34.45 mm, the developer's
  slide 35.12 mm). Verified → cropped pair with a 1.5 mm pad per side;
  otherwise the overscan is kept and the scan exits 4. `of135i recrop`
  refuses slide files. Tests 122 and 124.
- **`--slide-positive`** (bearyjd, #75): orientation only (mirror +
  rotate, as `--positive` does), no inversion, linear values, no sRGB
  tag, so the file looks dark in an ordinary viewer. Test 123 and 124.
- **Slide insertion rule** (Test 124): white, shiny side of the mount
  up toward the holder's open face. From two passes of the same slide
  on the developer's unit, identical images; the contributor has been
  asked whether his slides sat the same way.
- **IR dust cleaning scaled by resolution** (bearyjd, #66): footprints
  in physical size, so 600 dpi no longer over-masks (28.8 % → 6.0 % of
  the Test 84 slide); 3600 dpi unchanged.
- **README safety section** and a stronger disclaimer: only the 135i,
  do not patch the product id, the rules that apply to every unit.
- **`docs/capture-guide.md`**: recording the manufacturer's software
  from a Windows virtual machine with usbmon, for a unit or model that
  behaves differently.

## SANE backend

Revision v9 (v8 plus the calibration stop gate, co-authored by
bearyjd, and a comments-only commit) was pushed to merge request !1032
on 2026-11-08 as a fast-forward on v8; fork pipeline 8/8, no conflicts,
description updated to two units. On bearyjd's unit all 21 reg 0x100
gate polls over three scans found the engine still running and waited
9–26 ms; none timed out. Under review, not merged. The backend has no
slide path; slides are a driver feature in this release.

## Limits

- Slides: two units, two slides, cardboard mounts only. The lateral
  crop (across the frame) is not done: the full 36.6 mm window is kept
  across, with mount above and below the picture. Other mount types
  (glass, plastic, IR-translucent cardboard) are untested; an
  IR-translucent mount is refused rather than cropped.
- The insertion rule rests on the developer's one slide and the
  contributor's reading of his; a mount with the film the other way
  round would come out mirrored.
- The OpticFilm 135 is not supported (issue #72).
- Two best-effort start-up polls log a 0.3 s timeout before calibration
  on both units; they are not counted and have no effect.

## Thanks

- **bearyjd**: the whole slide track (#66, #67, #73, #74, #75) with
  hardware runs on his unit, the v9 test table including the infrared
  run, the blind poll-cadence check, and the logging and load-guidance
  patches held for the next SANE revision.
- **taprobane99**: the OpticFilm 135 report with `doctor`, sidecars and
  crops, including an empty-holder scan (issue #72).

## Gate

`tools/release_check.py`: FULL (800 tests, 0 skipped), with the
backend built, on the merged slide tree; the release commit re-runs it.
