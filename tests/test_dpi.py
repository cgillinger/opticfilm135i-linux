#!/usr/bin/env python3
"""Offline tests for the resolution profiles (of135i.tables_dpi600/1200/
2400/7200, Scanner._scan_dual at dpi != 3600).

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_dpi.py

Covers, for every dpi module:
  - STRUCTURE: the compiled geometry is self-consistent (chunk length =
    lines/chunk x width x 6, the default line count is whole chunks,
    the shading upload length is what calibrate builds for that width,
    the position phase carries a FEEDL batch with mode 0x18, the scan
    head carries the mode-0x30 batch with the line-count injection, the
    park phase starts with the end-of-access write) and the vendor's
    own captured line count/chunk count agree with the geometry.
  - SEQUENCE: Scanner.scan(frame=1, ir=True, dpi=<dpi>) run against a
    MOCK UsbIo (the same mock as tests/test_ir.py), with a small line
    count (two image chunks) so the 7200 dpi module stays cheap,
    asserting the emitted control-write stream equals the module's
    phase data with the injections pinned to synthetic measurements.
  - the second-upload formula reproduces the vendor's captured gain
    tables when fed the vendor's own measurements (only when the
    private measurement dumps are present -- skipped otherwise).
"""

from __future__ import annotations

import json
import struct
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import calibrate, device
from of135i.device import Scanner

REPO = Path(__file__).resolve().parents[1]
DPIS = (600, 1200, 2400, 7200)


def _pairs(data: bytes):
    return [(data[i], data[i + 1]) for i in range(0, len(data) - 1, 2)]


# -------------------------------------------------------------- structure


def test_structure():
    for dpi in DPIS:
        t = device.dual_tables(dpi)
        W = t.IMAGE_WIDTH
        assert t.DPI == dpi
        assert t.IMAGE_CHUNK_LEN == t.LINES_PER_CHUNK * W * 6, (dpi, t.IMAGE_CHUNK_LEN)
        assert t.DEFAULT_LINES == t.IMAGE_CHUNK_COUNT * t.LINES_PER_CHUNK
        assert t.DEFAULT_LINES % 2 == 0
        # One frame: about 5311 physical lines at 3600 dpi, scaled.
        physical = t.DEFAULT_LINES // 2
        want = 5311 * dpi / 3600
        assert abs(physical - want) <= t.LINES_PER_CHUNK, (dpi, physical, want)
        assert t.SHADING_UPLOAD_LEN == calibrate._shading_upload_len(W), (dpi, t.SHADING_UPLOAD_LEN)
        assert t.SHADING_LINES == 256
        assert struct.unpack("<II", t.IMAGE_DESC_DATA) == (0x10000000, t.IMAGE_CHUNK_LEN)
        assert t.CAPTURED_CHUNKS * t.LINES_PER_CHUNK <= t.CAPTURED_LINES
        # The vendor's strip captures: 374 + 12004 * dpi/600 lines (+-4).
        assert abs(t.CAPTURED_LINES - (374 + 12004 * dpi // 600)) <= 4, (dpi, t.CAPTURED_LINES)

        # Phase set and injection names.
        names = [p.name for p in t.PHASES]
        assert names == ["prep", "afe_base", "cal_dark_a", "cal_dark_b", "cal_white",
                         "cal_gain_check_a", "cal_gain_check_b", "cal_shading_measure",
                         "cal_shading_upload", "cal_shading_verify", "position", "scan", "park"], names
        assert set(t.CAL_GAIN_CHECK_A.injections) == {"gain_r", "gain_g", "gain_b"}
        assert set(t.CAL_SHADING_UPLOAD.injections) == {"shading_table_a", "shading_table_b"}
        assert set(t.CAL_SHADING_VERIFY.injections) == {"shading_table2_a", "shading_table2_b"}
        assert t.CAL_SHADING_VERIFY.split_at is not None
        assert set(t.POSITION.injections) == {"feedl_hi", "feedl_mid", "feedl_lo"}
        assert {"lines_hi", "lines_lo"} <= set(t.SCAN.injections)

        # Position: the FEEDL batch programs mode 0x18 and the captured
        # frame-1 target; the two slope uploads and the execute follow.
        _, fi, _ = t.POSITION.injections["feedl_hi"]
        fb = dict(_pairs(t.POSITION.ops[fi].data))
        assert fb[0x02] == 0x18 and (fb[0x3D] << 16 | fb[0x3E] << 8 | fb[0x3F]) == t.FEEDL_FRAME1
        assert t.FEEDL_FRAME1 == 6746 and t.FEEDL_PITCH == 10760
        bo = [op for op in t.POSITION.ops if op.kind == "bo"]
        assert len(bo) == 2 and bo[0].data == bo[1].data == t.SLOPE_TABLE_POSITION
        assert any(op.kind == "cw" and op.data == b"\x0f\x01" for op in t.POSITION.ops[fi:])
        assert t.POSITION.ops[-1].kind == "poll"

        # Scan: 3 identical slope uploads, then the mode-0x30 batch, then
        # the line-count batch (injection) before the first image read.
        scan0 = t.scan_phase(0).ops
        bo = [op for op in scan0 if op.kind == "bo"]
        assert len(bo) == 3 and all(b.data == t.SLOPE_TABLE_SCAN for b in bo)
        assert any(op.kind == "cw" and op.wv == 0x83 and (0x02, 0x30) in _pairs(op.data) for op in scan0)
        _, li, _ = t.SCAN.injections["lines_hi"]
        lb = dict(_pairs(t.SCAN.ops[li].data))
        assert (lb.get(0x25, 0) << 16 | lb[0x26] << 8 | lb[0x27]) == t.CAPTURED_LINES
        assert any(op.kind == "cw" and op.data == b"\x0f\x01" for op in scan0)
        # n chunks -> n descriptors + n blocks.
        for n in (0, 1, 2):
            ops = t.scan_phase(n).ops
            descs = [op for op in ops if op.kind == "cw" and op.wv == 0x82 and op.wi != 1]
            assert len(descs) == n, (dpi, n, len(descs))
            assert len(ops) == len(scan0) + n * (1 + len(t.IMAGE_BLOCK_OPS))
        assert len(t.SCAN.ops) == len(scan0) + t.IMAGE_CHUNK_COUNT * (1 + len(t.IMAGE_BLOCK_OPS))
        assert sum(op.length for op in t.IMAGE_BLOCK_OPS if op.kind == "bi") == t.IMAGE_CHUNK_LEN

        assert t.PARK.ops[0].kind == "cw" and t.PARK.ops[0].wv == 0x8D
        print(f"  {dpi:5d} dpi: width {W}, {t.LINES_PER_CHUNK} lines/chunk, "
              f"{t.IMAGE_CHUNK_COUNT} chunks = {t.DEFAULT_LINES} lines, "
              f"upload {t.SHADING_UPLOAD_LEN} B, {sum(len(p.ops) for p in t.PHASES)} ops")
    print("test_structure OK")


# -------------------------------------------------------------- MOCK UsbIo
# Same mock as tests/test_ir.py, parameterized by phase list.


class _FakeDev:
    def __init__(self, queues, writes, cal_buffers):
        self._queues = queues
        self._writes = writes
        self._cal_buffers = cal_buffers
        self._pending_read = None

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
    queues = {}
    for phase in phase_order:
        for op in phase.ops:
            if op.kind in ("cr", "poll"):
                queues.setdefault((op.bm, op.br, op.wv, op.wi), deque()).append(op.resp)
    return queues


class MockUsbIo:
    def __init__(self, phase_order, cal_buffers):
        self.writes = []
        self.dev = _FakeDev(_build_queues(phase_order), self.writes, cal_buffers)

    def write_regs(self, pairs):
        self.writes.append(bytes(b for pair in pairs for b in pair))

    def wait_reg(self, reg, value, timeout=0, mask=0xFF):
        return 0x22

    def end_access(self, which=0x8C, wIndex=16):
        pass

    def close(self):
        pass


def _captured_shading_offsets(t, phase, injection_name) -> np.ndarray:
    _, idxs = phase.injections[injection_name]
    payload = b"".join(phase.ops[i].data for i in idxs)[:t.SHADING_UPLOAD_LEN]
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


def _synthetic_measurement(t, a_off, b_off) -> bytes:
    W = t.IMAGE_WIDTH
    arr = np.zeros((t.SHADING_LINES, W, 3), dtype="<u2")
    arr[0::2] = np.broadcast_to(a_off.reshape(W, 3), (t.SHADING_LINES // 2, W, 3))
    arr[1::2] = np.broadcast_to(b_off.reshape(W, 3), (t.SHADING_LINES // 2, W, 3))
    return arr.tobytes()


def _white_buffer(t, gain_codes) -> bytes:
    """A 2-line white buffer at the captured white width whose odd
    (visible) line makes gain_codes() return `gain_codes` exactly."""
    white_len = [op for op in t.CAL_WHITE.ops if op.kind == "cw" and op.wv == 0x82][0]
    _, ln = struct.unpack("<II", white_len.data)
    n_px = ln // 12
    white = np.zeros((2, n_px, 3), dtype=np.uint16)
    white[0] = 40000
    for ch, code in enumerate(gain_codes):
        approx = round(calibrate._GAIN_DIVISOR * calibrate._GAIN_TARGET / code)
        for peak in range(max(1, approx - 8), approx + 9):
            if round(calibrate._GAIN_DIVISOR * calibrate._GAIN_TARGET / peak) == code:
                white[1, :, ch] = peak
                break
        else:
            raise AssertionError(code)
    assert calibrate.gain_codes(white[1].reshape(-1, 3)) == tuple(gain_codes)
    return white.astype("<u2").tobytes(), ln


def _run_sequence(dpi):
    t = device.dual_tables(dpi)
    W = t.IMAGE_WIDTH
    n_chunks = 2
    n_lines = n_chunks * t.LINES_PER_CHUNK
    scan = t.scan_phase(n_chunks)
    order = [t.CAL_DARK_A, t.CAL_DARK_B, t.CAL_WHITE, t.CAL_GAIN_CHECK_A, t.CAL_GAIN_CHECK_B,
             t.CAL_SHADING_MEASURE, t.CAL_SHADING_UPLOAD, t.CAL_SHADING_VERIFY, t.POSITION, scan, t.PARK]

    gc = t.CAL_GAIN_CHECK_A
    codes = tuple(gc.ops[gc.injections[k][1]].data[gc.injections[k][2]] for k in ("gain_r", "gain_g", "gain_b"))
    a_off = _captured_shading_offsets(t, t.CAL_SHADING_UPLOAD, "shading_table_a")
    b_off = _captured_shading_offsets(t, t.CAL_SHADING_UPLOAD, "shading_table_b")
    # Cross-connection: table A is built from ODD/visible measurement,
    # table B from EVEN/IR — swap even/odd so each half reproduces its
    # target vendor table (even=b_off→table B, odd=a_off→table A).
    meas = _synthetic_measurement(t, b_off, a_off)
    white, white_len = _white_buffer(t, codes)
    cal_buffers = {white_len: deque([white]), len(meas): deque([meas, meas])}

    mock = MockUsbIo(order, cal_buffers)
    scanner = Scanner(mock)
    raw, width, meta = scanner.scan(frame=1, ir=True, dpi=dpi, lines=n_lines)
    assert width == W and meta == {"width": W, "alternating": True, "dpi": dpi}
    assert len(raw) == n_chunks * t.IMAGE_CHUNK_LEN

    # Per-scan diagnostics (of135i.diag / device.py Part 2), dual-light
    # path -- mirrors tests/test_calibrate.py's same check.
    d = scanner.last_diag
    assert isinstance(d, dict), d
    assert d["dual"] is True and d["dpi"] == dpi, d
    assert d["warmup_attempts"] == 1, d["warmup_attempts"]
    assert "cal_white" in d["phase_seconds"] and "scan" in d["phase_seconds"], d["phase_seconds"]
    assert isinstance(d["poll_timeouts"], int) and isinstance(d["cr_mismatches"], int), d
    json.dumps(d, default=str)  # must be JSON-serializable

    # Expected stream.
    off_r, off_g, off_b = calibrate.offset_codes(np.zeros((4, 3), np.uint16), np.zeros((4, 3), np.uint16))
    feedl = t.feedl_for_frame(1)
    sm = np.frombuffer(meas, dtype="<u2").reshape(t.SHADING_LINES, W, 3)
    sm_ir, sm_vis = sm[0::2], sm[1::2]   # even=IR (b_off), odd=visible (a_off)
    # Cross-connection: table A from visible, table B from IR.
    inj = {
        "cal_gain_check_a": dict(gain_r=bytes([codes[0]]), gain_g=bytes([codes[1]]), gain_b=bytes([codes[2]])),
        "cal_shading_measure": dict(
            offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
            offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
            offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF])),
        "cal_shading_upload": dict(shading_table_a=calibrate.shading_table(sm_vis, width=W),
                                   shading_table_b=calibrate.shading_table(sm_ir, width=W)),
        "cal_shading_verify": dict(
            shading_table2_a=calibrate.shading_table2_dual(sm_vis, sm_vis, width=W, target=calibrate.SHADING2_TARGET_A),
            shading_table2_b=calibrate.shading_table2_dual(sm_ir, sm_ir, width=W, target=calibrate.SHADING2_TARGET_B)),
        "position": dict(feedl_hi=bytes([(feedl >> 16) & 0xFF]), feedl_mid=bytes([(feedl >> 8) & 0xFF]),
                         feedl_lo=bytes([feedl & 0xFF])),
        "scan": dict(lines_top=bytes([(n_lines >> 16) & 0xFF]), lines_hi=bytes([(n_lines >> 8) & 0xFF]),
                     lines_lo=bytes([n_lines & 0xFF])),
    }
    expected = bytearray()
    for phase in order:
        ops = phase.patched(**inj[phase.name]) if phase.name in inj else phase.ops
        expected += b"".join(op.data for op in ops if op.kind in ("cw", "bo"))
    actual = b"".join(mock.writes)
    assert actual == bytes(expected), (
        f"{dpi} dpi: stream mismatch, {len(actual)} B vs {len(expected)} B, first divergence at "
        f"{next((i for i in range(min(len(actual), len(expected))) if actual[i] != expected[i]), min(len(actual), len(expected)))}")
    # The first upload reproduces the vendor's captured payload pairs
    # (offsets = the synthetic measurement, gain 0x4000). Payload pairs
    # only: in these captures the two filler pairs closing each 512 B
    # block hold stale offset-like values (gain 0) instead of the zeros
    # the 3600 dpi captures carry -- no rule fits them, and the 3600
    # flows prove the scanner ignores them, so we keep writing zeros.
    up = t.CAL_SHADING_UPLOAD
    for name in ("shading_table_a", "shading_table_b"):
        _, idxs = up.injections[name]
        captured = b"".join(up.ops[i].data for i in idxs)[:t.SHADING_UPLOAD_LEN]
        assert _unpack(inj["cal_shading_upload"][name], W) == _unpack(captured, W), (dpi, name)
    return len(mock.writes), len(actual)


def _unpack(payload: bytes, width: int):
    """(offsets, gains) payload pairs of a shading upload, filler pairs
    stripped (126 payload + 2 filler pairs per full 512 B block)."""
    n_pairs = width * 3
    offs, gains = [], []
    p = i = 0
    while i < n_pairs:
        k = 126 if n_pairs - i >= 126 else n_pairs - i
        trailer = 2 if n_pairs - i >= 126 else 0
        arr = np.frombuffer(payload[p:p + k * 4], dtype="<u2").reshape(-1, 2)
        offs.extend(arr[:, 0].tolist())
        gains.extend(arr[:, 1].tolist())
        p += (k + trailer) * 4
        i += k
    return offs, gains


def test_scan_sequences():
    for dpi in DPIS:
        n, nb = _run_sequence(dpi)
        print(f"  {dpi:5d} dpi: {n} writes, {nb} B")
    print("test_scan_sequences OK")


# ------------------------------------------- vendor second-upload formula


def test_second_upload_formula_against_capture():
    """calibrate.shading_table2_dual() vs the vendor's captured re-upload,
    given the vendor's own measurements (private dumps, skipped if
    absent): within the illuminated window the gains must agree to
    within the measurement noise (0.2 % at the 99th percentile)."""
    import gzip, json
    dumps = Path.home() / "Dokument/plustek-135i-analys/dpi-data"
    checked = 0
    for dpi in DPIS:
        binf = dumps / f"{dpi}-bulkin.bin"
        if not binf.exists():
            continue
        t = device.dual_tables(dpi)
        W = t.IMAGE_WIDTH
        trace = REPO / "traces" / f"20260902-vendor-{dpi}dpi.trace.json.gz"
        ops = json.load(gzip.open(trace, "rt"))
        data = np.memmap(binf, dtype=np.uint8, mode="r")
        # Walk the trace's bulk-in ops to slice the two 256-line buffers.
        pos, cur, bufs = 0, None, []
        for op in ops:
            if op["t"] == "cw" and op["wv"] == 0x82 and len(op["data"]) == 16:
                if cur is not None:
                    bufs.append(cur)
                    cur = None
                addr, ln = struct.unpack("<II", bytes.fromhex(op["data"]))
                if op["wi"] != 1 and ln == t.SHADING_LINES * W * 6:
                    cur = bytearray()
            elif op["t"] == "bi":
                n = op["len"]
                if pos + n > len(data):
                    break
                if cur is not None:
                    cur.extend(bytes(data[pos:pos + n]))
                pos += n
            if len(bufs) == 2:
                break
        if len(bufs) < 2:
            continue
        meas = np.frombuffer(bytes(bufs[0]), dtype="<u2").reshape(t.SHADING_LINES, W, 3)
        ver = np.frombuffer(bytes(bufs[1]), dtype="<u2").reshape(t.SHADING_LINES, W, 3)
        for name, sl, target in (("shading_table2_a", slice(0, None, 2), calibrate.SHADING2_TARGET_A),
                                 ("shading_table2_b", slice(1, None, 2), calibrate.SHADING2_TARGET_B)):
            ours = calibrate.shading_table2_dual(ver[sl], meas[sl], width=W, target=target)
            _, idxs = t.CAL_SHADING_VERIFY.injections[name]
            theirs = b"".join(t.CAL_SHADING_VERIFY.ops[i].data for i in idxs)[:t.SHADING_UPLOAD_LEN]
            assert len(ours) == len(theirs)
            o_ours, g_ours = (np.array(x) for x in _unpack(ours, W))
            o_theirs, g_theirs = (np.array(x) for x in _unpack(theirs, W))
            lit = g_theirs < 65535
            rel = np.abs(g_ours[lit] - g_theirs[lit]) / g_theirs[lit]
            # Residual is the measurement noise of a 128-line mean
            # (~0.07 % at the 99th percentile on every capture).
            assert np.percentile(rel, 99) <= 0.002, (dpi, name, np.percentile(rel, 99), rel.max())
            assert rel.max() <= 0.005, (dpi, name, rel.max())
            assert np.abs(o_ours - o_theirs).mean() < 2.0, (dpi, name)
            checked += 1
    if checked:
        print(f"test_second_upload_formula_against_capture OK ({checked} tables)")
    else:
        print("test_second_upload_formula_against_capture SKIPPED (private measurement dumps not present)")


def main() -> int:
    tests = [test_structure, test_scan_sequences, test_second_upload_formula_against_capture]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
