#!/usr/bin/env python3
"""Offline tests for of135i.calibrate and the device.py phase sequencing.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python driver/tests/test_calibrate.py

Covers:
  - gain_codes() / offset_codes() / shading_table() against the
    captured ground truth in cal-data/capture/ (cal-analysis.md's
    "Resolution" section documents the expected values/tolerances).
  - a SEQUENCE test: Scanner.scan(frame=1) run against a MOCK UsbIo,
    asserting the emitted control-write stream (wv=0x83 register
    batches, wv=0x82 buffer descriptors, and bulk-OUT payloads --
    i.e. everything device.py's full-stream executor writes to the
    wire) equals tables.py's phase data (itself extracted verbatim
    from traces/03-singel-3600-IRav.trace.json.gz) for every phase
    Scanner.scan() touches, with the injectable values (gain codes,
    offset codes, FEEDL, line count, shading-table upload/re-upload)
    pinned to the trace's own captured defaults.
"""

from __future__ import annotations

import struct
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import calibrate, tables
from of135i.device import Scanner

REPO = Path(__file__).resolve().parents[1]
CAPTURE = REPO / "cal-data" / "capture"

# Phases Scanner.scan() runs, in the exact order it runs them (see
# device.py). Each entry's full op stream (cw/cr/poll/bo/bi) is
# replayed verbatim by the executor; only cw/bo ops write to the wire
# and are compared here.
PHASE_ORDER = [
    tables.CAL_DARK_A, tables.CAL_DARK_B, tables.CAL_WHITE,
    tables.CAL_GAIN_CHECK_A, tables.CAL_GAIN_CHECK_B,
    tables.CAL_SHADING_MEASURE, tables.CAL_SHADING_UPLOAD, tables.CAL_SHADING_VERIFY,
    tables.POSITION, tables.SCAN, tables.PARK,
]


# ------------------------------------------------------------- gain_codes


def test_gain_codes_against_capture():
    raw = (CAPTURE / "cal-frame00501-len31104.bin").read_bytes()
    white = np.frombuffer(raw, dtype="<u2").reshape(-1, 3)
    assert white.shape == (5184, 3), f"unexpected white-line shape {white.shape}"

    codes = calibrate.gain_codes(white)
    expected = (0x2E, 0x21, 0x29)
    for got, want, ch in zip(codes, expected, "RGB"):
        assert abs(got - want) <= 1, (
            f"gain_codes channel {ch}: got {got:#04x}, want {want:#04x} +/-1"
        )
    print(f"test_gain_codes_against_capture OK ({[hex(c) for c in codes]})")


# ----------------------------------------------------------- offset_codes


def test_offset_codes_fallback_on_zero_dark():
    """Zero dark buffers have slope=0 -> falls back to hardcoded default."""
    dark_a = np.zeros((512, 3), dtype=np.uint16)
    dark_b = np.zeros((512, 3), dtype=np.uint16)
    codes = calibrate.offset_codes(dark_a, dark_b)
    assert codes == (0x010B, 0x010A, 0x010B), codes
    print("test_offset_codes_fallback_on_zero_dark OK")


def test_offset_codes_from_reference_bracket():
    """Bracket data from the reference unit's capture (cal-analysis.md
    section 1) must reproduce the vendor's codes (267, 266, 267)."""
    # Reference unit means: dark_a at offset=0x80, dark_b at offset=0xff
    ref_means_a = [21411, 27770, 24897]   # R, G, B
    ref_means_b = [23644, 30052, 27174]
    dark_a = np.tile(np.array(ref_means_a, dtype=np.uint16), (512, 1))
    dark_b = np.tile(np.array(ref_means_b, dtype=np.uint16), (512, 1))
    codes = calibrate.offset_codes(dark_a, dark_b)
    expected = (0x010B, 0x010A, 0x010B)
    assert codes == expected, f"got {tuple(hex(c) for c in codes)}, want {tuple(hex(c) for c in expected)}"
    print(f"test_offset_codes_from_reference_bracket OK ({[hex(c) for c in codes]})")


def test_offset_codes_adapts_to_different_slope():
    """A unit with double the slope should produce different code steps
    but the same target dark level."""
    # Double the reference slope: (mean_b - mean_a) is doubled
    ref_means_a = [21411, 27770, 24897]
    ref_means_b = [23644, 30052, 27174]
    # With doubled slope, mean_b = mean_a + 2*(ref_mean_b - ref_mean_a)
    doubled_b = [a + 2 * (b - a) for a, b in zip(ref_means_a, ref_means_b)]
    dark_a = np.tile(np.array(ref_means_a, dtype=np.uint16), (512, 1))
    dark_b = np.tile(np.array(doubled_b, dtype=np.uint16), (512, 1))
    codes = calibrate.offset_codes(dark_a, dark_b)
    # Double slope -> half the margin in code steps -> 0xff + 6 = 261
    expected = (261, 261, 261)
    assert codes == expected, f"got {codes}, want {expected}"
    print(f"test_offset_codes_adapts_to_different_slope OK ({codes})")


# ---------------------------------------------------------- shading_table


def _parse_shading_blocks(buf: bytes):
    """Mirror calibrate.shading_table's block structure to recover the
    payload (offset, gain) pairs, stripping the 2 trailer pairs from
    every full 512 B block."""
    offsets, gains = [], []
    i, n = 0, len(buf)
    while i < n:
        remaining = n - i
        if remaining >= 512:
            block, i, n_payload = buf[i:i + 512], i + 512, 126
        else:
            block, i, n_payload = buf[i:i + remaining], i + remaining, remaining // 4
        pairs = np.frombuffer(block, dtype="<u2").reshape(-1, 2)
        offsets.extend(pairs[:n_payload, 0].tolist())
        gains.extend(pairs[:n_payload, 1].tolist())
    return np.array(offsets, dtype=np.uint16), np.array(gains, dtype=np.uint16)


def test_shading_table_against_capture():
    raw = (CAPTURE / "cal-frame00797-len2889216.bin").read_bytes()
    measurement = np.frombuffer(raw, dtype="<u2").reshape(128, 3762, 3)

    payload = calibrate.shading_table(measurement)
    truth = (CAPTURE / "shading-upload-len45856.bin").read_bytes()

    assert len(payload) == calibrate.SHADING_UPLOAD_LEN == 45856
    assert len(truth) == 45856

    got_offsets, got_gains = _parse_shading_blocks(payload)
    want_offsets, want_gains = _parse_shading_blocks(truth)

    assert len(got_offsets) == len(want_offsets) == 3762 * 3, (
        len(got_offsets), len(want_offsets)
    )

    # Block structure exact: same pair count per block, same total length.
    assert len(payload) == len(truth)

    # Gain field exact for every payload pixel (0x4000 in this capture).
    assert np.array_equal(got_gains, want_gains), "gain field mismatch"
    assert set(np.unique(want_gains).tolist()) == {0x4000}

    # Offset field within +/-8 for >=99% of pixels.
    diff = np.abs(got_offsets.astype(int) - want_offsets.astype(int))
    within_tol = (diff <= 8).mean()
    assert within_tol >= 0.99, f"only {within_tol:.4%} of pixels within +/-8"

    print(
        f"test_shading_table_against_capture OK "
        f"(within +/-8: {within_tol:.4%}, max diff {diff.max()})"
    )


# -------------------------------------------------------------- MOCK UsbIo


class _FakeDev:
    """Stands in for pyusb's Device for everything device.py's full-
    stream executor (_exec_ops/_poll_one) talks to directly: control
    transfers (cw/cr/poll) and bulk transfers (bo/bi). write_regs()
    (still used by the hand-written home()/eject() motor sequence)
    never reaches this -- MockUsbIo overrides it directly, appending
    into the same shared `writes` list.

    IN control transfers (cr and poll both call ctrl_transfer the same
    way -- the mock cannot structurally tell them apart) are served
    from one deque per (bm,br,wv,wi) key, pre-loaded with exactly one
    entry per cr/poll op that key sees across PHASE_ORDER, in true
    capture order. A poll's *own* entry is always its settled final
    response, so it matches (and returns) on the poll's very first
    attempt; a cr's own entry is its own captured response (content
    irrelevant -- device.py logs, never enforces, cr mismatches). This
    keeps polls sharing a key with interleaved cr reads (a real
    pattern in this trace, e.g. the busy/status register) from
    desynchronizing each other.
    """

    def __init__(self, queues: dict[tuple[int, int, int, int], deque],
                 writes: list[bytes], cal_buffers: dict[int, deque]):
        self._queues = queues
        self._writes = writes
        self._cal_buffers = cal_buffers
        self._pending_read: list | None = None  # [buf: bytes, offset: int]

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
    """Records every control-write/bulk-write byte payload verbatim
    (for the SEQUENCE comparison) and serves canned/zero data for
    bulk reads, without touching real USB hardware."""

    def __init__(self, cal_buffers: dict[int, deque]):
        self.writes: list[bytes] = []
        self.dev = _FakeDev(_build_queues(PHASE_ORDER), self.writes, cal_buffers)
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
    """Invert calibrate's gain formula (code = round(32*target/peak)) to
    find an integer peak that round-trips to exactly `target_code`."""
    approx = round(calibrate._GAIN_DIVISOR * calibrate._GAIN_TARGET / target_code)
    for peak in range(max(1, approx - 8), approx + 9):
        if round(calibrate._GAIN_DIVISOR * calibrate._GAIN_TARGET / peak) == target_code:
            return peak
    raise AssertionError(f"no peak round-trips to gain code {target_code:#04x}")


def _white_buffer_for_captured_gain():
    """A synthetic 31104 B white-line buffer that makes gain_codes()
    return exactly the trace's own captured codes (0x2e, 0x21, 0x29)."""
    trace_gain_codes = (0x2E, 0x21, 0x29)
    white = np.zeros((5184, 3), dtype=np.uint16)
    for ch, code in enumerate(trace_gain_codes):
        white[:, ch] = _peak_for_gain_code(code)
    white_bytes = white.astype("<u2").tobytes()
    assert len(white_bytes) == 31104

    got_codes = calibrate.gain_codes(white)
    assert got_codes == trace_gain_codes, (got_codes, trace_gain_codes)
    return white_bytes


def _captured_shading_offsets(phase, injection_name) -> np.ndarray:
    """The per-pixel offsets calibrate.shading_table() would need to
    reproduce `phase`'s own *captured* (pre-injection) bulk-OUT
    payload for `injection_name` bit-for-bit.

    The captured bo chunks include trailing USB-packet padding (46080
    B on the wire vs. calibrate.SHADING_UPLOAD_LEN == 45856 B of real
    payload -- see tables.py's SHADING_UPLOAD_CHUNK_LENS note); strip
    it before parsing, or the padding bytes get misread as extra
    (offset, gain) pairs."""
    _, idxs = phase.injections[injection_name]
    payload = b"".join(phase.ops[i].data for i in idxs)[:calibrate.SHADING_UPLOAD_LEN]
    offsets, gains = _parse_shading_blocks(payload)
    assert set(np.unique(gains).tolist()) == {0x4000}
    return offsets


def _synthetic_measurement_buffer(offsets: np.ndarray) -> bytes:
    """A synthetic 128-line measurement whose every line equals
    `offsets` exactly, so calibrate.shading_table()'s per-pixel mean
    reproduces `offsets` bit-for-bit (128 identical integer samples
    average back to themselves exactly -- no rounding drift)."""
    px = offsets.reshape(3762, 3)
    measurement = np.broadcast_to(px, (128, 3762, 3)).astype("<u2")
    raw = measurement.tobytes()
    assert len(raw) == 2889216
    return raw


def _build_cal_buffers():
    """Canned calibration buffers keyed by expected byte length, as
    per-length queues (cal_shading_measure and cal_shading_verify both
    read 2889216 B, but each gets its own queue entry, consumed in
    call order).

    White (31104 B) is crafted so gain_codes() returns exactly the
    trace's captured codes. The 2889216 B cal_shading_measure buffer
    is crafted (via _synthetic_measurement_buffer) so
    calibrate.shading_table() reproduces cal_shading_upload's own
    captured payload bit-for-bit -- see _captured_shading_offsets.

    cal_shading_verify's re-upload does NOT get the same treatment:
    its captured bo payload does not follow calibrate.shading_table()'s
    block format at all (checked directly against the trace -- every
    512 B block's would-be trailer pairs are non-zero, and the "gain"
    field varies per pixel instead of the documented constant 0x4000).
    The real device evidently computes a genuine per-pixel gain for
    this second pass; calibrate.py's model (constant gain, per
    cal-analysis.md) does not cover that, and extending it is out of
    this refactor's scope. So the verify buffer below reuses
    measure_offsets too -- shading_table2's injected bytes are only
    pinned to be *self-consistent* with what Scanner.scan() computes
    from this same canned buffer (see _expected_stream), not to the
    trace's own captured re-upload bytes.

    Every other buffer's content is irrelevant to the register-batch
    stream (dark pairs feed offset_codes(); the mock's zero-filled dark
    buffers have slope=0, triggering the fallback to the same hardcoded
    codes the trace uses) so plain zeros are used for those.
    """
    measure_offsets = _captured_shading_offsets(tables.CAL_SHADING_UPLOAD, "shading_table")

    return {
        31104: deque([_white_buffer_for_captured_gain()]),
        2889216: deque([
            _synthetic_measurement_buffer(measure_offsets),
            _synthetic_measurement_buffer(measure_offsets),
        ]),
    }


# --------------------------------------------------------------- SEQUENCE


def _expected_stream():
    """The exact control-write + bulk-OUT byte stream Scanner.scan()
    is expected to emit for PHASE_ORDER, injections pinned to the
    trace's own captured values -- with one documented exception: the
    cal_shading_verify re-upload (shading_table2) is only pinned to be
    self-consistent with the same canned buffer _build_cal_buffers()
    feeds the mock for that read, not to the trace's own captured
    re-upload bytes (calibrate.shading_table()'s constant-gain model
    cannot reproduce that capture -- see _build_cal_buffers)."""
    trace_gain_codes = (0x2E, 0x21, 0x29)
    gain_r, gain_g, gain_b = trace_gain_codes
    off_r, off_g, off_b = (0x010B, 0x010A, 0x010B)
    feedl = tables.feedl_for_frame(1)
    n_lines = tables.DEFAULT_LINES

    measure_offsets = _captured_shading_offsets(tables.CAL_SHADING_UPLOAD, "shading_table")
    bcast = np.broadcast_to(
        measure_offsets.reshape(3762, 3), (128, 3762, 3)).astype("<u2")
    shading = calibrate.shading_table(bcast)
    # The device computes upload #2 via shading_table2(white, dark);
    # the mock serves the same synthetic measurement for both reads.
    shading2 = calibrate.shading_table2(bcast, bcast)

    injections = {
        "cal_gain_check_a": dict(
            gain_r=bytes([gain_r]), gain_g=bytes([gain_g]), gain_b=bytes([gain_b])),
        "cal_shading_measure": dict(
            offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
            offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
            offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF])),
        "cal_shading_upload": dict(shading_table=shading),
        "cal_shading_verify": dict(shading_table2=shading2),
        "position": dict(
            feedl_hi=bytes([(feedl >> 16) & 0xFF]),
            feedl_mid=bytes([(feedl >> 8) & 0xFF]),
            feedl_lo=bytes([feedl & 0xFF])),
        "scan": dict(
            lines_hi=bytes([(n_lines >> 8) & 0xFF]),
            lines_lo=bytes([n_lines & 0xFF])),
    }

    out = bytearray()
    for phase in PHASE_ORDER:
        inj = injections.get(phase.name, {})
        ops = phase.patched(**inj) if inj else phase.ops
        out += b"".join(op.data for op in ops if op.kind in ("cw", "bo"))
    return bytes(out)


# ------------------------------------------------------ _gain_with_warmup


class _WarmupHarness:
    """Drives Scanner._gain_with_warmup() with a scripted sequence of
    white-line peak levels (one per CAL_WHITE run) and a recording
    time.sleep stub, so the retry loop can be exercised without
    hardware or real delays."""

    def __init__(self, peaks_per_attempt):
        self.peaks = deque(peaks_per_attempt)
        self.runs = 0
        self.sleeps: list[float] = []
        self.scanner = Scanner(MockUsbIo({}))
        self.scanner._run_phase = self._fake_run_phase  # type: ignore[method-assign]

    def _fake_run_phase(self, phase, **inject):
        self.runs += 1
        peak = self.peaks.popleft()
        white = np.full((5184, 3), peak, dtype="<u2")
        return [white.tobytes()]

    def run(self):
        from of135i import device as devmod
        parse = lambda raw: np.frombuffer(raw, dtype="<u2").reshape(-1, 3)
        real_sleep = devmod.time.sleep
        devmod.time.sleep = lambda s: self.sleeps.append(s)
        try:
            return self.scanner._gain_with_warmup(tables.CAL_WHITE, parse)
        finally:
            devmod.time.sleep = real_sleep


def _dim_peak_that_maxes_gain() -> int:
    """A white level so low that gain_codes() clips every channel to
    _GAIN_MAX_CODE -- what a cold lamp looks like."""
    peak = 1
    white = np.full((4, 3), peak, dtype=np.uint16)
    assert calibrate.gain_codes(white) == (calibrate._GAIN_MAX_CODE,) * 3
    return peak


def test_warmup_no_retry_when_gain_normal():
    """A warm lamp on the first measurement: one CAL_WHITE run, no sleep."""
    good = _peak_for_gain_code(0x21)
    h = _WarmupHarness([good])
    codes = h.run()
    assert codes == (0x21, 0x21, 0x21), codes
    assert h.runs == 1, h.runs
    assert h.sleeps == [], h.sleeps
    print("test_warmup_no_retry_when_gain_normal OK")


def test_warmup_retries_until_gain_stabilizes():
    """Cold lamp for two measurements, then warm: three runs, two
    delays of _WARMUP_RETRY_DELAY, and the *warm* codes are returned."""
    from of135i import device as devmod
    dim = _dim_peak_that_maxes_gain()
    good = _peak_for_gain_code(0x2E)
    h = _WarmupHarness([dim, dim, good])
    codes = h.run()
    assert codes == (0x2E, 0x2E, 0x2E), codes
    assert h.runs == 3, h.runs
    assert h.sleeps == [devmod._WARMUP_RETRY_DELAY] * 2, h.sleeps
    print("test_warmup_retries_until_gain_stabilizes OK")


def test_warmup_gives_up_after_max_retries():
    """Lamp never warms: exactly 1 + _WARMUP_MAX_RETRIES runs, one
    delay per retry, and the maxed codes are returned rather than
    raising (the scan proceeds, possibly flat)."""
    from of135i import device as devmod
    dim = _dim_peak_that_maxes_gain()
    n = devmod._WARMUP_MAX_RETRIES
    h = _WarmupHarness([dim] * (n + 1))
    codes = h.run()
    assert codes == (calibrate._GAIN_MAX_CODE,) * 3, codes
    assert h.runs == n + 1, h.runs
    assert len(h.sleeps) == n, h.sleeps
    print(f"test_warmup_gives_up_after_max_retries OK ({h.runs} runs)")


def test_scan_sequence_matches_trace():
    mock = MockUsbIo(_build_cal_buffers())
    scanner = Scanner(mock)
    raw, width = scanner.scan(frame=1)

    assert width == tables.IMAGE_WIDTH == 3762
    assert len(raw) == tables.IMAGE_CHUNK_COUNT * tables.IMAGE_CHUNK_LEN

    expected = _expected_stream()
    actual = b"".join(mock.writes)

    assert actual == expected, (
        f"control-write stream mismatch: {len(actual)} B emitted, "
        f"{len(expected)} B expected (first divergence at byte "
        f"{next((i for i in range(min(len(actual), len(expected))) if actual[i] != expected[i]), min(len(actual), len(expected)))})"
    )
    print(
        f"test_scan_sequence_matches_trace OK "
        f"({len(mock.writes)} writes, {len(actual)} B)"
    )


def main() -> int:
    tests = [
        test_gain_codes_against_capture,
        test_offset_codes_fallback_on_zero_dark,
        test_offset_codes_from_reference_bracket,
        test_offset_codes_adapts_to_different_slope,
        test_shading_table_against_capture,
        test_warmup_no_retry_when_gain_normal,
        test_warmup_retries_until_gain_stabilizes,
        test_warmup_gives_up_after_max_retries,
        test_scan_sequence_matches_trace,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
