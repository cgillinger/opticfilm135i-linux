#!/usr/bin/env python3
"""Run the 110 frame detector (of135i.film110) over saved aperture-
registered TIFFs and print a table.

    .venv/bin/python tools/film110_check.py --dpi 600 FILE...

Skips files ending in "-ir.tiff" (the IR channel, not what `detect`
takes) and files named "*.overscan.tiff" (the full overscan frame, not
aperture-registered -- `detect` assumes its input already went through
`aperture_crop.crop_to_aperture`). Reads each file with
`tifffile.imread`, one at a time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import film110
from of135i.holder import FILM_110


def _skip(path: Path) -> bool:
    name = path.name
    if name.endswith("-ir.tiff") or name.endswith("-ir.overscan.tiff"):
        return True
    if ".overscan." in name:
        return True
    return False


def _fmt(v, nd=1):
    return "-" if v is None else f"{v:.{nd}f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dpi", type=float, required=True)
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)

    import tifffile

    px_per_mm = args.dpi / 25.4

    for f in args.files:
        path = Path(f)
        if _skip(path):
            print(f"{path.name}: SKIPPED (not an aperture-registered visible frame)")
            continue
        image = tifffile.imread(str(path))
        find = film110.detect(image, dpi=args.dpi, film=FILM_110)

        print(f"\n=== {path.name} ({image.shape[1]}x{image.shape[0]}) ===")
        if find.empty:
            print(f"  empty ({find.reason})")
            del image
            continue

        band_mm = None
        if find.film_cols is not None:
            lo, hi = find.film_cols
            band_mm = (hi - lo) / px_per_mm
        print(f"  film band: {_fmt(band_mm)} mm  cols {find.film_cols}")
        print(f"  film_line_start: {_fmt(None if find.film_line_start is None else find.film_line_start / px_per_mm)} mm"
              f"  film_line_end: {_fmt(None if find.film_line_end is None else find.film_line_end / px_per_mm)} mm")
        print(f"  leading_continuation: {find.leading_continuation}")
        if find.reason:
            print(f"  reason: {find.reason}")

        if not find.frames:
            print("  (no frames predicted)")

        for fr in find.frames:
            print(f"  frame {fr.index}: {fr.orientation}"
                  f"  {'whole' if fr.whole else ('split-' + fr.split if fr.split else 'partial')}"
                  f"  lines {fr.line0 / px_per_mm:.2f}-{fr.line1 / px_per_mm:.2f} mm"
                  f"  cols {fr.col0 / px_per_mm:.2f}-{fr.col1 / px_per_mm:.2f} mm"
                  f"  size {fr.size_mm[0]:.2f}x{fr.size_mm[1]:.2f} mm"
                  f"  refined={fr.refined}"
                  f"  free_end_near={fr.free_end_near}"
                  f"  perforation@{fr.perforation_line / px_per_mm:.2f} mm")
        del image
    return 0


if __name__ == "__main__":
    sys.exit(main())
