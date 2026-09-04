"""Hardware-safety layer for the of135i driver.

This module is the ONE authoritative place where the driver decides
whether a USB write may be sent to the scanner. Everything else
(cli.py, device.py, tools/, future frontends) consults it; nothing
re-implements its rules.

Why it exists (docs/hardware-safety.md, docs/test-log.md Test 11d):
an aborted session left the scan engine running (reg 0x01 = 0x23).
A new session was started on top of that state; its register writes
and execute pulse hit a running engine, produced a motor event and a
firmware hang, and only a power cycle recovered the scanner. The
scanner is a single, irreplaceable unit, so the driver now fails
CLOSED: it writes nothing unless it has positively verified that the
scanner is in one of two explicitly known start states.

Three mechanisms, all in this module:

1. ``HardwareSession`` -- the per-process session state machine.
   A session starts UNVERIFIED (no writes allowed). ``verify_start_
   state()`` reads register 0x01 exactly once, strictly, and moves the
   session to ARMED (0x22, idle-homed: normal operations allowed),
   COLD (0x00, never homed: only the cold-init path allowed) or
   REFUSED (anything else, including a read failure: nothing allowed,
   ever, in this process). Any exception that escapes a hardware
   operation -- including KeyboardInterrupt -- moves the session to
   FAILED, which is equally final. READONLY sessions (``doctor``,
   ``status``) can never be armed at all.

2. ``GuardedDevice`` -- a proxy that wraps the pyusb device object
   inside every ``UsbIo``. Every control transfer is classified by
   its direction bit; every OUT transfer (control OUT and bulk OUT)
   asks the session for permission BEFORE it is issued and is counted
   afterwards, execute pulses (register 0x0f = 0x01) included. A
   transfer that reports fewer bytes than requested (a SHORT OUT
   transfer) is not a completed write: it fails the session, because
   part of the payload may have reached the scanner. pyusb's own
   state-changing standard requests (``clear_halt``, ``reset``,
   ``set_configuration``, ...) are blocked on the proxy so that no
   "recovery" can slip past it. The proxy keeps the only reference to
   the raw handle; the driver stores no other. The single exception
   is ``UsbIo.open()``, which issues the verified open sequence
   (kernel-driver detach, SET_CONFIGURATION) on its local raw handle
   -- and only AFTER ``verify_start_state()`` has accepted the start
   state through the proxy.

3. ``ProcessLock`` -- an ``flock`` on a well-known lock file, taken by
   ``UsbIo.open()`` before the first USB access and released by
   ``close()`` or, if the process dies, by the kernel. A second
   process (a writing session OR a read-only ``doctor``) is refused
   with ``ScannerBusyError`` before it touches the device.

What this module deliberately does NOT do: it never tries to bring the
scanner back to a known state. There is no PARK, home, eject or
initialize on the failure path -- the only recovery from an unknown
state is a power cycle, performed by a person.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum

from .errors import Of135iError

log = logging.getLogger("of135i")

# ----------------------------------------------------------- start states

#: Register 0x01 value of an idle, homed scanner: the vendor's own
#: "ready" state after PARK / after its power-on table (bit 0x20 = idle,
#: bit 0x02 = the base-table value). Every successful scan to date
#: started from this value.
REG01_IDLE = 0x22
#: Register 0x01 value of a freshly power-cycled scanner that has never
#: been homed. Only ``Scanner.cold_init()`` may write to it.
REG01_COLD = 0x00

POWER_CYCLE_INSTRUCTION = (
    "Power the scanner OFF, wait until its lights go out and it has "
    "disappeared from the USB bus (lsusb shows no 07b3:1436), then "
    "power it ON again. Restarting this program is NOT sufficient."
)
NO_COMMANDS_SENT = "No commands were sent to the scanner after the failed safety check."
NO_RECOVERY_ATTEMPTED = (
    "No recovery commands (park, home, eject, initialize) were sent: the "
    "hardware state is unknown and only a power cycle can reset it."
)


class StartState(str, Enum):
    """Classification of a register 0x01 value read at session start."""

    IDLE = "idle-homed"
    COLD = "cold-never-homed"
    UNSAFE = "unsafe"


def classify_reg01(value: int | None) -> StartState:
    """The one and only mapping from a register 0x01 value to a start
    state. ``None`` (value could not be read) is UNSAFE."""
    if value == REG01_IDLE:
        return StartState.IDLE
    if value == REG01_COLD:
        return StartState.COLD
    return StartState.UNSAFE


def has_execute_pulse(data: bytes) -> bool:
    """True if a register-batch payload (cw wValue=0x83: (reg, val)
    byte pairs) contains the pair (0x0f, 0x01) -- the GL126 "GO"."""
    return any(data[i] == 0x0F and data[i + 1] == 0x01 for i in range(0, len(data) - 1, 2))


# --------------------------------------------------------------- exceptions


class SafetyError(Of135iError):
    """Base class for every refusal issued by the safety layer.

    ``observed`` is the register 0x01 value the decision was based on
    (None if it could not be read), ``session`` a snapshot of the
    session at the time of the refusal (see HardwareSession.snapshot).
    """

    def __init__(self, message: str, *, observed: int | None = None,
                 session: dict | None = None):
        super().__init__(message)
        self.observed = observed
        self.session = session


class UnsafeStartStateError(SafetyError):
    """The scanner is not in an explicitly known start state (or its
    state could not be read). Nothing was written."""


class SessionFailedError(SafetyError):
    """A write was attempted after the session had already failed or
    been refused. Nothing was written by this attempt."""


class ReadOnlySessionError(SafetyError):
    """A write was attempted in a read-only session (doctor/status)."""


class OperationNotAllowedError(SafetyError):
    """The requested operation is not permitted in the current session
    state (e.g. scan() on a cold scanner, cold_init() on an idle one,
    or a low-level op runner used outside an operation)."""


class ScannerBusyError(SafetyError):
    """Another process holds the scanner lock."""


class LoadIncompleteError(SafetyError):
    """The magazine load sequence ran to its end but the scanner did not
    report the captured completion value (status word). The transport
    state is unknown and the magazine may not be latched; the session
    is FAILED and a power cycle is required. ``status_word`` is what
    was read, ``expected`` the vendor capture's completion value."""

    def __init__(self, message: str, *, status_word: int | None, expected: int, **kw):
        super().__init__(message, **kw)
        self.status_word = status_word
        self.expected = expected


class ShortTransferError(SafetyError):
    """An OUT transfer (control OUT or bulk OUT) completed with fewer
    bytes than were requested. Some or all of the payload may have
    reached the scanner: the hardware outcome is unknown, the session
    is FAILED, and nothing further is sent. ``expected`` / ``completed``
    are the requested and reported byte counts."""

    def __init__(self, message: str, *, expected: int, completed: int,
                 pulse: bool = False, **kw):
        super().__init__(message, **kw)
        self.expected = expected
        self.completed = completed
        self.pulse = pulse


# ------------------------------------------------------------------ session


class SessionState(str, Enum):
    UNVERIFIED = "unverified"   # no start-state check yet: no writes
    ARMED = "armed"             # 0x22 verified: operations allowed
    COLD = "cold"               # 0x00 verified: only cold_init allowed
    REFUSED = "refused"         # start state unsafe/unreadable: no writes, final
    FAILED = "failed"           # an operation failed/was interrupted: no writes, final
    READONLY = "readonly"       # doctor/status: never any writes


_FINAL_STATES = (SessionState.REFUSED, SessionState.FAILED)


class HardwareSession:
    """Per-process hardware session: start-state verdict, write/execute
    bookkeeping, and the failure record. One instance per ``UsbIo``.

    The operation stack (``operations``) is what makes a multi-frame
    batch ONE session: the start-state check runs once, before the
    first operation's first write, and transient engine states inside
    the session (0x02/0x03/0x23 between phases) are never re-checked.
    """

    def __init__(self, readonly: bool = False):
        self.state = SessionState.READONLY if readonly else SessionState.UNVERIFIED
        self.readonly = readonly
        self.start_reg01: int | None = None
        self.start_state: StartState | None = None
        self.verified_utc: str | None = None
        self.cold_init_done = False
        self.writes = 0                  # completed OUT transfers
        self.writes_attempted = 0        # OUT transfers issued (incl. failed)
        self.execute_pulses = 0          # completed batches carrying 0x0f=0x01
        self.execute_pulses_attempted = 0
        self.last_write_utc: str | None = None
        self.phase: str | None = None
        self.operations: list[str] = []
        self.failure: dict | None = None
        self.refusal: dict | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------ verdicts

    def arm(self, value: int) -> None:
        with self._lock:
            self._require_unverified()
            self.start_reg01 = value
            self.start_state = StartState.IDLE
            self.verified_utc = _now()
            self.state = SessionState.ARMED

    def mark_cold(self, value: int) -> None:
        with self._lock:
            self._require_unverified()
            self.start_reg01 = value
            self.start_state = StartState.COLD
            self.verified_utc = _now()
            self.state = SessionState.COLD

    def arm_after_cold_init(self, value: int) -> None:
        """cold_init() completed and the scanner reads idle: allow the
        normal operations from here on. Only valid from COLD."""
        with self._lock:
            if self.state is not SessionState.COLD:
                raise OperationNotAllowedError(
                    f"arm_after_cold_init in state {self.state.value}", session=self.snapshot())
            self.cold_init_done = True
            self.state = SessionState.ARMED

    def refuse(self, reason: str, observed: int | None = None) -> None:
        """Final: the start state is unsafe or unreadable."""
        with self._lock:
            if self.state in _FINAL_STATES:
                return
            self.refusal = {"reason": reason, "observed": _hex(observed), "utc": _now(),
                            "writes_before": self.writes}
            self.start_reg01 = observed
            self.start_state = StartState.UNSAFE
            self.state = SessionState.REFUSED

    def fail(self, exc: BaseException, where: str | None = None,
             extra: dict | None = None) -> None:
        """Final: an operation failed or was interrupted. Records the
        first failure only; later ones are consequences. ``extra`` is
        merged into the failure record (e.g. the short-transfer detail
        GuardedDevice supplies)."""
        with self._lock:
            if self.failure is not None:
                return
            self.failure = {
                "operation": self.operations[-1] if self.operations else None,
                "operations": list(self.operations),
                "phase": self.phase,
                "where": where,
                "exception": f"{type(exc).__name__}: {exc}",
                "writes": self.writes,
                "writes_attempted": self.writes_attempted,
                "execute_pulses": self.execute_pulses,
                "execute_pulses_attempted": self.execute_pulses_attempted,
                "utc": _now(),
            }
            if extra:
                self.failure.update(extra)
            if self.state is not SessionState.REFUSED:
                self.state = SessionState.FAILED
            log.error(
                "hardware session FAILED in operation %s, phase %s, after %d writes "
                "(%d execute pulses): %s. Hardware state is now UNKNOWN. %s %s",
                self.failure["operation"], self.phase, self.writes, self.execute_pulses,
                self.failure["exception"], NO_RECOVERY_ATTEMPTED, POWER_CYCLE_INSTRUCTION,
            )

    def _require_unverified(self) -> None:
        if self.state is not SessionState.UNVERIFIED:
            raise OperationNotAllowedError(
                f"start state already decided ({self.state.value})", session=self.snapshot())

    # ------------------------------------------------------- write gate

    def write_allowed(self) -> None:
        """Raise unless an OUT transfer may be issued right now. Called
        by GuardedDevice BEFORE every OUT transfer."""
        with self._lock:
            st = self.state
            if st is SessionState.ARMED:
                return
            if st is SessionState.COLD and "cold_init" in self.operations:
                return
            snap = self.snapshot()
            if st is SessionState.READONLY:
                raise ReadOnlySessionError(
                    "USB write attempted in a read-only session (doctor/status). "
                    "Refused; nothing was written.", session=snap)
            if st is SessionState.COLD:
                raise OperationNotAllowedError(
                    "the scanner is in the cold start state (reg 0x01 = 0x00): only the "
                    "cold-init path may write to it. Refused; nothing was written.",
                    observed=self.start_reg01, session=snap)
            if st is SessionState.UNVERIFIED:
                raise UnsafeStartStateError(
                    "USB write attempted before the start state was verified. "
                    "Refused; nothing was written.", session=snap)
            # REFUSED / FAILED
            raise SessionFailedError(
                f"USB write refused: the hardware session is {st.value} "
                f"({self._final_reason()}). {NO_RECOVERY_ATTEMPTED} {POWER_CYCLE_INSTRUCTION}",
                observed=self.start_reg01, session=snap)

    def write_allowed_or_final(self) -> None:
        """Raise SessionFailedError if the session is already final
        (refused/failed); otherwise return without deciding anything.
        Used by public methods before their own parameter checks so a
        dead session is always reported as such."""
        with self._lock:
            if self.state in _FINAL_STATES:
                self.write_allowed()

    def _final_reason(self) -> str:
        if self.failure:
            return (f"operation {self.failure['operation']} failed in phase "
                    f"{self.failure['phase']} after {self.failure['writes']} writes: "
                    f"{self.failure['exception']}")
        if self.refusal:
            return self.refusal["reason"]
        return "unknown"

    def note_write_attempt(self, kind: str, wv: int, data: bytes) -> bool:
        """Bookkeeping right before an OUT transfer. Returns whether the
        payload carries an execute pulse (the caller reports completion
        via note_write_done with the same flag)."""
        pulse = kind == "ctrl" and wv == 0x0083 and has_execute_pulse(data)
        with self._lock:
            self.writes_attempted += 1
            if pulse:
                self.execute_pulses_attempted += 1
        return pulse

    def note_write_done(self, pulse: bool) -> None:
        with self._lock:
            self.writes += 1
            if pulse:
                self.execute_pulses += 1
            self.last_write_utc = _now()

    # ------------------------------------------------------- operations

    @contextmanager
    def operation(self, name: str):
        """Mark `name` as the active hardware operation. Any exception
        (KeyboardInterrupt included) escaping the block marks the
        session FAILED -- no recovery is attempted -- and propagates."""
        with self._lock:
            if self.state in _FINAL_STATES:
                self.write_allowed()   # raises SessionFailedError with the reason
            self.operations.append(name)
        try:
            yield self
        except BaseException as e:
            self.fail(e, where=f"operation {name}")
            raise
        finally:
            with self._lock:
                if self.operations and self.operations[-1] == name:
                    self.operations.pop()

    # ------------------------------------------------------- reporting

    def snapshot(self) -> dict:
        """JSON-serializable summary for diagnostics and error reports."""
        with self._lock:
            return {
                "state": self.state.value,
                "readonly": self.readonly,
                "start_reg01": _hex(self.start_reg01),
                "start_state": self.start_state.value if self.start_state else None,
                "verified_utc": self.verified_utc,
                "cold_init_done": self.cold_init_done,
                "writes": self.writes,
                "writes_attempted": self.writes_attempted,
                "execute_pulses": self.execute_pulses,
                "execute_pulses_attempted": self.execute_pulses_attempted,
                "last_write_utc": self.last_write_utc,
                "phase": self.phase,
                "operations": list(self.operations),
                "failure": dict(self.failure) if self.failure else None,
                "refusal": dict(self.refusal) if self.refusal else None,
            }

    def describe_failure(self) -> str:
        """Operator-facing text for a failed/refused session."""
        snap = self.snapshot()
        if self.refusal:
            return (
                f"Refused before any write: {self.refusal['reason']} "
                f"(reg 0x01 = {self.refusal['observed']}). {NO_COMMANDS_SENT} "
                f"{POWER_CYCLE_INSTRUCTION}"
            )
        if self.failure:
            f = self.failure
            sent = (f"{f['writes']} write(s) and {f['execute_pulses']} execute pulse(s) had "
                    f"been sent by this session" if f["writes"] else
                    "no write had been sent by this session")
            short = f.get("short_transfer")
            ambiguity = ""
            if short:
                ambiguity = (
                    f" The last transfer was SHORT ({short['kind']} OUT, {short['completed']} of "
                    f"{short['expected']} bytes reported"
                    f"{', containing an execute pulse' if short['pulse'] else ''}): some or all "
                    f"of its bytes may have reached the scanner.")
            return (
                f"Operation {f['operation']} failed in phase {f['phase']}: "
                f"{f['exception']}. {sent}; the hardware state is now UNKNOWN.{ambiguity} "
                f"{NO_RECOVERY_ATTEMPTED} {POWER_CYCLE_INSTRUCTION}"
            )
        return f"session state {snap['state']}"


# ----------------------------------------------------------- guarded device


class GuardedDevice:
    """Proxy around a pyusb ``usb.core.Device`` (or any duck type with
    ctrl_transfer/read/write) that routes every OUT transfer through
    ``HardwareSession.write_allowed()`` and counts it.

    Direction is decided from the wire, not from the caller: bit 7 of
    bmRequestType for control transfers, the endpoint for bulk. IN
    transfers are passed through untouched (their errors are the
    operation's business, see HardwareSession.operation). A failing
    OUT transfer marks the session FAILED immediately -- a write whose
    outcome is unknown IS an unknown hardware state. So does a SHORT
    OUT transfer: pyusb reports the number of bytes transferred, and
    fewer than requested means the scanner may have received part of
    the payload (part of a register batch, part of a buffer) -- the
    session fails, the transfer is counted as attempted but not
    completed, and ShortTransferError is raised. Nothing is retried.

    The raw handle is kept only here (name-mangled ``__raw``), not on
    ``UsbIo``: driver and tool code has no attribute to reach the bus
    around the proxy. The one legitimate raw use -- the verified open
    sequence (kernel-driver detach + set_configuration), issued by
    ``UsbIo.open()`` AFTER the start state has been verified through
    this proxy -- runs on the local variable in ``open()`` and is never
    stored. ``dispose()`` releases the handle on close.
    """

    #: pyusb methods that issue standard control requests or reset the
    #: device. None of them is part of any verified sequence, and each
    #: is exactly the kind of "recovery" the safety model forbids. The
    #: open sequence uses two of them (detach_kernel_driver,
    #: set_configuration) on the raw device, only after verification.
    BLOCKED = frozenset({
        "clear_halt", "reset", "set_configuration", "set_interface_altsetting",
        "attach_kernel_driver", "detach_kernel_driver",
    })

    def __init__(self, raw, session: HardwareSession):
        object.__setattr__(self, "_GuardedDevice__raw", raw)
        object.__setattr__(self, "_session", session)

    # pyusb signature, kept positional-compatible with every call site.
    def ctrl_transfer(self, bmRequestType, bRequest, wValue=0, wIndex=0,
                      data_or_wLength=None, timeout=None):
        extra = {} if timeout is None else {"timeout": timeout}
        if bmRequestType & 0x80:
            return self.__raw.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex,
                                            data_or_wLength, **extra)
        session = self._session
        session.write_allowed()
        data = bytes(data_or_wLength) if data_or_wLength is not None else b""
        pulse = session.note_write_attempt("ctrl", wValue, data)
        where = f"control OUT wValue={wValue:#06x} wIndex={wIndex:#06x}"
        try:
            n = self.__raw.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex,
                                         data_or_wLength, **extra)
        except BaseException as e:
            session.fail(e, where=where)
            raise
        self._check_complete("control", where, len(data), n, pulse)
        session.note_write_done(pulse)
        return n

    def write(self, endpoint, data, timeout=None):
        session = self._session
        session.write_allowed()
        data = bytes(data)
        pulse = session.note_write_attempt("bulk", 0, b"")
        extra = {} if timeout is None else {"timeout": timeout}
        where = f"bulk OUT ep={endpoint:#04x}"
        try:
            n = self.__raw.write(endpoint, data, **extra)
        except BaseException as e:
            session.fail(e, where=where)
            raise
        self._check_complete("bulk", where, len(data), n, pulse)
        session.note_write_done(pulse)
        return n

    def _check_complete(self, kind: str, where: str, expected: int, n, pulse: bool) -> None:
        """A non-exception return is complete only if it reports exactly
        the requested length. ``expected`` is the actual payload length
        (0 for the verified zero-length control requests, which pyusb
        reports as 0). Anything else fails the session with the
        ambiguity on record and raises ShortTransferError."""
        try:
            completed = int(n)
        except (TypeError, ValueError):
            completed = -1
        if completed == expected:
            return
        session = self._session
        msg = (
            f"short {kind} OUT transfer ({where}): {completed} of {expected} bytes "
            f"reported{' -- the payload carried an execute pulse' if pulse else ''}. "
            f"Some or all of the bytes may have reached the scanner; the hardware state "
            f"is UNKNOWN. No further command was sent. {NO_RECOVERY_ATTEMPTED} "
            f"{POWER_CYCLE_INSTRUCTION}"
        )
        exc = ShortTransferError(msg, expected=expected, completed=completed, pulse=pulse,
                                 observed=session.start_reg01)
        session.fail(exc, where=where, extra={"short_transfer": {
            "kind": kind, "where": where, "expected": expected, "completed": completed,
            "pulse": pulse}})
        exc.session = session.snapshot()
        raise exc

    def read(self, endpoint, size_or_buffer, timeout=None):
        extra = {} if timeout is None else {"timeout": timeout}
        return self.__raw.read(endpoint, size_or_buffer, **extra)

    def dispose(self) -> None:
        """Release the pyusb handle (usb.util.dispose_resources): a
        handle release, never a transfer or a standard request."""
        import usb.util
        usb.util.dispose_resources(self.__raw)

    def __getattr__(self, name):
        if name in self.BLOCKED:
            raise SafetyError(
                f"usb device method {name}() is blocked by the safety layer: it issues a "
                f"standard request that is not part of any verified sequence.",
                session=self._session.snapshot())
        return getattr(self.__raw, name)

    def __setattr__(self, name, value):
        raise AttributeError("GuardedDevice is read-only")

    def __repr__(self) -> str:
        return f"GuardedDevice({self.__raw!r}, state={self._session.state.value})"


# ------------------------------------------------------------ start check


def verify_start_state(io) -> StartState:
    """Read register 0x01 once, strictly, and decide the session's start
    state. `io` is a UsbIo (or duck type) with ``read_reg(reg, strict=)``
    and a ``session``.

    Fails closed: any exception from the read (USB error, timeout,
    short or malformed reply, even a missing method on a duck type)
    refuses the session and raises UnsafeStartStateError. Calling it
    again after a verdict just returns the verdict (or re-raises the
    refusal) -- the check is performed at most once per session.
    """
    session: HardwareSession = io.session
    if session.state is SessionState.READONLY:
        raise ReadOnlySessionError("a read-only session cannot be armed", session=session.snapshot())
    if session.state in (SessionState.ARMED, SessionState.COLD):
        return session.start_state
    if session.state in _FINAL_STATES:
        session.write_allowed()   # raises with the recorded reason
    try:
        value = io.read_reg(0x01, strict=True)
    except KeyboardInterrupt:
        session.refuse("interrupted while reading the start state", None)
        raise
    except Exception as e:
        reason = f"could not read reg 0x01 ({type(e).__name__}: {e})"
        session.refuse(reason, None)
        raise UnsafeStartStateError(
            f"start-state check failed: {reason}. {NO_COMMANDS_SENT} {POWER_CYCLE_INSTRUCTION}",
            observed=None, session=session.snapshot()) from e
    if not isinstance(value, int):
        reason = f"malformed reg 0x01 reply {value!r}"
        session.refuse(reason, None)
        raise UnsafeStartStateError(
            f"start-state check failed: {reason}. {NO_COMMANDS_SENT} {POWER_CYCLE_INSTRUCTION}",
            observed=None, session=session.snapshot())
    verdict = classify_reg01(value)
    if verdict is StartState.IDLE:
        session.arm(value)
        log.info("start state: reg 0x01 = %#04x (idle-homed) -- session armed", value)
    elif verdict is StartState.COLD:
        session.mark_cold(value)
        log.info("start state: reg 0x01 = %#04x (cold, never homed) -- only cold_init permitted", value)
    else:
        reason = (f"reg 0x01 = {value:#04x} is neither idle-homed ({REG01_IDLE:#04x}) nor "
                  f"cold ({REG01_COLD:#04x}); typically an interrupted session left the scan "
                  f"engine running")
        session.refuse(reason, value)
        raise UnsafeStartStateError(
            f"unsafe start state: {reason}. {NO_COMMANDS_SENT} {POWER_CYCLE_INSTRUCTION}",
            observed=value, session=session.snapshot())
    return verdict


# ------------------------------------------------------------ process lock

DEFAULT_LOCK_PATH = "/tmp/of135i-07b3-1436.lock"
LOCK_PATH_ENV = "OF135I_LOCK_FILE"


def lock_path() -> str:
    return os.environ.get(LOCK_PATH_ENV, DEFAULT_LOCK_PATH)


class ProcessLock:
    """Exclusive, non-blocking ``flock`` on the scanner lock file.

    Held for the whole life of a ``UsbIo`` (writing or read-only): the
    lock is what guarantees that two processes never talk to the
    scanner at the same time. The kernel drops it when the holder
    exits, so a crashed process never leaves a stale lock; a hung one
    keeps it, which is the correct outcome.

    Releasing the lock says nothing about the scanner's physical
    state -- a failed session releases it too.
    """

    def __init__(self, path: str | None = None):
        self.path = path or lock_path()
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        except PermissionError:
            fd = os.open(self.path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as e:
            holder = _read_holder(fd)
            os.close(fd)
            raise ScannerBusyError(
                f"another of135i process holds the scanner lock {self.path}"
                f"{' (' + holder + ')' if holder else ''}. Wait for it to finish; "
                f"nothing was sent to the scanner.") from e
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"pid {os.getpid()} since {_now()}\n".encode())
        except OSError:
            pass   # read-only fallback: lock held, holder info unavailable

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _read_holder(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 128).decode(errors="replace").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hex(value: int | None) -> str | None:
    return None if value is None else f"0x{value:02x}"


__all__ = [
    "REG01_IDLE", "REG01_COLD", "POWER_CYCLE_INSTRUCTION", "NO_COMMANDS_SENT",
    "NO_RECOVERY_ATTEMPTED", "StartState", "classify_reg01", "has_execute_pulse",
    "SafetyError", "UnsafeStartStateError", "SessionFailedError", "ReadOnlySessionError",
    "OperationNotAllowedError", "ScannerBusyError", "SessionState", "HardwareSession",
    "GuardedDevice", "verify_start_state", "ProcessLock", "lock_path", "DEFAULT_LOCK_PATH",
    "LOCK_PATH_ENV",
]
