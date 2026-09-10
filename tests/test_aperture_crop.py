#!/usr/bin/env python3
"""Offline tests for of135i.aperture_crop -- no hardware required.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_aperture_crop.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import aperture
from of135i.aperture_crop import ApertureCoverage, along_strip_profile, crop_to_aperture, measure_coverage

DPI = 600
LINES_PER_MM = DPI / 25.4
WIDTH = 200


def _line_image(values) -> np.ndarray:
    """Broadcast a 1-D per-line value sequence to a (lines, width, 3)
    uint16 image, the same value on every column and channel -- the
    along-strip profile only needs a per-line signal, not real texture."""
    values = np.asarray(values, dtype=np.float64)
    arr = np.empty((len(values), WIDTH, 3), dtype="<u2")
    arr[:, :, :] = values[:, None, None]
    return arr


def _aperture_lines(margin_mm=25.5):
    """Line count for an aperture comfortably over MIN_APERTURE_MM."""
    return int(round(margin_mm * LINES_PER_MM))


def test_measure_coverage_clean_aperture_is_verified():
    leading, trailing = 100, 100
    n_ap = _aperture_lines()
    plastic, lit = 800.0, 40000.0

    values = [plastic] * leading + [lit] * n_ap + [plastic] * trailing
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI)

    assert coverage.verified, coverage.reason
    assert coverage.leading_line is not None and coverage.trailing_line is not None
    # start of the bright run is at index `leading`; the detector places
    # the crossing on the ramp itself, so within a line or two of it.
    assert abs(coverage.leading_line - leading) <= 2.0, coverage.leading_line
    expected_trailing = leading + n_ap - 1  # last bright line
    assert abs(coverage.trailing_line - expected_trailing) <= 2.0, coverage.trailing_line
    assert coverage.leading_margin_mm > 0.15
    assert coverage.trailing_margin_mm > 0.15
    print("test_measure_coverage_clean_aperture_is_verified OK")


def test_measure_coverage_clipped_leading_is_not_verified():
    # Almost no plastic before the aperture -- leading margin well under
    # min_margin_mm even though the edge is still (barely) resolvable.
    leading, trailing = 3, 100
    n_ap = _aperture_lines()
    plastic, lit = 800.0, 40000.0

    values = [plastic] * leading + [lit] * n_ap + [plastic] * trailing
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI, min_margin_mm=0.5)

    assert not coverage.verified
    assert "leading" in coverage.reason, coverage.reason
    print("test_measure_coverage_clipped_leading_is_not_verified OK")


def test_measure_coverage_clipped_trailing_is_not_verified():
    leading, trailing = 100, 3
    n_ap = _aperture_lines()
    plastic, lit = 800.0, 40000.0

    values = [plastic] * leading + [lit] * n_ap + [plastic] * trailing
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI, min_margin_mm=0.5)

    assert not coverage.verified
    assert "trailing" in coverage.reason, coverage.reason
    print("test_measure_coverage_clipped_trailing_is_not_verified OK")


def test_measure_coverage_no_aperture_found():
    # Uniformly dark -- no lit run at all.
    image = _line_image([800.0] * 400)
    coverage = measure_coverage(image, dpi=DPI)
    assert not coverage.verified
    assert coverage.leading_line is None and coverage.trailing_line is None
    assert "no aperture" in coverage.reason, coverage.reason
    print("test_measure_coverage_no_aperture_found OK")


def test_measure_coverage_colour_negative_mid_dip_stays_one_aperture():
    # A colour negative: the aperture itself reads at the inter-frame
    # rebate level (~9000), not full white, and a block of real "image
    # content" sits in the middle of it, well short of black. Chosen
    # levels: plastic 800, rebate 9000, content 4500. The PLASTIC_LEVEL
    # rule in of135i.aperture only treats a transition as a holder edge
    # if one side is within 15% of the floor-to-lit range of the floor;
    # here that cutoff is floor(800) + 0.15*(9000-800) = 2030, so a dip
    # to 4500 sits well above it and is correctly *not* mistaken for
    # plastic. (A dip closer to black, e.g. ~1500 as a first guess,
    # falls under that cutoff and gets treated as a second aperture
    # edge -- this level was raised specifically to stay clear of it,
    # per the task's "tune and document" allowance.)
    leading, trailing = 100, 100
    n_ap = _aperture_lines()
    plastic, rebate, content = 800.0, 9000.0, 4500.0

    dip_start, dip_len = n_ap // 2 - 25, 50
    ap_values = [rebate] * n_ap
    for i in range(dip_start, dip_start + dip_len):
        ap_values[i] = content

    values = [plastic] * leading + ap_values + [plastic] * trailing
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI)

    assert coverage.verified, coverage.reason
    assert abs(coverage.leading_line - leading) <= 2.0, coverage.leading_line
    expected_trailing = leading + n_ap - 1
    assert abs(coverage.trailing_line - expected_trailing) <= 2.0, coverage.trailing_line
    print("test_measure_coverage_colour_negative_mid_dip_stays_one_aperture OK")


def test_measure_coverage_multiple_apertures_not_verified():
    n_ap = _aperture_lines()
    plastic, lit = 800.0, 40000.0
    crossbar = 40  # < MIN_APERTURE_MM in line-length, separates two apertures

    values = (
        [plastic] * 100
        + [lit] * n_ap
        + [plastic] * crossbar
        + [lit] * n_ap
        + [plastic] * 100
    )
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI)

    assert not coverage.verified
    assert coverage.leading_line is None and coverage.trailing_line is None
    assert "2" in coverage.reason or "apertures" in coverage.reason, coverage.reason
    print("test_measure_coverage_multiple_apertures_not_verified OK")


def test_along_strip_profile_uses_requested_channel():
    lines = 10
    arr = np.zeros((lines, WIDTH, 3), dtype="<u2")
    arr[:, :, 0] = 100   # red
    arr[:, :, 1] = 200   # green
    arr[:, :, 2] = 300   # blue

    red = along_strip_profile(arr, channel="red")
    green = along_strip_profile(arr, channel="green")
    blue = along_strip_profile(arr, channel="blue")
    lum = along_strip_profile(arr, channel="lum")

    assert np.allclose(red, 100)
    assert np.allclose(green, 200)
    assert np.allclose(blue, 300)
    assert np.allclose(lum, 200)  # mean(100, 200, 300)
    print("test_along_strip_profile_uses_requested_channel OK")


def test_crop_to_aperture_line_count_and_no_mutation():
    lines = 80
    image = _line_image(list(range(lines)))
    original = image.copy()

    coverage = ApertureCoverage(
        verified=True,
        leading_line=10.4,
        trailing_line=50.6,
        leading_margin_mm=10.4 / LINES_PER_MM,
        trailing_margin_mm=(lines - 1 - 50.6) / LINES_PER_MM,
        reason="ok",
        threshold=0.0,
    )

    cropped = crop_to_aperture(image, coverage, dpi=DPI)

    # floor(10.4)=10 .. ceil(50.6)+1=52 (exclusive) -> 42 lines
    assert cropped.shape[0] == 42, cropped.shape[0]
    assert cropped.shape[1:] == image.shape[1:]
    assert np.array_equal(image, original), "crop_to_aperture mutated the input image"
    print("test_crop_to_aperture_line_count_and_no_mutation OK")


def test_crop_to_aperture_clamps_to_image_bounds_with_pad():
    lines = 60
    image = _line_image(list(range(lines)))

    coverage = ApertureCoverage(
        verified=True,
        leading_line=2.0,
        trailing_line=57.0,
        leading_margin_mm=2.0 / LINES_PER_MM,
        trailing_margin_mm=(lines - 1 - 57.0) / LINES_PER_MM,
        reason="ok",
        threshold=0.0,
    )

    # A large pad pushes both bounds past the image edges; the crop must
    # clamp rather than raise or return a negative-length slice.
    cropped = crop_to_aperture(image, coverage, dpi=DPI, pad_mm=5.0)
    assert cropped.shape[0] == lines
    print("test_crop_to_aperture_clamps_to_image_bounds_with_pad OK")


def test_crop_to_aperture_raises_without_located_edges():
    image = _line_image([100.0] * 20)
    coverage = ApertureCoverage(
        verified=False,
        leading_line=None,
        trailing_line=None,
        leading_margin_mm=None,
        trailing_margin_mm=None,
        reason="no aperture found in profile",
        threshold=0.0,
    )
    try:
        crop_to_aperture(image, coverage, dpi=DPI)
        raised = False
    except ValueError:
        raised = True
    assert raised, "expected ValueError when edges are not located"
    print("test_crop_to_aperture_raises_without_located_edges OK")


def main() -> int:
    tests = [
        test_measure_coverage_clean_aperture_is_verified,
        test_measure_coverage_clipped_leading_is_not_verified,
        test_measure_coverage_clipped_trailing_is_not_verified,
        test_measure_coverage_no_aperture_found,
        test_measure_coverage_colour_negative_mid_dip_stays_one_aperture,
        test_measure_coverage_multiple_apertures_not_verified,
        test_along_strip_profile_uses_requested_channel,
        test_crop_to_aperture_line_count_and_no_mutation,
        test_crop_to_aperture_clamps_to_image_bounds_with_pad,
        test_crop_to_aperture_raises_without_located_edges,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
