"""Control-read mismatches are locatable in the diag (cr_mismatch_details).

Scanner._exec_ops reads a "cr" op once and counts a reply that differs
from the captured one (cr_mismatches). Each mismatch is also recorded
(phase, op index, wv/wi, got/want), capped per scan like
poll_timeout_details. Pure logic against a fake control endpoint: no
USB, no scanner.

Runs under pytest or as ``python tests/test_cr_mismatch_details.py``.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_safety import FakeUsbDevice, _cal_buffers, fast_time, make_scanner  # noqa: E402

from of135i.device import Scanner  # noqa: E402
from of135i.tables import Op  # noqa: E402

CAP = 20


class _Dev:
    def __init__(self, reply: bytes):
        self.reply = reply

    def ctrl_transfer(self, bm, br, wv, wi, length):
        return self.reply


def _scanner(reply: bytes, phase: str = "cal_dark_a") -> Scanner:
    s = Scanner.__new__(Scanner)
    s.io = types.SimpleNamespace(dev=_Dev(reply))
    s._diag_cr_mismatches = 0
    s._diag_cr_mismatch_details = []
    s.session = types.SimpleNamespace(
        phase=phase, operations=["scan"], write_allowed_or_final=lambda: None)
    return s


def _cr(want: str = "f855", wv=0x018E, wi=0x0122) -> Op:
    return Op("cr", 0.0, bm=0xC0, br=0x04, wv=wv, wi=wi, length=2,
              resp=bytes.fromhex(want))


def test_mismatch_is_recorded_with_location():
    s = _scanner(bytes.fromhex("f155"))
    s._exec_ops([_cr("f155"), _cr("f855", wv=0x008E, wi=0x3222)])
    assert s._diag_cr_mismatches == 1
    assert s._diag_cr_mismatch_details == [
        {"phase": "cal_dark_a", "idx": 1, "wv": 0x008E, "wi": 0x3222,
         "got": "f155", "want": "f855"},
    ]


def test_cap_holds_but_counter_keeps_counting():
    s = _scanner(bytes.fromhex("f155"))
    s._exec_ops([_cr("f855") for _ in range(CAP + 7)])
    assert s._diag_cr_mismatches == CAP + 7
    assert len(s._diag_cr_mismatch_details) == CAP
    assert s._diag_cr_mismatch_details[-1]["idx"] == CAP - 1


def test_matching_reply_records_nothing():
    s = _scanner(bytes.fromhex("f855"))
    s._exec_ops([_cr("f855")])
    assert s._diag_cr_mismatches == 0
    assert s._diag_cr_mismatch_details == []


def test_details_reset_between_scans():
    s = make_scanner(FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers()))
    with fast_time():
        s.initialize()
        s._diag_cr_mismatches = 5
        s._diag_cr_mismatch_details = [{"phase": "stale"}] * 5
        s.scan(frame=1)
        assert all(r["phase"] != "stale" for r in s._diag_cr_mismatch_details)
        s._diag_cr_mismatch_details = [{"phase": "stale"}]
        s.initialize()
        s.scan(frame=1)
    d = s.last_diag
    assert "cr_mismatch_details" in d
    assert all(r["phase"] != "stale" for r in d["cr_mismatch_details"])
    assert len(d["cr_mismatch_details"]) == min(d["cr_mismatches"], CAP)


if __name__ == "__main__":
    import inspect

    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
    print(f"{len(fns)} tests passed")
