# WP-3 package — the exact SANE submission series, exported

**Status: prepared for review. Nothing has been sent.** No merge request,
issue, mail or contact with the SANE project or anyone else, and nothing
here initiates one. This directory holds the five-commit series in two
forms that recreate it without manual reconstruction.

**Revision v5, exported 2026-09-29.** The series is exactly the
repository's `sane/` (the nine `gl126_*` files and
`gl126-integration.patch`). v5 is v4 (2026-09-28 evening) with 42 lines
of dead code removed from `gl126.cpp`, which the upstream CI's clang job
rejected; its compiled code is identical to v4's, and v4's to v3's (v4
differed from v3 only in comments and in the wording of log messages).
v3 in turn superseded v2 (2026-09-15,
base `7fb102b`, tip tree `65a7b8bd…`), which predated the strict
cold-start completions, the post-eject fix, the digiKam dialog cleanup
and one-button loading.

## What is in here

| file | what |
|---|---|
| `wp3-gl126-submission.bundle` | `git bundle` of the series, `f8b5e16..wp3-gl126-submission-v5`. Preserves the exact commit ids. |
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
| 2 | `2520d125fbe48970d403f801f793480252b5fca5` | `d7982113c1c7c3f30db754a3300687fc252d42ab` | genesys: add support for the GL126 ASIC |
| 3 | `0d86c19fcd390b924dba7951e8287b77435e1fd6` | `1f334c7eea835c4a2f685cb68082e2c923956b96` | genesys: add the Plustek OpticFilm 135i (07b3:1436) |
| 4 | `7d93d623ba46bab5ac817041f3ad5ba2729f8d84` | `cf5aa74c70996ef88945f7238d3ac9a0a23a1b4a` | genesys: frame selection and magazine handling for the OpticFilm 135i |
| 5 | `c026a333a6ff8b85a789e919f07047ac7140f3f9` | `b166a3daf8fd4da719cf52657e3c4e78e73ab002` | genesys: document the GL126 and the OpticFilm 135i |

The tip tree `b166a3da…` is the identity of the package: any route below
that ends on that tree has recreated it exactly.

### What changed since v4 (same base, tip tree `16671d82…`)

Dead code removed from `gl126.cpp`, 42 lines, all in commit 2; nothing
else differs (`git diff` v4..v5 is one file, deletions only), and the
commit messages are v4's.

- `write_phase()`, a helper no code called. It carried
  `[[maybe_unused]]`, a C++17 attribute; the backend builds as C++11, and
  the upstream CI's `fedora-39-clang` job (clang 17, `-Werror`) rejects
  it. GCC accepts it silently, which is why every earlier build here was
  clean.
- `kFrameLinesPlain3600` and `kColourShiftLinesPlain3600`, two constants
  nothing read (`-Wunused-const-variable`, clang only). Every profile's
  figures come from `frame_geometry()`.

Found by running the upstream pipeline on the v4 branch in a fork before
anything was submitted. The compiled code is unchanged: the disassembly
of `gl126.o` built from v4 and from v5 is identical, and so are the
exported symbols. No hardware result and no `tstbackend` result is
affected.

### What changed between v3 and v4 (same day, same base, v3 tip tree `d891db5d…`)

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

The GL126 code here compiles to the same machine code as the
repository's `sane/` at `19b7605` (the Test 92 commit), which is what
was installed and run for Tests 90–92 (the source has since lost
citations in comments and 42 lines of dead code); every earlier hardware result (`docs/test-log.md` Tests
62–92) was produced on this code or on a strict subset of it whose
behaviour did not change. The rebase touched none of the GL126 files.
Nothing in this revision needs re-verification on hardware.

## Recreate it

Either route needs a clone of sane-backends containing the base commit.

**Route A — the bundle (exact commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git fetch /path/to/wp3-gl126-submission.bundle wp3-gl126-submission-v5
git checkout -b wp3-gl126-submission FETCH_HEAD
git rev-parse HEAD            # c026a333a6ff8b85a789e919f07047ac7140f3f9
git rev-parse HEAD^{tree}     # b166a3daf8fd4da719cf52657e3c4e78e73ab002
```

**Route B — the patches (same trees, new commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git checkout -b wp3-gl126-submission f8b5e16
git am /path/to/000[1-5]-*.patch
git rev-parse HEAD^{tree}     # b166a3daf8fd4da719cf52657e3c4e78e73ab002
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

## Verification of this export (v5, 2026-09-29)

Done in the v5 worktree, in two fresh repositories separate from the
development clone, and in the upstream CI's own container images:

| check | result |
|---|---|
| Standalone build in a worktree on `f8b5e16` (`autogen`, `configure --sysconfdir=/etc`, `lib`, `sanei`, `backend/libsane-genesys.la`, `-j8`) | exit 0, **0 compiler warnings** from any source file |
| exported symbols | **111** mentioning `gl126` — identical to the development build |
| `tests/test_sane_open_params.py` against that build | 7 passed |
| `tests/test_sane_calibration_cache.py` against that build | 6 passed |
| `tests/test_sane_magazine.py` against that build | 42 passed |
| `tools/gen_sane_tables.py --check` (tables == generator output) | up to date |
| Route B: `git am` of the five patches onto `f8b5e16` in a clean clone | tip tree `b166a3da…` — **identical** |
| Route A: fetch from the bundle in a clean clone | tip `c026a33…`, tree `b166a3da…` — **identical** |
| `git diff` v4..v5 | one file, `gl126.cpp`, 42 deletions, 0 additions |
| disassembly of `gl126.o`, v4 build vs v5 build | identical |
| upstream CI `make-dist` step (style check, `autogen`, `configure`, `make dist`) in `ci-envs:debian-bullseye-mini`, run locally | exit 0 |
| upstream CI `fedora-39-clang` job in its image (clang 17.0.1, `-Werror`), run locally from that tarball | build exit 0, **0 errors, 0 warnings**; `make check` exit 0, every test PASS (incl. `genesys_unit_tests`) |
| `git am` of the five patches onto upstream `ccabaad` (master as of 2026-09-29) | applies clean |
| symlinks in the recreated tree | 0 (`git ls-files -s`, mode 120000) |

The full offline gate of this repository (`tools/release_check.py`)
reports FULL VERIFICATION, 389 tests, 0 skipped, against the development
build (which includes the same GL126 files); see `docs/offline-checks.md`.

`tstbackend -l 1 -r 1`, built from the v4 tree and linked directly against
its `libsane-genesys.la` (Test 94; not repeated for v5, whose compiled
code is identical): `warnings: 0  error: 0  checks: 22965`, exit 0; the backend saw 21 control transfers, all reads of reg 0x01, and no write. Not run:
`scanimage -T` and `tstbackend -l 2` and up (they cancel a scan mid-pass;
`docs/sane-submission-runbook.md`).

## Nothing leaves this repository

This package is built and exported locally. It is committed only to
`cgillinger/opticfilm135i-linux`. No branch is pushed from the
sane-backends clone, which tracks the real upstream.
