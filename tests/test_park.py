#!/usr/bin/env python3
"""Offline tests for Scanner.park_semantic() / Scanner._park() dispatch
-- no hardware required.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_park.py

Covers:
  - A/B equivalence: park_semantic()'s ordered register-write pairs
    match tables.PARK's/tables_ir.PARK's own captured pairs, truncated
    after the first idle-loop round (computed from the data, not a
    hardcoded op index -- see _captured_pairs_truncated()); the two
    0x8b control writes and the 0x8d write land with the captured
    bm/br/wValue/wIndex/data, in the right order relative to the
    register writes.
  - Real read-modify-write: the writes to regs 0x15/0x32/0x35 are
    derived from the FAKE's *live* scripted read value, not the
    captured constant.
  - Timeouts on both waits log-and-continue rather than raising, and
    are reported in the per-scan diagnostics.
  - Scanner._park() dispatch: "verbatim"/"semantic" plus a ValueError
    on anything else; Scanner.park_mode defaults to "verbatim".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import device, tables, tables_ir
from of135i.device import Scanner
from of135i.safety import SessionState

# ------------------------------------------------------------- fake io/dev


class _FakeDev:
    """Records every OUT control transfer (bm, br, wv, wi, data) into
    the shared `events` list, tagged ("ctrl", ...), so relative
    ordering against register writes (also recorded into `events`,
    tagged ("reg", ...)) can be checked. IN transfers of the status
    word (wValue 0x018e, wIndex 0x0122) are served from `status_seq`:
    one entry per read, the last entry repeating once exhausted; an
    entry may be bytes (any length, to model short/empty/long replies)
    or an exception instance, which is raised. Each read is recorded
    as ("status", reply-or-exception)."""

    def __init__(self, events: list, status_seq=None):
        self._events = events
        self._status_seq = list(status_seq) if status_seq is not None else [b"\xe8\x55"]
        self._status_calls = 0

    def ctrl_transfer(self, bm, br, wv, wi, data):
        if bm & 0x80:                                  # IN transfer
            if wv == 0x018E and wi == 0x0122:
                idx = min(self._status_calls, len(self._status_seq) - 1)
                self._status_calls += 1
                entry = self._status_seq[idx]
                self._events.append(("status", entry))
                if isinstance(entry, BaseException):
                    raise entry
                return bytes(entry)
            return bytes(int(data))
        b = bytes(data)
        self._events.append(("ctrl", bm, br, wv, wi, b))
        return len(b)


class _FakeIo:
    """Duck type for UsbIo, scripted for park_semantic()'s reads.

    read_reg(0x15)/(0x35) return fixed scripted values; read_reg(0x32)
    is served from `reg32_seq`, one value per call (clamped to the
    last entry once exhausted) -- park_semantic() calls it once for
    the pre-wait RMW and once more for the idle-loop round's write-
    back. Wait B reads the STATUS WORD, scripted via `status_seq`
    (see _FakeDev); the default is an immediately idle 0xe855.
    """

    def __init__(self, reg15=0x90, reg35=0xFB, reg32_seq=(0x81, 0x95), status_seq=None):
        self.events: list = []
        self.dev = _FakeDev(self.events, status_seq)
        self._reg15 = reg15
        self._reg35 = reg35
        self._reg32_seq = list(reg32_seq)
        self._reg32_calls = 0

    def write_regs(self, pairs):
        for reg, val in pairs:
            self.events.append(("reg", reg, val))

    def read_reg(self, reg: int, strict: bool = False) -> int:
        if reg == 0x01:
            return 0x22   # start-state guard (of135i.safety): idle-homed
        if reg == 0x15:
            return self._reg15
        if reg == 0x35:
            return self._reg35
        if reg == 0x32:
            idx = min(self._reg32_calls, len(self._reg32_seq) - 1)
            self._reg32_calls += 1
            return self._reg32_seq[idx]
        return 0

    def read_ext_reg(self, reg: int) -> int:
        return 0

    def close(self) -> None:
        pass

    # -------------------------------------------------- helpers for tests

    def reg_pairs(self) -> list[tuple[int, int]]:
        return [(e[1], e[2]) for e in self.events if e[0] == "reg"]

    def ctrl_events(self) -> list[tuple]:
        return [e for e in self.events if e[0] == "ctrl"]

    def status_reads(self) -> list:
        return [e[1] for e in self.events if e[0] == "status"]

    def writes_after_last_status(self) -> list[tuple]:
        """Every write event recorded after the last status read."""
        last = max((i for i, e in enumerate(self.events) if e[0] == "status"), default=-1)
        return [e for e in self.events[last + 1:] if e[0] in ("reg", "ctrl")]


class _FakeClock:
    """time.monotonic() replacement that jumps forward by a large step
    on every call, so a 15 s wait's deadline is exceeded on the very
    next check -- deterministic and fast, no real sleeping needed."""

    def __init__(self, step: float = 1000.0):
        self.t = 0.0
        self.step = step

    def monotonic(self) -> float:
        self.t += self.step
        return self.t


class _Monkeypatch:
    """Minimal context manager: patch device.time.{sleep,monotonic} for
    the duration of a `with` block, restoring the originals after."""

    def __init__(self, sleep=None, monotonic=None):
        self._sleep = sleep
        self._monotonic = monotonic

    def __enter__(self):
        self._orig_sleep = device.time.sleep
        self._orig_monotonic = device.time.monotonic
        if self._sleep is not None:
            device.time.sleep = self._sleep
        if self._monotonic is not None:
            device.time.monotonic = self._monotonic
        return self

    def __exit__(self, *exc):
        device.time.sleep = self._orig_sleep
        device.time.monotonic = self._orig_monotonic
        return False


def _no_sleep(*_a, **_kw) -> None:
    pass


# ------------------------------------------------- captured-pairs helper


_IDLE_BLOCK = [(0x36, 0xFC), (0x3A, 0x00), (0x36, 0xFC), (0x33, 0x0E)]


def _flatten_reg_pairs(ops) -> list[tuple[int, int]]:
    """All cw wv=0x83 register-batch payloads, in order, as (reg, val)
    pairs (mirrors write_regs()'s own flattening)."""
    out = []
    for op in ops:
        if op.kind == "cw" and op.wv == 0x0083:
            data = op.data
            for i in range(0, len(data), 2):
                out.append((data[i], data[i + 1]))
    return out


def _captured_pairs_truncated(ops) -> list[tuple[int, int]]:
    """The captured PARK phase's register pairs, truncated after the
    first idle-loop round.

    Computed from the data rather than a hardcoded op index: the
    4-write block (36=fc, 3a=00, 36=fc, 33=0e) occurs once in the real
    park/teardown sequence and again at the start of each of the five
    idle-loop repetitions: this takes the SECOND occurrence (the
    first idle-loop round, not the teardown's own copy of the same
    four writes) and everything up to and including the (reg 0x32)
    write-back that immediately follows it.
    """
    flat = _flatten_reg_pairs(ops)
    n = len(_IDLE_BLOCK)
    occurrences = [i for i in range(len(flat) - n + 1) if flat[i:i + n] == _IDLE_BLOCK]
    assert len(occurrences) >= 2, (
        f"expected at least 2 occurrences of the idle-loop block, found {len(occurrences)}"
    )
    block_start = occurrences[1]
    boundary = block_start + n
    assert flat[boundary][0] == 0x32, (
        f"pair after the first idle-loop block is reg {flat[boundary][0]:#x}, want 0x32 "
        f"-- surrounding pairs: {flat[boundary - 2:boundary + 3]}"
    )
    return flat[:boundary + 1]


# --------------------------------------------------------- test 1/2: A/B


def _check_ab_equivalence(ops, ir: bool) -> None:
    io = _FakeIo()
    scanner = Scanner(io)
    with _Monkeypatch(sleep=_no_sleep):
        scanner.park_semantic(ir=ir)

    expected = _captured_pairs_truncated(ops)
    got = io.reg_pairs()
    assert got == expected, (
        f"ir={ir}: register-pair mismatch\n got: {got}\n want: {expected}"
    )

    ctrl = io.ctrl_events()
    assert len(ctrl) == 3, f"expected 3 control writes (0x8d, 0x8b x2), got {len(ctrl)}: {ctrl}"
    assert ctrl[0] == ("ctrl", 0x40, 0x0C, 0x8D, 0, b"\x00"), ctrl[0]
    assert ctrl[1] == ("ctrl", 0x40, 0x04, 0x8B, 0x0B, bytes.fromhex("0c000100")), ctrl[1]
    want_wf = bytes.fromhex("c0ff" if ir else "e0ff")
    assert ctrl[2] == ("ctrl", 0x40, 0x04, 0x8B, 0x0F, want_wf), ctrl[2]

    # Order relative to the register writes: the 0x8d write is the very
    # first event; the two 0x8b writes land after the last (0x03,0x00)
    # teardown write and before the (0x32, ...) pre-wait RMW.
    idx_0300 = io.events.index(("reg", 0x03, 0x00))
    idx_3281 = io.events.index(("reg", 0x32, 0x81))
    idx_8d = io.events.index(("ctrl", 0x40, 0x0C, 0x8D, 0, b"\x00"))
    idx_8b1 = io.events.index(("ctrl", 0x40, 0x04, 0x8B, 0x0B, bytes.fromhex("0c000100")))
    idx_8b2 = io.events.index(("ctrl", 0x40, 0x04, 0x8B, 0x0F, want_wf))
    assert idx_8d == 0, f"0x8d write must be the very first op, was at {idx_8d}"
    assert idx_0300 < idx_8b1 < idx_8b2 < idx_3281, (
        f"0x8b writes out of place: 0x03=00 @{idx_0300}, 0x8b#1 @{idx_8b1}, "
        f"0x8b#2 @{idx_8b2}, 0x32=81 @{idx_3281}"
    )


def test_ab_equivalence_plain():
    _check_ab_equivalence(tables.PARK.ops, ir=False)
    print("test_ab_equivalence_plain OK")


def test_ab_equivalence_ir():
    _check_ab_equivalence(tables_ir.PARK.ops, ir=True)
    # The IR variant carries one extra register write, 0x19=0x00,
    # right after the pre-wait 0x32 RMW.
    io = _FakeIo()
    scanner = Scanner(io)
    with _Monkeypatch(sleep=_no_sleep):
        scanner.park_semantic(ir=True)
    pairs = io.reg_pairs()
    idx_3281 = pairs.index((0x32, 0x81))
    assert pairs[idx_3281 + 1] == (0x19, 0x00), (
        f"expected (0x19, 0x00) right after the 0x32=0x81 RMW, got {pairs[idx_3281 + 1]}"
    )
    print("test_ab_equivalence_ir OK")


# ----------------------------------------------------- test 3: real RMW


def test_rmw_reads_live_values():
    """0x15/0x32/0x35 writes must be derived from the fake's scripted
    *live* read, not the tables.PARK-captured constant."""
    io = _FakeIo(reg15=0xD0, reg35=0x7B, reg32_seq=(0x9D, 0x9D, 0x9D))
    scanner = Scanner(io)
    with _Monkeypatch(sleep=_no_sleep):
        scanner.park_semantic(ir=False)

    pairs = io.reg_pairs()
    # 0x15: 0xd0 & ~0x10 = 0xc0 (captured constant would have been 0x80).
    assert (0x15, 0xC0) in pairs, pairs
    # 0x35: 0x7b & ~0x40 = 0x3b (captured constant would have been 0xbb).
    assert (0x35, 0x3B) in pairs, pairs
    # 0x32: written back exactly as read (0x9d), both the pre-wait RMW
    # and the idle-loop round's write-back.
    reg32_writes = [v for r, v in pairs if r == 0x32]
    assert reg32_writes and all(v == 0x9D for v in reg32_writes), reg32_writes
    print("test_rmw_reads_live_values OK")


# --------------------------------------------------- test 4: timeouts


def test_waits_time_out_fail_closed():
    """0x35 never sets bit 0x40: Wait A times out, raises
    StrictPollTimeoutError inside the park operation (session FAILED),
    writes nothing after the timeout (no 0x35 RMW clear, no heartbeat)
    and leaves the wait recorded in the diagnostics. Same for Wait B
    when 0x32 never reaches the masked target."""
    from of135i import safety
    from of135i.safety import SessionState
    io = _FakeIo(reg15=0x90, reg35=0x00, reg32_seq=(0x00,))
    scanner = Scanner(io)
    clock = _FakeClock()
    with _Monkeypatch(sleep=_no_sleep, monotonic=clock.monotonic):
        try:
            scanner.park_semantic(ir=False)
        except safety.StrictPollTimeoutError as e:
            assert "wait A" in str(e) and "Power the scanner OFF" in str(e)
        else:
            raise AssertionError("wait A timeout did not raise")
    pairs = io.reg_pairs()
    assert (0x35, 0x00) not in pairs and (0x35, 0xBB) not in pairs, pairs      # no RMW clear
    assert pairs[-1] in ((0x32, 0x00), (0x19, 0x00)), pairs[-1]               # last write = pre-wait
    waits = scanner._diag_park_waits
    assert waits["a_timed_out"] is True and waits["b_seconds"] is None, waits
    assert scanner.session.state is SessionState.FAILED

    io = _FakeIo(reg15=0x90, reg35=0xFB, status_seq=[b"\xa1\x55"])   # stuck busy
    scanner = Scanner(io)
    clock = _FakeClock()
    with _Monkeypatch(sleep=_no_sleep, monotonic=clock.monotonic):
        try:
            scanner.park_semantic(ir=False)
        except safety.StrictPollTimeoutError as e:
            assert "wait B" in str(e) and e.last == b"\xa1\x55"
        else:
            raise AssertionError("wait B timeout did not raise")
    pairs = io.reg_pairs()
    assert pairs[-1] == (0x35, 0xFB & ~0x40), pairs[-1]                        # RMW clear done, nothing after
    assert io.writes_after_last_status() == [], "writes after a failed wait B"
    waits = scanner._diag_park_waits
    assert waits["a_timed_out"] is False and waits["b_timed_out"] is True and waits["b_last"] == "a155", waits
    assert scanner.session.state is SessionState.FAILED
    print("test_waits_time_out_fail_closed OK")


# ------------------------------------------ Wait B: the idle-status predicate


def _run_park(status_seq, reg32_seq=(0x81, 0x81), clock=None, ir=False, mod=None):
    """Run park_semantic() over a fake with the given status script and
    return (io, scanner, exception-or-None)."""
    from of135i import safety
    io = _FakeIo(reg15=0x90, reg35=0xFB, reg32_seq=reg32_seq, status_seq=status_seq)
    scanner = Scanner(io)
    clock = clock or _FakeClock(step=0.001)     # a normal clock: waits do not time out
    exc = None
    with _Monkeypatch(sleep=_no_sleep, monotonic=clock.monotonic):
        try:
            if mod is not None:
                scanner.park_semantic(mod)
            else:
                scanner.park_semantic(ir=ir)
        except BaseException as e:            # KeyboardInterrupt included
            exc = e
    return io, scanner, exc


def _assert_park_failed_closed(io, scanner, exc, exc_type):
    """Common checks for every Wait B failure: the right exception, the
    park operation recorded, session FAILED, no write after the last
    status read, and the next writing operation refused."""
    from of135i import safety
    from of135i.safety import OperationNotAllowedError, SessionFailedError
    assert isinstance(exc, exc_type), (type(exc), exc)
    assert scanner.session.state is SessionState.FAILED, scanner.session.state
    snap = scanner.session.snapshot()
    assert snap.get("failed_in") in ("operation park", None) or "park" in str(snap), snap
    assert io.writes_after_last_status() == [], io.writes_after_last_status()
    waits = scanner._diag_park_waits
    assert waits is not None and waits["a_timed_out"] is False, waits
    # No recovery of any kind, and every later write is refused.
    n = len(io.events)
    try:
        scanner.park_semantic(ir=False)
    except (SessionFailedError, OperationNotAllowedError, safety.SafetyError):
        pass
    else:
        raise AssertionError("a writing operation ran on a FAILED session")
    assert len([e for e in io.events[n:] if e[0] in ("reg", "ctrl")]) == 0, "wrote on a FAILED session"


def test_park_idle_predicate_truth_table():
    """park_idle_status_matches(): required bits 0x80/0x40 set and
    0x02/0x01 clear; 0x20/0x10/0x08/0x04 ignored; anything else rejected."""
    m = device.park_idle_status_matches
    assert device.PARK_IDLE_REQUIRED_MASK == 0xC3 and device.PARK_IDLE_REQUIRED_VALUE == 0xC0
    for ok in ("e855", "e055", "ec55", "f855", "f055", "f455", "d855", "dc55", "c855", "cc55"):
        assert m(bytes.fromhex(ok)), ok                       # every idle value observed, and its variants
    for bad in ("a155", "a955", "a555", "8155", "d155", "d555",   # busy, all observed
                "9c55", "ad55", "bd55",                            # scanning classes
                "ea55", "e955",                                    # bit 0x02 / 0x01 set on an idle-looking value
                "4855", "4055", "0055"):                           # cold power-on values (0x80 clear)
        assert not m(bytes.fromhex(bad)), bad
    for malformed in (b"", b"\xe8", b"\xe8\x00", b"\xe8\x55\x00", b"\x55\xe8", None, "e855"):
        assert not m(malformed), malformed
    # Not the LOAD or POSITION rule: the load mask needs the sensor bit in
    # its captured state, the position mask needs class F.
    assert device.LOAD_STATUS_MASK != device.PARK_IDLE_REQUIRED_MASK
    assert device.POSITION_STATUS_MASK != device.PARK_IDLE_REQUIRED_MASK
    print("test_park_idle_predicate_truth_table OK")


def test_wait_b_observed_sequences_complete():
    """1) the observed return a1 -> a9 -> e8; 2) immediately idle;
    3) several a1 before a9 and idle; 8) session-variable bits (f8, ec,
    dc, f0) all complete, with the idle-loop round written afterwards
    and no timeout recorded."""
    cases = {
        "a1->a9->e8": [b"\xa1\x55", b"\xa9\x55", b"\xe8\x55"],
        "immediate e8": [b"\xe8\x55"],
        "a1 x5 -> a9 x2 -> e8": [b"\xa1\x55"] * 5 + [b"\xa9\x55"] * 2 + [b"\xe8\x55"],
        "capture 3600 plain d1 -> f8": [b"\xd1\x55", b"\xd1\x55", b"\xf8\x55"],
        "capture dual 81 -> e8": [b"\x81\x55", b"\x81\x55", b"\xe8\x55"],
        "hardware variant ec": [b"\xa1\x55", b"\xec\x55"],
        "loaded-idle dc": [b"\xa9\x55", b"\xdc\x55"],
        "f0": [b"\xa1\x55", b"\xf0\x55"],
    }
    for label, seq in cases.items():
        io, scanner, exc = _run_park(seq)
        assert exc is None, (label, exc)
        assert len(io.status_reads()) == len(seq), (label, io.status_reads())
        pairs = io.reg_pairs()
        assert pairs[-5:-1] == _IDLE_BLOCK and pairs[-1][0] == 0x32, (label, pairs[-6:])   # idle round after
        waits = scanner._diag_park_waits
        assert waits["b_timed_out"] is False and waits["b_last"] == seq[-1].hex(), (label, waits)
        assert scanner.session.state is SessionState.ARMED, label
    print(f"test_wait_b_observed_sequences_complete OK ({len(cases)} sequences)")


def test_wait_b_rejects_stuck_and_wrong_states():
    """4) stuck in a1; 5) stuck in a9; 6) wrong class (9c, scanning);
    7) idle-looking but busy bit set (e9); 9) a bit that is not
    documented variable (0x02: ea); 16) the total budget: each fails
    closed with StrictPollTimeoutError and nothing written after."""
    from of135i import safety
    for label, value in (("stuck a1", "a155"), ("stuck a9", "a955"), ("wrong class 9c", "9c55"),
                         ("busy bit on idle-looking e9", "e955"), ("undocumented bit 0x02: ea", "ea55"),
                         ("cold 4855", "4855")):
        io, scanner, exc = _run_park([bytes.fromhex(value)], clock=_FakeClock())   # fast clock: budget exhausted
        _assert_park_failed_closed(io, scanner, exc, safety.StrictPollTimeoutError)
        assert exc.last == bytes.fromhex(value) and "wait B" in str(exc) and "Power the scanner OFF" in str(exc), (label, str(exc))
        assert scanner._diag_park_waits["b_timed_out"] is True and scanner._diag_park_waits["b_last"] == value, label
    # 16) total budget: with a real-ish clock stepping 1 s per call, the
    # 30 s budget ends after a bounded number of reads, never a hang.
    io, scanner, exc = _run_park([b"\xa9\x55"], clock=_FakeClock(step=1.0))
    _assert_park_failed_closed(io, scanner, exc, safety.StrictPollTimeoutError)
    n_reads = len(io.status_reads())
    assert 2 <= n_reads <= 40, n_reads
    print("test_wait_b_rejects_stuck_and_wrong_states OK")


def test_wait_b_malformed_replies_fail_closed():
    """10) short reply; 11) empty reply; 12) too long / wrong ack: each
    is never accepted -- the wait keeps polling until the budget ends,
    then fails closed; a malformed reply is never a completion."""
    from of135i import safety
    for label, reply in (("short", b"\xe8"), ("empty", b""), ("too long", b"\xe8\x55\x00"),
                         ("wrong ack", b"\xe8\x00"), ("swapped", b"\x55\xe8")):
        io, scanner, exc = _run_park([reply], clock=_FakeClock())
        _assert_park_failed_closed(io, scanner, exc, safety.StrictPollTimeoutError)
        assert exc.last == reply, (label, exc.last)
    # A malformed reply followed by a real idle one completes: malformed
    # is "not yet", not "accept".
    io, scanner, exc = _run_park([b"", b"\xe8", b"\xe8\x55"])
    assert exc is None and scanner.session.state is SessionState.ARMED
    print("test_wait_b_malformed_replies_fail_closed OK")


def test_wait_b_usb_errors_and_ctrl_c_propagate_and_fail_the_session():
    """13) USB timeout during the wait; 14) another USB error;
    15) KeyboardInterrupt: each propagates untouched out of the park
    operation, the session is FAILED, nothing is written after, and
    every later writing operation is refused."""
    import usb.core
    timeout = usb.core.USBTimeoutError("[Errno 110] Operation timed out", errno=110)
    io, scanner, exc = _run_park([b"\xa1\x55", timeout])
    _assert_park_failed_closed(io, scanner, exc, usb.core.USBTimeoutError)
    pipe = usb.core.USBError("[Errno 32] Pipe error", errno=32)
    io, scanner, exc = _run_park([b"\xa9\x55", pipe])
    _assert_park_failed_closed(io, scanner, exc, usb.core.USBError)
    io, scanner, exc = _run_park([b"\xa1\x55", KeyboardInterrupt()])
    _assert_park_failed_closed(io, scanner, exc, KeyboardInterrupt)
    print("test_wait_b_usb_errors_and_ctrl_c_propagate_and_fail_the_session OK")


def test_wait_b_for_every_table_and_wait_a_unchanged():
    """The new Wait B completes for all six table variants on the
    observed a1 -> a9 -> e8 sequence (regression: the table-specific
    0x8b payloads / 0x19 write are untouched), Wait A is still the
    bounded 0x35 bit-0x40 wait (fails closed on its own), and
    park_mode still defaults to verbatim."""
    import importlib
    from of135i import safety
    mods = [tables, tables_ir] + [importlib.import_module(f"of135i.tables_dpi{d}") for d in (600, 1200, 2400, 7200)]
    for mod in mods:
        captured_32 = next(v for r, v in _flatten_reg_pairs(mod.PARK.ops) if r == 0x32)
        io, scanner, exc = _run_park([b"\xa1\x55", b"\xa9\x55", b"\xe8\x55"], reg32_seq=(captured_32, 0x95), mod=mod)
        assert exc is None, (mod.__name__, exc)
        ctrl = io.ctrl_events()
        assert len(ctrl) == 3 and ctrl[1][3] == 0x8B and ctrl[2][3] == 0x8B, (mod.__name__, ctrl)
        assert scanner._diag_park_waits["b_last"] == "e855"
    # Wait A unchanged: 0x35 never sets bit 0x40 -> fails before any status read.
    io = _FakeIo(reg15=0x90, reg35=0x00, status_seq=[b"\xe8\x55"])
    scanner = Scanner(io)
    with _Monkeypatch(sleep=_no_sleep, monotonic=_FakeClock().monotonic):
        try:
            scanner.park_semantic(ir=False)
        except safety.StrictPollTimeoutError as e:
            assert "wait A" in str(e)
        else:
            raise AssertionError("wait A did not fail closed")
    assert io.status_reads() == [] and scanner.session.state is SessionState.FAILED
    assert Scanner(_FakeIo()).park_mode == "verbatim"
    print("test_wait_b_for_every_table_and_wait_a_unchanged OK (6 tables)")


# --------------------------------------------------------- test 5: dispatch


def test_park_mode_default_and_dispatch():
    io = _FakeIo()
    scanner = Scanner(io)
    assert scanner.park_mode == "verbatim", scanner.park_mode

    scanner.park_mode = "bogus"
    try:
        scanner._park(tables, ir=False)
    except ValueError:
        pass
    else:
        raise AssertionError("_park() with an unknown park_mode should raise ValueError")
    print("test_park_mode_default_and_dispatch OK")


# --------------------------------------- test 6: every table module's PARK


def test_ab_equivalence_all_tables():
    """park_semantic(t) must reproduce each table module's own PARK
    constants: the two 0x8b payloads and the 0x19 write are table-
    specific (e.g. 600 dpi: 22000100 / f8ff; 7200 dpi writes 0x32
    back as 0x01 because that capture read 0x01). The fake's 0x32
    read is scripted from the captured write-back so the RMW result
    matches the capture; 0x15/0x35 are scripted to the shared values."""
    import importlib
    mods = [tables, tables_ir] + [
        importlib.import_module(f"of135i.tables_dpi{d}") for d in (600, 1200, 2400, 7200)
    ]
    for mod in mods:
        ops = mod.PARK.ops
        flat = _flatten_reg_pairs(ops)
        captured_32 = next(v for r, v in flat if r == 0x32)
        io = _FakeIo(reg32_seq=(captured_32, 0x95, 0x95))
        scanner = Scanner(io)
        with _Monkeypatch(sleep=_no_sleep):
            scanner.park_semantic(mod)
        expected = _captured_pairs_truncated(ops)
        got = io.reg_pairs()
        # The final idle-round write-back reads 0x95 in the fake but the
        # capture's own value in the table; compare that pair by register.
        assert got[:-1] == expected[:-1], (
            f"{mod.__name__}: pair mismatch\n got: {got}\n want: {expected}"
        )
        assert got[-1][0] == expected[-1][0] == 0x32
        want_8b = [(op.wi, bytes(op.data)) for op in ops if op.kind == "cw" and op.wv == 0x8B]
        got_8b = [(e[4], e[5]) for e in io.ctrl_events() if e[3] == 0x8B]
        assert got_8b == want_8b, f"{mod.__name__}: 0x8b payloads {got_8b} != {want_8b}"
        has_19 = any(r == 0x19 for r, _ in flat)
        assert ((0x19, 0x00) in got) == has_19, f"{mod.__name__}: 0x19 presence mismatch"
    print(f"test_ab_equivalence_all_tables OK ({len(mods)} tables)")


def main() -> int:
    tests = [
        test_ab_equivalence_plain,
        test_ab_equivalence_ir,
        test_rmw_reads_live_values,
        test_waits_time_out_fail_closed,
        test_park_idle_predicate_truth_table,
        test_wait_b_observed_sequences_complete,
        test_wait_b_rejects_stuck_and_wrong_states,
        test_wait_b_malformed_replies_fail_closed,
        test_wait_b_usb_errors_and_ctrl_c_propagate_and_fail_the_session,
        test_wait_b_for_every_table_and_wait_a_unchanged,
        test_park_mode_default_and_dispatch,
        test_ab_equivalence_all_tables,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
