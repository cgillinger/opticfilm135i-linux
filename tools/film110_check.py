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
outside, and the transition FOOT = the outermost sample still less than
FOOT_FRAC of the way from the picture level to the surround level -- and
prints how far the padded product edge lies OUTSIDE that foot. A
negative margin means the product cuts picture: LOSS.

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
LOSS_TOLERANCE_MM = 0.03  # ~4 px at 3600 dpi: sampling, not picture


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
    pad = pad_mm * px_per_mm
    n_lines, n_cols = g.shape
    li = int(fr.line0 + 0.1 * (fr.line1 - fr.line0)), int(fr.line1 - 0.1 * (fr.line1 - fr.line0))
    ci = int(fr.col0 + 0.1 * (fr.col1 - fr.col0)), int(fr.col1 - 0.1 * (fr.col1 - fr.col0))
    edges = (("line0", fr.line0 - pad, -1), ("line1", fr.line1 + pad, +1),
             ("col0", fr.col0 - pad, -1), ("col1", fr.col1 + pad, +1))
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
        pic, scatter = float(np.median(inside)), float(np.std(inside))
        thresh = max(4.0 * scatter, 0.02)
        start = np.searchsorted(rel, -near)
        ramp0 = None
        for k in range(start, len(prof)):
            if abs(prof[k] - pic) > thresh:
                ramp0 = k
                break
        if ramp0 is None:
            # No transition within the span: the product edge lies more
            # than SPAN_MM inside the picture?? -- or the whole span is
            # picture-level, which for an edge is a LOSS of unknown size.
            yield name, product / px_per_mm, None, None, pic, None
            continue
        direction = 1.0 if prof[min(ramp0 + 1, len(prof) - 1)] >= prof[ramp0] else -1.0
        slope = np.diff(prof) * direction
        peak = 0.0
        k = ramp0
        while k + 1 < len(prof):
            peak = max(peak, slope[k])
            if peak > 0 and slope[k] < RAMP_STOP_FRAC * peak:
                break
            k += 1
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
                    verdict = "LOSS" if margin < -LOSS_TOLERANCE_MM else "ok"
                    print(f"    {e}: product {product:.2f} mm  foot {foot:.2f} mm  "
                          f"margin {margin:+.2f} mm  (picture {pic:.2f} -> {sur:.2f} density)  {verdict}")
                    worst = margin if worst is None else min(worst, margin)
                if worst is not None:
                    print(f"    edge-check: worst margin {worst:+.2f} mm -> "
                          f"{'LOSS' if worst < -LOSS_TOLERANCE_MM else 'no picture lost'}")
        del image
    return 0


if __name__ == "__main__":
    sys.exit(main())
