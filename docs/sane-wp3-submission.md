# WP-3 — the SANE submission package, prepared only

**Status: PREPARED FOR REVIEW. Nothing has been sent.** No merge request,
no issue, no mail, no contact with the SANE project or anyone else. B2 is
**not** complete and must not be recorded as such: this work package
produces a reviewable package and stops there. Whether anything is ever
submitted, and by whom, is Christian's decision.

> **Safety note, read before touching the branch.** The package lives on a
> branch inside a clone of *sane-backends*, whose `origin` is
> `https://gitlab.com/sane-project/backends.git` — the real upstream. The
> branch deliberately has **no upstream tracking set**, so a bare `git
> push` from it fails rather than reaching SANE. Do not set tracking, and
> do not push from that clone.

## 1. Where it is

| | |
|---|---|
| Branch | `wp3-gl126-submission` |
| Worktree | `~/Dokument/Github/sane-wp3` (a `git worktree` of the sane-backends clone) |
| Base | `1d47d7c`, "Merge branch 'ci_juint_test_reports' into 'master'", 2026-09-02 |
| Commits | 4 |
| Scope | 9 new files, 16 modified, +30632 / −29 lines |

The base commit **is** `origin/master` as last fetched, and nothing
upstream has touched `backend/genesys/` since it. Before any real
submission the clone must be re-fetched and the branch rebased onto
current master — that is the first item in §7.

The worktree is separate from `~/Dokument/Github/sane-backends`, which
keeps the development arrangement (symlinks into this repo's `sane/`)
untouched. The package contains **no symlinks**: `git ls-files -s` reports
zero mode-120000 entries, and the nine GL126 files are real files
committed to the branch. That was the point of building it this way —
a reviewer clones, builds, and needs nothing from this repository.

To recreate it from scratch:

```
cd ~/Dokument/Github/sane-backends
git worktree add -b wp3-gl126-submission ~/Dokument/Github/sane-wp3 1d47d7c
cd ~/Dokument/Github/sane-wp3
cp ~/Dokument/Github/opticfilm135i-linux/sane/gl126*.{h,cpp} backend/genesys/
git apply ~/Dokument/Github/opticfilm135i-linux/sane/gl126-integration.patch
# then commit in the four groups described below
```

## 2. The commit series

1. **`genesys: add support for the GL126 ASIC`** — the command set and
   everything it needs: the nine `gl126_*` files, the `AsicType` entry
   and its string mapping, two `ScanSession` fields for the dual-light
   profiles, the image-pipeline hook, the GL126 branch in the USB
   interface's bulk read, the test-interface addition, `low.cpp`'s
   dispatch and `Makefile.am`.
2. **`genesys: add the Plustek OpticFilm 135i (07b3:1436)`** — the model
   and sensor entries, the USB id in `genesys.conf.in`, the `.desc`
   entry.
3. **`genesys: frame selection and magazine handling for the OpticFilm
   135i`** — the three options (`frame`, `load-film`, `eject-film`,
   `magazine`) in `genesys.{h,cpp}`, inactive on every other ASIC.
4. **`genesys: document the GL126 and the OpticFilm 135i`** — the man
   page's chip list, an `AUTHORS` entry, and the `.desc` status moving
   from `:untested` to `:good`.

## 3. What was verified, and how

**Build.** Configured and built from the branch alone
(`./autogen.sh && ./configure --sysconfdir=/etc`, then `lib`, `sanei` and
`backend/libsane-genesys.la`). Exit 0, **zero compiler errors or
warnings**. This is the check that matters most: it proves the package
stands without this repository.

**Exported symbols.** `nm -D` on the resulting library: 1519 dynamic
symbols, **107 mentioning gl126 — identical to the development build**,
and both the plain `sane_*` and the prefixed `sane_genesys_*` entry
points present as genesys expects.

**Offline tests against the package build.** The three suites that need a
built backend were re-run with `SANE_BACKENDS_DIR` pointed at the
worktree rather than the development tree:

| suite | result |
|---|---|
| `test_sane_open_params` | 7 passed |
| `test_sane_calibration_cache` | 6 passed |
| `test_sane_magazine` | 20 passed |

The full offline suite (316 tests) passes in this repository against the
development build.

**Checklist items from `doc/backend-writing.txt`.** That checklist is
written for a *new backend*; this is a new ASIC and model inside the
existing genesys backend, so `dll.conf`, a new man page file and a new
`.desc` file do not apply — genesys already has all three. What did
apply, and was done: the SANE licence in every source file (two generated
files were missing it — fixed in the generator so it stays fixed), the
`AUTHORS` entry, the source files in `Makefile.am`, the `.conf` USB id,
the man page chip list, and the `.desc` status and comment.
`po/POTFILES` needed no change: the new translatable strings are in
`genesys.cpp`, already listed, and the GL126 files use no `SANE_I18N`.
`indent -gnu` was not run — genesys is C++ and does not follow it; the
new code follows the surrounding style.

**Not run, deliberately: `scanimage -T` and `tstbackend`.** Neither is an
offline test. Both open a device and would drive the scanner, and neither
can be isolated to a mock. The nearest isolated equivalent already exists
and passes: `tests/gl126_calibration_cache_probe.cpp` and
`tests/gl126_magazine_probe.cpp` drive the real public flow
(`sane_open` → `sane_control_option` → `sane_start`) through genesys's own
testing mode, which cannot reach USB. Running the two real tools remains
a gap; see §7.

## 4. Hardware evidence, and its limits

All of it from **one scanner**. Only one exists for this project and no
second unit will be available, so cross-unit behaviour is unverified and
is declared as a limitation rather than assumed.

| test | what it establishes |
|---|---|
| 74 | The installed backend scans a frame through `scanimage` and inside digiKam; a real install defect found and fixed (`--sysconfdir`) |
| 75 | Full load → scan → eject from `scanimage` alone, no external command |
| 76 | The same from digiKam, plus a second frame on the same load |
| 77 | Power-cycled with a latched magazine, freed and loaded by the backend |
| 78 | The cold start's opening wait shortened; 18 of 19 polls byte-identical to the reference run |
| 79 | The settle check shortened; total cold start 40.1 s → 22.9 s |

Earlier work covering the profiles, geometry and image path is in
`docs/test-log.md` (Tests 62–73) and `docs/ROADMAP.md`.

## 5. Draft contribution description

*Not sent. Text only, for review.*

> **genesys: support for the GL126 and the Plustek OpticFilm 135i**
>
> This series adds the Genesys GL126 to the genesys backend, and with it
> the Plustek OpticFilm 135i (07b3:1436), a 35 mm film scanner.
>
> The GL126 is close enough to the GL124 for the framework to fit, but
> its scan flow is the vendor's rather than GL124's, so it is implemented
> as its own command set rather than a GL124 model variant. The register
> sequences are generated from USB captures of the vendor driver and
> verified byte-exact against them; the generator is not part of this
> series, but the tables it emits are, with the provenance recorded in
> their header.
>
> The scanner works differently enough from a flatbed to be worth
> describing. It scans one frame of a loaded film strip per pass,
> addressed by number through a `frame` option rather than by a scan
> area, because the geometry is fixed by the holder and positioning is a
> single absolute feed from the load reference. The film magazine is
> loaded and ejected through two further options: the vendor's own insert
> flow requires the operator to remove the magazine and re-seat it to a
> mechanical stop in the middle of the sequence, and since SANE offers no
> way to ask for that during `sane_start`, `load-film` performs the
> release, the operator re-seats, and the next `sane_start` completes the
> load. A read-only `magazine` option reports where it is believed to be.
> All four options are inactive on every other ASIC.
>
> Every motor sequence fails closed: the first unacknowledged write,
> short transfer or timed-out wait ends the sequence with nothing further
> written and no recovery attempted, because this hardware has a
> documented history of stalling when driven from an undefined state.
>
> Testing: one unit, over an extended bring-up. A full load → scan →
> eject cycle has been driven from `scanimage` and from digiKam, at every
> resolution the vendor's captures cover plus the infrared pass. Known
> limitations are listed in the accompanying notes; the most important is
> that a single unit exists for this work, so nothing here is verified
> across units.

## 6. Known limitations, to accompany any submission

1. **One unit.** Everything is verified on a single scanner. Cross-unit
   behaviour, and any unit-to-unit variation in calibration constants, is
   unverified.
2. **The backend delivers the whole overscan window.** The driver this
   grew from crops to the aperture on the host; that is not ported, so
   the image carries margins beyond the frame.
3. **No batch scanning.** One frame per `sane_start`. The vendor's
   software scans a whole strip in one operation; ours does not, and the
   protocol work for it is described in the roadmap but not done.
4. **GL126 always calibrates.** The calibration cache is deliberately not
   reused — a restored cache made `begin_scan` refuse — so every scan
   pays roughly four seconds of calibration.
5. **The interrupt endpoint is not drained.** The genesys USB abstraction
   has no interrupt transfer. Nothing in the SANE flow reads that
   endpoint, so nothing here is harmed, but a backend-driven load may
   leave it in an overflow state for other software until the next power
   cycle.
6. **A process lock shared with an external driver.** The backend takes a
   `flock` on a well-known path to keep itself and the reverse-engineered
   Python driver off the device simultaneously. No other genesys ASIC
   needs this, and a reviewer may reasonably question it.
7. **State on disk.** "A release is pending" is recorded beside that lock
   so `scanimage`, which reaches the backend in a fresh process each
   invocation, can complete a two-step load. Also likely to draw
   questions.
8. **2400 dpi is anisotropic** (3600 across, 2400 along) and is resampled
   on the host so delivered pixels are square.
9. **Colour rendering is not addressed.** The backend delivers linear raw
   data; the preview rendering in the companion driver has an
   unexplained cast (`docs/colour-rendering-analysis.md`). Nothing in
   this series depends on it.

## 7. What remains before anything could be submitted

Listed so the decision is informed, not to schedule it.

1. **Re-fetch and rebase.** The base is `origin/master` as of 2026-09-02.
   Upstream will have moved.
2. **Run `scanimage -T` and `tstbackend`** against the real device, or
   state explicitly that they were not run. Both need hardware.
3. **Decide the generated-table question.** `gl126_tables.cpp` is 1.9 MB
   of generated data whose generator lives in this repository, not
   upstream. A reviewer may ask for the generator, for the captures, or
   for the file to be reduced. There is no good answer prepared.
4. **Decide how much of the magazine machinery to offer.** Items 6 and 7
   of §6 are the two most likely to be challenged.
5. **Christian's own decision on contact.** Nothing in this package
   initiates it, and nothing should without him doing it himself.
