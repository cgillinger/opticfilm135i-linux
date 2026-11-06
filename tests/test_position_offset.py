#!/usr/bin/env python3
"""Offline tests for the per-unit position offset (issue #61) -- no
hardware.

The corrected mean mapping (holder.STRIP_FIDUCIAL) was fitted on one
scanner. On another unit every aperture sat about 0.5 mm earlier in
the scan window, on all six frames and two holders (issue #61, the
sidecars of 2026-11-05), so frames 4 and 5 fell under the 0.15 mm
leading-margin floor. `--position-offset MM` moves every window along
the strip by a per-unit amount (FiducialModel.shifted). Covered here:

  - the geometry: FEEDL moves by exactly the offset, the window length,
    chunk count and margins do not, on the plain and every dual
    profile, and for the sweeps (whose predicted edges are relative to
    the window start and so do not move);
  - the range check (+/-holder.POSITION_OFFSET_MAX_MM, finite);
  - device wiring: Scanner.position_offset_mm reaches the FEEDL on the
    wire and the diag record, on the plain and dual paths;
  - the CLI: validation before hardware, the sidecar field, the
    coverage-failure hint computed from the second unit's own margins.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_position_offset.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import aperture_crop, holder, image, tables
from of135i.device import Scanner, dual_tables

DPIS = (600, 1200, 2400, 3600, 7200)


def _units(mm: float) -> float:
    return mm / holder.MM_PER_UNIT


def _plain(frame, offset=0.0, overscan=holder.OVERSCAN_MM):
    return holder.overscan_geometry(
        frame, res_units_per_line=2, chunk_lines=tables.IMAGE_CHUNK_LINES,
        colour_crop_lines=image.align_shift(3600), overscan_mm=overscan,
        offset_mm=offset)


def _dual(frame, dpi, offset=0.0):
    t = dual_tables(dpi)
    return holder.dual_overscan_geometry(
        frame, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
        colour_crop_lines=image.align_shift(dpi),
        default_wire_lines=t.DEFAULT_LINES, offset_mm=offset)


# --------------------------------------------------------------- geometry


def test_shifted_model_moves_every_aperture_by_the_offset():
    fid = holder.STRIP_FIDUCIAL
    assert fid.shifted(0.0) is fid
    for off in (-0.5, 0.3, -2.0):
        sh = fid.shifted(off)
        for frame in range(1, 7):
            assert math.isclose(sh.trailing_hwdpi(frame) - fid.trailing_hwdpi(frame), _units(off))
            assert math.isclose(sh.leading_hwdpi(frame) - fid.leading_hwdpi(frame), _units(off))
        assert sh.pitch_hwdpi == fid.pitch_hwdpi and sh.aperture_mm == fid.aperture_mm
    print("test_shifted_model_moves_every_aperture_by_the_offset OK")


def test_plain_geometry_feedl_moves_window_does_not():
    for frame in range(1, 7):
        base = _plain(frame)
        for off in (-0.6, -0.1, 0.25, 1.0):
            g = _plain(frame, off)
            assert abs((g.feedl - base.feedl) - _units(off)) <= 1.0, (frame, off, g.feedl, base.feedl)
            assert (g.wire_lines, g.chunks, g.delivered_lines) == (
                base.wire_lines, base.chunks, base.delivered_lines), (frame, off)
            assert g.leading_margin_mm == base.leading_margin_mm
            assert math.isclose(g.trailing_margin_mm, base.trailing_margin_mm)
            assert math.isclose(g.end_hwdpi - base.end_hwdpi, _units(off))
    print("test_plain_geometry_feedl_moves_window_does_not OK")


def test_dual_geometry_every_profile_feedl_moves_window_does_not():
    for dpi in DPIS:
        for frame in (1, 4, 6):
            base, wire0 = _dual(frame, dpi)
            for off in (-0.5, 0.5):
                g, wire = _dual(frame, dpi, off)
                assert wire == wire0, (dpi, frame)
                assert abs((g.feedl - base.feedl) - _units(off)) <= 1.0, (dpi, frame, off)
                assert (g.chunks, g.delivered_lines) == (base.chunks, base.delivered_lines)
                assert g.leading_margin_mm == base.leading_margin_mm
                assert math.isclose(g.trailing_margin_mm, base.trailing_margin_mm)
    print("test_dual_geometry_every_profile_feedl_moves_window_does_not OK")


def test_the_second_units_shift_lands_inside_the_margin():
    """The reported unit: leading margins 0.10-0.31 mm against a 0.75 mm
    target (window about 0.5 mm late). A -0.5 mm offset keeps every frame's
    window inside proven travel on both paths."""
    for frame in range(1, 7):
        _plain(frame, -0.5)
        for dpi in DPIS:
            _dual(frame, dpi, -0.5)
    print("test_the_second_units_shift_lands_inside_the_margin OK")


def test_sweep_geometry_feedl_moves_predicted_edges_do_not():
    t = dual_tables(3600)
    kw = dict(dpi=3600, lines_per_chunk=t.LINES_PER_CHUNK,
              colour_crop_lines=image.align_shift(3600),
              default_wire_lines=t.DEFAULT_LINES)
    base, wire0 = holder.dual_sweep_geometry(1, 4, **kw)
    g, wire = holder.dual_sweep_geometry(1, 4, offset_mm=-0.5, **kw)
    assert wire == wire0 and g.chunks == base.chunks
    assert abs((g.feedl - base.feedl) - _units(-0.5)) <= 1.0, (g.feedl, base.feedl)
    assert math.isclose(g.start_hwdpi - base.start_hwdpi, g.feedl - base.feedl)
    # relative to the (rounded) FEEDL, so equal to within the rounding
    for k in range(1, 5):
        a, b = g.predicted_edges[k]
        a0, b0 = base.predicted_edges[k]
        assert abs(a - a0) <= 0.5 and abs(b - b0) <= 0.5, (k, a, a0, b, b0)
    # plain sweep: same contract (offline-only path)
    pb, _ = holder.plain_sweep_geometry(
        1, 2, chunk_lines=tables.IMAGE_CHUNK_LINES,
        colour_crop_lines=image.align_shift(3600), default_lines=tables.DEFAULT_LINES)
    pg, _ = holder.plain_sweep_geometry(
        1, 2, chunk_lines=tables.IMAGE_CHUNK_LINES,
        colour_crop_lines=image.align_shift(3600), default_lines=tables.DEFAULT_LINES,
        offset_mm=0.4)
    assert abs((pg.feedl - pb.feedl) - _units(0.4)) <= 1.0
    assert pg.predicted_edges == pb.predicted_edges
    print("test_sweep_geometry_feedl_moves_predicted_edges_do_not OK")


def test_offset_range_check():
    lim = holder.POSITION_OFFSET_MAX_MM
    assert holder.check_position_offset(0) == 0.0
    assert holder.check_position_offset(-lim) == -lim
    assert holder.check_position_offset(lim) == lim
    for bad in (lim + 0.01, -lim - 0.01, float("nan"), float("inf"), "x", None):
        try:
            holder.check_position_offset(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad!r}")
    for bad in (lim + 0.5, float("nan")):
        try:
            _plain(1, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"geometry accepted {bad!r}")
        try:
            _dual(1, 600, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"dual geometry accepted {bad!r}")
    print("test_offset_range_check OK")


# ----------------------------------------------------------- device wiring


def test_plain_wiring_offset_reaches_the_wire_and_the_diag():
    from tests.test_overscan import _RecordingScanner
    rec = _RecordingScanner()
    assert rec.s.position_offset_mm == 0.0
    rec.s.position_offset_mm = -0.5
    rec.s._scan_plain(frame=4)
    pos = next(inj for ph, inj in rec.runs if ph.name == "position")
    feedl = (pos["feedl_hi"][0] << 16) | (pos["feedl_mid"][0] << 8) | pos["feedl_lo"][0]
    g = _plain(4, -0.5)
    assert feedl == g.feedl, (feedl, g.feedl)
    assert feedl != _plain(4).feedl
    assert rec.s.last_diag["position_offset_mm"] == -0.5
    assert rec.s.last_diag["feedl"] == g.feedl
    print("test_plain_wiring_offset_reaches_the_wire_and_the_diag OK")


def test_dual_wiring_offset_reaches_the_wire_and_the_diag():
    from tests.test_dual_overscan import _RecordingDualScanner
    for dpi in (600, 3600):
        t = dual_tables(dpi)
        rec = _RecordingDualScanner(t)
        rec.s.position_offset_mm = -0.5
        feedl, n_lines, _n_desc, diag = rec.run()
        g, wire = _dual(1, dpi, -0.5)
        assert feedl == g.feedl, (dpi, feedl, g.feedl)
        assert n_lines == wire
        assert diag["position_offset_mm"] == -0.5
    print("test_dual_wiring_offset_reaches_the_wire_and_the_diag OK")


def test_sweep_validation_uses_the_scanner_offset():
    t = dual_tables(3600)
    g0, _ = Scanner._sweep_geometry(t, 1, 4, holder.OVERSCAN_MM)
    g1, _ = Scanner._sweep_geometry(t, 1, 4, holder.OVERSCAN_MM, -0.5)
    assert abs((g1.feedl - g0.feedl) - _units(-0.5)) <= 1.0
    print("test_sweep_validation_uses_the_scanner_offset OK")


# ---------------------------------------------------------------------- CLI


def test_cli_validates_the_offset_before_hardware():
    from of135i import cli
    assert cli._validate_position_offset(0.0) is None
    assert cli._validate_position_offset(-0.5) is None
    for bad in (3.5, -3.5, float("nan")):
        msg = cli._validate_position_offset(bad)
        assert msg and "--position-offset" in msg, (bad, msg)
    assert cli._validate_overscan(0.75, [1, 6], dpi=3600, dual=True, offset_mm=-0.5) is None
    assert cli._validate_overscan(0.75, [1, 6], dpi=3600, dual=False, offset_mm=0.5) is None
    msg = cli._validate_overscan(0.75, [1], dpi=3600, dual=False, offset_mm=9.0)
    assert msg and "--position-offset" in msg, msg
    parser = cli.build_parser()
    for cmd in (["scan", "--frame", "1", "-o", "x.tiff"],
                ["digitize", "-o", "d"]):
        assert parser.parse_args(cmd).position_offset is None, cmd
        assert parser.parse_args(cmd + ["--position-offset", "-0.5"]).position_offset == -0.5
    print("test_cli_validates_the_offset_before_hardware OK")


def test_environment_variable_is_the_per_unit_default():
    """OF135I_POSITION_OFFSET holds a unit's value so it is not retyped on
    every command; the flag overrides it; a bad value is refused before
    hardware like a bad flag."""
    import os
    from types import SimpleNamespace
    from of135i import cli
    env = cli.POSITION_OFFSET_ENV
    saved = os.environ.get(env)
    try:
        os.environ.pop(env, None)
        assert cli._position_offset(SimpleNamespace(position_offset=None)) == 0.0
        assert cli._position_offset(SimpleNamespace()) == 0.0
        assert cli._position_offset(SimpleNamespace(position_offset=-0.5)) == -0.5
        os.environ[env] = " -0.5 "
        assert cli._position_offset(SimpleNamespace(position_offset=None)) == -0.5
        assert cli._position_offset(SimpleNamespace(position_offset=0.2)) == 0.2
        assert cli._position_offset(SimpleNamespace(position_offset=0.0)) == 0.0   # 0 given wins
        off, err = cli._resolve_position_offset(SimpleNamespace(position_offset=None))
        assert (off, err) == (-0.5, None), (off, err)
        os.environ[env] = ""
        assert cli._position_offset(SimpleNamespace(position_offset=None)) == 0.0
        os.environ[env] = "abc"
        off, err = cli._resolve_position_offset(SimpleNamespace(position_offset=None))
        assert off is None and err and env in err and "--position-offset" in err, err
        os.environ[env] = "7"
        off, err = cli._resolve_position_offset(SimpleNamespace(position_offset=None))
        assert off is None and "outside" in err, err
        # the whole command refuses before hardware on a bad environment value
        import io
        from contextlib import redirect_stderr
        parser = cli.build_parser()
        args = parser.parse_args(["scan", "--frame", "1", "-o", "/nonexistent/x.tiff"])
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = args.func(args)
        assert rc == 2 and "--position-offset" in buf.getvalue(), (rc, buf.getvalue())
        # and the sidecar records what the environment gave
        os.environ[env] = "-0.5"
        import argparse, json, tempfile
        from of135i import diag

        class _Sc:
            last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6]}
            last_cal_data = None

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "f1.tiff")
            a = argparse.Namespace(no_diag=False, frames="1", dpi=3600, ir=False,
                                   positive=False, rotate=0, no_clean=False,
                                   position_offset=None)
            cli._write_diag_sidecar(a, _Sc(), out, 1)
            side = json.loads(Path(diag.sidecar_path(out)).read_text())
            assert side["cli"]["position_offset"] == -0.5, side["cli"]
    finally:
        if saved is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = saved
    print("test_environment_variable_is_the_per_unit_default OK")


def test_sweep_meta_records_the_offset():
    from of135i import cli
    t = dual_tables(600)
    g, _ = holder.dual_sweep_geometry(
        1, 4, dpi=600, lines_per_chunk=t.LINES_PER_CHUNK,
        colour_crop_lines=image.align_shift(600), default_wire_lines=t.DEFAULT_LINES,
        offset_mm=-0.5)
    m = cli._sweep_meta(g, 64, 600, 0.75, position_offset_mm=-0.5)
    assert m["position_offset_mm"] == -0.5
    assert cli._sweep_meta(g, 64, 600, 0.75)["position_offset_mm"] == 0.0
    print("test_sweep_meta_records_the_offset OK")


def test_cli_refuses_a_bad_offset_with_zero_hardware_activity():
    """`scan --position-offset 9` and `digitize --position-offset 9` exit 2
    before opening the device (no USB in this process at all)."""
    import io
    from contextlib import redirect_stderr
    from of135i import cli
    parser = cli.build_parser()
    for cmd in (["scan", "--frame", "1", "--position-offset", "9", "-o", "/nonexistent/x.tiff"],
                ["digitize", "-o", "/nonexistent-dir", "--position-offset", "9"]):
        args = parser.parse_args(cmd)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = args.func(args)
        assert rc == 2, (cmd, rc, err.getvalue())
        assert "--position-offset" in err.getvalue(), err.getvalue()
    print("test_cli_refuses_a_bad_offset_with_zero_hardware_activity OK")


def test_sidecar_records_the_offset():
    import argparse
    import json
    import os
    import tempfile
    from of135i import cli, diag

    class _Sc:
        last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6]}
        last_cal_data = None

    os.environ.pop(cli.POSITION_OFFSET_ENV, None)
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "f1.tiff")
        for given, want in ((None, 0.0), (-0.5, -0.5)):
            args = argparse.Namespace(no_diag=False, frames="1", dpi=3600, ir=False,
                                      positive=False, rotate=0, no_clean=False)
            if given is not None:
                args.position_offset = given
            cli._write_diag_sidecar(args, _Sc(), out, 1)
            side = json.loads(Path(diag.sidecar_path(out)).read_text())
            assert side["cli"]["position_offset"] == want, side["cli"]
    print("test_sidecar_records_the_offset OK")


def test_hint_from_the_second_units_margins():
    """Issue #61, roll-006 frame 4 at 3600 dpi: lead 0.1035 mm, trail
    1.2154 mm -> the opening sits 0.56 mm late, offset -0.6 centres it."""
    from of135i import cli
    cov = aperture_crop.ApertureCoverage(
        verified=False, leading_line=14.67, trailing_line=5114.73,
        leading_margin_mm=0.10348844819124389, trailing_margin_mm=1.215429612409803,
        reason="leading margin below min_margin_mm (0.15 mm)", threshold=0.0)
    hint = aperture_crop.position_offset_hint(cov)
    assert hint == {"off_centre_mm": hint["off_centre_mm"], "offset_mm": -0.6}, hint
    assert math.isclose(hint["off_centre_mm"], (1.215429612409803 - 0.10348844819124389) / 2)
    text = cli._offset_hint(cov)
    assert "--position-offset -0.6" in text and "+0.56 mm" in text, text
    # the owner's unit (Test 119-like margins): nothing to suggest
    centred = aperture_crop.ApertureCoverage(
        True, 100.0, 5200.0, 0.72, 0.78, "ok", 0.0)
    assert aperture_crop.position_offset_hint(centred) is None
    assert cli._offset_hint(centred) == ""
    # an edge not found: no two margins, no hint
    none = aperture_crop.ApertureCoverage(False, None, None, None, None, "no aperture", 0.0)
    assert aperture_crop.position_offset_hint(none) is None
    print("test_hint_from_the_second_units_margins OK")


def test_failure_message_carries_the_hint():
    """The per-frame coverage failure on the plain path prints the hint
    when both margins are known (the leading one too small)."""
    import io
    import os
    import tempfile
    from contextlib import redirect_stderr
    from tests.test_overscan import _fake_scan_args
    from of135i import cli
    import numpy as np
    # a 3600 dpi plain frame, the second unit's frame 4 in shape: a sliver
    # of plastic before the opening (a 10-line ramp, as a real 3600 dpi
    # edge is spread; the detector bins to ~600 dpi) and ~1.2 mm after
    lpm = 3600 / 25.4
    lead, ramp = 30, 10
    trail = int(round(1.2 * lpm))
    n_ap = int(round(36.0 * lpm))
    sh = image.align_shift(3600)
    total = sh + lead + n_ap + trail + sh
    col = np.full(total, 800.0)
    s0 = sh + lead
    col[s0:s0 + n_ap] = 40000.0
    for i in range(ramp):
        col[s0 - ramp + i] = 800.0 + (40000.0 - 800.0) * i / ramp
        col[s0 + n_ap + i] = 40000.0 - (40000.0 - 800.0) * i / ramp
    col = col.astype(np.uint16)
    arr = np.repeat(np.repeat(col[:, None], 3762, axis=1)[:, :, None], 3, axis=2).astype("<u2")
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "f4.pnm")
        err = io.StringIO()
        with redirect_stderr(err):
            cov = cli._finish_plain_scan(_fake_scan_args(out), arr.tobytes(), 3762, out)
        assert cov is not None and not cov.verified, cov
        assert cov.leading_margin_mm is not None and cov.leading_margin_mm < 0.15, cov
        msg = err.getvalue()
        assert "--position-offset -0." in msg, msg
        assert not os.path.exists(out)
    print("test_failure_message_carries_the_hint OK")


def _dual_raw_with_short_lead(dpi=600):
    """A dual-light raw buffer (alternating IR/visible lines) whose
    opening starts about 0.10 mm into the window: 600 dpi, unbinned, so
    the plastic run before the opening is 2-3 lines."""
    import numpy as np
    from of135i import image as _image
    from of135i.device import dual_tables
    t = dual_tables(dpi)
    W = t.IMAGE_WIDTH
    sh = _image.align_shift(dpi)
    lpm = dpi / 25.4
    lead, trail = 3, int(round(1.3 * lpm))
    n_ap = int(round(36.0 * lpm))
    vis_lines = sh + lead + n_ap + trail + sh
    # whole chunks of wire lines; chunk = t.LINES_PER_CHUNK wire lines
    chunk_vis = t.LINES_PER_CHUNK // 2
    vis_lines = -(-vis_lines // chunk_vis) * chunk_vis
    vis = np.full(vis_lines, 800, dtype=np.uint16)
    s0 = sh + lead
    vis[s0:s0 + n_ap] = 20000
    irr = np.full(vis_lines, 800, dtype=np.uint16)
    irr[s0:s0 + n_ap] = 60000
    # interleave: even wire line = IR, odd = visible (image.split_ir)
    wire = np.empty(2 * vis_lines, dtype=np.uint16)
    wire[0::2] = irr
    wire[1::2] = vis
    buf = np.repeat(np.repeat(wire[:, None], W, axis=1)[:, :, None], 3, axis=2).astype("<u2")
    return buf.tobytes(), W


def test_failure_message_carries_the_hint_on_the_dual_and_digitize_paths():
    """The IR/digitize path is the one the reporting unit uses (3600 dpi
    dual): both its failure prints carry the hint."""
    import io
    import os
    import tempfile
    from contextlib import redirect_stderr, redirect_stdout
    from types import SimpleNamespace
    from of135i import cli
    raw, W = _dual_raw_with_short_lead(600)
    with tempfile.TemporaryDirectory() as d:
        # scan's dual path
        out = os.path.join(d, "f4.tiff")
        args = SimpleNamespace(dpi=600, overscan=0.75, positive=False, rotate=0,
                               no_clean=True, ir=True, output=out, frames=None)
        err, so = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(so):
            cov = cli._finish_dual_scan(args, raw, W, out, write_ir=True)
        assert cov is not None and not cov.verified, cov
        assert cov.leading_margin_mm is not None, cov
        # (600 dpi: a chunk is ~2 mm, so the trailing margin and the hint are large)
        assert "--position-offset -" in err.getvalue(), err.getvalue()
        assert not os.path.exists(out)
        # digitize's per-frame writer, dual branch
        out2 = os.path.join(d, "f5.tiff")
        dargs = SimpleNamespace(dpi=600, positive=False, rotate=0, no_clean=True, ir=True,
                                frames="1-6", film="135")
        err, so = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(so):
            main, irf, prev, cleaned, cov2 = cli._finish_digitize_frame(
                dargs, raw, W, out2, True, progress={})
        assert main is None and not cov2.verified, (main, cov2)
        assert "--position-offset -" in err.getvalue(), err.getvalue()
    print("test_failure_message_carries_the_hint_on_the_dual_and_digitize_paths OK")


def main():
    tests = [
        test_shifted_model_moves_every_aperture_by_the_offset,
        test_plain_geometry_feedl_moves_window_does_not,
        test_dual_geometry_every_profile_feedl_moves_window_does_not,
        test_the_second_units_shift_lands_inside_the_margin,
        test_sweep_geometry_feedl_moves_predicted_edges_do_not,
        test_offset_range_check,
        test_plain_wiring_offset_reaches_the_wire_and_the_diag,
        test_dual_wiring_offset_reaches_the_wire_and_the_diag,
        test_sweep_validation_uses_the_scanner_offset,
        test_cli_validates_the_offset_before_hardware,
        test_environment_variable_is_the_per_unit_default,
        test_sweep_meta_records_the_offset,
        test_cli_refuses_a_bad_offset_with_zero_hardware_activity,
        test_sidecar_records_the_offset,
        test_hint_from_the_second_units_margins,
        test_failure_message_carries_the_hint,
        test_failure_message_carries_the_hint_on_the_dual_and_digitize_paths,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
