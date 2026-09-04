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

# ------------------------------------------------------------- fake io/dev


class _FakeDev:
    """Records every OUT control transfer (bm, br, wv, wi, data) into
    the shared `events` list, tagged ("ctrl", ...), so relative
    ordering against register writes (also recorded into `events`,
    tagged ("reg", ...)) can be checked."""

    def __init__(self, events: list):
        self._events = events

    def ctrl_transfer(self, bm, br, wv, wi, data):
        b = bytes(data)
        self._events.append(("ctrl", bm, br, wv, wi, b))
        return len(b)


class _FakeIo:
    """Duck type for UsbIo, scripted for park_semantic()'s reads.

    read_reg(0x15)/(0x35) return fixed scripted values; read_reg(0x32)
    is served from `reg32_seq`, one value per call (clamped to the
    last entry once exhausted) -- park_semantic() calls it once for
    the pre-wait RMW, repeatedly during Wait B, and once more for the
    idle-loop round's write-back.
    """

    def __init__(self, reg15=0x90, reg35=0xFB, reg32_seq=(0x81, 0x81, 0x95)):
        self.events: list = []
        self.dev = _FakeDev(self.events)
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


def test_waits_time_out_without_raising():
    """0x35 never sets bit 0x40 and 0x32 never reaches the masked
    target: both waits must time out (logged, not raised) and report
    timed_out=True in the diagnostics."""
    io = _FakeIo(reg15=0x90, reg35=0x00, reg32_seq=(0x00,))
    scanner = Scanner(io)
    clock = _FakeClock()
    with _Monkeypatch(sleep=_no_sleep, monotonic=clock.monotonic):
        scanner.park_semantic(ir=False)  # must return normally, not raise

    waits = scanner._diag_park_waits
    assert waits is not None
    assert waits["a_timed_out"] is True, waits
    assert waits["b_timed_out"] is True, waits
    assert isinstance(waits["a_seconds"], float) and isinstance(waits["b_seconds"], float), waits
    print("test_waits_time_out_without_raising OK")


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
        test_waits_time_out_without_raising,
        test_park_mode_default_and_dispatch,
        test_ab_equivalence_all_tables,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
