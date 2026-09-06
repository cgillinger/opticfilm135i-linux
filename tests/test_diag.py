#!/usr/bin/env python3
"""Offline tests for of135i.diag -- no hardware required.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_diag.py

Covers:
  - known_read_regs(): non-empty, sorted, plausible register range,
    contains the always-present status registers.
  - collect_doctor()/format_doctor() against a minimal fake io (no
    real USB device) -- read-only by construction (the fake has no
    write methods for collect_doctor to accidentally call).
  - write_sidecar()'s JSON round-trip and sidecar_path()'s naming.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import diag


# ------------------------------------------------------------- known_read_regs


def test_known_read_regs():
    regs = diag.known_read_regs()
    assert len(regs) > 0, "known_read_regs() must not be empty"
    assert list(regs) == sorted(regs), "must be sorted"
    assert len(set(regs)) == len(regs), "must be de-duplicated"
    assert 0x01 in regs, "reg 0x01 (engine/ready bit) must be in the read set"
    assert 0x32 in regs, "reg 0x32 (loader/transport state) must be in the read set"
    for reg in regs:
        assert reg <= 0xFF or 0x100 <= reg <= 0x1FF, f"register {reg:#x} out of range"
    print(f"test_known_read_regs OK ({len(regs)} registers)")


# -------------------------------------------------------------- fake doctor io


class _FakeDev:
    """Minimal duck type for usb.core.Device -- just enough for
    collect_doctor()'s chip_id read and USB descriptor fields. No
    write-capable methods exist here at all, so collect_doctor cannot
    accidentally write to it even if a bug tried."""

    idVendor = 0x07B3
    idProduct = 0x1436
    bcdDevice = 0x0100
    bus = 1
    address = 5

    def ctrl_transfer(self, bm, br, wv, wi, data_or_length):
        return b"\x00"


class _FakeIo:
    """Minimal duck type for UsbIo -- read-only methods only, per
    collect_doctor()'s contract."""

    def __init__(self):
        self.dev = _FakeDev()

    def read_reg(self, reg: int) -> int:
        return 0x22 if reg == 0x01 else 0

    def read_ext_reg(self, reg: int) -> int:
        return 0x08

    def read_status_word(self) -> int:
        return 0xF855

    def read_button(self):
        return None


def test_collect_doctor_and_format():
    report = diag.collect_doctor(_FakeIo())

    assert report["state"]["name"] == "idle-homed", report["state"]
    assert report["magazine_present"] is True, report["magazine_present"]
    assert report["button"] == "idle", report["button"]

    text = diag.format_doctor(report)
    assert isinstance(text, str) and text
    assert "0x01=0x22" in text, text

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "report.json"
        diag.write_sidecar(str(path), report)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["state"]["name"] == "idle-homed"
        assert loaded["magazine_present"] is True

    print("test_collect_doctor_and_format OK")


def test_format_doctor_never_raises_on_partial_report():
    # A report missing most keys (as if every guarded item failed)
    # must still render without raising.
    text = diag.format_doctor({})
    assert isinstance(text, str) and text
    print("test_format_doctor_never_raises_on_partial_report OK")


# ----------------------------------------------------------------- sidecar path


def test_sidecar_path():
    assert diag.sidecar_path("foo.tiff") == "foo.diag.json"
    assert diag.sidecar_path("dir/x.pnm") == str(Path("dir") / "x.diag.json")
    assert diag.sidecar_path("rulle-f2.tiff") == "rulle-f2.diag.json"
    print("test_sidecar_path OK")


# --------------------------------- calibration buffer dump (Test 29)


def _u16_buffer(rows):
    """rows: list of [R, G, B] triplets -> raw <u2 interleaved bytes,
    exactly how the driver's dark buffers arrive on the wire."""
    return np.asarray(rows, dtype="<u2").tobytes()


def test_cal_buffer_dump_flags_bit_identical_channels():
    """SYNTHETIC -- NOT a reproduction of the B5 fault (its raw buffer was
    never retained, Test 29). A dark_b whose three channels are
    bit-identical ([v, v, v] per triplet) must be flagged
    channels_bit_identical: the metadata the three-equal-means summary
    could not prove."""
    n = 300
    dark_a = _u16_buffer([[21600, 24400, 23350]] * n)   # normal per-channel
    dark_b = _u16_buffer([[26177, 26177, 26177]] * n)   # SYNTHETIC collapse
    with tempfile.TemporaryDirectory() as d:
        meta_path = diag.dump_calibration_buffers(
            d, "synthetic", {"kind": "synthetic"},
            {"dark_a": dark_a, "dark_b": dark_b})
        rec = json.load(open(meta_path))
        assert (Path(d) / "synthetic-dark_a.bin").read_bytes() == dark_a
        assert (Path(d) / "synthetic-dark_b.bin").read_bytes() == dark_b
        b = rec["buffers"]
        assert b["dark_b"]["channels_bit_identical"] is True
        assert b["dark_b"]["triplets_rgb_equal_frac"] == 1.0
        assert b["dark_a"]["channels_bit_identical"] is False
        assert b["dark_b"]["byte_len"] == len(dark_b)
        assert len(set(b["dark_b"]["channel_sha256"])) == 1
    print("test_cal_buffer_dump_flags_bit_identical_channels OK")


def test_cal_buffer_dump_normal_reference_not_flagged():
    """Normal per-channel dark buffers must NOT be flagged bit-identical
    -- the reference case any future validity check must keep accepting."""
    n = 300
    dark_a = _u16_buffer([[21411, 27770, 24897]] * n)
    dark_b = _u16_buffer([[23644, 30052, 27174]] * n)
    with tempfile.TemporaryDirectory() as d:
        meta_path = diag.dump_calibration_buffers(
            d, "ref", {}, {"dark_a": dark_a, "dark_b": dark_b})
        rec = json.load(open(meta_path))
        for name in ("dark_a", "dark_b"):
            assert rec["buffers"][name]["channels_bit_identical"] is False
            assert rec["buffers"][name]["reshape_ok"] is True
        assert rec["byte_identical_buffers"] == {}
    print("test_cal_buffer_dump_normal_reference_not_flagged OK")


def test_cal_buffer_dump_short_transfer_flagged():
    """A truncated/short read (length not a clean (N, 3) u16 buffer) must
    be flagged reshape_ok False rather than silently reshaped."""
    short = b"\x00\x01\x02\x03\x04"  # 5 bytes, not divisible by 6
    with tempfile.TemporaryDirectory() as d:
        meta_path = diag.dump_calibration_buffers(d, "short", {}, {"dark_b": short})
        rec = json.load(open(meta_path))
        assert rec["buffers"]["dark_b"]["reshape_ok"] is False
        assert rec["buffers"]["dark_b"]["byte_len"] == 5
    print("test_cal_buffer_dump_short_transfer_flagged OK")


def test_cal_buffer_dump_detects_buffer_reuse():
    """If dark_b were byte-identical to dark_a (buffer reuse / stale RAM),
    byte_identical_buffers must record it -- the direct reuse signal the
    mean-only summary cannot show."""
    n = 128
    same = _u16_buffer([[100, 200, 300]] * n)
    with tempfile.TemporaryDirectory() as d:
        meta_path = diag.dump_calibration_buffers(
            d, "reuse", {}, {"dark_a": same, "dark_b": bytes(same)})
        rec = json.load(open(meta_path))
        assert "dark_b" in rec["byte_identical_buffers"].get("dark_a", [])
    print("test_cal_buffer_dump_detects_buffer_reuse OK")


def test_equal_means_do_not_imply_bit_identical_channels():
    """Regression for the corrected claim (Test 30): three equal channel
    MEANS do not imply identical channels or identical value sets. This
    synthetic buffer averages to exactly 26177.375 in all three channels
    yet has different per-channel values. channels_bit_identical must be
    False and the three per-channel sha256 values must differ -- proving
    the checksum, not the mean, is what actually decides bit-identity."""
    rows = ([[26177, 26176, 26175]]
            + [[26177, 26177, 26177]] * 6
            + [[26180, 26181, 26182]])
    raw = np.asarray(rows, dtype="<u2").tobytes()
    stats = diag._buffer_stats(raw)
    assert stats["channel_mean"] == [26177.375, 26177.375, 26177.375], stats["channel_mean"]
    assert stats["channels_bit_identical"] is False, stats
    assert len(set(stats["channel_sha256"])) == 3, stats["channel_sha256"]
    print("test_equal_means_do_not_imply_bit_identical_channels OK")


def test_cal_buffer_dump_records_transfer_reads():
    """The optional `reads` record (per-bulk-transfer requested vs
    returned length + exception) is stored verbatim, so a short read of
    even a few bytes is visible where the total length would miss it. A
    read whose length is unknown after an exception is returned=None,
    distinct from a genuine zero-byte read (returned=0). (The scan-path
    integration of this capture is verified in test_calibrate.py.)"""
    reads = [
        {"seq": 1, "phase": "cal_dark_b", "frame": 2, "requested": 3072,
         "returned": 3066, "exception": None},               # 6 bytes short
        {"seq": 2, "phase": "cal_dark_b", "frame": 4, "requested": 3072,
         "returned": None, "exception": "USBError(...)"},     # length unknown
        {"seq": 3, "phase": "cal_white", "frame": 4, "requested": 3072,
         "returned": 0, "exception": None},                   # genuine zero
    ]
    with tempfile.TemporaryDirectory() as d:
        meta_path = diag.dump_calibration_buffers(
            d, "reads", {}, {"dark_b": _u16_buffer([[1, 2, 3]] * 8)},
            reads=reads)
        rec = json.load(open(meta_path))
        assert rec["reads"] == reads
        assert rec["reads"][1]["returned"] is None   # unknown
        assert rec["reads"][2]["returned"] == 0       # genuine zero
    print("test_cal_buffer_dump_records_transfer_reads OK")


def main() -> int:
    tests = [
        test_known_read_regs,
        test_collect_doctor_and_format,
        test_format_doctor_never_raises_on_partial_report,
        test_sidecar_path,
        test_cal_buffer_dump_flags_bit_identical_channels,
        test_cal_buffer_dump_normal_reference_not_flagged,
        test_cal_buffer_dump_short_transfer_flagged,
        test_cal_buffer_dump_detects_buffer_reuse,
        test_equal_means_do_not_imply_bit_identical_channels,
        test_cal_buffer_dump_records_transfer_reads,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
