#!/usr/bin/env python3
"""Offline tests for `of135i recrop` (of135i.recrop) -- no hardware.

A frame whose coverage did not verify keeps only its overscan frame(s).
`recrop` makes the products from those files with a margin floor the
operator chooses (issue #61: leading margins of 0.10-0.14 mm against the
0.15 mm floor, the picture whole in the overscan file). Covered here:

  - the file set derived from any of a frame's names;
  - the default floor refuses the frame as the scan did, a lower floor
    writes it; the written crop is exactly the scan commands' crop
    (same lines in both channels), rotated as the overscan file is;
  - an empty opening is not written; a positive overscan is refused; a
    product in the way is refused without --overwrite; no sidecar needs
    --dpi/--rotate; --dry-run writes nothing;
  - the CLI exit codes (0 written, 4 not verified, 2 not processable).

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_recrop.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import tifffile

from of135i import aperture_crop, image, recrop

DPI = 3600
LPM = DPI / 25.4
W = 400
LEAD, RAMP = 22, 10          # ~0.10 mm of plastic resolved before the opening
TRAIL = int(round(1.2 * LPM))
N_AP = int(round(36.0 * LPM))


def _profile(lo: float, hi: float) -> np.ndarray:
    total = LEAD + N_AP + TRAIL
    col = np.full(total, lo)
    col[LEAD:LEAD + N_AP] = hi
    for i in range(RAMP):
        col[LEAD - RAMP + i] = lo + (hi - lo) * i / RAMP
        col[LEAD + N_AP + i] = hi - (hi - lo) * i / RAMP
    return col


def _frame_arrays(empty: bool = False):
    """(visible (lines, W, 3), ir (lines, W)) uint16, strip along axis 0:
    plastic 800, film 20000 in the visible channels (an empty opening is
    open light, 65000), IR open 60000 over the whole opening."""
    vis_col = _profile(800.0, 65000.0 if empty else 20000.0).astype(np.uint16)
    ir_col = _profile(800.0, 60000.0).astype(np.uint16)
    visible = np.repeat(np.repeat(vis_col[:, None], W, axis=1)[:, :, None], 3, axis=2)
    ir = np.repeat(ir_col[:, None], W, axis=1)
    return np.ascontiguousarray(visible), np.ascontiguousarray(ir)


def _write_frame(d: str, stem: str, *, rotate: int = 0, ir: bool = True,
                 empty: bool = False, sidecar: bool = True, positive: bool = False):
    visible, irr = _frame_arrays(empty)
    k = rotate // 90
    vis_w = np.ascontiguousarray(np.rot90(visible, k)) if k else visible
    ir_w = np.ascontiguousarray(np.rot90(irr, k)) if k else irr
    p = recrop.paths_for(os.path.join(d, f"{stem}.tiff"))
    image.write_tiff16(vis_w, p.overscan, dpi=recrop.axis_dpi(DPI, rotate))
    if ir:
        image.write_tiff16(np.stack([ir_w, ir_w, ir_w], axis=-1), p.ir_overscan,
                           dpi=recrop.axis_dpi(DPI, rotate))
    if sidecar:
        p.sidecar.write_text(json.dumps({"cli": {"dpi": DPI, "rotate": rotate,
                                                 "positive": positive, "ir": ir,
                                                 "frame": 4}}))
    return p, visible, irr


def test_paths_for_every_name_of_a_frame():
    want = recrop.paths_for("/r/f4.overscan.tiff")
    for name in ("/r/f4.tiff", "/r/f4", "/r/f4-ir.overscan.tiff", "/r/f4-ir.tiff"):
        assert recrop.paths_for(name) == want, name
    assert want.main == Path("/r/f4.tiff") and want.ir == Path("/r/f4-ir.tiff")
    assert want.ir_overscan == Path("/r/f4-ir.overscan.tiff")
    assert want.sidecar == Path("/r/f4.diag.json") and want.record == Path("/r/f4.recrop.json")
    p = recrop.paths_for("/r/a-f1.overscan.tif")
    assert p.main == Path("/r/a-f1.tif") and p.ir_overscan == Path("/r/a-f1-ir.overscan.tiff")
    print("test_paths_for_every_name_of_a_frame OK")


def test_default_floor_refuses_as_the_scan_did():
    with tempfile.TemporaryDirectory() as d:
        p, _, _ = _write_frame(d, "f4")
        res = recrop.recrop(p.overscan)
        assert not res.written and not res.verified, res
        assert "leading margin below" in res.reason, res.reason
        assert res.leading_margin_mm is not None and res.leading_margin_mm < 0.15
        assert res.offset_hint_mm is not None and res.offset_hint_mm < 0, res
        assert not p.main.exists() and not p.ir.exists()
        rec = json.loads(p.record.read_text())
        assert rec["written"] is False and rec["dpi"] == DPI and rec["settings_source"] == "sidecar"
    print("test_default_floor_refuses_as_the_scan_did OK")


def test_lower_floor_writes_the_scan_commands_crop_in_both_channels():
    for rotate in (0, 90, 270):
        with tempfile.TemporaryDirectory() as d:
            p, visible, irr = _write_frame(d, "f4", rotate=rotate)
            res = recrop.recrop(p.overscan, min_margin_mm=0.05)
            assert res.written and res.verified and res.film_present, res
            assert res.source == "ir", res.source
            assert res.main == str(p.main) and res.ir == str(p.ir)
            # the reference: what the scan commands write for these arrays
            cov = aperture_crop.measure_coverage_dual(visible, irr, dpi=DPI, min_margin_mm=0.05)
            vis_ref = aperture_crop.crop_to_aperture(visible, cov, dpi=DPI)
            ir_ref = aperture_crop.crop_to_aperture(irr, cov, dpi=DPI)
            k = rotate // 90
            got_vis = tifffile.imread(str(p.main))
            got_ir = tifffile.imread(str(p.ir))
            if k:
                got_vis = np.rot90(got_vis, -k)
                got_ir = np.rot90(got_ir, -k)
            assert np.array_equal(got_vis, vis_ref), (rotate, got_vis.shape, vis_ref.shape)
            assert got_ir.shape[:2] == ir_ref.shape and np.array_equal(got_ir[:, :, 0], ir_ref), rotate
            assert vis_ref.shape[0] < visible.shape[0]
            rec = json.loads(p.record.read_text())
            assert rec["written"] is True and rec["rotate"] == rotate and rec["min_margin_mm"] == 0.05
    print("test_lower_floor_writes_the_scan_commands_crop_in_both_channels OK")


def test_plain_frame_without_ir_overscan():
    with tempfile.TemporaryDirectory() as d:
        p, visible, _ = _write_frame(d, "f2", ir=False)
        res = recrop.recrop(p.overscan, min_margin_mm=0.0)
        assert res.written and res.source == "visible" and res.ir is None, res
        assert p.main.exists() and not p.ir.exists()
        cov = aperture_crop.measure_coverage(visible, dpi=DPI, min_margin_mm=0.0)
        ref = aperture_crop.crop_to_aperture(visible, cov, dpi=DPI)
        assert np.array_equal(tifffile.imread(str(p.main)), ref)
    print("test_plain_frame_without_ir_overscan OK")


def test_empty_opening_is_not_written():
    with tempfile.TemporaryDirectory() as d:
        p, _, _ = _write_frame(d, "f6", empty=True)
        res = recrop.recrop(p.overscan, min_margin_mm=0.0)
        assert not res.written and res.verified and res.film_present is False, res
        assert res.reason == "empty aperture"
        assert not p.main.exists() and not p.ir.exists()
    print("test_empty_opening_is_not_written OK")


def test_refusals():
    with tempfile.TemporaryDirectory() as d:
        # a product in the way
        p, _, _ = _write_frame(d, "f4")
        p.main.write_bytes(b"x")
        try:
            recrop.recrop(p.overscan, min_margin_mm=0.0)
        except recrop.RecropError as e:
            assert "already exists" in str(e), e
        else:
            raise AssertionError("overwrote a product")
        assert p.main.read_bytes() == b"x"
        res = recrop.recrop(p.overscan, min_margin_mm=0.0, overwrite=True)
        assert res.written and p.main.stat().st_size > 1
        # --dry-run writes nothing, not even the record
        p2, _, _ = _write_frame(d, "f5")
        res = recrop.recrop(p2.overscan, min_margin_mm=0.0, dry_run=True)
        assert not res.written and res.verified and res.main == str(p2.main), res
        assert not p2.main.exists() and not p2.record.exists()
        # a positive rendering
        p3, _, _ = _write_frame(d, "f1", positive=True)
        try:
            recrop.recrop(p3.overscan, min_margin_mm=0.0)
        except recrop.RecropError as e:
            assert "--positive" in str(e), e
        else:
            raise AssertionError("accepted a positive overscan")
        # no sidecar: the settings must be given
        p4, _, _ = _write_frame(d, "f3", rotate=90, sidecar=False)
        try:
            recrop.recrop(p4.overscan, min_margin_mm=0.0)
        except recrop.RecropError as e:
            assert "--dpi" in str(e), e
        else:
            raise AssertionError("ran without settings")
        res = recrop.recrop(p4.overscan, min_margin_mm=0.0, dpi=DPI, rotate=90)
        assert res.written and res.settings_source == "arguments", res
        # a missing file
        try:
            recrop.recrop(os.path.join(d, "f9.overscan.tiff"))
        except recrop.RecropError as e:
            assert "does not exist" in str(e), e
        else:
            raise AssertionError("accepted a missing file")
    print("test_refusals OK")


def _run_cli(argv):
    from of135i import cli
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["recrop"] + argv)
    return rc, out.getvalue(), err.getvalue()


def test_cli_exit_codes_and_messages():
    with tempfile.TemporaryDirectory() as d:
        p, _, _ = _write_frame(d, "f4")
        rc, out, err = _run_cli([str(p.overscan)])
        assert rc == 4, (rc, out, err)
        assert "NOT verified" in err and "--position-offset -0." in err, err
        assert not p.main.exists()
        rc, out, err = _run_cli(["--dry-run", "--min-margin", "0.05", str(p.main)])
        assert rc == 0 and "would write" in out, (rc, out, err)
        assert not p.main.exists()
        rc, out, err = _run_cli(["--min-margin", "0.05", str(p.main)])
        assert rc == 0 and "wrote" in out and str(p.ir) in out, (rc, out, err)
        assert p.main.exists() and p.ir.exists()
        rc, out, err = _run_cli(["--force", str(p.overscan)])
        assert rc == 2 and "already exists" in err, (rc, out, err)
        rc, out, err = _run_cli(["--force", "--overwrite", "--no-ir", str(p.overscan)])
        assert rc == 0 and str(p.ir) not in out, (rc, out, err)
        rc, out, err = _run_cli([os.path.join(d, "nope.overscan.tiff")])
        assert rc == 2 and "does not exist" in err, (rc, out, err)
        p6, _, _ = _write_frame(d, "f6", empty=True)
        rc, out, err = _run_cli(["--force", str(p6.overscan)])
        assert rc == 4 and "EMPTY" in err, (rc, out, err)
    print("test_cli_exit_codes_and_messages OK")


def main():
    tests = [
        test_paths_for_every_name_of_a_frame,
        test_default_floor_refuses_as_the_scan_did,
        test_lower_floor_writes_the_scan_commands_crop_in_both_channels,
        test_plain_frame_without_ir_overscan,
        test_empty_opening_is_not_written,
        test_refusals,
        test_cli_exit_codes_and_messages,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
