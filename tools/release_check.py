#!/usr/bin/env python3
"""Reproducible offline gate for the of135i driver -- no USB, no scanner.

Runs the offline test suites and reports each as PASSED, PARTIAL (it ran,
but skipped some of its tests), SKIPPED (it ran none of its tests -- a
precondition such as a compiler or a built backend is absent) or FAILED.
A mandatory suite is NOT counted as done merely because its script exits
without error: a skipped test is a test that was not run, and a suite
that reports zero tests run proved nothing.

    .venv/bin/python tools/release_check.py [--allow-dirty] [--no-backend]

Suites fall into three groups:

  * core    -- pure Python (needs numpy/pillow/tifffile); always mandatory.
  * compiler-- build a standalone probe with g++; mandatory when a
               compiler is present, skipped (reported) otherwise.
  * backend -- drive the built SANE backend through its test mode; needs
               a built libsane-genesys.so (SANE_BACKENDS_DIR or a sibling
               sane-backends/). Mandatory by default; --no-backend makes
               skips in THIS group an acknowledged, limited run -- it
               excuses nothing in the other groups.

Exit code:
  0  FULL VERIFICATION PASSED -- every suite ran every one of its tests
     and all passed, nothing skipped anywhere; or LIMITED RUN PASSED --
     the same, except that backend suites skipped under --no-backend.
  1  any suite FAILED (including one that ran zero tests), uncommitted
     changes without --allow-dirty, or a mandatory suite SKIPPED or
     PARTIAL -- reported as PARTIAL, never as full verification.

The result parser (classify) and the verdict (verdict) are pure functions
so tests/test_release_check.py can pin them without running a suite.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (suite, group). Order is the run order.
SUITES = (
    ("test_safety", "core"), ("test_calibrate", "core"), ("test_hwblock", "core"),
    ("test_park", "core"), ("test_offline", "core"), ("test_diag", "core"),
    ("test_dpi", "core"), ("test_ir", "core"), ("test_image_probe", "core"),
    ("test_aperture_crop", "core"), ("test_overscan", "core"),
    ("test_dual_overscan", "core"), ("test_calibration_cache", "core"),
    ("test_film110", "core"),
    ("test_release_check", "core"),
    ("test_sane_lock", "compiler"), ("test_sane_ops", "compiler"),
    ("test_sane_geometry", "compiler"), ("test_sane_install", "compiler"),
    ("test_sane_open_params", "backend"), ("test_sane_calibration_cache", "backend"),
    ("test_sane_magazine", "backend"),
)

MISSING = {
    "core": "a Python dependency (numpy/pillow/tifffile)",
    "compiler": "a C/C++ compiler (g++)",
    "backend": "a built libsane-genesys.so (SANE_BACKENDS_DIR)",
}

PASS, PARTIAL, SKIP, FAIL = "PASS", "PARTIAL", "SKIP", "FAIL"


def classify(returncode: int, output: str) -> tuple[str, int, int]:
    """Classify one suite's run from its exit code and output.

    Returns (status, passed, skipped):

      PASS     -- exit 0, "N tests passed" with N > 0 and nothing skipped;
      PARTIAL  -- exit 0, N > 0 passed but some tests skipped: the suite is
                  not fully verified;
      SKIP     -- exit 0, zero passed and some skipped: nothing ran (a
                  precondition is absent);
      FAIL     -- non-zero exit; or no recognisable "N tests passed" line;
                  or a line reporting zero tests and zero skips (an empty
                  run proves nothing and is never a pass).
    """
    m = re.search(r"(\d+) tests passed", output)
    s = re.search(r"(\d+) skipped", output)
    passed = int(m.group(1)) if m else 0
    skipped = int(s.group(1)) if s else 0
    if returncode != 0:
        return FAIL, passed, skipped
    if m is None:
        # Clean exit but no recognisable result line -- a failure, never
        # a silent pass.
        return FAIL, passed, skipped
    if passed == 0 and skipped == 0:
        return FAIL, passed, skipped
    if passed == 0:
        return SKIP, passed, skipped
    if skipped > 0:
        return PARTIAL, passed, skipped
    return PASS, passed, skipped


def run_suite(name: str) -> tuple[str, int, int, str]:
    """Run tests/<name>.py and return (status, passed, skipped, output)."""
    proc = subprocess.run([sys.executable, str(REPO / "tests" / f"{name}.py")],
                          capture_output=True, text=True, cwd=REPO)
    out = proc.stdout + proc.stderr
    status, passed, skipped = classify(proc.returncode, out)
    return status, passed, skipped, out


def verdict(results, *, no_backend: bool, dirty: bool,
            allow_dirty: bool) -> tuple[int, list[str]]:
    """The overall result from per-suite (name, group, status, passed,
    skipped) tuples. Returns (exit code, message lines).

    FULL VERIFICATION PASSED requires every suite to be PASS: every
    mandatory test actually ran and passed, nothing skipped. --no-backend
    excuses SKIP/PARTIAL in the backend group only, and then the best
    outcome is LIMITED RUN PASSED.
    """
    lines: list[str] = []
    failed = [r for r in results if r[2] == FAIL]
    incomplete = [r for r in results if r[2] in (SKIP, PARTIAL)]
    excused = [r for r in incomplete if r[1] == "backend" and no_backend]
    unexcused = [r for r in incomplete if r not in excused]

    def describe(r):
        name, group, status, passed, skipped = r
        if status == SKIP:
            return f"{name} skipped entirely ({skipped} skipped) -- needs {MISSING[group]}"
        return f"{name} ran {passed} and skipped {skipped}"

    if failed:
        lines.append(f"release check FAILED: {len(failed)} suite(s) failed: "
                     + ", ".join(r[0] for r in failed))
        if unexcused:
            lines.append("Also incomplete: " + "; ".join(describe(r) for r in unexcused))
        return 1, lines

    if unexcused:
        for r in unexcused:
            lines.append(f"PARTIAL ({r[1]}): {describe(r)}")
        lines.append("Not full verification: a skipped test is a test that was not "
                     "run. Install the missing precondition, or state the run as "
                     "limited (--no-backend excuses the backend group only).")
        if dirty and not allow_dirty:
            lines.append("Also: uncommitted changes in the checkout.")
        return 1, lines

    if dirty and not allow_dirty:
        lines.append("release check FAILED: uncommitted changes in the checkout")
        return 1, lines

    total_pass = sum(r[3] for r in results)
    if excused:
        lines.append(f"LIMITED RUN PASSED: {total_pass} tests ran and passed; the "
                     f"backend group was excused by --no-backend ("
                     + "; ".join(describe(r) for r in excused)
                     + "). This is NOT full verification -- run without "
                     "--no-backend against a built backend for that.")
        return 0, lines

    lines.append(f"FULL VERIFICATION PASSED: every suite ran every test and passed "
                 f"({total_pass} tests, 0 skipped).")
    return 0, lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--allow-dirty", action="store_true",
                    help="do not fail on uncommitted changes")
    ap.add_argument("--no-backend", action="store_true",
                    help="acknowledge an explicitly limited run: the backend "
                         "suites may skip when no built backend is present")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(REPO))
    from of135i import __version__
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                         capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                capture_output=True, text=True).stdout.strip())
    print(f"of135i {__version__} @ {rev or 'unknown'}"
          f"{' (DIRTY checkout)' if dirty else ''}"
          f"{'  [--no-backend: limited run]' if args.no_backend else ''}")

    results = []
    total_pass = total_skip = 0
    for name, group in SUITES:
        status, passed, skipped, out = run_suite(name)
        results.append((name, group, status, passed, skipped))
        total_pass += passed
        total_skip += skipped
        if status == SKIP:
            note = f"{skipped} skipped, 0 ran -- needs {MISSING[group]}"
        elif status == FAIL and passed == 0 and skipped == 0:
            note = "ran no tests"
        else:
            note = f"{passed:3d} passed" + (f", {skipped} skipped" if skipped else "")
        print(f"  {name:28s} {status:7s} {note}")
        if status == FAIL:
            print(out[-2000:])

    print(f"  {'total passed':28s}         {total_pass}")
    print(f"  {'total skipped':28s}         {total_skip}")

    code, lines = verdict(results, no_backend=args.no_backend, dirty=dirty,
                          allow_dirty=args.allow_dirty)
    print()
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
