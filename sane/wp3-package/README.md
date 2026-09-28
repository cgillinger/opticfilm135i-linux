# WP-3 package — the exact SANE submission series, exported

**Status: prepared for review. Nothing has been sent.** No merge request,
issue, mail or contact with the SANE project or anyone else, and nothing
here initiates one. This directory holds the five-commit series in two
forms that recreate it without manual reconstruction.

**Revision v3, exported 2026-09-28.** The series is exactly the
repository's `sane/` (the nine `gl126_*` files and
`gl126-integration.patch`) at the commit that records Test 92 — WP-5's
one-button loading verified on hardware the same day — rebased onto
current upstream. It supersedes v2 (2026-09-15, base `7fb102b`, tip tree
`65a7b8bd…`), which predated the strict cold-start completions, Test 90's
post-eject fix, the digiKam dialog cleanup and WP-5.

## What is in here

| file | what |
|---|---|
| `wp3-gl126-submission.bundle` | `git bundle` of the series, `f8b5e16..wp3-gl126-submission-v3`. Preserves the exact commit ids. |
| `0001-genesys-fix-ImagePipelineNodeExtract-bytes-per-pixel.patch` | commit 1 |
| `0002-genesys-add-support-for-the-GL126-ASIC.patch` | commit 2 |
| `0003-genesys-add-the-Plustek-OpticFilm-135i-07b3-1436.patch` | commit 3 |
| `0004-genesys-frame-selection-and-magazine-handling-for-th.patch` | commit 4 |
| `0005-genesys-document-the-GL126-and-the-OpticFilm-135i.patch` | commit 5 |

## Revisions

Base: sane-backends `f8b5e162829169de68a0995d0b611919dddcd3a4` — "Merge
branch 'saned_unit_tests' into 'master'", `origin/master` as fetched
2026-09-28 from `https://gitlab.com/sane-project/backends.git`.

| # | commit | tree | subject |
|---|---|---|---|
| 1 | `b7b4142eded3a1a15090ed89c819bd286c042d58` | `f1d6d51fdf7809def518fe46a409488a6af29c65` | genesys: fix ImagePipelineNodeExtract bytes-per-pixel for multi-channel rows |
| 2 | `49191cf37aa1304403f1710f3db984694909ce3e` | `d49bc14e5fe41dabbacb12ead9702c75c5c0e362` | genesys: add support for the GL126 ASIC |
| 3 | `ea469909ae046c91459ae6416a208b2a05baa87b` | `24d6ded320c5dfcc85e82c3264e60b72c1796ef8` | genesys: add the Plustek OpticFilm 135i (07b3:1436) |
| 4 | `9ff847a3da90955bed646bc2cdfd3c90b5013381` | `6f12ecc68d3574bcf3a6a4101e60432044d408ad` | genesys: frame selection and magazine handling for the OpticFilm 135i |
| 5 | `030641e1b41f5d439a84dab7450bffc538a0e83d` | `d891db5dfc1e2aa7815180193046e5df153ababe` | genesys: document the GL126 and the OpticFilm 135i |

The tip tree `d891db5d…` is the identity of the package: any route below
that ends on that tree has recreated it exactly.

### What changed since the previous export (v2, base `7fb102b`)

- **Rebased onto current upstream** (`7fb102b` → `f8b5e16`, 14 upstream
  commits). One of them touches a file this series changes: `f561b04`,
  "genesys: count NUL byte in max_string_size for `std::vector`", one
  line in `genesys.cpp`, in a helper this series does not call (the
  `magazine` option's size is a fixed GL126 constant). The integration
  patch applied clean on top of it.
- **The cold start's nine motor completions fail closed** (`PollMasked`
  instead of `PollBestEffort`; 13 best-effort sites remain, each with
  its reason at the site), with the `gl126_magazine_armed` test
  checkpoint.
- **Test 90's post-eject fix:** the next-strip load after an eject no
  longer requires regs 0x3b/0x3c to read 0x00/0x00 (an eject leaves the
  last scan profile's values); only the base-table 0xff/0xff is refused.
- **digiKam dialog cleanup:** eleven genesys options the GL126 hooks do
  not implement are inactive for it; default mode Color, colour filter
  None; the film options grouped and ordered; resolution list ascending.
- **English-only strings** (no translation catalog), the `magazine`
  status line settable as a no-op so KSane renders it legibly.
- **WP-5, one-button loading:** `load-film` releases, waits for the
  loader sensor's out-then-in edge (read-only, up to 120 s) and loads;
  a scan never loads (refuses read-only); new `check-status` option;
  the magazine mark carries released / loaded / ejected / failed across
  processes. Commit 4's message and the `.desc` comment describe it.
  Test-mode-only additions in commit 2 (`test_usb_device.{h,cpp}`
  OUT-transfer counter, `test_scanner_interface` register seeds and
  write counter) support the offline suite for it.
- Commit messages 2 and 4 rewritten for the above; 1, 3 and 5 unchanged
  in substance.

### Hardware evidence, and whether it applies

The GL126 code here is byte-identical to the repository's `sane/` at
`19b7605` (the Test 92 commit), which is what was installed and run for
Tests 90–92; every earlier hardware result (`docs/test-log.md` Tests
62–92) was produced on this code or on a strict subset of it whose
behaviour did not change. The rebase touched none of the GL126 files.
Nothing in this revision needs re-verification on hardware.

## Recreate it

Either route needs a clone of sane-backends containing the base commit.

**Route A — the bundle (exact commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git fetch /path/to/wp3-gl126-submission.bundle wp3-gl126-submission-v3
git checkout -b wp3-gl126-submission FETCH_HEAD
git rev-parse HEAD            # 030641e1b41f5d439a84dab7450bffc538a0e83d
git rev-parse HEAD^{tree}     # d891db5dfc1e2aa7815180193046e5df153ababe
```

**Route B — the patches (same trees, new commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git checkout -b wp3-gl126-submission f8b5e16
git am /path/to/000[1-5]-*.patch
git rev-parse HEAD^{tree}     # d891db5dfc1e2aa7815180193046e5df153ababe
```

`git bundle verify wp3-gl126-submission.bundle` lists the one prerequisite
(`f8b5e16…`).

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

## Verification of this export (2026-09-28)

Done in the v3 worktree and in two fresh clones in a scratch directory,
separate from the development clone:

| check | result |
|---|---|
| Standalone build in a worktree on `f8b5e16` (`autogen`, `configure --sysconfdir=/etc`, `lib`, `sanei`, `backend/libsane-genesys.la`, `-j8`) | exit 0, **0 compiler warnings** from any source file |
| exported symbols | **111** mentioning `gl126` — identical to the development build |
| `tests/test_sane_open_params.py` against that build | 7 passed |
| `tests/test_sane_calibration_cache.py` against that build | 6 passed |
| `tests/test_sane_magazine.py` against that build | 42 passed |
| `tools/gen_sane_tables.py --check` (tables == generator output) | up to date |
| Route B: `git am` of the five patches onto `f8b5e16` in a clean clone | tip tree `d891db5d…` — **identical** |
| Route A: fetch from the bundle in a clean clone | tip `030641e…`, tree `d891db5d…` — **identical** |
| symlinks in the recreated tree | 0 (`git ls-files -s`, mode 120000) |

The full offline gate of this repository (`tools/release_check.py`)
reports FULL VERIFICATION, 389 tests, 0 skipped, against the development
build (which includes the same GL126 files); see `docs/offline-checks.md`.

Not run: `scanimage -T`, `tstbackend` (both drive the device; the plan is
in `docs/sane-submission-runbook.md`, and only `tstbackend -l 1` is judged
safe — read-only, no motor — to be run against the final version).

## Nothing leaves this repository

This package is built and exported locally. It is committed only to
`cgillinger/opticfilm135i-linux`. No branch is pushed from the
sane-backends clone, which tracks the real upstream.
