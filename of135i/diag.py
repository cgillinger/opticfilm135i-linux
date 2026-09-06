"""Read-only diagnostics for the of135i driver.

Two entry points:

  - `collect_doctor(io)` / `format_doctor(report)`: a hardware health
    report (the `doctor` CLI command). HARD RULE: this module's doctor
    path performs ONLY USB control READS (bmRequestType 0xC0) and
    pyusb descriptor accessors, plus the existing interrupt-endpoint
    button read (`io.read_button()`) the `status` command already
    does. No register writes, no `initialize()`/`cold_init()`, no
    motor commands, no bulk transfers.
  - `write_sidecar()` / `sidecar_path()`: JSON sidecar files for the
    per-scan diagnostics device.py's `Scanner.scan()`/`_scan_dual()`
    collect into `Scanner.last_diag` (see device.py's module docstring
    for what's recorded -- this module has no opinion on scan
    internals, only on how the result is serialized).

`known_read_regs()` derives the register-dump set at import time from
the phase tables themselves (tables.py, tables_ir.py, the four
tables_dpi<N>.py modules) rather than a hand-maintained list, so the
dump can never read a register the vendor driver itself was never
observed reading.
"""

from __future__ import annotations

import importlib
import json
import logging
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import usb.util

from . import safety, tables, tables_ir

log = logging.getLogger("of135i")

# tables_dpi<N> modules with a compiled dual-light phase set (see
# device.py's DUAL_DPIS); 3600's dual set lives in tables_ir instead.
_DPI_TABLE_MODULE_NAMES = ("tables_dpi600", "tables_dpi1200", "tables_dpi2400", "tables_dpi7200")

_BUTTON_NAMES = {0x48: "eject", 0x04: "sensor"}
_EP_TYPE_NAMES = {0: "control", 1: "isochronous", 2: "bulk", 3: "interrupt"}


# ------------------------------------------------------------- read-only set


def known_read_regs() -> tuple[int, ...]:
    """Every register the vendor driver itself is observed reading, as
    a sorted tuple of register numbers (plain regs 0x00-0xff, extended
    regs as 0x100 and up).

    Derived by scanning every Op in tables.PHASES, tables_ir.PHASES
    and the four tables_dpi<N>.PHASES modules for a plain register
    read (kind in ("cr", "poll"), br == 0x04, wv == 0x008E -- register
    number op.wi >> 8, per usbio.read_reg's wire shape) or an extended
    register read (same kind/br, wv == 0x018E -- register number
    0x100 | (op.wi >> 8), per usbio.read_ext_reg/read_status's wire
    shape: wIndex = ((reg & 0xff) << 8) | 0x22).
    """
    modules = [tables, tables_ir]
    for name in _DPI_TABLE_MODULE_NAMES:
        modules.append(importlib.import_module(f".{name}", __package__))

    regs: set[int] = set()
    for mod in modules:
        for phase in mod.PHASES:
            for op in phase.ops:
                if op.kind not in ("cr", "poll") or op.br != 0x04:
                    continue
                if op.wv == 0x008E:
                    regs.add(op.wi >> 8)
                elif op.wv == 0x018E:
                    regs.add(0x100 | (op.wi >> 8))
    return tuple(sorted(regs))


# ------------------------------------------------------------------ doctor


def _button_name(button: int | None) -> str:
    if button is None:
        return "idle"
    return _BUTTON_NAMES.get(button, f"0x{button:02x}")


def _ep_type_name(bm_attributes: int) -> str:
    return _EP_TYPE_NAMES.get(bm_attributes & 0x03, f"unknown(0x{bm_attributes:02x})")


def _collect_usb(dev) -> dict:
    """USB descriptor info via pyusb accessors only -- no control
    transfers here (chip_id/status_word/regs cover those separately)."""
    info: dict = {}
    for field, is_hex in (
        ("idVendor", True), ("idProduct", True), ("bcdDevice", True),
        ("bus", False), ("address", False), ("bNumConfigurations", False),
    ):
        try:
            val = getattr(dev, field)
            info[field] = f"0x{val:04x}" if is_hex else val
        except Exception as e:
            info[field] = {"error": str(e)}

    for name, idx_attr in (
        ("manufacturer", "iManufacturer"), ("product", "iProduct"), ("serial", "iSerialNumber"),
    ):
        try:
            idx = getattr(dev, idx_attr)
            # get_string commonly fails without udev/permission setup;
            # that's an expected, non-fatal outcome here.
            info[name] = usb.util.get_string(dev, idx) if idx else None
        except Exception as e:
            info[name] = f"<unavailable: {e}>"

    try:
        cfg = dev.get_active_configuration()
        interfaces = []
        for intf in cfg:
            endpoints = [
                {
                    "address": f"0x{ep.bEndpointAddress:02x}",
                    "type": _ep_type_name(ep.bmAttributes),
                    "max_packet_size": ep.wMaxPacketSize,
                }
                for ep in intf
            ]
            interfaces.append({
                "number": intf.bInterfaceNumber,
                "alt_setting": intf.bAlternateSetting,
                "endpoints": endpoints,
            })
        info["interfaces"] = interfaces
    except Exception as e:
        info["interfaces"] = f"<unavailable: {e}>"

    return info


def _collect_regs(io) -> dict:
    out: dict = {}
    for reg in known_read_regs():
        try:
            val = io.read_reg(reg) if reg <= 0xFF else io.read_ext_reg(reg)
            out[f"0x{reg:02x}"] = val
        except Exception as e:
            out[f"0x{reg:02x}"] = {"error": str(e)}
    return out


def _collect_state(io) -> dict:
    """Engine state from reg 0x01, classified by the ONE rule set in
    safety.classify_reg01 (the same rule the start-state guard
    enforces). An unsafe state gets the operator advice attached;
    doctor itself never acts on it."""
    val = io.read_reg(0x01)
    verdict = safety.classify_reg01(val)
    out = {"raw": f"0x{val:02x}", "name": verdict.value}
    if verdict is safety.StartState.UNSAFE:
        out["advice"] = (
            "not an accepted start state: every writing operation will refuse to "
            "start. " + safety.POWER_CYCLE_INSTRUCTION
        )
        out["magazine_sensor_note"] = (
            "the loader-sensor reading is unreliable in this state (a previous "
            "session has written the base register table)"
        )
    return out


def _collect_host() -> dict:
    info: dict = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        import usb
        info["pyusb"] = getattr(usb, "__version__", None)
    except Exception as e:
        info["pyusb"] = f"<unavailable: {e}>"

    # Package's parent dir, not cwd -- doctor may be invoked from
    # anywhere.
    repo_root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        )
        info["driver_revision"] = proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:
        info["driver_revision"] = None

    return info


def collect_doctor(io) -> dict:
    """Read-only hardware health report. `io` is an open UsbIo (or,
    for tests, a duck-typed equivalent exposing `.dev` and the same
    read_reg/read_ext_reg/read_status_word/read_button methods).

    Every item is collected independently (guarded with try/except)
    so one failing read doesn't blank out the rest of the report --
    e.g. a device string read commonly fails without extra permission
    setup, which shouldn't take down the register dump.
    """
    report: dict = {}

    def guarded(key, fn):
        try:
            report[key] = fn()
        except Exception as e:
            report[key] = {"error": str(e)}

    guarded("usb", lambda: _collect_usb(io.dev))
    guarded("chip_id", lambda: bytes(io.dev.ctrl_transfer(0xC0, 0x0C, 0x008A, 0x26FE, 1)).hex())
    guarded("status_word", lambda: f"0x{io.read_status_word():04x}")
    guarded("regs", lambda: _collect_regs(io))
    guarded("magazine_present", lambda: bool(io.read_ext_reg(0x101) & 0x08))
    guarded("button", lambda: _button_name(io.read_button()))
    guarded("state", lambda: _collect_state(io))
    guarded("host", _collect_host)

    return report


def format_doctor(report: dict) -> str:
    """Readable multi-line rendering of a collect_doctor() report.
    Never raises on missing/error-shaped keys -- a partial report
    (some items failed) still renders the parts that succeeded."""
    lines: list[str] = []

    def is_error(val) -> bool:
        return isinstance(val, dict) and set(val.keys()) == {"error"}

    lines.append("== USB ==")
    usb_info = report.get("usb")
    if isinstance(usb_info, dict) and not is_error(usb_info):
        for key in ("idVendor", "idProduct", "bcdDevice", "bus", "address", "bNumConfigurations",
                    "manufacturer", "product", "serial"):
            if key in usb_info:
                lines.append(f"  {key}: {usb_info[key]}")
        interfaces = usb_info.get("interfaces")
        if isinstance(interfaces, list):
            for intf in interfaces:
                lines.append(f"  interface {intf.get('number')} alt {intf.get('alt_setting')}:")
                for ep in intf.get("endpoints", []):
                    lines.append(
                        f"    ep {ep.get('address')} {ep.get('type')} "
                        f"max={ep.get('max_packet_size')}"
                    )
        elif interfaces is not None:
            lines.append(f"  interfaces: <unavailable: {interfaces}>")
    else:
        lines.append(f"  <unavailable: {usb_info}>")

    lines.append("")
    lines.append("== Chip ==")
    chip_id = report.get("chip_id")
    if is_error(chip_id):
        lines.append(f"  <unavailable: {chip_id.get('error')}>")
    else:
        lines.append(f"  id: {chip_id}")

    lines.append("")
    lines.append("== State ==")
    state = report.get("state")
    if isinstance(state, dict) and not is_error(state):
        lines.append(f"  reg 0x01 = {state.get('raw')} ({state.get('name')})")
        if state.get("advice"):
            lines.append(f"  ADVICE: {state['advice']}")
        if state.get("magazine_sensor_note"):
            lines.append(f"  note: {state['magazine_sensor_note']}")
    else:
        lines.append(f"  <unavailable: {state}>")
    status_word = report.get("status_word")
    if status_word is not None and not is_error(status_word):
        lines.append(f"  status word: {status_word}")

    lines.append("")
    lines.append("== Registers ==")
    regs = report.get("regs")
    if isinstance(regs, dict) and not is_error(regs):
        keys = sorted(regs.keys(), key=lambda k: int(k, 16))
        row: list[str] = []
        for key in keys:
            val = regs[key]
            val_str = f"0x{val:02x}" if isinstance(val, int) else str(val)
            row.append(f"{key}={val_str}")
            if len(row) == 4:
                lines.append("  " + "  ".join(row))
                row = []
        if row:
            lines.append("  " + "  ".join(row))
    else:
        lines.append(f"  <unavailable: {regs}>")

    lines.append("")
    lines.append("== Magazine ==")
    magazine = report.get("magazine_present")
    if is_error(magazine):
        lines.append(f"  <unavailable: {magazine.get('error')}>")
    else:
        lines.append(f"  present: {magazine}   (presence only: not fed, not latched)")

    lines.append("")
    lines.append("== Button ==")
    lines.append(f"  {report.get('button')}")

    lines.append("")
    lines.append("== Host ==")
    host = report.get("host")
    if isinstance(host, dict) and not is_error(host):
        for key, val in host.items():
            lines.append(f"  {key}: {val}")
    else:
        lines.append(f"  <unavailable: {host}>")

    return "\n".join(lines)


# --------------------------------------------------------------- sidecars


def _json_default(obj):
    """json.dump's `default=`: numpy scalars/arrays -> plain Python via
    .tolist(), bytes -> hex, anything else -> str()."""
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        try:
            return tolist()
        except Exception:
            pass
    return str(obj)


def write_sidecar(path: str, data: dict) -> None:
    """Write `data` as pretty-printed, key-sorted JSON to `path`."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True, default=_json_default)


def sidecar_path(out: str) -> str:
    """Per-scan diagnostics sidecar path for an output image path:
    foo.tiff -> foo.diag.json, dir/x.pnm -> dir/x.diag.json."""
    return str(Path(out).with_suffix(".diag.json"))


# ------------------------------------------------ calibration buffer dump
#
# Test 28+ / dark_b-collapse investigation (docs/test-log.md Test 24-29):
# the driver keeps only per-channel MEANS of the dark buffers, so when a
# batch's even frames showed dark_b_mean collapsing to three identical
# values (B5), the raw bytes needed to tell WHY (flat hardware data, a
# short/stale transfer, or channel replication) had already been dropped.
# This dump persists the raw dark buffers the driver ALREADY read, plus
# the metadata that would decide it. It adds NO USB traffic and does not
# touch the op sequence: callers invoke it after a phase's read has
# returned, on bytes already in host memory.


def _buffer_stats(raw: bytes) -> dict:
    """Interpret ``raw`` the way the driver does (``<u2`` reshaped to
    (N, 3)) and record what distinguishes the collapse hypotheses:
    exact byte length, whole-buffer sha256, and PER-CHANNEL sha256 +
    mean/min/max.  Three identical per-channel sha256 values prove the
    R/G/B columns are bit-identical (not merely equal in mean); a length
    that is not a clean (N, 3) u16 buffer flags a truncated/short read."""
    import hashlib

    out: dict = {
        "byte_len": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "reshape_ok": len(raw) % 6 == 0,  # N * 3 channels * 2 bytes
    }
    if not out["reshape_ok"] or len(raw) == 0:
        return out
    arr = np.frombuffer(raw, dtype="<u2").reshape(-1, 3)
    out["n_triplets"] = int(arr.shape[0])
    chan_sha = [hashlib.sha256(np.ascontiguousarray(arr[:, ch]).tobytes()).hexdigest()
                for ch in range(3)]
    out["channel_sha256"] = chan_sha
    out["channels_bit_identical"] = (chan_sha[0] == chan_sha[1] == chan_sha[2])
    a = arr.astype(np.float64)
    out["channel_mean"] = [float(x) for x in a.mean(axis=0)]
    out["channel_min"] = [int(x) for x in arr.min(axis=0)]
    out["channel_max"] = [int(x) for x in arr.max(axis=0)]
    # Per-triplet R==G==B share: the strongest tell for [v, v, v]
    # replication vs a genuine per-channel measurement.
    same = int(np.count_nonzero((arr[:, 0] == arr[:, 1]) & (arr[:, 1] == arr[:, 2])))
    out["triplets_rgb_equal"] = same
    out["triplets_rgb_equal_frac"] = same / arr.shape[0] if arr.shape[0] else None
    return out


def dump_calibration_buffers(out_dir: str, base: str, meta: dict,
                             named_buffers: dict) -> str:
    """Write the raw calibration buffers in ``named_buffers`` (name ->
    bytes) to ``out_dir`` as ``<base>-<name>.bin``, plus a
    ``<base>.calbuf.json`` metadata file combining ``meta`` (frame, dpi,
    dual, commit, host, timestamps) with per-buffer :func:`_buffer_stats`
    and a cross-buffer ``equal_to`` map (which buffers are byte-identical
    -- the direct test for buffer reuse/stale RAM, e.g. dark_b == dark_a).
    Pure host-side I/O; the caller has already read every buffer.  Returns
    the metadata file path."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    record = dict(meta)
    buffers: dict = {}
    for name, raw in named_buffers.items():
        raw = bytes(raw)
        (d / f"{base}-{name}.bin").write_bytes(raw)
        buffers[name] = _buffer_stats(raw)
    # Cross-buffer byte-identity: the direct stale-RAM / reuse signal.
    names = list(named_buffers)
    equal_to: dict = {}
    for i, a in enumerate(names):
        eq = [b for j, b in enumerate(names)
              if i != j and bytes(named_buffers[a]) == bytes(named_buffers[b])]
        if eq:
            equal_to[a] = eq
    record["buffers"] = buffers
    record["byte_identical_buffers"] = equal_to
    meta_path = str(d / f"{base}.calbuf.json")
    with open(meta_path, "w") as f:
        json.dump(record, f, indent=2, sort_keys=True, default=_json_default)
    return meta_path
