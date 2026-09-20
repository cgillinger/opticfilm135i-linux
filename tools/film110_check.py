#!/usr/bin/env python3
"""Run the 110 frame detector (of135i.film110) over saved aperture-
registered TIFFs and print a table.

    .venv/bin/python tools/film110_check.py --dpi 600 FILE...
    .venv/bin/python tools/film110_check.py --dpi 3600 --edge-check FILE...

--edge-check is the acceptance instrument for "no visible picture is
lost" (Astra's review, 2026-09-20): for every WHOLE image it measures
each of the four edges INDEPENDENTLY of the detector -- the mean density
profile across the edge over the middle 80 % of the perpendicular extent,
the picture level 0.3-1.0 mm inside and the surround level 0.3-1.0 mm
outside, and the transition FOOT = the start of the first flat plateau
beyond the picture (at another level, or reached through a step) -- and
prints how far the padded product edge lies OUTSIDE that foot. A
negative margin means the product cuts picture: LOSS. A flat sky inside
the picture can give a false LOSS; that errs towards a human look.

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


RAMP_STOP_FRAC = 0.10    # the foot: where the outward slope falls below this fraction of the ramp's peak slope
LEVEL_NEAR_MM, LEVEL_FAR_MM = 0.3, 1.0
SPAN_MM = 1.5            # profile extent either side of the product edge
SMOOTH_MM = 0.05
PLATEAU_MIN_MM = 0.15     # a flat run at least this long is a plateau ...
PLATEAU_SLOPE = 0.0025    # ... when every per-pixel density step is below this (3600 dpi: 0.035/0.1 mm)
PLATEAU_MIN_STEP = 0.04   # ... and it sits at least this far from the picture level
LOSS_TOLERANCE_MM = 0.03  # ~4 px at 3600 dpi: sampling, not picture
FILM_EDGE_TOLERANCE_MM = 0.1  # at the film's cut edge (air beyond): the blend into the gap


def _density(g):
    import numpy as np
    return -np.log10(np.maximum(g.astype(np.float32), 1.0) / 65535.0)


def edge_check(image, fr, px_per_mm, pad_mm):
    """Yield (edge, product_edge_mm, foot_mm, margin_mm, picture_level,
    ramp_level) for the four edges of one whole FrameFind.

    Independent of the detector: the mean DENSITY profile across the
    edge over the middle 80 % of the perpendicular extent, smoothed over
    SMOOTH_MM. The picture level is the median LEVEL_NEAR_MM..LEVEL_FAR_MM
    inside the product edge. Walking outward from LEVEL_NEAR_MM inside
    the product edge, the ramp starts where the profile leaves the
    picture level by more than 4 x its own scatter, and the FOOT is where
    the outward slope has dropped below RAMP_STOP_FRAC of the ramp's peak
    slope -- the first plateau reached (on 110 film the dark printed
    border; the rail beyond it is a second, later step and must not be
    mistaken for the surround). margin = how far the product edge lies
    outside that foot; negative = the product cuts picture.
    """
    import numpy as np
    g = image[:, :, 1]
    n_lines, n_cols = g.shape
    li = int(fr.line0 + 0.1 * (fr.line1 - fr.line0)), int(fr.line1 - 0.1 * (fr.line1 - fr.line0))
    ci = int(fr.col0 + 0.1 * (fr.col1 - fr.col0)), int(fr.col1 - 0.1 * (fr.col1 - fr.col0))
    # The product edges exactly as `film110.crop` writes them (pad, film
    # band clamp, array bounds).
    p_l0, p_l1, p_c0, p_c1 = film110.product_bounds(
        fr, dpi=px_per_mm * 25.4, pad_mm=pad_mm, shape=image.shape)
    edges = (("line0", float(p_l0), -1), ("line1", float(p_l1), +1),
             ("col0", float(p_c0), -1), ("col1", float(p_c1), +1))
    box = max(1, int(round(SMOOTH_MM * px_per_mm)))
    for name, product, outward in edges:
        span = int(SPAN_MM * px_per_mm)
        axis_len = n_lines if name[0] == "l" else n_cols
        lo, hi = int(max(product - span, 0)), int(min(product + span, axis_len))
        if hi - lo < 4:
            continue
        if name[0] == "l":
            prof = _density(g[lo:hi, ci[0]:ci[1]]).mean(axis=1)
        else:
            prof = _density(g[li[0]:li[1], lo:hi]).mean(axis=0)
        if box > 1:
            prof = np.convolve(prof, np.ones(box) / box, mode="same")
        pos = np.arange(lo, hi) + 0.5
        # Orient so that index increases OUTWARD.
        if outward < 0:
            prof, pos = prof[::-1], pos[::-1]
        near, far = LEVEL_NEAR_MM * px_per_mm, LEVEL_FAR_MM * px_per_mm
        rel = (pos - product) * outward           # mm-free: px outward of the product edge
        inside = prof[(rel <= -near) & (rel >= -far)]
        if inside.size < 2:
            continue
        pic = float(np.median(inside))
        # The foot: walking outward from LEVEL_NEAR_MM inside the product
        # edge, the start of the first FLAT run (>= PLATEAU_MIN_MM long,
        # |slope| < PLATEAU_SLOPE per px) whose level differs from the
        # picture level by more than PLATEAU_MIN_STEP density -- the first
        # plateau beyond the picture (a border, clear film, the light gap
        # or air). A flat sky inside the picture can trip this and give a
        # false LOSS; that errs on the side of a human look, never of a
        # missed cut. (The first version used a scatter threshold that the
        # ramp itself inflated, and walked through the border to the rail.)
        start = int(np.searchsorted(rel, -near))
        slope = np.abs(np.diff(prof))
        run_px = max(2, int(round(PLATEAU_MIN_MM * px_per_mm)))
        foot_k = None
        k = start
        back = max(1, int(round(0.15 * px_per_mm)))
        while k + run_px < len(prof):
            if (slope[k:k + run_px] < PLATEAU_SLOPE).all():
                # A plateau: at a level other than the picture's, OR
                # reached through a step from what lies 0.15 mm inside
                # it (a border that happens to share the picture's level
                # at this edge, 2026-09-20 image 4's perforated side).
                inner = prof[max(0, k - back)]
                if abs(prof[k] - pic) > PLATEAU_MIN_STEP or abs(prof[k] - inner) > PLATEAU_MIN_STEP:
                    foot_k = k
                    break
            k += 1
        if foot_k is None:
            yield name, product / px_per_mm, None, None, pic, None
            continue
        k = foot_k
        foot = pos[k]
        margin = (product - foot) * outward / px_per_mm
        yield name, product / px_per_mm, foot / px_per_mm, margin, pic, float(prof[k])
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dpi", type=float, required=True)
    ap.add_argument("--edge-check", action="store_true",
                    help="independent edge measurement of every whole image "
                         "against the padded product crop (see module doc)")
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
            if args.edge_check and fr.whole:
                worst = None
                for (e, product, foot, margin, pic, sur) in edge_check(
                        image, fr, px_per_mm, film110.PRODUCT_PAD_MM):
                    if foot is None:
                        print(f"    {e}: product {product:.2f} mm, no transition within "
                              f"{SPAN_MM} mm (picture level {pic:.2f} density) -- LOSS?")
                        worst = -SPAN_MM if worst is None else min(worst, -SPAN_MM)
                        continue
                    # At the film's own cut edge (air beyond it, or the
                    # light gap before the rail) the ramp's foot lies in
                    # the blend past the last film column; the film ends
                    # there, so up to FILM_EDGE_TOLERANCE_MM is the edge
                    # itself, not picture.
                    at_film_edge = sur < 0.1
                    tol = FILM_EDGE_TOLERANCE_MM if at_film_edge else LOSS_TOLERANCE_MM
                    verdict = "LOSS" if margin < -tol else "ok"
                    note = "  [film edge, air beyond]" if at_film_edge else ""
                    print(f"    {e}: product {product:.2f} mm  foot {foot:.2f} mm  "
                          f"margin {margin:+.2f} mm  (picture {pic:.2f} -> {sur:.2f} density)  {verdict}{note}")
                    if at_film_edge and verdict == "ok":
                        margin = max(margin, 0.0)   # the blend into the gap, not picture
                    worst = margin if worst is None else min(worst, margin)
                if worst is not None:
                    print(f"    edge-check: worst margin {worst:+.2f} mm -> "
                          f"{'LOSS' if worst < -LOSS_TOLERANCE_MM else 'no picture lost'}")
        del image
    return 0


if __name__ == "__main__":
    sys.exit(main())
