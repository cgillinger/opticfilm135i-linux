#!/usr/bin/env python3
"""Offline tests for the mounted-slide path (no hardware).

The slide geometry (holder.SLIDE_FIDUCIAL / slide_dual_geometry) is the
strip path's own dual geometry with a different fiducial model, so these
pin (a) the documented arithmetic, (b) the per-profile FEEDL grid, (c) the
refusals, including the stricter slide-only travel check, and (d) the
per-unit position offset (issue #61, FiducialModel.shifted), which must
move every slide window and be checked against the travel ceiling.

Then its caller (issue #70 step 1): Scanner.scan(slide=True) commands
exactly that geometry with the same phases as a strip frame, refuses
before the operation (nothing sent, session usable), and `scan --holder
slide` maps --frame/--frames to openings 1-4, refuses plain 3600,
--film 110 and --positive before the device opens, and writes the full
overscan frame(s) with a slide sidecar.

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
        # Slide 4 sits 175..307 steps (0.62..1.08 mm) inside the ceiling
        # on every profile, so +1.5 mm must be refused by the travel check.
        _raises(safety.FeedlOutOfRangeError, _slide_geom, 4, dpi, offset_mm=1.5)
        _slide_geom(3, dpi, offset_mm=1.5)
        for bad in (holder.POSITION_OFFSET_MAX_MM + 0.1, float("nan")):
            _raises(ValueError, _slide_geom, 1, dpi, offset_mm=bad)
    print("test_slide_position_offset_moves_every_feedl_and_is_travel_checked OK")


# ------------------------------------------------- device: scan(slide=True)


def _wire_feedl(runs):
    pos = next(inj for ph, inj in runs if ph.name == "position")
    return (pos["feedl_hi"][0] << 16) | (pos["feedl_mid"][0] << 8) | pos["feedl_lo"][0]


def _wire_lines(runs):
    inj = next(inj for ph, inj in runs if ph.name == "scan")
    return (inj["lines_top"][0] << 16) | (inj["lines_hi"][0] << 8) | inj["lines_lo"][0]


def test_scan_dual_slide_commands_the_slide_geometry():
    """_scan_dual(slide=True) commands exactly slide_dual_geometry's FEEDL
    and wire line count for openings 1-4, with and without the per-unit
    offset; the diag records the holder, the slide overscan and the offset.
    Same phases as a strip frame: only the FEEDL and the line count differ."""
    from tests.test_dual_overscan import _RecordingDualScanner
    for dpi in (600, 3600):
        t = dual_tables(dpi)
        strip = _RecordingDualScanner(t)
        strip.s._scan_dual(t, frame=1)
        strip_phases = [ph.name for ph, _ in strip.runs]
        for off in (0.0, -0.5):
            for n in range(1, 5):
                rec = _RecordingDualScanner(t)
                rec.s.position_offset_mm = off
                rec.s._scan_dual(t, frame=n, slide=True)
                g, wire = _slide_geom(n, dpi, offset_mm=off)
                assert _wire_feedl(rec.runs) == g.feedl, (dpi, off, n)
                assert _wire_lines(rec.runs) == wire, (dpi, off, n)
                assert [ph.name for ph, _ in rec.runs] == strip_phases, (dpi, n)
                d = rec.s.last_diag
                assert d["holder"] == "slide" and d["frame"] == n, d.get("holder")
                assert d["overscan_mm"] == holder.SLIDE_OVERSCAN_MM
                assert d["position_offset_mm"] == off and d["feedl"] == g.feedl
        # The strip path's diag is unchanged: no holder key there.
        assert "holder" not in strip.s.last_diag
    print("test_scan_dual_slide_commands_the_slide_geometry OK")


def _armed_scanner():
    """A Scanner whose public scan() gets past its preconditions, with
    every phase recorded instead of sent."""
    from tests.test_calibrate import MockUsbIo
    from of135i.device import Scanner
    s = Scanner(MockUsbIo({}))
    s._prepared_for_scan = True
    runs = []
    s._run_phase = lambda ph, **k: runs.append(ph) or [b""]   # type: ignore[method-assign]
    return s, runs


def test_public_scan_slide_refusals_send_nothing():
    """Every slide refusal happens in the public scan() BEFORE the
    operation: nothing is run, nothing written, the session is not failed
    (it can still scan)."""
    cases = (
        (dict(frame=1, slide=True), ValueError, "dual-light"),            # plain 3600
        (dict(frame=5, ir=True, slide=True), safety.FrameOutOfRangeError, None),
        (dict(frame=0, dpi=600, slide=True), safety.FrameOutOfRangeError, None),
        (dict(frame=1, ir=True, lines=1000, slide=True), ValueError, "lines="),
        (dict(frame=4, dpi=600, overscan_mm=2.5, slide=True), safety.FeedlOutOfRangeError, None),
    )
    for kw, exc, text in cases:
        s, runs = _armed_scanner()
        e = _raises(exc, s.scan, **kw)
        if text:
            assert text in str(e), (kw, e)
        assert not runs and not s.io.writes, kw
        assert s.session.snapshot()["state"] != "failed", kw
        assert s._prepared_for_scan, kw                 # not consumed
    # The per-unit offset is part of the pre-check: +1.5 mm pushes slide 4
    # past the ceiling and is refused before anything runs.
    s, runs = _armed_scanner()
    s.position_offset_mm = 1.5
    _raises(safety.FeedlOutOfRangeError, s.scan, frame=4, dpi=600, slide=True)
    assert not runs and not s.io.writes
    print("test_public_scan_slide_refusals_send_nothing OK")


def test_public_scan_slide_reaches_the_dual_path_with_the_slide_default():
    """scan(slide=True) hands _scan_dual the slide flag and the slide
    default overscan; slide=False (the default) is the strip call as before."""
    calls = []
    for kw in (dict(frame=2, ir=True, slide=True), dict(frame=2, dpi=600, slide=True),
               dict(frame=2, ir=True, overscan_mm=1.2, slide=True), dict(frame=2, ir=True)):
        s, _ = _armed_scanner()
        s._scan_dual = lambda t, **k: calls.append(k) or (b"", 0, {})   # type: ignore[method-assign]
        s.scan(**kw)
    assert calls[0] == dict(frame=2, lines=None, overscan_mm=holder.SLIDE_OVERSCAN_MM, slide=True), calls[0]
    assert calls[1]["overscan_mm"] == holder.SLIDE_OVERSCAN_MM and calls[1]["slide"]
    assert calls[2]["overscan_mm"] == 1.2 and calls[2]["slide"]
    assert calls[3] == dict(frame=2, lines=None, overscan_mm=holder.OVERSCAN_MM, slide=False), calls[3]
    print("test_public_scan_slide_reaches_the_dual_path_with_the_slide_default OK")


# ------------------------------------------------------------ CLI: --holder


def _cli_rc(argv, monkey_session=True):
    """Run `of135i <argv>`; returns (rc, stderr, stdout). The writing
    session is replaced by a tripwire so a test can prove a refusal never
    opened the device."""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    from of135i import cli
    opened = []
    saved = cli._run_writing_session
    if monkey_session:
        cli._run_writing_session = lambda body: opened.append(body) or 0
    try:
        args = cli.build_parser().parse_args(argv)
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = args.func(args)
    finally:
        cli._run_writing_session = saved
    return rc, err.getvalue(), out.getvalue(), opened


def test_cli_holder_option_parses_and_strip_stays_the_default():
    from of135i import cli
    p = cli.build_parser()
    assert p.parse_args(["scan", "--frame", "1", "-o", "x.tiff"]).holder == "strip"
    assert p.parse_args(["scan", "--holder", "slide", "--frame", "4", "--ir",
                         "-o", "x.tiff"]).holder == "slide"
    assert cli._parse_frames("1-4", holder.SLIDE) == [1, 2, 3, 4]
    assert cli._parse_frames("1-6") == [1, 2, 3, 4, 5, 6]          # strip default
    e = _raises(ValueError, cli._parse_frames, "3-5", holder.SLIDE)
    assert "mounted-slide holder" in str(e) and "1-4" in str(e), e
    print("test_cli_holder_option_parses_and_strip_stays_the_default OK")


def test_cli_slide_refusals_exit_2_before_the_device_opens(tmp=None):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        o = f"{d}/s.tiff"
        cases = (
            (["--frame", "1"], "dual-light"),                       # plain 3600
            (["--frame", "5", "--ir"], "1-4"),
            (["--frames", "1-6", "--dpi", "600"], "1-4"),
            (["--frame", "1", "--ir", "--positive"], "--positive"),
            (["--frame", "1", "--ir", "--film", "110"], "110"),
            (["--frame", "4", "--dpi", "600", "--overscan", "2.5"], "slide 4"),
            (["--frame", "4", "--dpi", "1200", "--position-offset", "1.5"], "--position-offset"),
        )
        for extra, text in cases:
            rc, err, _out, opened = _cli_rc(["scan", "--holder", "slide", *extra, "-o", o])
            assert rc == 2, (extra, rc, err)
            assert text in err, (extra, err)
            assert not opened, extra
        # The --positive refusal says why and that nothing was sent.
        _rc, err, _o, _ = _cli_rc(["scan", "--holder", "slide", "--frame", "1", "--ir",
                                  "--positive", "-o", o])
        assert "invert" in err and "nothing was sent" in err.lower(), err
        # Valid slide and strip commands reach the device session.
        for argv in (["--holder", "slide", "--frames", "1-4", "--ir"],
                     ["--holder", "slide", "--frame", "4", "--dpi", "600"],
                     ["--holder", "slide", "--frame", "2", "--ir", "--position-offset", "-0.5"],
                     ["--frame", "6"], ["--frames", "1-6", "--ir"],
                     ["--frame", "1", "--positive"]):
            rc, err, _out, opened = _cli_rc(["scan", *argv, "-o", o])
            assert rc == 0 and len(opened) == 1, (argv, rc, err)
    print("test_cli_slide_refusals_exit_2_before_the_device_opens OK")


def _slide_dual_raw(dpi):
    """A synthetic dual-light raw buffer the size of a slide window."""
    import numpy as np
    t = dual_tables(dpi)
    g, wire = _slide_geom(1, dpi)
    W = t.IMAGE_WIDTH
    lines = np.full(wire, 3000, dtype=np.uint16)
    lines[wire // 4: 3 * wire // 4] = 40000
    buf = np.repeat(np.repeat(lines[:, None], W, axis=1)[:, :, None], 3, axis=2).astype("<u2")
    return buf.tobytes(), W


class _FakeSlideScanner:
    """Stands in for the device inside _cmd_scan's session body."""

    def __init__(self, dpi):
        self.dpi = dpi
        self.calls = []
        self.last_diag = None
        self.last_cal_data = None
        self.position_offset_mm = 0.0

    def check_start_state(self):
        pass

    def is_magazine_present(self):
        return True

    def initialize(self, **k):
        self.calls.append(("initialize", k))

    def scan(self, **k):
        self.calls.append(("scan", k))
        raw, W = _slide_dual_raw(self.dpi)
        self.last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6],
                          "holder": "slide", "frame": k["frame"]}
        return raw, W, {"width": W, "alternating": True, "dpi": self.dpi}

    def eject(self):
        self.calls.append(("eject", {}))


def test_cli_slide_scan_writes_the_overscan_pair_and_a_slide_sidecar():
    """One opening end to end off hardware: scan(slide=True) with the
    slide default overscan and the unit offset, the full overscan frame(s)
    written, no aperture product (the strip crossbar check does not apply
    to a slide holder), sidecar coverage.applicable False, holder recorded,
    exit 0."""
    import json
    import os
    import tempfile
    from of135i import cli, diag
    os.environ.pop(cli.POSITION_OFFSET_ENV, None)
    fake = _FakeSlideScanner(600)
    saved = cli._run_writing_session
    with tempfile.TemporaryDirectory() as d:
        o = os.path.join(d, "s.tiff")
        try:
            cli._run_writing_session = lambda body: body(fake)
            rc, err, out, _ = _cli_rc(["scan", "--holder", "slide", "--frames", "3",
                                       "--dpi", "600", "--ir", "--no-clean",
                                       "--position-offset", "-0.5", "-o", o],
                                      monkey_session=False)
        finally:
            cli._run_writing_session = saved
        assert rc == 0, (rc, err)
        scans = [k for name, k in fake.calls if name == "scan"]
        assert scans == [dict(frame=3, ir=True, dpi=600,
                              overscan_mm=holder.SLIDE_OVERSCAN_MM, slide=True)], scans
        stem = os.path.join(d, "s-f3")
        assert os.path.exists(stem + ".overscan.tiff")
        assert os.path.exists(stem + "-ir.overscan.tiff")
        assert not os.path.exists(stem + ".tiff") and not os.path.exists(stem + "-ir.tiff")
        side = json.loads(open(diag.sidecar_path(stem + ".tiff")).read())
        assert side["coverage"]["applicable"] is False, side["coverage"]
        assert side["cli"]["holder"] == "slide" and side["holder"] == "slide"
        assert side["cli"]["position_offset"] == -0.5
        assert "not checked" in out, out
    print("test_cli_slide_scan_writes_the_overscan_pair_and_a_slide_sidecar OK")


def main():
    tests = [
        test_slide_model_matches_documented_numbers,
        test_slide_feedl_grid_every_profile,
        test_slide_ledger_default_every_profile,
        test_slide_geometry_refusals,
        test_slide_position_offset_moves_every_feedl_and_is_travel_checked,
        test_scan_dual_slide_commands_the_slide_geometry,
        test_public_scan_slide_refusals_send_nothing,
        test_public_scan_slide_reaches_the_dual_path_with_the_slide_default,
        test_cli_holder_option_parses_and_strip_stays_the_default,
        test_cli_slide_refusals_exit_2_before_the_device_opens,
        test_cli_slide_scan_writes_the_overscan_pair_and_a_slide_sidecar,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
