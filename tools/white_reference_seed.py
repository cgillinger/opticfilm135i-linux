#!/usr/bin/env python3
"""Seed the clean white-reference store (of135i.white_reference) from
exported calibration data (<stem>.cal.npz), so the first scan after an
install already fills a blocked white measurement from a reference rather
than by interpolation. Every scan keeps the store current by itself; this
only saves the first clean calibration. Host-only, no USB.

    tools/white_reference_seed.py <cal.npz> [...]   # the last clean one per kind wins
    tools/white_reference_seed.py --check <cal.npz> [...]   # report blockage, write nothing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import calibrate, white_reference  # noqa: E402


def subsets(z):
    v = z["verify_meas"]
    dpi = int(z["dpi"])
    if bool(z["dual"]):
        return [(white_reference.kind_key("dual-vis", dpi, v.shape[1]), v[1::2]),
                (white_reference.kind_key("dual-ir", dpi, v.shape[1]), v[0::2])]
    return [(white_reference.kind_key("plain", dpi, v.shape[1]), v)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    ap.add_argument("--dir", help="store directory (default: the driver's state directory)")
    args = ap.parse_args(argv)
    store = white_reference.WhiteReferenceStore(args.dir)
    for f in args.files:
        z = np.load(f)
        for key, meas in subsets(z):
            regions = calibrate.find_white_blockage(meas.astype(np.float64).mean(axis=0))
            if regions:
                print(f"{f}: {key}: BLOCKED {regions} -- not used")
            elif args.check:
                print(f"{f}: {key}: clean")
            elif store.save(key, meas.astype(np.float64).mean(axis=0)):
                print(f"{f}: {key}: saved to {store.dir}")
            else:
                print(f"{f}: {key}: NOT saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
