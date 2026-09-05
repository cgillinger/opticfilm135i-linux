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
  - the REAL UsbIo.open()/Scanner.open() path over the fake (lock,
    find, verify, configure): unsafe/unreadable/interrupted start
    states refuse with zero OUT transfers AND zero state-changing
    pyusb calls (set_configuration, kernel-driver detach, ...), the
    kernel driver left bound, lock and handle released; 0x22/0x00 read
    reg 0x01 BEFORE detach/set_configuration and keep one session; a
    configuration failure marks that session failed;
  - short OUT transfers (control before/containing/after an execute
    pulse, bulk before/after, 0 bytes reported): session FAILED,
    attempted-vs-completed bookkeeping, nothing sent afterwards, a
    driver-level restart over the left-behind state refused;
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
    performing the transfer; returning an int N (for an OUT transfer)
    performs a SHORT transfer: only the first N bytes "reach" the
    device (register side effects for the whole pairs among them),
    the transfer is logged in short_log (not out_log, not out_count)
    and N is returned -- what pyusb reports for a partial transfer.

    State-changing pyusb methods (set_configuration, clear_halt, reset,
    set_interface_altsetting, attach/detach_kernel_driver) are recorded
    in `blocked_calls`; `events` is the ordered log of everything the
    device saw (transfers and those calls) so a test can prove what
    happened BEFORE the start-state read. `touched` is the single
    "nothing changed" number: OUT transfers (complete or short) plus
    state-changing calls.

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
        self.short_count = 0
        self.out_log: list[dict] = []
        self.short_log: list[dict] = []
        self.blocked_calls: list[str] = []
        self.events: list[str] = []
        self.kernel_driver_active = False
        # High byte of the status word (reg 0x101): an int, or a callable
        # (fake) -> int. None = done-class idle values 0xF8/0xF0 by
        # magazine presence. Load tests use vendor_like_load_status()
        # (0xF0 after the feed, 0xD8 after the traverse, as captured) or
        # 0xEC/0xCC (what the real scanner answered in Test 12).
        self.status_high = None
        self.status_word_raises: BaseException | None = None
        self.set_configuration_raises: BaseException | None = None
        self.detach_raises: BaseException | None = None
        self.button_events: deque = deque()
        self._pending: list | None = None

    @property
    def touched(self) -> int:
        """OUT transfers (complete or short) + pyusb state-changing calls."""
        return self.out_count + self.short_count + len(self.blocked_calls)

    def events_before(self, marker: str) -> list[str]:
        """Events logged before the first occurrence of `marker`."""
        return self.events[:self.events.index(marker)] if marker in self.events else list(self.events)

    # ------------------------------------------------------------ faults

    def _maybe_fail(self, ev: dict):
        """Raise the injected exception, or return the injected short
        length (int) -- None means "perform the transfer normally"."""
        if self.fault is None:
            return None
        exc = self.fault(ev)
        if isinstance(exc, BaseException):
            raise exc
        return exc

    # ------------------------------------------------------- transfers

    def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        if not (bm & 0x80):
            data = bytes(data_or_wLength) if data_or_wLength is not None else b""
            pulse = (wv == 0x0083 and safety.has_execute_pulse(data))
            ev = {"kind": "ctrl_out", "bm": bm, "br": br, "wv": wv, "wi": wi, "data": data,
                  "out_index": self.out_count + 1, "pulse": pulse, "pulses_so_far": self.pulses}
            short = self._maybe_fail(ev)
            if short is not None:
                # Partial transfer: the first `short` bytes reach the device.
                self.short_count += 1
                self.short_log.append({**ev, "completed": short})
                self.events.append(f"ctrl_out short {short}/{len(data)}")
                if wv == 0x0083:
                    for i in range(0, (short // 2) * 2 - 1, 2):
                        self.regs[data[i]] = data[i + 1]
                return short
            self.out_count += 1
            self.out_log.append(ev)
            self.events.append("ctrl_out")
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
        if br == 0x04 and wv == 0x008E and (wi >> 8) == 0x01:
            self.events.append("ctrl_in reg01")
        else:
            self.events.append("ctrl_in")
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
                if self.status_word_raises is not None:
                    raise self.status_word_raises
                hi = self.status_high
                if callable(hi):
                    hi = hi(self)
                if hi is None:
                    hi = 0xF8 if self.magazine else 0xF0
                return bytes([hi, 0x55])
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
        short = self._maybe_fail(ev)
        if short is not None:
            self.short_count += 1
            self.short_log.append({**ev, "completed": short})
            self.events.append(f"bulk_out short {short}/{len(data)}")
            return short
        self.out_count += 1
        self.out_log.append(ev)
        self.events.append("bulk_out")
        return len(data)

    # ------------------------------------ pyusb state-changing methods

    def _state_call(self, name: str, raises: BaseException | None = None) -> None:
        self.blocked_calls.append(name)
        self.events.append(name)
        if raises is not None:
            raise raises

    def set_configuration(self, *a, **k):
        self._state_call("set_configuration", self.set_configuration_raises)

    def clear_halt(self, *a, **k):
        self._state_call("clear_halt")

    def reset(self, *a, **k):
        self._state_call("reset")

    def set_interface_altsetting(self, *a, **k):
        self._state_call("set_interface_altsetting")

    def attach_kernel_driver(self, intf):
        self._state_call("attach_kernel_driver")

    def is_kernel_driver_active(self, intf):
        self.events.append("is_kernel_driver_active")     # a query, not a change
        return self.kernel_driver_active

    def detach_kernel_driver(self, intf):
        self._state_call("detach_kernel_driver", self.detach_raises)
        self.kernel_driver_active = False


def vendor_like_load_status(fake: FakeUsbDevice, after_traverse: int = 0xDC,
                            after_feed: int = 0xF4, later: int | None = None,
                            jog: bool = False, jog_value: int = 0xF8) -> None:
    """Make the fake answer the LOAD flow's status-word polls like the
    clean-load capture (Test 14): `after_feed` once the first GO (feed
    6690) has completed (captured 0xf4: done class, loader-sensor bit
    0x08 CLEAR), `after_traverse` after the second (traverse 71490;
    captured 0xdc: done class, sensor bit SET again); `later`, if
    given, replaces after_traverse from the second status read on
    (models a value that settles differently after the poll). ``jog``:
    the flow starts with jog_magazine() (three GO pulses, each
    completing at ``jog_value``, captured 0xf8) before the load."""
    state = {"reads_after_traverse": 0}
    n_jog = 3 if jog else 0

    def high(f):
        if f.pulses == 0:
            return 0xE8
        if f.pulses <= n_jog:
            return jog_value
        if f.pulses == n_jog + 1:
            return after_feed
        state["reads_after_traverse"] += 1
        if later is not None and state["reads_after_traverse"] > 1:
            return later
        return after_traverse
    fake.status_high = high


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
    vendor_like_load_status(fake)
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
        vendor_like_load_status(fake)
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
    vendor_like_load_status(fake)    # a load that completes like the capture
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
    # The engaging feed (Test 14): every register batch between the
    # loader-sensor ack and the first GO, taken together, must program
    # the vendor's full block -- the values the old (eject-cut) table
    # left to the base table are exactly the ones that decided whether
    # the cassette engaged. Checked on the table itself, trace or not.
    ops = tables_load.LOAD.ops
    first_go = next(i for i, op in enumerate(ops)
                    if op.kind == "cw" and op.wv == 0x83 and op.data == bytes.fromhex("0f01"))
    feed_regs: dict[int, int] = {}
    for op in ops[:first_go]:
        if op.kind == "cw" and op.wv == 0x83:
            feed_regs.update(zip(op.data[0::2], op.data[1::2]))
    assert len(feed_regs) >= 126, len(feed_regs)
    for reg, val in ((0x02, 0x18), (0x3E, 0x1A), (0x3F, 0x22),      # feed 6690, loader mode
                     (0x01, 0x22), (0x03, 0x30), (0x15, 0x90), (0x35, 0xBB)):
        assert feed_regs.get(reg) == val, (hex(reg), feed_regs.get(reg))
    # Exactly two motor completions (feed, traverse), captured f455 / dc55.
    polls = [op for op in ops if op.kind == "poll" and op.wv == 0x018E]
    assert [op.resp.hex() for op in polls] == ["f455", "dc55"], polls
    # Byte identity against the trace slice.
    trace = REPO / "traces" / "20260905-vendor-clean-load.trace.json.gz"
    if trace.exists():
        import gzip
        import json
        full = json.load(gzip.open(trace, "rt"))
        for phase in (tables_load.OPEN, tables_load.JOG, tables_load.LOAD):
            raw = full[phase.op_range[0]:phase.op_range[1]]
            assert len(raw) == len(phase.ops), phase.name
            for o, op in zip(raw, phase.ops):
                assert o["t"] == op.kind
                if op.kind in ("cw", "bo"):
                    assert (bytes.fromhex(o["data"]) if o.get("data") else b"") == op.data
                if op.kind == "cw":
                    assert (o["bm"], o["br"], o["wv"], o["wi"]) == (op.bm, op.br, op.wv, op.wi)
        assert tables_load.OP_RANGE == tables_load.LOAD_OP_RANGE == tables_load.LOAD.op_range
        note = "trace verified"
    else:
        note = "trace absent, table self-check only"
    print(f"test_load_magazine_requires_initialize_and_matches_trace OK ({note})")


# ======================================================= short transfers


def _assert_short_failure(fake, scanner, exc, *, kind, pulse, completed_writes, completed_pulses,
                          operation=None, phase_prefix=None):
    """A short OUT transfer: session FAILED, attempted > completed, the
    short transfer on record with lengths/pulse/operation/phase, no
    later OUT reaches the device, no recovery, power-cycle advice."""
    assert isinstance(exc, safety.ShortTransferError), exc
    session = scanner.session
    assert session.state is SessionState.FAILED
    assert fake.short_count == 1, fake.short_count
    sh = fake.short_log[0]
    assert session.writes == completed_writes == fake.out_count, (session.writes, fake.out_count)
    assert session.writes_attempted == completed_writes + 1
    assert session.execute_pulses == completed_pulses == fake.pulses
    assert session.execute_pulses_attempted == completed_pulses + (1 if pulse else 0)
    assert exc.expected == (len(sh["data"]) if kind == "control" else sh["length"])
    assert exc.completed == sh["completed"] and exc.pulse is pulse
    rec = session.failure["short_transfer"]
    assert rec == {"kind": kind, "where": session.failure["where"], "expected": exc.expected,
                   "completed": exc.completed, "pulse": pulse}, rec
    assert session.failure["operation"] == (operation or session.failure["operation"])
    if operation:
        assert session.failure["operation"] == operation, session.failure
    if phase_prefix:
        assert (session.failure["phase"] or "").startswith(phase_prefix), session.failure["phase"]
    msg = str(exc)
    assert f"{exc.completed} of {exc.expected} bytes" in msg, msg
    assert "may have reached the scanner" in msg and "No further command was sent" in msg
    _assert_power_cycle_message(msg)
    text = session.describe_failure()
    assert "SHORT" in text and "may have reached the scanner" in text, text
    if pulse:
        assert "execute pulse" in msg and "execute pulse" in text
    # No OUT after the short one, no recovery, further operations refused.
    _assert_failed_and_frozen(fake, scanner, exc, operation=operation, phase_prefix=phase_prefix,
                              expect_writes_gt_zero=completed_writes > 0)
    assert fake.short_count == 1
    # Nothing OUT-bound after the short transfer, not even a short one.
    after = fake.events[fake.events.index(next(e for e in fake.events if "short" in e)) + 1:]
    assert not any(ev.startswith(("ctrl_out", "bulk_out")) for ev in after), after[:5]


def test_short_control_out_before_execute_pulse():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: 10 if ev["kind"] == "ctrl_out" and ev["out_index"] == 2 else None)
    _assert_short_failure(fake, scanner, exc, kind="control", pulse=False, completed_writes=1,
                          completed_pulses=0, operation="initialize", phase_prefix="base_init")
    _assert_new_session_rejected_unless_safe(fake)
    print("test_short_control_out_before_execute_pulse OK")


def test_short_control_out_containing_execute_pulse():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: len(ev["data"]) // 2 if ev.get("pulse") else None)
    _assert_short_failure(fake, scanner, exc, kind="control", pulse=True,
                          completed_writes=fake.out_count, completed_pulses=0, operation="scan")
    assert scanner.session.execute_pulses_attempted == 1 and scanner.session.execute_pulses == 0
    _assert_new_session_rejected_unless_safe(fake)
    print("test_short_control_out_containing_execute_pulse OK")


def test_short_control_out_after_completed_execute_pulse():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: 2 if ev["kind"] == "ctrl_out" and ev["pulses_so_far"] >= 1 and not ev["pulse"] else None)
    _assert_short_failure(fake, scanner, exc, kind="control", pulse=False,
                          completed_writes=fake.out_count, completed_pulses=1, operation="scan")
    _assert_new_session_rejected_unless_safe(fake)
    print("test_short_control_out_after_completed_execute_pulse OK")


def test_short_bulk_out_before_and_after_execute_pulse():
    # eject: bulk OUT before its execute pulse; load_magazine: bulk OUT
    # after its first execute pulse (both run outside a scan).
    cases = (
        ("before", lambda s: s.eject(), lambda ev: ev["pulses_so_far"] == 0, "eject", "eject", 0),
        ("after", lambda s: (s.initialize(), s.load_magazine()),
         lambda ev, : ev["pulses_so_far"] >= 1, "load_magazine", "load", None),
    )
    for when, run, cond, operation, phase, pulses in cases:
        fake = FakeUsbDevice(reg01=0x22)
        vendor_like_load_status(fake)
        scanner = make_scanner(fake)
        fake.fault = lambda ev, c=cond: 100 if ev["kind"] == "bulk_out" and c(ev) else None
        exc = None
        with fast_time():
            try:
                run(scanner)
            except BaseException as e:   # noqa: BLE001
                exc = e
        _assert_short_failure(fake, scanner, exc, kind="bulk", pulse=False,
                              completed_writes=fake.out_count,
                              completed_pulses=fake.pulses if pulses is None else pulses,
                              operation=operation, phase_prefix=phase)
        if when == "after":
            assert fake.pulses >= 1
    # ... and in a scan's shading upload (after several pulses), reported as 0 bytes.
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: 0 if ev["kind"] == "bulk_out" else None)
    _assert_short_failure(fake, scanner, exc, kind="bulk", pulse=False,
                          completed_writes=fake.out_count, completed_pulses=fake.pulses,
                          operation="scan")
    assert fake.pulses >= 1
    _assert_new_session_rejected_unless_safe(fake)
    print("test_short_bulk_out_before_and_after_execute_pulse OK")


def test_zero_bytes_returned_for_nonempty_payload():
    fake, scanner, exc = _run_scan_with_fault(
        lambda ev, s: 0 if ev["kind"] == "ctrl_out" and ev["out_index"] == 1 else None)
    _assert_short_failure(fake, scanner, exc, kind="control", pulse=False, completed_writes=0,
                          completed_pulses=0, operation="initialize")
    assert exc.completed == 0 and exc.expected == 64
    assert scanner.session.writes == 0 and scanner.session.writes_attempted == 1
    assert "no write had been sent" in scanner.session.describe_failure()
    print("test_zero_bytes_returned_for_nonempty_payload OK")


def test_full_length_and_legitimate_zero_length_out_succeed():
    # The cold path issues zero-length control OUTs (0x8b end-of-access
    # style requests); pyusb reports 0 for them, which is complete.
    fake = FakeUsbDevice(reg01=0x00, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    with fast_time():
        scanner.initialize()
        scanner.scan(frame=1)
    zero = [ev for ev in fake.out_log if ev["kind"] == "ctrl_out" and len(ev["data"]) == 0]
    assert zero, "expected zero-length control OUTs in the cold path"
    assert fake.short_count == 0
    assert scanner.session.state is SessionState.ARMED
    assert scanner.session.writes == scanner.session.writes_attempted == fake.out_count
    assert scanner.session.execute_pulses == scanner.session.execute_pulses_attempted == fake.pulses
    assert scanner.session.failure is None
    # A short zero-length request cannot exist; a wrong report for it is
    # still detected (the proxy compares against the actual payload).
    fake2 = FakeUsbDevice(reg01=0x22)
    io2 = UsbIo(fake2)
    io2.session.arm(0x22)
    fake2.fault = lambda ev: 1 if ev["kind"] == "ctrl_out" and len(ev["data"]) == 0 else None
    e = expect(safety.ShortTransferError, io2.end_access)
    assert e.expected == 0 and e.completed == 1 and io2.session.state is SessionState.FAILED
    print("test_full_length_and_legitimate_zero_length_out_succeed OK")


def test_short_transfer_leaving_engine_running_blocks_driver_restart():
    """The SCAN batch (0x01=0x23, engine running) completes, and the very
    next control OUT is short (0 bytes reported). The fake, like the
    real scanner, stays at 0x23, so a new driver-level session over it
    -- Scanner over the same transport, or the real open path of a new
    process -- is refused with zero writes and zero state changes; only
    a power cycle (0x00 again) lets the cold path proceed."""
    armed = {"scan_batch_sent": False}

    def fault(ev, s):
        if ev["kind"] != "ctrl_out":
            return None
        if armed["scan_batch_sent"]:
            return 0
        d = ev["data"]
        if ev["wv"] == 0x83 and any(d[i] == 0x01 and d[i + 1] == 0x23 for i in range(0, len(d) - 1, 2)):
            armed["scan_batch_sent"] = True
        return None
    fake, scanner, exc = _run_scan_with_fault(fault)
    # (the transfer after the SCAN batch is its execute pulse: a short
    # pulse is attempted, never completed)
    _assert_short_failure(fake, scanner, exc, kind="control", pulse=fake.short_log[0]["pulse"],
                          completed_writes=fake.out_count, completed_pulses=fake.pulses,
                          operation="scan")
    assert scanner.session.failure["phase"], "phase must be recorded"
    assert fake.regs[0x01] == 0x23
    n, touched = fake.out_count, fake.touched
    fake.fault = None
    s2 = make_scanner(fake)
    with fast_time():
        e = expect(UnsafeStartStateError, s2.initialize)
    assert e.observed == 0x23 and fake.out_count == n and fake.touched == touched
    # ... also through the real open path (new process).
    with real_open(fake) as path:
        e = expect(UnsafeStartStateError, Scanner.open)
        assert e.observed == 0x23 and fake.touched == touched
        _assert_lock_free(path)
    _assert_new_session_rejected_unless_safe(fake)
    print("test_short_transfer_leaving_engine_running_blocks_driver_restart OK")


def test_jog_magazine_is_the_vendor_jog_and_fails_closed():
    """Scanner.jog_magazine() replays tables_load.JOG (the vendor's
    app-start jog: feed 6690, feed 6690, eject 3090) verbatim after
    initialize(), never before it, and its four status-word polls are
    strict under the masked test: a move that does not complete
    (wrong class, sensor bit lost, busy) fails the session before the
    next move and sends nothing more."""
    ops = tables_load.JOG.ops
    gos = [i for i, op in enumerate(ops)
           if op.kind == "cw" and op.wv == 0x83 and op.data == bytes.fromhex("0f01")]
    assert len(gos) == 3
    # The three moves, in order: feed 6690, feed 6690, eject 3090 (regs 3e/3f).
    moves = []
    for g in gos:
        regs = {}
        for op in ops[:g]:
            if op.kind == "cw" and op.wv == 0x83:
                regs.update(zip(op.data[0::2], op.data[1::2]))
        moves.append((regs[0x02], regs[0x3E], regs[0x3F]))
    assert moves == [(0x18, 0x1A, 0x22), (0x18, 0x1A, 0x22), (0x18, 0x0C, 0x12)], moves
    polls = [op for op in ops if op.kind == "poll" and op.wv == 0x018E]
    assert len(polls) == 4 and all(op.resp == bytes.fromhex("f855") for op in polls)

    fake = FakeUsbDevice(reg01=0x22)
    vendor_like_load_status(fake, jog=True)
    scanner = make_scanner(fake)
    with fast_time():
        expect(OperationNotAllowedError, scanner.jog_magazine)
        assert fake.out_count == 0
        scanner.initialize(prep=False)
        n = fake.out_count
        scanner.jog_magazine()
    emitted = fake.out_log[n:]
    expected = [op for op in ops if op.kind in ("cw", "bo")]
    assert len(emitted) == len(expected), (len(emitted), len(expected))
    for ev, op in zip(emitted, expected):
        if op.kind == "cw":
            assert ev["kind"] == "ctrl_out" and (ev["bm"], ev["br"], ev["wv"], ev["wi"], ev["data"]) == \
                (op.bm, op.br, op.wv, op.wi, op.data)
        else:
            assert ev["kind"] == "bulk_out" and ev["length"] == len(op.data)
    assert fake.pulses == 3 and scanner.session.state is SessionState.ARMED
    n_full = fake.out_count - n

    for label, value, pulses in (("wrong class after a move (0xe8)", 0xE8, 1),
                                 ("sensor bit lost during the jog (0xf0)", 0xF0, 1),
                                 ("busy bit never clears (0xf9)", 0xF9, 1)):
        fake = FakeUsbDevice(reg01=0x22)
        vendor_like_load_status(fake, jog=True, jog_value=value)
        scanner = make_scanner(fake)
        with fast_time():
            scanner.initialize(prep=False); n_init = fake.out_count
            e = expect(safety.StrictPollTimeoutError, scanner.jog_magazine)
        assert fake.pulses == pulses, (label, fake.pulses)
        assert 0 < fake.out_count - n_init < n_full, label
        assert "mask 0xfb55" in str(e) and e.last.hex() == f"{value:02x}55", (label, str(e))
        _assert_failed_and_frozen(fake, scanner, e, operation="jog_magazine", phase_prefix="jog")
    print("test_jog_magazine_is_the_vendor_jog_and_fails_closed OK")


def _base_open_writes() -> int:
    """OUT transfers of initialize(prep=False) on a warm fake: the
    vendor's OPEN sequence."""
    return sum(1 for op in tables_load.OPEN.ops if op.kind in ("cw", "bo"))


def test_initialize_prep_false_is_the_vendor_device_open_state():
    """initialize(prep=False) replays tables_load.OPEN (the vendor's
    device-open sequence, loader motor profile in the table) verbatim
    and nothing else -- not BASE_INIT_PAIRS (scan profile: the jog's
    first move ran at scan speed on top of it, Test 16), no PREP. Its
    table carries the loader profile; jog/load are then permitted. A
    later initialize() writes BASE_INIT_PAIRS + PREP as for a scan."""
    ops = tables_load.OPEN.ops
    regs = {}
    for op in ops:
        if op.kind == "cw" and op.wv == 0x83:
            regs.update(zip(op.data[0::2], op.data[1::2]))
    assert (regs[0x7E], regs[0x7F], regs[0x02], regs[0x4F]) == (0x75, 0x30, 0x78, 0x63), regs
    base = dict(tables_base.BASE_INIT_PAIRS)
    assert (base[0x7E], base[0x7F]) != (0x75, 0x30)   # the scan profile: why OPEN exists

    fake = FakeUsbDevice(reg01=0x22)
    scanner = make_scanner(fake)
    with fast_time():
        expect(OperationNotAllowedError, scanner.jog_magazine)
        expect(OperationNotAllowedError, scanner.load_magazine)
        assert fake.out_count == 0
        scanner.initialize(prep=False)
    expected = [op for op in ops if op.kind in ("cw", "bo")]
    assert len(fake.out_log) == len(expected), (len(fake.out_log), len(expected))
    for ev, op in zip(fake.out_log, expected):
        assert ev["kind"] == "ctrl_out" and (ev["bm"], ev["br"], ev["wv"], ev["wi"], ev["data"]) == \
            (op.bm, op.br, op.wv, op.wi, op.data)
    assert fake.pulses == 0
    assert scanner._vendor_open and not scanner._base_initialized and not scanner._prepared_for_scan
    n_open = fake.out_count
    with fast_time():
        scanner.initialize()
    assert scanner._prepared_for_scan and scanner._base_initialized
    assert fake.out_count > n_open
    print("test_initialize_prep_false_is_the_vendor_device_open_state OK")


def test_position_wait_is_strict_and_scaled_with_feedl():
    """Test 18: the POSITION poll for frame 4 (FEEDL 39026) timed out
    after 3x the captured frame-1 move with the transport still moving
    (0xd555) and SCAN started anyway. Now the budget scales with FEEDL
    and the poll is strict on the state class: a transport that has
    not settled fails the operation before SCAN sends anything."""
    from of135i import tables
    assert device.POSITION_STATUS_MASK == 0xF0
    assert device.position_timeout_scale(tables, tables.feedl_for_frame(1)) == 1.0
    s4 = device.position_timeout_scale(tables, tables.feedl_for_frame(4))
    assert 5.5 < s4 < 6.0, s4
    poll = [op for op in tables.POSITION.ops if op.kind == "poll" and op.wv == 0x018E]
    assert len(poll) == 1 and poll[0].resp == bytes.fromhex("f455")
    assert 3 * poll[0].dur * s4 > 25, 3 * poll[0].dur * s4
    m = device.status_matches
    assert m(bytes.fromhex("f455"), poll[0].resp, 0xF0) and m(bytes.fromhex("f055"), poll[0].resp, 0xF0)
    assert not m(bytes.fromhex("d555"), poll[0].resp, 0xF0) and not m(bytes.fromhex("dd55"), poll[0].resp, 0xF0)

    feedl = tables.feedl_for_frame(4)
    patched = tables.POSITION.patched(feedl_hi=bytes([(feedl >> 16) & 0xFF]),
                                      feedl_mid=bytes([(feedl >> 8) & 0xFF]),
                                      feedl_lo=bytes([feedl & 0xFF]))
    position_feed = next(op.data for op in patched if op.kind == "cw" and op.wv == 0x83
                         and op.data[:2] == bytes([0x01, 0x22]) and b"\x3d" in op.data)
    assert bytes([0x3E, (feedl >> 8) & 0xFF, 0x3F, feedl & 0xFF]) in position_feed, position_feed.hex()

    def still_moving(f):
        if any(ev["kind"] == "ctrl_out" and ev["data"] == position_feed for ev in f.out_log):
            return 0xD5
        return 0xF8
    fake = FakeUsbDevice(reg01=0x22)
    fake.status_high = still_moving
    scanner = make_scanner(fake)
    with fast_time():
        scanner.initialize()
        e = expect(safety.StrictPollTimeoutError, scanner.scan, frame=4)
    assert "mask 0xf055" in str(e) and e.last.hex() == "d555", str(e)
    n_at_fail = fake.out_count
    scan_go = [op for op in tables.SCAN.ops if op.kind == "cw" and op.wv == 0x83
               and op.data == bytes.fromhex("0f01")]
    assert scan_go, "SCAN has a GO pulse"
    # The position feed was written (with frame 4's FEEDL), SCAN's ops were not.
    feeds = [ev for ev in fake.out_log if ev["kind"] == "ctrl_out" and ev["data"] == position_feed]
    assert len(feeds) == 1, len(feeds)
    _assert_failed_and_frozen(fake, scanner, e, operation="scan", phase_prefix="position")
    assert fake.out_count == n_at_fail
    # A settled transport (default fake: 0xf8) lets the same scan complete.
    fake = FakeUsbDevice(reg01=0x22)
    scanner = make_scanner(fake)
    with fast_time():
        scanner.initialize()
        scanner.scan(frame=4)
    assert scanner.session.state is SessionState.ARMED
    print("test_position_wait_is_strict_and_scaled_with_feedl OK")


def test_position_wait_is_strict_for_every_dpi_profile():
    """The POSITION completion rule (strict on the state class, budget
    scaled from the profile's own frame-1 FEEDL) holds for every DPI
    profile and for frame 4: a transport still moving (0xd5) fails the
    scan before SCAN's GO; a settled one (0xf8/0xf4) scans."""
    from of135i import tables as t3600
    profiles = [("3600", t3600, False)] + [(str(d), device.dual_tables(d), True) for d in (600, 1200, 2400, 7200)]
    for name, t, ir in profiles:
        polls = [op for op in t.POSITION.ops if op.kind == "poll" and op.wv == 0x018E]
        assert len(polls) == 1 and (polls[0].resp[0] & 0xF0) == 0xF0, (name, polls)
        f1, f4 = t.feedl_for_frame(1), t.feedl_for_frame(4)
        assert device.position_timeout_scale(t, f1) == 1.0
        assert abs(device.position_timeout_scale(t, f4) - f4 / f1) < 1e-9 and f4 / f1 > 5, (name, f4 / f1)
        patched = t.POSITION.patched(feedl_hi=bytes([(f4 >> 16) & 0xFF]), feedl_mid=bytes([(f4 >> 8) & 0xFF]),
                                     feedl_lo=bytes([f4 & 0xFF]))
        feed = next(op.data for op in patched if op.kind == "cw" and op.wv == 0x83
                    and op.data[:2] == bytes([0x01, 0x22]) and b"\x3d" in op.data)
        scan_go_idx = next(i for i, op in enumerate(t.SCAN.ops) if op.kind == "cw" and op.data == bytes.fromhex("0f01"))
        assert scan_go_idx >= 0

        def still_moving(f, feed=feed):
            return 0xD5 if any(ev["kind"] == "ctrl_out" and ev["data"] == feed for ev in f.out_log) else 0xF8
        fake = FakeUsbDevice(reg01=0x22)
        fake.status_high = still_moving
        scanner = make_scanner(fake)
        with fast_time():
            scanner.initialize(ir=ir, dpi=int(name))
            e = expect(safety.StrictPollTimeoutError, scanner.scan, frame=4, ir=ir, dpi=int(name))
        assert e.last.hex() == "d555" and "mask 0xf055" in str(e), (name, str(e))
        n_fail = fake.out_count
        assert sum(1 for ev in fake.out_log if ev["kind"] == "ctrl_out" and ev["data"] == feed) == 1, name
        _assert_failed_and_frozen(fake, scanner, e, operation="scan", phase_prefix="position")
        assert fake.out_count == n_fail
        fake = FakeUsbDevice(reg01=0x22)
        scanner = make_scanner(fake)
        with fast_time():
            scanner.initialize(ir=ir, dpi=int(name))
            scanner.scan(frame=4, ir=ir, dpi=int(name))
        assert scanner.session.state is SessionState.ARMED, name
    print(f"test_position_wait_is_strict_for_every_dpi_profile OK ({len(profiles)} profiles)")


def test_load_status_matches_is_class_and_sensor_bit():
    """The masked completion test (device.LOAD_STATUS_MASK = 0xfb):
    state class AND loader-sensor bit 0x08 AND busy bit, only bit 0x04
    ignored. Both vendor captures pass (f0/f4 after the feed, d8/dc
    after the traverse); every hardware failure value and every
    single-condition shortcut is rejected."""
    m = device.load_status_matches
    feed, trav = bytes.fromhex("f455"), bytes.fromhex("dc55")
    assert device.LOAD_STATUS_MASK == 0xFB
    # After the feed: done class with the sensor bit CLEAR.
    for ok in ("f455", "f055"):
        assert m(bytes.fromhex(ok), feed), ok
    for bad in ("ec55",   # Tests 12/13 on hardware: class E, sensor still set
                "f855",   # done class, but the cassette still in front of the sensor
                "f555",   # busy bit still set
                "e455",   # sensor clear but wrong class (sensor bit alone must not pass)
                "dc55",   # the traverse's value is not the feed's
                "f4",     # short reply
                "f400"):  # wrong ack byte
        assert not m(bytes.fromhex(bad), feed), bad
    # After the traverse: done class with the sensor bit SET again.
    for ok in ("dc55", "d855"):
        assert m(bytes.fromhex(ok), trav), ok
    for bad in ("cc55",   # Test 12 on hardware
                "d455",   # sensor clear: the cassette did not come back in front of it
                "dd55",   # busy bit
                "f455",   # the feed's value is not the traverse's
                "de55"):  # never-observed bit 0x02
        assert not m(bytes.fromhex(bad), trav), bad
    assert not m(b"", feed) and not m(feed, b"")
    print("test_load_status_matches_is_class_and_sensor_bit OK")


def test_load_completion_is_verified_not_assumed():
    """Test 12: the real scanner answered 0xec55 after the feed and
    0xcc55 after the traverse, and the tool reported success. Now: both
    completion polls are strict under the masked test, the flow stops
    at the first one that fails (a feed that did not engage the
    cassette never gets a traverse), the session is FAILED with
    phase/history recorded, nothing further is sent, the operator is
    told to power-cycle, and the tool exits non-zero. The final status
    read is checked the same way. Test 14: both vendor captures'
    values complete (the exact-match check rejected the correct load)."""
    import load_magazine as tool
    assert device.load_completion_target() == 0xDC55
    n_full = None

    # Reference: vendor-like loads complete -- the clean-load capture's
    # values (default), the eject-from-loaded capture's, and a final
    # read that settles on the other done value.
    for label, model in (("clean load f4/dc", vendor_like_load_status),
                         ("eject-from-loaded f0/d8",
                          lambda f: vendor_like_load_status(f, after_feed=0xF0, after_traverse=0xD8)),
                         ("final read d8 after dc", lambda f: vendor_like_load_status(f, later=0xD8))):
        fake = FakeUsbDevice(reg01=0x22)
        model(fake)
        scanner = make_scanner(fake)
        with fast_time():
            scanner.initialize(); n_init = fake.out_count
            scanner.load_magazine()
        assert fake.pulses == 2 and scanner.session.state is SessionState.ARMED, label
        assert n_full in (None, fake.out_count - n_init), label
        n_full = fake.out_count - n_init

    cases = [
        # (label, status model, expected exception, pulses sent, final status word)
        ("feed did not engage (0xec after feed, as on hardware)",
         lambda f: vendor_like_load_status(f, after_feed=0xEC), safety.StrictPollTimeoutError, 1, None),
        ("feed finished but the sensor bit is still set (0xf8): cassette not pulled past",
         lambda f: vendor_like_load_status(f, after_feed=0xF8), safety.StrictPollTimeoutError, 1, None),
        ("feed busy bit never clears (0xf5)",
         lambda f: vendor_like_load_status(f, after_feed=0xF5), safety.StrictPollTimeoutError, 1, None),
        ("traverse did not settle (0xcc after traverse, as on hardware)",
         lambda f: vendor_like_load_status(f, after_traverse=0xCC), safety.StrictPollTimeoutError, 2, None),
        ("traverse done class but sensor bit clear (0xd4)",
         lambda f: vendor_like_load_status(f, after_traverse=0xD4), safety.StrictPollTimeoutError, 2, None),
        ("polls matched but the final read is a different class (0xcc)",
         lambda f: vendor_like_load_status(f, later=0xCC), safety.LoadIncompleteError, 2, 0xCC55),
        ("polls matched but the final read lost the sensor bit (0xd4)",
         lambda f: vendor_like_load_status(f, later=0xD4), safety.LoadIncompleteError, 2, 0xD455),
    ]
    for label, model, exc_type, pulses, final_word in cases:
        fake = FakeUsbDevice(reg01=0x22)
        model(fake)
        scanner = make_scanner(fake)
        with fast_time():
            scanner.initialize(); n_init = fake.out_count
            e = expect(exc_type, scanner.load_magazine)
        assert fake.pulses == pulses, (label, fake.pulses)
        sent = fake.out_count - n_init
        assert 0 < sent < n_full if pulses < 2 or exc_type is safety.StrictPollTimeoutError else sent == n_full, (label, sent, n_full)
        msg = str(e)
        assert "No recovery commands" in msg, label
        _assert_power_cycle_message(msg)
        if exc_type is safety.StrictPollTimeoutError:
            assert "did not reach the captured value" in msg and e.want.hex() in msg and e.last.hex() in msg, (label, msg)
            assert "mask 0xfb55" in msg, msg
        else:
            assert "did NOT complete" in msg and "may not be latched" in msg and e.status_word == final_word, (label, msg)
            assert "mask 0xfb55" in msg, msg
        _assert_failed_and_frozen(fake, scanner, e, operation="load_magazine",
                                  phase_prefix="load")

    # The tool: exit 1 with FAILED text and the power-cycle instruction
    # on the hardware-observed sequence; exit 0 with an honest message
    # on a vendor-like one.
    # The tool runs the vendor's order: initialize(prep=False), the
    # JOG (3 pulses), the operator's reinsert prompt, then the LOAD.
    for label, model, want_code in (
            ("hardware", lambda f: vendor_like_load_status(f, after_feed=0xEC, jog=True), 1),
            ("vendor-like", lambda f: vendor_like_load_status(f, jog=True), 0)):
        fake = FakeUsbDevice(reg01=0x22)
        model(fake)
        asked = []

        def ask(prompt, fake=fake):
            asked.append((prompt, fake.pulses))
            return ""
        with cli_over(fake):
            with quiet():
                code = tool.main([], ask=ask)
            so, se = _STDOUT.getvalue(), _STDERR.getvalue()
        assert code == want_code, (label, code, se)
        assert asked == [(tool.REINSERT_PROMPT, 3)], (label, asked)   # asked once, after the jog
        # Nothing but the vendor's OPEN sequence precedes the jog's first write.
        first_jog = next(op for op in tables_load.JOG.ops if op.kind == "cw")
        idx = next(i for i, ev in enumerate(fake.out_log)
                   if ev["kind"] == "ctrl_out" and ev["data"] == first_jog.data)
        assert idx == _base_open_writes(), (label, idx)
        if want_code:
            assert "FAILED" in se and "did not reach" in se, se
            _assert_power_cycle_message(se)
            assert "completed" not in so, so
            assert fake.pulses == 4, fake.pulses
        else:
            assert "load sequence completed" in so and "presence, not latching" in so, so
            assert fake.pulses == 5, fake.pulses

    # Jog failure: the operator is never asked, no load is attempted.
    fake = FakeUsbDevice(reg01=0x22)
    vendor_like_load_status(fake, jog=True, jog_value=0xE8)
    asked = []
    with cli_over(fake):
        with quiet():
            code = tool.main([], ask=lambda p: asked.append(p))
        se = _STDERR.getvalue()
    assert code == 1 and not asked and fake.pulses == 1 and "FAILED" in se, (code, asked, fake.pulses)

    # Ctrl-C at the prompt: exit 130, no load, power-cycle text.
    fake = FakeUsbDevice(reg01=0x22)
    vendor_like_load_status(fake, jog=True)

    def interrupt(prompt):
        raise KeyboardInterrupt
    with cli_over(fake):
        with quiet():
            code = tool.main([], ask=interrupt)
        se = _STDERR.getvalue()
    assert code == 130 and fake.pulses == 3 and "interrupted" in se, (code, fake.pulses, se)
    _assert_power_cycle_message(se)
    print("test_load_completion_is_verified_not_assumed OK")


def test_sensor_probe_is_strictly_read_only():
    import sensor_probe
    for reg01 in (0x22, 0x00, 0x23, 0x02):
        fake = FakeUsbDevice(reg01=reg01)
        fake.kernel_driver_active = True
        fake.button_events.extend([0x04, 0x48])
        with real_open(fake) as path:
            with quiet():
                code = sensor_probe.main(["--seconds", "0.05", "--hz", "200"])
            so = _STDOUT.getvalue()
        assert code == 0, (hex(reg01), so)
        assert fake.touched == 0 and fake.events.count("ctrl_out") == 0, fake.events[:5]
        assert fake.kernel_driver_active and fake.disposed
        assert "present=True" in so and "sensor-event" in so and "eject-button" in so, so
        assert "session state readonly" in so and "writes 0" in so, so
        _assert_lock_free(path)
    print("test_sensor_probe_is_strictly_read_only OK")


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


def test_interrupt_overflow_is_named_and_never_fatal():
    """EP 0x83 answering with more than its 1-byte packet (usbfs
    EOVERFLOW, seen after every driver-run load 2026-09-05): read_button
    raises InterruptOverflowError; status prints it and exits 0, doctor
    records it, watch stops with exit 1, drain_events() returns
    "overflow" (the load tool logs it) -- and none of them writes."""
    import usb.core
    from of135i.usbio import InterruptOverflowError, Of135iError

    def overflow():
        return usb.core.USBError("[Errno 75] Overflow", errno=75)

    fake = FakeUsbDevice(reg01=0x22)
    fake.button_events.append(overflow())
    io_ = UsbIo(fake, readonly=True)
    e = expect(InterruptOverflowError, io_.read_button)
    assert "overflow" in str(e).lower() and isinstance(e, Of135iError)
    fake.button_events.append(overflow())
    assert io_.drain_events() == "overflow"
    fake.button_events.extend([0x04, 0x48])
    assert io_.drain_events() == [0x04, 0x48]
    assert fake.out_count == 0

    fake = FakeUsbDevice(reg01=0x22)
    fake.button_events.append(overflow())
    with cli_over(fake):
        with quiet():
            code = cli.main(["status"])
        so = _STDOUT.getvalue()
    assert code == 0 and "button: unreadable" in so and "overflow" in so, (code, so)
    assert fake.out_count == 0

    fake = FakeUsbDevice(reg01=0x22)
    fake.button_events.append(overflow())
    with cli_over(fake):
        report = diag.collect_doctor(UsbIo(fake, readonly=True))
    assert "overflow" in str(report["button"]).lower(), report["button"]
    assert fake.out_count == 0

    fake = FakeUsbDevice(reg01=0x22)
    fake.button_events.append(overflow())
    with cli_over(fake):
        with quiet():
            code = cli.main(["watch"])
        se = _STDERR.getvalue()
    assert code == 1 and "cannot watch" in se, (code, se)
    assert fake.out_count == 0

    # Any other USB error on the endpoint is a driver error too (nothing
    # hidden, nothing retried): status exits 1 with a message, the load
    # tool takes its normal error path (exit 1, session report) instead
    # of an unhandled pyusb exception, and drain_events() propagates it.
    from of135i.usbio import InterruptReadError
    import load_magazine as tool

    def io_error():
        return usb.core.USBError("[Errno 5] Input/Output Error", errno=5)
    fake = FakeUsbDevice(reg01=0x22)
    fake.button_events.append(io_error())
    io_ = UsbIo(fake, readonly=True)
    e = expect(InterruptReadError, io_.read_button)
    assert "Input/Output" in str(e) and isinstance(e, Of135iError)
    fake.button_events.append(io_error())
    expect(InterruptReadError, io_.drain_events)
    fake = FakeUsbDevice(reg01=0x22)
    fake.button_events.append(io_error())
    with cli_over(fake):
        with quiet():
            code = cli.main(["status"])
        se = _STDERR.getvalue()
    assert code == 1 and "error:" in se and "EP 0x83" in se, (code, se)
    fake = FakeUsbDevice(reg01=0x22)
    vendor_like_load_status(fake, jog=True)
    fake.button_events.append(io_error())          # hit by the drain after the jog
    with cli_over(fake):
        with quiet():
            code = tool.main([], ask=lambda p: "")
        se = _STDERR.getvalue()
    assert code == 1 and "error:" in se and "EP 0x83" in se and fake.pulses == 3, (code, se, fake.pulses)
    print("test_interrupt_overflow_is_named_and_never_fatal OK")


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


@contextmanager
def real_open(fake: FakeUsbDevice):
    """Route the REAL UsbIo.open()/Scanner.open() (lock, find, verify,
    configure) to the fake device and a private lock file."""
    path = str(Path(tempfile.gettempdir()) / f"of135i-test-{os.getpid()}.lock")
    with patched(usb.core, "find", lambda **k: fake), \
            patched(safety, "lock_path", lambda: path):
        yield path


def _assert_lock_free(path: str) -> None:
    lock = safety.ProcessLock(path)
    lock.acquire()          # raises ScannerBusyError if the open leaked it
    lock.release()


OPEN_UNSAFE_CASES = [
    ("0x02", 0x02),
    ("0x23", 0x23),
    ("unknown 0x77", 0x77),
    ("usb timeout", _usb_timeout()),
    ("usb error", _usb_error()),
    ("short reply", b"\x22"),
    ("malformed reply", b"\x22\x00"),
    ("interrupted read", KeyboardInterrupt()),
]

_STATE_CHANGING = ("set_configuration", "detach_kernel_driver", "clear_halt", "reset",
                   "set_interface_altsetting", "attach_kernel_driver")


def _assert_nothing_changed(fake: FakeUsbDevice, label: str) -> None:
    assert fake.out_count == 0, (label, fake.out_count)
    assert fake.short_count == 0, (label, fake.short_count)
    assert fake.pulses == 0, label
    assert fake.blocked_calls == [], (label, fake.blocked_calls)
    assert fake.touched == 0, (label, fake.touched)
    for name in _STATE_CHANGING:
        assert name not in fake.events, (label, name, fake.events)
    assert all(ev.startswith("ctrl_in") or ev == "is_kernel_driver_active" for ev in fake.events), \
        (label, fake.events)


def test_open_refuses_unsafe_states_before_any_state_change():
    """The real UsbIo.open()/Scanner.open() over a fake device: an
    unsafe or unreadable reg 0x01 refuses the session with zero OUT
    transfers AND zero pyusb state-changing calls (set_configuration,
    kernel-driver detach, ...), releases the lock and the handle, and
    says so. With a kernel driver bound the driver does NOT detach it
    to make the check possible."""
    n = 0
    for opener_name, opener in (("UsbIo.open", UsbIo.open), ("Scanner.open", Scanner.open)):
        for kernel_driver in (False, True):
            for label, reg01 in OPEN_UNSAFE_CASES:
                fake = FakeUsbDevice(reg01=reg01)
                fake.kernel_driver_active = kernel_driver
                tag = f"{opener_name}/{label}/kdrv={kernel_driver}"
                with real_open(fake) as path:
                    try:
                        opener()
                    except KeyboardInterrupt:
                        e = None
                        assert isinstance(reg01, KeyboardInterrupt), tag
                    except UnsafeStartStateError as exc:
                        e = exc
                    else:
                        raise AssertionError(f"{tag}: open() succeeded on an unsafe state")
                    _assert_nothing_changed(fake, tag)
                    assert fake.events.count("ctrl_in reg01") == 1, (tag, fake.events)
                    assert fake.disposed, tag
                    assert fake.kernel_driver_active == kernel_driver, tag   # never detached
                    _assert_lock_free(path)
                    if e is not None:
                        msg = str(e)
                        _assert_power_cycle_message(msg)
                        assert "No commands were sent" in msg, msg
                        assert e.session["state"] == "refused", e.session
                        assert e.session["writes"] == 0 and e.session["writes_attempted"] == 0
                        if isinstance(reg01, int):
                            assert e.observed == reg01 and f"{reg01:#04x}" in msg, (tag, msg)
                        else:
                            assert e.observed is None, tag
                n += 1
    print(f"test_open_refuses_unsafe_states_before_any_state_change OK ({n} cases)")


def test_open_verifies_before_configuring_and_keeps_one_session():
    """0x22: the start state is read FIRST; only then the verified open
    sequence (detach if bound, set_configuration). The session and its
    verdict survive; Scanner reuses them without a second read."""
    for kernel_driver in (False, True):
        fake = FakeUsbDevice(reg01=0x22)
        fake.kernel_driver_active = kernel_driver
        with real_open(fake) as path:
            io_ = UsbIo.open()
            expected = (["detach_kernel_driver"] if kernel_driver else []) + ["set_configuration"]
            assert fake.blocked_calls == expected, fake.blocked_calls
            # Order on the device: the reg 0x01 read precedes every state change.
            before = fake.events_before(expected[0])
            # (is_kernel_driver_active is a query, not a change; it too
            # comes after the read)
            assert before == ["ctrl_in reg01", "is_kernel_driver_active"], (before, fake.events)
            assert fake.events.index("ctrl_in reg01") < fake.events.index("set_configuration")
            assert fake.out_count == 0
            assert io_.session.state is SessionState.ARMED
            assert io_.session.start_reg01 == 0x22 and io_.session.start_state is StartState.IDLE
            assert io_.session.verified_utc is not None
            assert io_._lock.held
            session = io_.session
            scanner = Scanner(io_)
            assert scanner.session is session
            reads = fake.in_count
            assert scanner.check_start_state() is StartState.IDLE
            assert fake.in_count == reads, "verdict re-read instead of reused"
            with fast_time():
                scanner.initialize()
            assert scanner.session is session and session.state is SessionState.ARMED
            assert fake.out_count > 0
            scanner.close()
            assert not io_._lock.held and fake.disposed
            _assert_lock_free(path)

    # Scanner.open() is the same path.
    fake = FakeUsbDevice(reg01=0x22)
    with real_open(fake) as path:
        with Scanner.open() as scanner:
            assert scanner.session.state is SessionState.ARMED
            assert fake.blocked_calls == ["set_configuration"]
            assert fake.events_before("set_configuration") == ["ctrl_in reg01", "is_kernel_driver_active"]
        _assert_lock_free(path)
    # No raw handle is stored on UsbIo; the proxy blocks the standard requests.
    assert not hasattr(io_, "_raw_dev")
    print("test_open_verifies_before_configuring_and_keeps_one_session OK")


def test_open_accepts_cold_for_cold_init_only():
    fake = FakeUsbDevice(reg01=0x00)
    with real_open(fake) as path:
        io_ = UsbIo.open()
        assert fake.events_before("set_configuration") == ["ctrl_in reg01", "is_kernel_driver_active"]
        assert fake.blocked_calls == ["set_configuration"] and fake.out_count == 0
        assert io_.session.state is SessionState.COLD and io_.session.start_reg01 == 0x00
        # Nothing but the cold-init path may write.
        expect(OperationNotAllowedError, io_.write_regs, [(0x01, 0x22)])
        scanner = Scanner(io_)
        with fast_time():
            for name, fn in _driver_entry_points():
                if name.startswith(("initialize", "eject", "cold_init")):
                    continue
                expect(safety.SafetyError, fn, scanner)
        assert fake.out_count == 0 and fake.blocked_calls == ["set_configuration"]
        session = scanner.session
        with fast_time():
            scanner.initialize()          # cold_init first, then the base table
        assert scanner.session is session and session.state is SessionState.ARMED
        assert session.cold_init_done and fake.out_count > 0
        scanner.close()
        _assert_lock_free(path)
    print("test_open_accepts_cold_for_cold_init_only OK")


def test_open_configuration_failure_marks_session_failed():
    for which in ("set_configuration", "detach"):
        fake = FakeUsbDevice(reg01=0x22)
        if which == "set_configuration":
            fake.set_configuration_raises = _usb_error()
        else:
            fake.kernel_driver_active = True
            fake.detach_raises = _usb_error()
        with real_open(fake) as path:
            e = expect(SessionFailedError, UsbIo.open)
            assert e.session["state"] == "failed", e.session
            assert "configure_for_writing" in e.session["failure"]["where"]
            assert e.session["writes"] == 0
            _assert_power_cycle_message(str(e))
            assert "No recovery commands" in str(e)
            assert fake.out_count == 0 and fake.disposed
            assert fake.events[0] == "ctrl_in reg01"
            _assert_lock_free(path)
    print("test_open_configuration_failure_marks_session_failed OK")


def test_readonly_open_never_configures():
    for reg01 in (0x22, 0x00, 0x23):
        fake = FakeUsbDevice(reg01=reg01)
        fake.kernel_driver_active = True
        with real_open(fake) as path:
            io_ = UsbIo.open(readonly=True)
            assert fake.touched == 0 and fake.events == [], fake.events
            assert io_.session.state is SessionState.READONLY
            assert fake.kernel_driver_active
            io_.close()
            assert not io_._lock.held and fake.disposed
            _assert_lock_free(path)
            # Device absent: the lock is released again (no leak on error).
            with patched(usb.core, "find", lambda **k: None):
                expect(Exception, UsbIo.open)
                expect(Exception, UsbIo.open, readonly=True)
            _assert_lock_free(path)
    print("test_readonly_open_never_configures OK")


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
        test_short_control_out_before_execute_pulse,
        test_short_control_out_containing_execute_pulse,
        test_short_control_out_after_completed_execute_pulse,
        test_short_bulk_out_before_and_after_execute_pulse,
        test_zero_bytes_returned_for_nonempty_payload,
        test_full_length_and_legitimate_zero_length_out_succeed,
        test_short_transfer_leaving_engine_running_blocks_driver_restart,
        test_load_magazine_requires_initialize_and_matches_trace,
        test_jog_magazine_is_the_vendor_jog_and_fails_closed,
        test_initialize_prep_false_is_the_vendor_device_open_state,
        test_position_wait_is_strict_and_scaled_with_feedl,
        test_position_wait_is_strict_for_every_dpi_profile,
        test_load_status_matches_is_class_and_sensor_bit,
        test_load_completion_is_verified_not_assumed,
        test_sensor_probe_is_strictly_read_only,
        test_cli_scan_eject_watch_refuse_unsafe_states_with_zero_writes,
        test_cli_scan_normal_path_and_failure_reporting,
        test_cli_eject_and_watch_paths,
        test_interrupt_overflow_is_named_and_never_fatal,
        test_doctor_and_status_are_strictly_read_only,
        test_open_refuses_unsafe_states_before_any_state_change,
        test_open_verifies_before_configuring_and_keeps_one_session,
        test_open_accepts_cold_for_cold_init_only,
        test_open_configuration_failure_marks_session_failed,
        test_readonly_open_never_configures,
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
