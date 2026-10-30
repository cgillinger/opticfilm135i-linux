# v0.2.9 — the sweep in production, and a "busy" exit code

Release notes for the `v0.2.9` tag (2026-10-30) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.8](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.9/docs/release-notes-v0.2.8.md).

## What this release is

A small release on the evening the whole-strip sweep of v0.2.8 went into
production.

**Progress lines during the sweep.** The pass logs `scanning frame N @
<dpi> dpi (sweep pass, aperture N of first–last, P%)` as it reaches each
aperture's predicted leading edge, and the offline cut prints `cutting
frame N from the sweep (wire lines a–b, located by edges|pitch)`. A caller
that followed the per-frame `scanning frame N` banner needs no change.

**Exit 3 when the scanner is busy.** When another of135i process holds the
scanner lock, every hardware command now exits with `3` and says in plain
words that nothing was sent and no power cycle is needed. Before, the
refusal shared exit `1` with real failures, and `digitize` followed it
with its generic power-cycle advice. Seen in production the same evening:
a next-strip load was refused because a second instance of the calling
application held the lock; the application read exit 1 as a hardware
failure and the operator power-cycled a scanner that had received
nothing. The refusal is not recorded in the roll manifest (nothing
happened; the roll number is not consumed; the strip stays loaded).

**Production confirmation of the sweep (Test 111, evening 1).** Seven
colour-135 strips through the calling application with `digitize --sweep`:
all exit 0, 4 of 4 apertures verified by their edges on every strip,
device phases identical to the tenth of a second to the A/B run
(calibration 6.0 s, position 1.8 s, scan 161.4 s, park 10.3 s), 200–203 s
wall clock per strip against about 277 s per frame. The owner reports the
machine audibly quieter: one positioning move and one park per strip
instead of four. Every production strip counts as test data from here on;
the series continues in `docs/test-log.md`.

**Limits.** Unchanged from v0.2.8: one strip, one profile hardware-run with
the sweep; plain 3600 (`--no-ir`) keeps the per-frame cycle. The exit-3
change is offline-tested only (no scanner was involved; the lock is a host
file lock).

## Gate

`tools/release_check.py`: FULL (549 tests, 0 skipped). CI green on PR #38.
