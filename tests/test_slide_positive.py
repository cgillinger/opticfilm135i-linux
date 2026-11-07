#!/usr/bin/env python3
"""Offline tests for positive slide output (issue #70 step 3) -- no
hardware.

A mounted slide is normally positive (reversal) film. `--positive` is the
negative path's density inversion (image.to_positive) and stays refused
with `--holder slide` (owner decision, issue #70). `--slide-positive`
writes the slide as a positive: the vendor orientation the --positive
path applies (horizontal mirror + rot90(,3)) to the visible and IR images
alike, and NO inversion; the pixel values are the raw linear scan. Strip
output, with and without --positive, is unchanged.

Plain asserts, no pytest dependency. Run with:
    python3 tests/test_slide_positive.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import cli, image


def _vendor_orient(a):
    return np.ascontiguousarray(np.rot90(a, 3)[:, ::-1])


def _no_inversion():
    """Make any call of image.to_positive fail the test."""
    def boom(*a, **k):
        raise AssertionError("to_positive must not run on a positive slide")
    saved = image.to_positive
    image.to_positive = boom
    return saved


def test_orient_pair_slide_positive_turns_both_and_never_inverts():
    rng = np.random.default_rng(1)
    vis = rng.integers(0, 65535, (40, 30, 3), dtype=np.uint16)
    irr = rng.integers(0, 65535, (40, 30), dtype=np.uint16)
    saved = _no_inversion()
    try:
        for rot in (0, 90):
            args = SimpleNamespace(positive=False, slide_positive=True, rotate=rot)
            v, i = cli._orient_pair(args, vis, irr)
            want_v, want_i = _vendor_orient(vis), _vendor_orient(irr)
            if rot:
                want_v, want_i = np.rot90(want_v, 1), np.rot90(want_i, 1)
            assert np.array_equal(v, want_v) and np.array_equal(i, want_i), rot
    finally:
        image.to_positive = saved
    print("test_orient_pair_slide_positive_turns_both_and_never_inverts OK")


def test_strip_orientation_is_unchanged():
    """--positive (strip) is still mirror + rot + to_positive; no flag at
    all (an old Namespace without slide_positive) is the raw orientation."""
    rng = np.random.default_rng(2)
    vis = rng.integers(1, 65535, (40, 30, 3), dtype=np.uint16)
    irr = rng.integers(0, 65535, (40, 30), dtype=np.uint16)
    v, i = cli._orient_pair(SimpleNamespace(positive=True, rotate=0), vis, irr)
    assert np.array_equal(v, image.to_positive(_vendor_orient(vis)))
    assert np.array_equal(i, _vendor_orient(irr))
    v, i = cli._orient_pair(SimpleNamespace(positive=False, rotate=0), vis, irr)
    assert v is vis and i is irr
    v, i = cli._orient_pair(SimpleNamespace(positive=False, slide_positive=False,
                                            rotate=0), vis, irr)
    assert v is vis and i is irr
    print("test_strip_orientation_is_unchanged OK")


def test_axis_dpi_follows_the_turn_at_2400():
    """The 2400 dpi profile is anisotropic: the turned positive slide must
    carry the swapped axes, exactly like a turned --positive strip."""
    pos = SimpleNamespace(dpi=2400, rotate=0, positive=False, slide_positive=True)
    raw = SimpleNamespace(dpi=2400, rotate=0, positive=False, slide_positive=False)
    assert cli._axis_dpi(pos, cli._turned(pos)) == cli._axis_dpi(raw, True)
    assert cli._axis_dpi(raw, cli._turned(raw)) == cli._axis_dpi(raw, False)
    assert cli._axis_dpi(pos, cli._turned(pos)) != cli._axis_dpi(raw, False)
    print("test_axis_dpi_follows_the_turn_at_2400 OK")


def test_cli_slide_positive_validation_before_the_device():
    from tests.test_slides import _cli_rc
    p = cli.build_parser()
    assert p.parse_args(["scan", "--frame", "1", "-o", "x.tiff"]).slide_positive is False
    for argv, text in (
            (["--frame", "1", "--ir", "--slide-positive"], "--holder slide"),
            (["--holder", "slide", "--frame", "1", "--ir", "--positive",
              "--slide-positive"], "--positive is refused")):
        rc, err, _out, opened = _cli_rc(["scan", *argv, "-o", "/tmp/x.tiff"])
        assert rc == 2 and text in err and not opened, (argv, rc, err)
    # The --positive refusal now names the slide's own switch.
    rc, err, _o, _ = _cli_rc(["scan", "--holder", "slide", "--frame", "1", "--ir",
                              "--positive", "-o", "/tmp/x.tiff"])
    assert rc == 2 and "--slide-positive" in err, err
    rc, err, _o, opened = _cli_rc(["scan", "--holder", "slide", "--frame", "1", "--ir",
                                   "--slide-positive", "-o", "/tmp/x.tiff"])
    assert rc == 0 and opened, (rc, err)
    print("test_cli_slide_positive_validation_before_the_device OK")


def _capture_run(extra):
    """Run one verified slide scan (tests/test_slide_crop.py fixture) and
    capture every array the CLI writes: {path suffix: (array, icc)}."""
    import os
    import tempfile
    from tests.test_slide_crop import _run_cli, _slide_ir
    written = {}
    saved_t, saved_w = image.write_tiff16, cli._write_image

    def tiff(arr, path, icc=None, dpi=None):
        written[str(path)] = (np.array(arr), icc, dpi)

    image.write_tiff16 = tiff
    try:
        with tempfile.TemporaryDirectory() as d:
            o = os.path.join(d, "s.tiff")
            rc, err, _out = _run_cli(_slide_ir(600, width=8)[0],
                                     ["scan", "--holder", "slide", "--frame", "1",
                                      "--dpi", "600", "--ir", "--no-clean", *extra,
                                      "-o", o])
            assert rc == 0, (rc, err)
            return {Path(k).name: v for k, v in written.items()}
    finally:
        image.write_tiff16, cli._write_image = saved_t, saved_w


def test_cli_slide_positive_writes_the_turned_raw_values_no_icc():
    raw = _capture_run([])
    saved = _no_inversion()
    try:
        pos = _capture_run(["--slide-positive"])
    finally:
        image.to_positive = saved
    assert set(raw) == set(pos) == {"s.tiff", "s-ir.tiff", "s.overscan.tiff",
                                    "s-ir.overscan.tiff"}, (sorted(raw), sorted(pos))
    for name in raw:
        a, icc_a, _ = raw[name]
        b, icc_b, _ = pos[name]
        assert np.array_equal(b, _vendor_orient(a)), name     # turned, same values
        assert icc_a is None and icc_b is None, name           # linear data: no sRGB tag
    print("test_cli_slide_positive_writes_the_turned_raw_values_no_icc OK")


def test_sidecar_records_slide_positive():
    import argparse
    import json
    import os
    import tempfile
    from of135i import diag

    class _Sc:
        last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6]}
        last_cal_data = None

    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "s.tiff")
        a = argparse.Namespace(no_diag=False, frames=None, dpi=600, ir=True,
                               positive=False, rotate=0, no_clean=False,
                               holder="slide", slide_positive=True)
        cli._write_diag_sidecar(a, _Sc(), out, 1)
        side = json.loads(Path(diag.sidecar_path(out)).read_text())
        assert side["cli"]["slide_positive"] is True and side["cli"]["holder"] == "slide"
        a = argparse.Namespace(no_diag=False, frames=None, dpi=600, ir=True,
                               positive=False, rotate=0, no_clean=False)
        cli._write_diag_sidecar(a, _Sc(), out, 1)
        side = json.loads(Path(diag.sidecar_path(out)).read_text())
        assert "slide_positive" not in side["cli"] and "holder" not in side["cli"]
    print("test_sidecar_records_slide_positive OK")


def main():
    tests = [
        test_orient_pair_slide_positive_turns_both_and_never_inverts,
        test_strip_orientation_is_unchanged,
        test_axis_dpi_follows_the_turn_at_2400,
        test_cli_slide_positive_validation_before_the_device,
        test_cli_slide_positive_writes_the_turned_raw_values_no_icc,
        test_sidecar_records_slide_positive,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
