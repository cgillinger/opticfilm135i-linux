#!/usr/bin/env python3
"""Reproducible release check for the of135i driver -- offline, no USB.

Runs every offline test file in tests/, requires a clean git checkout,
and prints the version, the git revision and the per-file test counts.
Exit 0 only if everything passed and the checkout is clean (or --allow-dirty).

    .venv/bin/python tools/release_check.py [--allow-dirty]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITES = ("test_safety", "test_calibrate", "test_hwblock", "test_park",
          "test_offline", "test_diag", "test_dpi", "test_ir")


def run_suite(name: str) -> tuple[bool, int, str]:
    proc = subprocess.run([sys.executable, str(REPO / "tests" / f"{name}.py")],
                          capture_output=True, text=True, cwd=REPO)
    out = proc.stdout + proc.stderr
    m = re.search(r"(\d+) tests passed", out)
    return proc.returncode == 0 and m is not None, int(m.group(1)) if m else 0, out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--allow-dirty", action="store_true", help="do not fail on uncommitted changes")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(REPO))
    from of135i import __version__
    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True).stdout.strip())
    print(f"of135i {__version__} @ {rev or 'unknown'}{' (DIRTY checkout)' if dirty else ''}")
    total, ok_all = 0, True
    for name in SUITES:
        ok, n, out = run_suite(name)
        total += n
        ok_all &= ok
        print(f"  {name:15s} {n:3d} {'OK' if ok else 'FAILED'}")
        if not ok:
            print(out[-2000:])
    print(f"  total          {total:3d} {'OK' if ok_all else 'FAILED'}")
    if dirty and not args.allow_dirty:
        print("release check FAILED: uncommitted changes in the checkout")
        return 1
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
