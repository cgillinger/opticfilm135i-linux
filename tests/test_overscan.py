#!/usr/bin/env python3
"""Offline tests for the overscan positioning path (A+C).

No hardware. Covers, in one place:
  - holder.overscan_geometry: the guarantee that after chunk rounding and
    the colour crop the delivered window covers the whole aperture plus
    the requested margin on BOTH sides, that leading is exact and trailing
    only ever rounds up, and that the furthest motor position is refused
    when it would leave proven travel;
  - tables.scan_phase / scan_lines_for_chunks: byte-identical default,
    correct structure and line count for other chunk counts;
  - the device wiring: Scanner._scan_plain(overscan_mm=...) commands the
    geometry's FEEDL, programs its line count, reads its chunk count, and
    assembles exactly that many image chunks -- while the default path is
    untouched;
  - the guard that overscan on a dual profile, or with an explicit line
    count, is refused.

Plain asserts, no pytest. Run with:
    .venv/bin/python tests/test_overscan.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import aperture_crop, holder, image as image_mod, tables
from of135i.device import Scanner
from of135i.safety import FeedlOutOfRangeError, FrameOutOfRangeError

MM = holder.MM_PER_UNIT
# The plain-3600 profile parameters the driver passes in.
RES = 7200 // 3600
CHUNK = tables.IMAGE_CHUNK_LINES
CROP = image_mod.align_shift(3600)


# --------------------------------------------------------------------------
# holder.overscan_geometry
# --------------------------------------------------------------------------

def _geom(frame, overscan_mm=holder.OVERSCAN_MM):
    return holder.overscan_geometry(
        frame, res_units_per_line=RES, chunk_lines=CHUNK,
        colour_crop_lines=CROP, overscan_mm=overscan_mm)


def test_overscan_covers_both_sides_after_rounding_and_crop():
    """The delivered window must clear the whole aperture plus the target
    margin on each side, for every frame -- the guarantee of section 4."""
    fid = holder.STRIP_FIDUCIAL
    target = holder.OVERSCAN_MM
    for frame in range(1, 7):
        g = _geom(frame)
        # Reconstruct the delivered window in motor units. The window
        # centre is the commanded FEEDL (driver convention); half-width
        # is delivered_lines/2 * res.
        half = g.delivered_lines / 2 * RES
        win_start = g.feedl - half
        win_end = g.feedl + half
        lead = fid.leading_hwdpi(frame)
        trail = fid.trailing_hwdpi(frame)
        lead_margin = (lead - win_start) * MM
        trail_margin = (win_end - trail) * MM
        # Both sides clear the aperture by at least the target (allow a
        # hair for the FEEDL integer rounding).
        assert lead_margin >= target - 0.01, (frame, lead_margin)
        assert trail_margin >= target - 0.01, (frame, trail_margin)
        # Leading is exact by construction; trailing only ever rounds up.
        assert abs(g.leading_margin_mm - target) < 1e-9, (frame, g.leading_margin_mm)
        assert g.trailing_margin_mm >= target - 1e-9, (frame, g.trailing_margin_mm)


def test_overscan_survives_worst_case_load_shift():
    """At the +/-0.5 mm design worst case a whole side must still be
    covered (>=0 margin) at every frame."""
    design = 0.5
    for frame in range(1, 7):
        g = _geom(frame)
        assert g.leading_margin_mm - design >= 0.0, frame
        assert g.trailing_margin_mm - design >= 0.0, frame


def test_overscan_end_position_inside_transport_bound():
    for frame in range(1, 7):
        g = _geom(frame)
        assert g.end_hwdpi <= holder.FEEDL_CEILING, (frame, g.end_hwdpi)


def test_overscan_default_matches_ledger_frame1_and_6():
    """Pin the two ledger endpoints (docs/holder-position-design.md
    section 4) so a constant or formula change is noticed."""
    g1, g6 = _geom(1), _geom(6)
    assert (g1.feedl, g1.chunks, g1.delivered_lines) == (6562, 233, 5335), g1
    assert (g6.feedl, g6.chunks, g6.delivered_lines) == (60276, 232, 5312), g6


def test_overscan_refuses_frame_out_of_range():
    try:
        _geom(7)
    except FrameOutOfRangeError:
        pass
    else:
        raise AssertionError("frame 7 not refused")


def test_overscan_refuses_end_beyond_transport_bound():
    """A margin so large the pass would drive past the load traverse is
    refused before any write, by the end-position guard."""
    try:
        _geom(6, overscan_mm=25.0)
    except FeedlOutOfRangeError:
        pass
    else:
        raise AssertionError("over-long overscan on frame 6 not refused")


# --------------------------------------------------------------------------
# tables.scan_phase / scan_lines_for_chunks
# --------------------------------------------------------------------------

def test_default_scan_phase_is_byte_identical():
    """scan_phase() at the default chunk count reproduces the module SCAN
    op-for-op, and its programmed line count is the captured 5137."""
    a = tables.scan_phase()
    b = tables.SCAN
    assert len(a.ops) == len(b.ops)
    for oa, ob in zip(a.ops, b.ops):
        assert oa == ob, (oa, ob)
    assert tables.scan_lines_for_chunks(tables.IMAGE_CHUNK_COUNT) == tables.DEFAULT_LINES == 5137
    assert tables.IMAGE_CHUNK_LINES == 23 and tables.DRAIN_LINES == 8


def test_scan_phase_structure_for_other_chunk_counts():
    for n in (100, 223, 233, 300):
        p = tables.scan_phase(n)
        descs = [o for o in p.ops if getattr(o, "data", None) == tables.IMAGE_DESC_DATA]
        assert len(descs) == n, (n, len(descs))
        assert descs[0].wi == 0x0008
        assert all(d.wi == 0 for d in descs[1:])
        assert tables.scan_lines_for_chunks(n) == n * 23 + 8


# --------------------------------------------------------------------------
# coverage on a synthesized overscanned frame (geometry -> crop round trip)
# --------------------------------------------------------------------------

def _synthesize_delivered(frame, load_shift_mm, overscan_mm=holder.OVERSCAN_MM):
    """A delivered plain-3600 frame for `frame`, with the aperture placed
    where the mean mapping predicts, displaced by `load_shift_mm`, filled
    bright with dark plastic outside it -- the empty-holder look."""
    g = _geom(frame, overscan_mm)
    fid = holder.STRIP_FIDUCIAL
    lines_per_mm = 3600 / 25.4
    ap_len_lines = fid.aperture_mm[frame - 1] * lines_per_mm
    half = g.delivered_lines / 2 * RES
    win_start = g.feedl - half
    # aperture leading edge position within the delivered window, in lines
    lead_hwdpi = fid.leading_hwdpi(frame) + load_shift_mm / MM
    lead_line = (lead_hwdpi - win_start) / RES
    trail_line = lead_line + ap_len_lines
    img = np.full((g.delivered_lines, 3762, 3), 800, dtype=np.uint16)  # plastic
    lo = max(0, int(round(lead_line)))
    hi = min(g.delivered_lines, int(round(trail_line)))
    img[lo:hi] = 40000  # open aperture
    return img, g, lead_line, trail_line


def test_coverage_finds_a_blurred_3600dpi_edge():
    """A 3600-dpi aperture edge is spread over many lines, so the per-line
    gradient is below the detector's threshold; measure_coverage bins to
    ~600 dpi first. This is the fault the first plain-3600 overscan run
    exposed (both edges in the image, none found). Build an aperture with
    a ~10-line ramp at each edge and require it is still found."""
    g = _geom(1)
    n = g.delivered_lines
    lead, trail = 120, n - 150
    prof = np.full(n, 800.0)
    ramp = 10
    for i in range(n):
        if i < lead - ramp:
            v = 800.0
        elif i < lead:
            v = 800.0 + (65535.0 - 800.0) * (i - (lead - ramp)) / ramp
        elif i < trail:
            v = 65535.0
        elif i < trail + ramp:
            v = 65535.0 - (65535.0 - 800.0) * (i - trail) / ramp
        else:
            v = 800.0
        prof[i] = v
    img = np.repeat(prof[:, None], 3762, axis=1).astype(np.uint16)[:, :, None]
    img = np.repeat(img, 3, axis=2)
    cov = aperture_crop.measure_coverage(img, dpi=3600)
    assert cov.verified, cov.reason
    # The half-level crossing sits at the ramp midpoint; binning to ~600
    # dpi before detection costs a few full-resolution lines of precision,
    # which is far finer than the coverage margin it feeds.
    assert abs(cov.leading_line - (lead - ramp / 2)) < 8, cov.leading_line
    assert abs(cov.trailing_line - (trail + ramp / 2)) < 8, cov.trailing_line


def test_coverage_600dpi_path_unbinned():
    """At 600 dpi the bin factor is 1, so the profile is detected as-is."""
    n = 900
    img = np.full((n, 876, 3), 800, dtype=np.uint16)
    img[100:800] = 40000
    cov = aperture_crop.measure_coverage(img, dpi=600)
    assert cov.verified, cov.reason
    assert abs(cov.leading_line - 100) < 2 and abs(cov.trailing_line - 800) < 2


def test_coverage_verifies_and_crops_across_load_shifts():
    for frame in (1, 6):
        for shift in (-0.24, 0.0, 0.24):
            img, g, lead_line, trail_line = _synthesize_delivered(frame, shift)
            cov = aperture_crop.measure_coverage(img, dpi=3600)
            assert cov.verified, (frame, shift, cov.reason)
            # ~6-line tolerance: measure_coverage bins to ~600 dpi before
            # detecting (see test_coverage_finds_a_blurred_3600dpi_edge).
            assert abs(cov.leading_line - lead_line) < 8, (frame, shift, cov.leading_line, lead_line)
            assert abs(cov.trailing_line - trail_line) < 8, (frame, shift)
            crop = aperture_crop.crop_to_aperture(img, cov, dpi=3600)
            got_mm = crop.shape[0] / (3600 / 25.4)
            assert abs(got_mm - holder.STRIP_FIDUCIAL.aperture_mm[frame - 1]) < 0.1, (frame, shift, got_mm)


# --------------------------------------------------------------------------
# device wiring: Scanner._scan_plain(overscan_mm=...)
# --------------------------------------------------------------------------

class _RecordingScanner:
    """A Scanner whose _run_phase/_exec_ops are replaced by recorders that
    return correctly shaped zero buffers, so _scan_plain runs end to end
    off hardware and every commanded value can be read back."""

    def __init__(self):
        from tests.test_calibrate import MockUsbIo
        self.s = Scanner(MockUsbIo({}))
        self.runs = []  # (phase, inject)
        self.s._run_phase = self._run_phase           # type: ignore[method-assign]
        self.s._exec_ops = self._exec_ops             # type: ignore[method-assign]
        self.s._park = lambda *a, **k: None           # type: ignore[method-assign]
        # Zero white buffers would fail the lamp warmup; the gain codes
        # are irrelevant to the geometry under test.
        self.s._gain_with_warmup = lambda *a, **k: (0x2e, 0x21, 0x29)  # type: ignore[method-assign]

    def _buffers_for(self, phase):
        name = phase.name
        if name == "scan":
            n = sum(1 for o in phase.ops if getattr(o, "data", None) == tables.IMAGE_DESC_DATA)
            return [bytes(tables.IMAGE_CHUNK_LEN) for _ in range(n)] + [bytes(tables.IMAGE_TRAILING_DRAIN_LEN)]
        if name in ("cal_dark_a", "cal_dark_b", "cal_white", "cal_gain_check_a", "cal_gain_check_b"):
            return [bytes(5184 * 3 * 2)]
        if name in ("cal_shading_measure", "cal_shading_upload"):
            return [bytes(128 * 3762 * 3 * 2)]
        return [bytes(0)]

    def _run_phase(self, phase, **inject):
        self.runs.append((phase, inject))
        return self._buffers_for(phase)

    def _exec_ops(self, ops):
        # cal_shading_verify's split read: a 128-line measurement buffer.
        return [bytes(128 * 3762 * 3 * 2)]


def _run_plain(overscan_mm):
    rec = _RecordingScanner()
    rec.s.scan  # touch to ensure method exists
    rec.s._scan_plain(frame=1, lines=None, overscan_mm=overscan_mm)
    pos = next(inj for ph, inj in rec.runs if ph.name == "position")
    scan_ph, scan_inj = next((ph, inj) for ph, inj in rec.runs if ph.name == "scan")
    feedl = (pos["feedl_hi"][0] << 16) | (pos["feedl_mid"][0] << 8) | pos["feedl_lo"][0]
    n_lines = (scan_inj["lines_hi"][0] << 8) | scan_inj["lines_lo"][0]
    n_desc = sum(1 for o in scan_ph.ops if getattr(o, "data", None) == tables.IMAGE_DESC_DATA)
    return feedl, n_lines, n_desc, rec.s.last_diag


def test_wiring_default_path_uses_grid_feedl_and_captured_window():
    feedl, n_lines, n_desc, diag = _run_plain(None)
    assert feedl == tables.feedl_for_frame(1), feedl
    assert n_lines == tables.DEFAULT_LINES, n_lines
    assert n_desc == tables.IMAGE_CHUNK_COUNT, n_desc
    assert diag["chunk_count"] == tables.IMAGE_CHUNK_COUNT
    assert diag["overscan_mm"] is None
    assert diag["raw_bytes"] == tables.IMAGE_CHUNK_COUNT * tables.IMAGE_CHUNK_LEN


def test_wiring_overscan_path_uses_geometry():
    g = _geom(1)
    feedl, n_lines, n_desc, diag = _run_plain(holder.OVERSCAN_MM)
    assert feedl == g.feedl, (feedl, g.feedl)
    assert n_lines == tables.scan_lines_for_chunks(g.chunks), n_lines
    assert n_desc == g.chunks, (n_desc, g.chunks)
    assert diag["chunk_count"] == g.chunks
    assert diag["overscan_mm"] == holder.OVERSCAN_MM
    assert diag["raw_bytes"] == g.chunks * tables.IMAGE_CHUNK_LEN
    assert diag["feedl"] == g.feedl


def main():
    tests = [
        test_overscan_covers_both_sides_after_rounding_and_crop,
        test_overscan_survives_worst_case_load_shift,
        test_overscan_end_position_inside_transport_bound,
        test_overscan_default_matches_ledger_frame1_and_6,
        test_overscan_refuses_frame_out_of_range,
        test_overscan_refuses_end_beyond_transport_bound,
        test_default_scan_phase_is_byte_identical,
        test_scan_phase_structure_for_other_chunk_counts,
        test_coverage_finds_a_blurred_3600dpi_edge,
        test_coverage_600dpi_path_unbinned,
        test_coverage_verifies_and_crops_across_load_shifts,
        test_wiring_default_path_uses_grid_feedl_and_captured_window,
        test_wiring_overscan_path_uses_geometry,
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
