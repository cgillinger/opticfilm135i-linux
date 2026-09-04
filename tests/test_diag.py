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
    assert report["magazine_loaded"] is True, report["magazine_loaded"]
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
        assert loaded["magazine_loaded"] is True

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


def main() -> int:
    tests = [
        test_known_read_regs,
        test_collect_doctor_and_format,
        test_format_doctor_never_raises_on_partial_report,
        test_sidecar_path,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
