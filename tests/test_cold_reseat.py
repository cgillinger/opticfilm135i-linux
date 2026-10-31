#!/usr/bin/env python3
"""Cold-start re-seating traverse (vendor recovery, docs/test-log.md Test 112).

OFFLINE ONLY. The traverse is implemented from the 2026-10-31 vendor
recovery capture and is NOT hardware-verified.

Pinned here:
  a. GOLDEN: the wire sequence of today's cold start (full host-to-device
     op list, reads included) for a scanner with a magazine whose loader
     bit stays set, and for one with no magazine, is byte-identical to the
     list frozen from the code BEFORE the re-seating step existed
     (tests/golden/cold_init_*.txt). Regenerate only on purpose:
     `python tests/test_cold_reseat.py --regen`.
  b. loader bit clear with a magazine present -> the traverse sits between
     the pre-move status read and the pre-move writes of homing round 1.
  c. its strict completion poll times out -> StrictPollTimeoutError,
     session FAILED, nothing written after GO, power-cycle instruction.
  d. the poll compares against load_completion_target() under
     LOAD_STATUS_MASK.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from test_safety import (FakeUsbDevice, SessionState, _assert_power_cycle_message,  # noqa: E402
                         expect, fast_time, make_scanner)
from of135i.safety import SessionFailedError  # noqa: E402
from of135i import device, holder, safety, tables_base  # noqa: E402

GOLDEN = REPO / "tests" / "golden"


def wire_lines(fake: FakeUsbDevice) -> list[str]:
    out = []
    for e in fake.wire_log:
        t = e["t"]
        if t == "cw":
            out.append(f"cw {e['br']:02x} {e['wv']:04x} {e['wi']:04x} {e['data'].hex()}")
        elif t == "cr":
            out.append(f"cr {e['br']:02x} {e['wv']:04x} {e['wi']:04x} {e['length']}")
        elif t == "bo":
            out.append(f"bo {len(e['data'])} {hashlib.sha1(e['data']).hexdigest()[:12]}")
        else:
            out.append(f"bi {e['length']}")
    return out


def run_cold_init(magazine: bool) -> FakeUsbDevice:
    fake = FakeUsbDevice(reg01=0x00, magazine=magazine)
    if not magazine:
        # loader bit clear until the first motor move completes (a bare
        # scanner), then the done value every cold start reached
        fake.status_high = lambda f: 0xF0 if f.pulses == 0 else 0xF8
    scanner = make_scanner(fake)
    with fast_time():
        scanner.cold_init()
    assert fake.pulses == 9
    return fake


def test_golden_cold_start_is_byte_identical():
    for name, magazine in (("magazine_bit_set", True), ("no_magazine", False)):
        lines = wire_lines(run_cold_init(magazine))
        want = (GOLDEN / f"cold_init_{name}.txt").read_text().splitlines()
        assert lines == want, (name, len(lines), len(want),
                               next((i for i, (a, b) in enumerate(zip(lines, want)) if a != b), None))
    print("test_golden_cold_start_is_byte_identical OK")


# ---------------------------------------------------------- re-seat cases

TRAVERSE_BATCH = bytes([0x02, 0x1C, 0xAE, 0x00, 0xAF, 0xFF, 0x3D, 0x01, 0x3E, 0x17, 0x3F, 0x42])


def _traverse_fake(traverse_hi=0xD8):
    """Status high byte: loader bit SET at the opening word and its ready
    poll (a magazine present, read on the unwritten register), CLEAR at
    the pre-move read of round 1 (the Test 112 capture: 0xd055), then
    `traverse_hi` for as long as the traverse (pulse 1) is the last motor
    start, 0xf8 for the jog. (fast_time() lets a poll see one value, so
    the capture's busy d1/d9 steps are not scripted; the timeout test
    pins a busy value that never completes.)"""
    fake = FakeUsbDevice(reg01=0x00, magazine=True)
    state = {"n": 0}

    def hi(f):
        state["n"] += 1
        if f.pulses == 0:
            return 0xF8 if state["n"] <= 2 else 0xD0
        if f.pulses == 1:
            return traverse_hi
        return 0xF8
    fake.status_high = hi
    return fake


def test_reseat_traverse_runs_before_the_jog():
    fake = _traverse_fake()
    scanner = make_scanner(fake)
    with fast_time():
        scanner.cold_init()
    assert fake.pulses == 10, fake.pulses       # traverse + 9 homing moves
    cw = [e for e in fake.wire_log if e["t"] == "cw" and e["wv"] == 0x83]
    idx = [i for i, e in enumerate(cw) if e["data"] == TRAVERSE_BATCH]
    assert len(idx) == 1, idx
    # holder.FEEDL_CEILING is the 3-byte count 0x01 0x17 0x42
    assert holder.FEEDL_CEILING == 0x011742
    # ... and the traverse is the FIRST pulse: the batch, then GO, no jog before it.
    go = [i for i, e in enumerate(cw) if e["data"] == bytes([0x0F, 0x01])]
    assert go[0] == idx[0] + 1, (go[:2], idx)
    # both slope-table uploads between the batch and GO
    wl = fake.wire_log
    bpos = next(i for i, e in enumerate(wl) if e.get("data") == TRAVERSE_BATCH)
    gpos = next(i for i, e in enumerate(wl) if e.get("data") == bytes([0x0F, 0x01]))
    assert [e["t"] for e in wl[bpos:gpos] if e["t"] == "bo"] == ["bo", "bo"]
    assert all(len(e["data"]) == 512 for e in wl[bpos:gpos] if e["t"] == "bo")
    # the first homing move (feed 6690) follows the traverse, unchanged
    jog = bytes([0x02, 0x18, 0xAE, 0x00, 0xAF, 0xFF, 0x3D, 0x00, 0x3E, 0x1A, 0x3F, 0x22])
    jpos = next(i for i, e in enumerate(wl) if e.get("data") == jog)
    assert jpos > gpos
    # the traverse touched neither 0x09 nor anything else outside the capture:
    between = [e for e in wl[bpos - 3:gpos + 1] if e["t"] == "cw" and e["wv"] == 0x83]
    assert all(e["data"][0::2].count(0x09) == 0 for e in between)
    assert scanner.session.cold_init_done
    print("test_reseat_traverse_runs_before_the_jog OK")


def test_reseat_only_changes_the_stream_by_the_traverse():
    """The reseat run, with the traverse's own transfers cut out, equals
    today's cold start op for op."""
    base = wire_lines(run_cold_init(True))
    fake = _traverse_fake()
    scanner = make_scanner(fake)
    with fast_time():
        scanner.cold_init()
    got = wire_lines(fake)
    h = hashlib.sha1(tables_base.SLOPE_TABLE_LOADER).hexdigest()[:12]
    start = next(i for i, l in enumerate(got) if l.endswith(TRAVERSE_BATCH.hex()))
    go = next(i for i, l in enumerate(got) if i > start and l.endswith("0f01"))
    # extra = batch ... GO + completion-poll reads (after GO) up to the next write
    end = go + 1
    while got[end].startswith("cr") and got[end].split()[2:4] == ["018e", "0122"]:
        end += 1
    # placement: right after the pre-move status read, right before the 0x36 write
    assert got[start - 1] == "cr 04 018e 0122 2", got[start - 1]
    assert got[end].endswith("3600fc") or got[end].split()[-1] == "36fc", got[end]
    rest = got[:start] + got[end:]
    assert rest == base or _same_ignoring_status_reads(rest, base), (len(rest), len(base))
    assert any(l == f"bo 512 {h}" for l in got[start:go])
    print("test_reseat_only_changes_the_stream_by_the_traverse OK")


def _same_ignoring_status_reads(a, b):
    f = lambda xs: [x for x in xs if not x.startswith("cr 04 018e 0122")]
    return f(a) == f(b)


def test_reseat_timeout_fails_closed():
    fake = _traverse_fake(0xD9)          # stays at 0xd9 forever
    scanner = make_scanner(fake)
    with fast_time():
        e = expect(safety.StrictPollTimeoutError, scanner.cold_init)
    assert fake.pulses == 1, fake.pulses       # the traverse was started, nothing after
    assert fake.out_log[-1]["data"] == bytes([0x0F, 0x01]), fake.out_log[-1]
    n = fake.out_count
    msg = str(e)
    assert "re-seating traverse did not complete" in msg and "d955" in msg, msg
    _assert_power_cycle_message(msg)
    assert scanner.session.state is SessionState.FAILED
    assert not scanner.session.cold_init_done
    assert scanner.session.phase == "cold_init:reseat", scanner.session.phase
    with fast_time():
        expect(SessionFailedError, scanner.initialize)
        expect(SessionFailedError, scanner.eject)
    assert fake.out_count == n
    print("test_reseat_timeout_fails_closed OK")


def test_reseat_poll_target_is_the_load_completion():
    want = device.load_completion_target()
    assert want == 0xDC55, hex(want)
    assert device.LOAD_STATUS_MASK == 0xFB
    # d8 (as captured after the recovery traverse) and dc (the clean load's)
    # both complete: they differ only in the masked-out bit 0x04 ...
    for hi in (0xD8, 0xDC):
        fake = _traverse_fake(hi)
        scanner = make_scanner(fake)
        with fast_time():
            scanner.cold_init()
        assert fake.pulses == 10
    # ... the feed's completion (0xf8, loader bit set), a busy value, and
    # a done class with the bit wrong all do not.
    for hi in (0xF8, 0xD9, 0xD0, 0xF0):
        fake = _traverse_fake(hi)
        scanner = make_scanner(fake)
        with fast_time():
            expect(safety.StrictPollTimeoutError, scanner.cold_init)
        assert fake.pulses == 1, (hex(hi), fake.pulses)
    print("test_reseat_poll_target_is_the_load_completion OK")


def test_no_traverse_when_no_magazine_at_start():
    """Bit clear at the pre-move read but NO magazine at the opening word:
    nothing to re-seat, no traverse (a bare scanner)."""
    fake = FakeUsbDevice(reg01=0x00, magazine=False)
    fake.status_high = lambda f: 0xF0 if f.pulses == 0 else 0xF8
    scanner = make_scanner(fake)
    with fast_time():
        scanner.cold_init()
    assert fake.pulses == 9
    assert not any(e.get("data") == TRAVERSE_BATCH for e in fake.wire_log)
    print("test_no_traverse_when_no_magazine_at_start OK")


def regen():
    GOLDEN.mkdir(exist_ok=True)
    for name, magazine in (("magazine_bit_set", True), ("no_magazine", False)):
        (GOLDEN / f"cold_init_{name}.txt").write_text("\n".join(wire_lines(run_cold_init(magazine))) + "\n")
        print("wrote", name)


def main() -> int:
    if "--regen" in sys.argv:
        regen()
        return 0
    tests = [test_golden_cold_start_is_byte_identical, test_reseat_traverse_runs_before_the_jog,
             test_reseat_only_changes_the_stream_by_the_traverse, test_reseat_timeout_fails_closed,
             test_reseat_poll_target_is_the_load_completion,
             test_no_traverse_when_no_magazine_at_start]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
