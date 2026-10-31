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
from of135i import aperture, tables
from tests.test_dual_overscan import _RecordingDualScanner
from tests.test_overscan import _RecordingScanner

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

    def sweep(self, first, last, *, dpi, overscan_mm=None, sink, ir=True):
        self.events.append(("sweep", first, last, dpi))
        self.sweep_ir = ir
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
        for over in (dict(film="110"), dict(dpi=3600, ir=False, frames="1-6"), dict(strips=2),
                     dict(frames="1,3"), dict(frames="1-9"), dict(from_sweep=True, roll=None),
                     dict(sweep=False, keep_sweep=True), dict(from_frames=True)):
            rc, err = _forbid_hardware(_args(d, **over))
            assert rc == 2, (over, rc, err)
            assert err.startswith("error:"), (over, err)
        # WITHDRAWN after Test 112: the plain 3600 sweep is refused for every
        # range, with the test named, before the geometry is even looked at
        for fr in ("1-4", "1-6"):
            rc, err = _forbid_hardware(_args(d, dpi=3600, ir=False, frames=fr))
            assert rc == 2 and "Test 112" in err and "per-frame" in err, (fr, rc, err)
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


# ------------------------------------------------------- plain-3600 sweep
#
# Implemented offline; NOT hardware-verified (docs/whole-strip-sweep-design.md,
# "Plain-3600 sweep"). These tests pin the geometry, the gate, the device
# wiring, the profile/finder for a non-interleaved sweep and the CLI flow.

CHUNK = tables.IMAGE_CHUNK_LINES
CROP = image.align_shift(3600)


def _pgeom(first, last, overscan=holder.OVERSCAN_MM):
    return holder.plain_sweep_geometry(
        first, last, chunk_lines=CHUNK, colour_crop_lines=CROP,
        default_lines=tables.DEFAULT_LINES, overscan_mm=overscan)


def _k_end(g):
    """The furthest position under the fixed-start hypothesis."""
    return (g.feedl - tables.DEFAULT_LINES) + g.delivered_lines * 2


def test_plain_geometry_and_anchoring_gate():
    fid = holder.STRIP_FIDUCIAL
    over = holder.OVERSCAN_MM / holder.MM_PER_UNIT
    for first, last in ((1, 4), (2, 5)):
        g, wire = _pgeom(first, last)
        assert wire == g.chunks * CHUNK
        assert g.start_hwdpi == fid.leading_hwdpi(first) - over
        # FEEDL is the centre of the WHOLE window, not frame `first`'s
        assert g.feedl == round(g.start_hwdpi + g.delivered_lines)
        one = holder.overscan_geometry(
            first, res_units_per_line=2, chunk_lines=CHUNK,
            colour_crop_lines=CROP)
        assert g.feedl != one.feedl and abs(g.start_hwdpi - (one.feedl - one.delivered_lines)) <= 1.0
        assert g.end_hwdpi <= holder.FEEDL_CEILING and _k_end(g) <= holder.FEEDL_CEILING, \
            (first, last, g.end_hwdpi, _k_end(g))
        holder.check_feedl(g.feedl)
        assert tables.scan_lines_for_chunks(g.chunks) <= 0xFFFF
        # predicted apertures: lengths in lines, in visible (= wire) lines
        for k in range(first, last + 1):
            a, b = g.predicted_edges[k]
            want = fid.aperture_mm[k - 1] * 3600 / 25.4
            assert abs((b - a) - want) <= 1.0, (k, b - a, want)
        assert g.predicted_edges[first][0] > 0
        assert g.predicted_edges[last][1] < g.delivered_lines + 2 * CROP
    # the K-hypothesis ends, as in the spec: 1-4 ~60 500, 2-5 ~71 200
    assert abs(_k_end(_pgeom(1, 4)[0]) - 60500) < 300
    assert abs(_k_end(_pgeom(2, 5)[0]) - 71200) < 300
    for last in (5, 6):
        try:
            _pgeom(1, last)
        except safety.FeedlOutOfRangeError as e:
            m = str(e)
            assert f"plain sweep over apertures 1-{last} is refused until the plain " \
                   f"engine's anchoring of a long pass is hardware-verified" in m, m
            assert "fixed-start hypothesis" in m and "71490" in m
            assert "a range of four apertures (1-4, 2-5) is the longest allowed" in m, m
        else:
            raise AssertionError(f"1-{last} must be refused")
    # flipping the constant leaves only the centre hypothesis
    orig = holder.PLAIN_SWEEP_LONG_ANCHORING_VERIFIED
    holder.PLAIN_SWEEP_LONG_ANCHORING_VERIFIED = True
    try:
        try:
            _pgeom(1, 6)
        except safety.FeedlOutOfRangeError:
            raise AssertionError("1-6 must pass the gate once anchoring is verified "
                                 "(its centre-hypothesis end is inside the travel)")
        # ... but the centre end is still guarded
        try:
            _pgeom(1, 6, overscan=25.0)
        except safety.FeedlOutOfRangeError:
            pass
        else:
            raise AssertionError("the centre end must still be checked")
    finally:
        holder.PLAIN_SWEEP_LONG_ANCHORING_VERIFIED = orig
    for bad in ((1, 7), (0, 4)):
        try:
            _pgeom(*bad)
        except (safety.FrameOutOfRangeError, ValueError):
            pass
        else:
            raise AssertionError(bad)
    try:
        _pgeom(4, 1)
    except ValueError:
        pass
    else:
        raise AssertionError("first after last")
    # the 16-bit line guard: a (made-up) tiny chunk makes the count overflow
    try:
        holder.plain_sweep_geometry(
            1, 4, chunk_lines=1, colour_crop_lines=CROP,
            default_lines=tables.DEFAULT_LINES)
    except ValueError as e:
        assert "16-bit" in str(e), e
    else:
        raise AssertionError("line count above 0xFFFF must be refused")
    print("test_plain_geometry_and_anchoring_gate OK")


class _PlainSweepRec(_RecordingScanner):
    """_RecordingScanner whose _run_phase honours chunk_sink as _exec_ops
    does (buffers to the sink, empty list returned) and which records
    the _exec_ops calls."""

    def __init__(self):
        super().__init__()
        self.exec_calls = 0

    def _run_phase(self, phase, chunk_sink=None, **inject):
        self.runs.append((phase, inject))
        bufs = self._buffers_for(phase)
        if chunk_sink is not None:
            for b in bufs:
                chunk_sink(b)
            return []
        return bufs

    def _exec_ops(self, ops):
        self.exec_calls += 1
        return super()._exec_ops(ops)


def test_scan_plain_phase_sequence_is_frozen():
    """The golden phase list of Scanner._scan_plain, taken from the code
    BEFORE its calibration block was extracted into _plain_calibration."""
    fresh = ["cal_dark_a", "cal_dark_b", "cal_gain_check_a", "cal_gain_check_b",
             "cal_shading_measure", "cal_shading_upload", "position", "scan"]
    cached = ["cal_rewrite", "position", "scan"]
    rec = _PlainSweepRec()
    rec.s._scan_plain(frame=1)
    assert [ph.name for ph, _ in rec.runs] == fresh, [ph.name for ph, _ in rec.runs]
    assert rec.exec_calls == 2                  # cal_shading_verify's two halves
    assert rec.s.last_diag["calibration"] == "fresh"
    n = len(rec.runs)
    rec.s._scan_plain(frame=1)
    assert [ph.name for ph, _ in rec.runs[n:]] == cached
    assert rec.s.last_diag["calibration"] == "cached" and rec.s.last_cal_data is None
    print("test_scan_plain_phase_sequence_is_frozen OK")


def test_device_wiring_plain_sweep():
    g, wire = _pgeom(1, 4)
    rec = _PlainSweepRec()
    rec.s._prepared_for_scan = True            # as initialize() leaves it
    sink = []
    W, meta = rec.s._sweep_plain(1, 4, overscan_mm=holder.OVERSCAN_MM, sink=sink.append)
    names = [ph.name for ph, _ in rec.runs]
    assert names == ["cal_dark_a", "cal_dark_b", "cal_gain_check_a", "cal_gain_check_b",
                     "cal_shading_measure", "cal_shading_upload", "position", "scan"], names
    pos = next(inj for ph, inj in rec.runs if ph.name == "position")
    feedl = (pos["feedl_hi"][0] << 16) | (pos["feedl_mid"][0] << 8) | pos["feedl_lo"][0]
    assert feedl == g.feedl
    scan_inj = next(inj for ph, inj in rec.runs if ph.name == "scan")
    assert "lines_top" not in scan_inj
    n_lines = (scan_inj["lines_hi"][0] << 8) | scan_inj["lines_lo"][0]
    assert n_lines == tables.scan_lines_for_chunks(g.chunks)
    # exactly `chunks` image buffers, the drain NOT forwarded
    assert len(sink) == g.chunks and all(len(b) == tables.IMAGE_CHUNK_LEN for b in sink)
    d = rec.s.last_diag
    assert d["dual"] is False and d["frame"] is None and d["calibration"] == "fresh"
    assert d["sweep"]["chunk_count"] == g.chunks and d["sweep"]["extra_buffers"] == 1
    assert d["sweep"]["raw_bytes"] == g.chunks * tables.IMAGE_CHUNK_LEN
    assert d["lines"] == n_lines and d["feedl"] == g.feedl
    assert rec.s._park_scale == position_timeout_scale(tables, round(g.end_hwdpi))
    assert rec.s._park_scale > position_timeout_scale(tables, g.feedl)
    assert W == tables.IMAGE_WIDTH
    assert meta["geometry"] == g and meta["alternating"] is False and meta["dpi"] == 3600
    assert rec.s.last_cal_data is not None
    # a second sweep in the same session reuses the cache: cal_rewrite only
    n = len(rec.runs)
    sink2 = []
    rec.s._sweep_plain(1, 4, overscan_mm=holder.OVERSCAN_MM, sink=sink2.append)
    assert [ph.name for ph, _ in rec.runs[n:]] == ["cal_rewrite", "position", "scan"]
    assert rec.s.last_diag["calibration"] == "cached" and rec.s.last_cal_data is None
    assert len(sink2) == g.chunks
    # ... and so does a sweep after a plain scan
    rec2 = _PlainSweepRec()
    rec2.s._scan_plain(frame=1)
    n = len(rec2.runs)
    rec2.s._sweep_plain(1, 4, overscan_mm=holder.OVERSCAN_MM, sink=lambda b: None)
    assert [ph.name for ph, _ in rec2.runs[n:]] == ["cal_rewrite", "position", "scan"]
    assert rec2.s.last_diag["calibration"] == "cached"
    print("test_device_wiring_plain_sweep OK")


def test_public_plain_sweep_preconditions_and_dispatch():
    # ir=False only exists at 3600: refused before any phase or write
    rec = _PlainSweepRec()
    rec.s._prepared_for_scan = True
    try:
        rec.s.sweep(1, 4, dpi=2400, ir=False, sink=lambda b: None)
    except Exception as e:
        assert type(e).__name__ == "Of135iError" and "Test 112" in str(e), e
    else:
        raise AssertionError("plain sweep at 2400 dpi must be refused")
    assert not rec.runs and not rec.s.io.writes
    # the geometry gate still refuses a long range at the geometry level
    try:
        _pgeom(1, 6)
    except safety.FeedlOutOfRangeError:
        pass
    else:
        raise AssertionError("1-6 must be refused by the gate")
    # WITHDRAWN after Test 112: the public call refuses ir=False at 3600 too,
    # before any phase or write, and leaves the prepared flag alone
    try:
        rec.s.sweep(1, 4, dpi=3600, ir=False, sink=lambda b: None)
    except Exception as e:
        assert type(e).__name__ == "Of135iError" and "Test 112" in str(e), e
    else:
        raise AssertionError("the plain sweep must be refused (withdrawn after Test 112)")
    assert not rec.runs and not rec.s.io.writes and rec.s._prepared_for_scan is True
    print("test_public_plain_sweep_preconditions_and_dispatch OK")


def test_visible_profile_blockwise_and_wire_per_vis_1():
    rng = np.random.default_rng(2)
    shift = image.align_shift(3600)
    n, W = 80 + 4 * shift, 12
    mm = rng.integers(0, 60000, size=(n, W, 3)).astype("<u2")
    got = sweep.visible_profile(mm, dpi=3600, block_lines=7)
    lo = max(0, int(round(W / 2.0 - W * 0.25)))
    hi = min(W, max(lo + 1, int(round(W / 2.0 + W * 0.25))))
    want = np.roll(mm[:, lo:hi, 0].astype(np.float64).mean(axis=1), -shift)
    assert got.shape == (n,) and np.allclose(got, want, atol=1e-9)
    # a plain window: integers, no parity rounding, covers predicted +- overscan +- crop
    g, _ = _pgeom(1, 4)
    prof = _plain_profile(g)
    lpm = 3600 / 25.4
    odd = False
    for sh in (0.0, 1 / lpm, 3 / lpm, 0.51):
        shifts = {k: (sh, sh) for k in range(1, 5)}
        r = sweep.find_windows(_plain_profile(g, shifts=shifts), g, dpi=3600,
                               wire_per_vis=1)
        assert r.resolved, (sh, r.reason)
        for k, w in r.windows.items():
            assert isinstance(w.wire_start, int) and isinstance(w.wire_end, int)
            a, b = g.predicted_edges[k]
            a, b = a + r.offset_mm * lpm, b + r.offset_mm * lpm
            need = holder.OVERSCAN_MM * lpm + CROP
            assert w.wire_start <= max(0, a - need + 1) and w.wire_end >= min(len(prof), b + need - 1), (k, w, a, b)
            assert 0 <= w.wire_start < w.wire_end <= len(prof)
            odd = odd or (w.wire_start % 2 or w.wire_end % 2)
    assert odd, "plain windows are not parity-rounded: some bound must be odd"
    try:
        sweep.find_windows(prof, g, dpi=3600, wire_per_vis=3)
    except ValueError:
        pass
    else:
        raise AssertionError("wire_per_vis=3 must be refused")
    print("test_visible_profile_blockwise_and_wire_per_vis_1 OK")


def _plain_profile(g, *, shifts=None, missing=(), dark_level=800, lit=40000, dim=None):
    """Synthetic visible profile (one entry per line) of a plain sweep:
    lit runs at the predicted edges. ``dim[k] = (level, edge_mm)`` makes
    aperture k's first/last ``edge_mm`` a dim level and the rest ``lit``-ish
    middle level ``level`` -- see the study case below."""
    lpm = 3600 / 25.4
    n = g.chunks * CHUNK
    prof = np.full(n, dark_level, dtype=np.float64)
    for k, (a, b) in g.predicted_edges.items():
        if k in missing:
            continue
        s0, s1 = (shifts or {}).get(k, (0.0, 0.0))
        a2, b2 = int(round(a + s0 * lpm)), int(round(b + s1 * lpm))
        prof[a2:b2] = lit
        if dim and k in dim:
            mid, edge_mm, edge_level = dim[k]
            e = int(round(edge_mm * lpm))
            prof[a2:b2] = mid
            prof[a2:a2 + e] = edge_level
            prof[b2 - e:b2] = edge_level
    return prof


def test_plain_finder_study_case_pitch_located_edge_adjacent_content():
    """docs/bw-sweep-study.md 3.3: an aperture whose edge-adjacent content
    sits under the 8 % absolute step rule of the STRIP's range is lost to
    the sweep's edge finder and cut BY PITCH from its siblings' offset --
    and the window's own, smaller range then finds it."""
    g, _ = _pgeom(1, 4)
    rng_ = 40000.0 - 800.0
    edge_level = 800.0 + 0.05 * rng_            # 0.05 of the STRIP range
    mid_level = 800.0 + 0.25 * rng_             # edge = 0.20 of the window's own range
    prof = _plain_profile(g, dim={3: (mid_level, 1.5, edge_level)})
    r = sweep.find_windows(prof, g, dpi=3600, wire_per_vis=1)
    assert r.resolved, r.reason
    assert r.windows[3].located_by == "pitch", r.windows
    assert [r.windows[k].located_by for k in (1, 2, 4)] == ["edges"] * 3
    assert abs(r.offset_mm) < 0.05
    w = r.windows[3]
    win = prof[w.wire_start:w.wire_end]
    _, runs = aperture.apertures(win, 3600 / 25.4)
    assert len(runs) == 1, runs
    length_mm = (runs[0][1] - runs[0][0]) / (3600 / 25.4)
    assert abs(length_mm - holder.STRIP_FIDUCIAL.aperture_mm[2]) < 0.1, length_mm
    # the invariant: "never positions alone" -- a profile with NO plausible
    # run stays unresolved and cuts nothing although predicted edges exist
    flat = np.full(len(prof), 800.0)
    r2 = sweep.find_windows(flat, g, dpi=3600, wire_per_vis=1)
    assert not r2.resolved and not r2.windows and g.predicted_edges, r2.reason
    short = _plain_profile(g, dim=None)
    short[:] = 800.0
    a, _b = g.predicted_edges[2]
    short[int(a):int(a) + int(20 * 3600 / 25.4)] = 40000.0     # one 20 mm run
    r3 = sweep.find_windows(short, g, dpi=3600, wire_per_vis=1)
    assert not r3.resolved and not r3.windows, r3.reason
    print("test_plain_finder_study_case_pitch_located_edge_adjacent_content OK")


def test_plain_numbering_follows_measured_edges_not_position():
    """Everything +7 mm (the DPI-change shift): each window follows the
    MEASURED run, so aperture k's window brackets the shifted run and not
    the predicted position; with one run alone dark the offset still comes
    from the siblings and that window is only pitch-located."""
    g, _ = _pgeom(1, 4)
    lpm = 3600 / 25.4
    shifts = {k: (7.0, 7.0) for k in range(1, 5)}
    r = sweep.find_windows(_plain_profile(g, shifts=shifts), g, dpi=3600, wire_per_vis=1)
    assert r.resolved and abs(r.offset_mm - 7.0) < 0.05, r.reason
    for k in range(1, 4):       # aperture 4's shifted end leaves the profile
        a, b = g.predicted_edges[k]
        w = r.windows[k]
        assert w.located_by == "edges"
        assert w.wire_start > a + 6.0 * lpm and w.wire_end >= b + 7.0 * lpm - 1, (k, w)
    r = sweep.find_windows(_plain_profile(g, missing=(3,)), g, dpi=3600, wire_per_vis=1)
    assert r.resolved and r.windows[3].located_by == "pitch"
    print("test_plain_numbering_follows_measured_edges_not_position OK")


W_PLAIN = 64


def _synthetic_plain_sweep(missing=()):
    """Raw wire buffer of a plain 1-4 sweep (real line counts, W=64): every
    line visible, lit apertures with film specks."""
    g, wire = _pgeom(1, 4)
    n = g.chunks * CHUNK
    col = _plain_profile(g, missing=missing).astype(np.uint16)
    img = np.repeat(col[:, None], W_PLAIN, axis=1)
    for k, (a, b) in g.predicted_edges.items():
        if k in missing:
            continue
        r0 = int(round(a)) + 40 + 7 * k
        img[r0:r0 + 2, 2:4] = 65000
    raw = np.repeat(img[:, :, None], 3, axis=2).astype("<u2").tobytes()
    assert len(raw) == n * W_PLAIN * 6
    return raw, g


class _PlainFakeScanner(_SweepFakeScanner):
    def __init__(self, raw, g, events):
        super().__init__(raw, g, tables, events)

    def sweep(self, first, last, *, dpi, overscan_mm=None, sink, ir=True):
        assert ir is False, "the plain flow must call sweep(ir=False)"
        step = tables.IMAGE_CHUNK_LINES * W_PLAIN * 6
        sent = 0
        for i in range(0, len(self.raw), step):
            sink(self.raw[i:i + step])
            sent += 1
        assert sent == self.g.chunks, (sent, self.g.chunks)
        self.events.append(("sweep", first, last, dpi))
        self.last_diag["sweep"] = {"first": first, "last": last, "chunk_count": sent}
        return W_PLAIN, {"geometry": self.g, "width": W_PLAIN}


def _plain_args(d, **over):
    return _args(d, dpi=3600, ir=False, **over)


def test_e2e_plain_sweep_is_refused_before_hardware():
    """WITHDRAWN after Test 112: digitize --sweep --no-ir exits 2 before any
    hardware step, names the test, and creates nothing."""
    with tempfile.TemporaryDirectory() as d:
        rc, err = _forbid_hardware(_plain_args(d, keep_sweep=True))
        assert rc == 2 and "Test 112" in err and "per-frame" in err, (rc, err)
        assert not list(Path(d).iterdir()), "nothing may be created"
    print("test_e2e_plain_sweep_is_refused_before_hardware OK")


def _e2e_plain_sweep_products_identical_to_per_frame_writer_RETIRED():
    # Kept for the record of what the offline path produced before Test 112;
    # not run: the CLI refuses the plain sweep.
    raw, g = _synthetic_plain_sweep()
    with tempfile.TemporaryDirectory() as d:
        events = []
        rc, out, err = _run(_plain_args(d, keep_sweep=True), _PlainFakeScanner(raw, g, events))
        assert rc == 0, (rc, err, out)
        assert events == [("initialize", False, 3600), ("sweep", 1, 4, 3600), "eject"], events
        rdir = digitize.roll_dir(d, "", 1)
        rec = digitize.read_manifest(d)[-1]
        assert rec["status"] == "ok" and len(rec["per_frame"]) == 4, rec
        for e in rec["per_frame"]:
            assert e["coverage_verified"] is True and e["main"] and e["ir"] is None, e
            assert e["located_by"] == "edges", e
        sj = json.loads((rdir / "sweep.json").read_text())
        assert sj["geometry"]["dual"] is False and sj["geometry"]["wire_per_vis"] == 1
        assert sj["resolution"]["resolved"]
        assert not list(rdir.glob("*-ir*")), sorted(p.name for p in rdir.iterdir())
        mm = sweep.open_raw(rdir / "sweep.raw", W_PLAIN)
        for k in range(1, 5):
            w = sj["resolution"]["windows"][str(k)]
            win = sweep.SweepWindow(k, w["wire_start"], w["wire_end"], w["located_by"])
            with tempfile.TemporaryDirectory() as d2:
                out2 = os.path.join(d2, f"f{k}.tiff")
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    cli._finish_digitize_frame(
                        _plain_args(d2), bytes(sweep.window_bytes(mm, win)),
                        W_PLAIN, out2, False, progress={})
                for name in (f"f{k}.tiff", f"f{k}.overscan.tiff", f"f{k}-preview.tiff"):
                    assert (rdir / name).read_bytes() == (Path(d2) / name).read_bytes(), name
        del mm
        # --from-sweep rebuilds byte-identically from sweep.json alone
        before = {p.name: p.read_bytes() for p in rdir.iterdir() if p.suffix == ".tiff"}
        assert len(before) == 12, sorted(before)
        for p in rdir.glob("f*.tiff"):
            p.unlink()
        rc, err = _forbid_hardware(_plain_args(d, from_sweep=True))
        assert rc == 0, err
        assert {p.name: p.read_bytes() for p in rdir.iterdir() if p.suffix == ".tiff"} == before
    print("test_e2e_plain_sweep_products_identical_to_per_frame_writer OK")


def _e2e_plain_sweep_pitch_located_window_goes_through_coverage_RETIRED():
    """A window located BY PITCH is never verified coverage by itself: the
    per-frame writer's own check refuses an empty/dark aperture, no
    product is written, the sweep fails (exit 4) and the raw is kept."""
    raw, g = _synthetic_plain_sweep(missing=(3,))
    with tempfile.TemporaryDirectory() as d:
        rc, out, err = _run(_plain_args(d), _PlainFakeScanner(raw, g, []))
        rdir = digitize.roll_dir(d, "", 1)
        assert rc == 4, (rc, err)
        assert (rdir / "sweep.raw").exists() and "sweep.raw kept" in err
        assert not (rdir / "f3.tiff").exists() and (rdir / "f1.tiff").exists()
        e3 = digitize.read_manifest(d)[-1]["per_frame"][2]
        assert e3["coverage_verified"] is False and e3["located_by"] == "pitch", e3
    print("test_e2e_plain_sweep_pitch_located_window_goes_through_coverage OK")


def test_old_sweep_json_without_dual_is_a_dual_sweep():
    meta = {"dpi": 600, "width": 4, "first": 1, "last": 1, "lines_per_mm": 23.6,
            "overscan_mm": 0.75, "predicted_edges": {"1": [0, 1]}}
    assert bool(meta.get("dual", True)) is True
    m = cli._sweep_meta(_pgeom(1, 4)[0], 64, 3600, 0.75, dual=False)
    assert m["dual"] is False and m["wire_per_vis"] == 1
    m = cli._sweep_meta(_sgeom(1, 4, 600)[0], 64, 600, 0.75)
    assert m["dual"] is True and m["wire_per_vis"] == 2
    print("test_old_sweep_json_without_dual_is_a_dual_sweep OK")


def main():
    tests = [
        test_plain_geometry_and_anchoring_gate,
        test_scan_plain_phase_sequence_is_frozen,
        test_device_wiring_plain_sweep,
        test_public_plain_sweep_preconditions_and_dispatch,
        test_visible_profile_blockwise_and_wire_per_vis_1,
        test_plain_finder_study_case_pitch_located_edge_adjacent_content,
        test_plain_numbering_follows_measured_edges_not_position,
        test_e2e_plain_sweep_is_refused_before_hardware,
        test_old_sweep_json_without_dual_is_a_dual_sweep,
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
