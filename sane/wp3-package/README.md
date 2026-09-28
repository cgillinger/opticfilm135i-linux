# WP-3 package — the exact SANE submission series, exported

**Status: prepared for review. Nothing has been sent.** No merge request,
issue, mail or contact with the SANE project or anyone else, and nothing
here initiates one. This directory holds the five-commit series in two
forms that recreate it without manual reconstruction.

**Revision v4, exported 2026-09-28 (evening).** The series is exactly
the repository's `sane/` (the nine `gl126_*` files and
`gl126-integration.patch`) after the comment cleanup that followed Test
92, rebased onto current upstream. Its compiled code is identical to v3
(exported earlier the same day): v4 differs from v3 only in comments and
in the wording of log messages. v3 in turn superseded v2 (2026-09-15,
base `7fb102b`, tip tree `65a7b8bd…`), which predated the strict
cold-start completions, the post-eject fix, the digiKam dialog cleanup
and one-button loading.

## What is in here

| file | what |
|---|---|
| `wp3-gl126-submission.bundle` | `git bundle` of the series, `f8b5e16..wp3-gl126-submission-v4`. Preserves the exact commit ids. |
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
| 1 | `94727815e8e653bb5e0d4ca7782e7634f31d8b02` | `f1d6d51fdf7809def518fe46a409488a6af29c65` | genesys: fix ImagePipelineNodeExtract bytes-per-pixel for multi-channel rows |
| 2 | `d29d1bb9828a07ef220b735dd0de09263d4189ce` | `9c0df55c7d6d76c527238fd92261422e75481b5f` | genesys: add support for the GL126 ASIC |
| 3 | `ba843c0eedde54f45c8bc0463ba5f84d7dc127fb` | `d766ce69e99d80f4f65486f799b54e0c99289f7d` | genesys: add the Plustek OpticFilm 135i (07b3:1436) |
| 4 | `119019414f237ca035dfc347837f7ea4727f337c` | `eb6993ec95b38c3afcccd5f744e21e1f5399143c` | genesys: frame selection and magazine handling for the OpticFilm 135i |
| 5 | `f46b829263957610e3565d63c0197c05a2577504` | `16671d82bde8b107c921e8d789452dba442ee01f` | genesys: document the GL126 and the OpticFilm 135i |

The tip tree `16671d82…` is the identity of the package: any route below
that ends on that tree has recreated it exactly.

### What changed since v3 (same day, same base, tip tree `d891db5d…`)

Comments and log-message wording only; the compiled code is identical.
Every source comment that referred to the project's private working
documents, test-log numbers, work packages, review rounds, reviewers or
dates was rewritten to state the technical fact without the citation,
and one pointer to the public protocol documentation was added to
`gl126.h`'s header and to the generated tables' header. Debug and error
message strings lost the same citations and their double-hyphen dashes;
no option name, title, description, status value, mark keyword or device
string changed. Verified by stripping comments from every file of v3 and
v4 and comparing the remainder (identical for all 21 files touched;
string literals differ only in `gl126.cpp` and `gl126_ops.cpp`, in
messages), by the generator's `--check`, and by the full offline suite.
The commit messages are v3's.

### What changed between v2 (base `7fb102b`) and v3

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
git fetch /path/to/wp3-gl126-submission.bundle wp3-gl126-submission-v4
git checkout -b wp3-gl126-submission FETCH_HEAD
git rev-parse HEAD            # f46b829263957610e3565d63c0197c05a2577504
git rev-parse HEAD^{tree}     # 16671d82bde8b107c921e8d789452dba442ee01f
```

**Route B — the patches (same trees, new commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git checkout -b wp3-gl126-submission f8b5e16
git am /path/to/000[1-5]-*.patch
git rev-parse HEAD^{tree}     # 16671d82bde8b107c921e8d789452dba442ee01f
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

## Verification of this export (v4, 2026-09-28 evening)

Done in the v4 worktree and in two fresh clones in a scratch directory,
separate from the development clone:

| check | result |
|---|---|
| Standalone build in a worktree on `f8b5e16` (`autogen`, `configure --sysconfdir=/etc`, `lib`, `sanei`, `backend/libsane-genesys.la`, `-j8`) | exit 0, **0 compiler warnings** from any source file |
| exported symbols | **111** mentioning `gl126` — identical to the development build |
| `tests/test_sane_open_params.py` against that build | 7 passed |
| `tests/test_sane_calibration_cache.py` against that build | 6 passed |
| `tests/test_sane_magazine.py` against that build | 42 passed |
| `tools/gen_sane_tables.py --check` (tables == generator output) | up to date |
| Route B: `git am` of the five patches onto `f8b5e16` in a clean clone | tip tree `16671d82…` — **identical** |
| Route A: fetch from the bundle in a clean clone | tip `f46b829…`, tree `16671d82…` — **identical** |
| comment-stripped code of every file vs v3 | identical (21 files) |
| symlinks in the recreated tree | 0 (`git ls-files -s`, mode 120000) |

The full offline gate of this repository (`tools/release_check.py`)
reports FULL VERIFICATION, 389 tests, 0 skipped, against the development
build (which includes the same GL126 files); see `docs/offline-checks.md`.

`tstbackend -l 1 -r 1`, built from this tree and linked directly against
its `libsane-genesys.la`, against this build (Test 94): `warnings: 0  error: 0  checks: 22965`, exit 0; the backend saw 21 control transfers, all reads of reg 0x01, and no write. Not run:
`scanimage -T` and `tstbackend -l 2` and up (they cancel a scan mid-pass;
`docs/sane-submission-runbook.md`).

## Nothing leaves this repository

This package is built and exported locally. It is committed only to
`cgillinger/opticfilm135i-linux`. No branch is pushed from the
sane-backends clone, which tracks the real upstream.
