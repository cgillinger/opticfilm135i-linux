# Offline checks — the reproducible, hardware-free regression gate

Every check here runs without a scanner and without network access. It
is the reproducibility answer the project's updated course asks for: a
documented set of checks, their dependencies, their commands and their
expected results, that any commit can be held to. The substantiated
mechanism is the local commands below. A GitHub Actions workflow file is
also provided (`.github/workflows/offline-checks.yml`) but has not been
activated and no CI run has occurred -- see "The GitHub Actions workflow"
at the end.

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

Runs every offline suite and reports each as **PASS** (ran every test,
all passed), **PARTIAL** (ran, but skipped some of its tests), **SKIP**
(ran none of its tests -- a precondition such as a compiler or a built
backend is absent) or **FAIL**. It does **not** count a mandatory suite
as done just because its script exited cleanly: a skipped test is a test
that was not run, and a suite reporting zero tests run proved nothing
(that is a FAIL, never a pass). Verdict and exit code:

- **FULL VERIFICATION PASSED** (exit 0) only when every suite ran every
  one of its tests and all passed -- nothing skipped anywhere;
- **PARTIAL** (exit 1) when a mandatory suite skipped any test, entirely
  or in part; **FAILED** (exit 1) when any suite failed;
- **LIMITED RUN PASSED** (exit 0) under `--no-backend`, which excuses
  skips in the three backend suites only -- a skip in a core or compiler
  suite is still PARTIAL. `--allow-dirty` drops the clean-checkout
  requirement.

The parser and the verdict are pure functions pinned by
`tests/test_release_check.py` (mixed passed/skipped, entirely skipped,
zero tests, normal success, failure, and the `--no-backend` scope).

Substantiated: a full local run on 2026-09-15, against a built backend,
reported **FULL VERIFICATION PASSED, 338 tests, 0 skipped** (a later run the same day, after the parser fix and the cold-start change). Run it where the backend is
built (the development clone) for full verification, or with
`--no-backend` for the buildless subset.

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
         test_overscan test_dual_overscan test_trace_recorder; do
    .venv/bin/python tests/$t.py || exit 1
done
```

These cover the safety model, calibration maths, the park state machine,
the image path, the DPI and IR logic, the overscan/coverage geometry and
the bulk-digitisation workflow — the driver's own logic, all without
hardware.

`test_trace_recorder` uses fake USB devices and the upstream scan wire
digests, exercising recording on/off, private exclusive files, short and
failed transfers, recording failures and the converter. It needs no pytest.
The suite is also mandatory in `tools/release_check.py` and included in
the provided workflow.

For an explicitly requested hardware session, setting
`OF135I_TRACE=/new/path/run.jsonl` enables the diagnostic recorder; unset
or empty leaves it off. It observes transfers through `GuardedDevice`,
including interrupt reads, without adding commands, retries or sleeps.
Synchronous file writes do add host overhead, so recorded timing includes
that overhead and has not been hardware-validated. Files are created
exclusively as 0600; existing files and symlinks are refused. One warning
disables recording after an error while USB I/O continues; Ctrl-C while
recording disables it too and never replaces a failed transfer's error.
If Ctrl-C lands while a failed transfer is being recorded, it survives only
as a note on that USB exception: code that catches and retries the USB
error loses the Ctrl-C.
Bulk-IN and interrupt reads record the requested size (`want`) and mark
short reads `short:N`. One recorder is opened per `UsbIo`, so a process
that opens the device more than once records only the first session
(later opens warn); the recorder is not thread-safe.

Convert an existing recording entirely offline:

```
.venv/bin/python tools/trace_to_compiled.py run.jsonl -o run.trace.json.gz
```

The converter requires the recorder's `{"format": ...}` header and
validates every record. It ignores a damaged final line (interrupted
write); any other damaged or malformed line is reported on stderr and the
conversion is refused unless `--skip-damaged` is given. It skips
failed transfers, short OUT/control-IN transfers and interrupt records;
bulk-IN keeps the actual returned length plus `want`. Timestamps follow
`compile_trace.py` (cw/cr/bo at submission, bi at completion). The output
is written to a private temp file and linked into place (on filesystems
without hard links, such as exFAT or some FUSE/SMB mounts, it is copied to
an exclusively created 0600 file instead), so a failure leaves no partial
file; an existing output or symlink is refused.

Exit codes: 0 converted; 1 conversion refused or output not written
(damaged lines without `--skip-damaged`, existing output or symlink,
write error); 2 input unreadable or not an OF135I_TRACE recording
(missing or wrong `{"format": ...}` header), or a command-line usage error.

Only the first 64 bytes of control and bulk-OUT payloads are recorded;
bulk-IN image data is never recorded. A longer payload is never padded or
cut: its op gets `data`/`resp` `null`, the full `len` and
`"partial": true`. A record whose `data` is not strict hex or not exactly
`min(len, 64)` bytes counts as damaged.

Converter output is diagnostic only: transfers are dropped (failed, short
OUT/control-IN, interrupt reads, damaged lines), so every converted op
carries `"src": "of135i-trace/1"`. `tools/replay_trace.py` refuses,
before opening the device, any trace containing an op with `src`, a
partial op, or a control/bulk write whose data is not strict hex (pairs
of hex digits, no whitespace). Committed captures in `traces/` carry no
`src` key and still pass; loaders that read ops by key ignore it.

### 4. Compiler-based suites (need `g++`, no backend)

```
for t in test_sane_mark test_sane_ops test_sane_geometry test_sane_install; do
    .venv/bin/python tests/$t.py || exit 1
done
```

Each builds a small standalone probe (or, for `test_sane_install`, a
stand-in library) with `g++`/`cc` and exercises it: the magazine mark file and
its hardened file handling, the op programs' byte-for-byte
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

## The GitHub Actions workflow (provided, not yet activated)

A workflow file, `.github/workflows/offline-checks.yml`, is provided to
run checks 2-4 (the generator check, the twelve Python suites and the four
compiler-based suites) on push and pull request, installing only the four
Python packages, using the distribution's `g++`, building no backend and
touching no hardware.

It has **not been activated, and no CI run has occurred.** Publishing a
workflow file to this repository needs a token carrying the `workflow`
scope, which is the owner's to grant; until then the file sits ready and
the substantiated mechanism is the local commands above. **No green CI is
claimed.** Once enabled, the workflow would cover the driver's logic, the
generated tables and the backend's wire-level and lock behaviour, but
never an installed backend (check 5) or the scanner.
