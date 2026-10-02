# WP-3 package — the exact SANE submission series, exported

**Status: merge request !1032 is open (v5 submitted 2026-09-29,
<https://gitlab.com/sane-project/backends/-/merge_requests/1032>); this
directory now holds revision v6, prepared locally on 2026-10-02 and NOT
yet pushed to the merge request.** v6 exists because upstream moved: it
is v5 rebased onto current upstream. The series is exactly the
repository's `sane/` (the nine `gl126_*` files and
`gl126-integration.patch`), as four commits in two forms that recreate
it without manual reconstruction.

## Why v6 exists

After v5 was submitted, upstream merged a series adding the GL128 ASIC
(Plustek OpticFilm 8200i SE) that touches the same lines of
`backend/Makefile.am`, `backend/genesys/enums.h`, `low.cpp` and
`scanner_interface_usb.cpp`, so the merge request no longer merged
cleanly. v6 is v5 rebased onto upstream `7103e09b0`:

- Every conflict was of the kind "both sides added a line at the same
  spot" (a source list, an enumerator, an include, a `case` label, an
  `||` condition). Both lines were kept, GL126 before GL128, in the
  style upstream used for GL128. No GL126 code changed to resolve them.
- The first commit of v5, the `ImagePipelineNodeExtract`
  bytes-per-pixel fix, is **dropped**: upstream now carries an
  equivalent fix (`2bf54be64`), the rebase took upstream's version
  and the commit became empty. The series is four commits, not five.
- The nine GL126 source files are byte-identical to v5's. The patches of
  the model entry, the options and the documentation commits (2 to 4 here)
  are identical to v5's apart from context lines; commit 1 (the ASIC
  support) additionally carries the conflict resolutions above.

v5 (2026-09-29): base `f8b5e16`, five commits, tip `c026a333a6ff8b85a789e919f07047ac7140f3f9`,
tip tree `b166a3daf8fd4da719cf52657e3c4e78e73ab002`. Earlier revisions
v1 to v4 are described in `docs/sane-wp3-submission.md`; v4 and v5 differ
from each other only by dead-code removal, v3 and v4 only in comments.

## What is in here

| file | what |
|---|---|
| `wp3-gl126-submission.bundle` | `git bundle` of the series, `7103e09b0..wp3-gl126-submission-v6`. Preserves the exact commit ids. |
| `0001-genesys-add-support-for-the-GL126-ASIC.patch` | commit 1 |
| `0002-genesys-add-the-Plustek-OpticFilm-135i-07b3-1436.patch` | commit 2 |
| `0003-genesys-frame-selection-and-magazine-handling-for-th.patch` | commit 3 |
| `0004-genesys-document-the-GL126-and-the-OpticFilm-135i.patch` | commit 4 |

## Revisions

Base: sane-backends `7103e09b03aa8c7041c75e12f4d51fdf511be151` — "Merge
branch 'genesys-gl128-opticfilm-8200i-se' into 'master'", `origin/master`
as fetched 2026-10-02 from `https://gitlab.com/sane-project/backends.git`.

| # | commit | tree | subject |
|---|---|---|---|
| 1 | `7c2f94b915e335d0d5882077dc279e795f48dfa8` | `4be67132f0d14890316156300c5d33d63de2bf40` | genesys: add support for the GL126 ASIC |
| 2 | `38b59041b7c80d9b2bbd90ad4ee9d10656d5bcc0` | `9a927927a96a5c27cd56b89b4b4b01a9dc8a406c` | genesys: add the Plustek OpticFilm 135i (07b3:1436) |
| 3 | `89352b4ac6f8085b18f40adb7b44ea7d26422062` | `8e2959ebad7496fdafd4850903291daba5b1e119` | genesys: frame selection and magazine handling for the OpticFilm 135i |
| 4 | `ec21b712f1c120c6cc6d041f98385dcdb1876dde` | `83568e8e8eaf8d77add24174b2b473e819198c0a` | genesys: document the GL126 and the OpticFilm 135i |

The tip tree `83568e8e…` is the identity of the package: any route below
that ends on that tree has recreated it exactly.

### Hardware evidence, and whether it applies

The hardware tests (`docs/test-log.md`, Tests 62 to 92) and the
`tstbackend -l 1` run (Tests 93, 94) were made on earlier builds of the
same GL126 sources. **v6 has not been run on hardware.** The GL126
source files are byte-identical to v5's, and v5's compiled code to v4's.
What differs in a v6 build is upstream's shared code merged in between.
It is conditional on GL128 almost everywhere (per-chip `case` labels,
`AsicType::GL128` guards in `low.cpp`, `genesys.cpp` and
`scanner_interface_usb.cpp`, model, sensor, motor and frontend table
entries). The exception is the image pipeline: `ImagePipelineNodeMergeColorToGray`
now takes a colour filter, which is `NONE` for GL126's host-side gray
(the luminance weights are unchanged). A hardware run of the v6 build has
not been made.

## Recreate it

Either route needs a clone of sane-backends containing the base commit.

**Route A — the bundle (exact commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git fetch /path/to/wp3-gl126-submission.bundle wp3-gl126-submission-v6
git checkout -b wp3-gl126-submission FETCH_HEAD
git rev-parse HEAD            # ec21b712f1c120c6cc6d041f98385dcdb1876dde
git rev-parse HEAD^{tree}     # 83568e8e8eaf8d77add24174b2b473e819198c0a
```

**Route B — the patches (same trees, new commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git checkout -b wp3-gl126-submission 7103e09b0
git am /path/to/000[1-4]-*.patch
git rev-parse HEAD^{tree}     # 83568e8e8eaf8d77add24174b2b473e819198c0a
```

`git bundle verify wp3-gl126-submission.bundle` lists the one prerequisite
(`7103e09b0…`).

## Build

From the recreated branch, nothing from this repository is needed:

```
./autogen.sh
./configure --sysconfdir=/etc
make -j8 -C lib
make -j8 -C sanei
make -j8 -C backend libsane-genesys.la
nm -D backend/.libs/libsane-genesys.so | grep -c gl126     # 111
```

## Verification of this export (v6, 2026-10-02)

Done offline, with no scanner contact. Results apply to the v6 build.

| check | result |
|---|---|
| Standalone build in a worktree on `7103e09b0` (`autogen`, `configure --sysconfdir=/etc`, `lib`, `sanei`, `backend/libsane-genesys.la`, `-j8`) | exit 0, **0 compiler warnings** from any source file |
| exported symbols | **111** mentioning `gl126` (same as v5) |
| `tests/test_sane_open_params.py` against that build | 7 passed |
| `tests/test_sane_calibration_cache.py` against that build | 6 passed |
| `tests/test_sane_magazine.py` against that build | 42 passed |
| upstream `genesys_unit_tests` (`make -C testsuite/backend/genesys check`) | PASS |
| Route A: fetch from the bundle in a clean repository | tip `ec21b712…`, tree `83568e8e…`, identical |
| Route B: `git am` of the four patches onto `7103e09b0` in a clean repository | tree `83568e8e…`, identical |
| upstream CI `.gitlab-ci.yml` unchanged since `f8b5e16`; local replay of its jobs in the project's CI images | `make-dist` job replay (style check, autogen, configure, `make dist`) exit 0; `fedora-39-clang` replay (clang 17.0.1, `-Werror`) build exit 0 with 0 errors and 0 warnings in the series' files, and `genesys_unit_tests` PASS, after which `make check` stops at `testsuite/backend/escl_test` (`escl.h: requires libcurl, libavahi and libxml2`) — the same stop, same errors, on unmodified upstream `7103e09b0` run through the same dist-tarball path, so it is upstream's, not this series' |
| symlinks in the recreated tree | 0 (`git ls-files -s`, mode 120000) |

Not run: `tstbackend` (any level), `scanimage`, and anything that touches
the scanner. The `tstbackend -l 1` result in the history above is for the
v3 and v4 builds.

## What was pushed, and from where

This package is built and exported locally and committed to
`cgillinger/opticfilm135i-linux`. The owner pushed the v5 branch to his
own fork himself, from the v5 worktree, to open the merge request. v6
has not been pushed anywhere; pushing it to the merge request is his
step. Nothing is pushed to the SANE project's repository from the development
clone, which tracks the real upstream, and no automated session pushed
anything to GitLab.
