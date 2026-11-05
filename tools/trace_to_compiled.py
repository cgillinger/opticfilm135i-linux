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
- Failed transfers and the interrupt endpoint are skipped (the compiled
  format has no place for them); the count is printed.
- The recorder keeps only the first 64 bytes of a bulk-OUT payload; the
  converter zero-pads it to the recorded length so op lengths stay
  correct. Bytes beyond the first 64 are therefore NOT real data, and
  bulk-OUT content hashes are not comparable for payloads over 64 bytes.
- Control payloads in both directions longer than 64 bytes are likewise
  truncated. These diagnostic traces are not complete captures for replay.
- The output file is created exclusively with mode 0600; an existing file
  is refused. Failed transfers, short OUT and short control-IN transfers
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
import sys
from pathlib import Path


FORMAT = "of135i-trace/1"           # of135i.trace.FORMAT (no pyusb import)
KINDS = ("ctrl", "bulk", "intr")
SETUP = ("bm", "br", "wv", "wi", "wlen")


class TraceFormatError(ValueError):
    """The input is not an OF135I_TRACE recording."""


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_time(v) -> bool:
    return (_is_int(v) or isinstance(v, float)) and math.isfinite(v) and v >= 0


def _is_hex(v) -> bool:
    try:
        bytes.fromhex(v)
    except (TypeError, ValueError):
        return False
    return True


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
    if has_data and not _is_hex(r.get("data")):
        return "missing or invalid hex data"
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
        # The loader places a control read at (running sum - dur) and every
        # other op at the running sum: keep submission-time spacing.
        at = start + dur if r["kind"] == "ctrl" and r["dir"] == "in" else start
        dt = max(0.0, at - prev) if ops else at
        prev = max(prev, at)
        data = bytes.fromhex(r.get("data", ""))
        if r["kind"] == "ctrl":
            op = {"bm": r["bm"], "br": r["br"], "wv": r["wv"], "wi": r["wi"]}
            if r["dir"] == "out":
                op.update(t="cw", data=data.hex())
            else:
                op.update(t="cr", len=r["wlen"], resp=data.hex(), dur=dur)
        elif r["dir"] == "out":
            data = data + bytes(max(0, r["len"] - len(data)))
            op = {"t": "bo", "data": data.hex()}
        else:
            op = {"t": "bi", "len": r["len"]}
            if "want" in r:
                op["want"] = r["want"]
        op["dt"] = round(dt, 6)
        ops.append(op)
    return ops, skipped


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
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"refusing to overwrite {out}", file=sys.stderr)
        return 1
    with os.fdopen(fd, "wb") as raw:
        if out.suffix == ".gz":
            with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                gz.write(json.dumps(ops).encode())
        else:
            raw.write(json.dumps(ops).encode())
    print(f"{out}: {len(ops)} ops ({skipped} records skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
