# v0.2.11 — Second Scanner Gamble: frames where another unit puts them

Release notes for the `v0.2.11` tag (2026-11-06) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek. It is developed by
one person on a single OpticFilm 135i unit and has now scanned on two
other units. Previous release notes:
[v0.2.10](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.11/docs/release-notes-v0.2.10.md).

## What this release is

The second release shaped by other units. The frame positions were
fitted on one scanner. On a third unit every frame sits about 0.5 mm
earlier than that model, on all six frames and with both of that user's
holders, so frames 4 and 5 fell under the 0.15 mm margin floor and were
refused on some strips (issue #61). No scan was wrong: the whole picture
was in each refused frame's overscan file.

**A per-unit position offset** (`--position-offset MM` on `scan` and
`digitize`, with `OF135I_POSITION_OFFSET` as the unit's default) moves
every scan window along the strip by that amount. On the developer's
unit the mechanism is hardware-verified (Test 120: at 0, −0.3 and
+0.3 mm FEEDL moved by exactly the offset, the window's length did not
change, and the measured margins followed). It has not yet run on the
unit it is for. A coverage failure now ends with the value that would
centre the opening, so a unit's own sidecars give its offset.

**The refused frames need no second scan.** `of135i recrop` makes the
products from a frame's preserved overscan files, with a margin floor
the operator chooses.

The release also adds an album that numbers a film's photographs
through its strips (issue #65), and a release codename, printed by
`of135i version` and recorded in every sidecar.

Thanks to **Krustenviech**, whose sidecars showed the shift and who
asked for the album, and to **bearyjd**, who ran the cross-unit check
and the SANE v9 load on their unit.

## Changes that can break a caller

None in behaviour. With no offset set the position model is the
unchanged one (offline: a zero shift returns the model itself), so
`scan` and `digitize` position as v0.2.10 does; the new commands and
options are additions.

One output change: `of135i version` now prints the codename after the
version number; a caller that parses that line should allow for it.

## New

- **`--position-offset MM`** on `scan` and `digitize`, and the
  `OF135I_POSITION_OFFSET` environment variable as the per-unit default
  (the flag overrides it). Negative moves the window earlier. Margins,
  window length and chunk count are unchanged; the transport limit is
  checked after the shift, before anything is sent; at most ±3 mm.
  Recorded in the sidecar and in `sweep.json`. A coverage failure whose
  two margins are known ends with the suggested value (for example
  `--position-offset -0.6`). Hardware-verified on the developer's unit
  (Test 120).
- **`of135i recrop FILE...`**: `fN.tiff` and `fN-ir.tiff` from a frame's
  `fN.overscan.tiff` (and `fN-ir.overscan.tiff`), with coverage measured
  as the scan measures it, `--min-margin MM` (default 0.15), `--force`
  for any located edge, `--dry-run` and `--overwrite`. Empty openings
  are refused; the overscan files are never altered; a `fN.recrop.json`
  records the run. Offline-verified.
- **`digitize --album NAME`** and **`of135i album -o DIR NAME`**: one
  folder per film with `NN.tiff` and `NN.diag.json`, numbered 01–36
  without gaps in strip order. An empty opening takes no number; a frame
  scanned but not delivered keeps its number and is listed as missing in
  `NAME/index.json` until `recrop` delivers it. The files are hard links
  to each strip's own products, and the album is rebuilt from the
  manifest after every strip. 135 film only. Offline-verified.
- **Release codename**: `of135i version` prints
  `of135i 0.2.11 "Second Scanner Gamble" (driver revision …)`, and the
  sidecar's host block carries `driver_codename`.

## SANE backend

No code change. `gl126.cpp`, `gl126.h` and `gl126_registers.h` began as
copies of the genesys backend's gl124 files and now credit their
original authors in the file headers. Revision v9 (v8 plus the
calibration stop gate) is still prepared and **not** on merge request
!1032, which holds v8. On the second unit bearyjd ran v9's magazine load
twice: one miss with the known "transport ran, cassette not taken"
signature, one load whose transfers, from the reg 0x32 write to the end
of the load, match the Python driver's on the same unit (external
report in the test log). v9's scans on that unit, the part that tests
the gate, are pending.

## Limits

- The position offset has not run on the unit that reported the shift;
  the value for it (−0.5 or −0.6 mm) is predicted from its sidecars and
  Test 120.
- The whole-strip sweep carries the offset through the same function as
  the per-frame scan, but no sweep has run with an offset.
- `recrop` and the album are offline-verified only.
- SANE v9's scans on the second unit are pending; the backend's stop
  gate has still not run on a unit that shows the calibration failure.

## Thanks

- **Krustenviech**: issue #61 with fourteen sidecars from three strips,
  which located the shift and its size; issue #65, the album.
- **bearyjd**: the cross-unit check on the second unit (4 of 4,
  coverage verified at frame 1 with the slide holder), the two v9 load
  runs with a wire comparison against the Python driver, and the
  logging patches held for the next SANE revision.

## Gate

`tools/release_check.py`: FULL (763 tests, 0 skipped), with the
backend built. `tools/gen_sane_tables.py --check` passes.
