#!/usr/bin/env python3
"""Offline tests for the whole-strip sweep (docs/whole-strip-sweep-design.md).

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_sweep.py

Covered (design doc section 7): the sweep geometry for every dual profile;
the device wiring (phase order, FEEDL = frame `first`'s, the scan line
count, one sink call per chunk, an empty returned list, the park budget
from the END position); the window finder on synthetic profiles (cases
a-g); byte-identity of a window's products with the per-frame writer;
the failure paths (unresolved sweep, unverified window, --from-sweep);
the shared calibration file; the "sweep.raw kept" note; and the CLI
validation (exit 2 before any hardware).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import cli, digitize, holder, image, safety, sweep, aperture_crop
from of135i.device import Scanner, dual_tables, position_timeout_scale
from of135i.tables import Op
from tests.test_dual_overscan import _RecordingDualScanner

DPIS = (600, 1200, 2400, 3600, 7200)
RANGES = ((1, 4), (1, 6), (2, 3))


def _sgeom(first, last, dpi, overscan=holder.OVERSCAN_MM):
    t = dual_tables(dpi)
    return holder.dual_sweep_geometry(
        first, last, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
        colour_crop_lines=image.align_shift(dpi),
        default_wire_lines=t.DEFAULT_LINES, overscan_mm=overscan)


# ---------------------------------------------------------------- geometry


def test_geometry_every_profile_and_range():
    for dpi in DPIS:
        t = dual_tables(dpi)
        for first, last in RANGES:
            g, wire = _sgeom(first, last, dpi)
            assert g.end_hwdpi < holder.FEEDL_CEILING, (dpi, first, last)
            assert wire % 2 == 0 and wire <= 0xFFFFFF, (dpi, wire)
            assert wire == g.chunks * t.LINES_PER_CHUNK, (dpi, first, last)
            fg, _ = holder.dual_overscan_geometry(
                first, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
                colour_crop_lines=image.align_shift(dpi),
                default_wire_lines=t.DEFAULT_LINES)
            assert g.feedl == fg.feedl, (dpi, first, last, g.feedl, fg.feedl)
            holder.check_feedl(g.feedl)
            ks = sorted(g.predicted_edges)
            assert ks == list(range(first, last + 1))
            leads = [g.predicted_edges[k][0] for k in ks]
            trails = [g.predicted_edges[k][1] for k in ks]
            assert leads == sorted(leads) and trails == sorted(trails)
            pitch = holder.STRIP_FIDUCIAL.pitch_hwdpi / (7200 // dpi)
            for a, b in zip(trails, trails[1:]):
                assert abs((b - a) - pitch) < 1e-6, (dpi, a, b, pitch)
            # the window reaches both outer apertures' overscan
            assert g.predicted_edges[first][0] > 0
            vis = g.chunks * (t.LINES_PER_CHUNK // 2)
            assert g.predicted_edges[last][1] < vis, (dpi, first, last)
    for bad in ((1, 7), (0, 4), (4, 1)):
        try:
            _sgeom(*bad, 600)
        except (safety.FrameOutOfRangeError, ValueError):
            pass
        else:
            raise AssertionError(f"{bad} must be refused")
    print("test_geometry_every_profile_and_range OK")


# ------------------------------------------------------------ device wiring


class _SweepRec(_RecordingDualScanner):
    """_RecordingDualScanner whose _run_phase honours chunk_sink the way
    _exec_ops does: buffers go to the sink, the returned list is empty."""

    def __init__(self, t):
        super().__init__(t)
        self.sink_buffers = []

    def _run_phase(self, phase, chunk_sink=None, **inject):
        self.runs.append((phase, inject))
        bufs = self._buffers_for(phase)
        if chunk_sink is not None:
            for b in bufs:
                chunk_sink(b)
            return []
        return bufs


def test_device_wiring_sweep():
    for dpi in (600, 3600):
        t = dual_tables(dpi)
        rec = _SweepRec(t)
        sink = []
        W, meta = rec.s._sweep_dual(t, first=1, last=4,
                                    overscan_mm=holder.OVERSCAN_MM,
                                    sink=sink.append)
        g, wire = _sgeom(1, 4, dpi)
        names = [ph.name for ph, _ in rec.runs]
        cal_names = [n for n in names if n.startswith("cal_")]
        assert names == cal_names + ["position", "scan"], names
        ref = _RecordingDualScanner(t)
        ref.run()
        ref_names = [ph.name for ph, _ in ref.runs]
        assert ref_names == names, (ref_names, names)       # same phase order
        pos = next(inj for ph, inj in rec.runs if ph.name == "position")
        feedl = (pos["feedl_hi"][0] << 16) | (pos["feedl_mid"][0] << 8) | pos["feedl_lo"][0]
        fg, _ = holder.dual_overscan_geometry(
            1, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
            colour_crop_lines=image.align_shift(dpi),
            default_wire_lines=t.DEFAULT_LINES)
        assert feedl == fg.feedl == g.feedl, (feedl, fg.feedl)
        scan_inj = next(inj for ph, inj in rec.runs if ph.name == "scan")
        n_lines = ((scan_inj["lines_top"][0] << 16) | (scan_inj["lines_hi"][0] << 8)
                   | scan_inj["lines_lo"][0])
        assert n_lines == wire, (n_lines, wire)
        # one sink call per chunk, nothing retained, no whole-sweep join
        assert len(sink) == g.chunks, (len(sink), g.chunks)
        assert all(len(b) == t.IMAGE_CHUNK_LEN for b in sink)
        d = rec.s.last_diag
        assert d["sweep"]["chunk_count"] == g.chunks
        assert d["sweep"]["raw_bytes"] == g.chunks * t.IMAGE_CHUNK_LEN
        assert d["frame"] is None and d["feedl"] == g.feedl
        # park budget from the END position, not from FEEDL
        assert rec.s._park_scale == position_timeout_scale(t, round(g.end_hwdpi))
        assert rec.s._park_scale > position_timeout_scale(t, g.feedl)
        assert W == t.IMAGE_WIDTH and meta["geometry"] == g
    print("test_device_wiring_sweep OK")


def test_sweep_logs_scanning_frame_per_aperture():
    """A frontend keys its progress on "scanning frame N": the sweep logs
    one such line per aperture, in order, as the pass reaches it, and the
    cut prints one "cutting frame N" line per aperture."""
    import logging
    t = dual_tables(3600)
    rec = _SweepRec(t)
    msgs = []

    class _H(logging.Handler):
        def emit(self, r):
            msgs.append(r.getMessage())
    h = _H()
    lg = logging.getLogger("of135i")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        rec.s._sweep_dual(t, first=1, last=4, overscan_mm=holder.OVERSCAN_MM,
                          sink=lambda b: None)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    import re
    frames = [int(m.group(1)) for m in
              (re.search(r"scanning frame (\d+)", x) for x in msgs) if m]
    assert frames == [1, 2, 3, 4], frames
    print("test_sweep_logs_scanning_frame_per_aperture OK")


def test_exec_ops_chunk_sink_streams_and_returns_empty():
    s = Scanner(__import__("tests.test_calibrate", fromlist=["x"]).MockUsbIo({}))

    class Dev:
        def ctrl_transfer(self, *a, **k):
            return 8

        def read(self, ep, length, timeout=0):
            return bytes([7]) * length

    s.io.dev = Dev()
    desc = b"\x00" * 8
    ops = ([Op("cw", br=0x04, wv=0x0082, wi=0, data=desc)]
           + [Op("bi", length=100) for _ in range(5)]
           + [Op("cw", br=0x04, wv=0x0082, wi=0, data=desc)]
           + [Op("bi", length=100) for _ in range(5)]
           + [Op("cw", br=0x04, wv=0x0082, wi=1, data=desc)])
    s.session.operations.append("sweep")
    got = []
    out = s._exec_ops(ops, chunk_sink=got.append)
    assert out == [], out
    assert [len(b) for b in got] == [500, 500], [len(b) for b in got]
    # without a sink the behaviour is unchanged
    assert [len(b) for b in s._exec_ops(ops)] == [500, 500]
    print("test_exec_ops_chunk_sink_streams_and_returns_empty OK")


def test_public_sweep_requires_initialize():
    from tests.test_calibrate import MockUsbIo
    s = Scanner(MockUsbIo({}))
    try:
        s.sweep(1, 4, dpi=600, sink=lambda b: None)
    except safety.OperationNotAllowedError:
        pass
    else:
        raise AssertionError("sweep() before initialize() must be refused")
    assert not s.io.writes
    print("test_public_sweep_requires_initialize OK")


# ------------------------------------------------------------ window finder


def _profile(g, dpi, *, shifts=None, missing=(), length_mm=None, dark_level=800,
             lit=40000):
    """Synthetic IR profile of a sweep: lit runs at the predicted aperture
    edges. shifts[k] = (start_shift_mm, end_shift_mm)."""
    lpm = dpi / 25.4
    t = dual_tables(dpi)
    n = g.chunks * (t.LINES_PER_CHUNK // 2)
    prof = np.full(n, dark_level, dtype=np.float64)
    for k, (a, b) in g.predicted_edges.items():
        if k in missing:
            continue
        s0, s1 = (shifts or {}).get(k, (0.0, 0.0))
        a2, b2 = int(round(a + s0 * lpm)), int(round(b + s1 * lpm))
        if length_mm is not None:
            b2 = a2 + int(round(length_mm * lpm))
        prof[a2:b2] = lit
    return prof


def test_finder_cases_a_to_g():
    dpi = 3600
    g, _ = _sgeom(1, 4, dpi)
    lpm = dpi / 25.4
    n_wire = 2 * len(_profile(g, dpi))

    def check_parity(res):
        for w in res.windows.values():
            assert w.wire_start % 2 == 0 and w.wire_end % 2 == 0, w
            assert 0 <= w.wire_start < w.wire_end <= n_wire, w

    # (a) four runs at the predicted positions shifted by +0.3 mm
    r = sweep.find_windows(_profile(g, dpi, shifts={k: (0.3, 0.3) for k in range(1, 5)}),
                           g, dpi=dpi)
    assert r.resolved and abs(r.offset_mm - 0.3) < 0.05, (r.reason, r.offset_mm)
    assert [r.windows[k].located_by for k in range(1, 5)] == ["edges"] * 4
    check_parity(r)
    # (b) run 1 shortened by 8 mm at its start (film end opaque in IR)
    r = sweep.find_windows(_profile(g, dpi, shifts={1: (8.0, 0.0)}), g, dpi=dpi)
    assert r.resolved and r.windows[1].located_by == "pitch", r.reason
    assert [r.windows[k].located_by for k in (2, 3, 4)] == ["edges"] * 3
    assert abs(r.offset_mm) < 0.05
    check_parity(r)
    # the pitch window still brackets the true aperture 1
    a1, b1 = g.predicted_edges[1]
    w1 = r.windows[1]
    assert w1.wire_start <= 2 * a1 and w1.wire_end >= 2 * b1, (w1, a1, b1)
    # (c) run 3 missing (dark)
    r = sweep.find_windows(_profile(g, dpi, missing=(3,)), g, dpi=dpi)
    assert r.resolved and r.windows[3].located_by == "pitch", r.reason
    assert {k for k, w in r.windows.items() if w.located_by == "edges"} == {1, 2, 4}
    check_parity(r)
    # (d) everything +7 mm (the DPI-change shift)
    r = sweep.find_windows(_profile(g, dpi, shifts={k: (7.0, 7.0) for k in range(1, 5)}),
                           g, dpi=dpi)
    assert r.resolved and abs(r.offset_mm - 7.0) < 0.05, (r.reason, r.offset_mm)
    check_parity(r)
    # (e) two runs 0.5 pitch apart from the grid -> unresolved, names the spread
    r = sweep.find_windows(_profile(g, dpi, shifts={2: (17.0, 17.0)}, missing=(3, 4)),
                           g, dpi=dpi)
    assert not r.resolved and "spread" in r.reason and not r.windows, r.reason
    # (f) no run reaches MIN_APERTURE_MM
    r = sweep.find_windows(_profile(g, dpi, length_mm=20.0), g, dpi=dpi)
    assert not r.resolved and not r.windows, r.reason
    # (g) parity on odd-shifted inputs: shifts of a single line
    for sh in (0.0, 1 / lpm, 3 / lpm, 0.51):
        r = sweep.find_windows(_profile(g, dpi, shifts={k: (sh, sh) for k in range(1, 5)}),
                               g, dpi=dpi)
        assert r.resolved, (sh, r.reason)
        check_parity(r)
    # a whole-strip offset beyond what the runs can disambiguate
    r = sweep.find_windows(_profile(g, dpi, shifts={k: (15.0, 15.0) for k in range(1, 5)}),
                           g, dpi=dpi)
    assert not r.resolved, r.offset_mm
    print("test_finder_cases_a_to_g OK")


def test_ir_profile_blockwise_equals_direct():
    """Block-wise reduction equals the direct one, and the stagger roll
    matches image.split_ir's: the profile of the IR array split_ir
    returns (as aperture_crop.along_strip_profile takes it) and
    ir_profile on the same bytes agree to the uint16 rounding, away from
    the wrapped ``shift`` lines at each end."""
    rng = np.random.default_rng(1)
    for dpi in (600, 3600):
        shift = image.align_shift(dpi)
        n_wire, W = 2 * (40 + 4 * shift), 12
        mm = rng.integers(0, 60000, size=(n_wire, W, 3)).astype("<u2")
        got = sweep.ir_profile(mm, dpi=dpi, block_lines=7)
        _, ir = image.split_ir(mm.tobytes(), width=W, dpi=dpi)
        want = aperture_crop.along_strip_profile(ir)
        assert got.shape == want.shape == (n_wire // 2,), (got.shape, want.shape)
        core = slice(shift, len(want) - shift) if shift else slice(None)
        assert np.abs(got[core] - want[core]).max() <= 0.5, dpi
    print("test_ir_profile_blockwise_equals_direct OK")


def test_finder_resolves_a_colour_staggered_ir_sweep():
    """The three IR channels are staggered along the strip by the colour
    line shift (R lags, B leads G by align_shift lines). On the first
    hardware sweep (2026-10-30) the un-registered channel mean turned
    every aperture edge into a ramp ~2 x shift lines wide and the edge
    finder found NO run, while the per-frame coverage on the same bytes
    verified every aperture. ir_profile must bring the channels into
    register first; this builds a staggered 1-4 sweep at 3600 dpi and
    requires (a) the registered profile to resolve all four apertures by
    their edges, (b) the naive channel mean to fail -- so the test is
    known to exercise the stagger."""
    dpi = 3600
    shift = image.align_shift(dpi)
    g, wire = _sgeom(1, 4, dpi)
    n_vis = wire // 2
    W = 16
    col = np.full(n_vis, 1300, dtype=np.float64)
    for k, (a, b) in g.predicted_edges.items():
        col[int(round(a)):int(round(b))] = 49000
    # G is the reference; R lags (its edge appears `shift` lines LATER),
    # B leads (`shift` lines earlier) -- the directions split_ir undoes.
    r = np.roll(col, +shift)
    bch = np.roll(col, -shift)
    ir = np.stack([r, col, bch], axis=-1)              # (n_vis, 3)
    mm = np.zeros((2 * n_vis, W, 3), dtype="<u2")
    mm[0::2] = np.repeat(ir[:, None, :], W, axis=1).astype("<u2")
    mm[1::2] = mm[0::2]
    prof = sweep.ir_profile(mm, dpi=dpi)
    res = sweep.find_windows(prof, g, dpi=dpi)
    assert res.resolved, res.reason
    assert [res.windows[k].located_by for k in range(1, 5)] == ["edges"] * 4, res.windows
    assert abs(res.offset_mm) < 0.1, res.offset_mm
    # (b) the un-registered mean does not resolve: the ramp hides the edges
    naive = mm[0::2, :, :].astype(np.float64).mean(axis=(1, 2))
    bad = sweep.find_windows(naive, g, dpi=dpi)
    assert not bad.resolved, "the naive profile unexpectedly resolved; the test no longer exercises the stagger"
    print("test_finder_resolves_a_colour_staggered_ir_sweep OK")


# ------------------------------------------------ end to end through the CLI

W_TEST = 64
DPI_E2E = 600


def _synthetic_sweep(shifts=None, missing=()):
    """Raw wire buffer of a 1-4 sweep at 600 dpi (real line counts,
    W=64): alternating IR/visible lines, lit apertures with film specks."""
    t = dual_tables(DPI_E2E)
    g, wire = _sgeom(1, 4, DPI_E2E)
    lpm = DPI_E2E / 25.4
    n_vis = wire // 2
    col = np.full(n_vis, 800, dtype=np.uint16)
    for k, (a, b) in g.predicted_edges.items():
        if k in missing:
            continue
        s0, s1 = (shifts or {}).get(k, (0.0, 0.0))
        col[int(round(a + s0 * lpm)):int(round(b + s1 * lpm))] = 40000
    ir = np.repeat(col[:, None], W_TEST, axis=1)
    vis = ir.copy()
    for k, (a, b) in g.predicted_edges.items():
        if k in missing:
            continue
        s0 = (shifts or {}).get(k, (0.0, 0.0))[0]
        r0 = int(round(a + s0 * lpm)) + 12 + 7 * k
        vis[r0:r0 + 2, 2:4] = 65000
    inter = np.empty((wire, W_TEST), dtype=np.uint16)
    inter[0::2] = ir
    inter[1::2] = vis
    raw = np.repeat(inter[:, :, None], 3, axis=2).astype("<u2").tobytes()
    return raw, g, t


class _SweepFakeScanner:
    park_mode = "verbatim"
    warmup_budget_s = 60.0

    def __init__(self, raw, g, t, events):
        self.raw, self.g, self.t, self.events = raw, g, t, events
        self.last_cal_data = {"white_line": np.arange(6, dtype=np.uint16),
                              "gain_codes": np.array([1, 2, 3], dtype=np.uint16)}
        self.last_diag = {
            "gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6],
            "dark_b_substituted": False, "calibration": "fresh",
            "white_dips": {"a": [], "b": []},
            "phase_seconds": {"cal_dark_a": 1.0, "position": 2.0, "scan": 3.0, "park": 4.0},
            "total_seconds": 12.0}

    def check_start_state(self): pass
    def is_magazine_present(self): return True

    def initialize(self, ir, dpi):
        self.events.append(("initialize", ir, dpi))

    def sweep(self, first, last, *, dpi, overscan_mm=None, sink):
        self.events.append(("sweep", first, last, dpi))
        n = len(self.raw)
        step = self.t.LINES_PER_CHUNK * W_TEST * 6
        sent = 0
        for i in range(0, n, step):
            sink(self.raw[i:i + step])
            sent += 1
        assert sent == self.g.chunks, (sent, self.g.chunks)
        self.last_diag["sweep"] = {"first": first, "last": last, "chunk_count": sent}
        return W_TEST, {"geometry": self.g, "width": W_TEST}

    def eject(self):
        self.events.append("eject")


def _args(d, **over):
    base = dict(out=d, prefix="", roll=1, force=False, assume_loaded=True,
                dpi=DPI_E2E, positive=True, rotate=0, ir=True, no_clean=True,
                no_diag=False, no_cal_data=False, park="verbatim",
                warmup_budget=None, frames="1-4", film="135", strips=1,
                placement=None, sweep=True, keep_sweep=False, from_sweep=False,
                from_frames=False)
    base.update(over)
    return argparse.Namespace(**base)


def _run(args, scanner):
    """Run _cmd_digitize with a fake scanner; returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    orig = cli._run_writing_session
    cli._run_writing_session = lambda body: body(scanner)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli._cmd_digitize(args)
    finally:
        cli._run_writing_session = orig
    return rc, out.getvalue(), err.getvalue()


def _forbid_hardware(args):
    """_cmd_digitize with every hardware entry point raising."""
    orig = cli._run_writing_session
    def boom(body):
        raise AssertionError("hardware touched")
    cli._run_writing_session = boom
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli._cmd_digitize(args)
    finally:
        cli._run_writing_session = orig
    return rc, err.getvalue()


PRODUCTS = ("f{k}.tiff", "f{k}-ir.tiff", "f{k}.overscan.tiff",
            "f{k}-ir.overscan.tiff", "f{k}-preview.tiff")


def test_e2e_sweep_products_identical_to_per_frame_writer():
    raw, g, t = _synthetic_sweep()
    with tempfile.TemporaryDirectory() as d:
        events = []
        args = _args(d, keep_sweep=True)
        rc, out, err = _run(args, _SweepFakeScanner(raw, g, t, events))
        assert rc == 0, (rc, err)
        assert events == [("initialize", True, DPI_E2E), ("sweep", 1, 4, DPI_E2E), "eject"], events
        rdir = digitize.roll_dir(d, "", 1)
        rec = digitize.read_manifest(d)[-1]
        assert rec["status"] == "ok" and len(rec["per_frame"]) == 4, rec
        assert rec["sweep"]["first"] == 1 and rec["sweep"]["raw_kept"] is True
        assert set(rec["sweep"]["seconds"]) == {"position", "scan", "park", "calibration", "total"}
        assert rec["sweep"]["seconds"]["calibration"] == 1.0
        need = {"frame", "main", "ir", "preview", "dust_cleaned", "stage",
                "coverage_verified", "coverage_reason", "coverage_source",
                "film_present", "saturated_share", "calibration", "gain_codes",
                "offset_codes", "dark_b_substituted", "located_by"}
        for e in rec["per_frame"]:
            assert need <= set(e), need - set(e)
            assert e["coverage_verified"] is True and e["main"], e
        sj = json.loads((rdir / "sweep.json").read_text())
        assert sj["resolution"]["resolved"] and set(sj["geometry"]) >= {
            "first", "last", "dpi", "feedl", "end_hwdpi", "wire_lines",
            "chunk_count", "overscan_mm", "width"}
        assert sj["timing"]["phase_seconds"]["scan"] == 3.0
        # byte identity with the per-frame writer on the same window bytes
        mm = sweep.open_raw(rdir / "sweep.raw", W_TEST)
        assert (rdir / "sweep.raw").read_bytes() == raw
        for k in range(1, 5):
            w = sj["resolution"]["windows"][str(k)]
            win = sweep.SweepWindow(k, w["wire_start"], w["wire_end"], w["located_by"])
            with tempfile.TemporaryDirectory() as d2:
                out2 = os.path.join(d2, f"f{k}.tiff")
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    cli._finish_digitize_frame(
                        _args(d2), bytes(sweep.window_bytes(mm, win)),
                        W_TEST, out2, True, progress={})
                for pat in PRODUCTS:
                    name = pat.format(k=k)
                    a, b = (rdir / name), Path(d2) / name
                    assert a.read_bytes() == b.read_bytes(), name
        del mm
        # one shared calibration: every fN.cal.npz exists, hard-linked
        f1 = rdir / "f1.cal.npz"
        for k in range(1, 5):
            fk = rdir / f"f{k}.cal.npz"
            assert fk.exists(), fk
            assert os.path.samefile(f1, fk) or fk.read_bytes() == f1.read_bytes()
            sc = json.loads((rdir / f"f{k}.diag.json").read_text())
            assert sc["cal_data"] == str(fk) and sc["cal_data_shared"] is True, sc["cal_data"]
            assert "white_dips" in sc and sc["sweep_window"]["aperture"] == k
        assert "sweep.raw kept" in err and str(rdir / "sweep.raw") in err and "GB" in err
    print("test_e2e_sweep_products_identical_to_per_frame_writer OK")


def test_clean_roll_removes_sweep_raw_and_says_nothing():
    raw, g, t = _synthetic_sweep()
    with tempfile.TemporaryDirectory() as d:
        rc, out, err = _run(_args(d), _SweepFakeScanner(raw, g, t, []))
        rdir = digitize.roll_dir(d, "", 1)
        assert rc == 0, err
        assert not (rdir / "sweep.raw").exists()
        assert (rdir / "sweep.json").exists()
        assert "sweep.raw kept" not in err and "sweep.raw kept" not in out
        rec = digitize.read_manifest(d)[-1]
        assert rec["sweep"]["raw_kept"] is False
    print("test_clean_roll_removes_sweep_raw_and_says_nothing OK")


def test_unresolved_sweep_keeps_everything():
    raw, g, t = _synthetic_sweep(shifts={2: (17.0, 17.0)}, missing=(3, 4))
    with tempfile.TemporaryDirectory() as d:
        rc, out, err = _run(_args(d), _SweepFakeScanner(raw, g, t, []))
        rdir = digitize.roll_dir(d, "", 1)
        assert rc == 4, (rc, err)
        assert not list(rdir.glob("f*.tiff")), sorted(p.name for p in rdir.iterdir())
        assert (rdir / "sweep.raw").read_bytes() == raw and (rdir / "sweep.json").exists()
        rec = digitize.read_manifest(d)[-1]
        assert rec["status"] == "failed", rec
        for e in rec["per_frame"]:
            assert e["coverage_verified"] is False
            assert e["coverage_reason"].startswith("sweep unresolved:") and "spread" in e["coverage_reason"]
            assert e["main"] is None
        assert "sweep.raw kept" in err
    print("test_unresolved_sweep_keeps_everything OK")


def test_unverified_window_keeps_sweep_raw():
    raw, g, t = _synthetic_sweep(missing=(3,))      # aperture 3 dark in IR
    with tempfile.TemporaryDirectory() as d:
        rc, out, err = _run(_args(d), _SweepFakeScanner(raw, g, t, []))
        rdir = digitize.roll_dir(d, "", 1)
        assert rc == 4, (rc, err)
        assert (rdir / "sweep.raw").exists() and "sweep.raw kept" in err
        assert not (rdir / "f3.tiff").exists() and (rdir / "f1.tiff").exists()
        rec = digitize.read_manifest(d)[-1]
        assert rec["status"] == "failed"
        e3 = rec["per_frame"][2]
        assert e3["coverage_verified"] is False and e3["located_by"] == "pitch", e3
    print("test_unverified_window_keeps_sweep_raw OK")


def test_from_sweep_rebuilds_byte_for_byte():
    raw, g, t = _synthetic_sweep()
    with tempfile.TemporaryDirectory() as d:
        rc, _, err = _run(_args(d, keep_sweep=True), _SweepFakeScanner(raw, g, t, []))
        assert rc == 0, err
        rdir = digitize.roll_dir(d, "", 1)
        before = {p.name: p.read_bytes() for p in rdir.iterdir()
                  if p.suffix == ".tiff"}
        assert len(before) == 20, sorted(before)
        for p in rdir.glob("f*.tiff"):
            p.unlink()
        rc, err = _forbid_hardware(_args(d, from_sweep=True, keep_sweep=False))
        assert rc == 0, err
        after = {p.name: p.read_bytes() for p in rdir.iterdir() if p.suffix == ".tiff"}
        assert after == before
        rec = digitize.read_manifest(d)[-1]
        assert rec["rebuilt_from_sweep"] is True and rec["status"] == "ok"
        assert (rdir / "sweep.raw").exists() and (rdir / "sweep.json").exists()
        assert "sweep.raw kept" in err
        for k in range(1, 5):
            assert (rdir / f"f{k}.cal.npz").exists()
        # refuses without the sweep files
        (rdir / "sweep.json").unlink()
        rc, err = _forbid_hardware(_args(d, from_sweep=True))
        assert rc == 2, rc
    print("test_from_sweep_rebuilds_byte_for_byte OK")


def test_clear_roll_outputs_sweep_files():
    with tempfile.TemporaryDirectory() as d:
        rdir = digitize.roll_dir(d, "", 1)
        rdir.mkdir(parents=True)
        for n in ("sweep.raw", "sweep.json", "f1.tiff", "family.tiff"):
            (rdir / n).write_bytes(b"x")
        gone = digitize.clear_roll_outputs(d, "", 1, include_sweep=False)
        assert gone == ["f1.tiff"] and (rdir / "sweep.raw").exists()
        gone = digitize.clear_roll_outputs(d, "", 1)
        assert sorted(gone) == ["sweep.json", "sweep.raw"]
        assert (rdir / "family.tiff").exists()
    print("test_clear_roll_outputs_sweep_files OK")


def test_cli_validation_exits_2_before_hardware():
    with tempfile.TemporaryDirectory() as d:
        for over in (dict(film="110"), dict(dpi=3600, ir=False), dict(strips=2),
                     dict(frames="1,3"), dict(frames="1-9"), dict(from_sweep=True, roll=None),
                     dict(sweep=False, keep_sweep=True), dict(from_frames=True)):
            rc, err = _forbid_hardware(_args(d, **over))
            assert rc == 2, (over, rc, err)
            assert err.startswith("error:"), (over, err)
        rc, err = _forbid_hardware(_args(d, dpi=3600, ir=False))
        assert "plain 3600 keeps the per-frame cycle" in err
        assert not list(Path(d).iterdir()), "nothing may be created before the checks"
    print("test_cli_validation_exits_2_before_hardware OK")


def test_sweep_busy_lock_exits_3_records_nothing():
    """The scanner lock held elsewhere: sweep digitize exits 3, no manifest
    record, no kept-raw note, no power-cycle advice."""
    with tempfile.TemporaryDirectory() as d:
        orig = cli._run_writing_session
        cli._run_writing_session = lambda body: 3
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = cli._cmd_digitize(_args(d))
        finally:
            cli._run_writing_session = orig
        e = err.getvalue()
        assert rc == 3, (rc, e)
        assert "not started" in e and "No power cycle is needed" in e, e
        assert "Power-cycle" not in e and "sweep.raw" not in e, e
        assert digitize.read_manifest(d) == []
    print("test_sweep_busy_lock_exits_3_records_nothing OK")


def main():
    tests = [
        test_sweep_busy_lock_exits_3_records_nothing,
        test_geometry_every_profile_and_range,
        test_device_wiring_sweep,
        test_sweep_logs_scanning_frame_per_aperture,
        test_exec_ops_chunk_sink_streams_and_returns_empty,
        test_public_sweep_requires_initialize,
        test_finder_cases_a_to_g,
        test_ir_profile_blockwise_equals_direct,
        test_finder_resolves_a_colour_staggered_ir_sweep,
        test_e2e_sweep_products_identical_to_per_frame_writer,
        test_clean_roll_removes_sweep_raw_and_says_nothing,
        test_unresolved_sweep_keeps_everything,
        test_unverified_window_keeps_sweep_raw,
        test_from_sweep_rebuilds_byte_for_byte,
        test_clear_roll_outputs_sweep_files,
        test_cli_validation_exits_2_before_hardware,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
