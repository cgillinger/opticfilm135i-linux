#!/usr/bin/env python3
"""Offline tests for of135i.film110 -- the 110 (Pocket Instamatic) frame
detector -- and its CLI wiring, against synthetic fixtures built in-test
(no private data enters the repo; the real-strip fixtures live in the
private analysis area and are checked with tools/film110_check.py).

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_film110.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import cli, film110
from of135i.holder import FILM_110, STRIP

DPIS = (600, 3600)

MM_PER_INCH = 25.4
AIR = 65535.0
PLASTIC = 500.0
# Orange-mask-like clear base, per channel (docs/film-110-proposal.md).
CLEAR = {"R": 24000.0, "G": 11000.0, "B": 9000.0}
CHANNELS = ("R", "G", "B")


def _px_per_mm(dpi):
    return dpi / MM_PER_INCH


def _build_fixture(dpi, film=FILM_110, *, aperture_length_mm=36.0,
                   aperture_width_mm=24.0, perforated_side="lo",
                   holes_mm=(), images_mm=(), leading_dense_to_mm=None,
                   free_end_mm=None, seed=0, noise=120.0,
                   printed_border=False, light_border=False):
    """A synthetic aperture-registered crop: air on one side of the
    aperture width, a plastic rail on the other, and a `film.width_mm`
    band of clear-base film between them (with the perforated edge on
    `perforated_side`, "lo" or "hi").

    `holes_mm`: [(start, end), ...] air-level rectangles at the film's
    perforated edge, within the edge search margin.
    `images_mm`: [(line0, line1, col0, col1), ...] image rectangles; each
    is split into an upper "sky" half and a lower "shadow" half, all
    three channels scaled the same way off their own clear-base level.
    Without `printed_border` the halves are 15%/85% of the base (the
    surround is CLEAR film, brighter than the picture); with it they are
    60%/90% (the surround is the dark printed border, dimmer than the
    picture -- see `printed_border` below).
    `leading_dense_to_mm`: if given, the WHOLE film band from line 0 to
    this position is filled at a dense (continuing-image-like) level, to
    build a leading_continuation fixture.
    `free_end_mm`: if given, the film band (including its edge zone)
    reverts to air beyond this position -- a strip end inside the
    aperture.
    `printed_border`: model the real 110 film (measured 2026-09-19): a
    pre-exposed dark printed border (0.42 x clear base) surrounds every
    picture, filling the film band's interior from the rim inside the
    perforated edge to the far band edge -- not just the 0.8-2.0 mm
    strip the base fixture always paints. Each image is then painted
    BRIGHTER than that border (60%/90% instead of 15%/85%), and a thin
    (0.1 mm) bright halo (100% of clear base) is added just INSIDE each
    image's line0 and line1 edges -- the camera-gate halo that the first
    version of `_refine_edge` locked onto instead of the picture's own
    foot (see its docstring, Astra's review 2026-09-20). False (the
    default) reproduces today's fixture byte for byte.
    """
    rng = np.random.default_rng(seed)
    ppm = _px_per_mm(dpi)
    n_lines = int(round(aperture_length_mm * ppm))
    width = int(round(aperture_width_mm * ppm))
    band_w = int(round(film.width_mm * ppm))
    air_margin = int(round(2.0 * ppm))

    if perforated_side == "lo":
        band_lo = air_margin
    else:
        band_lo = width - air_margin - band_w
    band_hi = band_lo + band_w

    arr = np.empty((n_lines, width, 3), dtype=np.float64)
    arr[:, :, :] = AIR
    if perforated_side == "lo":
        arr[:, band_hi:, :] = PLASTIC
    else:
        arr[:, :band_lo, :] = PLASTIC
    for ci, key in enumerate(CHANNELS):
        arr[:, band_lo:band_hi, ci] = CLEAR[key]

    if leading_dense_to_mm is not None:
        end = int(round(leading_dense_to_mm * ppm))
        for ci, key in enumerate(CHANNELS):
            arr[:end, band_lo:band_hi, ci] = CLEAR[key] * 0.20

    if free_end_mm is not None:
        end_line = int(round(free_end_mm * ppm))
        arr[end_line:, band_lo:band_hi, :] = AIR

    # The real 110 film (measured 2026-09-19): a ~0.6 mm intact clear rim
    # at the perforated edge, a dark printed border ~0.8-2.0 mm inside the
    # edge along the whole strip, and the perforation punched INSIDE the
    # rim (0.6-2.1 mm from the edge) -- the film edge itself is continuous
    # through a hole.
    rim_px = int(round(0.6 * ppm))
    hole_w_px = int(round(1.5 * ppm))
    border_lo_px, border_hi_px = int(round(0.8 * ppm)), int(round(2.0 * ppm))
    for ci, key in enumerate(CHANNELS):
        if perforated_side == "lo":
            arr[:, band_lo + border_lo_px:band_lo + border_hi_px, ci] = CLEAR[key] * 0.42
        else:
            arr[:, band_hi - border_hi_px:band_hi - border_lo_px, ci] = CLEAR[key] * 0.42
    if printed_border:
        # The whole band interior (rim excepted) reads at the printed-
        # border level, not just the narrow 0.8-2.0 mm strip above --
        # inserted before the hole carving below, so holes still end up
        # AIR.
        for ci, key in enumerate(CHANNELS):
            if perforated_side == "lo":
                arr[:, band_lo + rim_px:band_hi, ci] = CLEAR[key] * 0.42
            else:
                arr[:, band_lo:band_hi - rim_px, ci] = CLEAR[key] * 0.42
    for (s_mm, e_mm) in holes_mm:
        s, e = int(round(s_mm * ppm)), int(round(e_mm * ppm))
        s, e = max(0, s), min(n_lines, e)
        if e <= s:
            continue
        if perforated_side == "lo":
            arr[s:e, band_lo + rim_px:band_lo + rim_px + hole_w_px, :] = AIR
        else:
            arr[s:e, band_hi - rim_px - hole_w_px:band_hi - rim_px, :] = AIR

    dense_frac, thin_frac = (0.60, 0.90) if printed_border else (0.15, 0.85)
    if light_border:
        # The second real strip: a ~0.5 mm fogged margin on both lateral
        # sides of the picture, LIGHTER than the picture (density ~0.8
        # against ~0.9-1.05), so the picture halves are made denser than
        # that margin; the along-transport border stays the dark one.
        dense_frac, thin_frac = 0.30, 0.55
    strip_px = int(round(0.5 * ppm))
    halo_px = max(1, int(round(0.1 * ppm)))
    for (l0_mm, l1_mm, c0_mm, c1_mm) in images_mm:
        l0, l1 = int(round(l0_mm * ppm)), int(round(l1_mm * ppm))
        c0, c1 = int(round(c0_mm * ppm)), int(round(c1_mm * ppm))
        l0c, l1c = max(0, l0), min(n_lines, l1)
        c0c, c1c = max(0, c0), min(width, c1)
        if l1c <= l0c or c1c <= c0c:
            continue
        mid = (l0c + l1c) // 2
        if light_border:
            for ci, key in enumerate(CHANNELS):
                arr[l0c:l1c, max(0, c0c - strip_px):c0c, ci] = CLEAR[key] * 0.75
                arr[l0c:l1c, c1c:min(width, c1c + strip_px), ci] = CLEAR[key] * 0.75
        for ci, key in enumerate(CHANNELS):
            base = CLEAR[key]
            arr[l0c:mid, c0c:c1c, ci] = base * dense_frac   # sky: dense half
            arr[mid:l1c, c0c:c1c, ci] = base * thin_frac    # shadow: thin half
        if printed_border:
            # The camera-gate halo: brighter than the picture itself, just
            # inside each along-transport edge.
            for ci, key in enumerate(CHANNELS):
                arr[l0c:min(l1c, l0c + halo_px), c0c:c1c, ci] = CLEAR[key] * 1.0
                arr[max(l0c, l1c - halo_px):l1c, c0c:c1c, ci] = CLEAR[key] * 1.0

    arr += rng.normal(0.0, noise, size=arr.shape)
    arr = np.clip(arr, 0, 65535)
    return arr.astype(np.uint16), ppm, band_lo, band_hi


def _mm(v, ppm):
    return v / ppm


# ---------------------------------------------------------------- detector


def test_whole_and_trailing_split():
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
        # hole1 -> whole image; hole2 (one pitch on) -> image runs off
        # the far end. Holes sit in the gap between images: image end +
        # trail = next hole start, hole end + lead = next image start.
        h1 = (3.0, 4.5)
        img1_line0 = h1[1] + lead
        h2 = (h1[0] + FILM_110.pitch_mm, h1[1] + FILM_110.pitch_mm)
        img2_line0 = h2[1] + lead
        col0 = 2.0 + FILM_110.image_lateral_offset_mm
        col1 = col0 + FILM_110.image_mm[1]
        arr, _ppm, _lo, _hi = _build_fixture(
            dpi, holes_mm=[h1, h2],
            images_mm=[(img1_line0, img1_line0 + ilen, col0, col1),
                       (img2_line0, img2_line0 + ilen, col0, col1)])
        find = film110.detect(arr, dpi=dpi, film=FILM_110)
        assert not find.empty, (dpi, find.reason)
        assert find.reason == "", (dpi, find.reason)
        assert len(find.frames) == 2, (dpi, [f.whole for f in find.frames])
        whole = [f for f in find.frames if f.whole]
        split = [f for f in find.frames if not f.whole]
        assert len(whole) == 1 and len(split) == 1, (dpi, find.frames)
        w = whole[0]
        assert w.orientation == "hole-before-image"
        assert abs(_mm(w.line0, ppm) - img1_line0) <= 0.3, (dpi, w.line0 / ppm)
        assert abs(_mm(w.line1, ppm) - (img1_line0 + ilen)) <= 0.3, (dpi, w.line1 / ppm)
        assert abs(_mm(w.col0, ppm) - col0) <= 0.3, (dpi, w.col0 / ppm)
        assert abs(_mm(w.col1, ppm) - col1) <= 0.3, (dpi, w.col1 / ppm)
        assert set(w.refined) == {"line0", "line1", "col0", "col1"}
        s = split[0]
        assert s.split == "trailing", (dpi, s.split)
        assert s.index != w.index
        print(f"test_whole_and_trailing_split OK ({dpi} dpi)")


def test_leading_continuation_and_whole():
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
        hole = (8.0, 9.5)
        img_line0 = hole[1] + lead
        col0 = 2.0 + FILM_110.image_lateral_offset_mm
        col1 = col0 + FILM_110.image_mm[1]
        # The continuing (split) image ends trail_mm before the hole.
        cont_end = hole[0] - FILM_110.perforation_trail_mm
        arr, _ppm, _lo, _hi = _build_fixture(
            dpi, holes_mm=[hole], leading_dense_to_mm=cont_end,
            images_mm=[(img_line0, img_line0 + ilen, col0, col1)])
        find = film110.detect(arr, dpi=dpi, film=FILM_110)
        assert not find.empty, (dpi, find.reason)
        assert find.leading_continuation is True, dpi
        assert find.film_line_start is None, (dpi, find.film_line_start)
        # Two finds: the continuing image (a leading split, predicted from
        # the hole after it) and the whole one.
        assert len(find.frames) == 2, (dpi, find.frames)
        lead_split = [f for f in find.frames if f.split == "leading"]
        assert len(lead_split) == 1, (dpi, find.frames)
        assert abs(_mm(lead_split[0].line1, ppm) - cont_end) <= 0.3, (
            dpi, lead_split[0].line1 / ppm)
        w = [f for f in find.frames if f.whole][0]
        assert abs(_mm(w.line0, ppm) - img_line0) <= 0.3, (dpi, w.line0 / ppm)
        print(f"test_leading_continuation_and_whole OK ({dpi} dpi)")


def test_empty_aperture():
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        n_lines = int(round(36.0 * ppm))
        width = int(round(24.0 * ppm))
        arr = np.full((n_lines, width, 3), AIR, dtype=np.uint16)
        find = film110.detect(arr, dpi=dpi, film=FILM_110)
        assert find.empty, (dpi, find.reason)
        assert find.frames == []
        print(f"test_empty_aperture OK ({dpi} dpi)")


def test_free_end_near_image():
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
        hole = (10.0, 11.5)
        img_line0 = hole[1] + lead              # ~13.8
        img_line1 = img_line0 + ilen             # ~31.0
        col0 = 2.0 + FILM_110.image_lateral_offset_mm
        col1 = col0 + FILM_110.image_mm[1]
        free_end = img_line1 + 1.0               # well within pitch_mm (25.5)
        arr, _ppm, _lo, _hi = _build_fixture(
            dpi, holes_mm=[hole],
            images_mm=[(img_line0, img_line1, col0, col1)],
            free_end_mm=free_end)
        find = film110.detect(arr, dpi=dpi, film=FILM_110)
        assert not find.empty, (dpi, find.reason)
        assert find.film_line_end is not None, dpi
        assert abs(_mm(find.film_line_end, ppm) - free_end) <= 0.3, (
            dpi, find.film_line_end / ppm)
        assert len(find.frames) == 1, (dpi, find.frames)
        w = find.frames[0]
        assert w.whole and w.free_end_near, (dpi, w)
        print(f"test_free_end_near_image OK ({dpi} dpi)")


def test_reversed_orientation_hole_after_image():
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
        img_line0, img_line1 = 5.0, 5.0 + ilen   # image BEFORE its hole
        hole = (img_line1 + lead, img_line1 + lead + 1.5)
        col0 = 2.0 + FILM_110.image_lateral_offset_mm
        col1 = col0 + FILM_110.image_mm[1]
        arr, _ppm, _lo, _hi = _build_fixture(
            dpi, holes_mm=[hole],
            images_mm=[(img_line0, img_line1, col0, col1)])
        find = film110.detect(arr, dpi=dpi, film=FILM_110)
        assert not find.empty, (dpi, find.reason)
        assert len(find.frames) == 1, (dpi, find.frames)
        w = find.frames[0]
        assert w.orientation == "hole-after-image", (dpi, w.orientation)
        assert w.whole, (dpi, w)
        assert abs(_mm(w.line0, ppm) - img_line0) <= 0.3, (dpi, w.line0 / ppm)
        assert abs(_mm(w.line1, ppm) - img_line1) <= 0.3, (dpi, w.line1 / ppm)
        print(f"test_reversed_orientation_hole_after_image OK ({dpi} dpi)")


def test_perforated_edge_on_high_side():
    """The lateral sign handling (col0/col1 direction) for a film band
    whose perforated edge is the HIGH-column side of the aperture."""
    dpi = 600
    ppm = _px_per_mm(dpi)
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    hole = (10.0, 11.5)
    img_line0 = hole[1] + lead
    # perforated edge is "hi": image sits INSIDE the band, offset from
    # the high edge towards the interior (i.e. towards lower columns).
    band_hi_mm = 24.0 - 2.0  # matches the fixture's air_margin=2.0mm on lo side...
    col1 = band_hi_mm - FILM_110.image_lateral_offset_mm
    col0 = col1 - FILM_110.image_mm[1]
    arr, _ppm, band_lo, band_hi = _build_fixture(
        dpi, perforated_side="hi", holes_mm=[hole],
        images_mm=[(img_line0, img_line0 + ilen, col0, col1)])
    find = film110.detect(arr, dpi=dpi, film=FILM_110)
    assert not find.empty, find.reason
    assert len(find.frames) == 1, find.frames
    w = find.frames[0]
    assert w.whole, w
    assert abs(_mm(w.col0, ppm) - col0) <= 0.3, w.col0 / ppm
    assert abs(_mm(w.col1, ppm) - col1) <= 0.3, w.col1 / ppm
    print("test_perforated_edge_on_high_side OK")


def test_crop_matches_visible_and_ir_indices():
    dpi = 600
    ppm = _px_per_mm(dpi)
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    hole = (10.0, 11.5)
    img_line0 = hole[1] + lead
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    arr, _ppm, _lo, _hi = _build_fixture(
        dpi, holes_mm=[hole],
        images_mm=[(img_line0, img_line0 + ilen, col0, col1)])
    find = film110.detect(arr, dpi=dpi, film=FILM_110)
    w = find.frames[0]
    vis_crop = film110.crop(arr, w)
    ir2d = arr[:, :, 1]  # a stand-in 2-D "IR" array on the same grid
    ir_crop = film110.crop(ir2d, w)
    assert vis_crop.shape[:2] == ir_crop.shape, (vis_crop.shape, ir_crop.shape)
    assert vis_crop.shape[0] > 0 and vis_crop.shape[1] > 0
    print("test_crop_matches_visible_and_ir_indices OK")


# The along-transport (line0/line1) and rail-side (col1) edges in this
# fixture are perfectly sharp (a single-pixel step) -- unlike a real
# camera-gate edge, which is soft over a few tenths of a millimetre. Box-
# smoothing that sharp step (REFINE_SMOOTH_MM) before the foot walk
# always produces two (600 dpi, a 2-tap box) or a whole ramp (3600 dpi, a
# 14-tap box) of near-equal steps, so the walk-to-the-foot lands a
# reproducible ~0.05-0.08 mm past the painted edge regardless of hole/
# image position or noise seed (measured by hand, both DPIs, four
# along-strip offsets) -- not the true asymptotic accuracy of the fix,
# which docs/film-110.md §9 puts at 0.1-0.2 mm on real (soft-edged)
# hardware frames. 0.09 mm covers the measured worst case (0.0815 mm at
# 600 dpi) with headroom; see this test's docstring.
_FOOT_TOLERANCE_MM = 0.09


def test_refined_edges_land_on_the_foot_with_a_dark_printed_border():
    """With a printed dark border around the picture (`printed_border`),
    every edge -- including col0, the perforated side, which the first
    version of `_refine_edge` treated as unrefinable -- must land within
    `_FOOT_TOLERANCE_MM` of the painted picture edge, all four `refined`
    flags True.

    The first version of `_refine_edge` assumed a clear (brighter)
    surround (the base fixture's default) and, on the real 110 strip,
    locked onto the fall-off of the bright camera-gate halo 0.1-0.4 mm
    INSIDE the picture instead of the picture's own foot (Astra's review,
    2026-09-20; see `_refine_edge`'s docstring). This fixture reproduces
    that surround -- a dark printed border plus the halo -- and checks
    the fix against it.
    """
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
        hole = (10.0, 11.5)
        img_line0 = hole[1] + lead
        img_line1 = img_line0 + ilen
        # col0 sits image_lateral_offset_mm (2.0 mm) inside the
        # perforated film edge -- >= 1.0 mm, so the level bands
        # _refine_edge reads (LEVEL_NEAR_MM..LEVEL_FAR_MM either side of
        # the predicted edge) stay inside the printed border, never into
        # the 0.6 mm intact rim (see `_build_fixture`'s `printed_border`
        # docstring).
        col0 = 2.0 + FILM_110.image_lateral_offset_mm
        col1 = col0 + FILM_110.image_mm[1]
        arr, _ppm, _lo, _hi = _build_fixture(
            dpi, holes_mm=[hole],
            images_mm=[(img_line0, img_line1, col0, col1)],
            printed_border=True)
        find = film110.detect(arr, dpi=dpi, film=FILM_110)
        assert not find.empty, (dpi, find.reason)
        whole = [f for f in find.frames if f.whole]
        assert len(whole) == 1, (dpi, find.frames)
        w = whole[0]
        assert all(w.refined.values()), (dpi, w.refined)
        assert abs(_mm(w.line0, ppm) - img_line0) <= _FOOT_TOLERANCE_MM, (dpi, w.line0 / ppm)
        assert abs(_mm(w.line1, ppm) - img_line1) <= _FOOT_TOLERANCE_MM, (dpi, w.line1 / ppm)
        assert abs(_mm(w.col0, ppm) - col0) <= _FOOT_TOLERANCE_MM, (dpi, w.col0 / ppm)
        assert abs(_mm(w.col1, ppm) - col1) <= _FOOT_TOLERANCE_MM, (dpi, w.col1 / ppm)
        print(f"test_refined_edges_land_on_the_foot_with_a_dark_printed_border OK ({dpi} dpi)")


def test_lateral_edges_found_with_a_lighter_border_too():
    """The second real strip (2026-09-20) has a ~0.5 mm fogged margin on
    both sides of the picture that is LIGHTER than the picture (density
    ~0.8 against ~0.9-1.05), where the first strip's printed border is
    darker; the rail-side refinement, given a fixed step direction and a
    level check around a prediction 0.4 mm off, either found nothing or
    a picture-content step, and the first cut lost 0.2 mm of picture on
    two images. The lateral refinement is now direction-free: both
    border polarities must land within 0.09 mm of the painted edge."""
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
        hole = (10.0, 11.5)
        img = hole[1] + lead
        col0 = 2.0 + FILM_110.image_lateral_offset_mm - 0.4   # 0.4 mm off the model, as on the real strip
        col1 = col0 + FILM_110.image_mm[1] + 0.4
        for light_border in (False, True):
            arr, _, _, _ = _build_fixture(
                dpi, holes_mm=[hole], printed_border=True, light_border=light_border,
                images_mm=[(img, img + ilen, col0, col1)])
            find = film110.detect(arr, dpi=dpi, film=FILM_110)
            whole = [f for f in find.frames if f.whole]
            assert len(whole) == 1, (dpi, light_border, find.frames)
            w = whole[0]
            assert w.refined["col0"] and w.refined["col1"], (dpi, light_border, w.refined)
            assert abs(_mm(w.col0, ppm) - col0) <= 0.09, (dpi, light_border, w.col0 / ppm)
            assert abs(_mm(w.col1, ppm) - col1) <= 0.09, (dpi, light_border, w.col1 / ppm)
        print(f"test_lateral_edges_found_with_a_lighter_border_too OK ({dpi} dpi)")


def test_image_number_is_the_strip_position_in_both_placements():
    """Pure-function test of image_number/placement_phase_mm against the
    strip positions measured 2026-09-19 (docs/film-110.md §3): the same
    photograph's aperture-local position, converted through the aperture
    grid to a strip position, gets the same number under both
    placements' phase."""
    cases_a = [(1, 3.53, 1), (1, 28.85, 2), (2, 16.17, 3), (3, 3.70, 4)]
    cases_b = [(1, 15.73, 1), (2, 3.26, 2), (2, 28.74, 3), (3, 16.32, 4)]
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        for placement, cases in (("A", cases_a), ("B", cases_b)):
            for aperture, pos_mm, expected_n in cases:
                n, residual = film110.image_number(
                    aperture, pos_mm * ppm, dpi, placement)
                assert n == expected_n, (dpi, placement, aperture, pos_mm, n)
                assert abs(residual) <= 1.0, (
                    dpi, placement, aperture, pos_mm, residual)

        # The first, unruled placement of that evening: 17.00 mm declared
        # "A" is off that phase grid by more than the tolerance.
        _n, residual = film110.image_number(1, 17.00 * ppm, dpi, "A")
        assert abs(residual) > film110.PLACEMENT_PHASE_TOLERANCE_MM, (dpi, residual)

    try:
        film110.placement_phase_mm("C")
        assert False, "placement_phase_mm('C') must raise ValueError"
    except ValueError:
        pass
    print("test_image_number_is_the_strip_position_in_both_placements OK")


# --------------------------------------------------------------------- CLI


def _hook_args(dpi=600, positive=False, rotate=0, placement="A"):
    import types
    return types.SimpleNamespace(dpi=dpi, positive=positive, rotate=rotate,
                                 placement=placement)


def test_cli_hook_numbers_across_apertures_and_writes_whole_only():
    """Placement A, apertures 1 and 2 (docs/film-110.md §3): image 1
    starts at placement A's own phase in aperture 1, image 2 (one film
    pitch on) runs off the aperture's far end (trailing split, not
    written), and image 3 (two film pitches on) lands whole in
    aperture 2 -- with image 2's tail continuing in as aperture 2's
    leading split, already counted in aperture 1 and not renumbered."""
    dpi = 600
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    phase = film110.PLACEMENT_A_PHASE_MM
    pitch_ap = STRIP.pitch_mm
    pitch_film = FILM_110.pitch_mm

    img1 = phase                                  # aperture 1, local mm
    img2_global = phase + pitch_film               # == aperture 1, local mm
    h1 = (img1 - lead - 1.5, img1 - lead)
    h2 = (img2_global - lead - 1.5, img2_global - lead)
    ap1, _, _, _ = _build_fixture(
        dpi, holes_mm=[h1, h2],
        images_mm=[(img1, img1 + ilen, col0, col1),
                   (img2_global, img2_global + ilen, col0, col1)], seed=1)

    img3_global = phase + 2 * pitch_film
    img3_local = img3_global - pitch_ap            # aperture 2, local mm
    h3 = (img3_local - lead - 1.5, img3_local - lead)
    img2_tail_end = (img2_global + ilen) - pitch_ap  # aperture 2, local mm
    ap2, _, _, _ = _build_fixture(
        dpi, holes_mm=[h3], leading_dense_to_mm=img2_tail_end,
        images_mm=[(img3_local, img3_local + ilen, col0, col1)], seed=2)

    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi, placement="A")
        state = cli.Film110State()
        cli._film110_hook(args, state, 1, out, ap1, None)
        cli._film110_hook(args, state, 2, out, ap2, None)

        # aperture 1: image 1 (whole) + image 2 (trailing split, not
        # written); aperture 2: image 3 (whole; image 2's leading tail
        # was already counted in aperture 1, so it is not renumbered).
        assert Path(cli._film110_image_path(out, 1)).exists()
        assert not Path(cli._film110_image_path(out, 2)).exists()
        assert Path(cli._film110_image_path(out, 3)).exists()
        assert state.problems == [], state.problems
    print("test_cli_hook_numbers_across_apertures_and_writes_whole_only OK")


def test_cli_hook_writes_ir_with_same_indices():
    dpi = 600
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    img = film110.PLACEMENT_A_PHASE_MM
    hole = (img - lead - 1.5, img - lead)
    vis, _, _, _ = _build_fixture(
        dpi, holes_mm=[hole], images_mm=[(img, img + ilen, col0, col1)])
    ir = vis[:, :, 1].copy()  # a stand-in single-channel IR frame

    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi, placement="A")
        state = cli.Film110State()
        cli._film110_hook(args, state, 1, out, vis, ir)
        assert Path(cli._film110_image_path(out, 1)).exists()
        assert Path(cli._film110_image_path(out, 1, ir=True)).exists()
        assert state.problems == [], state.problems
    print("test_cli_hook_writes_ir_with_same_indices OK")


def test_cli_hook_refuses_to_overwrite():
    """A second hook call that lands on the same image number and the
    same -o stem must not touch the files the first call wrote -- for
    either the visible product or its IR sidecar -- and must record
    exactly one problem."""
    dpi = 600
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    img = film110.PLACEMENT_A_PHASE_MM
    hole = (img - lead - 1.5, img - lead)
    vis, _, _, _ = _build_fixture(
        dpi, holes_mm=[hole], images_mm=[(img, img + ilen, col0, col1)])
    ir = vis[:, :, 1].copy()

    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi, placement="A")
        state = cli.Film110State()
        cli._film110_hook(args, state, 1, out, vis, ir)
        vis_path = cli._film110_image_path(out, 1)
        ir_path = cli._film110_image_path(out, 1, ir=True)
        assert Path(vis_path).exists() and Path(ir_path).exists()
        assert state.problems == [], state.problems
        before_vis = Path(vis_path).read_bytes()
        before_ir = Path(ir_path).read_bytes()

        cli._film110_hook(args, state, 1, out, vis, ir)
        assert Path(vis_path).read_bytes() == before_vis, "visible product overwritten"
        assert Path(ir_path).read_bytes() == before_ir, "IR sidecar overwritten"
        assert len(state.problems) == 1, state.problems
        assert "not overwritten" in state.problems[0], state.problems
    print("test_cli_hook_refuses_to_overwrite OK")


def test_cli_hook_refuses_wrong_placement_phase():
    """An image whose aperture-local position matches placement A's
    phase, run with --placement B, is off that placement's phase grid --
    refused, nothing written."""
    dpi = 600
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    img = film110.PLACEMENT_A_PHASE_MM
    hole = (img - lead - 1.5, img - lead)
    vis, _, _, _ = _build_fixture(
        dpi, holes_mm=[hole], images_mm=[(img, img + ilen, col0, col1)])

    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi, placement="B")
        state = cli.Film110State()
        cli._film110_hook(args, state, 1, out, vis, None)
        assert list(Path(d).iterdir()) == [], "no file should have been written"
        assert len(state.problems) == 1, state.problems
        assert "phase" in state.problems[0], state.problems
    print("test_cli_hook_refuses_wrong_placement_phase OK")


def test_cli_hook_same_photo_same_number_across_placements():
    """The same four-image strip, scanned as two separate `scan`
    commands (one per placement, docs/film-110.md §3): placement A sees
    apertures 1-2 (image 1 whole, image 2 trailing split), placement B
    sees apertures 2-3 (image 2 whole, image 3 trailing split; image 4
    whole in aperture 3). Every whole image gets the SAME number
    regardless of which placement or apertures it was scanned in."""
    dpi = 600
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    pitch_ap = STRIP.pitch_mm
    pitch_film = FILM_110.pitch_mm
    phase_a = film110.PLACEMENT_A_PHASE_MM
    phase_b = film110.placement_phase_mm("B")

    # Placement A: image 1 in aperture 1, image 2 (one film pitch on)
    # trailing off aperture 1's far end, continuing into aperture 2 where
    # image 3 (two film pitches on) lands whole.
    img1_a = phase_a
    img2_a_global = phase_a + pitch_film
    h1a = (img1_a - lead - 1.5, img1_a - lead)
    h2a = (img2_a_global - lead - 1.5, img2_a_global - lead)
    a_ap1, _, _, _ = _build_fixture(
        dpi, holes_mm=[h1a, h2a],
        images_mm=[(img1_a, img1_a + ilen, col0, col1),
                   (img2_a_global, img2_a_global + ilen, col0, col1)], seed=11)

    img3_a_global = phase_a + 2 * pitch_film
    img3_a_local = img3_a_global - pitch_ap
    h3a = (img3_a_local - lead - 1.5, img3_a_local - lead)
    tail2_a_end = (img2_a_global + ilen) - pitch_ap
    a_ap2, _, _, _ = _build_fixture(
        dpi, holes_mm=[h3a], leading_dense_to_mm=tail2_a_end,
        images_mm=[(img3_a_local, img3_a_local + ilen, col0, col1)], seed=12)

    # Placement B: image 2 (one film pitch past image 1's placement-A
    # position) lands whole in aperture 2, image 3 trails off aperture 2's
    # far end into aperture 3, where image 4 lands whole.
    img2_b_global = phase_b + pitch_film
    img2_b_local = img2_b_global - pitch_ap
    h2b = (img2_b_local - lead - 1.5, img2_b_local - lead)

    img3_b_global = phase_b + 2 * pitch_film
    img3_b_local = img3_b_global - pitch_ap
    h3b = (img3_b_local - lead - 1.5, img3_b_local - lead)

    b_ap2, _, _, _ = _build_fixture(
        dpi, holes_mm=[h2b, h3b],
        images_mm=[(img2_b_local, img2_b_local + ilen, col0, col1),
                   (img3_b_local, img3_b_local + ilen, col0, col1)], seed=21)

    img4_b_global = phase_b + 3 * pitch_film
    img4_b_local = img4_b_global - 2 * pitch_ap
    h4b = (img4_b_local - lead - 1.5, img4_b_local - lead)
    b_ap3, _, _, _ = _build_fixture(
        dpi, holes_mm=[h4b],
        images_mm=[(img4_b_local, img4_b_local + ilen, col0, col1)], seed=22)

    with tempfile.TemporaryDirectory() as d:
        a_out = str(Path(d) / "a.tiff")
        b_out = str(Path(d) / "b.tiff")
        a_args = _hook_args(dpi=dpi, placement="A")
        b_args = _hook_args(dpi=dpi, placement="B")
        a_state = cli.Film110State()
        b_state = cli.Film110State()

        cli._film110_hook(a_args, a_state, 1, a_out, a_ap1, None)
        cli._film110_hook(a_args, a_state, 2, a_out, a_ap2, None)
        cli._film110_hook(b_args, b_state, 2, b_out, b_ap2, None)
        cli._film110_hook(b_args, b_state, 3, b_out, b_ap3, None)

        assert Path(cli._film110_image_path(a_out, 1)).exists()
        assert Path(cli._film110_image_path(a_out, 3)).exists()
        assert Path(cli._film110_image_path(b_out, 2)).exists()
        assert Path(cli._film110_image_path(b_out, 4)).exists()
        # ... and nothing else: no counter-numbered duplicates, no
        # product for the trailing splits.
        written = sorted(p.name for p in Path(d).iterdir())
        assert written == ["a-image1.tiff", "a-image3.tiff",
                           "b-image2.tiff", "b-image4.tiff"], written
        assert a_state.problems == [], a_state.problems
        assert b_state.problems == [], b_state.problems
    print("test_cli_hook_same_photo_same_number_across_placements OK")


def test_validate_film110_args():
    parser = cli.build_parser()

    args = parser.parse_args(["scan", "--film", "110", "-o", "x.tiff"])
    err = cli._validate_film110_args(args)
    assert err is not None and "--placement" in err, err

    args = parser.parse_args(["scan", "--placement", "A", "-o", "x.tiff"])
    err = cli._validate_film110_args(args)
    assert err is not None, err

    args = parser.parse_args(["scan", "--film", "110", "--placement", "B", "-o", "x.tiff"])
    err = cli._validate_film110_args(args)
    assert err is None, err

    args = parser.parse_args(["scan", "-o", "x.tiff"])
    err = cli._validate_film110_args(args)
    assert err is None, err
    print("test_validate_film110_args OK")


def test_cli_hook_is_noop_for_film_135():
    """--film 135 (film110_state=None, the default): the hook must be a
    complete no-op, nothing written, nothing raised -- byte-identical to
    not having the hook at all."""
    dpi = 600
    arr = np.full((100, 200, 3), 40000, dtype=np.uint16)
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi)
        cli._film110_hook(args, None, 1, out, arr, None)
        assert list(Path(d).iterdir()) == [], "the 135 path must write nothing"
    print("test_cli_hook_is_noop_for_film_135 OK")


def test_cli_scan_parser_defaults_to_135():
    parser = cli.build_parser()
    args = parser.parse_args(["scan", "-o", "/tmp/x.tiff"])
    assert args.film == "135", args.film
    args2 = parser.parse_args(["scan", "--film", "110", "-o", "/tmp/x.tiff"])
    assert args2.film == "110", args2.film
    print("test_cli_scan_parser_defaults_to_135 OK")


def main() -> int:
    tests = [
        test_whole_and_trailing_split,
        test_leading_continuation_and_whole,
        test_empty_aperture,
        test_free_end_near_image,
        test_reversed_orientation_hole_after_image,
        test_perforated_edge_on_high_side,
        test_crop_matches_visible_and_ir_indices,
        test_refined_edges_land_on_the_foot_with_a_dark_printed_border,
        test_lateral_edges_found_with_a_lighter_border_too,
        test_image_number_is_the_strip_position_in_both_placements,
        test_cli_hook_numbers_across_apertures_and_writes_whole_only,
        test_cli_hook_writes_ir_with_same_indices,
        test_cli_hook_refuses_to_overwrite,
        test_cli_hook_refuses_wrong_placement_phase,
        test_cli_hook_same_photo_same_number_across_placements,
        test_cli_hook_is_noop_for_film_135,
        test_cli_scan_parser_defaults_to_135,
        test_validate_film110_args,
    ]
    passed = 0
    for t in tests:
        t()
        passed += 1
    print(f"\n{passed} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
