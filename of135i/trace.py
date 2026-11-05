"""Opt-in transfer recorder (diagnostic, pure observer).

When ``OF135I_TRACE=<path>`` is set, every control and bulk transfer that
passes through ``safety.GuardedDevice`` is appended to ``<path>`` as one
JSON line, so a native run can be compared offline with another capture
(``tools/trace_to_compiled.py`` converts the file to the compiled trace
format the offline diff tools load).

Contract:

- Default (variable unset or empty): ``from_env()`` returns ``None`` and
  the transfer path runs the bare call -- no file, no clock read, no
  extra transfer.
- Observer only: the recorder reads what the transfer already produced.
  It adds no transfer, retry or sleep, but synchronous file writes add
  host overhead. It uses
  ``time.perf_counter`` (not ``time.monotonic``/``time.sleep``, which the
  driver's poll loops use for their deadlines).
- Failures are never fatal: any error opening, encoding or writing logs
  ONE warning, disables recording and lets scanner I/O continue.
- The file is created exclusively (``O_EXCL``: an existing file or a
  symlink is refused, never overwritten) with mode 0600. Records are
  written immediately; an interrupted write can leave a partial final
  line, which the converter ignores.
- Data: control transfers (both directions) and bulk-OUT keep the first
  ``DATA_CAP`` bytes plus the full length; bulk-IN (image data) and the
  interrupt endpoint record only the length.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("of135i")

TRACE_ENV = "OF135I_TRACE"
DATA_CAP = 64
FORMAT = "of135i-trace/1"


def _warn(message, *args) -> None:
    """A broken logging sink must not change a USB result or exception."""
    try:
        log.warning(message, *args)
    except Exception:
        pass


class TraceRecorder:
    """Appends one JSON line per transfer to an exclusively created file."""

    def __init__(self, path: str):
        self._fd: int | None = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._seq = 0
        try:
            self._t0 = time.perf_counter()
            self._emit({"format": FORMAT})
        except Exception as e:
            self._disable(e)

    @property
    def active(self) -> bool:
        return self._fd is not None

    def run(self, kind, direction, ep, setup, out_data, call, size=None):
        """Perform ``call()`` and record it; the transfer's result or
        exception passes through untouched. ``setup`` is (bm, br, wv, wi,
        wlen) for control transfers, else None; ``out_data`` the bytes
        sent for an OUT transfer; ``size`` the requested length or buffer
        of a bulk/interrupt read."""
        if self._fd is None:
            return call()
        try:
            t0 = time.perf_counter()
        except Exception as e:
            self._disable(e)
            return call()
        try:
            result = call()
        except BaseException as e:
            try:
                self._record(kind, direction, ep, setup, out_data, None, t0,
                             f"error:{type(e).__name__}", size)
            except BaseException as secondary:  # never replaces the USB outcome
                try:
                    e.add_note(f"transfer trace: {type(secondary).__name__} while recording")
                except BaseException:
                    pass
            raise
        self._record(kind, direction, ep, setup, out_data, result, t0, "ok", size)
        return result

    def _record(self, kind, direction, ep, setup, out_data, result, t0, status,
                size=None):
        try:
            dur = time.perf_counter() - t0
            rec = {"seq": self._seq + 1, "t": round(t0 - self._t0, 6),
                   "dur": round(dur, 6), "kind": kind, "dir": direction,
                   "status": status}
            if ep is not None:
                rec["ep"] = ep
            if setup is not None:
                rec["bm"], rec["br"], rec["wv"], rec["wi"], rec["wlen"] = setup
            if size is not None:    # never copies a read buffer
                rec["want"] = size if isinstance(size, int) else len(size)
            payload = b""
            if direction == "out":
                payload = bytes(out_data[:DATA_CAP]) if out_data is not None else b""
                rec["len"] = len(out_data) if out_data is not None else 0
                if status == "ok":
                    try:
                        if int(result) != rec["len"]:
                            rec["status"] = f"short:{int(result)}"
                    except (TypeError, ValueError):
                        rec["status"] = "short:?"
            elif result is not None:
                # Do not copy large image buffers merely to count them.
                rec["len"] = len(result) if not isinstance(result, int) else result
                want = setup[4] if kind == "ctrl" else rec.get("want")
                if status == "ok" and want is not None and rec["len"] != want:
                    rec["status"] = f"short:{rec['len']}"
                if kind == "ctrl":
                    payload = bytes(result[:DATA_CAP]) if not isinstance(result, int) else b""
            if kind == "ctrl" or (kind == "bulk" and direction == "out"):
                rec["data"] = payload[:DATA_CAP].hex()
            self._emit(rec)
        except BaseException as e:  # recording must never reach scanner I/O
            self._disable(e)
            if not isinstance(e, Exception):
                raise               # Ctrl-C / exit still propagate

    def _emit(self, rec: dict) -> None:
        line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        fd = self._fd
        if fd is None:
            return
        try:
            view = memoryview(line)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("trace write made no progress")
                view = view[n:]
        except BaseException as e:
            # Stop after a partial line so it stays the (ignored) last line.
            self._disable(e)
            if not isinstance(e, Exception):
                raise
            return
        self._seq = rec.get("seq", self._seq)

    def _disable(self, err: BaseException) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return                  # already disabled: warned once
        try:
            os.close(fd)
        except OSError:
            pass
        _warn("transfer trace disabled (%s: %s); scanning continues unrecorded",
              type(err).__name__, err)

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def from_env() -> "TraceRecorder | None":
    """A recorder when ``OF135I_TRACE`` names a path, else None. A path
    that cannot be created exclusively logs one warning and gives None."""
    path = os.environ.get(TRACE_ENV)
    if not path:
        return None
    try:
        rec = TraceRecorder(path)
    except Exception as e:
        _warn("transfer trace not started (%s: %s); scanning continues unrecorded",
              type(e).__name__, e)
        return None
    return rec if rec.active else None
