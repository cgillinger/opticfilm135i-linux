# Offline checks — the reproducible, hardware-free regression gate

Every check here runs without a scanner and without network access. It
is the reproducibility answer the project's updated course asks for: a
documented set of checks, their dependencies, their commands and their
expected results, that any commit can be held to. A small CI job
(`.github/workflows/offline-checks.yml`) runs the subset that needs no
built SANE backend; the rest is a local step.

Nothing here talks to USB. The suites that would drive hardware are the
scanning tests, and those are not offline — they live in
`docs/test-log.md` as hardware runs.

## Dependencies

| For | Needs |
|---|---|
| the Python suites | Python 3.10+, and `numpy`, `pillow`, `tifffile`, `pyusb` (the repo venv has them: `.venv/`) |
| the generator check | Python only |
| the compiler-based suites | a C/C++ compiler (`g++`) on `PATH`; they print `SKIPPED` and pass trivially without one |
| the three backend suites | additionally a built `libsane-genesys.so` and `SANE_BACKENDS_DIR` (see below) — **not** in CI |

## The checks

### 1. The full local gate

```
.venv/bin/python tools/release_check.py
```

Runs every offline suite, requires a clean checkout (`--allow-dirty` to
skip that), and prints the version, git revision and per-file counts.
Exit 0 only if all pass. This is the one command to run before a release
or a submission refresh. It expects the three backend suites' build to be
present; run it where the backend has been built (the development clone),
or read the CI subset below as the buildless equivalent.

### 2. Generated-table consistency

```
.venv/bin/python tools/gen_sane_tables.py --check
```

Regenerates `sane/gl126_tables.{cpp,h}` into memory and fails if the
checked-in files differ. Guards against a hand-edit of the generated
tables, and against the generator and the committed output drifting
apart. Expected: `generated SANE tables are up to date`.

### 3. Python offline suites (no compiler, no backend)

Each is a standalone script that prints `N tests passed` and exits 0:

```
for t in test_safety test_calibrate test_hwblock test_park test_offline \
         test_diag test_dpi test_ir test_image_probe test_aperture_crop \
         test_overscan test_dual_overscan; do
    .venv/bin/python tests/$t.py || exit 1
done
```

These cover the safety model, calibration maths, the park state machine,
the image path, the DPI and IR logic, the overscan/coverage geometry and
the bulk-digitisation workflow — the driver's own logic, all without
hardware.

### 4. Compiler-based suites (need `g++`, no backend)

```
for t in test_sane_lock test_sane_ops test_sane_geometry test_sane_install; do
    .venv/bin/python tests/$t.py || exit 1
done
```

Each builds a small standalone probe (or, for `test_sane_install`, a
stand-in library) with `g++`/`cc` and exercises it: the process lock and
the hardened lock-file handling, the op programs' byte-for-byte
wire-equality against the Python driver, the geometry ledger, and the
installer's staging behaviour. Without a compiler they self-skip.

### 5. Backend suites (need a built backend — local only, not CI)

```
SANE_BACKENDS_DIR=/path/to/sane-backends \
  .venv/bin/python tests/test_sane_open_params.py
SANE_BACKENDS_DIR=/path/to/sane-backends \
  .venv/bin/python tests/test_sane_calibration_cache.py
SANE_BACKENDS_DIR=/path/to/sane-backends \
  .venv/bin/python tests/test_sane_magazine.py
```

These drive the real `sane_open → sane_control_option → sane_start` flow
through genesys's own testing mode (which cannot reach USB), so they need
`libsane-genesys.so` built from a sane-backends checkout carrying the
GL126 changes. They **auto-skip** when that build is absent, printing the
reason. They are deliberately outside CI: building sane-backends there
would add an autotools toolchain and a clone of the upstream tree for
three suites whose logic the op and geometry suites already cover on the
wire. Run them in the development clone, or against a recreated WP-3
package build (`sane/wp3-package/README.md` shows how).

## What CI runs

`.github/workflows/offline-checks.yml` runs checks 2, 3 and 4 on push and
pull request: the generator check, the twelve Python suites and the four
compiler-based suites. It installs only the four Python packages and uses
the distribution's `g++`. It never builds the full backend and never
touches hardware. A green run means the driver's logic, the generated
tables and the backend's wire-level and lock behaviour are unregressed;
it does not exercise an installed backend (check 5) or the scanner.
