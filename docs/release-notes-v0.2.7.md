# v0.2.7 — 110 over six apertures, and black-and-white 110

Release notes for the `v0.2.7` tag (2026-10-28) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.6](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.7/docs/release-notes-v0.2.6.md).

## What this release is

The Python driver's 110 (Pocket Instamatic) workflow extended to the last
strips in the owner's collection: longer strips and black-and-white film.
Everything was driven by one evening's real strips through a production
application (Tests 108–110); every change was checked against all stored
colour rolls first — 20 rolls rebuilt with the old and the new code give
identical deliveries.

**All six apertures by default.** `digitize --film 110` now defaults to
`--frames 1-6` (135 keeps `1-4`): a long 110 strip reaches apertures 5–6,
and a short one costs nothing because the run still stops after the first
empty aperture that follows film.

**Black-and-white 110.** Scanned without IR (`--no-ir`; the silver image
blocks IR), on the plain 3600 path. Four detector changes, each measured
on the real frames:
- **Coverage beside the strip.** When dense film hides the aperture's edge
  in the frame's centre, the aperture is measured on the bare air beside
  the 16 mm strip: column bands that show the holder's own opening length
  (35.92 ± 1.0 mm) and agree within 0.3 mm. Film can only shorten a band,
  so the bands seeing the longest opening decide. A shorter central
  opening is not taken for the aperture (it would crop film off). Manifest
  `coverage_source: "visible-side"`.
- **Dense pictures are not the rail.** A dark column run between other
  columns counts as plastic only if it is dark at its 95th percentile too:
  the rail is dark on every line, film is lifted by its printed gaps.
- **The light clear base.** The hole search retries with an edge threshold
  of 0.9 × air when 0.6 finds no hole (a black-and-white base reads 0.78).
- **A thin picture is not a blank.** A window that reads as clear film
  along transport must also be flat across (also seen on colour, Test 108).

**Three numbering gaps closed.**
- A strip's **last picture**, whose hole lies at the end of the previous
  aperture and which is followed by fogged film, is located from that
  hole's prediction carried over (`located_by` "previous aperture's
  hole").
- A pass 1 seated **between the two placement phases** with its front end
  out of view is numbered from its first whole image (placement `free`,
  provisional numbers) instead of writing every photograph unnumbered.
- Passes scanned without IR are **recognised in log intensity**: dense
  pictures sit near black, where linear thumbnails matched the same
  photograph at only 0.24–0.49.

## Evidence

Five black-and-white strips and the day's colour strips through the
application on 2026-10-28; every miss reproduced and fixed offline on the
stored frames, then confirmed live on the last strip (three passes, 4 of 4,
roll done). See
[docs/test-log.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.7/docs/test-log.md)
Tests 108–110 and
[docs/film-110.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.7/docs/film-110.md).
`tools/release_check.py`: full verification, 531 tests, 0 skipped.

## Unchanged

The SANE backend (unchanged since v0.2.3; 110 support stays in the Python
driver), all motor sequences, calibration and the 135 path.
