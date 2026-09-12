#!/usr/bin/env python3
"""Offline tests for the dual-light A+C production geometry.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_dual_overscan.py

The dual profiles carry the same A+C contract as plain 3600 (Test 58),
expressed in VISIBLE lines: one IR and one visible line per physical
line position, so the transport advances (7200/dpi)/2 motor units per
wire line and the wire register is twice the visible count
(holder.dual_overscan_geometry). Covered here:

  - the geometry ledger for every profile x frames 1-6 (margins,
    transport ceiling, 24-bit register, chunk/parity invariants);
  - device wiring: Scanner._scan_dual's production default commands the
    geometry FEEDL and wire count, and explicit lines= keeps the
    historical capture grid (the documented diagnostic path);
  - the CLI dual contract: overscan artefacts always preserved, the
    visible and IR products cropped to the SAME lines (registration),
    fail-closed on a coverage failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import holder, image, tables
from of135i.device import Scanner, dual_tables

DPIS = (600, 1200, 2400, 3600, 7200)


def _geom(frame, dpi, overscan=holder.OVERSCAN_MM):
    t = dual_tables(dpi)
    return holder.dual_overscan_geometry(
        frame, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
        colour_crop_lines=image.align_shift(dpi),
        default_wire_lines=t.DEFAULT_LINES, overscan_mm=overscan)


# ------------------------------------------------------------------ ledger


def test_dual_ledger_every_profile_and_frame():
    """Margins, transport bound and register invariants for all 5 x 6."""
    for dpi in DPIS:
        t = dual_tables(dpi)
        assert t.LINES_PER_CHUNK % 2 == 0, (dpi, t.LINES_PER_CHUNK)
        crop = image.align_shift(dpi)
        for frame in range(1, 7):
            g, wire = _geom(frame, dpi)
            assert g.leading_margin_mm == holder.OVERSCAN_MM, (dpi, frame)
            assert g.trailing_margin_mm >= holder.OVERSCAN_MM - 1e-9, (
                dpi, frame, g.trailing_margin_mm)
            assert g.end_hwdpi < holder.FEEDL_CEILING, (dpi, frame, g.end_hwdpi)
            assert wire == g.chunks * t.LINES_PER_CHUNK, (dpi, frame)
            assert wire == 2 * (g.delivered_lines + 2 * crop), (dpi, frame)
            assert wire % 2 == 0 and wire <= 0xFFFFFF, (dpi, frame, wire)
            holder.check_feedl(g.feedl)
    print("test_dual_ledger_every_profile_and_frame OK")


def test_dual_geometry_refuses_bad_input():
    try:
        holder.dual_overscan_geometry(
            1, dpi=600, lines_per_chunk=97, colour_crop_lines=2,
            default_wire_lines=1764)
    except ValueError:
        pass
    else:
        raise AssertionError("odd lines_per_chunk must be refused")
    try:
        holder.dual_overscan_geometry(
            1, dpi=1000, lines_per_chunk=98, colour_crop_lines=2,
            default_wire_lines=1764)
    except ValueError:
        pass
    else:
        raise AssertionError("a dpi that does not divide 7200 must be refused")
    from of135i import safety
    try:
        _geom(7, 600)
    except safety.FrameOutOfRangeError:
        pass
    else:
        raise AssertionError("frame 7 must be refused")
    print("test_dual_geometry_refuses_bad_input OK")


# ----------------------------------------------------------- device wiring


class _RecordingDualScanner:
    """Scanner with _run_phase/_exec_ops replaced by recorders returning
    correctly shaped zero buffers, so _scan_dual runs end to end off
    hardware and every commanded value can be read back."""

    def __init__(self, t):
        from tests.test_calibrate import MockUsbIo
        self.t = t
        self.s = Scanner(MockUsbIo({}))
        self.runs = []  # (phase, inject)
        self.s._run_phase = self._run_phase           # type: ignore[method-assign]
        self.s._exec_ops = self._exec_ops             # type: ignore[method-assign]
        self.s._park = lambda *a, **k: None           # type: ignore[method-assign]
        self.s._gain_with_warmup = lambda *a, **k: (0x2E, 0x21, 0x29)  # type: ignore[method-assign]

    def _buffers_for(self, phase):
        t = self.t
        name = phase.name
        if name == "scan":
            n = sum(1 for o in phase.ops
                    if getattr(o, "data", None) == t.IMAGE_DESC_DATA)
            return [bytes(t.IMAGE_CHUNK_LEN) for _ in range(n)]
        if name in ("cal_dark_a", "cal_dark_b", "cal_white",
                    "cal_gain_check_a", "cal_gain_check_b"):
            return [bytes(2 * 5184 * 3 * 2)]
        if name in ("cal_shading_measure", "cal_shading_upload"):
            return [bytes(t.SHADING_LINES * t.IMAGE_WIDTH * 3 * 2)]
        return [bytes(0)]

    def _run_phase(self, phase, **inject):
        self.runs.append((phase, inject))
        return self._buffers_for(phase)

    def _exec_ops(self, ops):
        return [bytes(self.t.SHADING_LINES * self.t.IMAGE_WIDTH * 3 * 2)]

    def run(self, **kw):
        self.s._scan_dual(self.t, frame=1, **kw)
        pos = next(inj for ph, inj in self.runs if ph.name == "position")
        scan_ph, scan_inj = next((ph, inj) for ph, inj in self.runs
                                 if ph.name == "scan")
        feedl = (pos["feedl_hi"][0] << 16) | (pos["feedl_mid"][0] << 8) | pos["feedl_lo"][0]
        n_lines = ((scan_inj["lines_top"][0] << 16)
                   | (scan_inj["lines_hi"][0] << 8) | scan_inj["lines_lo"][0])
        n_desc = sum(1 for o in scan_ph.ops
                     if getattr(o, "data", None) == self.t.IMAGE_DESC_DATA)
        return feedl, n_lines, n_desc, self.s.last_diag


def test_dual_wiring_default_is_the_overscan_geometry():
    """The production default (no lines=) commands the geometry FEEDL and
    wire count -- checked on the coarsest (600) and finest-grained
    (ir/3600) profiles -- and NOT the retired capture grid."""
    for dpi in (600, 3600):
        t = dual_tables(dpi)
        g, wire = _geom(1, dpi)
        feedl, n_lines, n_desc, diag = _RecordingDualScanner(t).run()
        assert feedl == g.feedl, (dpi, feedl, g.feedl)
        assert n_lines == wire, (dpi, n_lines, wire)
        # tables_ir's tail issues one extra descriptor that the vendor
        # cancelled with no data (ir-analysis.md); only `chunks` buffers
        # are consumed either way (diag assertion below).
        assert n_desc in (g.chunks, g.chunks + 1), (dpi, n_desc, g.chunks)
        assert diag["chunk_count"] == g.chunks
        assert diag["overscan_mm"] == holder.OVERSCAN_MM
        assert diag["raw_bytes"] == g.chunks * t.IMAGE_CHUNK_LEN
        assert feedl != t.feedl_for_frame(1), dpi
    print("test_dual_wiring_default_is_the_overscan_geometry OK")


def test_dual_wiring_explicit_lines_keeps_capture_grid():
    """lines= is the documented diagnostic/capture-replay path: it keeps
    the historical FEEDL grid and the requested chunk rounding -- what
    the SANE wire-equality tests and long sweeps rely on."""
    t = dual_tables(600)
    want = 2 * t.LINES_PER_CHUNK
    feedl, n_lines, n_desc, diag = _RecordingDualScanner(t).run(lines=want)
    assert feedl == t.feedl_for_frame(1), feedl
    assert n_lines == want and n_desc == 2, (n_lines, n_desc)
    assert diag["overscan_mm"] is None
    print("test_dual_wiring_explicit_lines_keeps_capture_grid OK")


# ------------------------------------------------------------ CLI contract


def _synthetic_dual_raw(dpi, n_chunks, aperture_vis, clipped=False):
    """Alternating-line raw for `n_chunks` dual chunks at `dpi`: bright
    aperture between aperture_vis=(a, b) in VISIBLE-line indices, dark
    plastic outside (in both channels -- the open aperture is bright in
    IR too). `clipped` runs the aperture off the leading end."""
    t = dual_tables(dpi)
    wire = n_chunks * t.LINES_PER_CHUNK
    vis = wire // 2
    col = np.full(vis, 800, dtype=np.uint16)
    a, b = aperture_vis
    col[max(0, a):b] = 40000
    if clipped:
        col[:b] = 40000
    inter = np.empty(wire, dtype=np.uint16)
    inter[0::2] = col  # IR lines (even)
    inter[1::2] = col  # visible lines (odd)
    W = t.IMAGE_WIDTH
    img = np.repeat(inter[:, None], W, axis=1)
    arr = np.repeat(img[:, :, None], 3, axis=2).astype("<u2")
    return arr.tobytes(), W


def _dual_args(out, dpi, ir=True):
    import types
    return types.SimpleNamespace(
        dpi=dpi, overscan=holder.OVERSCAN_MM, positive=False, rotate=0,
        no_clean=True, ir=ir, output=out, frames=None)


def test_cli_dual_verified_writes_registered_pair():
    import os
    from of135i import cli
    dpi = 600
    # 12 chunks = 588 visible lines ~ 24.9 mm; aperture 100..500 visible
    # lines ~ 16.9 mm -- below the detector's real-aperture band, so use
    # a longer window: 20 chunks = 980 vis (~41 mm), aperture 100..800
    # (~29.6 mm > MIN).
    raw, W = _synthetic_dual_raw(dpi, 20, (100, 800))
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "f1.tiff")
        cov = cli._finish_dual_scan(_dual_args(out, dpi), raw, W, out, write_ir=True)
        assert cov is not None and cov.verified, cov.reason if cov else None
        ir_out = os.path.join(d, "f1-ir.tiff")
        over = cli._overscan_raw_path(out)
        ir_over = os.path.join(d, "f1-ir.overscan.tiff")
        for p in (out, ir_out, over, ir_over):
            assert os.path.exists(p), f"missing artefact {p}"
        # Registration: the visible product and the IR product are
        # cropped to the SAME lines -> same height (axis 0), and both
        # shorter than the full overscan frame.
        from PIL import Image
        h_vis = Image.open(out).size[1]
        h_ir = Image.open(ir_out).size[1]
        h_over = Image.open(over).size[1]
        assert h_vis == h_ir, (h_vis, h_ir)
        assert h_vis < h_over, (h_vis, h_over)
    print("test_cli_dual_verified_writes_registered_pair OK")


def test_cli_dual_failure_writes_no_products():
    import os
    from of135i import cli
    dpi = 600
    raw, W = _synthetic_dual_raw(dpi, 20, (0, 800), clipped=True)
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "f1.tiff")
        cov = cli._finish_dual_scan(_dual_args(out, dpi), raw, W, out, write_ir=True)
        assert cov is not None and not cov.verified
        assert not os.path.exists(out), "no visible product after a coverage failure"
        assert not os.path.exists(os.path.join(d, "f1-ir.tiff")), (
            "no IR product after a coverage failure")
        assert os.path.exists(cli._overscan_raw_path(out)), "overscan raw must be kept"
        assert os.path.exists(os.path.join(d, "f1-ir.overscan.tiff")), (
            "IR overscan raw must be kept")
    print("test_cli_dual_failure_writes_no_products OK")


def test_validate_overscan_covers_dual():
    from of135i import cli
    for dpi in DPIS:
        assert cli._validate_overscan(0.75, [1, 6], dpi=dpi, dual=True) is None, dpi
    assert cli._validate_overscan(0.75, [7], dpi=600, dual=True) is not None
    assert cli._validate_overscan(-1.0, [1], dpi=600, dual=True) is not None
    print("test_validate_overscan_covers_dual OK")


# ------------------------------------------ TIFF DPI metadata (per-axis)
#
# The 2400 dpi dual profile is anisotropic: 3600 dpi across the sensor,
# 2400 dpi along the transport (image.sampling_resolution). The TIFF's
# XResolution/YResolution must follow the delivered image's ACTUAL axes
# after the orientation transform. _finish_dual_scan orients the IR
# channel IDENTICALLY to the visible one (the --positive mirror+rot90 and
# --rotate go through _orient_pair for both), so both must carry the same
# (x, y) dpi -- the dual-path IR metadata bug stamped the IR with
# _axis_dpi(..., False) while its pixels were rotated like the visible.


def _small_dual_raw(dpi, vis_lines, aperture, W=8, clipped=False):
    """A small-WIDTH alternating IR/visible raw for the metadata tests:
    an asymmetric scene with distinguishable axes -- a bright aperture
    between aperture=(a, b) in visible-line indices, dark plastic outside,
    and a brightness ramp DOWN the width so a 90 deg turn is visible in
    the pixels too. Small W keeps the arrays tiny (the coverage detector
    only reads the central width band, so width does not affect the
    verdict). `clipped` runs the aperture off the leading end."""
    a, b = aperture
    col = np.full(vis_lines, 800, dtype=np.uint16)
    col[max(0, a):b] = 40000
    if clipped:
        col[:b] = 40000
    wire = vis_lines * 2
    inter = np.empty(wire, dtype=np.uint16)
    inter[0::2] = col  # IR lines (even)
    inter[1::2] = col  # visible lines (odd)
    ramp = np.linspace(1.0, 0.6, W)  # asymmetric across the width
    img = (inter[:, None].astype(np.float64) * ramp[None, :]).astype(np.uint16)
    arr = np.repeat(img[:, :, None], 3, axis=2).astype("<u2")
    return arr.tobytes(), W


def _meta_args(out, dpi, positive, rotate, ir=True):
    import types
    return types.SimpleNamespace(
        dpi=dpi, overscan=holder.OVERSCAN_MM, positive=positive, rotate=rotate,
        no_clean=True, ir=ir, output=out, frames=None)


def _xy_res(path):
    from PIL import Image
    im = Image.open(path)
    return int(im.tag_v2[282]), int(im.tag_v2[283])


def test_dual_ir_and_visible_share_dpi_axes_2400():
    """The real _finish_dual_scan export, read back from disk: at 2400 dpi
    the visible and IR products (and their overscan frames) carry the SAME
    per-axis dpi, matching cli._axis_dpi(args, args.positive), for
    positive off/on and every 90 deg rotation. This is the dual-path IR
    metadata regression (the IR frame used to claim the un-rotated axes)."""
    import os
    from of135i import cli
    dpi = 2400
    # ~3200 visible lines (~33.9 mm along) with a ~28.6 mm aperture: above
    # the detector's real-aperture minimum, margins well over min_margin.
    raw, W = _small_dual_raw(dpi, 3200, (300, 3000))
    for positive in (False, True):
        for rotate in (0, 90, 180, 270):
            args = _meta_args("f.tiff", dpi, positive, rotate)
            want = cli._axis_dpi(args, args.positive)
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "f1.tiff")
                args.output = out
                cov = cli._finish_dual_scan(args, raw, W, out, write_ir=True)
                assert cov is not None and cov.verified, (
                    positive, rotate, cov.reason if cov else None)
                vis = _xy_res(out)
                ir = _xy_res(os.path.join(d, "f1-ir.tiff"))
                vis_over = _xy_res(cli._overscan_raw_path(out))
                ir_over = _xy_res(os.path.join(d, "f1-ir.overscan.tiff"))
                assert vis == want, (positive, rotate, vis, want)
                assert ir == want, ("IR must match visible axes",
                                    positive, rotate, ir, want)
                assert vis_over == want and ir_over == want, (
                    positive, rotate, vis_over, ir_over, want)
    print("test_dual_ir_and_visible_share_dpi_axes_2400 OK")


def test_dual_overscan_metadata_on_coverage_failure_2400():
    """Even when coverage FAILS (no products written), the preserved
    visible and IR overscan frames still carry matching, orientation-
    correct per-axis dpi -- the metadata fix reaches the overscan writer
    (the required 'overscan export at a failed coverage check' case)."""
    import os
    from of135i import cli
    dpi = 2400
    raw, W = _small_dual_raw(dpi, 3200, (0, 3000), clipped=True)
    for positive in (False, True):
        args = _meta_args("f.tiff", dpi, positive, rotate=90)
        want = cli._axis_dpi(args, args.positive)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "f1.tiff")
            args.output = out
            cov = cli._finish_dual_scan(args, raw, W, out, write_ir=True)
            assert cov is not None and not cov.verified, positive
            assert not os.path.exists(out), "no product on failure"
            vis_over = _xy_res(cli._overscan_raw_path(out))
            ir_over = _xy_res(os.path.join(d, "f1-ir.overscan.tiff"))
            assert vis_over == want and ir_over == want, (
                positive, vis_over, ir_over, want)
    print("test_dual_overscan_metadata_on_coverage_failure_2400 OK")


def test_dual_isotropic_profiles_square_and_pixels_unchanged():
    """Isotropic dual profiles (600/1200/7200) keep X == Y == dpi and the
    metadata fix leaves pixels and dimensions untouched: the read-back
    visible and IR products equal the independently oriented+cropped
    arrays (proof the change is metadata-only)."""
    import os
    from PIL import Image
    from of135i import aperture_crop, cli, image
    for dpi in (600, 1200, 7200):
        # ~30 mm aperture in a ~40 mm window at each dpi.
        lpm = dpi / 25.4
        vis_lines = int(round(40 * lpm))
        a = int(round(4 * lpm))
        b = int(round(34 * lpm))
        raw, W = _small_dual_raw(dpi, vis_lines, (a, b))
        args = _meta_args("f.tiff", dpi, positive=False, rotate=90)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "f1.tiff")
            args.output = out
            cov = cli._finish_dual_scan(args, raw, W, out, write_ir=True)
            assert cov is not None and cov.verified, (dpi, cov.reason if cov else None)
            for p in (out, os.path.join(d, "f1-ir.tiff")):
                x, y = _xy_res(p)
                assert x == dpi and y == dpi, (dpi, p, x, y)
            # Recompute the expected oriented visible/IR products and compare
            # pixels, byte for byte.
            visible, ir = image.split_ir(raw, width=W, dpi=dpi)
            shift = round(24 * dpi / 7200)
            visible = image.align_channels(visible, dpi=dpi)
            if shift:
                ir = ir[shift:-shift]
            c = aperture_crop.measure_coverage(visible, dpi=dpi)
            vis_c = aperture_crop.crop_to_aperture(visible, c, dpi=dpi)
            ir_c = aperture_crop.crop_to_aperture(ir, c, dpi=dpi)
            k = args.rotate // 90
            exp_vis = np.ascontiguousarray(np.rot90(vis_c, k=k))
            exp_ir = np.ascontiguousarray(np.rot90(ir_c, k=k))
            exp_ir_rgb = np.repeat(exp_ir[:, :, None], 3, axis=2)
            # Read back the 16-bit pixels straight from the TIFF (PIL opens a
            # 16-bit RGB TIFF as 8-bit): the writer puts pixel data right
            # after the 8-byte header, row-major RGB16LE.
            def _pixels(path, shape):
                buf = Path(path).read_bytes()[8:8 + int(np.prod(shape)) * 2]
                return np.frombuffer(buf, dtype="<u2").reshape(shape)
            got_vis = _pixels(out, exp_vis.shape)
            got_ir = _pixels(os.path.join(d, "f1-ir.tiff"), exp_ir_rgb.shape)
            assert np.array_equal(got_vis, exp_vis), dpi
            assert np.array_equal(got_ir, exp_ir_rgb), dpi
            # Dimensions match what a square-pixel viewer expects (X == Y).
            assert Image.open(out).size == (exp_vis.shape[1], exp_vis.shape[0]), dpi
    print("test_dual_isotropic_profiles_square_and_pixels_unchanged OK")


def main():
    tests = [
        test_dual_ledger_every_profile_and_frame,
        test_dual_geometry_refuses_bad_input,
        test_dual_wiring_default_is_the_overscan_geometry,
        test_dual_wiring_explicit_lines_keeps_capture_grid,
        test_cli_dual_verified_writes_registered_pair,
        test_cli_dual_failure_writes_no_products,
        test_validate_overscan_covers_dual,
        test_dual_ir_and_visible_share_dpi_axes_2400,
        test_dual_overscan_metadata_on_coverage_failure_2400,
        test_dual_isotropic_profiles_square_and_pixels_unchanged,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
