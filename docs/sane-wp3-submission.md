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

The base commit was `origin/master` as fetched on 2026-09-02. Upstream
has moved since (a dry-run fetch on 2026-09-15 showed new commits on
master); whether any of them touch `backend/genesys/` is not yet checked.
Before any real submission the clone must be re-fetched and the branch
rebased onto current master — that is the first item in §7.

The worktree is separate from `~/Dokument/Github/sane-backends`, which
keeps the development arrangement (symlinks into this repo's `sane/`)
untouched. The package contains **no symlinks**: `git ls-files -s` reports
zero mode-120000 entries, and the nine GL126 files are real files
committed to the branch. That was the point of building it this way —
a reviewer clones, builds, and needs nothing from this repository.

**Exported 2026-09-15 to `sane/wp3-package/`** — a bundle and the four
patches, with the base and every commit and tree id, recreation and build
instructions, and the verification done in two clean clones. That is the
reviewable form; the worktree is the working copy.

To recreate it from scratch by hand instead:

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

The full offline suite passes in this repository against the development
build: 316 tests at the `12d5193` baseline this package was exported
from, 321 with the lock-file-hardening tests added afterwards (which
belong to the next series revision, §7).

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
> The motor sequences are guarded rather than retried: an unacknowledged
> write, a short transfer, or a timed-out wait on a motor-completion
> condition ends the sequence with nothing further written and no
> recovery attempted, because this hardware has a documented history of
> stalling when driven from an undefined state. Not every wait is of that
> kind. A minority of polls are best-effort — they mirror the vendor
> driver's own non-raising status reads — and a timeout there is recorded
> and the sequence continues. That distinction is deliberate: the cold
> start's opening poll waits on a state the engine cannot reach before
> its first homing move, and treating it as fatal would refuse every
> power-cycled unit. Which kind each wait is, is marked at the site in
> the generated tables.
>
> Testing: one unit, over an extended bring-up. Every resolution the
> vendor's captures cover, plus the infrared pass, has been scanned
> through the backend from `scanimage`, with the magazine loaded by the
> companion command-line driver. The backend-driven magazine flow — a
> full load → scan → eject cycle with no external command, from
> `scanimage` and from digiKam, including a power-cycled unit with a
> latched magazine — has been run at 3600 dpi only. Interrupting a scan
> mid-pass leaves the transport unparked and needs a power cycle; no
> automatic recovery exists, by design. Known limitations are listed in
> the accompanying notes; the most important is that a single unit
> exists for this work, so nothing here is verified across units.

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
   endpoint, so nothing here is harmed. Whether a backend-driven load
   leaves it in an overflow state for other software was predicted but
   not observed: the one direct check afterwards (Test 75) read the
   endpoint normally.
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
   data; rendering a negative is the frontend's job. The companion
   driver's preview shows a cast on some strips that also appears in the
   vendor's own rendering and has not been traced to any code
   (`docs/colour-rendering-analysis.md`). Nothing in this series depends
   on it.

## 8. Changes to shared genesys code, hunk by hunk

Classified 2026-09-15 for the reviewer's question "what does this series
do to scanners that are not a GL126?". Everything outside the nine
`gl126_*` files is listed; "gated" means the new behaviour is behind
`asic_type == AsicType::GL126` and other ASICs execute exactly the code
they did before.

| file | change | class | effect on other ASICs |
|---|---|---|---|
| `enums.{h,cpp}` | `AsicType::GL126`, `ModelId::PLUSTEK_OPTICFILM_135I`, `SensorId::CCD_PLUSTEK_OPTICFILM_135I`, their name strings | additive | none |
| `low.cpp` | `create_cmd_set` and `scanner_read_status` dispatch for GL126; the dual-light pipeline nodes pushed only for GL126 | gated | none |
| `scanner_interface_usb.cpp` | GL126 added to the GL124-style 16-bit register read/write and `write_fe_register` branches; `bulk_read_data` takes the GL126 path first | gated | none |
| `settings.h` | `Genesys_Settings::frame` (default 1), `ScanSession::gl126_keep_parity` (default 2 = keep every line) | additive fields with defaults | none — no shared code reads them |
| `genesys.h` | four new `Genesys_Option` values, `Genesys_Scanner::frame` | additive | option **indices** after `OPT_EXPIRATION_TIME` shift by four for every model; SANE frontends address options by name, and the four are `SANE_CAP_INACTIVE` on every other ASIC |
| `genesys.cpp`, option setup / get / set | the `frame`, `magazine`, `load-film`, `eject-film` options | gated (inactive elsewhere) | none |
| `genesys.cpp`, `sane_open_impl` / `sane_close_impl` | the process lock with its RAII guards; lamp-off, `clear_halt` and `reset` skipped at close | gated | none — the guard is armed only for GL126 |
| `genesys.cpp`, `genesys_flatbed_calibration` | `init_shading_data` and `move_to_ta` skipped | gated | none |
| `genesys.cpp`, `genesys_start_scan` | home / TA moves, calibration-cache restore, `write_registers`, the post-`begin_scan` waits skipped; `load_document` also called for GL126 | gated | one line is not: `dev->parking = false` after the `if (dev->parking)` block now runs for every ASIC. Upstream's `sanei_genesys_wait_for_home` already clears `parking` on entry, so this is a redundant assignment, not a behaviour change |
| `genesys.cpp`, `calculate_scan_settings` | `settings.frame = s->frame` | additive | none — the field is unused outside GL126 |
| `image_pipeline.cpp`, `ImagePipelineNodeExtract::get_next_row_data` | bytes per pixel computed from the row format instead of `depth / 8` | **shared bug fix, not gated** | any user of `ImagePipelineNodeExtract` on a multi-channel format copied one third of each row and zero-padded the rest. In the base revision no in-tree model reaches that node with a multi-channel format (GL126's IR crop was the first), so no existing model's output changes; a reviewer may still want this as its own commit, and the next series revision makes it one |
| `test_scanner_interface.cpp` | seeds regs 0x01 and 0x101 for GL126 in testing mode | gated | none |
| `tables_model.cpp`, `tables_sensor.cpp` | the model and sensor entries | additive | none. **Found in this review:** the model comment still called the entry a "stage 1 skeleton, untested" with placeholder ids "replaced during bring-up", and `ModelFlag::UNTESTED` was still set although the `.desc` says `:good`. Corrected in the integration patch 2026-09-15: the adc/gpio/motor ids stay the 7200's because the model must register valid ids and no GL126 hook consults those tables; the flag is gone. The `settings.h` comment said frames 1–4; it is 1–6 |
| `Makefile.am`, `genesys.conf.in`, `.desc`, man page, `AUTHORS` | build list, USB id, model entry, chip list, author | additive | none |

**Offline coverage of the shared changes.** The op and geometry suites
(`tests/test_sane_ops.py`, `test_sane_geometry.py`) prove the GL126 path
byte-exact against the Python driver; `tests/gl126_session_probe.cpp` runs
the real `calculate_scan_session` and the pipeline in `pull` mode against
a counting pattern mock, which is what caught the `Extract` bug (Test 69).
Nothing in this repository exercises another ASIC through the modified
functions, and nothing can without that hardware: for the gated hunks the
argument is the gate itself, for the `Extract` fix it is the analysis
above. That is the honest extent of the regression evidence.

**Best-effort waits, at the site.** The generated tables now carry a
trailing comment on every `PollBestEffort` op saying why that wait may
time out and continue (22 sites: the cold start's ready and settle polls,
its motor completions, the device-open status read, one lenient reg 0x32
read in LOAD, and the eject completion loop). Motor completions in JOG
and LOAD are `PollMasked` and fail closed. `gl126_ops.h` documents the
kinds; `tools/gen_sane_tables.py` refuses to emit a best-effort site it
has no reason for.

## 7. What remains before anything could be submitted

**These steps are one submission-time mission**, frozen with its
decisions in **[docs/sane-submission-runbook.md](sane-submission-runbook.md)**:
the rebase, the `tstbackend -l 1` conformance run against the rebased
build, and Christian's go/no-go. They are version-bound and run
together the day the code is ready, not before.

Listed so the decision is informed, not to schedule it.

1. **Re-fetch and rebase.** The base is `origin/master` as of 2026-09-02.
   Upstream will have moved.
2. **Run `scanimage -T` and `tstbackend`** against the real device, or
   state explicitly that they were not run. Both need hardware.
3. **The generated-table question, answered rather than avoided.**
   `gl126_tables.cpp` is 1.9 MB of generated data whose generator lives
   in this repository. The prepared answer: the tables are the vendor's
   register sequences, verified byte-exact against USB captures and on
   hardware, and every calibration value that varies is injected at run
   time rather than baked in; the file header records the generator, its
   inputs and the revision. Reducing the representation would mean
   re-deriving semantics that were reverse-engineered as sequences, which
   is a rewrite of hardware-verified code for appearance. If a maintainer
   asks for the generator, it can be offered as a follow-up; the captures
   themselves are not published. What is *not* prepared is a smaller
   file, on purpose.
4. **Refresh the series (next revision).** Since the export: best-effort
   poll reasons in the generated tables, the model-table comment and
   `UNTESTED` flag, the `settings.h` comment, the lock-file hardening,
   and the `ImagePipelineNodeExtract` fix moved to its own first commit.
   Rebase on current master, rebuild standalone, re-run the checks on
   the branch, re-export to `sane/wp3-package/`.
5. **Decide how much of the magazine machinery to offer.** Items 6 and 7
   of §6 are the two most likely to be challenged; the file handling
   behind them is hardened and documented in `gl126_lock.h`.
6. **Christian's own decision on contact.** Nothing in this package
   initiates it, and nothing should without him doing it himself.
