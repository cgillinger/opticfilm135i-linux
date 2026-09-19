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
from of135i.holder import FILM_110

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
                   free_end_mm=None, seed=0, noise=120.0):
    """A synthetic aperture-registered crop: air on one side of the
    aperture width, a plastic rail on the other, and a `film.width_mm`
    band of clear-base film between them (with the perforated edge on
    `perforated_side`, "lo" or "hi").

    `holes_mm`: [(start, end), ...] air-level rectangles at the film's
    perforated edge, within the edge search margin.
    `images_mm`: [(line0, line1, col0, col1), ...] image rectangles; each
    is split into an upper "sky" half (very dense, 15% of the green base)
    and a lower "shadow" half (thin, 85% of the base), all three channels
    scaled the same way off their own clear-base level.
    `leading_dense_to_mm`: if given, the WHOLE film band from line 0 to
    this position is filled at a dense (continuing-image-like) level, to
    build a leading_continuation fixture.
    `free_end_mm`: if given, the film band (including its edge zone)
    reverts to air beyond this position -- a strip end inside the
    aperture.
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
    for (s_mm, e_mm) in holes_mm:
        s, e = int(round(s_mm * ppm)), int(round(e_mm * ppm))
        s, e = max(0, s), min(n_lines, e)
        if e <= s:
            continue
        if perforated_side == "lo":
            arr[s:e, band_lo + rim_px:band_lo + rim_px + hole_w_px, :] = AIR
        else:
            arr[s:e, band_hi - rim_px - hole_w_px:band_hi - rim_px, :] = AIR

    for (l0_mm, l1_mm, c0_mm, c1_mm) in images_mm:
        l0, l1 = int(round(l0_mm * ppm)), int(round(l1_mm * ppm))
        c0, c1 = int(round(c0_mm * ppm)), int(round(c1_mm * ppm))
        l0c, l1c = max(0, l0), min(n_lines, l1)
        c0c, c1c = max(0, c0), min(width, c1)
        if l1c <= l0c or c1c <= c0c:
            continue
        mid = (l0c + l1c) // 2
        for ci, key in enumerate(CHANNELS):
            base = CLEAR[key]
            arr[l0c:mid, c0c:c1c, ci] = base * 0.15   # sky: very dense
            arr[mid:l1c, c0c:c1c, ci] = base * 0.85   # shadow: thin

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


# --------------------------------------------------------------------- CLI


def _hook_args(dpi=600, positive=False, rotate=0):
    import types
    return types.SimpleNamespace(dpi=dpi, positive=positive, rotate=rotate)


def test_cli_hook_numbers_across_apertures_and_writes_whole_only():
    dpi = 600
    ppm = _px_per_mm(dpi)
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]

    h1 = (10.0, 11.5)
    img1 = h1[1] + lead
    h2 = (25.0, 26.5)
    img2 = h2[1] + lead
    ap1, _, _, _ = _build_fixture(
        dpi, holes_mm=[h1, h2],
        images_mm=[(img1, img1 + ilen, col0, col1),
                   (img2, img2 + ilen, col0, col1)], seed=1)

    h3 = (8.0, 9.5)
    img3 = h3[1] + lead
    ap2, _, _, _ = _build_fixture(
        dpi, holes_mm=[h3], leading_dense_to_mm=8.0,
        images_mm=[(img3, img3 + ilen, col0, col1)], seed=2)

    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi)
        state = cli.Film110State()
        cli._film110_hook(args, state, 1, out, ap1, None)
        cli._film110_hook(args, state, 2, out, ap2, None)

        # aperture 1: image 1 (whole) + image 2 (trailing split, not
        # written); aperture 2: image 3 (whole; the leading_continuation
        # itself produced no FrameFind, so it is not counted).
        assert Path(cli._film110_image_path(out, 1)).exists()
        assert not Path(cli._film110_image_path(out, 2)).exists()
        assert Path(cli._film110_image_path(out, 3)).exists()
        assert state.next_index == 4, state.next_index
    print("test_cli_hook_numbers_across_apertures_and_writes_whole_only OK")


def test_cli_hook_writes_ir_with_same_indices():
    dpi = 600
    ppm = _px_per_mm(dpi)
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    hole = (10.0, 11.5)
    img = hole[1] + lead
    vis, _, _, _ = _build_fixture(
        dpi, holes_mm=[hole], images_mm=[(img, img + ilen, col0, col1)])
    ir = vis[:, :, 1].copy()  # a stand-in single-channel IR frame

    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi)
        state = cli.Film110State()
        cli._film110_hook(args, state, 1, out, vis, ir)
        assert Path(cli._film110_image_path(out, 1)).exists()
        assert Path(cli._film110_image_path(out, 1, ir=True)).exists()
    print("test_cli_hook_writes_ir_with_same_indices OK")


def test_cli_hook_numbering_note_keyed_on_first_scanned_aperture():
    """The 'numbering is relative' note fires only when the FIRST
    aperture this command scans is not 1 -- not merely whenever the
    current aperture differs from 1 (a bug caught by this test: a
    two-aperture run starting at 1 must never print the note)."""
    import io
    import contextlib

    dpi = 600
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    hole = (10.0, 11.5)
    img = hole[1] + lead
    ap, _, _, _ = _build_fixture(
        dpi, holes_mm=[hole], images_mm=[(img, img + ilen, col0, col1)])

    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "f.tiff")
        args = _hook_args(dpi=dpi)

        buf = io.StringIO()
        state = cli.Film110State()
        with contextlib.redirect_stdout(buf):
            cli._film110_hook(args, state, 1, out, ap, None)
            cli._film110_hook(args, state, 2, out, ap, None)
        assert "numbering is relative" not in buf.getvalue(), buf.getvalue()

        buf2 = io.StringIO()
        state2 = cli.Film110State()
        with contextlib.redirect_stdout(buf2):
            cli._film110_hook(args, state2, 3, out, ap, None)
        assert "numbering is relative to the first scanned aperture" in buf2.getvalue()
    print("test_cli_hook_numbering_note_keyed_on_first_scanned_aperture OK")


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
        test_cli_hook_numbers_across_apertures_and_writes_whole_only,
        test_cli_hook_numbering_note_keyed_on_first_scanned_aperture,
        test_cli_hook_writes_ir_with_same_indices,
        test_cli_hook_is_noop_for_film_135,
        test_cli_scan_parser_defaults_to_135,
    ]
    passed = 0
    for t in tests:
        t()
        passed += 1
    print(f"\n{passed} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
