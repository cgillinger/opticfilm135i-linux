#!/usr/bin/env python3
"""Reproducible offline gate for the of135i driver -- no USB, no scanner.

Runs the offline test suites and reports each as PASSED, FAILED, or
SKIPPED (a precondition -- a compiler or a built backend -- is absent).
A mandatory suite is NOT counted as done merely because its script exits
without error: a suite that prints "0 tests passed, N skipped" ran none
of its tests and is reported as skipped, not passed.

    .venv/bin/python tools/release_check.py [--allow-dirty] [--no-backend]

Suites fall into three groups:

  * core    -- pure Python (needs numpy/pillow/tifffile); always mandatory.
  * compiler-- build a standalone probe with g++; mandatory when a
               compiler is present, skipped (reported) otherwise.
  * backend -- drive the built SANE backend through its test mode; needs
               a built libsane-genesys.so (SANE_BACKENDS_DIR or a sibling
               sane-backends/). Mandatory by default; --no-backend makes
               their absence an acknowledged, limited run.

Exit code:
  0  full verification, or a --no-backend limited run, with every
     mandatory suite PASSED;
  1  any suite FAILED, uncommitted changes without --allow-dirty, or a
     mandatory suite SKIPPED (its precondition is missing) -- in which
     case the run is reported as PARTIAL, never as full verification.
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
    ("test_dual_overscan", "core"),
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


def run_suite(name: str) -> tuple[str, int, int, str]:
    """Return (status, passed, skipped, output).

    status is "PASS" (ran tests, all passed), "FAIL" (non-zero exit, or no
    "N tests passed" line at all), or "SKIP" (exited cleanly but ran no
    tests -- "0 tests passed" with some skipped, i.e. a precondition is
    absent)."""
    proc = subprocess.run([sys.executable, str(REPO / "tests" / f"{name}.py")],
                          capture_output=True, text=True, cwd=REPO)
    out = proc.stdout + proc.stderr
    m = re.search(r"(\d+) tests passed", out)
    s = re.search(r"(\d+) skipped", out)
    passed = int(m.group(1)) if m else 0
    skipped = int(s.group(1)) if s else 0
    if proc.returncode != 0:
        return "FAIL", passed, skipped, out
    if m is None:
        # Clean exit but no recognizable result line -- treat as a failure,
        # never as a silent pass.
        return "FAIL", passed, skipped, out
    if passed == 0 and skipped > 0:
        return "SKIP", passed, skipped, out
    return "PASS", passed, skipped, out


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

    total_pass = 0
    n_fail = 0
    skipped_by_group: dict[str, list[str]] = {}
    for name, group in SUITES:
        status, passed, skipped, out = run_suite(name)
        total_pass += passed
        note = ""
        if status == "PASS":
            note = f"{passed:3d} passed" + (f", {skipped} skipped" if skipped else "")
        elif status == "SKIP":
            note = f"skipped -- needs {MISSING[group]}"
            skipped_by_group.setdefault(group, []).append(name)
        else:  # FAIL
            n_fail += 1
        print(f"  {name:28s} {status:4s} {note}")
        if status == "FAIL":
            print(out[-2000:])

    print(f"  {'total passed':28s}      {total_pass}")

    # Verdict. Distinguish PASSED / FAILED / SKIPPED explicitly.
    if n_fail:
        print(f"\nrelease check FAILED: {n_fail} suite(s) failed")
        return 1

    # Which skipped groups are acceptable for this run?
    unacceptable = {g: names for g, names in skipped_by_group.items()
                    if not (g == "backend" and args.no_backend)}
    if unacceptable:
        for g, names in unacceptable.items():
            print(f"\nPARTIAL: {len(names)} {g} suite(s) skipped -- {MISSING[g]} "
                  f"is missing: {', '.join(names)}")
        print("Not full verification. Install the missing precondition, or "
              "state the run as limited (see --no-backend for the backend "
              "suites); mandatory checks skipped for a missing precondition "
              "do not count as passed.")
        if dirty and not args.allow_dirty:
            print("Also: uncommitted changes in the checkout.")
        return 1

    if dirty and not args.allow_dirty:
        print("\nrelease check FAILED: uncommitted changes in the checkout")
        return 1

    if skipped_by_group.get("backend") and args.no_backend:
        print("\nLIMITED RUN PASSED: every core and compiler suite passed; the "
              "backend suites were skipped by --no-backend (no built backend). "
              "This is NOT full verification -- run without --no-backend against "
              "a built backend for that.")
        return 0

    print("\nFULL VERIFICATION PASSED: every mandatory suite ran and passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
