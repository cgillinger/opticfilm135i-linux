"""USB transport primitives for the Plustek OpticFilm 135i (GL126).

Implements the Genesys vendor protocol over control endpoint 0, as
documented in protocol-notes.md (transport table + pass 4/5). This
module knows nothing about scanner semantics (registers, tables,
sequences) — it only speaks the wire protocol.

Hardware safety (see safety.py / docs/hardware-safety.md): every
``UsbIo`` carries a ``HardwareSession`` and exposes the pyusb device
only through a ``GuardedDevice`` proxy, so every OUT transfer -- from
this module's own helpers or from device.py's verbatim op executor --
is gated and counted in one place, and a short OUT transfer fails the
session. ``open()`` takes the process lock before the first USB access
and, for a writing session, verifies the start state (one control-IN
read of reg 0x01) BEFORE the only state-changing standard requests the
driver ever issues (kernel-driver detach, SET_CONFIGURATION);
``open(readonly=True)`` (doctor/status) never issues those at all and
can never be armed.
"""

from __future__ import annotations

import logging
import struct
from typing import Iterable, Sequence, Tuple

import usb.core

from . import safety
from .errors import Of135iError

log = logging.getLogger("of135i")

VID = 0x07B3
PID = 0x1436

EP_BULK_IN = 0x81
EP_BULK_OUT = 0x02
EP_INT_IN = 0x83


class InterruptReadError(Of135iError):
    """A read of the interrupt endpoint (EP 0x83) failed with a USB error
    other than a timeout or the overflow case: the error is reported as
    a driver error so every caller (status, doctor, watch, the load
    tool) takes its normal error path instead of an unhandled pyusb
    exception. Reads change no scanner state; nothing is hidden and
    nothing is retried."""


class InterruptOverflowError(Of135iError):
    """The interrupt endpoint (EP 0x83, wMaxPacketSize 1) answered a
    read with more data than its packet size (usbfs EOVERFLOW, errno
    75), so no event can be read from it. Observed 2026-09-05 after
    magazine loads run by this driver while it never read the endpoint
    (Tests 17-19: present from the first doctor after the load, through
    eject and idle), never before a load and never after the vendor
    application's own load. Test 20 settled it: a power cycle clears
    the state, and draining the endpoint during the load (as the vendor
    does continuously; tools/load_magazine.py now does after the jog,
    the reinsert and the load) prevents it. Harmless for scanning; it
    only blocks the button/sensor event reads (status, watch, doctor)."""

    def __init__(self, msg: str | None = None):
        super().__init__(msg or (
            "interrupt endpoint (EP 0x83) overflow: the scanner answers with more than its "
            "1-byte packet size, so no button/sensor event can be read (an event backlog "
            "from a load that did not drain the endpoint; a power cycle clears it)"))

_WRITE_CHUNK = 64          # bytes = 32 (reg, val) pairs
_BUF_CHUNK = 16384         # bulk transfer chunk size


def _configure_for_writing(dev: "usb.core.Device") -> None:
    """The verified open sequence for a writing session: detach a bound
    kernel driver (if any) and SET_CONFIGURATION. Both are standard
    requests that change device state, so UsbIo.open() calls this on
    the raw handle ONLY after safety.verify_start_state() has accepted
    the start state; nowhere else in the driver may they be issued
    (GuardedDevice blocks them)."""
    try:
        if dev.is_kernel_driver_active(0):
            log.info("detaching kernel driver from interface 0")
            dev.detach_kernel_driver(0)
    except NotImplementedError:
        # Platforms without kernel-driver introspection (e.g. Windows).
        pass
    dev.set_configuration()


class UsbIo:
    """Thin wrapper around a pyusb device handle for the 07b3:1436 scanner.

    ``dev`` is a safety.GuardedDevice around the real handle; ``session``
    the safety.HardwareSession that decides whether writes may go out.
    The raw handle is not stored on this object (see GuardedDevice).
    """

    def __init__(self, dev: "usb.core.Device", readonly: bool = False,
                 lock: "safety.ProcessLock | None" = None):
        self.session = safety.HardwareSession(readonly=readonly)
        self.dev = safety.GuardedDevice(dev, self.session)
        self._lock = lock

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def open(cls, readonly: bool = False) -> "UsbIo":
        """Find and claim the scanner on the host USB bus.

        Order, for a writing session (docs/hardware-safety.md):

        1. take the process lock (safety.ProcessLock -- raises
           ScannerBusyError if another of135i process, writing or
           read-only, holds the scanner);
        2. find the device (usb.core.find: descriptor lookup only);
        3. construct this UsbIo with the ONE HardwareSession that owns
           the whole session, and the GuardedDevice proxy around the
           handle;
        4. verify the start state through that proxy: one strict
           control-IN read of reg 0x01 (a device-recipient vendor
           request; pyusb neither configures the device nor claims
           the interface for it) -- with NO set_configuration, NO
           kernel-driver detach, NO reset/clear_halt/altsetting change
           before it. The kernel configured the device at enumeration,
           which is all a control-IN on endpoint 0 needs; this is the
           same read path ``doctor``/``status`` use;
        5. if the state is anything but 0x22/0x00, or the read fails,
           times out, is short or malformed, or is interrupted: the
           session is REFUSED, the handle is released and the lock
           dropped, and UnsafeStartStateError (or KeyboardInterrupt)
           propagates. Zero vendor OUT transfers and zero pyusb
           state-changing calls have happened;
        6. only after the verdict is IDLE or COLD: the verified open
           sequence on the raw handle -- detach the kernel driver if
           one is bound, then set_configuration. A failure here marks
           the SAME session FAILED (the device may now be half
           configured: power cycle), releases everything and raises
           SessionFailedError.

        The session object and its verdict survive step 6 unchanged;
        Scanner._operation()'s own check_start_state() then just
        returns the recorded verdict without a second read.

        If a kernel driver were bound and the device-recipient read
        failed because of it, step 5 refuses: the driver never
        detaches first merely to make the check possible.

        readonly=True (doctor/status): steps 4-6 are skipped entirely;
        the session can never be armed, and no standard request is
        ever issued. Raises Of135iError with likely-cause hints if the
        device is not present (commonly: still attached to a VM, or
        in USB standby -- see protocol-notes.md pass 4).
        """
        lock = safety.ProcessLock()
        lock.acquire()
        io: "UsbIo | None" = None
        try:
            dev = usb.core.find(idVendor=VID, idProduct=PID)
            if dev is None:
                raise Of135iError(
                    "Scanner 07b3:1436 not found on the host USB bus. "
                    "Likely causes: (1) the device is still attached to a VM "
                    "(release it via the hypervisor's USB/removable-devices "
                    "menu first); (2) the scanner is in USB standby after "
                    "inactivity — unplug/replug or power-cycle to wake it."
                )
            io = cls(dev, readonly=readonly, lock=lock)
            if not readonly:
                # Step 4/5: verify BEFORE any state-changing call. Raises
                # (session REFUSED, nothing sent) on anything but 0x22/0x00.
                verdict = safety.verify_start_state(io)
                # Step 6: the verified open sequence, on the local raw
                # handle only, after the verdict.
                try:
                    _configure_for_writing(dev)
                except KeyboardInterrupt as e:
                    io.session.fail(e, where="open: configure_for_writing")
                    raise
                except Exception as e:
                    io.session.fail(e, where="open: configure_for_writing")
                    raise safety.SessionFailedError(
                        f"the USB open sequence (set_configuration) failed after the start "
                        f"state had been verified as {verdict.value}: {type(e).__name__}: {e}. "
                        f"The device may be partly configured; its state is UNKNOWN. "
                        f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                        observed=io.session.start_reg01, session=io.session.snapshot()) from e
            log.info("opened device bus=%d addr=%d%s", dev.bus, dev.address,
                     " (read-only session)" if readonly else "")
            return io
        except BaseException:
            if io is not None:
                io.close()        # releases the handle and the lock, sends nothing
            else:
                lock.release()
            raise

    def close(self) -> None:
        """Release the USB handle and the process lock. Sends nothing to
        the scanner: a session that failed stays failed on the wire --
        releasing the lock does not make the hardware safe."""
        try:
            if self.dev is not None:
                self.dev.dispose()
                self.dev = None
        finally:
            if self._lock is not None:
                self._lock.release()

    def __enter__(self) -> "UsbIo":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------ registers

    def write_regs(self, pairs: Iterable[Tuple[int, int]]) -> None:
        """Batch-write (reg, val) byte pairs.

        Wire: 0x40/0x04/0x0083, up to 64 B (32 pairs) per control transfer.
        """
        data = bytes(b for pair in pairs for b in pair)
        for i in range(0, len(data), _WRITE_CHUNK):
            chunk = data[i:i + _WRITE_CHUNK]
            n = self.dev.ctrl_transfer(0x40, 0x04, 0x0083, 0, chunk)
            if n != len(chunk):
                raise Of135iError(
                    f"short register write at offset {i}: "
                    f"wrote {n} of {len(chunk)} bytes"
                )

    def read_reg(self, reg: int, strict: bool = False) -> int:
        """Read one register.

        Wire: 0xc0/0x04/0x008e, wIndex=(reg<<8)|0x22, 2 B reply
        [value, 0x55]. The second byte is a constant ack; log (don't
        raise) if it deviates — captured hardware sometimes returns
        driver/hardware-managed low bits (see protocol-notes.md pass 4).
        With strict=True a deviating ack byte is a malformed reply and
        raises instead (the start-state check uses this: an unreadable
        or malformed state is an unsafe state, see safety.py).
        """
        resp = self.dev.ctrl_transfer(0xC0, 0x04, 0x008E, (reg << 8) | 0x22, 2)
        resp = bytes(resp)
        if len(resp) != 2:
            raise Of135iError(f"reg 0x{reg:02x} read: expected 2 B, got {resp!r}")
        if resp[1] != 0x55:
            if strict:
                raise Of135iError(
                    f"reg 0x{reg:02x} read: malformed reply {resp.hex()} "
                    f"(ack byte 0x{resp[1]:02x}, expected 0x55)"
                )
            log.warning(
                "reg 0x%02x read: unexpected ack byte 0x%02x (expected 0x55)",
                reg, resp[1],
            )
        return resp[0]

    def read_status(self) -> int:
        """Read the vendor driver's motor/engine status word.

        Wire: 0xc0/0x04/0x018e, wIndex=0x0122 -- the read the vendor
        polls after every stand-alone motor move (wValue 0x018e, not
        the 0x008e register read). Observed sequence during an eject:
        0xd9 -> 0xf9 -> 0xf8, the driver continues at 0xf8.
        """
        resp = bytes(self.dev.ctrl_transfer(0xC0, 0x04, 0x018E, 0x0122, 2))
        return resp[0] if resp else 0

    def read_status_word(self) -> int:
        """Read the engine/motor status as a combined 16-bit word.

        Same wire read as read_status() (0xc0/0x04/0x018e, wIndex
        0x0122) but returns both bytes combined (high byte << 8 | low
        byte) instead of just the high byte: the cold-start sequence
        (01-init.pcap, Scanner.cold_init()) polls on the full word
        (e.g. 0xf855) rather than the high byte alone.
        """
        resp = bytes(self.dev.ctrl_transfer(0xC0, 0x04, 0x018E, 0x0122, 2))
        if len(resp) < 2:
            return (resp[0] << 8) if resp else 0
        return (resp[0] << 8) | resp[1]

    def poll_status_word(self, mask: int, value: int, timeout: float = 30.0,
                          interval: float = 0.02) -> int:
        """Poll read_status_word() until (word & mask) == value.

        Non-raising, like read_status()/eject()'s own completion poll:
        logs a warning and returns the last value on timeout rather
        than raising, so a cold-init call site can continue best-
        effort through a stuck poll instead of aborting the whole
        sequence. Returns the last word read either way.
        """
        import time

        deadline = time.monotonic() + timeout
        last = self.read_status_word()
        while (last & mask) != value:
            if time.monotonic() > deadline:
                log.warning(
                    "poll_status_word timed out after %.1fs: last %#06x, "
                    "want %#06x (mask %#06x) -- continuing",
                    timeout, last, value, mask,
                )
                return last
            time.sleep(interval)
            last = self.read_status_word()
        return last

    def read_ext_reg(self, reg: int) -> int:
        """Read a register using the GL124/GL126 extended addressing scheme.

        SANE's GL124 backend addresses registers above 0xFF by setting
        bit 0x100 in wValue and packing only the low 8 bits of the
        register address into wIndex. This is the same wire shape
        read_status() uses for its fixed wValue=0x018e/wIndex=0x0122
        read of reg 0x101 (0x101 & 0xFF = 0x01, 0x01<<8|0x22 = 0x0122);
        this method generalises that to any register, low or extended.
        For reg <= 0xFF this is equivalent to read_reg() and delegates
        to it directly.

        Wire (reg > 0xFF): 0xc0/0x04/0x018e, wIndex=((reg&0xff)<<8)|0x22,
        2 B reply [value, 0x55]. Same ack-byte handling as read_reg().
        """
        if reg <= 0xFF:
            return self.read_reg(reg)
        resp = self.dev.ctrl_transfer(0xC0, 0x04, 0x018E, ((reg & 0xFF) << 8) | 0x22, 2)
        resp = bytes(resp)
        if len(resp) != 2:
            raise Of135iError(f"reg 0x{reg:03x} read: expected 2 B, got {resp!r}")
        if resp[1] != 0x55:
            log.warning(
                "reg 0x%03x read: unexpected ack byte 0x%02x (expected 0x55)",
                reg, resp[1],
            )
        return resp[0]

    def read_button(self, timeout_ms: int = 100) -> int | None:
        """Read one event byte from the interrupt endpoint (EP 0x83).

        Known codes: 0x48 = eject button, 0x04 = loader sensor event.
        Returns None on timeout (no event pending) rather than raising,
        since the interrupt endpoint is expected to be idle most of
        the time. Raises InterruptOverflowError when the scanner
        answers with more than the endpoint's 1-byte packet (EOVERFLOW,
        see the class docstring) -- the event cannot be read then.
        """
        try:
            data = self.dev.read(EP_INT_IN, 1, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as e:
            if getattr(e, "errno", None) == 75 or "Overflow" in str(e):
                raise InterruptOverflowError() from e
            raise InterruptReadError(f"interrupt endpoint (EP 0x83) read failed: {e}") from e
        return int(data[0]) if len(data) else None

    def drain_events(self, max_events: int = 8, timeout_ms: int = 50) -> list[int] | str:
        """Read pending interrupt events (EP 0x83) until the endpoint is
        idle or ``max_events`` were read -- what the vendor driver does
        continuously in its own loop (its captures show 1-byte reads,
        mostly empty, 0x48/0x04 when something happened). Reads only.
        Returns the event codes, or the string "overflow" when the
        endpoint is in the EOVERFLOW state (InterruptOverflowError)."""
        events: list[int] = []
        try:
            for _ in range(max_events):
                ev = self.read_button(timeout_ms=timeout_ms)
                if ev is None:
                    break
                events.append(ev)
        except InterruptOverflowError:
            return "overflow"
        return events

    def wait_reg(self, reg: int, value: int, timeout: float, mask: int = 0xFF) -> int:
        """Poll read_reg(reg) until (val & mask) == value, or raise on timeout.

        Returns the last read value. timeout is in seconds.
        """
        import time

        deadline = time.monotonic() + timeout
        last = None
        while True:
            last = self.read_reg(reg)
            if (last & mask) == value:
                return last
            if time.monotonic() > deadline:
                raise Of135iError(
                    f"wait_reg(0x{reg:02x}) timed out after {timeout}s: "
                    f"last value 0x{last:02x} (mask 0x{mask:02x}, "
                    f"want 0x{value:02x})"
                )
            time.sleep(0.004)

    # ------------------------------------------------------------- buffers

    def buf_read(self, addr: int, length: int, chunk: int = _BUF_CHUNK,
                 wIndex: int = 0) -> bytes:
        """Read a data buffer from the scanner.

        Wire: descriptor [addr u32 LE][length u32 LE] via
        0x40/0x04/0x0082, then bulk IN on EP 0x81 in `chunk`
        byte pieces (last piece may be shorter). wIndex is 0 for most
        reads; the capture uses wIndex=8 on the first image-data
        descriptor of a scan (meaning unknown; kept verbatim).
        """
        descriptor = struct.pack("<II", addr, length)
        self.dev.ctrl_transfer(0x40, 0x04, 0x0082, wIndex, descriptor)

        out = bytearray()
        remaining = length
        while remaining > 0:
            want = min(chunk, remaining)
            data = self.dev.read(EP_BULK_IN, want, timeout=60000)
            out.extend(data)
            remaining -= len(data)
            if len(data) == 0:
                raise Of135iError("buf_read: bulk IN returned 0 bytes")
        return bytes(out)

    def buf_write(self, addr: int, data: bytes, chunk: int = _BUF_CHUNK) -> None:
        """Write a data buffer to the scanner.

        Wire: descriptor [addr u32 LE][length u32 LE] via
        0x40/0x04/0x0082 wIndex=1, then bulk OUT on EP 0x02 in
        `chunk` byte pieces.
        """
        descriptor = struct.pack("<II", addr, len(data))
        self.dev.ctrl_transfer(0x40, 0x04, 0x0082, 1, descriptor)

        for i in range(0, len(data), chunk):
            self.dev.write(EP_BULK_OUT, data[i:i + chunk], timeout=5000)

    # --------------------------------------------------------------- misc

    def end_access(self, which: int = 0x8C, wIndex: int = 16) -> None:
        """Signal end-of-access for a buffer transfer.

        Wire: 0x40/0x0c/wValue=which, wIndex as given. Observed values:
        which=0x8c with wIndex 16 or 19, and which=0x8d with wIndex 0 (protocol-notes.md
        pass 2).
        """
        self.dev.ctrl_transfer(0x40, 0x0C, which, wIndex, b"")
