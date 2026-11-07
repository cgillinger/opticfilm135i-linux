#!/usr/bin/env python3
"""Offline tests for the mounted-slide geometry (library only, no
hardware, no caller yet).

The slide geometry (holder.SLIDE_FIDUCIAL / slide_dual_geometry) is the
strip path's own dual geometry with a different fiducial model, so these
pin (a) the documented arithmetic, (b) the per-profile FEEDL grid, (c) the
refusals, including the stricter slide-only travel check, and (d) the
per-unit position offset (issue #61, FiducialModel.shifted), which must
move every slide window and be checked against the travel ceiling.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_slides.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import holder, image, safety
from of135i.device import dual_tables

DPIS = (600, 1200, 2400, 3600, 7200)
MM = holder.MM_PER_UNIT


def _slide_geom(slide, dpi, overscan=holder.SLIDE_OVERSCAN_MM, offset_mm=0.0):
    t = dual_tables(dpi)
    return holder.slide_dual_geometry(
        slide, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
        colour_crop_lines=image.align_shift(dpi),
        default_wire_lines=t.DEFAULT_LINES, overscan_mm=overscan,
        offset_mm=offset_mm)


def _strip_geom(frame, dpi, overscan=holder.OVERSCAN_MM):
    t = dual_tables(dpi)
    return holder.dual_overscan_geometry(
        frame, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
        colour_crop_lines=image.align_shift(dpi),
        default_wire_lines=t.DEFAULT_LINES, overscan_mm=overscan)


def _raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}")


def test_slide_model_matches_documented_numbers():
    """The 600-dual anchor K is the table module's; strip frame 1 maps to
    Test 84's FEEDL 6519; the slide openings land on 23.5 + (n-1) x 62.6
    mm in Test 84's coordinate; the shift from strip frame 1 is -0.248."""
    t600 = dual_tables(600)
    k = t600.DEFAULT_LINES * ((7200 // 600) / 2) / 2
    assert holder.DUAL600_START_ANCHOR_HWDPI == k == 5292, k
    g, _ = _strip_geom(1, 600)
    assert g.feedl == 6519, g.feedl          # Test 84's frame-1 ledger value
    assert abs(holder.SLIDE_SHIFT_MM - (-0.248)) < 0.001, holder.SLIDE_SHIFT_MM
    for n, start in enumerate((23.5, 86.1, 148.7, 211.3), start=1):
        lead = holder.SLIDE_FIDUCIAL.leading_hwdpi(n)
        got = (lead + holder.DUAL600_START_ANCHOR_HWDPI) * MM
        assert abs(got - start) < 0.01, (n, got, start)
        trail = holder.SLIDE_FIDUCIAL.trailing_hwdpi(n)
        assert abs((trail - lead) * MM - holder.SLIDE_OPENING_MM) < 1e-9
    # Test 84's measured openings 2-4 (86.12 / 148.68 / 211.35): within
    # 0.06 mm of the model.
    for n, meas in ((2, 86.12), (3, 148.68), (4, 211.35)):
        got = (holder.SLIDE_FIDUCIAL.leading_hwdpi(n)
               + holder.DUAL600_START_ANCHOR_HWDPI) * MM
        assert abs(got - meas) < 0.06, (n, got, meas)
    assert holder.SLIDE.frames == 4 and holder.SLIDE_OVERSCAN_MM == 1.5
    print("test_slide_model_matches_documented_numbers OK")


def test_slide_feedl_grid_every_profile():
    """FEEDL(N) = FEEDL(1) + (N-1) x 17744.88 (+-1 rounding) on every
    profile; the window covers the 38.05 mm opening plus the overscan on
    both sides; registers and parity as for strips."""
    for dpi in DPIS:
        t = dual_tables(dpi)
        over = holder.SLIDE_OVERSCAN_MM
        g1, _ = _slide_geom(1, dpi, over)
        for n in range(1, 5):
            g, wire = _slide_geom(n, dpi, over)
            want = g1.feedl + (n - 1) * holder.SLIDE_PITCH_HWDPI
            assert abs(g.feedl - want) <= 1, (dpi, n, g.feedl, want)
            assert g.leading_margin_mm == over, (dpi, n)
            assert g.trailing_margin_mm >= over - 1e-9, (dpi, n, g.trailing_margin_mm)
            span_mm = g.delivered_lines * (7200 // dpi) * MM
            assert span_mm >= holder.SLIDE_OPENING_MM + 2 * over - 1e-9, (dpi, n, span_mm)
            assert wire == g.chunks * t.LINES_PER_CHUNK and wire % 2 == 0
            assert holder.slide_travel_end(g.feedl, wire, dpi) <= holder.FEEDL_CEILING, (dpi, n)
    print("test_slide_feedl_grid_every_profile OK")


#: Per profile at the default overscan: slide -> (commanded FEEDL,
#: FEEDL + wire travel).
_DEFAULT_LEDGER = {
    600: {1: (6236, 17996), 2: (23981, 35741), 3: (41726, 53486), 4: (59471, 71231)},
    1200: {1: (6272, 18080), 2: (24017, 35825), 3: (41762, 53570), 4: (59507, 71315)},
    2400: {1: (6260, 17948), 2: (24005, 35693), 3: (41750, 53438), 4: (59495, 71183)},
    3600: {1: (6255, 17951), 2: (24000, 35696), 3: (41745, 53441), 4: (59490, 71186)},
    7200: {1: (6256, 17944), 2: (24001, 35689), 3: (41746, 53434), 4: (59491, 71179)},
}


def test_slide_ledger_default_every_profile():
    """With the default overscan ALL four slides pass validation on every
    profile, and the FEEDL / travel numbers are the pinned ones."""
    for dpi, want in _DEFAULT_LEDGER.items():
        got = {}
        for n in range(1, 5):
            g, wire = _slide_geom(n, dpi)
            got[n] = (g.feedl, holder.slide_travel_end(g.feedl, wire, dpi))
            assert got[n][1] <= holder.FEEDL_CEILING
        assert got == want, (dpi, got)
    print("test_slide_ledger_default_every_profile OK")


def test_slide_geometry_refusals():
    """Slide numbers outside 1-4, and any window whose FEEDL + travel
    passes FEEDL_CEILING, are refused by the geometry itself."""
    for bad in (0, 5, 6):
        _raises(safety.FrameOutOfRangeError, _slide_geom, bad, 3600)
    for bad in (0, -1.5, float("nan"), float("inf")):
        _raises(ValueError, _slide_geom, 1, 3600, bad)
    # Explicit overscans whose FEEDL + travel passes 71490 at slide 4: at
    # 600 dual the 588-step chunk rounding makes 2.0 / 2.5 too much.
    for dpi, ov in ((600, 2.0), (600, 2.5), (1200, 2.5), (3600, 3.0), (7200, 3.0)):
        e = _raises(safety.FeedlOutOfRangeError, _slide_geom, 4, dpi, ov)
        assert e.ceiling == holder.FEEDL_CEILING
        _slide_geom(3, dpi, ov)                   # slide 3 is far inside
    # The strip path is untouched by the stricter slide-only check.
    for dpi in DPIS:
        for frame in range(1, 7):
            _strip_geom(frame, dpi)
    print("test_slide_geometry_refusals OK")


def test_slide_position_offset_moves_every_feedl_and_is_travel_checked():
    """offset_mm (--position-offset, issue #61) moves every slide FEEDL by
    the same amount and leaves window length, chunks and margins alone; a
    positive offset that pushes slide 4 past the ceiling is refused; a
    negative one is not; an out-of-range offset is a ValueError."""
    for dpi in DPIS:
        for off in (-0.5, 0.25):
            shift = off / MM
            for n in range(1, 5):
                g0, w0 = _slide_geom(n, dpi)
                g1, w1 = _slide_geom(n, dpi, offset_mm=off)
                assert abs((g1.feedl - g0.feedl) - shift) <= 1, (dpi, n, off)
                assert (g1.chunks, g1.delivered_lines, w1) == \
                    (g0.chunks, g0.delivered_lines, w0), (dpi, n, off)
                assert abs(g1.leading_margin_mm - g0.leading_margin_mm) < 1e-9
                assert abs(g1.trailing_margin_mm - g0.trailing_margin_mm) < 1e-6
                assert holder.slide_travel_end(g1.feedl, w1, dpi) \
                    <= holder.FEEDL_CEILING, (dpi, n, off)
        assert _slide_geom(1, dpi, offset_mm=0.0) == _slide_geom(1, dpi)
        # Slide 4 sits <= 175 steps (0.62 mm) inside the ceiling on every
        # profile, so +1.0 mm must be refused by the travel check.
        _raises(safety.FeedlOutOfRangeError, _slide_geom, 4, dpi, offset_mm=1.0)
        _slide_geom(3, dpi, offset_mm=1.0)
        for bad in (holder.POSITION_OFFSET_MAX_MM + 0.1, float("nan")):
            _raises(ValueError, _slide_geom, 1, dpi, offset_mm=bad)
    print("test_slide_position_offset_moves_every_feedl_and_is_travel_checked OK")


def main():
    tests = [
        test_slide_model_matches_documented_numbers,
        test_slide_feedl_grid_every_profile,
        test_slide_ledger_default_every_profile,
        test_slide_geometry_refusals,
        test_slide_position_offset_moves_every_feedl_and_is_travel_checked,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
