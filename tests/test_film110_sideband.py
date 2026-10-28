#!/usr/bin/env python3
"""Offline tests for the 110 side-band coverage fallback
(aperture_crop.measure_coverage_side_band, docs/film-110.md §10): on the
plain path a dense 110 strip can hide the aperture edge from the central-
band verdict; the fallback measures the bare air beside the strip and
accepts only the holder's own opening. Synthetic neutral (R=G=B) frames,
no hardware.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_film110_sideband.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np

from of135i import aperture_crop, cli, digitize

AIR = 65535
PLASTIC = 500
CLEARF = 30000        # clear film base, neutral
DENSE = 3000          # a dense picture: reads as plastic-like to the edge finder
APT = 36.0
PAD = 3.0


def _frame(dpi, width_mm=24.0, *, air=((0.0, 2.0),), film=(2.0, 18.0),
           dense=(), film_dark=False, air_lines=None, shift=None, seed=1):
    """A frame: PAD mm of plastic, the APT mm opening, PAD mm of plastic.
    Columns in `air` (mm ranges) are bare air over the opening
    (`air_lines` = (start, end) mm restricts them; `shift` = {range
    index: mm} moves a range's opening along the strip); `film` is a
    clear-film band with `dense` = [(line0, line1) mm in the opening]
    painted dense; `film_dark` makes the whole band opaque. The rest is
    rail (plastic)."""
    ppm = dpi / 25.4
    pad = int(round(PAD * ppm))
    n_apt = int(round(APT * ppm))
    w = int(round(width_mm * ppm))
    arr = np.full((n_apt + 2 * pad, w), PLASTIC, dtype=np.float64)
    c = lambda mm: int(round(mm * ppm))
    if film is not None:
        f0, f1 = c(film[0]), c(film[1])
        arr[pad:pad + n_apt, f0:f1] = PLASTIC if film_dark else CLEARF
        if not film_dark:
            for a, b in dense:
                arr[pad + c(a):pad + c(b), f0:f1] = DENSE
    for i, (a, b) in enumerate(air):
        l0, l1 = (0.0, APT) if air_lines is None else air_lines
        if shift and i in shift:
            l0, l1 = l0 + shift[i], l1 + shift[i]
        arr[pad + c(l0):pad + c(l1), c(a):c(b)] = AIR
    arr += np.random.default_rng(seed).normal(0, 100, arr.shape)
    arr = np.clip(arr, 0, 65535).astype(np.uint16)
    return np.repeat(arr[:, :, None], 3, axis=2)


def _true_edges(dpi):
    ppm = dpi / 25.4
    pad = int(round(PAD * ppm))
    return pad, pad + int(round(APT * ppm)) - 1


def test_dense_film_over_an_edge_in_the_centre_band():
    dpi = 600
    img = _frame(dpi, dense=[(0.0, 14.0)])
    normal = aperture_crop.measure_coverage(img, dpi=dpi)
    assert not normal.verified, normal
    side = aperture_crop.measure_coverage_side_band(img, dpi=dpi)
    assert side.verified and side.source == "visible-side", side
    lead, trail = _true_edges(dpi)
    assert abs(side.leading_line - lead) < 3 and abs(side.trailing_line - trail) < 3, (side, lead, trail)
    assert "columns" in side.reason, side.reason


def test_dense_images_splitting_the_centre_run():
    dpi = 600
    img = _frame(dpi, dense=[(0.0, 6.0), (12.0, 22.0)])
    assert not aperture_crop.measure_coverage(img, dpi=dpi).verified
    side = aperture_crop.measure_coverage_side_band(img, dpi=dpi)
    assert side.verified, side
    lead, trail = _true_edges(dpi)
    assert abs(side.leading_line - lead) < 3 and abs(side.trailing_line - trail) < 3


def test_a_shorter_lit_run_beside_the_strip_is_never_accepted():
    # The air columns are lit over 28 mm only (a strip end / opaque thing
    # covers the rest); edges are clean, the run is not the holder's opening.
    dpi = 600
    img = _frame(dpi, film_dark=True, air_lines=(4.0, 32.0))
    side = aperture_crop.measure_coverage_side_band(img, dpi=dpi)
    assert not side.verified and "wrong-length" in side.reason, side
    # ... and a run within the tolerance of the opening IS accepted.
    img2 = _frame(dpi, film_dark=True, air_lines=(0.3, 35.9))
    assert aperture_crop.measure_coverage_side_band(img2, dpi=dpi).verified


def test_a_strip_tip_over_some_bands_does_not_veto_the_bare_ones():
    """Test 109: a fogged strip end over part of an aperture's leading edge
    shortens the opening in the bands it covers (film only hides light).
    The longest-opening bands are the holder and decide; the shorter ones
    are left out, not counted as disagreement."""
    dpi = 600
    ppm = dpi / 25.4
    img = _frame(dpi, film_dark=True, air=((0.0, 4.5), (19.0, 22.0))).copy()
    pad = int(round(PAD * ppm))
    img[pad:pad + int(round(0.5 * ppm)), int(round(19.0 * ppm)):int(round(22.0 * ppm))] = PLASTIC
    side = aperture_crop.measure_coverage_side_band(img, dpi=dpi)
    assert side.verified, side
    lead, trail = _true_edges(dpi)
    assert abs(side.leading_line - lead) < 3, (side, lead)
    assert "shorter" in side.reason, side.reason


def test_bands_that_disagree_are_refused_not_chosen():
    dpi = 600
    img = _frame(dpi, film_dark=True,
                 air=((0.0, 2.0), (19.0, 22.0)), shift={1: 0.8})
    side = aperture_crop.measure_coverage_side_band(img, dpi=dpi)
    assert not side.verified and "disagree" in side.reason, side
    # Agreeing bands (no shift) are fine.
    ok = aperture_crop.measure_coverage_side_band(
        _frame(dpi, film_dark=True, air=((0.0, 2.0), (19.0, 22.0))), dpi=dpi)
    assert ok.verified, ok


def test_full_width_dense_film_without_air_is_refused():
    dpi = 600
    img = _frame(dpi, air=(), film=(0.0, 22.0), film_dark=True)
    assert not aperture_crop.measure_coverage(img, dpi=dpi).verified
    side = aperture_crop.measure_coverage_side_band(img, dpi=dpi)
    assert not side.verified and side.leading_line is None, side


def test_no_edge_is_inferred_from_the_expected_length():
    # The leading edge is off the image (clipped 1 mm inside the opening):
    # the band's edge is not measurable, so nothing is verified -- the
    # 36 mm is never used to place it.
    dpi = 600
    ppm = dpi / 25.4
    img = _frame(dpi, film_dark=True)
    cut = img[int(round((PAD + 1.0) * ppm)):]
    side = aperture_crop.measure_coverage_side_band(cut, dpi=dpi)
    assert not side.verified, side


def test_normal_verdict_is_untouched_and_default_is_the_central_band():
    dpi = 600
    img = _frame(dpi, film=None, air=((0.0, 24.0),))
    a = aperture_crop.measure_coverage(img, dpi=dpi)
    b = aperture_crop.measure_coverage(img, dpi=dpi, band=0.5)
    assert a == b and a.verified and a.source == "visible", a


# ------------------------------------------------------------ end to end

E2E_DPI = 3600
E2E_W = 20.0
TMP_BASE = HERE.parent / ".sideband-test-tmp"     # on disk, not tmpfs


def _digitize_plain(d, frame_img):
    """One `_cmd_digitize --film 110 --no-ir` run (plain 3600 path), a
    single aperture, fake scanner."""
    scans = []

    class _Fake:
        park_mode = "verbatim"
        warmup_budget_s = 60.0
        last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6],
                     "dark_b_substituted": False, "calibration": "fresh"}

        def check_start_state(self): pass
        def is_magazine_present(self): return True
        def initialize(self, ir, dpi): pass
        def scan(self, frame, ir=None, dpi=None):
            scans.append(frame)
            return frame_img.astype("<u2").tobytes(), frame_img.shape[1]
        def eject(self): scans.append("eject")

    args = argparse.Namespace(
        out=d, prefix="", roll=1, force=False, assume_loaded=True,
        dpi=E2E_DPI, positive=False, rotate=0, ir=False, no_clean=True,
        no_diag=False, park="verbatim", warmup_budget=None, frames="1",
        film="110", placement="A")
    orig = cli._run_writing_session
    cli._run_writing_session = lambda body: body(_Fake())
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli._cmd_digitize(args)
    finally:
        cli._run_writing_session = orig
    recs = digitize.read_manifest(d)
    return rc, (recs[-1] if recs else None), out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _disk_tmp():
    TMP_BASE.mkdir(exist_ok=True)
    d = tempfile.mkdtemp(prefix="e2e-", dir=str(TMP_BASE))
    try:
        yield d
    finally:
        shutil.rmtree(TMP_BASE, ignore_errors=True)


def _frame_entry(rec):
    return rec["per_frame"][0]


def test_end_to_end_plain_110_uses_the_side_band_and_writes_the_frame():
    with _disk_tmp() as d:
        img = _frame(E2E_DPI, E2E_W, dense=[(0.0, 14.0)])
        rc, rec, out, err = _digitize_plain(d, img)
        assert rec is not None, (rc, err)
        fe = _frame_entry(rec)
        assert fe["coverage_verified"] is True, (rc, fe, err)
        assert fe["coverage_source"] == "visible-side", fe
        assert "side bands" in fe["coverage_reason"], fe
        roll = digitize.roll_dir(d, "", 1)
        assert any(p.name.endswith(".overscan.tiff") for p in roll.iterdir())


def test_end_to_end_neither_verifies_is_the_usual_refusal_with_both_reasons():
    with _disk_tmp() as d:
        img = _frame(E2E_DPI, E2E_W, air=(), film=(0.0, 20.0), film_dark=True)
        rc, rec, out, err = _digitize_plain(d, img)
        assert rec is not None, (rc, err)
        fe = _frame_entry(rec)
        assert rc == 4, (rc, err)
        assert fe["coverage_verified"] is False, fe
        assert fe["coverage_source"] == "visible", fe
        assert "visible:" in fe["coverage_reason"] and "side bands" in fe["coverage_reason"], fe
        names = [p.name for p in digitize.roll_dir(d, "", 1).iterdir()]
        assert any(n.endswith(".overscan.tiff") for n in names), names
        assert not any(n.startswith("image") for n in names), names


def test_end_to_end_a_frame_that_verifies_today_is_identical():
    with _disk_tmp() as d:
        img = _frame(E2E_DPI, E2E_W)
        rc, rec, out, err = _digitize_plain(d, img)
        assert rec is not None, (rc, err)
        fe = _frame_entry(rec)
        assert fe["coverage_verified"] is True and fe["coverage_source"] == "visible", fe
        assert fe["coverage_reason"] == "ok", fe


def test_end_to_end_a_short_central_opening_is_not_taken_for_the_aperture():
    """Dense film over the first 8 mm leaves a 28 mm lit run in the
    central band -- long enough to pass as an aperture. For 110 the
    opening must be the holder's own length, so the side bands decide and
    the crop starts at the plastic, not at the film (which it would cut)."""
    with _disk_tmp() as d:
        img = _frame(E2E_DPI, E2E_W, dense=[(0.0, 8.0)])
        normal = aperture_crop.measure_coverage(img, dpi=E2E_DPI)
        assert normal.verified, normal              # the trap: it verifies today
        rc, rec, out, err = _digitize_plain(d, img)
        fe = _frame_entry(rec)
        assert fe["coverage_verified"] is True, (rc, fe, err)
        assert fe["coverage_source"] == "visible-side", fe
        lead, _trail = _true_edges(E2E_DPI)
        assert abs(normal.leading_line - lead) > 8 * E2E_DPI / 25.4 * 0.9, normal


def main() -> int:
    tests = [
        test_dense_film_over_an_edge_in_the_centre_band,
        test_dense_images_splitting_the_centre_run,
        test_a_shorter_lit_run_beside_the_strip_is_never_accepted,
        test_bands_that_disagree_are_refused_not_chosen,
        test_a_strip_tip_over_some_bands_does_not_veto_the_bare_ones,
        test_full_width_dense_film_without_air_is_refused,
        test_no_edge_is_inferred_from_the_expected_length,
        test_normal_verdict_is_untouched_and_default_is_the_central_band,
        test_end_to_end_plain_110_uses_the_side_band_and_writes_the_frame,
        test_end_to_end_neither_verifies_is_the_usual_refusal_with_both_reasons,
        test_end_to_end_a_frame_that_verifies_today_is_identical,
        test_end_to_end_a_short_central_opening_is_not_taken_for_the_aperture,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
