# v0.2.6 — 110 film until the roll is done, and film in the calibration light path

Release notes for the `v0.2.6` tag (2026-10-26) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.5](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.6/docs/release-notes-v0.2.5.md).

## What this release is

The Python driver's 110 (Pocket Instamatic) workflow, shaped by about a
dozen real strips run through a production application between 2026-10-21
and 2026-10-26 (Tests 103–107), plus one calibration fix those strips
uncovered. Everything is in
[docs/film-110.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.6/docs/film-110.md);
the owner's rule behind all of it: *the system serves the photographs* —
the bookkeeping never stops the operator and never discards a picture.

**110: free passes until the roll is done.** `digitize --film 110 --roll N`
treats every run as one pass (`p1-`, `p2-`, …) at whatever seating the
operator chose; the placement is measured and logged, never a gate. From
the second pass on, numbering is decided by **recognising the roll's own
pictures** (slid or turned end for end), never by positions alone. The best
copy of each photograph wins (whole and supported > near-whole and
supported > whole with the strip end free > …). `advice.done` says when
every frame position between the strip's ends is delivered or blank.

**A pass report in the holder's language.** Each pass reports what lies in
each aperture and under each bar, with measured amounts ("image 2 split,
7.9 mm under bar 1"), and gives **one** next placement, naming which
photograph to move and why, with something visible to aim for — never a
bare distance. Numbers count frame positions from the strip's front end;
unexposed positions are blanks (a number, no file).

**The holes are never a gate.** A second detector locates images from the
printed border between them when no hole can be read; a hole is judged as
air across the hole zone, so a clear edge rim no longer makes false holes
(Test 107).

**Rebuilding from stored frames.** `digitize --film 110 --roll N
--from-frames` renumbers a roll and rewrites its products from the frames
of earlier passes, with no scanner; a driver correction never costs a new
pass. The rebuild is atomic; replaced products move to `superseded-<UTC>/`.

**Whole photographs are never discarded.** When a pass's numbering cannot
be established, its whole and near-whole images are written unnumbered
(`p<k>-a<aperture>-<i>.tiff`, exit 5) instead of nothing.

**Film in the calibration's white measurement.** A strip whose end lay a
little into the holder's end at aperture 1 blocked the white reference
over the film band; the gain over-corrected those columns and visible red
and infrared clipped in every frame of that pass. The driver now finds such
a blockage and fills it from the newest clean white profile, kept between
runs in `~/.local/state/of135i` (sidecar field `white_blocked`). Validated
on 389 stored calibrations (exactly the 20 blocked ones found); **one
hardware pass with the strip in that position is still to confirm it.**

## What did not change

- Everything sent to the scanner: no motor sequence, register write or
  timing changed. The calibration fix changes table contents only when the
  white measurement is blocked.
- 35 mm scanning: the same output as v0.2.5 on clean calibrations.
- The SANE backend (merge request !1032, revision v8, under review):
  unchanged since v0.2.3. The 110 workflow is a Python-driver feature.

## Verification

- Hardware: Tests 103–107 (docs/test-log.md), real 110 strips through the
  production application; the fixes found by Tests 105–107 are verified
  offline on those passes' stored frames (old against new detector on 123
  distinct real 110 frames: 119 identical, three corrected, one 0.4 mm).
- The offline suite: 511 passed, 0 skipped (`tools/release_check.py`, FULL
  VERIFICATION; v0.2.5 had 424).

## Changes since v0.2.5

The full list is the `v0.2.6` section of
[CHANGELOG.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.6/CHANGELOG.md).
