# WP-3 package — the exact SANE submission series, exported

**Status: prepared for review. Nothing has been sent.** No merge request,
issue, mail or contact with the SANE project or anyone else, and nothing
here initiates one. This directory holds the five-commit series in two
forms that recreate it without manual reconstruction.

**Superseded in one respect (2026-09-15, after this export):** the
repository's `sane/` now runs the cold-start program's nine motor
completions as `PollMasked` (fail-closed) instead of `PollBestEffort`,
and carries a `gl126_magazine_armed` test checkpoint; this series still
has the earlier form. It must be refreshed from the current `sane/` and
re-exported before any submission (`docs/sane-wp3-submission.md` §7,
item 5). Left as exported so the revisions below stay true of the files
here.

## What is in here

| file | what |
|---|---|
| `wp3-gl126-submission.bundle` | `git bundle` of the series, `7fb102b..wp3-gl126-submission-v2`. Preserves the exact commit ids. |
| `0001-genesys-fix-ImagePipelineNodeExtract-bytes-per-pixel.patch` | commit 1 |
| `0002-genesys-add-support-for-the-GL126-ASIC.patch` | commit 2 |
| `0003-genesys-add-the-Plustek-OpticFilm-135i-07b3-1436.patch` | commit 3 |
| `0004-genesys-frame-selection-and-magazine-handling-for-th.patch` | commit 4 |
| `0005-genesys-document-the-GL126-and-the-OpticFilm-135i.patch` | commit 5 |

## Revisions

Base: sane-backends `7fb102bf...` — "Merge branch 'fix/scanimage-abort-status'
into 'master'", `origin/master` as fetched 2026-09-15 from
`https://gitlab.com/sane-project/backends.git`.

| # | commit | tree | subject |
|---|---|---|---|
| 1 | `077df55d3e264b173cca2a1c317759a9e98d483b` | `e1114f55191f422d5c0288a36878e0ed37aff800` | genesys: fix ImagePipelineNodeExtract bytes-per-pixel for multi-channel rows |
| 2 | `5c6777cf689946a8bab4d574fb0dd0c96a83202d` | `9e5f27015328da7f1799ec75e7665013c8936c63` | genesys: add support for the GL126 ASIC |
| 3 | `b68756e6dc313883c719ebc84fde0f5909339600` | `47b7dab5b192b796c95b28102fb592a12ed29221` | genesys: add the Plustek OpticFilm 135i (07b3:1436) |
| 4 | `a95bd2f8da7821bc899fcd1f1bcf4fe063896197` | `a1f0b93a7bb323ce4050803ff1e213b0764e0fc5` | genesys: frame selection and magazine handling for the OpticFilm 135i |
| 5 | `92a6bdd1ab463a4134709635db825459ce8bf490` | `65a7b8bd1416968ad3630a617e7aebdc9bfcf068` | genesys: document the GL126 and the OpticFilm 135i |

The tip tree `65a7b8bd…` is the identity of the package: any route below
that ends on that tree has recreated it exactly.

### What changed since the previous export (base `1d47d7c`, four commits)

- **Rebased onto current upstream** (`1d47d7c` → `7fb102b`). Upstream
  touched none of the files this series changes (`backend/genesys/`,
  `backend/Makefile.am`, `backend/genesys.conf.in`, the `.desc`, the man
  page, `AUTHORS` all have zero upstream commits in that range), so the
  rebase was clean and no GL126 behaviour changed.
- **The `ImagePipelineNodeExtract` bytes-per-pixel fix is now its own
  commit (1)**, ahead of the ASIC, since it is a latent bug in shared
  code rather than GL126-specific.
- The series carries this session's **lock-file hardening**
  (`O_NOFOLLOW` + `O_NONBLOCK` + a regular-file / single-hard-link check,
  atomic mark write), the **per-site reasons on every best-effort poll**
  in the generated tables, and the **removal of `ModelFlag::UNTESTED`**
  so the runtime warning and the `.desc` `:good` agree.

### Hardware evidence, and whether it still applies

All of it (`docs/test-log.md` Tests 62–79) was produced on the GL126 code
that this series ships, and upstream did not touch `backend/genesys/`
between the old base and `7fb102b`, so the code is byte-identical in
behaviour and **every prior hardware result still applies unchanged.**
Nothing in this revision needs re-verification on hardware; the changes
since the last export are offline (file-handling hardening, comments, a
build flag, the commit split).

## Recreate it

Either route needs a clone of sane-backends containing the base commit.

**Route A — the bundle (exact commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git fetch /path/to/wp3-gl126-submission.bundle wp3-gl126-submission-v2
git checkout -b wp3-gl126-submission FETCH_HEAD
git rev-parse HEAD            # 92a6bdd1ab463a4134709635db825459ce8bf490
git rev-parse HEAD^{tree}     # 65a7b8bd1416968ad3630a617e7aebdc9bfcf068
```

**Route B — the patches (same trees, new commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git checkout -b wp3-gl126-submission 7fb102b
git am /path/to/000[1-5]-*.patch
git rev-parse HEAD^{tree}     # 65a7b8bd1416968ad3630a617e7aebdc9bfcf068
```

`git bundle verify wp3-gl126-submission.bundle` lists the one prerequisite
(`7fb102b…`).

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

## Verification of this export (2026-09-15)

Done in fresh clones/worktrees in a scratch directory, separate from the
development clone:

| check | result |
|---|---|
| Standalone build in a worktree on `7fb102b` (`autogen`, `configure --sysconfdir=/etc`, `lib`, `sanei`, `backend/libsane-genesys.la`, `-j8`) | exit 0, **0 compiler warnings** from any source file (only autotools notices) |
| exported symbols | **107** mentioning `gl126` |
| `tests/test_sane_open_params.py` against that build | 7 passed |
| `tests/test_sane_calibration_cache.py` against that build | 6 passed |
| `tests/test_sane_magazine.py` against that build | 20 passed |
| `tools/gen_sane_tables.py --check` (tables == generator output) | up to date |
| Route B: `git am` of the five patches onto `7fb102b` | tip tree `65a7b8bd…` — **identical** |
| Route A: fetch from the bundle | tip `92a6bdd…`, tree `65a7b8bd…` — **identical** |
| symlinks in the recreated tree | 0 (`git ls-files -s`, mode 120000) |

The full offline gate of this repository (`tools/release_check.py`)
reports FULL VERIFICATION, 325 tests, against the development build (which
includes the same GL126 files); see `docs/offline-checks.md`.

Not run: `scanimage -T`, `tstbackend` (both drive the device; the plan is
in `docs/sane-submission-runbook.md`, and only `tstbackend -l 1` is judged
safe — read-only, no motor — to be run against the final version).

## Nothing leaves this repository

This package is built and exported locally. It is committed only to
`cgillinger/opticfilm135i-linux`. No branch is pushed from the
sane-backends clone, which tracks the real upstream.
