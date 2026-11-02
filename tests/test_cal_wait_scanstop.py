"""Opt-in stop gate after a calibration SCAN-clear (Scanner.cal_wait_scanstop).

Ported from bearyjd/opticfilm135i-linux, branch feat/cal-wait-scanstop
(f462b0b), onto this driver (issue #35; docs/cal-scanstop-gate.md).

EXPERIMENTAL, not hardware-verified on the owner's unit, motor-adjacent
(it delays the next calibration execute pulse). In the fork's hardware
logs, calibration reads that returned stale data were re-armed a few
milliseconds after the previous acquisition's SCAN-clear while that
acquisition was still running: the data-enable bit (register 0x100 bit 0)
and the motor-enable bit (register 0x101 bit 0) still read set. The
captured vendor stream reads both once after every calibration clear and
saw them clear (f055, dc55/d855).

Scanner.cal_wait_scanstop:
  "off"     -- (default) today's behaviour; the transfer sequence is
               byte-identical (no extra read, sleep or clock call). Pinned
               here against wire digests taken on the unmodified driver.
  "enforce" -- after the SCAN-clear (register 0x01 written with bit 0
               clear) that closes an execute pulse in a calibration
               acquisition phase, and before the next OUT transfer of any
               kind (the next phase's register writes, SCAN-set, execute
               pulse, buffer descriptor or the shading bulk-OUT): poll
               register 0x100 until bit 0 is clear, then register 0x101
               until bit 0 is clear. Only a 2-byte reply with the 0x55
               acknowledge counts. Both waits share ONE deadline, the
               existing non-strict poll floor; at that deadline
               safety.CalibrationError is raised before any further write
               and the session is FAILED.

All status values are synthetic. Pure offline logic: no USB, no scanner.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_safety import (FakeUsbDevice, _assert_failed_and_frozen,  # noqa: E402
                         _cal_buffers, _cli, cli_over, fast_time, make_scanner)

from of135i import cli, device, safety  # noqa: E402
from of135i.device import Scanner  # noqa: E402

FLOOR = device.NONSTRICT_POLL_FLOOR_S
R100, R101 = 0x0022, 0x0122


def _digest(wire_log):
    """sha256 over FakeUsbDevice.wire_log (host->device side of every
    completed transfer, in order)."""
    h = hashlib.sha256()
    for e in wire_log:
        h.update(repr(sorted((k, v.hex() if isinstance(v, bytes) else v)
                             for k, v in e.items())).encode())
    return h.hexdigest()


# (digest, transfer count) of a frame-1 scan over FakeUsbDevice(reg01=0x22,
# cal_buffers=_cal_buffers()) with fast_time(), taken on master 1d0091a
# BEFORE the gate existed (scratch script, 2026-11-02). "off" must
# reproduce them exactly; any change to the default wire fails here.
BASELINE = {
    "plain_3600_frame1": ("e4bc3bfcaa325096a07231e3403cf6c28ddc3f256b03ecc36eb02daf234b8cd4", 9789),
    "dual_600_frame1": ("06fc658adf2bc5d3a9c6c1203d68cb4c605c5987f1fe689c3207891c04808af9", 2069),
    "ir_3600_frame1": ("64f24a402c88a67a3d70b71c515c745dbf128714c96a208b7e72ef7d05e75eda", 24164),
}


@contextmanager
def sim_time(step: float = 0.01):
    """time.monotonic() advancing ``step`` per call and time.sleep(s)
    advancing the same clock by ``s``; yields the state so a fake can
    read the simulated time without advancing it."""
    state = {"t": 0.0}

    def mono():
        state["t"] += step
        return state["t"]

    def sleep(seconds):
        state["t"] += max(0.0, float(seconds))
    real_sleep, real_mono = time.sleep, time.monotonic
    time.sleep, time.monotonic = sleep, mono
    try:
        yield state
    finally:
        time.sleep, time.monotonic = real_sleep, real_mono


class GateFake(FakeUsbDevice):
    """FakeUsbDevice that, once the SCAN-clear closing an execute pulse in
    ``arm_phase`` has been written, answers reads of register 0x100 and
    0x101 from scripts until the next OUT transfer, and logs that window.

    A script is a list (one reply per read, the last one repeating) or a
    callable ``f(dt) -> bytes`` of the simulated seconds since the clear.
    A list item is an int (status byte, ack 0x55) or raw bytes."""

    def __init__(self, arm_phase, r100, r101, clock=None, read_hook=None,
                 clear_gap=0.0, **kw):
        super().__init__(reg01=0x22, cal_buffers=_cal_buffers(), **kw)
        self.read_hook = read_hook        # f(fake, wi) before each scripted reply
        self.clear_gap = clear_gap        # simulated host time right after the clear
        self.timeouts: list = []
        self.arm_phase = arm_phase
        self.scripts = {R100: r100, R101: r101}
        self.clock = clock
        self.scanner = None
        self.armed = False
        self._go = False
        self._n = {R100: 0, R101: 0}
        self.t_clear = None
        self.window: list[tuple] = []     # ("in", wi, reply, t) / ("out", phase)
        self.windows: list[list[tuple]] = []

    def _phase(self):
        return getattr(getattr(self.scanner, "session", None), "phase", None)

    def _now(self):
        return self.clock["t"] if self.clock is not None else 0.0

    def _reply(self, wi):
        sc = self.scripts[wi]
        if callable(sc):
            r = sc(self._now() - self.t_clear)
        else:
            r = sc[min(self._n[wi], len(sc) - 1)]
            self._n[wi] += 1
        return bytes([r, 0x55]) if isinstance(r, int) else bytes(r)

    def _on_out(self):
        if self.armed:
            self.window.append(("out", self._phase()))
            self.windows.append(self.window)
            self.armed = False

    def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        if not (bm & 0x80):
            self._on_out()
            r = super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)
            data = bytes(data_or_wLength or b"")
            if wv == 0x0083 and self._phase() == self.arm_phase:
                for i in range(0, len(data) - 1, 2):
                    reg, val = data[i], data[i + 1]
                    if reg == 0x0F and val == 0x01:
                        self._go = True
                    elif reg == 0x01 and not val & 0x01 and self._go:
                        self._go = False
                        self.armed = True
                        self.t_clear = self._now()
                        self._n = {R100: 0, R101: 0}
                        self.window = []
            return r
        if self.armed and br == 0x04 and wv == 0x018E and wi in (R100, R101):
            super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)
            self.timeouts.append(timeout)
            if not self.window and self.clock is not None:
                self.clock["t"] += self.clear_gap     # host gap before the first read
            if self.read_hook is not None:
                self.read_hook(self, wi)
            rep = self._reply(wi)
            self.window.append(("in", wi, rep, self._now()))
            return rep
        return super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)

    def write(self, endpoint, data, timeout=None):
        self._on_out()
        return super().write(endpoint, data, timeout)


def _run(arm_phase, r100, r101, mode="enforce", ir=False, step=0.01, **kw):
    exc = None
    with sim_time(step) as clock:
        fake = GateFake(arm_phase, r100, r101, clock=clock, **kw)
        s = make_scanner(fake)
        fake.scanner = s
        if mode is not None:
            s.cal_wait_scanstop = mode
        try:
            s.initialize(ir=ir)
            s.scan(frame=1, ir=ir)
        except safety.SafetyError as e:
            exc = e
    return fake, s, exc


def _ins(window):
    return [(wi, rep.hex()) for kind, *rest in window if kind == "in"
            for wi, rep in [rest[:2]]]


# ------------------------------------------------------------ table rows


def test_row1_waits_for_data_enable_clear_then_motor_enable():
    fake, s, exc = _run("cal_dark_a", [0xF1, 0xF1, 0xF0], [0xDC])
    assert exc is None, exc
    w = fake.windows[0]
    # Captured post-clear reads (f1, dc), then the gate: f1, f0, then dc.
    assert _ins(w) == [(R100, "f155"), (R101, "dc55"),
                       (R100, "f155"), (R100, "f055"), (R101, "dc55")]
    assert w[-1] == ("out", "cal_dark_b")


def test_row2_waits_until_motor_enable_clears():
    fake, s, exc = _run("cal_dark_a", [0xF0], [0xDD, 0xDD, 0xDC])
    assert exc is None, exc
    assert _ins(fake.windows[0]) == [(R100, "f055"), (R101, "dd55"),
                                     (R100, "f055"), (R101, "dd55"), (R101, "dc55")]


def _assert_gate_refused(fake, s, exc, phase):
    assert isinstance(exc, safety.CalibrationError), exc
    assert "scan-stop" in str(exc).lower() or "still running" in str(exc).lower()
    assert fake.armed, "no OUT transfer may follow the refusal"
    assert all(k == "in" for k, *_ in fake.window)
    # The refusal names the acquisition that armed the gate; the session
    # failure records the phase whose first write was withheld.
    assert f"after phase {phase}:" in str(exc)
    _assert_failed_and_frozen(fake, s, exc, operation="scan", phase_prefix="cal_")


def test_row3_data_enable_never_clears_raises_at_the_floor():
    fake, s, exc = _run("cal_dark_a", [0xF1], [0xDC])
    _assert_gate_refused(fake, s, exc, "cal_dark_a")
    ins = [e for e in fake.window if e[0] == "in"]
    gate = ins[2:]                    # after the two captured post-clear reads
    assert gate and all(wi == R100 for _, wi, *_ in gate), "0x101 read before 0x100 clear"
    assert gate[-1][3] - gate[0][3] <= FLOOR + 0.03
    assert gate[-1][3] - gate[0][3] >= FLOOR - 0.03


def test_row4_shading_upload_only_after_engine_stopped():
    fake, s, exc = _run("cal_shading_measure", [0xF3, 0xF3, 0xF0], [0xF9, 0xF9, 0xD8])
    assert exc is None, exc
    w = fake.windows[0]
    # Captured post-clear reads (f3 via the table's poll, f9), then the gate.
    assert _ins(w) == [(R100, "f355"), (R101, "f955"),
                       (R100, "f355"), (R100, "f055"), (R101, "f955"), (R101, "d855")]
    # The next OUT transfer is the upload phase's descriptor, then bulk-OUT.
    assert w[-1] == ("out", "cal_shading_upload")


@pytest.mark.parametrize("bad", [b"\xf0\x00", b"\xf0", b"\xf0\x55\x00"],
                         ids=["bad-ack", "short", "long"])
def test_row6_malformed_data_enable_reply_is_not_clear(bad):
    fake, s, exc = _run("cal_dark_a", [bad], [0xDC])
    _assert_gate_refused(fake, s, exc, "cal_dark_a")


@pytest.mark.parametrize("bad", [b"\xdc\x00", b"\xdc"], ids=["bad-ack", "short"])
def test_row6_malformed_motor_enable_reply_is_not_clear(bad):
    fake, s, exc = _run("cal_dark_a", [0xF0], [bad])
    _assert_gate_refused(fake, s, exc, "cal_dark_a")


def test_motor_enable_never_clears_raises():
    fake, s, exc = _run("cal_gain_check_a", [0xF0], [0xDD])
    _assert_gate_refused(fake, s, exc, "cal_gain_check_a")


def test_both_waits_share_one_deadline():
    # Data-enable clears 0.25 s after the clear; motor-enable never does.
    # One shared deadline: the refusal comes at the floor, not 0.25 + floor.
    def r100(dt):
        return bytes([0xF0 if dt >= 0.25 else 0xF1, 0x55])
    fake, s, exc = _run("cal_dark_a", r100, [0xDD])
    _assert_gate_refused(fake, s, exc, "cal_dark_a")
    ins = [e for e in fake.window if e[0] == "in"][2:]
    assert ins[-1][3] - fake.t_clear <= FLOOR + 0.05
    assert any(wi == R101 for _, wi, *_ in ins), "0x101 must be polled once 0x100 cleared"


@pytest.mark.parametrize("slow", [R100, R101], ids=["reg100", "reg101"])
def test_a_clear_reply_arriving_after_the_deadline_does_not_count(slow):
    # One gate read (of ``slow``) takes 0.4 s and returns "clear" past the
    # floor: that reply must not authorise the next write.
    def hook(fake, wi):
        if len([e for e in fake.window if e[0] == "in"]) >= 2 and wi == slow:
            fake.clock["t"] += 0.4
    fake, s, exc = _run("cal_dark_a", [0xF0], [0xDC], read_hook=hook)
    _assert_gate_refused(fake, s, exc, "cal_dark_a")
    w = s._cal_scanstop_waits[-1]
    assert w["outcome"] == "timeout"
    assert w["reads_101"] == (1 if slow == R101 else 0)


def test_every_gate_read_is_bounded_by_the_time_left():
    fake, s, exc = _run("cal_dark_a", [0xF1], [0xDC])
    assert isinstance(exc, safety.CalibrationError)
    gate = fake.timeouts[2:]          # after the two captured post-clear reads
    assert gate and all(isinstance(t, int) and 1 <= t <= FLOOR * 1000 for t in gate)
    assert gate == sorted(gate, reverse=True) and gate[-1] < gate[0]


class _Stall(Exception):
    pass


@pytest.mark.parametrize("late", [True, False])
def test_a_failed_gate_read_is_refused_or_propagated(late):
    def hook(fake, wi):
        if len([e for e in fake.window if e[0] == "in"]) >= 2:
            if late:
                fake.clock["t"] += 1.0          # the read timed out at the deadline
            raise _Stall("read failed")
    fake, s, exc = None, None, None
    with pytest.raises((_Stall, safety.CalibrationError)) as ei:
        with sim_time(0.01) as clock:
            fake = GateFake("cal_dark_a", [0xF0], [0xDC], clock=clock, read_hook=hook)
            s = make_scanner(fake)
            fake.scanner = s
            s.cal_wait_scanstop = "enforce"
            s.initialize()
            s.scan(frame=1)
    if late:
        assert isinstance(ei.value, safety.CalibrationError)
        assert isinstance(ei.value.__cause__, _Stall)
    else:
        assert type(ei.value) is _Stall
    assert fake.armed and all(k == "in" for k, *_ in fake.window)


def test_the_phase_own_post_clear_poll_counts_against_the_budget():
    # tables.CAL_SHADING_MEASURE (plain 3600) polls register 0x100 itself
    # after its clear. When that poll uses up the floor (a status class it
    # never accepts), the gate does not grant a second floor: refusal at
    # about one floor after the clear, and no clear reply later than that
    # is honoured.
    def r100(dt):
        return bytes([0xF0 if dt >= 0.45 else 0x91, 0x55])
    fake, s, exc = _run("cal_shading_measure", r100, [0xDC], step=0.001)
    _assert_gate_refused(fake, s, exc, "cal_shading_measure")
    assert fake.window[-1][3] - fake.t_clear <= FLOOR + 0.02


def test_a_quick_post_clear_poll_leaves_the_gate_its_budget():
    def r100(dt):
        return bytes([0xF0 if dt >= 0.2 else 0xF1, 0x55])     # class accepted at once
    fake, s, exc = _run("cal_shading_measure", r100, [0xDC], step=0.001)
    assert exc is None, exc
    assert fake.windows[0][-1] == ("out", "cal_shading_upload")


def test_a_host_gap_after_the_clear_is_not_a_refusal():
    # Host-side time between the clear and the next write (e.g. the lamp
    # warm-up's interval before it re-runs the white phase) is not part of
    # the gate's budget.
    fake, s, exc = _run("cal_white", [0xF0], [0xDC], clear_gap=5.0)
    assert exc is None, exc
    w = [x for x in s.last_diag["cal_scanstop_waits"] if x["phase"] == "cal_white"][0]
    assert w["outcome"] == "clear" and w["since_clear_s"] >= 5.0


def test_shading_verify_gate_runs_before_the_re_upload_not_before_position():
    # cal_shading_verify's SCAN-clear is followed, inside the phase, by the
    # re-upload of the second shading table: the gate fires before that
    # descriptor. Nothing arms after it, so POSITION and the image pass
    # (the _exec_ops NOTE about never waiting during the pass) are never
    # delayed.
    fake, s, exc = _run("cal_shading_verify", [0xF1, 0xF1, 0xF0], [0xDC])
    assert exc is None, exc
    assert len(fake.windows) == 1
    assert _ins(fake.windows[0])[-2:] == [(R100, "f055"), (R101, "dc55")]
    assert fake.windows[0][-1] == ("out", "cal_shading_verify")
    phases = [w["phase"] for w in s.last_diag["cal_scanstop_waits"]]
    assert phases[-1] == "cal_shading_verify"
    assert not any(p in ("position", "scan", "park", "cal_shading_upload") for p in phases)


@pytest.mark.parametrize("ir", [False, True], ids=["plain", "dual"])
@pytest.mark.parametrize("phase", sorted(device.CAL_ACQUISITION_PHASES))
def test_every_calibration_acquisition_is_gated(phase, ir):
    fake, s, exc = _run(phase, [0xF1], [0xDC], ir=ir)
    _assert_gate_refused(fake, s, exc, phase)


# ------------------------------------------------------------ scope


def _ops_run(phase, mode, last=None):
    """Execute pulse, SCAN-clear, then one register write (or ``last``), in
    ``phase``, against a device whose data-enable bit never clears."""
    fake = FakeUsbDevice(reg01=0x22)
    fake.status_high = 0xDD
    real = fake.ctrl_transfer

    def ct(bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        if (bm & 0x80) and wv == 0x018E and wi == R100:
            real(bm, br, wv, wi, data_or_wLength, timeout)
            return b"\xf1\x55"
        return real(bm, br, wv, wi, data_or_wLength, timeout)
    fake.ctrl_transfer = ct
    s = make_scanner(fake)
    s.cal_wait_scanstop = mode
    ops = [device.Op("cw", 0.0, bm=0x40, br=0x04, wv=0x0083, wi=0, data=bytes([0x0F, 0x01])),
           device.Op("cw", 0.0, bm=0x40, br=0x04, wv=0x0083, wi=0, data=bytes([0x01, 0x02])),
           last or device.Op("cw", 0.0, bm=0x40, br=0x04, wv=0x0083, wi=0,
                             data=bytes([0x10, 0x00]))]
    exc = None
    with sim_time(0.01):
        try:
            with s._operation("scan"):
                s.session.phase = phase
                s._exec_ops(ops)
        except safety.SafetyError as e:
            exc = e
    return fake, exc


@pytest.mark.parametrize("phase", ["scan", "position", "prep", "cal_rewrite", None])
def test_gate_not_applied_outside_calibration(phase):
    fake, exc = _ops_run(phase, "enforce")
    assert exc is None
    assert [e["t"] for e in fake.wire_log[-3:]] == ["cw", "cw", "cw"]


def test_same_ops_in_calibration_are_gated():
    fake, exc = _ops_run("cal_dark_a", "enforce")
    assert isinstance(exc, safety.CalibrationError)
    assert fake.wire_log[-1]["t"] == "cr"
    assert sum(e["t"] == "cw" for e in fake.wire_log[-5:]) <= 2


def test_bulk_out_directly_after_the_clear_is_gated():
    bo = device.Op("bo", 0.0, data=b"\x00" * 512)
    fake, exc = _ops_run("cal_shading_verify", "enforce", last=bo)
    assert isinstance(exc, safety.CalibrationError)
    assert not any(e["t"] == "bo" for e in fake.wire_log)
    fake, exc = _ops_run("cal_shading_verify", "off", last=bo)
    assert exc is None and fake.wire_log[-1]["t"] == "bo"


def test_clear_without_a_preceding_execute_pulse_does_not_arm():
    fake = FakeUsbDevice(reg01=0x22)
    s = make_scanner(fake)
    s.cal_wait_scanstop = "enforce"
    ops = [device.Op("cw", 0.0, bm=0x40, br=0x04, wv=0x0083, wi=0, data=bytes([0x01, 0x02])),
           device.Op("cw", 0.0, bm=0x40, br=0x04, wv=0x0083, wi=0, data=bytes([0x10, 0x00]))]
    with sim_time(0.01):
        with s._operation("scan"):
            s.session.phase = "cal_white"
            s._exec_ops(ops)
    assert [e["t"] for e in fake.wire_log[-2:]] == ["cw", "cw"]


# ------------------------------------------------------------ default traffic


def _frame1(mode, *, ir=False, dpi=3600, scripted=False):
    exc = None
    with sim_time(0.01) as clock:
        if scripted:
            fake = GateFake("cal_dark_a", [0xF1], [0xDD], clock=clock)
        else:
            fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        s = make_scanner(fake)
        if scripted:
            fake.scanner = s
        if mode is not None:
            s.cal_wait_scanstop = mode
        s.initialize(ir=ir)
        s.scan(frame=1, ir=ir, dpi=dpi)
    return fake, s


def _baseline_run(name, mode):
    """The exact run BASELINE was taken from: fast_time(), not sim_time."""
    ir, dpi = {"plain_3600_frame1": (False, 3600), "dual_600_frame1": (False, 600),
               "ir_3600_frame1": (True, 3600)}[name]
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    s = make_scanner(fake)
    if mode is not None:
        s.cal_wait_scanstop = mode
    with fast_time():
        s.initialize(ir=ir)
        s.scan(frame=1, ir=ir, dpi=dpi)
    return fake, s


@pytest.mark.parametrize("mode", [None, "off"], ids=["default", "off"])
@pytest.mark.parametrize("name", sorted(BASELINE))
def test_default_and_off_match_the_pre_gate_wire_digest(name, mode):
    fake, s = _baseline_run(name, mode)
    assert s.cal_wait_scanstop == "off"
    assert (_digest(fake.wire_log), len(fake.wire_log)) == BASELINE[name], name
    assert s.last_diag["cal_scanstop_waits"] == []


def test_off_mode_is_identical_even_while_the_engine_reads_running():
    f_default, _ = _frame1(None, scripted=True)
    f_off, s_off = _frame1("off", scripted=True)
    f_plain, _ = _frame1(None, scripted=False)
    assert f_default.wire_log == f_off.wire_log == f_plain.wire_log
    assert s_off.last_diag["cal_scanstop_waits"] == []


def _is_status_read(e):
    return e["t"] == "cr" and e["wv"] == 0x018E and e["wi"] in (R100, R101)


@pytest.mark.parametrize("ir,dpi", [(False, 3600), (False, 600), (True, 3600)],
                         ids=["plain3600", "dual600", "ir3600"])
def test_enforce_on_an_idle_engine_adds_only_status_reads(ir, dpi):
    f_base, _ = _frame1(None, ir=ir, dpi=dpi)
    f_enf, s = _frame1("enforce", ir=ir, dpi=dpi)
    strip = [e for e in f_enf.wire_log if not _is_status_read(e)]
    assert strip == [e for e in f_base.wire_log if not _is_status_read(e)]
    extra = (sum(map(_is_status_read, f_enf.wire_log))
             - sum(map(_is_status_read, f_base.wire_log)))
    waits = s.last_diag["cal_scanstop_waits"]
    assert len(waits) == 7                       # one per calibration acquisition
    assert extra == 2 * len(waits)               # one read of each register
    assert all(w["outcome"] == "clear" and w["reads_100"] == 1 and w["reads_101"] == 1
               for w in waits)
    assert [w["phase"] for w in waits] == [
        "cal_dark_a", "cal_dark_b", "cal_white", "cal_gain_check_a",
        "cal_gain_check_b", "cal_shading_measure", "cal_shading_verify"]


def test_cached_calibration_frame_is_not_gated():
    # Frame 2 of a plain-3600 session reuses the cached calibration
    # (cal_rewrite): no acquisition, no gate, an empty wait list.
    with sim_time(0.01):
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        s = make_scanner(fake)
        s.cal_wait_scanstop = "enforce"
        s.initialize()
        s.scan(frame=1)
        assert len(s.last_diag["cal_scanstop_waits"]) == 7
        s.initialize()
        s.scan(frame=2)
    assert s.last_diag["calibration"] == "cached"
    assert s.last_diag["cal_scanstop_waits"] == []


def test_diag_records_the_wait():
    fake, s, exc = _run("cal_dark_a", [0xF1, 0xF1, 0xF0], [0xDD, 0xDC])
    assert exc is None
    w = s.last_diag["cal_scanstop_waits"][0]
    assert w["phase"] == "cal_dark_a"
    assert w["reads_100"] == 2 and w["reads_101"] == 1
    assert w["last_100"] == "f055" and w["last_101"] == "dc55"
    assert w["outcome"] == "clear" and 0 < w["wait_s"] <= FLOOR
    assert w["since_clear_s"] >= w["wait_s"]


# ------------------------------------------------------------ property and CLI


def test_default_and_validation():
    s = make_scanner(FakeUsbDevice(reg01=0x22))
    assert s.cal_wait_scanstop == "off"
    s.cal_wait_scanstop = "enforce"
    assert s.cal_wait_scanstop == "enforce"
    for bad in ("observe", "on", True, None, ""):
        with pytest.raises(ValueError):
            s.cal_wait_scanstop = bad


@pytest.mark.parametrize("cmd", [["digitize", "-o", "x"], ["scan", "--frame", "1", "-o", "f.tiff"]])
def test_parsers_default_off_and_accept_enforce(cmd):
    parser = cli.build_parser()
    assert parser.parse_args(cmd).cal_wait_scanstop == "off"
    assert parser.parse_args(cmd + ["--cal-wait-scanstop", "enforce"]).cal_wait_scanstop == "enforce"
    with pytest.raises(SystemExit):
        parser.parse_args(cmd + ["--cal-wait-scanstop", "observe"])


def test_help_text_says_experimental():
    parser = cli.build_parser()
    for sub in ("scan", "digitize"):
        sp = parser._subparsers._group_actions[0].choices[sub]
        act = next(a for a in sp._actions if "--cal-wait-scanstop" in a.option_strings)
        assert "experimental" in act.help.lower() and "not hardware-verified" in act.help.lower()


@pytest.mark.parametrize("argv_mode,expected", [(None, "off"), ("off", "off"),
                                                ("enforce", "enforce")])
def test_scan_cli_flag_reaches_the_scanner(tmp_path, argv_mode, expected):
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    argv = ["scan", "--frame", "1", "-o", str(tmp_path / "f.tiff"), "--no-diag"]
    if argv_mode is not None:
        argv += ["--cal-wait-scanstop", argv_mode]
    # cli_over's fast clock jumps 1000 s per call: every gate read would be
    # past its deadline. A small simulated tick inside it instead.
    with cli_over(fake) as opened, sim_time(0.01):
        code, _out, err = _cli(argv)
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cal_wait_scanstop == expected
    assert code == 0, err


def test_digitize_sets_the_mode_on_its_scanner(tmp_path):
    fake = FakeUsbDevice(reg01=0x23)
    with cli_over(fake) as opened:
        _cli(["digitize", "-o", str(tmp_path), "--assume-loaded",
              "--cal-wait-scanstop", "enforce"])
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cal_wait_scanstop == "enforce"
    assert fake.out_count == 0
