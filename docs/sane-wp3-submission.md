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
| Branch | `wp3-gl126-submission-v4` |
| Worktree | `~/Dokument/Github/sane-wp3-v4` (a `git worktree` of the sane-backends clone) |
| Base | `f8b5e16`, "Merge branch 'saned_unit_tests' into 'master'", fetched 2026-09-28 |
| Commits | 5 |
| Tip tree | `16671d82bde8b107c921e8d789452dba442ee01f` |

Fourth revision, 2026-09-28 evening: the comment cleanup. Every source
comment and log message that cited the project's private documents,
test-log numbers, work packages, review rounds, reviewers or dates was
rewritten to state the fact without the citation, so the series reads
as ordinary upstream code; one pointer to the public protocol
documentation was added to `gl126.h`'s header and the generated tables'
header. Compiled code identical to v3 (comment-stripped comparison of
all 21 files, generator `--check`, full offline suite). Same base and
commit messages as v3.

Third revision, 2026-09-28: refreshed from the repository's current
`sane/` (everything since the 2026-09-15 export — the strict cold-start
completions, the post-eject precondition fix of Test 90, the digiKam
dialog cleanup, English-only strings, the settable status line, and
WP-5's one-button loading, hardware-verified in Test 92 the same day)
and rebased onto current upstream (from `7fb102b`). Between the two
bases upstream made 14 commits, **one** of them in the files this
series changes: `f561b04`, "genesys: count NUL byte in max_string_size
for `std::vector`", a one-line change in `genesys.cpp` to a helper this
series does not call (the `magazine` option's size is a fixed GL126
constant, not `max_string_size`). The integration patch applied clean on
top of it and nothing in this series interacts with it. Should upstream
move again before a submission, only the then-relevant difference needs
assessing; the package is not invalidated wholesale (§7).

Previous revisions: v3 (2026-09-28, same base, tip tree `d891db5d…`),
v2 (2026-09-15, base `7fb102b`, tip tree `65a7b8bd…`), v1 (2026-09-13,
base `1d47d7c`, four commits). All branches still exist in the
sane-backends clone; the exported package is v4.

The worktree is separate from `~/Dokument/Github/sane-backends`, which
keeps the development arrangement (symlinks into this repo's `sane/`)
untouched. The package contains **no symlinks**: `git ls-files -s` reports
zero mode-120000 entries, and the nine GL126 files are real files
committed to the branch. That was the point of building it this way —
a reviewer clones, builds, and needs nothing from this repository.

**Exported 2026-09-28 to `sane/wp3-package/`** — a bundle and the five
patches, with the base and every commit and tree id, recreation and build
instructions, and the verification (standalone build, symbols, backend
suites, both recreation routes). That is the reviewable form; the worktree
is the working copy. The five commit and tree ids are in
`sane/wp3-package/README.md`.

To recreate it from scratch by hand instead:

```
cd ~/Dokument/Github/sane-backends
git worktree add -b wp3-gl126-submission-v4 ~/Dokument/Github/sane-wp3-v4 f8b5e16
cd ~/Dokument/Github/sane-wp3-v4
cp -L ~/Dokument/Github/opticfilm135i-linux/sane/gl126*.{h,cpp} backend/genesys/
git apply ~/Dokument/Github/opticfilm135i-linux/sane/gl126-integration.patch
# the man page, AUTHORS and .desc-status hunks of commit 5 are not in the
# integration patch (the development clone carries no doc changes):
git apply --include=AUTHORS --include=doc/sane-genesys.man \
    ~/Dokument/Github/opticfilm135i-linux/sane/wp3-package/0005-*.patch
# then commit in the five groups described below (the Extract fix first);
# the .desc status flip is applied from the same 0005 patch before commit 5
```

## 2. The commit series

1. **`genesys: fix ImagePipelineNodeExtract bytes-per-pixel for
   multi-channel rows`** — the one shared-code bug fix, on its own:
   `get_next_row_data()` used the per-channel depth as the pixel stride,
   copying a third of each multi-channel row. No in-tree model reached
   that node with a multi-channel format before GL126's infrared crop, so
   no existing model's output changes.
2. **`genesys: add support for the GL126 ASIC`** — the command set and
   everything it needs: the nine `gl126_*` files (including
   `gl126_lock.{h,cpp}`), the `AsicType` entry and its string mapping,
   two `ScanSession` fields for the dual-light profiles, the pipeline
   hook in `low.cpp`, the GL126 branch in the USB interface's bulk read,
   the test-interface addition and `Makefile.am`.
3. **`genesys: add the Plustek OpticFilm 135i (07b3:1436)`** — the model
   and sensor entries, the USB id in `genesys.conf.in`, the `.desc`
   entry.
4. **`genesys: frame selection and magazine handling for the OpticFilm
   135i`** — the five options (`frame`, `load-film`, `eject-film`,
   `check-status`, `magazine`) in `genesys.{h,cpp}`, the options the
   GL126 hooks do not implement made inactive for it, and the process
   lock in `sane_open`/`sane_close`; all inactive on every other ASIC.
5. **`genesys: document the GL126 and the OpticFilm 135i`** — the man
   page's chip list, an `AUTHORS` entry, and the `.desc` status moving
   from `:untested` to `:good`.

## 3. What was verified, and how

**Build.** Configured and built from the branch alone (v4, 2026-09-28,
`./autogen.sh && ./configure --sysconfdir=/etc`, then `lib`, `sanei` and
`backend/libsane-genesys.la`). Exit 0, **zero compiler errors or
warnings**. This is the check that matters most: it proves the package
stands without this repository.

**Exported symbols.** `nm -D` on the resulting library: **111 mentioning
gl126 — identical to the development build**,
and both the plain `sane_*` and the prefixed `sane_genesys_*` entry
points present as genesys expects.

**Offline tests against the package build.** The three suites that need a
built backend were re-run with `SANE_BACKENDS_DIR` pointed at the
worktree rather than the development tree:

| suite | result |
|---|---|
| `test_sane_open_params` | 7 passed |
| `test_sane_calibration_cache` | 6 passed |
| `test_sane_magazine` | 42 passed |

The full offline suite passes in this repository against the development
build: `tools/release_check.py` FULL VERIFICATION, 389 tests, 0 skipped,
at `19b7605` (the Test 92 commit this package was exported from).

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

**`tstbackend -l 1` — run 2026-09-28 against the v3 build (Test 93) and
again against this v4 build (Test 94): `warnings: 0  error: 0  checks: 22965`, exit 0, both times.** The tool was built
from the package tree's own `frontend/tstbackend.c` and linked directly
against its `libsane-genesys.la`; level 1 covers init/exit, ten
open/close cycles and the option-consistency walk (recursion depth 1;
the default depth 5 is combinatorial and was stopped by a timeout with
the same clean partial report). The backend's log shows 21 control
transfers in the whole run, every one a read of reg 0x01, and zero
writes. Its two info classes are the genesys-wide named groups and the
vendor-string documentation note.

**Not run, deliberately: `scanimage -T` and `tstbackend -l 2` and up.**
Neither is an offline test. Both start a scan and cancel it mid-pass,
and neither can be isolated to a mock. The nearest isolated equivalent already exists
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

*Not sent. Text only, for review. Written to stand on its own as a merge
request description: no dates, no internal document names, no test
numbers. Rewritten 2026-09-28 to that standard.*

> **genesys: support for the GL126 and the Plustek OpticFilm 135i**
>
> This series adds the Genesys GL126 to the genesys backend, and with it
> the Plustek OpticFilm 135i (07b3:1436), a 35 mm film scanner.
>
> The GL126 is close enough to the GL124 for the framework to fit, but
> its scan flow is the vendor's rather than GL124's, so it is implemented
> as its own command set rather than a GL124 model variant. The register
> sequences are generated from USB captures of the vendor driver and
> verified byte-exact against them. The generator is not part of this
> series; the tables it emits are, with the provenance recorded in their
> header. The protocol notes and the reverse-engineering record are
> public: https://github.com/cgillinger/opticfilm135i-linux
>
> The scanner works differently enough from a flatbed to be worth
> describing. It scans one frame of a loaded film strip per pass,
> addressed by number through a `frame` option rather than by a scan
> area, because the geometry is fixed by the holder and positioning is a
> single absolute feed from the load reference. The film magazine is
> handled through three further options. The vendor's own insert flow
> requires the operator to remove the magazine and re-seat it to a
> mechanical stop in the middle of the sequence, and SANE offers no way
> to ask for that during `sane_start`, so `load-film` does the whole
> thing in one call: it releases the magazine, polls the loader sensor
> read-only (up to 120 s) until it has seen the magazine taken out and
> pushed back in, and then loads it. A scan never loads the magazine
> itself; with nothing loaded, `sane_start` refuses after one register
> read. `eject-film` ejects, and `check-status` re-reads the hardware.
> A `magazine` option tells the operator the next step. It accepts a SET
> so that a frontend which greys out strictly read-only options still
> renders it legibly, but its value is fixed by the state machine, not
> by the caller. All five options are inactive on every other ASIC, and
> the genesys options the GL126 does not implement are inactive for it.
>
> The motor waits fall into two kinds, and which kind each one is, is
> marked at the site in the generated tables.
>
> The waits that gate a film-bearing move or a scan fail closed: the feed
> and traverse completions of the load and of the release jog, the
> per-frame positioning move, and the park. A timeout on any of these,
> like an unacknowledged write or a short transfer, ends the sequence
> with nothing further written and no recovery attempted, because this
> hardware has a documented history of stalling when driven from an
> undefined state. The nine motor completions inside the vendor's
> power-on sequence fail closed as well. Each is the only wait between
> one motor start and the next, and a timeout that continued would start
> the following move, up to eight of them before the closing check, on
> an engine not known to have finished. Every logged cold start, with
> the magazine latched or loose, has completed each of the nine moves in
> 1.0 to 1.9 s, so this rule changes no observed run, only the
> never-observed one.
>
> The remaining waits inside the power-on sequence (its opening ready
> poll, the per-round ready polls and the settle reads) and the eject
> completion are best-effort: a timeout is recorded and the sequence
> continues. This is not a relaxation of the rule above but a
> consequence of it. The opening ready poll times out on every cold
> start, because at power-on the engine is not yet in the class it waits
> for; a latched magazine adds nothing to that, and the sequence must
> still complete, since freeing a latched magazine after a power cycle
> is a required, supported operation. The eject completion is checked
> against the eject-done state rather than full idle, which a successful
> eject never re-enters. What makes these safe is that none of them
> separates one motor start from the next, and that the gates are
> downstream and hard: after the power-on sequence the backend reads
> reg 0x01 and fails the session unless it is the idle-homed 0x22;
> before the load it re-reads reg 0x01, the loader sensor and the
> base-table registers and refuses unless the unit is idle-homed with
> the magazine present; and every session opens with the same
> start-state check. So a best-effort timeout never reaches a
> film-bearing move. It leaves the transport in a state the next check
> evaluates, and the recovery is a power cycle, never an automatic
> retry.
>
> Testing: one unit, over an extended bring-up. Every resolution the
> vendor's captures cover (600, 1200, 2400, 3600, 7200 dpi) plus the
> infrared pass has been scanned through the backend from `scanimage`.
> The backend-driven magazine flow, a full load, scan and eject cycle
> with no external command, has been run from `scanimage` and from
> digiKam, including a power-cycled unit with a latched magazine, the
> 120 s timeout and both refusals. `tstbackend -l 1` passes against this
> series (22 965 checks, 0 warnings, 0 errors, no writes to the device);
> `tstbackend -l 2` and `scanimage -T` were not run, because they cancel
> a scan mid-pass, which on this unit needs a power cycle, and there is
> one unit. Interrupting a scan mid-pass leaves the transport unparked
> and needs a power cycle; no automatic recovery exists, by design.
> Known limitations are listed below; the most important is that a
> single unit exists for this work, so nothing here is verified across
> units.

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
   reused (a restored cache made `begin_scan` refuse), so every scan
   pays roughly four seconds of calibration.
5. **The interrupt endpoint is not drained.** The genesys USB abstraction
   has no interrupt transfer. Nothing in the SANE flow reads that
   endpoint, so nothing here is harmed. Whether a backend-driven load
   leaves it in an overflow state for other software was predicted but
   not observed: the one direct check afterwards on hardware read the
   endpoint normally.
6. **A process lock shared with an external driver.** The backend takes a
   `flock` on a well-known path to keep itself and the reverse-engineered
   Python driver off the device simultaneously. No other genesys ASIC
   needs this, and a reviewer may reasonably question it.
7. **State on disk.** The magazine state (released, loaded, ejected or
   failed) is recorded beside that lock so `scanimage`, which reaches the
   backend in a fresh process each invocation, knows whether a scan may
   start and whether the next `load-film` needs a release. Never trusted
   alone: the hardware is re-read before any motor move. Also likely to
   draw questions.
8. **2400 dpi is anisotropic** (3600 across, 2400 along) and is resampled
   on the host so delivered pixels are square.
9. **Colour rendering is not addressed.** The backend delivers linear raw
   data; rendering a negative is the frontend's job. The companion
   driver's preview shows a cast on some strips that also appears in the
   vendor's own rendering and has not been traced to any code
   (the analysis is in the companion repository). Nothing in this series depends
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
| `image_pipeline.cpp`, `ImagePipelineNodeExtract::get_next_row_data` | bytes per pixel computed from the row format instead of `depth / 8` | **shared bug fix, its own commit (commit 1)** | any user of `ImagePipelineNodeExtract` on a multi-channel format copied one third of each row and zero-padded the rest. No in-tree model reaches that node with a multi-channel format (GL126's IR crop was the first), so no existing model's output changes. It is the first commit of the series, standalone and separately reviewable |
| `test_scanner_interface.cpp` | seeds regs 0x01 and 0x101 for GL126 in testing mode | gated | none |
| `tables_model.cpp`, `tables_sensor.cpp` | the model and sensor entries | additive | none. **Found in this review:** the model comment still called the entry a "stage 1 skeleton, untested" with placeholder ids "replaced during bring-up", and `ModelFlag::UNTESTED` was still set although the `.desc` says `:good`. Corrected in the integration patch 2026-09-15: the adc/gpio/motor ids stay the 7200's because the model must register valid ids and no GL126 hook consults those tables; the flag is gone. The `settings.h` comment said frames 1–4; it is 1–6 |
| `Makefile.am`, `genesys.conf.in`, `.desc`, man page, `AUTHORS` | build list, USB id, model entry, chip list, author | additive | none |

**Addendum, 2026-09-27 (offline — digiKam dialog usability review,
`docs/ROADMAP.md`).** Six more hunks in the shared files (two added in a
follow-up review round the same day); folded into the exported package
in `sane/wp3-package/` with the 2026-09-28 re-export (v3).

| file | change | class | effect on other ASICs |
|---|---|---|---|
| `genesys.h` | `OPT_FILM_GROUP` inserted between the enhancement and extras groups; the four film options (from the row above) moved under it and reordered to `magazine`, `load-film`, `eject-film`, `frame` | additive + reorder | option **indices** shift again for every model past `OPT_CONTRAST` — the same class of change as the original four-option addition above; still addressed by name, still `SANE_CAP_INACTIVE` on every other ASIC |
| `genesys.cpp`, `init_options` (the film options' text) | `magazine`'s title changed from "Magazine" to "Film magazine" and its desc shortened ("believed to be" dropped); `Load film`'s desc shortened and reworded (drops the "freshly powered-on" phrasing in favour of "cold scanner", states the ~25 s figure plainly); `Eject film`'s desc rewritten to describe the next-strip flow instead of just naming the action; `Frame`'s desc gained a trailing period. Titles/values a translator sees, not behaviour | additive (text only) | none — gated the same as the options themselves |
| `genesys.cpp`, `init_options` | eleven existing options (`scan-exposure-time`, `brightness`, `contrast`, `lamp-off-time`, `lamp-off-scan`, `color-filter`, `calibration-file`, `expiration-time`, `clear-calibration`, `force-calibration`, `ignore-internal-offsets`) get `SANE_CAP_INACTIVE` added when `asic_type == AsicType::GL126`. Ten of the eleven were already inert on GL126 (no consumer reads them for this chip) and are only hidden. `color-filter` is different: its GL126-specific branch changes the **default value**, not just visibility — from the generic branch's `"Green"` (a single-channel capture `calculate_scan_session` refuses before any device I/O, Test 62) to `"None"` (host-side gray, a capture this backend performs) — so a Gray-mode scan on GL126 that previously failed at `sane_start` now succeeds. This is a behaviour change for GL126, not merely hiding a dead control | gated | none for every other ASIC — the pre-existing branches are unchanged; the behaviour change is GL126-only |
| `genesys.cpp`, `init_options`, `OPT_MODE` | `s->mode` set to `SANE_VALUE_SCAN_MODE_COLOR` for GL126, after the generic `SANE_VALUE_SCAN_MODE_GRAY` default line | gated | none — the generic default line is unchanged; GL126 overrides it immediately after with its own default |
| `genesys.cpp`, `set_option_value`, `OPT_MODE` (Gray branch) | the pre-existing `ENABLE(OPT_COLOR_FILTER)` on switching to Gray is now also gated off for GL126 (found in a follow-up review the same day: without this, picking Gray from a live dialog reopened the hidden option with its stock default, undoing the `init_options` default above and putting an option in front of the operator that `sane_start` would then refuse) | gated | none — the existing condition (`GL646 && is_cis`) is unchanged for every other ASIC; only the added `&& asic_type != GL126` term is new |
| `genesys.cpp`, `set_option_value`, `OPT_BIT_DEPTH` | the pre-existing `ENABLE(OPT_CONTRAST)`/`ENABLE(OPT_BRIGHTNESS)` at depth ≤ 8 is likewise gated off for GL126. Latent today — this model's `bpp_gray_values`/`bpp_color_values` are both `{16}`, so the ≤ 8 branch is never reached — gated anyway so the same contradiction cannot appear if that ever changes | gated (currently unreachable for GL126) | none |

**Addendum, 2026-09-27 evening (Test 91 — the first live digiKam session,
and two libksane display bugs found by reading `LabeledCombo`'s source;
`docs/sane-install.md` §6 "Two libksane display bugs").** One more hunk,
folded into `sane/wp3-package/` with the 2026-09-28 re-export (v3). Earlier the same evening a
Swedish `po/sv.po` catalog for this port's strings was added and briefly
installed; the live session it enabled found the bugs above, and the
owner decided afterwards that the backend should carry no translations at
all, so that catalog and its `po/sv.po` / `po/POTFILES.in` entries were
removed the same evening. There is no translation row here for that
reason — there is no translation. The seven magazine status strings
(`sane/gl126.cpp`, one of this port's own files, not shared code) are
correspondingly no longer wrapped in `SANE_I18N`; that change needs no row
here either, for the same reason the strings never did — it is entirely
inside the nine `gl126_*` files this table does not cover.

| file | change | class | effect on other ASICs |
|---|---|---|---|
| `genesys.cpp`, `set_resolution_option_values` | for GL126 only, the resolution word list handed to the frontend is reversed to ascending (600 first) before being copied into `opt_resolution_values`; the value SELECTION below it (nearest-value pick, min-element default) is unchanged and remains order-independent | gated | none — every other ASIC's list is built from the same `get_resolutions()` call, still returned descending as `device.cpp`'s `MethodResolutions::get_resolutions()` always sorts it; only the GL126 branch reverses its own copy afterward |

**Addendum, 2026-09-27, later the same evening (this task -- the
digiKam dialog's second live session, `docs/test-log.md` Test 91's
second paragraph, and the owner's verdict "no one can do this process
without a written manual").** One more hunk in shared code, folded into
`sane/wp3-package/` with the 2026-09-28 re-export (v3).

| file | change | class | effect on other ASICs |
|---|---|---|---|
| `genesys.cpp`, `init_options` (`OPT_MAGAZINE`) | `cap` changed from `SANE_CAP_SOFT_DETECT` (read-only) to `SANE_CAP_SOFT_SELECT \| SANE_CAP_SOFT_DETECT` (settable); title changed from "Film magazine" to "Magazine -- next step"; desc rewritten from one sentence to the whole four-step procedure | gated (inactive elsewhere) | none |
| `genesys.cpp`, `set_option_value` (new `case OPT_MAGAZINE`) | a SET handler that changes nothing: it sets only `SANE_INFO_RELOAD_OPTIONS`, so the frontend re-reads and re-displays the true value regardless of what was sent | additive (a new case in a switch shared by every ASIC, reached only when `option == OPT_MAGAZINE`; `SANE_CAP_INACTIVE` on every other ASIC, so the case is never dispatched there) | none |

**Rationale.** KSaneWidgets renders a `SANE_CAP_SOFT_DETECT`-only option
(this port's own prior choice -- "read-only, so the widget can't be
misused") DISABLED: grey label, grey value. That made the status line
-- the operator's only channel for "what do I do next", since SANE has
no dialogs -- effectively unreadable in the second live session, even
though its values were already short and correctly ordered (Tests
76/91). Adding `SANE_CAP_SOFT_SELECT` makes the option render enabled
without giving it anything real to control: `sanei_constrain_value`
(SANE core, unmodified) still rejects any value outside the seven
listed ones before the handler ever runs, and a value that IS listed
is accepted (`SANE_STATUS_GOOD`) and then immediately overridden by the
`RELOAD_OPTIONS`-driven re-read -- the state machine and the on-disk
mark are untouched either way. Tested through the real
`sane_control_option` path (not the handler in isolation) in
`tests/test_sane_magazine.py`'s `test_setting_the_status_line_is_a_no_op`
(probe scenarios `magazine-set-accepts-a-listed-value-as-a-no-op` and
`magazine-set-rejects-an-unlisted-value`, `tests/gl126_magazine_probe.cpp`).

The seven value texts (each now names the frontend's own button,
"Scan", instead of a generic "scan") and the tooltip's procedure text
changed too, but both live entirely inside `sane/gl126.cpp` -- one of
this port's own nine files, not shared code -- so, like the
English-only change in the addendum above, they need no row here.

**Addendum, 2026-09-27, WP-5 (one-button loading,
`docs/sane-wp5-load-button.md`).** Supersedes the two-step protocol the
addendum above still describes: `OPT_LOAD_FILM` now runs release, wait
and load in one call, and `OPT_MAGAZINE`'s twelve values (was seven)
include four `Check status`-only diagnostic texts. Four hunks in shared
code, folded into `sane/wp3-package/` with the 2026-09-28 re-export (v3),
after Test 92 verified the flow on hardware.

| file | change | class | effect on other ASICs |
|---|---|---|---|
| `genesys.h` | one new `Genesys_Option` value, `OPT_CHECK_STATUS`, inserted between `OPT_EJECT_FILM` and `OPT_FRAME` | additive | option **indices** from `OPT_FRAME` onward shift by one more, for every model -- same class of change as the two option-enum additions above; addressed by name, `SANE_CAP_INACTIVE` on every other ASIC |
| `genesys.cpp`, `init_options` | the `OPT_CHECK_STATUS` button declared (type, title, desc, unit, constraint); added to the GL126-only active set and to the non-GL126 `SANE_CAP_INACTIVE` block alongside the other three film options; `OPT_LOAD_FILM`'s desc rewritten for the one-button procedure | gated (inactive elsewhere) | none |
| `genesys.cpp`, `set_option_value` (new `case OPT_LOAD_FILM` body, new `case OPT_CHECK_STATUS`) | `OPT_LOAD_FILM`'s case now calls `gl126::magazine_load_film()` (was `magazine_release()`, a plain rename of the C++ function it forwards to -- the case's own three lines are otherwise the same shape); `OPT_CHECK_STATUS`'s case calls `gl126::magazine_check_status()` and sets `SANE_INFO_RELOAD_OPTIONS`, the same pattern the other two film buttons already use | additive (a new case in a switch shared by every ASIC, reached only when `option == OPT_CHECK_STATUS`; unreachable elsewhere since the option is `SANE_CAP_INACTIVE` there) | none |
| `test_scanner_interface.{h,cpp}` | GL126's test-mode constructor now also seeds regs `0x3b`/`0x3c` to `0x00`, alongside the pre-existing `0x01`/`0x101` seeds; **review round two (finding F)**: every `write_*` method (`write_register`, `write_registers`, `write_0x8c`, `bulk_write_data`, `write_buffer`, `write_gamma`, `write_ahb`, `write_fe_register`) now increments a counter, exposed as `write_count()` alongside the pre-existing `out_transfer_count()`; a new `seed_register()` writes the cache directly, uncounted, for test setup | gated (the register seeds, `AsicType::GL126` branch only) / additive, test-mode only (the counter and `seed_register()`) | none -- test-mode-only, no other ASIC's branch touched, no production caller |
| `test_usb_device.{h,cpp}` (in commit 2 of the series since v3) | a small `out_transfer_count()` counter on the test-mode USB mock, incremented on every OUT control transfer and `bulk_write` | additive, test-mode only | none -- these are test-harness classes with no production caller |

**Rationale for the register-seed and counter additions.** Both are
test-harness fixes needed by the new offline suite
(`tests/test_sane_magazine.py`'s edge-wait tests), not production
behaviour changes: `magazine_check_status_impl()` reads regs 0x3b/0x3c
unconditionally, and `RegisterCache::get()` (test-mode only) throws for
an address never written, unlike real hardware, which always answers a
register read with something; the counter lets a test assert "the edge
wait's own polls put nothing on the wire" without a new mechanism in
genesys core. Neither has a code path reachable by any other ASIC in any
mode other than the backend's own unit tests.

The renamed C++ functions themselves (`magazine_release` ->
`magazine_load_film`, `magazine_load_if_pending` ->
`magazine_check_scan_allowed`, both `gl126.h`/`gl126.cpp`), the new
`wait_for_magazine_edge()`, `magazine_check_status_impl()`, the third
mark kind `MagazineMarkKind::Loaded` (`gl126_lock.{h,cpp}`) and the
twelve status-value constants all live entirely inside the nine
`gl126_*` files, not shared code, so -- like every other GL126-only
change in this document -- they need no row here.

**Review round two (2026-09-27, later the same day, docs/sane-wp5-load-
button.md §9.5): nine more findings (A-I), all fixed inside `gl126.cpp`/
`gl126_lock.{h,cpp}` -- a fourth mark kind (`Failed`), the post-edge
presence+class check, the pre-wait mark write, the Ejected-origin retry
fix, the `saw_clear` debounce, and the `load_document()` cold read (item
J) -- add NO new shared-code rows beyond the `test_scanner_interface.{h,
cpp}` row already updated above (finding F's write-counting).** Every
other fix is entirely inside this port's own nine files.

**Offline coverage of the shared changes.** The op and geometry suites
(`tests/test_sane_ops.py`, `test_sane_geometry.py`) prove the GL126 path
byte-exact against the Python driver; `tests/gl126_session_probe.cpp` runs
the real `calculate_scan_session` and the pipeline in `pull` mode against
a counting pattern mock, which is what caught the `Extract` bug (Test 69).
Nothing in this repository exercises another ASIC through the modified
functions, and nothing can without that hardware: for the gated hunks the
argument is the gate itself, for the `Extract` fix it is the analysis
above. That is the honest extent of the regression evidence. The 2026-09-27
addendum's hunks are covered the same way: `tests/test_sane_magazine.py`'s
`test_dead_options_are_inactive_and_film_group_is_placed_and_ordered` walks
every option descriptor of the built backend in test mode for the GL126
device id, asserting the eleven options are inactive, the `Film` group
sits between `Enhancement` and `Extras` with its four options in order
right after it, and the default mode/colour filter are `Color`/`None`; the
same walk against a GL124 device id checks only two things by name —
`brightness`/`contrast` stay active (proving the new `DISABLE` block did
not leak past its `asic_type` gate) and the `Film` group/its four options
stay inactive (the pre-existing per-option gate, unchanged) — it does not
walk or assert anything about GL124's other options. The same test also
covers the 2026-09-27 evening addendum's resolution-order hunk: it reads
the built backend's `resolution` word-list constraint (in option/display
order, via `tests/gl126_magazine_probe.cpp`'s `layout` command) and
asserts it is ascending with 600 first for the GL126 device id — nothing
here exercises GL124's list, so the claim that other ASICs are unaffected
rests on the gate (`asic_type == AsicType::GL126`) alone, the same as
every other row in that addendum. `test_switching_to_
gray_does_not_reopen_hidden_options_on_gl126` covers the two `ENABLE` gate
fixes: on GL126, setting mode to Gray through the real option path leaves
`color-filter`/`brightness`/`contrast` inactive; on GL124 the same set
still re-enables `color-filter` (the pre-existing behaviour, checked by
name, unchanged).

**Best-effort waits, at the site.** The generated tables carry a
trailing comment on every `PollBestEffort` op saying why that wait may
time out and continue (13 sites: the cold start's opening ready poll,
its per-round ready polls and settle reads, the device-open status
read, one lenient reg 0x32 read in LOAD, and the eject completion loop).
Every motor completion — the cold start's nine, JOG's four, LOAD's two —
is `PollMasked` and fails closed, and each of those sites is marked so
too. `gl126_ops.h` documents the kinds; `tools/gen_sane_tables.py`
refuses to emit a best-effort site it has no reason for. (Until
2026-09-15 the cold start's nine completions were best-effort as well,
22 sites in all; the exported package carried that form until the
2026-09-28 re-export.)

## 7. What remains before anything could be submitted

The rebase and the package preparation are **done for this revision**
(v3, 2026-09-28): the series is current with the repository's `sane/`
and with upstream `f8b5e16`, builds standalone and is exported to
`sane/wp3-package/`. What remains is frozen in
**[docs/sane-submission-runbook.md](sane-submission-runbook.md)**: a
re-check against whatever upstream is at submission time (re-rebase only
if it has touched the affected paths), the `tstbackend -l 1` conformance
run against the final build, and Christian's go/no-go.

Listed so the decision is informed, not to schedule it.

1. **Re-fetch and rebase — done for this revision.** Rebased onto
   `f8b5e16` (2026-09-28); upstream had touched one affected file with
   one line this series does not use (§1), and the patch applied clean.
   If upstream moves again before a submission, re-assess only the
   then-relevant difference.
2. **Run the SANE test tools, or state they were not run — done for
   this revision.** `tstbackend -l 1` ran 2026-09-28 against the v3
   build (Test 93) and again against v4 (Test 94): 22 965 checks, 0
   warnings, 0 errors, zero writes to the device, both times. `tstbackend -l 2+` and `scanimage -T` drive the motor and
   cancel a scan mid-pass and are documented as not run (§3). A later
   revision of the package needs the run repeated, since the evidence
   is version-bound.
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
4. **Refresh the series — done, twice.** v2 (2026-09-15) folded in the
   best-effort poll reasons, the model-table `UNTESTED` removal, the
   `settings.h` comment and the lock-file hardening, and split the
   `ImagePipelineNodeExtract` fix into its own first commit. v3
   (2026-09-28) folds in everything since: the strict cold-start
   completions and the `gl126_magazine_armed` checkpoint, Test 90's
   post-eject precondition fix, the digiKam dialog cleanup (dead options
   inactive on GL126, Color default), English-only strings, the settable
   status line, and WP-5's one-button loading with its `check-status`
   option and the test-mode counters (§8's addenda, all now in the
   package). Rebuilt standalone (0 warnings, 111 gl126 symbols), the
   three backend suites pass against it, and it is re-exported to
   `sane/wp3-package/`. v4 (2026-09-28 evening) is v3 with the source
   comments and log messages cleaned of every internal citation; code
   identical. The package is exactly the repository's `sane/` at the
   commit that records the cleanup.
5. **Decide how much of the magazine machinery to offer.** Items 6 and 7
   of §6 are the two most likely to be challenged; the file handling
   behind them is hardened and documented in `gl126_lock.h`.
6. **Christian's own decision on contact.** Nothing in this package
   initiates it, and nothing should without him doing it himself.
