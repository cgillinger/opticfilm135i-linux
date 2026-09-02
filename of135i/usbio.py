"""USB transport primitives for the Plustek OpticFilm 135i (GL126).

Implements the Genesys vendor protocol over control endpoint 0, as
documented in protocol-notes.md (transport table + pass 4/5). This
module knows nothing about scanner semantics (registers, tables,
sequences) — it only speaks the wire protocol.
"""

from __future__ import annotations

import logging
import struct
from typing import Iterable, Sequence, Tuple

import usb.core
import usb.util

log = logging.getLogger("of135i")

VID = 0x07B3
PID = 0x1436

EP_BULK_IN = 0x81
EP_BULK_OUT = 0x02
EP_INT_IN = 0x83

_WRITE_CHUNK = 64          # bytes = 32 (reg, val) pairs
_BUF_CHUNK = 16384         # bulk transfer chunk size


class Of135iError(RuntimeError):
    """Raised for transport-level failures specific to this driver."""


class UsbIo:
    """Thin wrapper around a pyusb device handle for the 07b3:1436 scanner."""

    def __init__(self, dev: "usb.core.Device"):
        self.dev = dev

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def open(cls) -> "UsbIo":
        """Find and claim the scanner on the host USB bus.

        Raises Of135iError with likely-cause hints if the device is not
        present (commonly: still attached to a VM, or in USB standby —
        see protocol-notes.md pass 4).
        """
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise Of135iError(
                "Scanner 07b3:1436 not found on the host USB bus. "
                "Likely causes: (1) the device is still attached to a VM "
                "(release it via the hypervisor's USB/removable-devices "
                "menu first); (2) the scanner is in USB standby after "
                "inactivity — unplug/replug or power-cycle to wake it."
            )
        try:
            if dev.is_kernel_driver_active(0):
                log.info("detaching kernel driver from interface 0")
                dev.detach_kernel_driver(0)
        except NotImplementedError:
            # Platforms without kernel-driver introspection (e.g. Windows).
            pass
        dev.set_configuration()
        log.info("opened device bus=%d addr=%d", dev.bus, dev.address)
        return cls(dev)

    def close(self) -> None:
        if self.dev is not None:
            usb.util.dispose_resources(self.dev)
            self.dev = None

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

    def read_reg(self, reg: int) -> int:
        """Read one register.

        Wire: 0xc0/0x04/0x008e, wIndex=(reg<<8)|0x22, 2 B reply
        [value, 0x55]. The second byte is a constant ack; log (don't
        raise) if it deviates — captured hardware sometimes returns
        driver/hardware-managed low bits (see protocol-notes.md pass 4).
        """
        resp = self.dev.ctrl_transfer(0xC0, 0x04, 0x008E, (reg << 8) | 0x22, 2)
        resp = bytes(resp)
        if len(resp) != 2:
            raise Of135iError(f"reg 0x{reg:02x} read: expected 2 B, got {resp!r}")
        if resp[1] != 0x55:
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
        the time.
        """
        try:
            data = self.dev.read(EP_INT_IN, 1, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            return None
        return int(data[0]) if len(data) else None

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
