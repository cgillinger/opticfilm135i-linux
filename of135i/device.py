"""Scan sequencing for the Plustek OpticFilm 135i (GL126).

`Scanner` drives the phase list in tables.py (generated from the
verified 3600 dpi single-frame trace, see driver/gen_tables.py and
driver-design.md's "Scan sequence" section) over a UsbIo transport,
replaying each phase's FULL verbatim op stream -- not just its
register batches -- and injecting the calibration.py values at the
documented injection points.

2026-08-30 rework: hardware A/B testing proved that executing only
the register batches + a poll subset (as this module used to) is
insufficient -- the verbatim replayer (replay_trace.py), which
executes the full op stream (all single control reads, exact op
ordering, captured dt pacing), measures correct calibration levels
where the slimmed execution saturates. Some of the skipped reads/
pacing evidently carry real state transitions on the device (lamp
settling etc.), so `_exec_ops` below reproduces replay_trace.py's
executor semantics (see its module docstring) exactly, phase by
phase, plus the hardware-discovered engine-busy wait after every
execute pulse.

Hardware safety (safety.py, docs/hardware-safety.md): every public
method that can write to the scanner -- initialize(), cold_init(),
scan(), eject(), home(), park_semantic(), load_magazine() -- runs as
a *hardware operation* (`_operation()`). The first operation of a
session verifies the start state (reg 0x01 must read 0x22, or 0x00
for the cold-init path only) before its first write; an exception
escaping any operation, KeyboardInterrupt included, marks the
session failed and no recovery command of any kind is sent
afterwards. The low-level executor `_exec_ops()` refuses to run
outside an operation, so nothing can bypass this by calling it
directly. Read-only methods (is_magazine_present, check_start_state)
never write.
"""

from __future__ import annotations

import importlib
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np

from . import calibrate, safety, tables, tables_base, tables_ir
from .safety import (
    OperationNotAllowedError, SessionState, StartState, UnsafeStartStateError,
)
from .tables import Op, Phase
from .usbio import EP_BULK_IN, EP_BULK_OUT, Of135iError, UsbIo

log = logging.getLogger("of135i")

# Resolutions with a compiled dual-light (alternating IR/visible line)
# phase set: 3600 -> tables_ir (trace 04), the others -> tables_dpi<N>
# (the 2026-09-02 QuickScan captures, see tools/gen_tables.py's
# compile_dual). 3600 is the only resolution that also has a plain
# (visible-only) phase set (tables.py).
DUAL_DPIS = (600, 1200, 2400, 3600, 7200)
SUPPORTED_DPIS = DUAL_DPIS


def dual_tables(dpi: int):
    """The dual-light table module for `dpi` (imported on first use --
    the 7200 dpi module alone is a few thousand lines)."""
    if dpi == 3600:
        return tables_ir
    if dpi not in DUAL_DPIS:
        raise Of135iError(f"no scan profile for {dpi} dpi (have {', '.join(map(str, SUPPORTED_DPIS))})")
    return importlib.import_module(f".tables_dpi{dpi}", __package__)


def _tables_for(dpi: int, ir: bool):
    """Phase-set module for a scan: the plain 3600 dpi set unless IR is
    requested or the resolution only exists as a dual-light capture."""
    if dpi == 3600 and not ir:
        return tables
    return dual_tables(dpi)

# replay_trace.py's pacing: sleep a captured inter-op gap when it
# exceeds this threshold, capped so a real outlier can't stall a run.
_PACE_THRESHOLD = 0.05
_PACE_CAP = 2.0


_has_execute_pulse = safety.has_execute_pulse


# ---------------------------------------------------- per-scan shading diag

def _parse_shading_offsets(payload: bytes) -> np.ndarray:
    """Recover the per-pixel offset field of a calibrate.shading_table()/
    shading_table2()-style payload (126 payload + 2 zero trailer
    (offset, gain) u16 LE pairs per full 512 B block, final block
    unpadded), mirroring tests/test_calibrate.py's _parse_shading_blocks.
    Returns an (n_pixels, 3) float64 array (pixel-interleaved RGB, same
    order as the image line format). Diagnostics only -- does not feed
    back into calibrate.py or the scan flow."""
    offsets: list[int] = []
    i, n = 0, len(payload)
    while i < n:
        remaining = n - i
        if remaining >= 512:
            block, n_payload = payload[i:i + 512], 126
            i += 512
        else:
            block, n_payload = payload[i:i + remaining], remaining // 4
            i += remaining
        pairs = np.frombuffer(block, dtype="<u2").reshape(-1, 2)
        offsets.extend(pairs[:n_payload, 0].tolist())
    arr = np.array(offsets, dtype=np.float64)
    if arr.size == 0 or arr.size % 3 != 0:
        raise ValueError(f"shading payload does not hold a whole number of RGB pixels ({arr.size} values)")
    return arr.reshape(-1, 3)


def _shading_stats(payload: bytes) -> dict:
    """Per-scan diagnostics summary of one shading-table upload payload:
    per-channel offset mean/min/max when the block format parses
    cleanly, else just the payload length (see diag.py/device.py's
    module docs -- this never raises, and never touches calibrate.py)."""
    try:
        px = _parse_shading_offsets(payload)
        return {
            "per_channel_offset": [
                {"mean": float(px[:, ch].mean()), "min": float(px[:, ch].min()), "max": float(px[:, ch].max())}
                for ch in range(3)
            ],
        }
    except Exception as e:
        return {"len_bytes": len(payload), "note": f"could not parse per-channel stats: {e}"}


# Lamp warmup retry: if gain_codes returns all-maxed (0x3F) on every
# channel, the lamp is likely cold (insufficient white-line levels for
# meaningful calibration).  Wait and re-measure up to this many times.
_WARMUP_MAX_RETRIES = 3
_WARMUP_RETRY_DELAY = 5.0   # seconds between retries

# Semantic PARK (see park_semantic() and docs/replay-analysis.md's
# "Conversion order" step 1): timeout and poll interval for the two
# condition waits that replace the captured trace's pacing.
_PARK_WAIT_TIMEOUT = 15.0
_PARK_POLL_INTERVAL = 0.02


# Masked completion test for the LOAD flow (docs/test-log.md Test 14,
# docs/load-analysis.md). The status word (reg 0x101 high byte, ack
# 0x55 low byte) is compared against the captured completion value
# under LOAD_STATUS_MASK, so the check requires BOTH the finished state
# class (upper nibble) AND the loader-sensor bit 0x08 in its captured
# state (feed: CLEAR, cassette pulled past the sensor; traverse: SET,
# cassette back in front of it), plus the busy bit 0x01 clear and the
# never-observed bit 0x02 clear. Only bit 0x04 is masked out: it is
# session-variable in every vendor capture (f0/f4 after the feed,
# d8/dc after the traverse) and carries no completion meaning.
#
# Not an exact match (0xf055/0xd855 rejected the correct vendor load),
# not a range or "upper nibble only" (0xf855 -- feed finished but the
# cassette still in front of the sensor -- would pass), and not the
# sensor bit alone (a still-running or wrong-class engine would pass).
LOAD_STATUS_MASK = 0xFB


def load_status_matches(got: bytes, want: bytes) -> bool:
    """True when status-word reply ``got`` completes like captured
    reply ``want`` under LOAD_STATUS_MASK (see above). The ack byte
    must match exactly; a short or malformed reply never matches."""
    if len(got) != 2 or len(want) != 2 or got[1] != want[1]:
        return False
    return (got[0] & LOAD_STATUS_MASK) == (want[0] & LOAD_STATUS_MASK)


def load_completion_target() -> int:
    """The status word the vendor capture settled on after the LOAD
    flow's traverse: the last coalesced status-word poll in
    tables_load.LOAD (0xdc55 in the clean-load capture). Derived from
    the table so it cannot drift from it; compared under
    LOAD_STATUS_MASK via load_status_matches()."""
    from . import tables_load
    polls = [op for op in tables_load.LOAD.ops if op.kind == "poll" and op.wv == 0x018E]
    if not polls or len(polls[-1].resp) != 2:
        raise RuntimeError("tables_load.LOAD has no final status-word poll")
    return int.from_bytes(polls[-1].resp, "big")


class Scanner:
    """Drives the of135i scan sequence over a UsbIo transport."""

    def __init__(self, io: UsbIo):
        self.io = io
        self._base_initialized = False
        # Set by initialize(), cleared by scan(): the vendor re-runs its
        # PREP/AFE_BASE equivalent before every frame, and a scan
        # without it is not a verified sequence.
        self._prepared_for_scan = False
        # Set by initialize(prep=False): the vendor's device-open
        # sequence (tables_load.OPEN) has been replayed this session.
        self._vendor_open = False

        # Hardware-safety session (safety.py). A real UsbIo brings its
        # own; a duck-typed transport (tests) gets one attached here,
        # with its device handle wrapped in the same write-gating proxy,
        # so the guard cannot be skipped by constructing Scanner over
        # something other than UsbIo.
        session = getattr(io, "session", None)
        if not isinstance(session, safety.HardwareSession):
            session = safety.HardwareSession()
            try:
                io.session = session
            except AttributeError:
                pass
            dev = getattr(io, "dev", None)
            if dev is not None and not isinstance(dev, safety.GuardedDevice):
                io.dev = safety.GuardedDevice(dev, session)
        self.session: safety.HardwareSession = session

        # Per-scan diagnostics (see diag.py). last_diag is populated by
        # scan()/_scan_dual() on every call (overwritten, not
        # accumulated); the _diag_* attributes below are the recording
        # points _run_phase/_exec_ops/_poll_one/_gain_with_warmup write
        # into, reset at the start of each scan() / _scan_dual() call.
        # None of this adds or changes any USB operation -- it only
        # observes values/timings the driver already computes.
        self.last_diag: dict | None = None
        self._diag_phase_seconds: dict[str, float] = {}
        self._diag_poll_timeouts: int = 0
        self._diag_cr_mismatches: int = 0
        self._diag_poll_timeout_details: list[dict] = []
        self._diag_warmup: dict | None = None
        self._diag_park_waits: dict | None = None

        # PARK phase implementation switch (see park_semantic() and
        # docs/replay-analysis.md): "verbatim" (default) replays the
        # captured op stream exactly, byte for byte; "semantic" issues
        # the same register writes with real read-modify-write and
        # condition waits instead of captured pacing. See _park().
        self.park_mode: str = "verbatim"

    @classmethod
    def open(cls) -> "Scanner":
        return cls(UsbIo.open())

    def close(self) -> None:
        self.io.close()

    def __enter__(self) -> "Scanner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Close only. Deliberately NO park/home/eject/initialize here:
        # if the block is left by an exception the hardware state is
        # unknown and the only valid recovery is a power cycle.
        self.close()

    # ------------------------------------------------------- safety gate

    def check_start_state(self) -> StartState:
        """Read-only: verify the scanner's start state for this session
        (reg 0x01, read once, strictly) and return the classification.
        Raises safety.UnsafeStartStateError -- with zero writes -- if
        the state is neither idle-homed (0x22) nor cold (0x00), or if
        it cannot be read. Idempotent: later calls return the verdict.
        """
        return safety.verify_start_state(self.io)

    @contextmanager
    def _operation(self, name: str, *, cold_ok: bool = False):
        """Run a public hardware operation.

        Outermost operation of a session: verifies the start state
        first (no writes happen before that), and refuses anything but
        the cold-init path (`cold_ok` operations: initialize/eject run
        cold_init() themselves; cold_init itself) on a cold scanner.
        Nested operations (initialize -> cold_init, scan -> park) run
        inside the outer one without re-checking -- the transient
        engine states inside a session are expected. Any exception
        escaping the block marks the session FAILED (safety.
        HardwareSession.operation); nothing is sent afterwards.
        """
        session = self.session
        if not session.operations:
            self.check_start_state()
            if session.state is SessionState.COLD and not cold_ok:
                raise OperationNotAllowedError(
                    f"{name}() refused: the scanner is in the cold start state "
                    f"(reg 0x01 = 0x00) and only initialize()/eject() -- which run the "
                    f"cold-init path first -- or cold_init() itself may write to it. "
                    f"{safety.NO_COMMANDS_SENT}",
                    observed=session.start_reg01, session=session.snapshot())
        with session.operation(name):
            yield

    def session_report(self) -> dict:
        """JSON-serializable snapshot of the safety session (state,
        write/execute counters, phase, failure record)."""
        return self.session.snapshot()

    # ------------------------------------------------------- op execution

    def _exec_ops(self, ops: list[Op], strict_polls: frozenset[int] = frozenset()) -> list[bytes]:
        """Execute a verbatim op list with replay_trace.py semantics:

          - cw: ctrl_transfer verbatim; if it's a wv=0x82 buffer
            descriptor, flush/(re)start the current measurement buffer
            (read descriptors start a new one, write descriptors just
            flush); if it's a wv=0x83 register batch containing the
            execute pulse (0x0f=0x01), wait for the engine-idle bit
            afterwards.
          - cr: perform the read; log (don't raise) on a captured-
            response mismatch.
          - poll: poll until the final captured response, timing out
            after max(3*dur, 10s); log-and-continue.
          - bo: bulk write verbatim.
          - bi: bulk read, routed into the current measurement buffer
            (if any).
          - dt pacing: sleep dt before the op when it exceeds
            _PACE_THRESHOLD, capped at _PACE_CAP.

        Returns the list of collected read buffers, in the order their
        buffer-read descriptors were encountered (most phases produce
        0 or 1; "scan" produces one per image chunk plus the trailing
        drain).

        Refuses to run outside a hardware operation (see _operation):
        this is the only way op streams reach the wire, and every
        caller must therefore be a guarded public method.
        """
        self.session.write_allowed_or_final()
        if not self.session.operations:
            raise OperationNotAllowedError(
                "_exec_ops() called outside a hardware operation; use the public "
                "Scanner methods (initialize/scan/eject/load_magazine). Nothing was sent.",
                session=self.session.snapshot())
        dev = self.io.dev
        collected: list[bytes] = []
        cur: bytearray | None = None

        def flush() -> None:
            nonlocal cur
            if cur is not None:
                collected.append(bytes(cur))
                cur = None

        for idx, op in enumerate(ops):
            if op.dt > _PACE_THRESHOLD:
                time.sleep(min(op.dt, _PACE_CAP))

            if op.kind == "cw":
                if op.wv == 0x0082 and op.br == 0x04 and len(op.data) == 8:
                    flush()
                    if op.wi != 1:
                        cur = bytearray()
                dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.data)
                # NOTE: no engine-idle wait here. The verbatim replayer
                # never waits and works (verified on hardware twice,
                # 2026-08-30): the trace's own polls carry the timing,
                # and during the image pass the scanner streams while
                # bulk reads drain it -- blocking until idle overflows
                # the scanner's buffer and yields stale RAM instead of
                # live data. _wait_engine_idle() remains in _motor_run
                # for our own out-of-trace moves (home/eject).
            elif op.kind == "cr":
                got = bytes(dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.length))
                if got != op.resp:
                    self._diag_cr_mismatches += 1
                    log.debug(
                        "cr wv=%#06x wi=%#06x: got %s want %s -- continuing",
                        op.wv, op.wi, got.hex(), op.resp.hex(),
                    )
            elif op.kind == "poll":
                self._poll_one(op, strict=idx in strict_polls)
            elif op.kind == "bo":
                dev.write(EP_BULK_OUT, op.data, timeout=5000)
            elif op.kind == "bi":
                data = dev.read(EP_BULK_IN, op.length, timeout=60000)
                if cur is not None:
                    cur.extend(data)
            else:
                raise ValueError(f"unknown op kind {op.kind!r}")

        flush()
        return collected

    def _poll_one(self, op: Op, strict: bool = False) -> None:
        """Poll until the captured settled value (with the documented
        leniencies) or the deadline. ``strict``: for completion waits an
        operation must not continue past (load_magazine's two motor-
        completion polls): a status-word poll (wValue 0x018e) must
        satisfy load_status_matches() -- state class AND loader-sensor
        bit, mask LOAD_STATUS_MASK -- any other poll must match exactly,
        and a timeout raises safety.StrictPollTimeoutError instead of
        logging."""
        want = op.resp
        # Most captured polls settled in < 0.03 s; the previous 10 s
        # floor added ~200 s of wasted timeouts per scan when dynamic
        # register bits (magazine presence, sensor state) differed from
        # the capture.  1 s is generous for state checks; real motor
        # waits (POSITION dur ~1.6 s) still get 3× their captured time.
        timeout = max(3 * op.dur, 1.0)
        t_start = time.monotonic()
        deadline = t_start + timeout
        dev = self.io.dev
        while True:
            got = bytes(dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.length))
            if got == want:
                if strict:
                    log.info("completion poll settled %s (exact) after %.2fs",
                             got.hex(), time.monotonic() - t_start)
                return
            if strict:
                if op.wv == 0x018E and load_status_matches(got, want):
                    log.info("completion poll settled %s (captured %s) after %.2fs",
                             got.hex(), want.hex(), time.monotonic() - t_start)
                    return
                if time.monotonic() > deadline:
                    self._diag_poll_timeouts += 1
                    raise safety.StrictPollTimeoutError(
                        f"completion poll wv={op.wv:#06x} wi={op.wi:#06x} did not reach the "
                        f"captured value: last {got.hex()}, want {want.hex()}"
                        f"{f' (mask {LOAD_STATUS_MASK:#04x}55)' if op.wv == 0x018E else ''} "
                        f"after {timeout:.1f}s. "
                        f"The hardware did not complete this step; the operation stops here. "
                        f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                        last=got, want=want, observed=self.session.start_reg01,
                        session=self.session.snapshot())
                time.sleep(0.004)
                continue
            # Status-word polls (0x018e): the upper nibble of the high
            # byte encodes the state class (0x9=busy, 0xD=transitioning,
            # 0xF=done); the lower nibble carries session-variable bits
            # (magazine sensor, lamp state) that legitimately differ from
            # the capture.  Accept an upper-nibble match so the poll
            # completes when the hardware reaches the right state class
            # instead of timing out on an irrelevant bit difference.
            if (op.wv == 0x018E and len(got) >= 1 and len(want) >= 1
                    and (got[0] & 0xF0) == (want[0] & 0xF0)):
                return
            # Reg 0x32 polls (wIndex 0x3222): bits 3-4 (0x18) reflect
            # loader-sensor and transport state that vary with magazine
            # presence; mask them out so a poll doesn't time out just
            # because the magazine is in a different position than the
            # capture session had (hardware-verified 2026-09-02: bit 3
            # is the loader sensor, bit 4 co-varies).
            if (op.wv == 0x008E and op.wi == 0x3222
                    and len(got) >= 1 and len(want) >= 1
                    and (got[0] & ~0x18 & 0xFF) == (want[0] & ~0x18 & 0xFF)):
                return
            if time.monotonic() > deadline:
                self._diag_poll_timeouts += 1
                if len(self._diag_poll_timeout_details) < 20:
                    self._diag_poll_timeout_details.append({
                        "wv": f"0x{op.wv:04x}", "wi": f"0x{op.wi:04x}",
                        "last": got.hex(), "want": want.hex(),
                    })
                log.warning(
                    "poll wv=%#06x wi=%#06x timed out after %.1fs "
                    "(last %s, want %s) -- continuing",
                    op.wv, op.wi, timeout, got.hex(), want.hex(),
                )
                return
            time.sleep(0.004)

    def _run_phase(self, phase: Phase, strict_polls: frozenset[int] = frozenset(),
                   **inject) -> list[bytes]:
        """Execute a phase's full op stream (with injections applied),
        returning its collected read buffers.

        Timed into self._diag_phase_seconds[phase.name] (accumulated,
        in case a phase name is ever run more than once per scan) for
        the per-scan diagnostics -- see diag.py and scan()/_scan_dual().
        """
        ops = phase.patched(**inject) if inject else phase.ops
        self.session.phase = phase.name
        t0 = time.monotonic()
        try:
            return self._exec_ops(ops, strict_polls=strict_polls)
        finally:
            dt = time.monotonic() - t0
            self._diag_phase_seconds[phase.name] = self._diag_phase_seconds.get(phase.name, 0.0) + dt

    # -------------------------------------------------------------- park

    def _park(self, t, ir: bool = False) -> None:
        """Dispatch on self.park_mode: "verbatim" replays `t.PARK`'s
        captured op stream exactly as before (byte-identical, see
        test_scan_sequence_matches_trace/test_dpi.py's
        test_scan_sequences -- every dpi's own table module has its
        own PARK data, hence `t` is the caller's actual table module);
        "semantic" runs park_semantic(t), which takes its table-
        specific constants (the two 0x8b control-write payloads and
        the optional 0x19=0x00 write) from that same `t.PARK`. Called
        at the end of scan()/_scan_dual() in place of the former bare
        self._run_phase(<tables>.PARK)."""
        if self.park_mode == "verbatim":
            self._run_phase(t.PARK)
        elif self.park_mode == "semantic":
            self.park_semantic(t, ir=ir)
        else:
            raise ValueError(f"unknown park_mode {self.park_mode!r} (want 'verbatim' or 'semantic')")

    def park_semantic(self, t=None, ir: bool = False) -> None:
        """Semantic replacement for the captured PARK phase (see
        docs/replay-analysis.md's "Conversion order" step 1 and PARK's
        row in the phase-by-phase table).

        The captured PARK op stream is a real park/teardown sequence
        followed by five identical repetitions of the vendor app's
        idle-loop heartbeat (36=fc, 3a=00, 36=fc, 33=0e, read/write-
        back reg 0x32, a 2 s pause, read reg 0x35, poll reg 0x32) --
        the repetitions are the vendor polling for completion, not a
        hardware requirement. This reproduces the same writes with:

          - real read-modify-write on regs 0x15 (clear bit 0x10),
            0x32 (write back what was read, unchanged) and 0x35
            (clear bit 0x40) instead of the captured constants, which
            are only correct on the capture unit/session;
          - Wait A: poll reg 0x35 until bit 0x40 is set (replaces the
            captured 0.74 + 2.06 s pacing before the RMW clear);
          - Wait B: poll reg 0x32 until (v & ~0x18) == (0x95 & ~0x18)
            -- bits 0x18 are loader-sensor/transport bits that
            legitimately differ between sessions, same mask
            _poll_one() already applies to 0x32 polls elsewhere
            (replaces the captured poll + 2.07 s pause + poll);
          - exactly ONE idle-loop round afterwards (heartbeat only,
            no 2 s pause), instead of five.

        Both waits are logged at DEBUG with what they waited for and
        how long it took, and never raise on timeout (15 s each,
        _PARK_WAIT_TIMEOUT) -- a WARNING is logged and the method
        continues, since everything left after Wait A/B is a status/
        heartbeat write, not a motor command.

        The table-specific constants come from `t.PARK` itself (`t`
        defaults to tables_ir when `ir` else tables): the 0x8b
        control-write payloads (wIndex 0x0b: 0c000100 in the 3600 dpi
        captures, 22000100 in the 600-7200 dpi captures; wIndex 0x0f:
        e0ff/c0ff/f8ff/feff/fcff/f0ff -- varies per dpi and IR, meaning
        unknown, kept verbatim per table) and whether a 0x19=0x00 write
        follows the 0x32 write-back (every dual-light table has it, the
        plain 3600 table does not). Everything else in PARK is
        identical across all six tables.

        Records total elapsed time into self._diag_phase_seconds
        ["park"] and wait details into self._diag_park_waits (see
        scan()/_scan_dual()'s last_diag "park_waits" key).
        """
        if t is None:
            t = tables_ir if ir else tables
        park_ops = t.PARK.ops
        ctrl_8b = [(op.wi, bytes(op.data)) for op in park_ops
                   if op.kind == "cw" and op.wv == 0x008B]
        has_0x19 = any(
            op.kind == "cw" and op.wv == 0x0083
            and any(op.data[i] == 0x19 for i in range(0, len(op.data), 2))
            for op in park_ops
        )
        if len(ctrl_8b) != 2:
            raise Of135iError(
                f"park_semantic: expected 2 control writes with wValue 0x8b in "
                f"{t.__name__}.PARK, found {len(ctrl_8b)}"
            )
        with self._operation("park"):
            self._park_semantic_body(ctrl_8b, has_0x19)

    def _park_semantic_body(self, ctrl_8b, has_0x19) -> None:
        self.session.phase = "park"
        t0 = time.monotonic()
        dev = self.io.dev
        waits = {
            "a_seconds": None, "a_timed_out": False,
            "b_seconds": None, "b_timed_out": False,
        }

        # ---- real park/teardown sequence (captured ops 0-55) -----------
        dev.ctrl_transfer(0x40, 0x0C, 0x8D, 0, b"\x00")
        self.io.write_regs([(0x03, 0x30)])
        self.io.write_regs([(0x03, 0x20)])
        self.io.write_regs([(0x01, 0x22)])
        self.io.write_regs([(0x3A, 0x00)])

        v15 = self.io.read_reg(0x15)
        self.io.write_regs([(0x15, v15 & ~0x10 & 0xFF)])

        self.io.write_regs([(0x02, 0x30)])
        self.io.write_regs([(0x36, 0xFC), (0x3A, 0x00), (0x36, 0xFC), (0x33, 0x0E)])
        self.io.write_regs([(0x03, 0x10)])
        self.io.write_regs([(0x03, 0x00)])

        for wi, payload in ctrl_8b:
            dev.ctrl_transfer(0x40, 0x04, 0x8B, wi, payload)

        v32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, v32)])
        if has_0x19:
            self.io.write_regs([(0x19, 0x00)])

        # ---- Wait A: reg 0x35 bit 0x40 set, then clear it (RMW) ---------
        _t = time.monotonic()
        deadline = _t + _PARK_WAIT_TIMEOUT
        v35 = self.io.read_reg(0x35)
        while not (v35 & 0x40):
            if time.monotonic() > deadline:
                waits["a_timed_out"] = True
                log.warning(
                    "park_semantic: wait A (reg 0x35 bit 0x40) timed out "
                    "after %.1fs (last 0x%02x) -- continuing",
                    _PARK_WAIT_TIMEOUT, v35,
                )
                break
            time.sleep(_PARK_POLL_INTERVAL)
            v35 = self.io.read_reg(0x35)
        waits["a_seconds"] = time.monotonic() - _t
        log.debug(
            "park_semantic: wait A (reg 0x35 bit 0x40) done in %.3fs (last 0x%02x, timed_out=%s)",
            waits["a_seconds"], v35, waits["a_timed_out"],
        )
        self.io.write_regs([(0x35, v35 & ~0x40 & 0xFF)])

        # ---- Wait B: reg 0x32 reaches state 0x95 (masked 0x18) ----------
        _t = time.monotonic()
        deadline = _t + _PARK_WAIT_TIMEOUT
        target = 0x95 & ~0x18 & 0xFF
        v32 = self.io.read_reg(0x32)
        while (v32 & ~0x18 & 0xFF) != target:
            if time.monotonic() > deadline:
                waits["b_timed_out"] = True
                log.warning(
                    "park_semantic: wait B (reg 0x32 -> 0x95 masked 0x18) "
                    "timed out after %.1fs (last 0x%02x) -- continuing",
                    _PARK_WAIT_TIMEOUT, v32,
                )
                break
            time.sleep(_PARK_POLL_INTERVAL)
            v32 = self.io.read_reg(0x32)
        waits["b_seconds"] = time.monotonic() - _t
        log.debug(
            "park_semantic: wait B (reg 0x32 -> 0x95 masked 0x18) done in "
            "%.3fs (last 0x%02x, timed_out=%s)",
            waits["b_seconds"], v32, waits["b_timed_out"],
        )

        # ---- one idle-loop round (heartbeat), no captured 2 s pause -----
        self.io.write_regs([(0x36, 0xFC), (0x3A, 0x00), (0x36, 0xFC), (0x33, 0x0E)])
        v32b = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, v32b)])

        self._diag_park_waits = waits
        dt = time.monotonic() - t0
        self._diag_phase_seconds["park"] = self._diag_phase_seconds.get("park", 0.0) + dt

    # -------------------------------------------------------------- init

    def initialize(self, ir: bool = False, dpi: int = 3600, prep: bool = True) -> None:
        """Full device initialization.

        First (once per session) the power-on base register table + AFE
        base values -- what the vendor driver programs at device open;
        without these the scanner keeps whatever state the previous
        session left, which breaks calibration (observed 2026-08-30 as
        a saturated white measurement after a VM session). Then the
        pre-scan phases from the capture (tables.PREP + AFE_BASE), full
        op stream.

        Called once per frame in a batch. The vendor never rewrites the
        power-on table between frames -- only the PREP/AFE_BASE
        equivalent (protocol-notes.md pass 14) -- so the base table is
        written on the first call only.

        Cold start: the session's start-state check (safety.py) reads
        reg 0x01 once before the first write. 0x22 (idle-homed) is the
        state every successful run to date started from; 0x00 (fresh
        power-on, never homed) makes this run the vendor's cold-start
        sequence first, exactly once per session -- and nothing else
        is accepted: any other value, or an unreadable one, refuses
        the whole session with zero writes.

        ``prep=False``: the vendor's device-open state for the magazine
        flow -- replays tables_load.OPEN (clean-load capture ops 37-88:
        chip-id ack, the app-start register table with the LOADER motor
        profile, acks, AFE enable, EEPROM reads, AFE reset, AFE base
        values) verbatim instead of BASE_INIT_PAIRS + AFE values, after
        cold_init() if the scanner is cold. BASE_INIT_PAIRS carries the
        scan motor profile (0x7e/0x7f) and the jog's first move takes
        its profile from the table: on top of BASE_INIT_PAIRS it ran at
        scan speed (Test 16). No scan preparation is run; scan() still
        requires a prep=True initialize() in the same session, which
        then writes BASE_INIT_PAIRS as usual.
        """
        # Validate parameters before entering the operation, so a bad
        # argument is a plain error and not a hardware-session failure.
        t = _tables_for(dpi, ir)
        with self._operation("initialize", cold_ok=True):
            if not prep:
                from . import tables_load
                if self.session.state is SessionState.COLD:
                    log.info("initialize: reg 0x01=0x00 -- cold-start state, running cold_init() first")
                    self.cold_init()
                self.session.phase = "vendor_open"
                self._run_phase(tables_load.OPEN)
                self._vendor_open = True
                return
            if not self._base_initialized:
                if self.session.state is SessionState.COLD:
                    log.info(
                        "initialize: reg 0x01=0x00 -- cold-start state (fresh "
                        "power-on), running cold_init() first",
                    )
                    self.cold_init()
                self.session.phase = "base_init"
                self.io.write_regs(tables_base.BASE_INIT_PAIRS)
                for adr, val in tables_base.AFE_BASE_PAIRS:
                    self.io.write_regs([(0x51, adr), (0x5D, 0x00), (0x5E, val)])
                self._base_initialized = True
            # IR mode: trace 04's own prep carries IR-LED setup that the
            # plain prep lacks (a dim IR pass was observed without it,
            # 2026-08-30) -- run the matching phase set. Other resolutions:
            # their own prep/afe_base (the base register table is dpi-
            # dependent: regs 0x3b/0x3c, and the sensor timing at 7200).
            self._run_phase(t.PREP)
            self._run_phase(t.AFE_BASE)
            self._prepared_for_scan = True

    # ---------------------------------------------------------- cold-start

    def cold_init(self) -> None:
        """Vendor cold-start sequence (USB capture 01-init.pcap).

        A freshly power-cycled scanner has never been homed: reg 0x01
        reads without the vendor's "ready" bit (0x20) set, and
        initialize()'s usual power-on table assumes an already-homed
        transport (0x01=0x22, 0x32=0x1f -- see driver-design.md/
        protocol-notes.md). This reproduces the vendor's own recovery
        from that state: a GL chip handshake, the cold-start register
        table (COLD_INIT_PAIRS -- the loader motor speed profile,
        not the scan one), an AFE bring-up (access enable, EEPROM
        read, reset, base values), an interrupt/sensor toggle, and
        three rounds of loader homing moves (3 moves each), with the
        register table + AFE sequence rewritten between rounds.

        Called automatically by initialize()/eject() on a cold scanner;
        can also be called directly, but ONLY from the cold start state
        (reg 0x01 = 0x00, see safety.py): on an already-homed scanner,
        or one in any other state, it refuses with zero writes -- re-
        homing a magazine-loaded transport is not a verified sequence.
        Leaves the scanner in the same homed state initialize()
        otherwise assumes, but does NOT itself write BASE_INIT_PAIRS --
        COLD_INIT_PAIRS carries the loader (not scan) motor profile, so
        initialize() still runs its normal power-on table write
        afterwards.

        When the sequence completes, reg 0x01 is read again (strictly)
        and must be 0x22 -- COLD_INIT_PAIRS writes 0x01=0x22 and the
        idle bit 0x20 sets when the last homing move completes -- for
        the session to be armed for normal operations. Anything else
        is a new observation: the session is refused there, no
        further command is sent, and the operator is asked to power-
        cycle (and to record the value).
        """
        # Precondition BEFORE the operation (a refusal here is not a
        # hardware failure): the read-only start check, then COLD-only.
        self.check_start_state()
        if self.session.state is not SessionState.COLD or self.session.cold_init_done:
            raise OperationNotAllowedError(
                "cold_init() is only permitted from the cold start state (reg 0x01 = "
                f"0x00) and only once per session; session is {self.session.state.value}"
                f"{' (cold_init already done)' if self.session.cold_init_done else ''}. "
                f"{safety.NO_COMMANDS_SENT}",
                observed=self.session.start_reg01, session=self.session.snapshot())
        with self._operation("cold_init", cold_ok=True):
            self.session.phase = "cold_init"
            self._cold_init_body()
            self.session.phase = "cold_init:verify"
            val01 = self.io.read_reg(0x01, strict=True)
            if val01 != safety.REG01_IDLE:
                self.session.refuse(
                    f"cold_init completed but reg 0x01 = {val01:#04x} instead of "
                    f"{safety.REG01_IDLE:#04x}", val01)
                raise UnsafeStartStateError(
                    f"cold_init completed but the scanner reads reg 0x01 = {val01:#04x}, "
                    f"not the expected idle state {safety.REG01_IDLE:#04x}. No further "
                    f"commands were sent. This is a new observation -- please record it. "
                    f"{safety.POWER_CYCLE_INSTRUCTION}",
                    observed=val01, session=self.session.snapshot())
            self.session.arm_after_cold_init(val01)
            log.info("cold_init: complete, reg 0x01 = %#04x -- session armed", val01)

    def _cold_init_body(self) -> None:
        log.info("cold_init: starting vendor cold-start sequence")

        # ---- 1: GL chip handshake --------------------------------------
        chip_id = bytes(self.io.dev.ctrl_transfer(0xC0, 0x0C, 0x008A, 0x26FE, 1))
        log.info("cold_init: chip id %s", chip_id.hex())
        self.io.dev.ctrl_transfer(0x40, 0x0C, 0x008B, 0x26FE, b"")

        # ---- 2: status word, poll until ready ---------------------------
        word = self.io.read_status_word()
        log.info("cold_init: initial status word %#06x", word)
        self.io.poll_status_word(mask=0xF000, value=0xF000, timeout=15.0)

        # ---- 3-7: cold register table + end_access + AFE bring-up ------
        self._cold_write_table_and_afe()

        # ---- 8: interrupt/sensor setup -----------------------------------
        val31 = self.io.read_reg(0x31)
        self.io.write_regs([(0x31, val31 & 0x7F)])
        val31 = self.io.read_reg(0x31)
        self.io.write_regs([(0x31, (val31 | 0x80) & 0xFF)])

        # ---- 9: three rounds of loader homing (3 moves each) -------------
        for round_n in range(1, 4):
            log.info("cold_init: homing round %d/3", round_n)
            self.session.phase = f"cold_init:homing-{round_n}"
            self._cold_homing_round()
            if round_n < 3:
                self._cold_write_table_and_afe()

        log.info("cold_init: homing done")

    def _cold_write_table_and_afe(self) -> None:
        """Steps 3-7 of cold_init(): the cold register table, end-of-
        access acks, AFE access enable + EEPROM read (logged only, not
        yet consumed), AFE reset, and AFE base values. Re-run between
        each of the three homing rounds in cold_init()'s step 9, per
        the capture."""
        self.io.write_regs(tables_base.COLD_INIT_PAIRS)

        self.io.end_access(0x8C, 16)
        self.io.end_access(0x8C, 19)

        # AFE access enable.
        self.io.write_regs([(0x0B, 0x64)])
        self.io.write_regs([(0x13, 0x0F)])
        self.io.write_regs([(0x0B, 0x6C)])

        # EEPROM read sequence -- logged for now, not yet used.
        self.io.end_access(0x8B, 7)
        self.io.dev.ctrl_transfer(0x40, 0x04, 0x008B, 0x0009, bytes(4))
        eeprom1 = bytes(self.io.dev.ctrl_transfer(0xC0, 0x04, 0x008A, 0x0010, 3))
        self.io.dev.ctrl_transfer(0x40, 0x04, 0x008B, 0x000B, b"\x00\x00\x01\x00")
        eeprom2 = bytes(self.io.dev.ctrl_transfer(0xC0, 0x04, 0x008A, 0x000F, 19))
        log.info("cold_init: EEPROM data: %s / %s", eeprom1.hex(), eeprom2.hex())

        # AFE reset.
        self.io.write_regs([(0x03, 0x10)])
        self.io.write_regs([(0x03, 0x00)])

        # AFE base values.
        for adr, val in tables_base.AFE_BASE_PAIRS:
            self.io.write_regs([(0x51, adr), (0x5D, 0x00), (0x5E, val)])

    def _cold_motor_move(self, feedl: int, full_speed_regs: bool) -> None:
        """One loader-profile motor move within a cold_init() homing
        round: enable, feed-length + motor params (the full 19-register
        loader speed profile when `full_speed_regs`, otherwise just the
        6 move-specific registers -- COLD_INIT_PAIRS already primed the
        speed profile at the top of the round), the loader slope table
        uploaded to both scanner-RAM addresses (same table _motor_run/
        eject() use), execute, poll to completion (target 0xf855, per
        the capture)."""
        self.io.write_regs([(0x09, 0x08)])
        move_regs = [
            (0x02, 0x18), (0xAE, 0x00), (0xAF, 0xFF),
            (0x3D, 0x00), (0x3E, feedl & 0xFF), (0x3F, (feedl >> 8) & 0xFF),
        ]
        if full_speed_regs:
            self.io.write_regs(list(tables_base.LOADER_SPEED_PAIRS) + move_regs)
        else:
            self.io.write_regs(move_regs)
        for addr in (0x1000C000, 0x10010000):
            self.io.buf_write(addr, tables_base.SLOPE_TABLE_LOADER)
        self.io.write_regs([(0x0F, 0x01)])
        self.io.poll_status_word(mask=0xFFFF, value=0xF855, timeout=30.0)

    def _cold_homing_round(self) -> None:
        """One of cold_init()'s three homing rounds: pre-move sensor/
        int-ack prep, move 1 (feedl=8730, minimal regs), a resync
        interstitial + move 2 (feedl=8730, full speed-profile rewrite),
        move 3 (feedl=4620), then a settle poll on regs 0x35/0x32."""
        FEEDL_1_2 = 8730
        FEEDL_3 = 4620

        # ---- pre-move setup -----------------------------------------
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, val32 & ~0x02 & 0xFF)])
        self.io.read_status_word()
        self.io.write_regs([(0x36, 0xFC)])
        self.io.write_regs([(0x33, 0x8E)])
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, (val32 | 0x02) & 0xFF)])
        self.io.read_status_word()

        # ---- move 1 ----------------------------------------------------
        self._cold_motor_move(FEEDL_1_2, full_speed_regs=False)

        # ---- resync between move 1 and move 2 --------------------------
        self.io.write_regs([(0x09, 0x00)])
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, val32)])
        val35 = self.io.read_reg(0x35)
        self.io.write_regs([(0x35, val35 & ~0x40 & 0xFF)])
        self.io.read_reg(0x32)
        self.io.poll_status_word(mask=0xF000, value=0xF000, timeout=15.0)
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, val32 & ~0x02 & 0xFF)])
        w = self.io.read_status_word()
        if w != 0xF055:
            log.debug("cold_init: resync status word %#06x (want 0xf055)", w)
        w = self.io.read_status_word()
        if w != 0xF855:
            log.debug("cold_init: resync status word %#06x (want 0xf855)", w)

        # ---- move 2 (full speed-profile rewrite) ------------------------
        self._cold_motor_move(FEEDL_1_2, full_speed_regs=True)

        # ---- move 3 ------------------------------------------------------
        self._cold_motor_move(FEEDL_3, full_speed_regs=False)

        # ---- settle: motor disable, poll 0x35/0x32 until stable ---------
        self.io.write_regs([(0x09, 0x00)])
        val35 = val32 = None
        for _ in range(30):
            val35 = self.io.read_reg(0x35)
            val32 = self.io.read_reg(0x32)
            if val35 == 0xBB and val32 == 0x1F:
                break
            time.sleep(0.05)
        else:
            log.warning(
                "cold_init: settle poll did not reach 0x35=0xbb/0x32=0x1f "
                "(last 0x35=%#04x 0x32=%#04x) -- continuing",
                val35, val32,
            )

    def _wait_engine_idle(self, timeout: float = 90.0) -> None:
        """Wait until the scan/motor engine is idle again.

        Discovered on hardware 2026-08-30 (resolves open question 5 in
        protocol-notes.md): bit 0x20 of reg 0x01 CLEARS while the
        engine executes (0x22 -> 0x02 observed during homing) and sets
        again on completion. Waiting on this bit replaces mimicking the
        capture's poll timing, which raced the hardware and returned
        stale buffer data. Applied after every cw wv=0x83 batch
        containing the execute pulse (0x0f=0x01), before any
        subsequent buffer reads -- see _exec_ops.
        """
        try:
            self.io.wait_reg(0x01, 0x20, timeout=timeout, mask=0x20)
        except Of135iError as e:
            log.warning("engine-idle wait: %s -- continuing", e)

    def is_magazine_present(self) -> bool:
        """Check the loader sensor (vendor INI: LoaderSensorReg=0x101,0x08).

        Bit 0x08 set = a magazine is physically PRESENT in the slot
        (hardware-verified 2026-09-02: 0xe0 without, 0xe8 with; Test 12
        2026-09-04: also set for a loose, unlatched magazine with the
        orange LED, read on a cold, unwritten register). It says nothing
        about the magazine being fed in or latched, and is unreliable
        after the base register table has been written.
        """
        val = self.io.read_ext_reg(0x101)
        return bool(val & 0x08)

    # -------------------------------------------------------------- motor

    def _motor_run(self, mode: int, feedl: int) -> None:
        """Stand-alone motor move, modelled on the vendor's eject in the
        SilverFast capture (protocol-notes.md pass 14, eject addendum):
        sensor/int-ack prep (33=8e, 32=8f), 09=08, batch 02=mode
        ae/af 3d/3e/3f=FEEDL (24-bit, hi/mid/lo), the positioning slope
        table uploaded to 0x1000c000 and 0x10010000, 0f=01 executes,
        wait, 09=00.

        The slope-table uploads are NOT optional: without them the
        motor runs against whatever curve happens to be in scanner RAM.
        That worked while a positioning move had left the table there
        and stalled with a grinding noise once a scan pass had
        overwritten it (eject after a 4-frame batch, 2026-09-02;
        cf. pass 13 on the naked load feed)."""
        self.io.write_regs([(0x33, 0x8E)])
        self.io.write_regs([(0x32, 0x8F)])
        self.io.write_regs([(0x09, 0x08)])
        self.io.write_regs([
            (0x02, mode), (0xAE, 0x00), (0xAF, 0xFF),
            (0x3D, (feedl >> 16) & 0xFF),
            (0x3E, (feedl >> 8) & 0xFF),
            (0x3F, feedl & 0xFF),
        ])
        for addr in (0x1000C000, 0x10010000):
            self.io.buf_write(addr, tables.SLOPE_TABLE_POSITION)
        self.io.write_regs([(0x0F, 0x01)])
        # No motor-busy bit is confirmed yet (driver-design.md open
        # items / protocol-notes.md pass 4 "Motor/feed" note): reg 0x01
        # was observed unchanged across a real homing move on hardware.
        # Poll it briefly so a deployed driver at least has somewhere
        # to add a real busy check later, but don't block indefinitely.
        time.sleep(0.1)
        self._wait_engine_idle()
        self.io.write_regs([(0x09, 0x00)])

    def home(self) -> None:
        """Mode 0x30, FEEDL=1 execute -- historically taken for a homing
        move (pass 3/4). Pass 14 showed mode 0x30 is the SCAN PASS: this
        runs the engine for whatever line count regs 0x25-0x27 hold, and
        the vendor flow has no homing command at all (positioning is an
        absolute mode-0x18 feed from wherever the carriage is). Not used
        by the scan flow, the CLI or any tool; kept only as a guarded
        entry point (it is NOT a recovery command)."""
        with self._operation("home"):
            self.session.phase = "home"
            self._motor_run(0x30, 1)

    def _strict_status_polls(self, phase: Phase) -> frozenset[int]:
        """Indices of a phase's status-word polls (wValue 0x018e): the
        motor completions, run strictly under load_status_matches()."""
        return frozenset(i for i, op in enumerate(phase.ops)
                         if op.kind == "poll" and op.wv == 0x018E)

    def jog_magazine(self) -> None:
        """Run the vendor's app-start jog (tables_load.JOG, clean-load
        capture ops 88-170): interrupt/sensor acks, motor enable, feed
        6690, 0x35=0xbb, feed 6690, eject 3090, motor disable -- with
        the loader profile and slope tables, verbatim.

        Why: every vendor load that engaged the cassette was preceded
        by this jog (eject-from-loaded 2026-09-02, load-only
        2026-08-30, clean-load 2026-09-05), and every load of ours
        that did not engage lacked it (Tests 11b/12b/13/15: feed done,
        loader-sensor bit still set, magazine loose). The jog's own
        feeds never clear the sensor bit (0xf855 after each move);
        it positions the mechanism for the load that follows. The
        vendor runs it with the magazine loose in the slot, right
        after its device-open sequence (initialize(prep=False) =
        tables_load.OPEN, LOADER motor profile in the table: the jog's
        first move takes 0x7e/0x7f from it),
        and then has the operator take the magazine out and reinsert
        it to the stop before load_magazine().

        Its four status-word polls are strict under the masked test
        (done class, sensor bit SET, busy clear; captured 0xf855): a
        move that does not complete stops the flow, fails the session
        and requires a power cycle. NOT yet hardware-verified as part
        of a load.
        """
        from . import tables_load
        self.session.write_allowed_or_final()
        if not (self._base_initialized or self._vendor_open):
            raise OperationNotAllowedError(
                "jog_magazine() requires initialize() first in this session. "
                f"{safety.NO_COMMANDS_SENT}", session=self.session.snapshot())
        with self._operation("jog_magazine"):
            self._run_phase(tables_load.JOG, strict_polls=self._strict_status_polls(tables_load.JOG))
            word = self.io.read_status_word()
            log.info("jog_magazine: complete, status word %#06x", word)

    def load_magazine(self) -> None:
        """Run the vendor's standalone magazine insert flow
        (tables_load.LOAD, from the 2026-09-05 clean-load capture, Test
        14): loader-sensor ack, the engaging feed with the vendor's full
        register block, the prescan traverse and one idle housekeeping
        cycle. The cassette must already be pushed in by hand, fully,
        to the stop (Test 14: the vendor loads from there), AFTER
        jog_magazine() has run in this session -- the vendor's flow is
        initialize(prep=False) -> jog -> operator reinserts -> load,
        and a load without the jog has never engaged the cassette
        (Tests 11b-15). Requires initialize() first in this session;
        tools/load_magazine.py runs the whole flow.

        Completion is verified, not assumed, at three points, each with
        the masked test load_status_matches() (state class AND loader-
        sensor bit 0x08, mask LOAD_STATUS_MASK -- see the module-level
        note): the feed's completion poll must reach the done class
        with the sensor bit CLEAR (captured 0xf455: the cassette was
        pulled past the sensor; 0xec55/0xf855 fail), the traverse's
        poll the done class with the sensor bit SET (captured 0xdc55;
        0xcc55 fails), and after the replay the status word is read
        once more and must still match the traverse target
        (load_completion_target()), else LoadIncompleteError. A poll
        that does not get there stops the flow right there (safety.
        StrictPollTimeoutError: a feed that did not engage the cassette
        never gets a traverse). Either failure marks the session
        FAILED: the transport state is then unknown and the magazine
        may not be latched. No recovery is attempted; a power cycle is
        required.

        Even a completed load only sets the vendor's "loaded"
        indication (blue LED); the loader sensor reports presence,
        not latching (docs/hardware-safety.md). This table has NOT yet
        been hardware-verified as a load.
        """
        from . import tables_load
        self.session.write_allowed_or_final()
        if not (self._base_initialized or self._vendor_open):
            raise OperationNotAllowedError(
                "load_magazine() requires initialize() first in this session. "
                f"{safety.NO_COMMANDS_SENT}", session=self.session.snapshot())
        expected = load_completion_target()
        with self._operation("load_magazine"):
            self._run_phase(tables_load.LOAD,
                            strict_polls=self._strict_status_polls(tables_load.LOAD))
            self.session.phase = "load_verify"
            word = self.io.read_status_word()
            if not load_status_matches(word.to_bytes(2, "big"), expected.to_bytes(2, "big")):
                raise safety.LoadIncompleteError(
                    f"magazine load did NOT complete: status word {word:#06x}, expected "
                    f"{expected:#06x} under mask {LOAD_STATUS_MASK:#04x}55 (the vendor capture's "
                    f"completion class with the loader-sensor bit). The transport "
                    f"state is unknown and the magazine may not be latched. "
                    f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                    status_word=word, expected=expected, observed=self.session.start_reg01,
                    session=self.session.snapshot())
            log.info("load_magazine: complete, status word %#06x", word)

    def eject(self) -> None:
        """Eject the film magazine the way the vendor driver does it from
        a LOADED magazine (its app-start "jog": feed 6690, eject 3090 --
        load-only capture ops 151-169, ramval ops 736-760; protocol-
        notes.md pass 14 eject addendum):

            33=8e, 32=<read>|02, 09=08,
            1c=00 02=18 3d/3e/3f=3090 7d=00 7e=75 7f=30 8a-92=00 ae=00 af=ff,
            loader slope table -> 0x1000c000 and 0x10010000,
            0f=01, busy-wait, 09=00

        The post-scan eject variant (06-eject: positioning slope table,
        fast speed regs left over from the scan flow) stalled twice on
        2026-09-02 when issued from a loaded/parked transport; this
        variant is the one that freed the magazine each time via the
        vendor software.

        Safety guards:
        - Start-state guard (safety.py): a session that reads anything
          but 0x22/0x00 in reg 0x01 is refused before any write.
        - If no magazine is present (loader sensor bit 0x08 clear),
          log a notice and return immediately — no motor commands.
          (The sensor is only trustworthy before the first initialize()
          of the scanner's power cycle -- docs/hardware-safety.md.)
        - Cold start (0x00, never homed): run cold_init() first —
          ejecting from an unhomed state is undefined.
        """
        with self._operation("eject", cold_ok=True):
            if not self.is_magazine_present():
                log.info("eject: no magazine detected — nothing to do")
                return
            if self.session.state is SessionState.COLD:
                log.info("eject: reg 0x01=0x00 (cold state) — running cold_init() first")
                self.cold_init()
            self.session.phase = "eject"
            self._eject_body()

    def _eject_body(self) -> None:
        feedl = 3090
        self.io.write_regs([(0x33, 0x8E)])
        self.io.write_regs([(0x32, (self.io.read_reg(0x32) | 0x02) & 0xFF)])
        self.io.write_regs([(0x09, 0x08)])
        self.io.write_regs(
            [(0x1C, 0x00), (0x02, 0x18),
             (0x3D, (feedl >> 16) & 0xFF), (0x3E, (feedl >> 8) & 0xFF), (0x3F, feedl & 0xFF)]
            + [p for p in tables_base.LOADER_SPEED_PAIRS if p[0] != 0x1C]
            + [(0xAE, 0x00), (0xAF, 0xFF)]
        )
        for addr in (0x1000C000, 0x10010000):
            self.io.buf_write(addr, tables_base.SLOPE_TABLE_LOADER)
        self.io.write_regs([(0x0F, 0x01)])
        # Vendor completion poll: status word (wValue 0x018e) reads
        # 0xd9 -> 0xf9 -> 0xf8 over ~0.9 s; done at 0xf8. The 0x008e
        # reg-0x01 engine bit used elsewhere stays clear after an eject
        # (observed 2026-09-02: 90 s timeout on a successful eject).
        deadline = time.monotonic() + 10.0
        last = None
        while time.monotonic() < deadline:
            last = self.io.read_status()
            if (last & 0x21) == 0x20:
                break
            time.sleep(0.02)
        else:
            log.warning("eject: status word stuck at %#04x -- continuing", last)
        self.io.write_regs([(0x09, 0x00)])

    # -------------------------------------------------- lamp warmup retry

    def _gain_with_warmup(self, cal_white_phase, parse_white):
        """Compute AFE gain codes from a white-line measurement, retrying
        if the gain clips to maximum on every channel (a sign that the
        lamp hasn't warmed up yet — observed after cold_init).

        `cal_white_phase` is the Phase to run for the white measurement.
        `parse_white` is a callable(raw_bytes) -> (N, 3) ndarray of the
        white line(s) to feed to gain_codes().

        Returns (gain_r, gain_g, gain_b).

        Records into self._diag_warmup (for scan()/_scan_dual()'s
        per-scan diagnostics -- see diag.py): "attempts" (CAL_WHITE run
        count), "gain_history" (one [r,g,b] per attempt), "exhausted"
        (bool), and "white_mean"/"white_max" (per-channel, from the
        LAST attempt's white array -- the one gain_codes() was
        actually computed from for the returned codes).
        """
        diag = {"attempts": 0, "gain_history": [], "exhausted": False,
                "white_mean": None, "white_max": None}
        self._diag_warmup = diag

        for attempt in range(_WARMUP_MAX_RETRIES + 1):
            white_raw = self._run_phase(cal_white_phase)[0]
            white = parse_white(white_raw)
            gain_r, gain_g, gain_b = calibrate.gain_codes(white, clamp_nonpositive=True)

            diag["attempts"] += 1
            diag["gain_history"].append([gain_r, gain_g, gain_b])
            diag["white_mean"] = [float(x) for x in white.astype(np.float64).mean(axis=0)]
            diag["white_max"] = [float(x) for x in white.astype(np.float64).max(axis=0)]

            all_maxed = (gain_r == calibrate._GAIN_MAX_CODE
                         and gain_g == calibrate._GAIN_MAX_CODE
                         and gain_b == calibrate._GAIN_MAX_CODE)
            if not all_maxed:
                if attempt > 0:
                    log.info("gain stabilized after %d warmup retry(s)", attempt)
                return gain_r, gain_g, gain_b

            if attempt < _WARMUP_MAX_RETRIES:
                log.warning(
                    "gain maxed R=G=B=0x3F — lamp may need warmup, "
                    "retrying in %.0fs (%d/%d)",
                    _WARMUP_RETRY_DELAY, attempt + 1, _WARMUP_MAX_RETRIES,
                )
                time.sleep(_WARMUP_RETRY_DELAY)

        diag["exhausted"] = True
        log.warning(
            "gain still maxed after %d retries — proceeding "
            "(image may be flat/underexposed)",
            _WARMUP_MAX_RETRIES,
        )
        return gain_r, gain_g, gain_b

    # -------------------------------------------------------------- scan

    def scan(
        self, frame: int = 1, lines: int | None = None, ir: bool = False,
        dpi: int = 3600,
    ) -> tuple[bytes, int] | tuple[bytes, int, dict]:
        """Run the full calibration + scan sequence for one frame.

        dpi=3600, ir=False (default): the plain visible-only flow;
        returns (raw_bytes, width) -- raw_bytes is the pixel-interleaved
        RGB16LE image buffer (image.assemble() turns it into an ndarray),
        width is tables.IMAGE_WIDTH. This path is byte-identical to
        before ir/dpi were added (tests/test_calibrate.py's sequence
        test).

        ir=True, or any other dpi: the dual-light (alternating IR/
        visible line) flow of that resolution's table module
        (dual_tables(dpi); every non-3600 capture is a dual-light one)
        via _scan_dual(), returning (raw_bytes, width, meta) with meta
        {"width": W, "alternating": True, "dpi": dpi} -- see
        image.split_ir() for turning raw_bytes into separate visible/IR
        arrays.

        Safety: runs as a hardware operation (start-state guard on the
        session's first write; any exception marks the session failed
        and nothing -- no PARK -- is sent afterwards). Requires
        initialize() first in this session, before EVERY scan, as the
        vendor flow does; refuses with zero writes otherwise.
        """
        # Magazine presence: the CLI checks the loader sensor before
        # initialize() (the only point where it's reliable). By the time
        # scan() runs, the base-register writes have changed ext reg
        # 0x101 so re-checking here would be unreliable. Callers that
        # bypass the CLI should verify the magazine is loaded before
        # calling scan().
        #
        # Parameter validation BEFORE the operation: a bad argument is a
        # plain error, not a hardware-session failure.
        self.session.write_allowed_or_final()
        t = dual_tables(dpi) if (ir or dpi != 3600) else None
        if self.park_mode not in ("verbatim", "semantic"):
            raise ValueError(f"unknown park_mode {self.park_mode!r} (want 'verbatim' or 'semantic')")
        if not self._prepared_for_scan:
            raise OperationNotAllowedError(
                "scan() requires initialize() first (before every frame) in this session. "
                f"{safety.NO_COMMANDS_SENT}", session=self.session.snapshot())
        if t is not None:
            n_lines_check = lines if lines is not None else t.DEFAULT_LINES
            if n_lines_check > 0xFFFFFF:
                raise Of135iError(f"line count {n_lines_check} does not fit the 24-bit register")
        with self._operation("scan"):
            self._prepared_for_scan = False
            if t is not None:
                return self._scan_dual(t, frame=frame, lines=lines)
            return self._scan_plain(frame=frame, lines=lines)

    def _scan_plain(self, frame: int, lines: int | None) -> tuple[bytes, int]:
        # No homing move here. The vendor flow has none (protocol-notes.md
        # pass 14): positioning below is an absolute mode-0x18 feed that
        # works from wherever the previous frame left the carriage. The
        # home() call that used to sit here executed mode 0x30 -- the
        # scan pass -- with the previous frame's line count still in
        # regs 0x25-0x27, i.e. a full-length engine run before the
        # calibration reads (frame 2+ of a batch calibrated dark,
        # 2026-09-01).

        # ---- per-scan diagnostics: reset recording state (see diag.py) --
        self._diag_phase_seconds = {}
        self._diag_poll_timeouts = 0
        self._diag_cr_mismatches = 0
        self._diag_poll_timeout_details = []
        self._diag_warmup = None
        self._diag_park_waits = None
        started_utc = datetime.now(timezone.utc).isoformat()
        t_start = time.monotonic()

        # ---- dark pair (offset bracket, gain=0) ------------------------
        dark_a_raw = self._run_phase(tables.CAL_DARK_A)[0]
        dark_b_raw = self._run_phase(tables.CAL_DARK_B)[0]
        dark_a = np.frombuffer(dark_a_raw, dtype="<u2").reshape(-1, 3)
        dark_b = np.frombuffer(dark_b_raw, dtype="<u2").reshape(-1, 3)

        # ---- white line (gain=0) -> compute AFE gain -------------------
        gain_r, gain_g, gain_b = self._gain_with_warmup(
            tables.CAL_WHITE,
            lambda raw: np.frombuffer(raw, dtype="<u2").reshape(-1, 3),
        )
        log.info("computed gain codes: R=%#04x G=%#04x B=%#04x", gain_r, gain_g, gain_b)

        # ---- gain-check pair (offset bracket, gain=computed) -----------
        self._run_phase(
            tables.CAL_GAIN_CHECK_A,
            gain_r=bytes([gain_r]), gain_g=bytes([gain_g]), gain_b=bytes([gain_b]),
        )
        self._run_phase(tables.CAL_GAIN_CHECK_B)

        # ---- shading measurement (offset=computed final) ---------------
        off_r, off_g, off_b = calibrate.offset_codes(dark_a, dark_b)
        log.info("offset codes: R=%#06x G=%#06x B=%#06x", off_r, off_g, off_b)
        shading_meas_raw = self._run_phase(
            tables.CAL_SHADING_MEASURE,
            offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
            offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
            offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF]),
        )[0]
        shading_meas = np.frombuffer(shading_meas_raw, dtype="<u2").reshape(128, 3762, 3)
        shading = calibrate.shading_table(shading_meas)

        # ---- upload + verify (re-measure, re-upload once) ---------------
        self._run_phase(tables.CAL_SHADING_UPLOAD, shading_table=shading)

        # cal_shading_verify genuinely re-measures then re-uploads a
        # shading table computed from *that* read, so it can't be run
        # as a single _run_phase() call: shading_table2 depends on data
        # only available after the phase's own read completes. Run its
        # ops up to (not including) the re-upload's buffer-write
        # descriptor (split_at), compute, then run the rest patched.
        verify_phase = tables.CAL_SHADING_VERIFY
        self.session.phase = verify_phase.name
        _t0 = time.monotonic()
        verify_raw = self._exec_ops(verify_phase.ops[:verify_phase.split_at])[0]
        verify_meas = np.frombuffer(verify_raw, dtype="<u2").reshape(128, 3762, 3)
        shading2 = calibrate.shading_table2(verify_meas, shading_meas)
        remaining = verify_phase.patched(shading_table2=shading2)[verify_phase.split_at:]
        self._exec_ops(remaining)
        self._diag_phase_seconds["cal_shading_verify"] = (
            self._diag_phase_seconds.get("cal_shading_verify", 0.0) + (time.monotonic() - _t0)
        )

        # ---- position: relative feed from current carriage position ------
        feedl = tables.feedl_for_frame(frame)
        log.info("positioning to frame %d (FEEDL=%d)", frame, feedl)
        self._run_phase(
            tables.POSITION,
            feedl_hi=bytes([(feedl >> 16) & 0xFF]),
            feedl_mid=bytes([(feedl >> 8) & 0xFF]),
            feedl_lo=bytes([feedl & 0xFF]),
        )

        # ---- scan: 3 slope tables, line count, execute, image data ------
        n_lines = lines if lines is not None else tables.DEFAULT_LINES
        buffers = self._run_phase(
            tables.SCAN,
            lines_hi=bytes([(n_lines >> 8) & 0xFF]),
            lines_lo=bytes([n_lines & 0xFF]),
        )
        # buffers[:IMAGE_CHUNK_COUNT] are the image data (capture
        # fidelity: the first image descriptor carries wIndex=8,
        # subsequent ones 0 -- meaning unknown, baked into tables.py).
        # The trailing buffer (buffers[-1]) is a drain of unclear
        # purpose (not part of the documented image size); already
        # read verbatim above, discarded here.
        image = b"".join(buffers[:tables.IMAGE_CHUNK_COUNT])

        # ---- park ---------------------------------------------------------
        # PARK's own first op is the captured end-of-access control
        # write (cw wv=0x8d) -- no separate call needed here.
        self._park(tables, ir=False)

        warmup = self._diag_warmup or {}
        self.last_diag = {
            "dpi": 3600,
            "frame": frame,
            "dual": False,
            "lines": n_lines,
            "width": tables.IMAGE_WIDTH,
            "raw_bytes": len(image),
            "chunk_count": tables.IMAGE_CHUNK_COUNT,
            "dark_a_mean": [float(x) for x in dark_a.astype(np.float64).mean(axis=0)],
            "dark_b_mean": [float(x) for x in dark_b.astype(np.float64).mean(axis=0)],
            "white_mean": warmup.get("white_mean"),
            "white_max": warmup.get("white_max"),
            "gain_codes": [gain_r, gain_g, gain_b],
            "warmup_attempts": warmup.get("attempts"),
            "warmup_gain_history": warmup.get("gain_history"),
            "warmup_exhausted": warmup.get("exhausted"),
            "offset_codes": [off_r, off_g, off_b],
            "shading": _shading_stats(shading),
            "shading2": _shading_stats(shading2),
            "feedl": feedl,
            "phase_seconds": dict(self._diag_phase_seconds),
            "total_seconds": time.monotonic() - t_start,
            "poll_timeouts": self._diag_poll_timeouts,
            "cr_mismatches": self._diag_cr_mismatches,
            "poll_timeout_details": list(self._diag_poll_timeout_details),
            "park_mode": self.park_mode,
            "park_waits": dict(self._diag_park_waits) if self._diag_park_waits is not None else None,
            "started_utc": started_utc,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "session": self.session.snapshot(),
        }

        return image, tables.IMAGE_WIDTH

    # ----------------------------------------------------------- scan (IR)

    def _scan_dual(self, t, frame: int = 1, lines: int | None = None) -> tuple[bytes, int, dict]:
        """Dual-light (alternating IR/visible line) counterpart of scan(),
        driven by the phases of table module `t` -- tables_ir (3600 dpi,
        trace 04) or a tables_dpi<N> module (the 2026-09-02 captures);
        all compiled by tools/gen_tables.py's compile_dual() to one
        interface.

        Every calibration buffer in this mode is at the RAW sensor
        readout width (t.IMAGE_WIDTH: 5184 at 3600 dpi, not the windowed
        3762 the plain scan uses) and covers ALTERNATING lines -- even
        index = IR pass, odd index = visible pass (see ../cal-data/ir/
        ir-analysis.md and image.split_ir(), which applies the same
        convention to the final image). Measurements that feed a per-
        channel computation (gain, shading) are de-interleaved into
        their halves BEFORE computing. The shading table is uploaded to
        TWO scanner-RAM addresses with a CROSS-CONNECTION: address A
        (0x10014000) is computed from the ODD (visible) measurement
        lines but applied by the scanner hardware to ODD (visible)
        scan lines; address B (0x10034000) from EVEN (IR) measurement
        lines, applied to EVEN (IR) scan lines. The vendor uploads
        were analysed as "A from even, B from odd" (pass 18), which
        correctly identified the SOURCE data but incorrectly assumed
        same-source application; empirical testing (2026-09-03) proved
        the scanner applies A→odd and B→even, matching the pre-pass-18
        code that produced working images. See calibrate.SHADING2_
        TARGET_A/_B for the per-address white-uniformity targets.
        AFE gain/offset (regs 2-7) and FEEDL/line-count injections
        are the same single-register mechanism as the plain scan.

        The line count is programmed as whole image chunks: `lines`
        (alternating lines, default t.DEFAULT_LINES) rounded up to
        t.LINES_PER_CHUNK, and that many chunks are read.
        """
        # No homing move -- see scan().
        W = t.IMAGE_WIDTH
        dpi = t.DPI

        # ---- per-scan diagnostics: reset recording state (see diag.py) --
        self._diag_phase_seconds = {}
        self._diag_poll_timeouts = 0
        self._diag_cr_mismatches = 0
        self._diag_poll_timeout_details = []
        self._diag_warmup = None
        self._diag_park_waits = None
        started_utc = datetime.now(timezone.utc).isoformat()
        t_start = time.monotonic()

        # ---- dark pair (offset bracket, gain=0) ------------------------
        # Both reads feed offset_codes() (per-channel means, same as the
        # plain path); the doubled buffer size (alternating IR/visible)
        # doesn't matter -- flattened to (N, 3) either way, and the IR
        # and visible dark lines are both dark.
        dark_a_raw = self._run_phase(t.CAL_DARK_A)[0]
        dark_b_raw = self._run_phase(t.CAL_DARK_B)[0]
        dark_a = np.frombuffer(dark_a_raw, dtype="<u2").reshape(-1, 3)
        dark_b = np.frombuffer(dark_b_raw, dtype="<u2").reshape(-1, 3)

        # ---- white line (gain=0) -> compute AFE gain -------------------
        # 2 raw lines (alternating): index 0 = IR, index 1 = visible --
        # at the full sensor width (5184 px at every dpi below 7200, not
        # W), hence the reshape by line count rather than by W. Only ONE
        # set of AFE gain registers exists (regs 2/3/4, same as the plain
        # path) -- computed from the VISIBLE line only, since mixing in
        # the IR line's near-flat/bright broadcast values would badly
        # skew the 99.9th-percentile peak calculation.
        gain_r, gain_g, gain_b = self._gain_with_warmup(
            t.CAL_WHITE,
            lambda raw: np.frombuffer(raw, dtype="<u2").reshape(2, -1, 3)[1],
        )
        log.info("computed gain codes (%d dpi dual): R=%#04x G=%#04x B=%#04x", dpi, gain_r, gain_g, gain_b)

        # ---- gain-check pair (offset bracket, gain=computed) -----------
        self._run_phase(
            t.CAL_GAIN_CHECK_A,
            gain_r=bytes([gain_r]), gain_g=bytes([gain_g]), gain_b=bytes([gain_b]),
        )
        self._run_phase(t.CAL_GAIN_CHECK_B)

        # ---- shading measurement (offset=computed final) ---------------
        off_r, off_g, off_b = calibrate.offset_codes(dark_a, dark_b)
        log.info("offset codes (%d dpi dual): R=%#06x G=%#06x B=%#06x", dpi, off_r, off_g, off_b)
        shading_meas_raw = self._run_phase(
            t.CAL_SHADING_MEASURE,
            offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
            offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
            offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF]),
        )[0]
        shading_meas = np.frombuffer(shading_meas_raw, dtype="<u2").reshape(t.SHADING_LINES, W, 3)
        shading_meas_ir = shading_meas[0::2]       # even lines = IR pass
        shading_meas_vis = shading_meas[1::2]      # odd lines  = visible pass
        # Address A (0x10014000) is applied by the scanner to ODD
        # (visible) scan lines; address B (0x10034000) to EVEN (IR).
        # The vendor computes table A FROM even (IR) measurements and
        # table B FROM odd (visible) — a cross-connection (see pass 18
        # analysis, protocol-notes.md).  But the old code (pre pass-18)
        # proved empirically that address A corrects VISIBLE and B
        # corrects IR: uploading visible → A and IR → B produced images
        # with 170% dynamic range; the swapped assignment (pass 18)
        # produced flat noise.  So: visible measurement → table A,
        # IR measurement → table B.
        shading_a = calibrate.shading_table(shading_meas_vis, width=W)
        shading_b = calibrate.shading_table(shading_meas_ir, width=W)

        # ---- upload + verify (re-measure, re-upload once) ---------------
        self._run_phase(t.CAL_SHADING_UPLOAD, shading_table_a=shading_a, shading_table_b=shading_b)

        verify_phase = t.CAL_SHADING_VERIFY
        self.session.phase = verify_phase.name
        _t0 = time.monotonic()
        verify_raw = self._exec_ops(verify_phase.ops[:verify_phase.split_at])[0]
        verify_meas = np.frombuffer(verify_raw, dtype="<u2").reshape(t.SHADING_LINES, W, 3)
        # Same cross-connection: table A (visible lines) uses TARGET_A,
        # table B (IR lines) uses TARGET_B — matching the vendor's
        # per-address targets, with the line subsets swapped to the
        # empirically correct assignment.
        shading2_a = calibrate.shading_table2_dual(
            verify_meas[1::2], shading_meas_vis, width=W, target=calibrate.SHADING2_TARGET_A)
        shading2_b = calibrate.shading_table2_dual(
            verify_meas[0::2], shading_meas_ir, width=W, target=calibrate.SHADING2_TARGET_B)
        remaining = verify_phase.patched(
            shading_table2_a=shading2_a, shading_table2_b=shading2_b,
        )[verify_phase.split_at:]
        self._exec_ops(remaining)
        self._diag_phase_seconds["cal_shading_verify"] = (
            self._diag_phase_seconds.get("cal_shading_verify", 0.0) + (time.monotonic() - _t0)
        )

        # ---- position: relative feed from current carriage position ------
        # POSITION uses mode 0x18 (feed) which advances FEEDL steps from
        # wherever the carriage is. In same-DPI sessions the preceding
        # PARK always returns to the same offset, so the fixed FEEDL
        # lands at the correct frame. After a DPI change the PARK end
        # position differs (observed ~1060 rows / 7.5 mm shift at 3600
        # dpi between 2400 and 3600); re-loading the magazine resets
        # the carriage to the load-position reference. A proper homing
        # command would fix this, but requires hardware testing.
        feedl = t.feedl_for_frame(frame)
        log.info("positioning to frame %d (FEEDL=%d, %d dpi dual)", frame, feedl, dpi)
        self._run_phase(
            t.POSITION,
            feedl_hi=bytes([(feedl >> 16) & 0xFF]),
            feedl_mid=bytes([(feedl >> 8) & 0xFF]),
            feedl_lo=bytes([feedl & 0xFF]),
        )

        # ---- scan: 3 slope tables, line count, execute, image data ------
        if lines is None:
            n_chunks = t.IMAGE_CHUNK_COUNT
            n_lines = t.DEFAULT_LINES
        else:
            n_chunks = max(1, -(-lines // t.LINES_PER_CHUNK))
            n_lines = n_chunks * t.LINES_PER_CHUNK
        if n_lines > 0xFFFFFF:
            raise Of135iError(f"line count {n_lines} does not fit the 24-bit register")
        buffers = self._run_phase(
            t.scan_phase(n_chunks),
            lines_top=bytes([(n_lines >> 16) & 0xFF]),
            lines_hi=bytes([(n_lines >> 8) & 0xFF]),
            lines_lo=bytes([n_lines & 0xFF]),
        )
        # buffers[:n_chunks] are the image data. tables_ir's tail issues
        # a further descriptor that the vendor cancelled with no data
        # (ir-analysis.md); it is not among the collected buffers at all
        # (_exec_ops only appends a buffer on flush, and a cancelled
        # read never gets any bi data to flush).
        image = b"".join(buffers[:n_chunks])

        # ---- park ---------------------------------------------------------
        self._park(t, ir=True)

        warmup = self._diag_warmup or {}
        self.last_diag = {
            "dpi": dpi,
            "frame": frame,
            "dual": True,
            "lines": n_lines,
            "width": W,
            "raw_bytes": len(image),
            "chunk_count": n_chunks,
            "dark_a_mean": [float(x) for x in dark_a.astype(np.float64).mean(axis=0)],
            "dark_b_mean": [float(x) for x in dark_b.astype(np.float64).mean(axis=0)],
            "white_mean": warmup.get("white_mean"),
            "white_max": warmup.get("white_max"),
            "gain_codes": [gain_r, gain_g, gain_b],
            "warmup_attempts": warmup.get("attempts"),
            "warmup_gain_history": warmup.get("gain_history"),
            "warmup_exhausted": warmup.get("exhausted"),
            "offset_codes": [off_r, off_g, off_b],
            "shading": {"a": _shading_stats(shading_a), "b": _shading_stats(shading_b)},
            "shading2": {"a": _shading_stats(shading2_a), "b": _shading_stats(shading2_b)},
            "feedl": feedl,
            "phase_seconds": dict(self._diag_phase_seconds),
            "total_seconds": time.monotonic() - t_start,
            "poll_timeouts": self._diag_poll_timeouts,
            "cr_mismatches": self._diag_cr_mismatches,
            "poll_timeout_details": list(self._diag_poll_timeout_details),
            "park_mode": self.park_mode,
            "park_waits": dict(self._diag_park_waits) if self._diag_park_waits is not None else None,
            "started_utc": started_utc,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "session": self.session.snapshot(),
        }

        return image, W, {"width": W, "alternating": True, "dpi": dpi}
