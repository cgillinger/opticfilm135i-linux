#!/usr/bin/env python3
"""Offline tests for the mounted-slide crop (issue #70 step 2) -- no
hardware, synthetic fixtures only.

The strip coverage check looks for the strip holder's opening edges. In
a slide window the light the IR channel sees is: holder plastic (dark),
the mount (opaque to IR), the film in the mount's aperture (IR-
transparent, E6 dye film: IR mean ~48300 in Test 84), the mount, the
plastic. aperture_crop.measure_slide_coverage() finds that lit run in the
IR channel only (visible light is fooled by dark picture content,
docs/slide-holder-analysis.md section 5) with the same edge detector as
strips, and gives it the strips' contract: exactly one mount aperture,
both edges found with at least min_margin_mm inside the window ->
"coverage VERIFIED", crop to it; anything else -> "NOT verified", the
full overscan frame(s) kept, exit 4, so no picture is ever lost.

Slide-specific: the run must be 33-37 mm long (a mount aperture's long
side; Tests 84/85 measured 35.06-35.1 mm). Shorter is refused, because
an IR-dense picture area at the mount edge (silver, some Kodachrome)
shortens the lit run and a crop to it would cut into the picture.
Longer is refused too: that is the 38.05 mm holder opening itself (no
mount, or a mount the IR passes through).

The fixtures are profiles built from the Test 84 geometry, not scans; a
real scan is what settles the crop (issue #70 step 4).

Plain asserts, no pytest dependency. Run with:
    python3 tests/test_slide_crop.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import aperture, aperture_crop, holder

PLASTIC, MOUNT, FILM, OPEN = 800, 900, 48000, 60000
OVER = holder.SLIDE_OVERSCAN_MM
AP = 35.06                                   # Test 85's mount aperture, mm


def _slide_ir(dpi=600, *, lead_border=None, aperture_mm=AP, mount=MOUNT,
              dense_lead_mm=0.0, shift_mm=0.0, empty=False, width=64):
    """A (lines, width) IR frame of one slide window: OVER mm of plastic,
    the 38.05 mm opening (mount + aperture, or empty), OVER mm of plastic.
    ``lead_border`` is the mount border before the aperture (centred by
    default); ``dense_lead_mm`` makes the first mm of the picture IR-dense;
    ``shift_mm`` moves the holder content later in the window."""
    lpm = dpi / 25.4
    opening = holder.SLIDE_OPENING_MM
    n = int(round((opening + 2 * OVER) * lpm))
    x = (np.arange(n) + 0.5) / lpm - shift_mm          # mm along the window
    prof = np.full(n, PLASTIC, dtype=np.float64)
    in_open = (x >= OVER) & (x < OVER + opening)
    if empty:
        prof[in_open] = OPEN
    else:
        if lead_border is None:
            lead_border = (opening - aperture_mm) / 2
        a0 = OVER + lead_border
        prof[in_open] = mount
        prof[(x >= a0) & (x < a0 + aperture_mm)] = FILM
        prof[(x >= a0) & (x < a0 + dense_lead_mm)] = PLASTIC + 100
    return np.repeat(prof[:, None], width, axis=1).astype(np.uint16), lpm


def _cov(ir, dpi=600):
    return aperture_crop.measure_slide_coverage(ir, dpi=dpi)


def test_centred_mount_is_verified_and_cropped_to_the_aperture():
    for dpi in (600, 1200, 3600):
        ir, lpm = _slide_ir(dpi)
        cov = _cov(ir, dpi)
        assert cov.verified and cov.source == "ir", (dpi, cov)
        length = (cov.trailing_line - cov.leading_line) / lpm
        tol = max(1, round(dpi / 600)) * 2 / lpm       # binned edge finder
        assert abs(length - AP) <= tol, (dpi, length)
        border = (holder.SLIDE_OPENING_MM - AP) / 2
        assert abs(cov.leading_margin_mm - (OVER + border)) <= tol, (dpi, cov)
        crop = aperture_crop.crop_to_aperture(ir, cov, dpi=dpi)
        assert abs(crop.shape[0] / lpm - AP) <= tol + 2 / lpm, (dpi, crop.shape)
        assert crop.shape[1] == ir.shape[1]             # along axis 0 only
    print("test_centred_mount_is_verified_and_cropped_to_the_aperture OK")


def test_off_centre_and_dark_grey_mounts_are_verified():
    """Test 84's slide sat 2.7 mm into its opening; a mount that is dark
    but not black in IR (within the edge finder's plastic level) still
    bounds the run."""
    ir, lpm = _slide_ir(lead_border=2.7)
    cov = _cov(ir)
    assert cov.verified, cov
    assert abs(cov.leading_margin_mm - (OVER + 2.7)) < 3 / lpm, cov
    ir, _ = _slide_ir(mount=4800)                       # ~8 % of the film level
    assert _cov(ir).verified
    print("test_off_centre_and_dark_grey_mounts_are_verified OK")


def test_ir_dense_picture_at_the_mount_edge_is_not_verified():
    """5 mm of IR-dense picture at the leading edge leaves a 30 mm lit
    run: the strip floor (25 mm) would accept it and crop into the
    picture; the slide floor refuses it."""
    ir, lpm = _slide_ir(dense_lead_mm=5.0)
    assert aperture.MIN_APERTURE_MM < 30 < aperture_crop.SLIDE_MIN_APERTURE_MM
    strip_like = aperture_crop.measure_coverage(ir, dpi=600)
    assert strip_like.verified                          # the hazard
    cov = _cov(ir)
    assert not cov.verified and cov.leading_line is None, cov
    assert "mount aperture" in cov.reason and "33" in cov.reason, cov.reason
    print("test_ir_dense_picture_at_the_mount_edge_is_not_verified OK")


def test_empty_opening_and_ir_translucent_mount_are_not_verified():
    for kw in (dict(empty=True), dict(mount=24000)):
        ir, _ = _slide_ir(**kw)
        cov = _cov(ir)
        assert not cov.verified, (kw, cov)
    cov = _cov(_slide_ir(empty=True)[0])
    assert "longer than" in cov.reason and "empty opening" in cov.reason, cov.reason
    print("test_empty_opening_and_ir_translucent_mount_are_not_verified OK")


def test_clipped_aperture_is_not_verified():
    """The holder content 3 mm later than the window: the aperture's
    trailing edge falls outside the window, so it cannot be proven."""
    ir, _ = _slide_ir(shift_mm=3.0)
    cov = _cov(ir)
    assert not cov.verified, cov
    print("test_clipped_aperture_is_not_verified OK")


def test_strip_coverage_is_unchanged():
    """measure_coverage keeps the strip floor by default; the new keyword
    only narrows it when asked."""
    ir, _ = _slide_ir(dense_lead_mm=5.0)
    a = aperture_crop.measure_coverage(ir, dpi=600)
    b = aperture_crop.measure_coverage(ir, dpi=600, min_aperture_mm=aperture.MIN_APERTURE_MM)
    assert a == b and a.verified
    print("test_strip_coverage_is_unchanged OK")


# ---------------------------------------------------------------- CLI


def _dual_raw(ir_frame, dpi):
    """Interleave a (lines, W) IR frame with a matching visible frame into
    a dual-light raw buffer (even wire line = IR, odd = visible). The
    align_shift lines at each end are padded with plastic, as split_ir /
    align_channels crop them."""
    from of135i import image as _image
    sh = _image.align_shift(dpi)
    prof = ir_frame[:, 0].astype(np.uint16)
    prof = np.concatenate([np.full(sh, PLASTIC, np.uint16), prof,
                           np.full(sh, PLASTIC, np.uint16)])
    vis = np.where(prof > 20000, 3000, 700).astype(np.uint16)  # a dark picture
    from of135i.device import dual_tables
    W = dual_tables(dpi).IMAGE_WIDTH
    wire = np.empty(2 * len(prof), dtype=np.uint16)
    wire[0::2] = prof
    wire[1::2] = vis
    buf = np.repeat(np.repeat(wire[:, None], W, axis=1)[:, :, None], 3, axis=2).astype("<u2")
    return buf.tobytes(), W


def _run_cli(ir_frame, argv, dpi=600):
    import io
    import os
    from contextlib import redirect_stderr, redirect_stdout
    from of135i import cli
    from tests.test_slides import _FakeSlideScanner
    raw, W = _dual_raw(ir_frame, dpi)

    class _Fake(_FakeSlideScanner):
        def scan(self, **k):
            self.calls.append(("scan", k))
            self.last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6],
                              "holder": "slide", "frame": k["frame"]}
            return raw, W, {"width": W, "alternating": True, "dpi": dpi}

    os.environ.pop(cli.POSITION_OFFSET_ENV, None)
    fake = _Fake(dpi)
    saved = cli._run_writing_session
    cli._run_writing_session = lambda body: body(fake)
    try:
        args = cli.build_parser().parse_args(argv)
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = args.func(args)
    finally:
        cli._run_writing_session = saved
    return rc, err.getvalue(), out.getvalue()


def test_cli_slide_verified_writes_the_cropped_pair_and_keeps_the_overscan():
    import json
    import os
    import tempfile
    from of135i import diag
    ir, lpm = _slide_ir(600, width=8)
    with tempfile.TemporaryDirectory() as d:
        o = os.path.join(d, "s.tiff")
        rc, err, out = _run_cli(ir, ["scan", "--holder", "slide", "--frame", "2",
                                     "--dpi", "600", "--ir", "--no-clean", "-o", o])
        assert rc == 0, (rc, err)
        stem = os.path.join(d, "s")
        for f in (".tiff", "-ir.tiff", ".overscan.tiff", "-ir.overscan.tiff"):
            assert os.path.exists(stem + f), f
        side = json.loads(open(diag.sidecar_path(o)).read())
        c = side["coverage"]
        assert c["verified"] is True and c["source"] == "ir", c
        assert c["method"] == "slide-ir", c
        assert abs((c["trailing_line"] - c["leading_line"]) / lpm - AP) < 0.2, c
        assert "VERIFIED" in out and "mount aperture" in out, out
    print("test_cli_slide_verified_writes_the_cropped_pair_and_keeps_the_overscan OK")


def test_cli_slide_not_verified_keeps_the_overscan_and_exits_4():
    import json
    import os
    import tempfile
    from of135i import diag
    ir, _ = _slide_ir(600, width=8, dense_lead_mm=5.0)
    with tempfile.TemporaryDirectory() as d:
        o = os.path.join(d, "s.tiff")
        rc, err, out = _run_cli(ir, ["scan", "--holder", "slide", "--frame", "1",
                                     "--dpi", "600", "--ir", "--no-clean", "-o", o])
        assert rc == 4, (rc, err)
        stem = os.path.join(d, "s")
        assert os.path.exists(stem + ".overscan.tiff")
        assert os.path.exists(stem + "-ir.overscan.tiff")
        assert not os.path.exists(o) and not os.path.exists(stem + "-ir.tiff")
        assert "NOT verified" in err and "--position-offset" not in err, err
        side = json.loads(open(diag.sidecar_path(o)).read())
        assert side["coverage"]["verified"] is False, side["coverage"]
    print("test_cli_slide_not_verified_keeps_the_overscan_and_exits_4 OK")


def main():
    tests = [
        test_centred_mount_is_verified_and_cropped_to_the_aperture,
        test_off_centre_and_dark_grey_mounts_are_verified,
        test_ir_dense_picture_at_the_mount_edge_is_not_verified,
        test_empty_opening_and_ir_translucent_mount_are_not_verified,
        test_clipped_aperture_is_not_verified,
        test_strip_coverage_is_unchanged,
        test_cli_slide_verified_writes_the_cropped_pair_and_keeps_the_overscan,
        test_cli_slide_not_verified_keeps_the_overscan_and_exits_4,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
