#!/usr/bin/env python3
"""Offline hardware-safety tests for the of135i driver -- no hardware.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_safety.py

Every test here drives the REAL transport (of135i.usbio.UsbIo) and the
REAL driver (of135i.device.Scanner) over a fake pyusb device
(FakeUsbDevice below) that models the scanner's registers and counts
every OUT transfer at the pyusb boundary -- i.e. every byte that would
have reached the USB bus. "Zero writes" in these tests means
FakeUsbDevice.out_count did not change, not merely that an exception
was raised.

Covers (docs/hardware-safety.md):
  - start-state matrix: 0x22 / 0x00 / 0x02 / 0x23 / unknown / read
    timeout / malformed reply / USB error, for every public writing
    entry point (Scanner methods, CLI scan/eject/watch, hwblock warm/
    cold, the raw-io verify used by replay_trace.py);
  - the cold path: 0x00 permits cold_init only, and only once; the
    post-cold-init verification of reg 0x01;
  - fault injection at every point in the task list (before the first
    write, after the first register write, before/after an execute
    pulse, calibration bulk IN, image bulk IN, PARK, between batch
    frames, eject, magazine load) plus KeyboardInterrupt, verifying
    the session fails, records phase/write/execute history, sends NO
    recovery command, and refuses any further operation with zero
    writes; a new session over the left-behind state is rejected
    unless the state is an accepted start state again;
  - process lock (a second process, writing or read-only, is refused
    before touching the device; lock released on close/exception);
  - doctor/status strictly read-only: zero OUT transfers, no pyusb
    state-changing calls, no initialization, works on an interrupted
    (0x23) scanner and gives power-cycle advice;
  - tables_load.LOAD byte-identical to the trace slice the old tool
    replayed (when the trace file is present).
"""

from __future__ import annotations

import io as _io
import logging
import os
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tools"))

import numpy as np  # noqa: E402
import usb.core  # noqa: E402

from of135i import cli, device, diag, safety, tables, tables_base, tables_load  # noqa: E402
from of135i.device import Scanner  # noqa: E402
from of135i.safety import (  # noqa: E402
    OperationNotAllowedError, ReadOnlySessionError, ScannerBusyError, SessionFailedError,
    SessionState, StartState, UnsafeStartStateError,
)
from of135i.usbio import UsbIo  # noqa: E402

# Quiet: the fake register model makes many captured polls "time out"
# (instantly, see _FastClock), each of which logs a warning.
logging.getLogger("of135i").setLevel(logging.CRITICAL)
logging.getLogger("of135i.hwblock").setLevel(logging.CRITICAL)


# ============================================================ fake device


def _usb_timeout() -> usb.core.USBTimeoutError:
    return usb.core.USBTimeoutError("Operation timed out", errno=110)


def _usb_error() -> usb.core.USBError:
    return usb.core.USBError("Pipe error", errno=32)


class FakeUsbDevice:
    """Stands in for pyusb's usb.core.Device at the wire boundary.

    Register model: control OUT wValue=0x83 batches update `regs`, so a
    write of 0x01=0x23 (SCAN phase) or 0x01=0x22 (COLD_INIT/BASE table)
    is what a later read of reg 0x01 returns -- exactly the mechanism
    by which an interrupted real scan is left at 0x23. Extended reads
    (wValue 0x018e) return the magazine sensor for reg 0x101 and a
    "done" status word (0xf855) otherwise. Bulk IN serves scripted
    buffers by descriptor length (cal_buffers), else zeros.

    Counting: out_count/out_log count every completed OUT transfer
    (control OUT + bulk OUT); pulses counts completed execute pulses.
    A transfer the fault hook aborts is NOT counted as completed (it
    never reached the device in full).

    Faults: `fault(event)` is called before every transfer with a dict
    describing it (kind, wv/wi/data or length, out_index, pulse,
    pulses_so_far, ...); returning an exception raises it instead of
    performing the transfer.

    reg01: an int is the initial register value; an Exception instance
    is raised on every read of reg 0x01; bytes are returned raw
    (malformed reply); a callable is called with no arguments.
    """

    idVendor = 0x07B3
    idProduct = 0x1436
    bcdDevice = 0x0100
    bus = 1
    address = 7
    bNumConfigurations = 1
    iManufacturer = 0
    iProduct = 0
    iSerialNumber = 0

    class _Ctx:
        """What usb.util.dispose_resources() talks to on close(): a
        handle release, never a transfer."""
        def dispose(self, device):
            device.disposed = True

    def __init__(self, reg01=0x22, magazine: bool = True, fault=None, cal_buffers=None):
        self._ctx = self._Ctx()
        self.disposed = False
        self.regs = {0x01: 0x22, 0x31: 0xFC, 0x32: 0x1F, 0x35: 0xBB, 0x15: 0x90}
        self.reg01_reply = None
        if isinstance(reg01, int):
            self.regs[0x01] = reg01
        else:
            self.reg01_reply = reg01
        self.magazine = magazine
        self.fault = fault
        self.cal_buffers = cal_buffers or {}
        self.out_count = 0
        self.in_count = 0
        self.bulk_in_count = 0
        self.pulses = 0
        self.out_log: list[dict] = []
        self.blocked_calls: list[str] = []
        self.button_events: deque = deque()
        self._pending: list | None = None

    # ------------------------------------------------------------ faults

    def _maybe_fail(self, ev: dict) -> None:
        if self.fault is None:
            return
        exc = self.fault(ev)
        if exc is not None:
            raise exc

    # ------------------------------------------------------- transfers

    def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        if not (bm & 0x80):
            data = bytes(data_or_wLength) if data_or_wLength is not None else b""
            pulse = (wv == 0x0083 and safety.has_execute_pulse(data))
            ev = {"kind": "ctrl_out", "bm": bm, "br": br, "wv": wv, "wi": wi, "data": data,
                  "out_index": self.out_count + 1, "pulse": pulse, "pulses_so_far": self.pulses}
            self._maybe_fail(ev)
            self.out_count += 1
            self.out_log.append(ev)
            if wv == 0x0083:
                for i in range(0, len(data) - 1, 2):
                    self.regs[data[i]] = data[i + 1]
                if pulse:
                    self.pulses += 1
            if br == 0x04 and wv == 0x0082 and len(data) == 8:
                _addr, ln = struct.unpack("<II", data)
                if wi == 1:
                    self._pending = None
                else:
                    q = self.cal_buffers.get(ln)
                    self._pending = [q.popleft() if q else bytes(ln), 0]
            return len(data)

        length = data_or_wLength
        ev = {"kind": "ctrl_in", "bm": bm, "br": br, "wv": wv, "wi": wi, "length": length,
              "pulses_so_far": self.pulses}
        self._maybe_fail(ev)
        self.in_count += 1
        if br == 0x04 and wv == 0x008E:
            reg = wi >> 8
            if reg == 0x01 and self.reg01_reply is not None:
                r = self.reg01_reply
                if isinstance(r, BaseException):
                    raise r
                if callable(r):
                    r = r()
                if isinstance(r, BaseException):
                    raise r
                return bytes(r)
            return bytes([self.regs.get(reg, 0) & 0xFF, 0x55])
        if br == 0x04 and wv == 0x018E:
            reg = 0x100 | (wi >> 8)
            if reg == 0x101:
                return bytes([0xE8 if self.magazine else 0xE0, 0x55])
            return bytes([0xF8, 0x55])
        return bytes(length or 0)

    def read(self, endpoint, size_or_buffer, timeout=None):
        length = int(size_or_buffer)
        if endpoint == 0x83:
            if self.button_events:
                ev = self.button_events.popleft()
                if isinstance(ev, BaseException):
                    raise ev
                return bytes([ev])
            raise _usb_timeout()
        ev = {"kind": "bulk_in", "ep": endpoint, "length": length,
              "bulk_index": self.bulk_in_count + 1, "pulses_so_far": self.pulses}
        self._maybe_fail(ev)
        self.bulk_in_count += 1
        if self._pending is not None:
            buf, off = self._pending
            chunk = buf[off:off + length]
            self._pending[1] = off + length
            if len(chunk) < length:
                chunk += bytes(length - len(chunk))
            return chunk
        return bytes(length)

    def write(self, endpoint, data, timeout=None):
        data = bytes(data)
        ev = {"kind": "bulk_out", "ep": endpoint, "length": len(data),
              "out_index": self.out_count + 1, "pulse": False, "pulses_so_far": self.pulses}
        self._maybe_fail(ev)
        self.out_count += 1
        self.out_log.append(ev)
        return len(data)

    # ------------------------------------ pyusb state-changing methods

    def set_configuration(self, *a, **k):
        self.blocked_calls.append("set_configuration")

    def clear_halt(self, *a, **k):
        self.blocked_calls.append("clear_halt")

    def reset(self, *a, **k):
        self.blocked_calls.append("reset")

    def is_kernel_driver_active(self, intf):
        return False

    def detach_kernel_driver(self, intf):
        self.blocked_calls.append("detach_kernel_driver")


# ================================================================ helpers


class _FastClock:
    """time.monotonic() that jumps 1000 s per call: every captured poll
    that does not match the fake register model "times out" on its
    first mismatch (device.py logs and continues, as on hardware)."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1000.0
        return self.t


@contextmanager
def fast_time():
    real_sleep, real_mono = time.sleep, time.monotonic
    time.sleep = lambda s: None
    time.monotonic = _FastClock()
    try:
        yield
    finally:
        time.sleep, time.monotonic = real_sleep, real_mono


@contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def _cal_buffers():
    from test_calibrate import _build_cal_buffers
    return _build_cal_buffers()


def _find_table(out_log, pairs):
    """Index of the first ctrl-OUT event where `pairs` (a full register
    table, sent as consecutive 64-byte 0x83 batches) begins, or None."""
    full = bytes(b for pr in pairs for b in pr)
    chunks = [full[i:i + 64] for i in range(0, len(full), 64)]
    for i in range(len(out_log) - len(chunks) + 1):
        window = out_log[i:i + len(chunks)]
        if all(ev["kind"] == "ctrl_out" and ev["wv"] == 0x83 and ev["data"] == c
               for ev, c in zip(window, chunks)):
            return i
    return None


def make_scanner(fake: FakeUsbDevice) -> Scanner:
    return Scanner(UsbIo(fake))


def expect(exc_type, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc_type as e:
        return e
    raise AssertionError(f"expected {exc_type.__name__} from {getattr(fn, '__name__', fn)}")


def _assert_power_cycle_message(text: str) -> None:
    assert "Power the scanner OFF" in text, text
    assert "Restarting this program is NOT sufficient" in text, text


_STDOUT = _io.StringIO()
_STDERR = _io.StringIO()


@contextmanager
def quiet():
    _STDOUT.seek(0); _STDOUT.truncate()
    _STDERR.seek(0); _STDERR.truncate()
    with redirect_stdout(_STDOUT), redirect_stderr(_STDERR):
        yield


# ======================================================= classification


def test_classify_reg01():
    assert safety.classify_reg01(0x22) is StartState.IDLE
    assert safety.classify_reg01(0x00) is StartState.COLD
    for v in (0x02, 0x03, 0x23, 0x20, 0x21, 0x42, 0xFF, None, -1):
        assert safety.classify_reg01(v) is StartState.UNSAFE, v
    assert safety.has_execute_pulse(bytes([0x09, 0x08, 0x0F, 0x01]))
    assert not safety.has_execute_pulse(bytes([0x0F, 0x00, 0x01, 0x0F]))
    print("test_classify_reg01 OK")


# ============================================ start-state matrix, driver


UNSAFE_START_CASES = [
    ("0x02", 0x02),
    ("0x23", 0x23),
    ("unknown 0x7f", 0x7F),
    ("unknown 0x20", 0x20),
    ("read timeout", _usb_timeout()),
    ("usb error", _usb_error()),
    ("malformed: 1 byte", b"\x22"),
    ("malformed: bad ack", b"\x22\x00"),
    ("malformed: empty", b""),
    ("malformed: 3 bytes", b"\x22\x55\x00"),
]


def _driver_entry_points():
    """Every public Scanner method that can write, as (name, fn)."""
    return [
        ("initialize", lambda s: s.initialize()),
        ("initialize(ir,2400)", lambda s: s.initialize(ir=True, dpi=2400)),
        ("scan", lambda s: s.scan(frame=1)),
        ("scan(ir)", lambda s: s.scan(frame=1, ir=True)),
        ("eject", lambda s: s.eject()),
        ("home", lambda s: s.home()),
        ("cold_init", lambda s: s.cold_init()),
        ("park_semantic", lambda s: s.park_semantic()),
        ("load_magazine", lambda s: s.load_magazine()),
        ("_exec_ops(PREP)", lambda s: s._exec_ops(tables.PREP.ops)),
        ("io.write_regs", lambda s: s.io.write_regs([(0x01, 0x22)])),
        ("io.buf_write", lambda s: s.io.buf_write(0x1000C000, b"\x00" * 16)),
        ("io.end_access", lambda s: s.io.end_access()),
        ("dev.ctrl_transfer OUT", lambda s: s.io.dev.ctrl_transfer(0x40, 0x04, 0x83, 0, b"\x01\x22")),
        ("dev.write bulk OUT", lambda s: s.io.dev.write(0x02, b"\x00" * 8)),
    ]


def test_unsafe_start_states_refuse_every_entry_point_with_zero_writes():
    n = 0
    for label, reg01 in UNSAFE_START_CASES:
        for name, fn in _driver_entry_points():
            fake = FakeUsbDevice(reg01=reg01, cal_buffers=_cal_buffers())
            scanner = make_scanner(fake)
            with fast_time():
                e = expect(safety.SafetyError, fn, scanner)
            assert fake.out_count == 0, (label, name, fake.out_count)
            assert fake.pulses == 0
            assert fake.blocked_calls == [], (label, name, fake.blocked_calls)
            assert scanner.session.state in (SessionState.REFUSED, SessionState.UNVERIFIED), \
                (label, name, scanner.session.state)
            msg = str(e)
            assert any(k in msg.lower() for k in ("no commands were sent", "nothing was written", "nothing was sent")), (label, name, msg)
            if isinstance(e, UnsafeStartStateError) and scanner.session.state is SessionState.REFUSED:
                _assert_power_cycle_message(msg)
                if isinstance(reg01, int):
                    assert e.observed == reg01, (label, name, e.observed)
                    assert f"{reg01:#04x}" in msg, msg
                else:
                    assert e.observed is None, (label, name, e.observed)
            # A second attempt in the same session: still zero writes,
            # still refused (the refusal is final for the process).
            with fast_time():
                expect(safety.SafetyError, fn, scanner)
            assert fake.out_count == 0, (label, name)
            # Closing the session sends nothing either.
            scanner.close()
            assert fake.out_count == 0
            n += 1
    print(f"test_unsafe_start_states_refuse_every_entry_point_with_zero_writes OK ({n} cases)")


def test_idle_state_permits_normal_operations():
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    with fast_time():
        assert scanner.check_start_state() is StartState.IDLE
        assert fake.out_count == 0, "check_start_state must not write"
        assert scanner.session.state is SessionState.ARMED
        scanner.initialize()
        n_init = fake.out_count
        assert n_init > 0
        raw, width = scanner.scan(frame=1)
    assert width == tables.IMAGE_WIDTH and len(raw) > 0
    assert fake.pulses >= 8, fake.pulses          # one per calibration/position/scan phase
    assert scanner.session.state is SessionState.ARMED
    assert scanner.session.failure is None
    snap = scanner.last_diag["session"]
    assert snap["writes"] == fake.out_count, (snap["writes"], fake.out_count)
    assert snap["execute_pulses"] == fake.pulses, (snap["execute_pulses"], fake.pulses)
    assert snap["start_reg01"] == "0x22" and snap["state"] == "armed"
    # After PARK the model reads 0x22 again: a NEW session may start.
    assert fake.regs[0x01] == 0x22
    with fast_time():
        s2 = make_scanner(fake)
        assert s2.check_start_state() is StartState.IDLE
    print(f"test_idle_state_permits_normal_operations OK ({fake.out_count} writes, {fake.pulses} pulses)")


def test_scan_requires_initialize_before_every_frame():
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    with fast_time():
        expect(OperationNotAllowedError, scanner.scan, frame=1)
        assert fake.out_count == 0
        scanner.initialize()
        scanner.scan(frame=1)
        n = fake.out_count
        expect(OperationNotAllowedError, scanner.scan, frame=1)   # no second initialize()
        assert fake.out_count == n
        assert scanner.session.state is SessionState.ARMED     # a refusal, not a failure
    print("test_scan_requires_initialize_before_every_frame OK")


def test_batch_is_one_session_and_transient_states_are_tolerated():
    """Inside a session the model's reg 0x01 goes 0x23/0x02/0x03 between
    phases; the guard reads it only once, so a 4-frame batch runs."""
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    seen = set()
    real_ctrl = fake.ctrl_transfer

    def spy(bm, br, wv=0, wi=0, data=None, timeout=None):
        seen.add(fake.regs[0x01])
        return real_ctrl(bm, br, wv, wi, data, timeout)
    fake.ctrl_transfer = spy
    scanner = make_scanner(fake)
    with fast_time():
        for frame in range(1, 5):
            scanner.initialize()
            scanner.scan(frame=frame)
        scanner.eject()
    assert 0x23 in seen and 0x22 in seen, seen
    assert scanner.session.state is SessionState.ARMED
    assert scanner.session.start_reg01 == 0x22
    print("test_batch_is_one_session_and_transient_states_are_tolerated OK")


# ================================================================ cold


def test_cold_state_permits_only_the_cold_init_path():
    # Every non-cold entry point is refused with zero writes.
    for name, fn in _driver_entry_points():
        if name.startswith(("initialize", "eject", "cold_init")):
            continue
        fake = FakeUsbDevice(reg01=0x00, cal_buffers=_cal_buffers())
        scanner = make_scanner(fake)
        with fast_time():
            e = expect(safety.SafetyError, fn, scanner)
        assert fake.out_count == 0, (name, fake.out_count)
        assert any(k in str(e).lower() for k in ("no commands were sent", "nothing was written", "nothing was sent")), (name, str(e))

    # initialize() on a cold scanner runs cold_init first, then the base
    # table; the model's COLD_INIT table writes 0x01=0x22, which is what
    # the post-cold-init verification reads.
    fake = FakeUsbDevice(reg01=0x00, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    with fast_time():
        assert scanner.check_start_state() is StartState.COLD
        assert scanner.session.state is SessionState.COLD
        scanner.initialize()
    assert scanner.session.cold_init_done
    assert scanner.session.state is SessionState.ARMED
    first_batch = fake.out_log[0]
    assert first_batch["kind"] == "ctrl_out" and first_batch["wv"] == 0x008B, first_batch   # chip handshake
    # The cold table came before the base table.
    cold_idx = _find_table(fake.out_log, tables_base.COLD_INIT_PAIRS)
    base_idx = _find_table(fake.out_log, tables_base.BASE_INIT_PAIRS)
    assert cold_idx is not None and base_idx is not None and cold_idx < base_idx, (cold_idx, base_idx)
    assert fake.pulses == 9, fake.pulses   # 3 rounds x 3 homing moves
    with fast_time():
        scanner.scan(frame=1)
    assert scanner.session.state is SessionState.ARMED

    # cold_init a second time in the same session: refused, zero writes.
    n = fake.out_count
    with fast_time():
        expect(OperationNotAllowedError, scanner.cold_init)
    assert fake.out_count == n

    # eject() on a cold scanner: cold_init first, then the eject move.
    fake = FakeUsbDevice(reg01=0x00)
    scanner = make_scanner(fake)
    with fast_time():
        scanner.eject()
    assert scanner.session.cold_init_done and fake.pulses == 10, fake.pulses

    # cold_init() directly on an IDLE scanner: refused, zero writes.
    fake = FakeUsbDevice(reg01=0x22)
    scanner = make_scanner(fake)
    with fast_time():
        expect(OperationNotAllowedError, scanner.cold_init)
    assert fake.out_count == 0
    print("test_cold_state_permits_only_the_cold_init_path OK")


def test_cold_init_post_verification_fails_closed():
    """If reg 0x01 does not read 0x22 after cold_init, no further
    command is sent and the session is refused."""
    fake = FakeUsbDevice(reg01=0x00)

    def keep_cold(bm, br, wv=0, wi=0, data=None, timeout=None):
        n = FakeUsbDevice.ctrl_transfer(fake, bm, br, wv, wi, data, timeout)
        if not (bm & 0x80) and wv == 0x83:
            fake.regs[0x01] = 0x02      # the table's 0x01=0x22 "does not take"
        return n
    fake.ctrl_transfer = keep_cold
    scanner = make_scanner(fake)
    with fast_time():
        e = expect(UnsafeStartStateError, scanner.initialize)
    n = fake.out_count
    assert n > 0 and e.observed == 0x02, (n, e.observed)
    assert "No further commands were sent" in str(e)
    _assert_power_cycle_message(str(e))
    assert scanner.session.state in (SessionState.REFUSED, SessionState.FAILED)
    # The base table was never written, nothing else either.
    assert _find_table(fake.out_log, tables_base.BASE_INIT_PAIRS) is None
    assert _find_table(fake.out_log, tables_base.COLD_INIT_PAIRS) is not None
    with fast_time():
        expect(SessionFailedError, scanner.initialize)
        expect(SessionFailedError, scanner.eject)
    assert fake.out_count == n
    print("test_cold_init_post_verification_fails_closed OK")


# ======================================================= fault injection


def _run_scan_with_fault(fault, reg01=0x22, frames=(1,)):
    """initialize()+scan() over a faulting fake; returns (fake, scanner,
    exception-or-None)."""
    fake = FakeUsbDevice(reg01=reg01, cal_buffers=_cal_buffers(), fault=None)
    scanner = make_scanner(fake)
    fake.fault = lambda ev: fault(ev, scanner)
    exc = None
    with fast_time():
        try:
            for frame in frames:
                scanner.initialize()
                scanner.scan(frame=frame)
        except BaseException as e:   # noqa: BLE001 -- KeyboardInterrupt included
            exc = e
    return fake, scanner, exc


def _assert_failed_and_frozen(fake, scanner, exc, *, expect_writes_gt_zero=True,
                              expect_pulses=None, phase_prefix=None, operation=None):
    assert exc is not None, "expected the injected fault to escape"
    session = scanner.session
    assert session.state is SessionState.FAILED, session.state
    f = session.failure
    assert f is not None
    assert f["writes"] == fake.out_count, (f["writes"], fake.out_count)
    assert f["execute_pulses"] == fake.pulses, (f["execute_pulses"], fake.pulses)
    if expect_writes_gt_zero:
        assert fake.out_count > 0
    if expect_pulses is not None:
        assert fake.pulses == expect_pulses, (fake.pulses, expect_pulses)
    if phase_prefix is not None:
        assert (f["phase"] or "").startswith(phase_prefix), (f["phase"], phase_prefix)
    if operation is not None:
        assert f["operation"] == operation, (f["operation"], operation)
    assert type(exc).__name__ in f["exception"], (f["exception"], exc)
    text = session.describe_failure()
    _assert_power_cycle_message(text)
    assert "No recovery commands" in text

    # NOTHING after the failure: no recovery, no new init, no park.
    n_out, n_pulses = fake.out_count, fake.pulses
    with fast_time():
        for name, fn in _driver_entry_points():
            e = expect(SessionFailedError, fn, scanner)
            assert "Power the scanner OFF" in str(e)
        scanner.close()
    assert fake.out_count == n_out, (fake.out_count, n_out)
    assert fake.pulses == n_pulses
    assert fake.blocked_calls == [], fake.blocked_calls
    # The last completed OUT transfer happened before the failure was
    # recorded -- i.e. no write was issued by any except/finally path.
    assert len(fake.out_log) == n_out


def _assert_new_session_rejected_unless_safe(fake):
    """A new independently invoked session over the left-behind state."""
    left = fake.regs[0x01]
    n = fake.out_count
    fake.fault = None
    s2 = make_scanner(fake)
    with fast_time():
        if safety.classify_reg01(left) is StartState.UNSAFE:
            expect(UnsafeStartStateError, s2.initialize)
            assert fake.out_count == n, "new session wrote to an unsafe scanner"
        else:
            s2.initialize()
            assert fake.out_count > n
    # "Power cycle": the scanner comes back cold -> the cold path is allowed.
    fake.regs[0x01] = 0x00
    fake.fault = None
    s3 = make_scanner(fake)
    with fast_time():
        assert s3.check_start_state() is StartState.COLD
        s3.initialize()
    assert s3.session.state is SessionState.ARMED


def test_fault_before_first_write():
    fake = FakeUsbDevice(reg01=_usb_timeout(), cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    with fast_time():
        e = expect(UnsafeStartStateError, scanner.initialize)
    assert fake.out_count == 0 and e.observed is None
    assert scanner.session.refusal["writes_before"] == 0
    assert "No commands were sent" in scanner.session.describe_failure()
    print("test_fault_before_first_write OK")


def test_fault_after_first_register_write():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: _usb_error() if ev["kind"] == "ctrl_out" and ev["out_index"] == 2 else None)
    assert fake.out_count == 1, fake.out_count
    _assert_failed_and_frozen(fake, scanner, exc, expect_pulses=0, operation="initialize",
                              phase_prefix="base_init")
    assert scanner.session.writes_attempted == 2
    _assert_new_session_rejected_unless_safe(fake)
    print("test_fault_after_first_register_write OK")


def test_fault_immediately_before_execute_pulse():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: _usb_timeout() if ev.get("pulse") else None)
    _assert_failed_and_frozen(fake, scanner, exc, expect_pulses=0, operation="scan",
                              phase_prefix="cal_dark_a")
    assert scanner.session.execute_pulses_attempted == 1
    assert scanner.session.execute_pulses == 0
    _assert_new_session_rejected_unless_safe(fake)
    print("test_fault_immediately_before_execute_pulse OK")


def test_fault_immediately_after_execute_pulse():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: _usb_timeout() if ev["pulses_so_far"] >= 1 else None)
    _assert_failed_and_frozen(fake, scanner, exc, expect_pulses=1, operation="scan",
                              phase_prefix="cal_dark_a")
    assert isinstance(exc, usb.core.USBTimeoutError)
    _assert_new_session_rejected_unless_safe(fake)
    print("test_fault_immediately_after_execute_pulse OK")


def test_fault_during_calibration_bulk_in():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: _usb_timeout() if ev["kind"] == "bulk_in" and (s.session.phase or "").startswith("cal_white") else None)
    _assert_failed_and_frozen(fake, scanner, exc, operation="scan", phase_prefix="cal_white")
    assert fake.regs[0x01] in (0x02, 0x03), hex(fake.regs[0x01])   # what the real scanner showed after the 11c abort
    _assert_new_session_rejected_unless_safe(fake)
    print("test_fault_during_calibration_bulk_in OK")


def test_fault_during_image_bulk_in():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: _usb_timeout() if ev["kind"] == "bulk_in" and s.session.phase == "scan" and ev["bulk_index"] % 50 == 0 else None)
    _assert_failed_and_frozen(fake, scanner, exc, operation="scan", phase_prefix="scan")
    assert fake.regs[0x01] == 0x23, hex(fake.regs[0x01])   # SCAN batch wrote 0x01=0x23 -- the 2026-09-04 state
    # PARK was NOT run: no 0x01=0x22 write after the scan batch.
    assert fake.out_log[-1]["wv"] != 0x8D
    _assert_new_session_rejected_unless_safe(fake)
    print("test_fault_during_image_bulk_in OK")


def test_fault_during_park_verbatim_and_semantic():
    for mode in ("verbatim", "semantic"):
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        scanner = make_scanner(fake)
        scanner.park_mode = mode
        fake.fault = (lambda ev, s=scanner: _usb_error()
                      if ev["kind"] == "ctrl_out" and s.session.phase == "park" and ev["out_index"] > 0 and ev["wv"] == 0x83 else None)
        exc = None
        with fast_time():
            try:
                scanner.initialize()
                scanner.scan(frame=1)
            except BaseException as e:   # noqa: BLE001
                exc = e
        _assert_failed_and_frozen(fake, scanner, exc, phase_prefix="park")
        assert scanner.session.failure["operations"][-1] in ("scan", "park"), scanner.session.failure
        _assert_new_session_rejected_unless_safe(fake)
    print("test_fault_during_park_verbatim_and_semantic OK")


def test_fault_between_frames_in_batch():
    holder = {"frames": 0}
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)

    def fault(ev):
        if ev["kind"] == "ctrl_out" and holder["frames"] >= 1 and scanner.session.operations \
                and scanner.session.operations[-1] == "initialize":
            return _usb_timeout()
        return None
    fake.fault = fault
    exc = None
    with fast_time():
        try:
            for frame in (1, 2, 3):
                scanner.initialize()
                scanner.scan(frame=frame)
                holder["frames"] += 1
        except BaseException as e:   # noqa: BLE001
            exc = e
    assert holder["frames"] == 1
    _assert_failed_and_frozen(fake, scanner, exc, operation="initialize", phase_prefix="prep")
    _assert_new_session_rejected_unless_safe(fake)
    print("test_fault_between_frames_in_batch OK")


def test_fault_during_eject():
    fake = FakeUsbDevice(reg01=0x22)
    scanner = make_scanner(fake)
    fake.fault = lambda ev: _usb_error() if ev.get("pulse") and scanner.session.phase == "eject" else None
    exc = None
    with fast_time():
        try:
            scanner.eject()
        except BaseException as e:   # noqa: BLE001
            exc = e
    _assert_failed_and_frozen(fake, scanner, exc, expect_pulses=0, operation="eject", phase_prefix="eject")
    print("test_fault_during_eject OK")


def test_fault_during_magazine_load():
    fake = FakeUsbDevice(reg01=0x22)
    scanner = make_scanner(fake)
    fake.fault = lambda ev: _usb_timeout() if ev["kind"] == "bulk_in" and scanner.session.phase == "load" else None
    exc = None
    with fast_time():
        try:
            scanner.initialize()
            scanner.load_magazine()
        except BaseException as e:   # noqa: BLE001
            exc = e
    if exc is None:
        # LOAD has no bulk IN; fail on its second execute pulse instead.
        fake = FakeUsbDevice(reg01=0x22)
        scanner = make_scanner(fake)
        fake.fault = lambda ev: _usb_timeout() if ev.get("pulse") and scanner.session.phase == "load" and ev["pulses_so_far"] >= 1 else None
        with fast_time():
            try:
                scanner.initialize()
                scanner.load_magazine()
            except BaseException as e:   # noqa: BLE001
                exc = e
    _assert_failed_and_frozen(fake, scanner, exc, operation="load_magazine", phase_prefix="load")
    print("test_fault_during_magazine_load OK")


def test_keyboard_interrupt_marks_session_failed_without_recovery():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: KeyboardInterrupt() if ev["kind"] == "bulk_in" and (s.session.phase or "").startswith("cal_shading_measure") else None)
    assert isinstance(exc, KeyboardInterrupt)
    _assert_failed_and_frozen(fake, scanner, exc, operation="scan", phase_prefix="cal_shading_measure")
    assert "KeyboardInterrupt" in scanner.session.failure["exception"]
    _assert_new_session_rejected_unless_safe(fake)
    print("test_keyboard_interrupt_marks_session_failed_without_recovery OK")


def test_load_magazine_requires_initialize_and_matches_trace():
    fake = FakeUsbDevice(reg01=0x22)
    scanner = make_scanner(fake)
    with fast_time():
        expect(OperationNotAllowedError, scanner.load_magazine)
        assert fake.out_count == 0
        scanner.initialize()
        n = fake.out_count
        scanner.load_magazine()
    emitted = [ev for ev in fake.out_log[n:]]
    expected = [op for op in tables_load.LOAD.ops if op.kind in ("cw", "bo")]
    assert len(emitted) == len(expected), (len(emitted), len(expected))
    for ev, op in zip(emitted, expected):
        if op.kind == "cw":
            assert ev["kind"] == "ctrl_out" and (ev["bm"], ev["br"], ev["wv"], ev["wi"], ev["data"]) == \
                (op.bm, op.br, op.wv, op.wi, op.data)
        else:
            assert ev["kind"] == "bulk_out" and ev["length"] == len(op.data)
    assert fake.pulses == 2
    # Byte identity against the trace slice the old tool replayed.
    trace = REPO / "traces" / "20260902-vendor-eject-from-loaded.trace.json.gz"
    if trace.exists():
        import gzip
        import json
        raw = json.load(gzip.open(trace, "rt"))[tables_load.OP_RANGE[0]:tables_load.OP_RANGE[1]]
        assert len(raw) == len(tables_load.LOAD.ops)
        for o, op in zip(raw, tables_load.LOAD.ops):
            assert o["t"] == op.kind
            if op.kind in ("cw", "bo"):
                assert bytes.fromhex(o["data"]) if o.get("data") else b"" == op.data
            if op.kind == "cw":
                assert (o["bm"], o["br"], o["wv"], o["wi"]) == (op.bm, op.br, op.wv, op.wi)
        note = "trace verified"
    else:
        note = "trace absent, table self-check only"
    print(f"test_load_magazine_requires_initialize_and_matches_trace OK ({note})")


# ================================================================== CLI


@contextmanager
def cli_over(fake: FakeUsbDevice):
    """Route Scanner.open()/UsbIo.open() to the fake, and skip image
    post-processing (covered elsewhere)."""
    opened = []

    def fake_scanner_open():
        s = Scanner(UsbIo(fake))
        opened.append(s)
        return s

    def fake_io_open(readonly=False):
        io_ = UsbIo(fake, readonly=readonly)
        opened.append(io_)
        return io_

    with patched(Scanner, "open", staticmethod(fake_scanner_open)), \
         patched(UsbIo, "open", staticmethod(fake_io_open)), \
         patched(cli, "_finish_plain_scan", lambda *a, **k: None), \
         patched(cli, "_finish_dual_scan", lambda *a, **k: None), \
         fast_time():
        yield opened


def _cli(argv):
    with quiet():
        code = cli.main(argv)
    return code, _STDOUT.getvalue(), _STDERR.getvalue()


def test_cli_scan_eject_watch_refuse_unsafe_states_with_zero_writes():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")
        cases = [
            ("scan", ["scan", "--frame", "1", "-o", out, "--no-diag"]),
            ("scan batch+eject", ["scan", "--frames", "1-2", "--eject", "-o", out, "--no-diag"]),
            ("scan ir", ["scan", "--frame", "1", "--ir", "-o", out, "--no-diag"]),
            ("eject", ["eject"]),
            ("watch", ["watch"]),
        ]
        n = 0
        for label, reg01 in UNSAFE_START_CASES:
            for name, argv in cases:
                fake = FakeUsbDevice(reg01=reg01, cal_buffers=_cal_buffers())
                fake.button_events.append(0x48)
                with cli_over(fake):
                    code, so, se = _cli(argv)
                assert code == 1, (label, name, code, se)
                assert fake.out_count == 0, (label, name, fake.out_count)
                assert fake.blocked_calls == [], (label, name)
                assert "refused" in se, (label, name, se)
                _assert_power_cycle_message(se)
                n += 1
    print(f"test_cli_scan_eject_watch_refuse_unsafe_states_with_zero_writes OK ({n} cases)")


def test_cli_scan_normal_path_and_failure_reporting():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")
        # Normal path: idle scanner, one frame, with diag sidecar.
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        with cli_over(fake) as opened:
            code, so, se = _cli(["scan", "--frame", "1", "-o", out])
        assert code == 0, (code, se)
        assert fake.pulses >= 8 and opened[0].session.state is SessionState.ARMED
        sidecar = Path(diag.sidecar_path(out))
        assert sidecar.exists()
        import json
        d = json.loads(sidecar.read_text())
        assert d["session"]["writes"] == fake.out_count and d["session"]["state"] == "armed", d["session"]

        # No magazine: refused before initialize (zero writes).
        fake = FakeUsbDevice(reg01=0x22, magazine=False, cal_buffers=_cal_buffers())
        with cli_over(fake):
            code, so, se = _cli(["scan", "--frame", "1", "-o", out, "--no-diag"])
        assert code == 1 and fake.out_count == 0 and "no magazine" in se

        # Failure mid-scan: exit 1, failure record printed, no eject
        # even though --eject was requested, nothing after the failure.
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        with cli_over(fake) as opened:
            fake.fault = lambda ev: _usb_timeout() if ev["kind"] == "bulk_in" and opened and opened[-1].session.phase == "scan" else None
            code, so, se = _cli(["scan", "--frames", "1-2", "--eject", "-o", out, "--no-diag"])
        assert code == 1, (code, se)
        assert opened[0].session.state is SessionState.FAILED
        assert "failed in phase scan" in se and "No recovery commands" in se, se
        _assert_power_cycle_message(se)
        assert "ejected" not in so
        assert fake.regs[0x01] == 0x23
        assert opened[0].session.failure["writes"] == fake.out_count

        # Ctrl-C mid-scan: exit 130, same guarantees.
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        with cli_over(fake) as opened:
            fake.fault = lambda ev: KeyboardInterrupt() if ev["kind"] == "bulk_in" and opened and opened[-1].session.phase == "scan" else None
            code, so, se = _cli(["scan", "--frame", "1", "-o", out, "--no-diag"])
        assert code == 130, (code, se)
        assert opened[0].session.state is SessionState.FAILED
        assert "interrupted" in se and "KeyboardInterrupt" in se
        _assert_power_cycle_message(se)
        n = fake.out_count
        # A new process on top of it (0x23): refused with zero writes.
        fake.fault = None
        with cli_over(fake):
            code, so, se = _cli(["scan", "--frame", "1", "-o", out, "--no-diag"])
        assert code == 1 and fake.out_count == n and "0x23" in se, (code, se)
    print("test_cli_scan_normal_path_and_failure_reporting OK")


def test_cli_eject_and_watch_paths():
    # eject on an idle scanner.
    fake = FakeUsbDevice(reg01=0x22)
    with cli_over(fake):
        code, so, se = _cli(["eject"])
    assert code == 0 and fake.pulses == 1, (code, se)

    # eject fails on the wire: reported, session failed, nothing more.
    fake = FakeUsbDevice(reg01=0x22)
    fake.fault = lambda ev: _usb_error() if ev.get("pulse") else None
    with cli_over(fake) as opened:
        code, so, se = _cli(["eject"])
    assert code == 1 and opened[0].session.state is SessionState.FAILED and "eject" in se
    _assert_power_cycle_message(se)

    # watch: button -> eject -> Ctrl-C while idle -> clean exit 0.
    fake = FakeUsbDevice(reg01=0x22)
    fake.button_events.extend([0x48, KeyboardInterrupt()])
    with cli_over(fake):
        code, so, se = _cli(["watch"])
    assert code == 0 and fake.pulses == 1 and "ejected" in so, (code, so, se)

    # watch: eject fails on the button press -> the watch ends, no second attempt.
    fake = FakeUsbDevice(reg01=0x22)
    fake.button_events.extend([0x48, 0x48, 0x48])
    fake.fault = lambda ev: _usb_timeout() if ev.get("pulse") else None
    with cli_over(fake) as opened:
        code, so, se = _cli(["watch"])
    assert code == 1 and opened[0].session.state is SessionState.FAILED
    assert len(fake.button_events) == 2, "watch kept polling after a failed eject"
    _assert_power_cycle_message(se)
    print("test_cli_eject_and_watch_paths OK")


# ============================================================== doctor


def test_doctor_and_status_are_strictly_read_only():
    for reg01 in (0x22, 0x00, 0x23, 0x02):
        fake = FakeUsbDevice(reg01=reg01)
        with cli_over(fake) as opened:
            code, so, se = _cli(["doctor"])
        assert code == 0, (hex(reg01), se)
        assert fake.out_count == 0 and fake.pulses == 0, hex(reg01)
        assert fake.blocked_calls == [], fake.blocked_calls    # no set_configuration in read-only open
        assert fake.in_count > 50
        io_ = opened[0]
        assert io_.session.state is SessionState.READONLY and io_.session.writes == 0
        name = safety.classify_reg01(reg01).value
        assert name in so, (name, so)
        if safety.classify_reg01(reg01) is StartState.UNSAFE:
            assert "ADVICE" in so and "Power the scanner OFF" in so, so

        with cli_over(fake) as opened:
            code, so, se = _cli(["status"])
        assert code == 0 and fake.out_count == 0, (hex(reg01), se)
        assert opened[0].session.state is SessionState.READONLY

    # A read-only session can never be armed or written to -- even by
    # code that tries. Verified at the pyusb boundary.
    fake = FakeUsbDevice(reg01=0x22)
    io_ = UsbIo(fake, readonly=True)
    expect(ReadOnlySessionError, safety.verify_start_state, io_)
    expect(ReadOnlySessionError, io_.write_regs, [(0x01, 0x22)])
    expect(ReadOnlySessionError, io_.buf_write, 0x1000C000, b"\x00" * 8)
    expect(ReadOnlySessionError, io_.end_access)
    expect(ReadOnlySessionError, io_.dev.ctrl_transfer, 0x40, 0x0C, 0x8B, 0x26FE, b"")
    expect(ReadOnlySessionError, io_.dev.write, 0x02, b"\x00")
    scanner = Scanner(io_)
    with fast_time():
        for name, fn in _driver_entry_points():
            expect(safety.SafetyError, fn, scanner)
    assert fake.out_count == 0 and fake.blocked_calls == []

    # pyusb's own state-changing requests are blocked on the proxy.
    for meth in ("set_configuration", "clear_halt", "reset", "set_interface_altsetting",
                 "attach_kernel_driver", "detach_kernel_driver"):
        expect(safety.SafetyError, getattr, io_.dev, meth)
    assert fake.blocked_calls == []

    # collect_doctor directly over a read-only io: zero OUT, and it never
    # calls initialize()/cold_init() (there is no Scanner at all).
    report = diag.collect_doctor(io_)
    assert fake.out_count == 0
    assert report["state"]["name"] == "idle-homed"
    print("test_doctor_and_status_are_strictly_read_only OK")


def test_readonly_open_skips_set_configuration_and_writing_open_keeps_it():
    fake = FakeUsbDevice(reg01=0x22)
    with patched(usb.core, "find", lambda **k: fake), \
            patched(safety, "lock_path", lambda: str(Path(tempfile.gettempdir()) / f"of135i-test-{os.getpid()}.lock")):
        io_ = UsbIo.open(readonly=True)
        assert fake.blocked_calls == [] and io_.session.state is SessionState.READONLY
        assert io_._lock.held
        io_.close()
        assert not io_._lock.held
        io_ = UsbIo.open()
        assert fake.blocked_calls == ["set_configuration"]     # the verified open sequence
        assert io_.session.state is SessionState.UNVERIFIED
        io_.close()
        assert not io_._lock.held
        # Device absent: the lock is released again (no leak on error).
        with patched(usb.core, "find", lambda **k: None):
            expect(Exception, UsbIo.open)
        io_ = UsbIo.open()
        io_.close()
    print("test_readonly_open_skips_set_configuration_and_writing_open_keeps_it OK")


# ================================================================ lock


def test_process_lock_excludes_second_process():
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "of135i.lock")
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, sys.argv[2]); "
             "from of135i.safety import ProcessLock; l = ProcessLock(sys.argv[1]); l.acquire(); "
             "print('held', flush=True); time.sleep(60)", path, str(REPO)],
            stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "held"
            os.environ[safety.LOCK_PATH_ENV] = path
            e = expect(ScannerBusyError, safety.ProcessLock().acquire)
            assert "nothing was sent" in str(e)
            # UsbIo.open() -- writing AND read-only -- refuses before
            # touching USB at all (usb.core.find is never called).
            def boom(**k):
                raise AssertionError("usb.core.find called while the lock is held")
            with patched(usb.core, "find", boom):
                expect(ScannerBusyError, UsbIo.open)
                expect(ScannerBusyError, UsbIo.open, readonly=True)
                expect(ScannerBusyError, Scanner.open)
        finally:
            holder.kill()
            holder.wait()
            os.environ.pop(safety.LOCK_PATH_ENV, None)
        # Holder gone: the kernel released its lock; we can take it.
        lock = safety.ProcessLock(path)
        lock.acquire()
        assert lock.held
        lock.release()
    print("test_process_lock_excludes_second_process OK")


# ============================================================== hwblock


def test_hwblock_uses_central_guard_and_writes_nothing_when_unsafe():
    import hwblock

    def run(argv, fake):
        with tempfile.TemporaryDirectory() as td:
            import builtins
            with cli_over(fake), patched(builtins, "input", lambda *a, **k: ""), quiet():
                code = hwblock.main(argv + ["--out", td])
            import json
            summary = json.loads((Path(td) / "summary.json").read_text())
        return code, summary

    for reg01 in (0x23, 0x02, _usb_timeout(), b"\x22\x00"):
        fake = FakeUsbDevice(reg01=reg01)
        code, summary = run(["warm", "--repeat", "2", "--skip-dpi-change"], fake)
        assert code == 1 and fake.out_count == 0, (reg01, code, fake.out_count)
        assert "unsafe start state" in summary["status"], summary["status"]
        assert summary["session"]["state"] == "refused"
        fake = FakeUsbDevice(reg01=reg01)
        code, summary = run(["cold"], fake)
        assert code == 1 and fake.out_count == 0, (reg01, code, fake.out_count)
        assert "unsafe start state" in summary["status"], summary["status"]

    # warm on a cold scanner: policy refusal, zero writes.
    fake = FakeUsbDevice(reg01=0x00)
    code, summary = run(["warm", "--repeat", "2", "--skip-dpi-change"], fake)
    assert code == 1 and fake.out_count == 0 and "cold scanner" in summary["status"]

    # warm, failure mid-block: FAILED, session recorded, nothing after.
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    fake.fault = lambda ev: _usb_timeout() if ev["kind"] == "bulk_in" and ev["bulk_index"] == 400 else None
    code, summary = run(["warm", "--repeat", "2", "--skip-dpi-change"], fake)
    assert code == 1 and summary["status"].startswith("FAILED at step W1"), summary["status"]
    assert summary["session"]["state"] == "failed"
    assert summary["session"]["failure"]["writes"] == fake.out_count
    _assert_power_cycle_message(summary["session_text"])
    print("test_hwblock_uses_central_guard_and_writes_nothing_when_unsafe OK")


# ============================================================= raw io


def test_verify_start_state_on_raw_io_for_tools():
    """replay_trace.py uses safety.verify_start_state(io) + the session's
    operation() directly, without Scanner."""
    fake = FakeUsbDevice(reg01=0x23)
    io_ = UsbIo(fake)
    expect(UnsafeStartStateError, safety.verify_start_state, io_)
    expect(safety.SafetyError, io_.write_regs, [(0x01, 0x22)])   # refused session: final
    assert fake.out_count == 0

    fake = FakeUsbDevice(reg01=0x22)
    io_ = UsbIo(fake)
    # Armed, but a write outside any operation is still counted and
    # gated by state (no operation model at raw-io level; Scanner adds
    # that). A failing write marks the session failed immediately.
    assert safety.verify_start_state(io_) is StartState.IDLE
    fake.fault = lambda ev: _usb_error() if ev["kind"] == "ctrl_out" and ev["out_index"] == 3 else None
    with io_.session.operation("replay"):
        io_.write_regs([(0x33, 0x8E)])
        io_.write_regs([(0x32, 0x8F)])
        try:
            io_.write_regs([(0x09, 0x08)])
        except usb.core.USBError:
            pass
    assert io_.session.state is SessionState.FAILED and fake.out_count == 2
    expect(SessionFailedError, io_.write_regs, [(0x09, 0x00)])
    assert fake.out_count == 2
    print("test_verify_start_state_on_raw_io_for_tools OK")


# ================================================================ main


def main() -> int:
    tests = [
        test_classify_reg01,
        test_unsafe_start_states_refuse_every_entry_point_with_zero_writes,
        test_idle_state_permits_normal_operations,
        test_scan_requires_initialize_before_every_frame,
        test_batch_is_one_session_and_transient_states_are_tolerated,
        test_cold_state_permits_only_the_cold_init_path,
        test_cold_init_post_verification_fails_closed,
        test_fault_before_first_write,
        test_fault_after_first_register_write,
        test_fault_immediately_before_execute_pulse,
        test_fault_immediately_after_execute_pulse,
        test_fault_during_calibration_bulk_in,
        test_fault_during_image_bulk_in,
        test_fault_during_park_verbatim_and_semantic,
        test_fault_between_frames_in_batch,
        test_fault_during_eject,
        test_fault_during_magazine_load,
        test_keyboard_interrupt_marks_session_failed_without_recovery,
        test_load_magazine_requires_initialize_and_matches_trace,
        test_cli_scan_eject_watch_refuse_unsafe_states_with_zero_writes,
        test_cli_scan_normal_path_and_failure_reporting,
        test_cli_eject_and_watch_paths,
        test_doctor_and_status_are_strictly_read_only,
        test_readonly_open_skips_set_configuration_and_writing_open_keeps_it,
        test_process_lock_excludes_second_process,
        test_hwblock_uses_central_guard_and_writes_nothing_when_unsafe,
        test_verify_start_state_on_raw_io_for_tools,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
