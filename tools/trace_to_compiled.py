#!/usr/bin/env python3
"""Convert an OF135I_TRACE recording into the compiled trace format.

The driver's opt-in recorder (of135i/trace.py, ``OF135I_TRACE=<path>``)
writes one JSON line per transfer. This tool turns that file into the
compiled op list described in compile_trace.py (``cw``/``cr``/``bo``/
``bi`` ops with ``dt``), saved as ``*.trace.json`` or ``*.trace.json.gz``,
which offline trace tools can load next to a compiled capture.

Notes:
- A truncated final line (interrupted run) is ignored; every complete
  line before it is kept.
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
  are skipped. Bulk-IN records retain the actual returned length.

Usage: trace_to_compiled.py TRACE.jsonl [-o OUT.trace.json[.gz]]
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path


def read_records(text: str) -> list[dict]:
    """Complete JSON lines only; a damaged or partial line ends the read."""
    out = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            break
        if isinstance(rec, dict) and "kind" in rec:
            out.append(rec)
    return out


def convert(records: list[dict]) -> tuple[list[dict], int]:
    """(compiled ops, number of skipped records)."""
    ops: list[dict] = []
    skipped = 0
    prev = 0.0
    for r in records:
        if r.get("status") != "ok" or r["kind"] not in ("ctrl", "bulk"):
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
        op["dt"] = round(dt, 6)
        ops.append(op)
    return ops, skipped


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trace")
    ap.add_argument("-o", "--out", help="default: TRACE with .trace.json")
    args = ap.parse_args(argv)
    src = Path(args.trace)
    out = Path(args.out) if args.out else src.with_suffix(".trace.json")
    ops, skipped = convert(read_records(src.read_text()))
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
