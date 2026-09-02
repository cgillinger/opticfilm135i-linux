#!/usr/bin/env python3
"""Offline tests for the IR-enabled scan mode (of135i.tables_ir,
Scanner._scan_dual, of135i.image.split_ir).

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python driver/tests/test_ir.py

Covers:
  - a SEQUENCE test: Scanner.scan(frame=1, ir=True) run against a MOCK
    UsbIo, asserting the emitted control-write stream equals tables_ir.py's
    phase data (itself extracted verbatim from
    traces/04-singel-3600-IRpa.trace.json.gz, see gen_tables.py's
    main_ir()) for every phase _scan_ir() touches, with the injectable
    values (gain codes, offset codes, FEEDL, line count, the two
    shading-table upload/re-upload pairs) pinned to synthetic
    measurements chosen so the computed injections are checkable --
    mirrors tests/test_calibrate.py's test_scan_sequence_matches_trace,
    generalized for tables_ir's doubled/alternating buffers and its two
    shading-upload addresses (A = even/IR lines, B = odd/visible lines)
    instead of one. tests/test_dpi.py runs the same test against the
    600/1200/2400/7200 dpi modules.
  - split_ir() statistics against the ground-truth raw capture
    cal-data/ir/04-image.raw (per cal-data/ir/ir-analysis.md: IR lines
    near-equal channel means, visible lines with clear R>G>B separation).
"""

from __future__ import annotations

import struct
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import calibrate, image, tables_ir
from of135i.device import Scanner

REPO = Path(__file__).resolve().parents[1]
IR_DIR = REPO / "cal-data" / "ir"

W = tables_ir.IMAGE_WIDTH  # 5184

PHASE_ORDER_IR = [
    tables_ir.CAL_DARK_A, tables_ir.CAL_DARK_B, tables_ir.CAL_WHITE,
    tables_ir.CAL_GAIN_CHECK_A, tables_ir.CAL_GAIN_CHECK_B,
    tables_ir.CAL_SHADING_MEASURE, tables_ir.CAL_SHADING_UPLOAD, tables_ir.CAL_SHADING_VERIFY,
    tables_ir.POSITION, tables_ir.SCAN, tables_ir.PARK,
]

# This trace's own captured gain codes (AFE regs 2/3/4), read directly
# off tables_ir.CAL_GAIN_CHECK_A's injection bytes so this stays correct
# if the trace/compilation ever changes.
_TRACE_GAIN_CODES = tuple(
    tables_ir.CAL_GAIN_CHECK_A.ops[idx].data[off]
    for idx, off in (
        (tables_ir.CAL_GAIN_CHECK_A.injections["gain_r"][1], tables_ir.CAL_GAIN_CHECK_A.injections["gain_r"][2]),
        (tables_ir.CAL_GAIN_CHECK_A.injections["gain_g"][1], tables_ir.CAL_GAIN_CHECK_A.injections["gain_g"][2]),
        (tables_ir.CAL_GAIN_CHECK_A.injections["gain_b"][1], tables_ir.CAL_GAIN_CHECK_A.injections["gain_b"][2]),
    )
)


# -------------------------------------------------------------- split_ir


def test_split_ir_against_capture():
    _raw = REPO / "cal-data" / "ir" / "04-image.raw"
    if not _raw.exists():
        print("test_split_ir_against_capture SKIPPED (ground-truth raw not shipped)")
        return
    raw_path = IR_DIR / "04-image.raw"
    raw = raw_path.read_bytes()
    assert len(raw) == 327_960_576, len(raw)

    visible, ir = image.split_ir(raw, width=W)
    assert visible.shape == (5272, W, 3), visible.shape
    assert visible.dtype == np.uint16
    assert ir.shape == (5272, W), ir.shape
    assert ir.dtype == np.uint16

    vf = visible.astype(np.float64)
    means = [vf[..., c].mean() for c in range(3)]
    # Clear R > G > B separation (color-negative under visible light),
    # per ir-analysis.md's full-dataset table (R=13963, G=6989, B=5576).
    assert means[0] > means[1] > means[2], means
    for got, want in zip(means, (13963, 6989, 5576)):
        assert abs(got - want) / want < 0.05, (means, "vs expected ~(13963, 6989, 5576)")

    irf = ir.astype(np.float64)
    ir_mean = irf.mean()
    # ir-analysis.md: mean_of_line_means ~= 33925 (near-flat, bright).
    assert abs(ir_mean - 33925) / 33925 < 0.05, ir_mean

    # Cross-check the "near-equal channel means" claim directly on the
    # pre-collapse RGB IR lines (not just the collapsed single channel).
    ir_rgb = image.assemble(raw, W)[0::2].astype(np.float64)
    ir_ch_means = [ir_rgb[..., c].mean() for c in range(3)]
    spread = (max(ir_ch_means) - min(ir_ch_means)) / ir_mean
    assert spread < 0.01, (ir_ch_means, "expected near-equal R/G/B for the IR pass")

    print(
        f"test_split_ir_against_capture OK "
        f"(visible means {tuple(round(m) for m in means)}, ir mean {round(ir_mean)})"
    )


# -------------------------------------------------------------- MOCK UsbIo
#
# Structurally identical to tests/test_calibrate.py's _FakeDev/MockUsbIo
# (see that file's docstring for the full rationale); duplicated here
# rather than imported since tests/ isn't a package.


class _FakeDev:
    def __init__(self, queues: dict[tuple[int, int, int, int], deque],
                 writes: list[bytes], cal_buffers: dict[int, deque]):
        self._queues = queues
        self._writes = writes
        self._cal_buffers = cal_buffers
        self._pending_read: list | None = None

    def ctrl_transfer(self, bm, br, wv, wi, data_or_length):
        if isinstance(data_or_length, (bytes, bytearray)):
            data = bytes(data_or_length)
            self._writes.append(data)
            if br == 0x04 and wv == 0x0082 and len(data) == 8:
                addr, ln = struct.unpack("<II", data)
                if wi == 1:
                    self._pending_read = None
                else:
                    q = self._cal_buffers.get(ln)
                    canned = q.popleft() if q else bytes(ln)
                    self._pending_read = [canned, 0]
            return len(data)
        length = data_or_length
        q = self._queues.get((bm, br, wv, wi))
        if q:
            resp = q.popleft()
            if len(resp) == length:
                return resp
        return bytes(length)

    def read(self, ep, length, timeout=0):
        if self._pending_read is not None:
            buf, off = self._pending_read
            chunk = buf[off:off + length]
            self._pending_read[1] = off + length
            if len(chunk) < length:
                chunk = chunk + bytes(length - len(chunk))
            return chunk
        return bytes(length)

    def write(self, ep, data, timeout=0):
        self._writes.append(bytes(data))


def _build_queues(phase_order):
    queues: dict[tuple[int, int, int, int], deque] = {}
    for phase in phase_order:
        for op in phase.ops:
            if op.kind in ("cr", "poll"):
                key = (op.bm, op.br, op.wv, op.wi)
                queues.setdefault(key, deque()).append(op.resp)
    return queues


class MockUsbIo:
    def __init__(self, cal_buffers: dict[int, deque]):
        self.writes: list[bytes] = []
        self.dev = _FakeDev(_build_queues(PHASE_ORDER_IR), self.writes, cal_buffers)
        self.buf_reads: list[tuple[int, int]] = []
        self.buf_writes: list[tuple[int, bytes]] = []

    def write_regs(self, pairs):
        self.writes.append(bytes(b for pair in pairs for b in pair))

    def wait_reg(self, reg, value, timeout=0, mask=0xFF):
        return 0x22

    def end_access(self, which=0x8C, wIndex=16):
        pass

    def close(self):
        pass


def _peak_for_gain_code(target_code: int) -> int:
    approx = round(calibrate._GAIN_DIVISOR * calibrate._GAIN_TARGET / target_code)
    for peak in range(max(1, approx - 8), approx + 9):
        if round(calibrate._GAIN_DIVISOR * calibrate._GAIN_TARGET / peak) == target_code:
            return peak
    raise AssertionError(f"no peak round-trips to gain code {target_code:#04x}")


def _white_buffer_for_captured_gain() -> bytes:
    """A synthetic 62208 B (2-line, alternating) white buffer whose
    ODD (visible) line makes gain_codes() return exactly this trace's
    own captured codes; the even (IR) line's content is irrelevant to
    the gain computation (device.py de-interleaves before calling
    gain_codes()) so it's filled with a distinct, obviously-not-visible
    value to make a de-interleaving bug fail loudly if introduced."""
    white = np.zeros((2, W, 3), dtype=np.uint16)
    white[0, :, :] = 40000          # IR line -- must NOT affect gain_codes()
    for ch, code in enumerate(_TRACE_GAIN_CODES):
        white[1, :, ch] = _peak_for_gain_code(code)
    got_codes = calibrate.gain_codes(white[1].reshape(-1, 3))
    assert got_codes == _TRACE_GAIN_CODES, (got_codes, _TRACE_GAIN_CODES)
    white_bytes = white.astype("<u2").tobytes()
    assert len(white_bytes) == 62208
    return white_bytes


def _captured_shading_offsets(phase, injection_name) -> np.ndarray:
    """Mirrors test_calibrate.py's helper of the same name: the per-
    pixel offsets calibrate.shading_table() would need to reproduce
    `phase`'s own captured (pre-injection) bulk-OUT payload for
    `injection_name` bit-for-bit (stripping the wire-padding tail)."""
    _, idxs = phase.injections[injection_name]
    payload = b"".join(phase.ops[i].data for i in idxs)[:tables_ir.SHADING_UPLOAD_LEN]
    offsets = []
    i, n = 0, len(payload)
    while i < n:
        remaining = n - i
        if remaining >= 512:
            block, i, n_payload = payload[i:i + 512], i + 512, 126
        else:
            block, i, n_payload = payload[i:i + remaining], i + remaining, remaining // 4
        pairs = np.frombuffer(block, dtype="<u2").reshape(-1, 2)
        offsets.extend(pairs[:n_payload, 0].tolist())
    return np.array(offsets, dtype=np.uint16)


def _synthetic_measurement_buffer(a_offsets: np.ndarray, b_offsets: np.ndarray) -> bytes:
    """A synthetic 256-line (alternating) measurement whose even (table
    A / IR) and odd (table B / visible) halves each equal the given per-
    pixel offsets exactly (128 identical integer samples average back
    to themselves exactly), so calibrate.shading_table() reproduces
    each bit-for-bit."""
    a_px = a_offsets.reshape(W, 3)
    b_px = b_offsets.reshape(W, 3)
    arr = np.zeros((256, W, 3), dtype="<u2")
    arr[0::2] = np.broadcast_to(a_px, (128, W, 3))
    arr[1::2] = np.broadcast_to(b_px, (128, W, 3))
    raw = arr.tobytes()
    assert len(raw) == 7_962_624
    return raw


def _build_cal_buffers():
    """Canned calibration buffers keyed by expected byte length. White
    (62208 B) is crafted so gain_codes() (on its de-interleaved visible
    line) returns exactly this trace's captured codes. The 7962624 B
    cal_shading_measure buffer is crafted so calibrate.shading_table()
    reproduces cal_shading_upload's own captured payloads (both
    addresses) bit-for-bit. cal_shading_verify's re-measurement reuses
    the same synthetic buffer -- like test_calibrate.py, its re-upload
    is only pinned to be self-consistent with what Scanner._scan_dual()
    computes from this same canned buffer (calibrate.shading_table2_dual
    with the vendor's targets), not to the trace's own captured
    re-upload bytes.
    """
    a_off = _captured_shading_offsets(tables_ir.CAL_SHADING_UPLOAD, "shading_table_a")
    b_off = _captured_shading_offsets(tables_ir.CAL_SHADING_UPLOAD, "shading_table_b")
    meas = _synthetic_measurement_buffer(a_off, b_off)

    return {
        62208: deque([_white_buffer_for_captured_gain()]),
        7_962_624: deque([meas, meas]),
    }


# --------------------------------------------------------------- SEQUENCE


def _expected_stream():
    """The exact control-write + bulk-OUT byte stream Scanner._scan_dual()
    is expected to emit for PHASE_ORDER_IR, injections pinned to the
    values computed from _build_cal_buffers()'s canned data -- mirrors
    tests/test_calibrate.py's _expected_stream()."""
    off_r, off_g, off_b = (0x010B, 0x010A, 0x010B)  # calibrate.offset_codes()'s constant
    feedl = tables_ir.feedl_for_frame(1)
    n_lines = tables_ir.DEFAULT_LINES

    a_off = _captured_shading_offsets(tables_ir.CAL_SHADING_UPLOAD, "shading_table_a")
    b_off = _captured_shading_offsets(tables_ir.CAL_SHADING_UPLOAD, "shading_table_b")
    meas = _synthetic_measurement_buffer(a_off, b_off)
    shading_meas = np.frombuffer(meas, dtype="<u2").reshape(256, W, 3)
    shading_meas_a = shading_meas[0::2]
    shading_meas_b = shading_meas[1::2]
    shading_a = calibrate.shading_table(shading_meas_a, width=W)
    shading_b = calibrate.shading_table(shading_meas_b, width=W)

    # The mock serves the SAME canned buffer for cal_shading_verify's
    # own re-measurement, so verify halves == shading_meas halves.
    shading2_a = calibrate.shading_table2_dual(
        shading_meas_a, shading_meas_a, width=W, target=calibrate.SHADING2_TARGET_A)
    shading2_b = calibrate.shading_table2_dual(
        shading_meas_b, shading_meas_b, width=W, target=calibrate.SHADING2_TARGET_B)

    injections = {
        "cal_gain_check_a": dict(
            gain_r=bytes([_TRACE_GAIN_CODES[0]]), gain_g=bytes([_TRACE_GAIN_CODES[1]]),
            gain_b=bytes([_TRACE_GAIN_CODES[2]])),
        "cal_shading_measure": dict(
            offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
            offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
            offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF])),
        "cal_shading_upload": dict(shading_table_a=shading_a, shading_table_b=shading_b),
        "cal_shading_verify": dict(shading_table2_a=shading2_a, shading_table2_b=shading2_b),
        "position": dict(
            feedl_hi=bytes([(feedl >> 16) & 0xFF]),
            feedl_mid=bytes([(feedl >> 8) & 0xFF]),
            feedl_lo=bytes([feedl & 0xFF])),
        "scan": dict(
            lines_top=bytes([(n_lines >> 16) & 0xFF]),
            lines_hi=bytes([(n_lines >> 8) & 0xFF]),
            lines_lo=bytes([n_lines & 0xFF])),
    }

    out = bytearray()
    for phase in PHASE_ORDER_IR:
        inj = injections.get(phase.name, {})
        ops = phase.patched(**inj) if inj else phase.ops
        out += b"".join(op.data for op in ops if op.kind in ("cw", "bo"))
    return bytes(out)


def test_scan_sequence_matches_trace_ir():
    mock = MockUsbIo(_build_cal_buffers())
    scanner = Scanner(mock)
    raw, width, meta = scanner.scan(frame=1, ir=True)

    assert width == tables_ir.IMAGE_WIDTH == 5184
    assert meta == {"width": 5184, "alternating": True, "dpi": 3600}
    assert len(raw) == tables_ir.IMAGE_CHUNK_COUNT * tables_ir.IMAGE_CHUNK_LEN

    expected = _expected_stream()
    actual = b"".join(mock.writes)

    assert actual == expected, (
        f"control-write stream mismatch: {len(actual)} B emitted, "
        f"{len(expected)} B expected (first divergence at byte "
        f"{next((i for i in range(min(len(actual), len(expected))) if actual[i] != expected[i]), min(len(actual), len(expected)))})"
    )
    print(
        f"test_scan_sequence_matches_trace_ir OK "
        f"({len(mock.writes)} writes, {len(actual)} B)"
    )


# ---------------------------------------------------------------- dust removal


def test_dust_removal_synthetic():
    """Synthetic dust/scratch removal check for image.dust_mask/
    remove_dust: inject dark specks into a flat IR field (plus a
    near-black film-holder border strip on each side, per
    ir-analysis.md) and matching darkened patches into a textured
    visible image, and check:
      - the border is never masked, however dark;
      - injected specks ARE masked (their centers, at least);
      - overall coverage stays a small, sane fraction of the frame
        (not "half the image" -- the border-exclusion and threshold
        calibration both hold on synthetic as well as real data);
      - remove_dust leaves every unmasked visible pixel bit-for-bit
        untouched;
      - remove_dust pulls masked pixels back toward the true
        (pre-speck) texture, not just replaces one arbitrary value with
        another.
    """
    rng = np.random.default_rng(20260830)
    H, W = 200, 300
    BORDER = 30  # px on each side

    ir_bg = 40000.0
    ir = np.full((H, W), ir_bg, dtype=np.float64)
    ir += rng.normal(0, ir_bg * 0.01, size=(H, W))  # ~1% shot-noise-ish, per ir-analysis.md
    ir[:, :BORDER] = 800.0 + rng.normal(0, 20, size=(H, BORDER))     # film-holder border
    ir[:, W - BORDER:] = 800.0 + rng.normal(0, 20, size=(H, BORDER))

    # A mildly textured "true" visible image (no dust) -- a smooth
    # per-channel gradient plus a touch of texture, well clear of the
    # border columns' territory. remove_dust never sees this; it's the
    # ground truth used only to score the fill afterwards.
    yy, xx = np.mgrid[0:H, 0:W]
    texture_true = np.stack([
        8000 + 20 * xx + 15 * np.sin(yy / 7.0) * 50,
        5000 + 15 * yy + 10 * np.cos(xx / 9.0) * 50,
        3000 + 10 * xx + 10 * yy,
    ], axis=-1).astype(np.float64)
    visible = texture_true + rng.normal(0, 30, size=texture_true.shape)

    # Three specks, well inside the interior, away from the border and
    # from the array edges (np.roll wraps, which would otherwise let a
    # speck "see" the opposite edge) -- sizes 3, 8 and 15 px, covering
    # the "up to ~20 px" spec.
    specks = [
        (40, 100, 3), (100, 150, 8), (150, 200, 15),
    ]
    speck_mask = np.zeros((H, W), dtype=bool)
    for cy, cx, size in specks:
        y0, y1 = cy - size // 2, cy - size // 2 + size
        x0, x1 = cx - size // 2, cx - size // 2 + size
        speck_mask[y0:y1, x0:x1] = True
        ir[y0:y1, x0:x1] *= 0.5           # opaque debris: much darker in IR...
        visible[y0:y1, x0:x1, :] *= 0.6   # ...and darker (not necessarily as much) in visible

    ir = ir.astype(np.uint16)
    visible = np.clip(visible, 0, 65535).astype(np.uint16)

    mask = image.dust_mask(ir, sensitivity=1.0)
    assert mask.shape == (H, W)
    assert mask.dtype == np.bool_

    assert not mask[:, :BORDER].any(), "border falsely masked"
    assert not mask[:, W - BORDER:].any(), "border falsely masked"

    for cy, cx, _size in specks:
        assert mask[cy, cx], f"speck at ({cy},{cx}) not masked"

    coverage = mask.mean()
    injected_frac = speck_mask.mean()
    assert 0 < coverage < 0.10, f"mask coverage implausible: {coverage:.4%}"
    # Dilation (2-3 px) grows the mask past the raw injected footprint,
    # but not by an order of magnitude.
    assert coverage < injected_frac * 6, (coverage, injected_frac)

    cleaned = image.remove_dust(visible, ir, sensitivity=1.0)
    assert cleaned.shape == visible.shape
    assert cleaned.dtype == visible.dtype

    unmasked = ~mask
    assert np.array_equal(cleaned[unmasked], visible[unmasked]), \
        "remove_dust modified an unmasked pixel"

    err_before = np.abs(visible[mask].astype(np.float64) - texture_true[mask])
    err_after = np.abs(cleaned[mask].astype(np.float64) - texture_true[mask])
    assert err_after.mean() < err_before.mean() * 0.5, (
        err_before.mean(), err_after.mean(), "cleaning did not clearly improve on the speck")

    print(
        f"test_dust_removal_synthetic OK "
        f"(coverage {coverage:.4%}, mean abs error {err_before.mean():.0f} -> {err_after.mean():.0f})"
    )


def main() -> int:
    tests = [
        test_split_ir_against_capture,
        test_scan_sequence_matches_trace_ir,
        test_dust_removal_synthetic,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
