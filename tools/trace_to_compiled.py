#!/usr/bin/env python3
"""Convert an OF135I_TRACE recording into the compiled trace format.

The driver's opt-in recorder (of135i/trace.py, ``OF135I_TRACE=<path>``)
writes one JSON line per transfer. This tool turns that file into the
compiled op list described in compile_trace.py (``cw``/``cr``/``bo``/
``bi`` ops with ``dt``), saved as ``*.trace.json`` or ``*.trace.json.gz``,
which offline trace tools can load next to a compiled capture.

Notes:
- The first line must be the recorder's ``{"format": ...}`` header.
- A truncated final line (interrupted run) is ignored. Any other damaged
  or malformed line is reported on stderr; the conversion is refused
  (exit 1) unless ``--skip-damaged`` is given, which skips those lines.
  A record whose hex ``data`` is not strict (pairs of hex digits, no
  whitespace) or not exactly ``min(len, 64)`` bytes counts as damaged.
- Failed transfers and the interrupt endpoint are skipped (the compiled
  format has no place for them); the count is printed.
- The recorder keeps only the first 64 bytes of control and bulk-OUT
  payloads. An op whose payload was longer gets ``data``/``resp`` null,
  its full ``len`` and ``"partial": true`` -- no padded or truncated bytes
  are ever emitted.
- The output is diagnostic only, never a replayable capture: transfers
  are dropped (failed, short OUT/control-IN, interrupt, damaged lines),
  so every op carries ``"src": "of135i-trace/1"`` and
  tools/replay_trace.py refuses any trace containing such an op (or a
  partial op, or a write without strict hex data). Loaders that read
  ops by key ignore the extra key.
- The output is written to a 0600 temp file in the same directory and
  hard-linked into place, so a failed run leaves no partial output; an
  existing file or symlink is refused. Failed transfers, short OUT and short control-IN transfers
  are skipped. Bulk-IN records retain the actual returned length and,
  as ``want``, the requested one (a short bulk read is kept).

Usage: trace_to_compiled.py TRACE.jsonl [-o OUT.trace.json[.gz]] [--skip-damaged]
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path


FORMAT = "of135i-trace/1"           # of135i.trace.FORMAT (no pyusb import)
DATA_CAP = 64                       # of135i.trace.DATA_CAP
HEX = re.compile(r"(?:[0-9a-fA-F]{2})*")
KINDS = ("ctrl", "bulk", "intr")
SETUP = ("bm", "br", "wv", "wi", "wlen")


class TraceFormatError(ValueError):
    """The input is not an OF135I_TRACE recording."""


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_time(v) -> bool:
    return (_is_int(v) or isinstance(v, float)) and math.isfinite(v) and v >= 0


def _is_hex(v) -> bool:
    """Strict hex: pairs of hex digits only, no whitespace ("" is valid)."""
    return isinstance(v, str) and HEX.fullmatch(v) is not None


def record_problem(r) -> str | None:
    """Why ``r`` is not a usable transfer record, or None."""
    if not isinstance(r, dict):
        return "not a JSON object"
    if r.get("kind") not in KINDS or r.get("dir") not in ("in", "out"):
        return "unknown kind/dir"
    if not _is_time(r.get("t")) or not _is_time(r.get("dur", 0.0)):
        return "missing or invalid t/dur"
    status = r.get("status")
    if not isinstance(status, str):
        return "missing status"
    if status != "ok" and not status.startswith("short:"):
        return None                 # failed transfer: skipped, nothing else needed
    need = ("len", "want") if "want" in r else ("len",)
    if r["kind"] == "ctrl":
        need += SETUP
    if not all(_is_int(r.get(k)) and r[k] >= 0 for k in need):
        return "missing or invalid " + "/".join(need)
    has_data = r["kind"] == "ctrl" or (r["kind"] == "bulk" and r["dir"] == "out")
    if has_data:
        if not _is_hex(r.get("data")):
            return "missing or invalid hex data"
        if len(r["data"]) // 2 != min(r["len"], DATA_CAP):
            return f"data length does not match len (recorder keeps {DATA_CAP} bytes)"
    return None


def _header_ok(line: bytes) -> bool:
    try:
        head = json.loads(line.decode())
    except ValueError:
        return False
    return isinstance(head, dict) and head.get("format") == FORMAT


def scan_records(data: bytes | str) -> tuple[list[dict], list[str], bool]:
    """(valid records, problems with complete lines, partial final line?).

    The first line must be the recorder's format header. Every other
    damaged or malformed line is reported and skipped; only an
    unterminated final line counts as an interrupted write."""
    raw = data.encode() if isinstance(data, str) else data
    lines = raw.split(b"\n")
    if len(lines) < 2 or not _header_ok(lines[0]):
        raise TraceFormatError(f'not an OF135I_TRACE recording: first line is not '
                               f'{{"format": "{FORMAT}"}}')
    last = len(lines) - 1           # unterminated tail ("" after a final newline)
    records, problems, partial = [], [], False
    for no, line in enumerate(lines[1:], start=2):
        if not line and no - 1 == last:
            continue
        try:
            rec = json.loads(line.decode())
            why = record_problem(rec)
        except ValueError as e:     # includes UnicodeDecodeError
            rec, why = None, f"unreadable ({type(e).__name__})"
        if why is None:
            records.append(rec)
        elif no - 1 == last:
            partial = True
        else:
            problems.append(f"line {no}: {why}")
    return records, problems, partial


def read_records(text: str) -> list[dict]:
    """Valid records of a recording (see scan_records)."""
    return scan_records(text)[0]


def convert(records: list[dict]) -> tuple[list[dict], int]:
    """(compiled ops, number of skipped records)."""
    ops: list[dict] = []
    skipped = 0
    prev = 0.0
    for r in records:
        status = r.get("status")
        short_in = r["kind"] == "bulk" and r["dir"] == "in" and str(status).startswith("short:")
        if (status != "ok" and not short_in) or r["kind"] not in ("ctrl", "bulk"):
            skipped += 1
            continue
        start, dur = float(r["t"]), float(r.get("dur", 0.0))
        # compile_trace.py stamps cw/cr/bo at URB submission and bi at its
        # completion; dt is the gap to the previous op (0.0 for the first).
        bulk_in = r["kind"] == "bulk" and r["dir"] == "in"
        at = start + dur if bulk_in else start
        dt = max(0.0, at - prev) if ops else 0.0
        prev = max(prev, at)
        data = bytes.fromhex(r.get("data", ""))
        # A payload longer than the recorded cap is never padded or cut:
        # it becomes null and the op is marked partial (replay refuses it).
        whole = data.hex() if len(data) >= r["len"] else None
        if r["kind"] == "ctrl":
            op = {"bm": r["bm"], "br": r["br"], "wv": r["wv"], "wi": r["wi"]}
            if r["dir"] == "out":
                op.update(t="cw", data=whole)
            else:
                op.update(t="cr", len=r["wlen"], resp=whole)
        elif r["dir"] == "out":
            op = {"t": "bo", "data": whole}
        else:
            op = {"t": "bi", "len": r["len"]}
            if "want" in r:
                op["want"] = r["want"]
        if whole is None and op["t"] != "bi":
            op.update(len=r["len"], partial=True)
        op["dt"] = round(dt, 6)
        op["src"] = FORMAT          # diagnostic only: replay_trace.py refuses it
        ops.append(op)
    return ops, skipped


def write_exclusive(out: Path, payload: bytes) -> None:
    """Write a private (0600) temp file beside ``out``, then hard-link it
    into place: ``out`` appears complete or not at all, an existing file
    or symlink is never replaced, and the temp file is always removed."""
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as raw:
            if out.suffix == ".gz":
                with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                    gz.write(payload)
            else:
                raw.write(payload)
        os.link(tmp, out)
    finally:
        os.unlink(tmp)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trace")
    ap.add_argument("-o", "--out", help="default: TRACE with .trace.json")
    ap.add_argument("--skip-damaged", action="store_true",
                    help="convert even if complete lines are damaged (they are skipped)")
    args = ap.parse_args(argv)
    src = Path(args.trace)
    out = Path(args.out) if args.out else src.with_suffix(".trace.json")
    try:
        records, problems, _ = scan_records(src.read_bytes())
    except (OSError, TraceFormatError) as e:
        print(f"error: {src}: {e}", file=sys.stderr)
        return 2
    for problem in problems[:10]:
        print(f"{src}: {problem}", file=sys.stderr)
    if problems:
        print(f"{src}: {len(problems)} damaged line(s) skipped", file=sys.stderr)
        if not args.skip_damaged:
            print("refusing to convert; pass --skip-damaged to convert the rest",
                  file=sys.stderr)
            return 1
    ops, skipped = convert(records)
    try:
        write_exclusive(out, json.dumps(ops).encode())
    except FileExistsError:
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: cannot write {out}: {e}", file=sys.stderr)
        return 1
    print(f"{out}: {len(ops)} ops ({skipped} records skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
