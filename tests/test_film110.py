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
DEFAULT_FIXTURE_SHA256 = "c44b1bbd44994e0c0430090bd8a71e343239ac7445d7217ba47f85a565df2cbc"

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

    # --placement is optional with --film 110 now (inferred).
    args = parser.parse_args(["scan", "--film", "110", "-o", "x.tiff"])
    err = cli._validate_film110_args(args)
    assert err is None, err

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


def _one_image_aperture(local_mm, dpi=600, seed=5):
    """Aperture 1 with a single whole image starting `local_mm` in."""
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    hole = (local_mm - lead - 1.5, local_mm - lead)
    vis, _, _, _ = _build_fixture(
        dpi, holes_mm=[hole], images_mm=[(local_mm, local_mm + ilen, col0, col1)],
        seed=seed)
    return vis


def test_infer_placement_on_the_documented_phases():
    """infer_placement on the measured phases of docs/film-110.md §3/§5
    (A 3.5 mm: measured 3.53/3.58; B 16.25 mm: measured 15.73) and the
    midway case (~9.9 mm: 6.4 mm from each phase -> None)."""
    for dpi in DPIS:
        ppm = _px_per_mm(dpi)
        for aperture, pos, want in [(1, 3.53, "A"), (1, 3.58, "A"),
                                    (1, 15.73, "B"), (1, 16.25, "B"),
                                    (2, 16.17, "A"), (2, 3.26, "B"),
                                    (3, 3.70, "A"), (3, 16.32, "B"),
                                    (1, 1.0, "A"), (1, 18.0, "B")]:
            got, res = film110.infer_placement(aperture, pos * ppm, dpi)
            assert got == want, (dpi, aperture, pos, got, res)
            assert abs(res) <= film110.PLACEMENT_INFER_TOLERANCE_MM, res
        got, res = film110.infer_placement(1, 9.9 * ppm, dpi)
        assert got is None and abs(res) > film110.PLACEMENT_INFER_TOLERANCE_MM, (got, res)
    assert film110.PLACEMENT_INFER_TOLERANCE_MM == 4.0
    print("test_infer_placement_on_the_documented_phases OK")


def test_cli_hook_infers_a_and_b():
    """No --placement: the first numbered image decides, and says so."""
    import contextlib
    import io
    dpi = 600
    for local, want, number in ((film110.PLACEMENT_A_PHASE_MM, "A", 1),
                                (film110.placement_phase_mm("B"), "B", 1)):
        vis = _one_image_aperture(local, dpi)
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "f.tiff")
            args = _hook_args(dpi=dpi, placement=None)
            state = cli.Film110State()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli._film110_hook(args, state, 1, out, vis, None)
            assert state.placement == want and not state.placement_given, state.placement
            assert Path(cli._film110_image_path(out, number)).exists()
            assert state.problems == [], state.problems
            assert f"placement {want} (inferred)" in buf.getvalue(), buf.getvalue()
            assert [e["number"] for e in state.images] == [number], state.images
    # Given placement is reported as given.
    vis = _one_image_aperture(film110.PLACEMENT_A_PHASE_MM, dpi)
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        buf = io.StringIO()
        state = cli.Film110State()
        with contextlib.redirect_stdout(buf):
            cli._film110_hook(_hook_args(dpi=dpi, placement="A"), state, 1, out, vis, None)
        assert "placement A (given)" in buf.getvalue(), buf.getvalue()
        assert state.placement_given
    print("test_cli_hook_infers_a_and_b OK")


def test_cli_hook_refuses_the_ambiguous_position_then_decides_later():
    """An image midway between the phases cannot be inferred: problem
    recorded, nothing written, placement still undecided -- a later image
    on a clear phase then decides."""
    dpi = 600
    mid = _one_image_aperture(9.9, dpi)
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi, placement=None)
        state = cli.Film110State()
        cli._film110_hook(args, state, 1, out, mid, None)
        assert list(Path(d).iterdir()) == []
        assert state.placement is None
        assert len(state.problems) == 1, state.problems
        assert "between the two placements" in state.problems[0], state.problems
        assert "--placement A or B" in state.problems[0], state.problems
        clear = _one_image_aperture(film110.placement_phase_mm("B"), dpi)
        cli._film110_hook(args, state, 1, out, clear, None)
        assert state.placement == "B", state.placement
        assert Path(cli._film110_image_path(out, 1)).exists()
    print("test_cli_hook_refuses_the_ambiguous_position_then_decides_later OK")


def test_explicit_placement_overrides_inference():
    """--placement B on an image that reads as A (phase 3.5 mm): the
    explicit flag wins -- the image is not inferred away, it is refused
    as off the given placement's phase, exactly as before."""
    dpi = 600
    vis = _one_image_aperture(film110.PLACEMENT_A_PHASE_MM, dpi)
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        state = cli.Film110State()
        cli._film110_hook(_hook_args(dpi=dpi, placement="B"), state, 1, out, vis, None)
        assert state.placement == "B" and state.placement_given
        assert list(Path(d).iterdir()) == []
        assert len(state.problems) == 1 and "phase" in state.problems[0], state.problems
    print("test_explicit_placement_overrides_inference OK")


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


def test_number_by_shift():
    pitch = FILM_110.pitch_mm
    rec = [(1, 3.5), (3, 3.5 + 2 * pitch), (4, 3.5 + 3 * pitch)]
    now = [3.5 + 10.6 + k * pitch for k in range(4)]
    for k, want in enumerate((1, 2, 3, 4)):
        num, res = film110.number_by_shift(now[k], 10.6, rec)
        assert num == want and abs(res) < 1e-9, (k, num, res)
    # Before the roll's first image: number < 1 (the caller refuses it).
    num, _res = film110.number_by_shift(3.5 + 10.6 - pitch, 10.6, rec)
    assert num == 0, num
    print("test_number_by_shift OK")


def test_thumbnail_and_decide_orientation():
    rng = np.random.default_rng(7)

    def picture(seed):
        r = np.random.default_rng(seed)
        base = r.normal(size=(12, 9))
        return np.kron(base, np.ones((40, 40)))[:480, :360]

    pics = {k: picture(k) for k in (1, 2, 3, 4)}
    th = {k: film110.thumbnail(v) for k, v in pics.items()}
    assert th[1].shape == film110.THUMB_SIZE
    assert abs(float(th[1].mean())) < 1e-5 and abs(float(th[1].std()) - 1) < 1e-4
    assert film110.thumb_correlation(th[1], th[1]) > 0.999
    assert abs(film110.thumb_correlation(th[1], th[2])) < 0.5
    # The thumbnail does not depend on the scan resolution.
    half = film110.thumbnail(pics[1][::2, ::2])
    assert film110.thumb_correlation(th[1], half) > 0.98
    assert float(film110.thumbnail(np.full((200, 200), 5.0)).std()) == 0.0
    pitch = FILM_110.pitch_mm
    L = FILM_110.image_mm[0]
    # Sightings: (number, position, u0, u1, patch) / (position, u0, u1, patch);
    # the pictures are 480 x 360 px for 17.2 x 13 mm, i.e. ~28 px/mm.
    pp = {k: film110.patch(v, 480 / L) for k, v in pics.items()}
    old = [(k, 3.5 + (k - 1) * pitch, 0.0, L, pp[k]) for k in (1, 2, 3, 4)]
    rec = [(n, r) for n, r, *_ in old]
    # Slid by +10.6 mm: same pictures, same orientation.
    new = [(3.5 + 10.6 + (k - 1) * pitch, 0.0, L, pp[k]) for k in (1, 2, 3, 4)]
    dec = film110.decide_orientation(new, old)
    assert dec["orientation"] == "same", dec
    assert abs(dec["key"] - 10.6) < 1e-6 and dec["corr"] > 0.99, dec
    # Slid by a whole aperture and a bit (38 mm): positions alone would read
    # it as 12.5 mm (one pitch less) and number every image one too high;
    # the pictures say 38.
    far = [(3.5 + 38.0 + (k - 1) * pitch, 0.0, L, pp[k]) for k in (1, 2, 3, 4)]
    dec = film110.decide_orientation(far, old)
    assert dec["orientation"] == "same" and abs(dec["key"] - 38.0) < 1e-6, dec
    num, _r = film110.number_by_shift(far[0][0], dec["key"], rec)
    assert num == 1, num
    # Moved back out by more than half a pitch: the same.
    back = [(3.5 - 14.0 + (k - 1) * pitch, 0.0, L, pp[k]) for k in (2, 3, 4)]
    dec = film110.decide_orientation(back, old)
    assert dec["orientation"] == "same" and abs(dec["key"] + 14.0) < 1e-6, dec
    # A split sighting pairs on the part it shows: image 2 seen from 0 to
    # 9 mm (a trailing split) against the whole recorded image 2.
    n2 = int(round(pp[2].shape[0] * 9.0 / L))      # patch rows, not picture rows
    part = [(3.5 + 10.6 + pitch, 0.0, 9.0, pp[2][:n2])]
    dec = film110.decide_orientation(part, old)
    assert dec["orientation"] == "same" and abs(dec["key"] - 10.6) < 1e-6, dec
    assert dec["corr"] > 0.95, dec
    # Too little overlap (4 mm): no pair, no decision.
    n4 = int(round(pp[2].shape[0] * 4.0 / L))
    assert film110.decide_orientation([(3.5 + 10.6 + pitch, 0.0, 4.0, pp[2][:n4])], old) is None
    # Turned: reverse order, each picture rotated 180 degrees; the photograph
    # that started at r now starts at C - r (C = 100 here).
    turned = [(100.0 - old[k - 1][1], 0.0, L, pp[k][::-1, ::-1]) for k in (1, 2, 3, 4)]
    dec = film110.decide_orientation(turned, old)
    assert dec["orientation"] == "turned" and dec["corr"] > 0.99, dec
    num, res = film110.number_turned(100.0 - old[1][1], dec["key"], rec)
    assert num == 2 and abs(res) < 1e-6, (num, res)
    # A turned strip's leading split shows the END of the picture (its
    # last 9 mm) rotated: paired on that part.
    tail = pp[3][pp[3].shape[0] - n2:][::-1, ::-1]
    dec = film110.decide_orientation([(100.0 - old[2][1], 0.0, 9.0, tail)], old)
    assert dec["orientation"] == "turned" and dec["corr"] > 0.95, dec
    # Other pictures altogether: cannot tell.
    other = [(3.5 + 10.6 + (k - 1) * pitch, 0.0, L, film110.patch(picture(50 + k), 480 / L))
             for k in (1, 2, 3, 4)]
    assert film110.decide_orientation(other, old) is None
    # Nothing on one side: no decision (never geometry alone).
    assert film110.decide_orientation([], old) is None
    assert film110.decide_orientation(new, []) is None
    # Physically impossible moves are pruned: with the slot ending at 60 mm
    # the +38 mm slide cannot be, so the pictures must say something else.
    assert film110.decide_orientation(far, old, pos_range=(-17.2, 60.0)) is None
    # A free end seen in both runs pins the slide.
    assert film110.decide_orientation(new, old, end_shift=10.0)["orientation"] == "same"
    assert film110.decide_orientation(new, old, end_shift=20.0) is None
    print("test_thumbnail_and_decide_orientation OK")


def test_bar_centering_shifts_put_the_bar_in_the_gap():
    bars = film110.bar_spans_mm(1, 1)
    # The holder's end wall before aperture 1 counts as a bar (one bar
    # wide), then the crossbar after each aperture.
    assert bars[0] == (-(STRIP.pitch_mm - STRIP.aperture_mm), 0.0), bars[0]
    assert abs(bars[1][0] - STRIP.aperture_mm) < 1e-9
    assert film110.bar_spans_mm(2, 2)[0][0] > 100.0        # slot 2: no end wall
    # Image 1 seated at the end wall (0.0 mm, Test 104): a move of about
    # a gap's half plus half a bar further in frees it.
    c0 = film110.bar_centering_shifts(0.0, bars)
    s0 = min((c for c in c0 if c > 0), key=abs)
    assert 2.5 < s0 < 4.5, (s0, c0)
    # Image 2 of placement A (29.0 .. 46.2 mm) is split by the first bar.
    cands = film110.bar_centering_shifts(29.0, bars)
    assert len(cands) == 2, cands
    s = min(cands, key=abs)
    centre = (bars[1][0] + bars[1][1]) / 2
    gap = FILM_110.pitch_mm - FILM_110.image_mm[0]
    new_start = 29.0 + s
    # The bar's centre ends up in the middle of a gap beside the image.
    before = new_start - gap / 2
    after = new_start + FILM_110.image_mm[0] + gap / 2
    assert min(abs(before - centre), abs(after - centre)) < 1e-6, (s, new_start)
    assert abs(s - 12.2) < 0.2, s
    # No image under a bar: nothing to centre.
    assert film110.bar_centering_shifts(10.0, bars) == []
    print("test_bar_centering_shifts_put_the_bar_in_the_gap OK")


def test_cli_scan_parser_defaults_to_135():
    parser = cli.build_parser()
    args = parser.parse_args(["scan", "-o", "/tmp/x.tiff"])
    assert args.film == "135", args.film
    args2 = parser.parse_args(["scan", "--film", "110", "-o", "/tmp/x.tiff"])
    assert args2.film == "110", args2.film
    print("test_cli_scan_parser_defaults_to_135 OK")


# ---------------------------------------------------- frame locator (no holes)


def _frame_fixture(dpi, *, holes=(), n_images=2, aperture_length_mm=52.0, first_mm=1.0, **kw):
    """printed_border fixture with `n_images` pictures one pitch apart from
    1.0 mm; returns (arr, ppm, band_lo, band_hi, images_mm)."""
    L = FILM_110.image_mm[0]
    c0 = 2.0 + FILM_110.image_lateral_offset_mm
    c1 = c0 + FILM_110.image_mm[1]
    imgs = [(first_mm + k * FILM_110.pitch_mm, first_mm + k * FILM_110.pitch_mm + L, c0, c1)
            for k in range(n_images)]
    arr, ppm, lo, hi = _build_fixture(dpi, holes_mm=holes, images_mm=imgs,
                                      printed_border=True,
                                      aperture_length_mm=aperture_length_mm, **kw)
    return arr, ppm, lo, hi, imgs


def test_default_fixture_is_byte_stable():
    import hashlib
    arr, _p, _l, _h = _build_fixture(
        600, holes_mm=[(3.0, 4.5)], images_mm=[(6.8, 24.0, 3.5, 16.5)])
    digest = hashlib.sha256(arr.tobytes()).hexdigest()
    assert digest == DEFAULT_FIXTURE_SHA256, digest
    print("test_default_fixture_is_byte_stable OK")


def test_frame_locator_finds_images_without_holes():
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs = _frame_fixture(dpi, aperture_length_mm=36.0)
        find = film110.detect(arr, dpi=dpi)
        assert find.reason == "", (dpi, find.reason)
        assert len(find.frames) == 2, (dpi, find.frames)
        for fr, (l0, l1, c0, c1) in zip(find.frames, imgs):
            assert fr.located_by == "frame", fr.located_by
            assert fr.perforation_line is None and fr.orientation == "unknown"
            for got, want in ((fr.line0, l0), (fr.line1, l1), (fr.col0, c0), (fr.col1, c1)):
                assert abs(got / ppm - want) <= 0.3, (dpi, got / ppm, want)
        assert find.frames[0].whole and find.frames[0].split is None
        assert not find.frames[1].whole and find.frames[1].split == "trailing"
        assert [f.index for f in find.frames] == [1, 2]
        print(f"test_frame_locator_finds_images_without_holes OK ({dpi} dpi)")


def test_frame_locator_leaves_hole_frames_unchanged():
    def key(f):
        d = dict(f.__dict__)
        d.pop("located_by")
        return d
    h1 = (3.0, 4.5)
    h2 = (h1[0] + FILM_110.pitch_mm, h1[1] + FILM_110.pitch_mm)
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs = _frame_fixture(
            dpi, holes=[h1, h2], first_mm=h1[1] + FILM_110.perforation_lead_mm)
        # the hole path predicts images at hole end + lead; shift the painted
        # pictures there so both locators see the same strip
        film110.FRAME_LOCATOR_ENABLED = False
        try:
            off = film110.detect(arr, dpi=dpi)
        finally:
            film110.FRAME_LOCATOR_ENABLED = True
        on = film110.detect(arr, dpi=dpi)
        assert off.frames, dpi
        assert all(f.located_by == "holes" for f in off.frames)
        hole_part = [f for f in on.frames if f.located_by in ("holes", "holes+frame")]
        assert [key(f) for f in hole_part] == [key(f) for f in off.frames], dpi
        assert any(f.located_by == "holes+frame" for f in on.frames), dpi
        print(f"test_frame_locator_leaves_hole_frames_unchanged OK ({dpi} dpi)")


def test_frame_locator_finds_the_image_after_a_cut_hole():
    h1 = (3.0, 4.5)
    for dpi in DPIS:
        ilen = FILM_110.image_mm[0]
        L = ilen
        c0 = 2.0 + FILM_110.image_lateral_offset_mm
        c1 = c0 + FILM_110.image_mm[1]
        i1 = h1[1] + FILM_110.perforation_lead_mm
        i2 = i1 + FILM_110.pitch_mm
        arr, ppm, lo, hi = _build_fixture(
            dpi, holes_mm=[h1], images_mm=[(i1, i1 + L, c0, c1), (i2, i2 + L, c0, c1)],
            printed_border=True, aperture_length_mm=52.0, free_end_mm=i2 + L + 0.5)
        find = film110.detect(arr, dpi=dpi)
        by = {round(f.line0 / ppm): f for f in find.frames}
        assert any(abs(f.line0 / ppm - i2) <= 0.3 and f.located_by == "frame"
                   for f in find.frames), (dpi, [(f.located_by, f.line0 / ppm) for f in find.frames])
        assert any(abs(f.line0 / ppm - i1) <= 0.3 and f.located_by == "holes+frame"
                   for f in find.frames), dpi
        print(f"test_frame_locator_finds_the_image_after_a_cut_hole OK ({dpi} dpi)")


def test_frame_locator_ignores_sky_and_dense_leader():
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs = _frame_fixture(dpi, n_images=2)
        # texture picture 1 (so it is not two flat halves) with a 4 mm flat sky
        a = arr.astype(np.float64)
        rim = int(round(2.5 * ppm))
        l0, l1 = int(imgs[0][0] * ppm), int(imgs[0][1] * ppm)
        n = l1 - l0
        tex = 0.30 + 0.12 * np.sin(np.arange(n) / ppm * 5.0)
        sky0 = int(5.0 * ppm)
        sky1 = sky0 + int(4.0 * ppm)
        tex[sky0:sky1] = 0.11 * AIR / CLEAR["G"]
        for ci, key in enumerate(CHANNELS):
            a[l0:l1, lo + rim:hi - rim, ci] = (tex * CLEAR[key])[:, None]
        arr2 = np.clip(a, 0, 65535).astype(np.uint16)
        gaps = film110._frame_gaps(np.median(arr2[:, lo + rim:hi - rim, 1].astype(np.float64), axis=1) / AIR,
                                   np.ones(arr2.shape[0], dtype=bool), ppm, 17.2, arr2.shape[0])
        for g0, g1, t0, t1 in gaps:
            assert not (l0 <= g0 and g1 <= l1), ("sky taken as a gap", g0 / ppm, g1 / ppm)
        find = film110.detect(arr2, dpi=dpi)
        assert len(find.frames) == 2, (dpi, [(f.line0 / ppm, f.line1 / ppm) for f in find.frames])
        # a dense leader (0.03 x air, flat, 11 mm) is never a gap
        b = arr.astype(np.float64)
        for ci, key in enumerate(CHANNELS):
            b[int(20.0 * ppm):int(31.0 * ppm), lo + rim:hi - rim, ci] = 0.03 * AIR * CLEAR[key] / CLEAR["G"]
        arr3 = np.clip(b, 0, 65535).astype(np.uint16)
        prof = np.median(arr3[:, lo + rim:hi - rim, 1].astype(np.float64), axis=1) / AIR
        gaps = film110._frame_gaps(prof, np.ones(len(prof), dtype=bool), ppm, 17.2, len(prof))
        for g0, g1, t0, t1 in gaps:
            assert not (19.5 * ppm <= g0 and g1 <= 31.5 * ppm), ("leader taken as a gap", g0 / ppm, g1 / ppm)
        print(f"test_frame_locator_ignores_sky_and_dense_leader OK ({dpi} dpi)")


def test_frame_locator_with_the_band_hidden_under_rails():
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs = _frame_fixture(dpi)
        pad = int(round(4.0 * ppm))
        arr[:, :lo, :] = int(PLASTIC)
        # plastic both sides of the band, air only beyond the far rail
        arr = np.concatenate([np.full((arr.shape[0], pad, 3), int(PLASTIC), dtype=arr.dtype), arr,
                              np.full((arr.shape[0], pad, 3), int(AIR), dtype=arr.dtype)], axis=1)
        find = film110.detect(arr, dpi=dpi)
        assert find.reason == "", (dpi, find.reason)
        assert len(find.frames) == 2, (dpi, find.frames)
        for fr, (l0, l1, c0, c1) in zip(find.frames, imgs):
            assert fr.located_by == "frame"
            assert abs(fr.line0 / ppm - l0) <= 0.3 and abs(fr.line1 / ppm - l1) <= 0.3
        print(f"test_frame_locator_with_the_band_hidden_under_rails OK ({dpi} dpi)")



def _blank_fixture(dpi, **kw):
    """Pictures one and three of a strip with an unexposed (clear film)
    frame position between them; returns (arr, ppm, lo, hi, imgs, window)."""
    L = FILM_110.image_mm[0]
    arr, ppm, lo, hi, imgs = _frame_fixture(dpi, n_images=3, aperture_length_mm=78.0, **kw)
    window = imgs[1][:2]
    a = arr.astype(np.float64)
    l0, l1 = int(round(window[0] * ppm)), int(round(window[1] * ppm))
    for ci, key in enumerate(CHANNELS):
        a[l0:l1, lo + int(round(0.6 * ppm)):hi, ci] = 0.98 * CLEAR[key]
    return np.clip(a, 0, 65535).astype(np.uint16), ppm, lo, hi, [imgs[0], imgs[2]], window


def test_blank_window_between_gaps_is_reported_and_is_no_image():
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs, window = _blank_fixture(dpi)
        find = film110.detect(arr, dpi=dpi)
        assert find.reason == "", (dpi, find.reason)
        assert len(find.frames) == 2, (dpi, [(f.line0 / ppm, f.line1 / ppm) for f in find.frames])
        assert len(find.blanks) == 1, (dpi, find.blanks)
        b0, b1 = find.blanks[0]
        assert abs(b0 / ppm - window[0]) <= 0.4 and abs(b1 / ppm - window[1]) <= 0.4, (dpi, b0 / ppm, b1 / ppm, window)
        assert all(abs(f.line0 / ppm - w[0]) <= 0.3 for f, w in zip(find.frames, imgs))
        print(f"test_blank_window_between_gaps_is_reported_and_is_no_image OK ({dpi} dpi)")


def _thin_window(arr, ppm, lo, hi, window, amp, ramp=0.0):
    """Paint the blank fixture's window as a thin picture: clear base x
    (0.93 + amp x sin x sin) over 3 x 4 mm, plus a lengthwise `ramp`
    (fog). The per-line median over the band stays ~flat -- the along-
    transport profile cannot tell it from clear film (Test 108)."""
    a = arr.astype(np.float64)
    l0, l1 = int(round(window[0] * ppm)), int(round(window[1] * ppm))
    c0 = lo + int(round(0.6 * ppm))
    rr, cc = np.mgrid[l0:l1, c0:hi]
    pat = 0.93 + amp * np.sin(2 * np.pi * cc / (3.0 * ppm)) * np.sin(2 * np.pi * rr / (4.0 * ppm))
    pat = pat + ramp * (rr - l0) / max(1, l1 - l0)
    for ci, key in enumerate(CHANNELS):
        a[l0:l1, c0:hi, ci] = CLEAR[key] * pat
    return np.clip(a, 0, 65535).astype(np.uint16)


def test_thin_low_contrast_picture_is_an_image_not_a_blank():
    """Test 108: an underexposed photograph reads as clear film along
    transport; texture across the window makes it an image."""
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs, window = _blank_fixture(dpi)
        a = _thin_window(arr, ppm, lo, hi, window, amp=0.12)
        find = film110.detect(a, dpi=dpi)
        assert find.blanks == [], (dpi, find.blanks)
        assert len(find.frames) == 3, (dpi, [(f.line0 / ppm, f.line1 / ppm) for f in find.frames])
        mid = sorted(find.frames, key=lambda f: f.line0)[1]
        assert abs(mid.line0 / ppm - window[0]) <= 0.4, (dpi, mid.line0 / ppm, window)
        print(f"test_thin_low_contrast_picture_is_an_image_not_a_blank OK ({dpi} dpi)")


def test_fogged_blank_with_a_gradient_stays_a_blank():
    """A plane (fog gradient) is not texture: the window stays a blank."""
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs, window = _blank_fixture(dpi)
        a = _thin_window(arr, ppm, lo, hi, window, amp=0.0, ramp=0.05)
        find = film110.detect(a, dpi=dpi)
        assert len(find.blanks) == 1 and len(find.frames) == 2, (dpi, find.blanks, len(find.frames))
        print(f"test_fogged_blank_with_a_gradient_stays_a_blank OK ({dpi} dpi)")


def test_blank_is_not_reported_where_the_film_ends_inside_the_window():
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs, window = _blank_fixture(dpi)
        a = arr.copy()
        a[int(round((window[0] + 8.0) * ppm)):, lo:hi, :] = int(AIR)    # the film ends inside the window
        find = film110.detect(a, dpi=dpi)
        assert find.blanks == [], (dpi, find.blanks)
        print(f"test_blank_is_not_reported_where_the_film_ends_inside_the_window OK ({dpi} dpi)")


def test_leader_beyond_the_free_start_is_not_a_blank_and_pictures_have_none():
    for dpi in DPIS:
        # a clear leader before the first gap, film starting inside the aperture
        arr, ppm, lo, hi, imgs = _frame_fixture(dpi, n_images=2, first_mm=20.0, aperture_length_mm=70.0)
        a = arr.copy()
        a[:int(round(4.0 * ppm)), lo:hi, :] = int(AIR)                    # the film begins at 4 mm
        for ci, key in enumerate(CHANNELS):
            a[int(round(4.0 * ppm)):int(round(10.0 * ppm)), lo:hi, ci] = int(0.98 * CLEAR[key])
        find = film110.detect(a, dpi=dpi)
        assert find.blanks == [], (dpi, find.blanks)
        # and a plain strip of pictures reports none either
        plain = film110.detect(_frame_fixture(dpi, n_images=2)[0], dpi=dpi)
        assert plain.blanks == []
        print(f"test_leader_beyond_the_free_start_is_not_a_blank_and_pictures_have_none OK ({dpi} dpi)")



def test_gap_survives_a_thin_scratch_and_dust():
    """(a) A 0.05 mm bright line and a few specks across a gap must not
    shorten it: the gap's ends stay on the real level step."""
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs = _frame_fixture(dpi, n_images=2, aperture_length_mm=52.0)
        clean = film110.detect(arr, dpi=dpi)
        g0, g1 = imgs[0][1], imgs[1][0]                 # the gap between the pictures
        a = arr.astype(np.float64)
        rim = int(round(0.6 * ppm))
        marks = [(g0 + 3.2, 0.05), (g0 + 1.1, 0.03), (g0 + 5.6, 0.03), (g0 + 6.5, 0.02)]
        for pos, height in marks:
            r0 = int(round(pos * ppm))
            r1 = max(r0 + 1, int(round((pos + height) * ppm)))
            for ci, key in enumerate(CHANNELS):
                a[r0:r1, lo + rim:hi, ci] = 0.9 * CLEAR[key]
        scratched = np.clip(a, 0, 65535).astype(np.uint16)
        prof = np.median(scratched[:, lo + int(2.5 * ppm):hi - int(2.5 * ppm), 1].astype(np.float64), axis=1) / AIR
        gaps = film110._frame_gaps(prof, np.ones(len(prof), dtype=bool), ppm, 17.2, len(prof))
        assert any(abs(x / ppm - g0) <= 0.4 and abs(y / ppm - g1) <= 0.4 for x, y, _t0, _t1 in gaps), \
            (dpi, [(x / ppm, y / ppm) for x, y, _a, _b in gaps], g0, g1)
        find = film110.detect(scratched, dpi=dpi)
        assert len(find.frames) == len(clean.frames) == 2, (dpi, find.frames)
        for f, c in zip(find.frames, clean.frames):
            assert abs(f.line0 - c.line0) <= 0.3 * ppm and abs(f.line1 - c.line1) <= 0.3 * ppm
        print(f"test_gap_survives_a_thin_scratch_and_dust OK ({dpi} dpi)")


def test_blank_frame_is_not_an_image_when_a_prediction_runs_into_the_gap():
    """(b) A hole predicts a window that starts 1.5 mm inside the gap before
    a clear (blank) frame. The dark gap lines must not make the clear window
    look like a picture: it is a blank, never an image."""
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs, window = _blank_fixture(dpi)
        a = arr.copy()
        lead = FILM_110.perforation_lead_mm
        he = window[0] - 1.5 - lead
        hs = he - 2.6
        rim = int(round(0.6 * ppm))
        hole_w = int(round(1.5 * ppm))
        a[int(round(hs * ppm)):int(round(he * ppm)), lo + rim:lo + rim + hole_w, :] = int(AIR)
        find = film110.detect(a, dpi=dpi)
        assert all(not (window[0] - 3.0 <= f.line0 / ppm <= window[0] + 3.0) for f in find.frames), \
            (dpi, [(f.line0 / ppm, f.located_by) for f in find.frames])
        assert len(find.frames) == 2, (dpi, [(f.line0 / ppm, f.line1 / ppm, f.located_by) for f in find.frames])
        assert len(find.blanks) == 1 and abs(find.blanks[0][0] / ppm - window[0]) <= 0.6, (dpi, find.blanks)
        print(f"test_blank_frame_is_not_an_image_when_a_prediction_runs_into_the_gap OK ({dpi} dpi)")



def test_clear_edge_rim_speckle_is_not_a_hole():
    """Test 107: a strip whose unexposed edge rim reads close to air level
    puts air-level pixels into the hole zone on almost every line. A hole
    is air ACROSS the zone (the fixture's holes: 1.5 of the zone's 2.7 mm,
    like the real 0.55), so speckle never makes a hole and the real holes
    are still found, not lost in one long run."""
    lead = FILM_110.perforation_lead_mm
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs = _frame_fixture(
            dpi, holes=[(1.0 + k * FILM_110.pitch_mm - lead - 1.5,
                         1.0 + k * FILM_110.pitch_mm - lead) for k in (1,)],
            n_images=2)
        clean = film110.detect(arr, dpi=dpi)
        rng = np.random.default_rng(7)
        a = arr.copy()
        z0, z1 = lo + int(round(0.3 * ppm)), lo + int(round(3.0 * ppm))
        speck = rng.random((a.shape[0], z1 - z0)) < 0.03
        zone = a[:, z0:z1, :]
        zone[speck] = 65535
        find = film110.detect(a, dpi=dpi)
        assert find.reason == "", (dpi, find.reason)
        got = [(round(f.line0 / ppm, 1), f.whole, f.split) for f in find.frames]
        want = [(round(f.line0 / ppm, 1), f.whole, f.split) for f in clean.frames]
        assert len(got) == len(want) and all(
            abs(g[0] - w[0]) <= 0.3 and g[1:] == w[1:] for g, w in zip(got, want)), (dpi, got, want)
        assert all("holes" in f.located_by for f in find.frames), [f.located_by for f in find.frames]
        print(f"test_clear_edge_rim_speckle_is_not_a_hole OK ({dpi} dpi)")


def test_gap_is_not_extended_past_its_maximum_into_a_dense_picture_edge():
    """Test 107: a dense, textured picture region right after the printed
    border (a white fence, 0.145-0.16 against the border's 0.122 x air)
    lies inside the extension tolerance; the extension made the 8.3 mm gap
    too long and the gap was dropped, so the image after it was not
    located. An extension past FRAME_GAP_MAX_MM is undone."""
    for dpi in DPIS:
        arr, ppm, lo, hi, imgs = _frame_fixture(dpi, n_images=2)
        a = arr.astype(np.float64)
        rim = int(round(0.6 * ppm))
        f0, f1 = int(round(imgs[1][0] * ppm)), int(round((imgs[1][0] + 2.5) * ppm))
        u = np.arange(f1 - f0) / ppm
        tex = 0.50 * (1.0 + 0.06 * np.sin(2 * np.pi * u))
        for ci, key in enumerate(CHANNELS):
            a[f0:f1, lo + rim:hi, ci] = (tex * CLEAR[key])[:, None]
        arr2 = np.clip(a, 0, 65535).astype(np.uint16)
        find = film110.detect(arr2, dpi=dpi)
        assert len(find.frames) == 2, (dpi, [(f.line0 / ppm, f.line1 / ppm) for f in find.frames])
        fr = find.frames[1]
        assert fr.located_by == "frame" and abs(fr.line0 / ppm - imgs[1][0]) <= 0.3, (dpi, fr.line0 / ppm)
        print(f"test_gap_is_not_extended_past_its_maximum_into_a_dense_picture_edge OK ({dpi} dpi)")


def test_orientation_is_decided_by_evidence_over_near_duplicate_shots():
    """Test 106 pass 3: the true slide (22.4 mm) has four distinct new
    sightings agreeing (0.91-0.96); a competing slide (-3.0 mm, one pitch
    off) has 0.84, 0.71, 0.10 because the strip holds near-duplicate shots.
    Best pair 0.96 - 0.84 = 0.12 < ORIENT_MARGIN, yet the evidence (3.75
    against 1.65) decides. One new sighting alone does not."""
    old_p = [0.0, 25.5, 51.0, 76.5]
    new_p = [r + 22.4 for r in old_p]
    true = {(0, 0): 0.91, (1, 1): 0.93, (2, 2): 0.95, (3, 3): 0.96}
    other = {(0, 1): 0.84, (1, 2): 0.71, (2, 3): 0.10}
    table = {**true, **other}

    def stub(item, oitem, kind, film=FILM_110):
        if kind != "same":
            return None
        return table.get((item[3], oitem[4]))

    saved = film110.pair_correlation
    film110.pair_correlation = stub
    try:
        new = [(p, 0.0, 17.2, i) for i, p in enumerate(new_p)]
        old = [(j + 1, r, 0.0, 17.2, j) for j, r in enumerate(old_p)]
        dec = film110.decide_orientation(new, old)
        assert dec is not None and dec["orientation"] == "same", dec
        assert abs(dec["key"] - 22.4) <= 1.0, dec
        assert dec["evidence"] - dec["runner_up_evidence"] >= film110.ORIENT_EVIDENCE_MARGIN, dec
        assert dec["corr"] - dec["other"] < film110.ORIENT_MARGIN, dec     # the old rule would refuse
        # a member that correlates with nothing adds nothing and vetoes nothing
        table[(2, 2)] = -0.19
        dec = film110.decide_orientation(new, old)
        assert dec is not None and abs(dec["key"] - 22.4) <= 1.0, dec
        table[(2, 2)] = 0.95
        # ONE new sighting: 0.96 against 0.84 is still not enough
        table.clear()
        table.update({(0, 0): 0.96, (0, 1): 0.84})
        dec = film110.decide_orientation(new[:1], old)
        assert dec is None, dec
    finally:
        film110.pair_correlation = saved
    print("test_orientation_is_decided_by_evidence_over_near_duplicate_shots OK")


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
        test_infer_placement_on_the_documented_phases,
        test_cli_hook_infers_a_and_b,
        test_cli_hook_refuses_the_ambiguous_position_then_decides_later,
        test_explicit_placement_overrides_inference,
        test_cli_hook_is_noop_for_film_135,
        test_number_by_shift,
        test_thumbnail_and_decide_orientation,
        test_bar_centering_shifts_put_the_bar_in_the_gap,
        test_cli_scan_parser_defaults_to_135,
        test_validate_film110_args,
        test_default_fixture_is_byte_stable,
        test_frame_locator_finds_images_without_holes,
        test_frame_locator_leaves_hole_frames_unchanged,
        test_frame_locator_finds_the_image_after_a_cut_hole,
        test_frame_locator_ignores_sky_and_dense_leader,
        test_frame_locator_with_the_band_hidden_under_rails,
        test_blank_window_between_gaps_is_reported_and_is_no_image,
        test_blank_is_not_reported_where_the_film_ends_inside_the_window,
        test_thin_low_contrast_picture_is_an_image_not_a_blank,
        test_fogged_blank_with_a_gradient_stays_a_blank,
        test_leader_beyond_the_free_start_is_not_a_blank_and_pictures_have_none,
        test_gap_survives_a_thin_scratch_and_dust,
        test_blank_frame_is_not_an_image_when_a_prediction_runs_into_the_gap,
        test_orientation_is_decided_by_evidence_over_near_duplicate_shots,
        test_clear_edge_rim_speckle_is_not_a_hole,
        test_gap_is_not_extended_past_its_maximum_into_a_dense_picture_edge,
    ]
    passed = 0
    for t in tests:
        t()
        passed += 1
    print(f"\n{passed} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
