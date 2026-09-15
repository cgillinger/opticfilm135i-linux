# WP-3 package — the exact SANE submission series, exported

**Status: prepared for review. Nothing has been sent.** This directory
holds the four-commit series described in `docs/sane-wp3-submission.md`
in two forms that recreate it without manual reconstruction. No merge
request, issue, mail or contact with the SANE project has been made, and
nothing here initiates one.

## What is in here

| file | what |
|---|---|
| `wp3-gl126-submission.bundle` | `git bundle` of the series, `1d47d7c..wp3-gl126-submission`. Preserves the exact commit ids. |
| `0001-genesys-add-support-for-the-GL126-ASIC.patch` | commit 1, `git format-patch` output |
| `0002-genesys-add-the-Plustek-OpticFilm-135i-07b3-1436.patch` | commit 2 |
| `0003-genesys-frame-selection-and-magazine-handling-for-th.patch` | commit 3 |
| `0004-genesys-document-the-GL126-and-the-OpticFilm-135i.patch` | commit 4 |

## Revisions

Base: sane-backends `1d47d7ca48a0b86b206698dc30594bd5ea47309b`
("Merge branch 'ci_juint_test_reports' into 'master'", `origin/master` as
fetched 2026-09-02 from `https://gitlab.com/sane-project/backends.git`).

| # | commit | tree | subject |
|---|---|---|---|
| 1 | `b998ffbd70da30c5f628937c365f14c17b33e635` | `01ad1b8b33522d0ae56180835c02c551b65c301c` | genesys: add support for the GL126 ASIC |
| 2 | `b85136d4497c11a0fa5d6d8038ded81522465b47` | `f04ed9a0594547d5d2cb731c79a614fbb6924f97` | genesys: add the Plustek OpticFilm 135i (07b3:1436) |
| 3 | `6e61ed56967f941854f81b0bfb64af6cb1bbe2a7` | `dd0d8ef6e5dbeea1fc304aee09b3c213accf5bf4` | genesys: frame selection and magazine handling for the OpticFilm 135i |
| 4 | `05f2ea7016b3bfc93ce16fa9172f90f79de3ef48` | `f3133681db32ee2ef267fd381aeac078dce8db93` | genesys: document the GL126 and the OpticFilm 135i |

The tip tree `f313368…` is the identity of the package: any route below
that ends on that tree has recreated it exactly.

Relationship to this repository at the time of export (2026-09-15): the
nine `backend/genesys/gl126_*` files in the series are byte-identical to
`sane/gl126*` at commit `12d5193`, and the series' changes to shared
genesys files equal `sane/gl126-integration.patch` at that commit plus
the three items commit 4 adds (the `AUTHORS` entry, the `.desc` status
`:untested → :good` with its comment, and the man page's chip list).
Work after that commit (best-effort poll annotations in the generated
tables, the model-table comment and `UNTESTED` flag, the lock-file
hardening) is **not** in this package; it goes into the next series
revision, which will be rebased on current upstream and re-exported here
with a new revision table.

## Recreate it

Either route needs a clone of sane-backends that contains the base commit.

**Route A — the bundle (exact commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git fetch /path/to/wp3-gl126-submission.bundle wp3-gl126-submission
git checkout -b wp3-gl126-submission FETCH_HEAD
git rev-parse HEAD            # 05f2ea7016b3bfc93ce16fa9172f90f79de3ef48
git rev-parse HEAD^{tree}     # f3133681db32ee2ef267fd381aeac078dce8db93
```

**Route B — the patches (same trees, new commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git checkout -b wp3-gl126-submission 1d47d7c
git am /path/to/000[1-4]-*.patch
git rev-parse HEAD^{tree}     # f3133681db32ee2ef267fd381aeac078dce8db93
```

`git bundle verify wp3-gl126-submission.bundle` lists the one prerequisite
(`1d47d7c…`).

## Build

From the recreated branch, nothing from this repository is needed:

```
./autogen.sh
./configure --sysconfdir=/etc
make -j8 -C lib
make -j8 -C sanei
make -j8 -C backend libsane-genesys.la
nm -D backend/.libs/libsane-genesys.so | grep -c gl126     # 107
```

`--sysconfdir=/etc` matters for an installed backend (see
`docs/sane-install.md`); it does not affect the build check itself.

## Verification of this export (2026-09-15)

Done in two fresh clones in a scratch directory, separate from the
development clone and its worktree:

| check | result |
|---|---|
| Route A: fetch from the bundle | `FETCH_HEAD` = `05f2ea7…`, tree `f313368…` — **identical** |
| Route B: `git am` of the four patches onto `1d47d7c` | four commits, per-commit trees `01ad1b8…`, `f04ed9a…`, `dd0d8ef…`, `f313368…` — **identical** |
| symlinks in the recreated tree | 0 (`git ls-files -s`, mode 120000) |
| standalone build in the Route A clone (`autogen`, `configure`, `lib`, `sanei`, `backend/libsane-genesys.la`, `-j8`) | exit 0, 45 s wall; 0 compiler warnings (the log's 12 "warning" lines are all autotools notices from `configure.ac` / `Makefile.am`, none from a source file) |
| exported symbols | 107 mentioning `gl126`, as in the development build |
| `tests/test_sane_open_params.py` against that build | 7 passed |
| `tests/test_sane_calibration_cache.py` against that build | 6 passed |
| `tests/test_sane_magazine.py` against that build | 20 passed |

These results apply to the package identified by tree `f313368…` and to
no other revision. The full offline suite of this repository (316 tests
at `12d5193`) runs against the development build, not the package;
`docs/sane-wp3-submission.md` §3 records that.

To run the three backend-dependent suites against a recreated build:

```
cd /path/to/opticfilm135i-linux
SANE_BACKENDS_DIR=/path/to/sane-backends .venv/bin/python tests/test_sane_open_params.py
SANE_BACKENDS_DIR=/path/to/sane-backends .venv/bin/python tests/test_sane_calibration_cache.py
SANE_BACKENDS_DIR=/path/to/sane-backends .venv/bin/python tests/test_sane_magazine.py
```

Not run: `scanimage -T`, `tstbackend` (both drive the device; see
`docs/sane-wp3-submission.md` §7).

## Hardware evidence the package rests on

Tests 74–79 in `docs/test-log.md`, on one unit; summarised in
`docs/sane-wp3-submission.md` §4. Nothing in this directory was produced
by touching the scanner.
