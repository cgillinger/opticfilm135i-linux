#!/usr/bin/env python3
"""Replay a compiled usbmon trace against the OpticFilm 135i.

Executes the exact command stream a compiled segment contains (see
compile_trace.py): control writes, status polls, bulk OUT (slope tables,
shading data) and bulk IN (calibration + image data). Data read from the
scanner is stored per buffer-descriptor in --out.

Usage:
  .venv/bin/python tools/replay_trace.py TRACE.json.gz --out OUTDIR [--dry-run]

Safety (docs/hardware-safety.md): the device is opened through the
driver's guarded transport (of135i.usbio.UsbIo), so the process lock
and the start-state guard apply -- the replay refuses to send anything
unless reg 0x01 reads 0x22 (idle-homed). A cold scanner (0x00) is
refused too: a replay is not the cold-init path. The FIRST USB error
of any kind aborts the replay; nothing is retried, no stall is
"cleared", no recovery command is sent, and the operator is told to
power-cycle. The trace is replayed verbatim; motor moves are the
captured ones. Run with the film magazine in the same state as the
capture (loaded, frame 1).
"""

import argparse
import gzip
import json
import logging
import struct
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from of135i import safety  # noqa: E402
from of135i.usbio import EP_BULK_IN, EP_BULK_OUT, Of135iError, UsbIo  # noqa: E402


class Replayer:
    def __init__(self, dev, outdir, log):
        self.dev, self.outdir, self.log = dev, outdir, log
        self.buffers = []
        self.cur_tag, self.cur = None, bytearray()
        self.mismatch = 0

    def flush_buffer(self):
        if self.cur:
            self.buffers.append((self.cur_tag, self.cur))
            print(f"buffer {self.cur_tag}: {len(self.cur)} B", file=self.log)
        self.cur_tag, self.cur = None, bytearray()

    def exec_op(self, i, op):
        t, dev = op["t"], self.dev
        if t == "cw":
            data = bytes.fromhex(op["data"]) if op["data"] else b""
            if op["br"] == 4 and op["wv"] == 0x0082 and len(data) == 8:
                self.flush_buffer()
                addr, ln = struct.unpack("<II", data)
                self.cur_tag = f"{i:05d}-addr{addr:08x}-len{ln}"
            dev.ctrl_transfer(op["bm"], op["br"], op["wv"], op["wi"], data)
        elif t == "cr":
            got = bytes(dev.ctrl_transfer(op["bm"], op["br"], op["wv"],
                                          op["wi"], op["len"])).hex()
            if got != op["resp"]:
                self.mismatch += 1
                print(f"op{i} cr wv={op['wv']:#06x} wi={op['wi']:#06x}: "
                      f"got {got} want {op['resp']}", file=self.log)
        elif t == "poll":
            want = op["resps"][-1]
            deadline = time.time() + max(3 * op["dur"], 5.0)
            while True:
                got = bytes(dev.ctrl_transfer(op["bm"], op["br"], op["wv"],
                                              op["wi"], op["len"])).hex()
                if got == want:
                    return
                if time.time() > deadline:
                    self.mismatch += 1
                    print(f"op{i} poll wi={op['wi']:#06x}: stuck at {got}, "
                          f"want {want} — continuing", file=self.log)
                    return
                time.sleep(0.004)
        elif t == "bo":
            dev.write(EP_BULK_OUT, bytes.fromhex(op["data"]), timeout=5000)
        elif t == "bi":
            # A bulk-IN timeout is NOT skipped any more: it aborts the
            # replay like every other USB error (safety pass 2026-09-05).
            data = dev.read(EP_BULK_IN, op["len"], timeout=15000)
            self.cur.extend(data)

    def save(self):
        self.flush_buffer()
        for tag, buf in self.buffers:
            (self.outdir / f"buf-{tag}.bin").write_bytes(bytes(buf))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--out", default="replay-out")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pace", type=float, default=0.05,
                    help="sleep captured gaps longer than this (s)")
    args = ap.parse_args()

    ops = json.load(gzip.open(args.trace, "rt"))
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        stats, bi = {}, 0
        for op in ops:
            stats[op["t"]] = stats.get(op["t"], 0) + 1
            bi += op.get("len", 0) if op["t"] == "bi" else 0
        print(f"{len(ops)} ops {stats}; bulk-in {bi} B; no device touched.")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        io = UsbIo.open()
    except Of135iError as e:
        sys.exit(f"error: {e}")
    log = open(outdir / "replay.log", "w")
    r = Replayer(io.dev, outdir, log)
    t0 = time.time()
    status = 1
    try:
        verdict = safety.verify_start_state(io)
        if verdict is not safety.StartState.IDLE:
            raise safety.OperationNotAllowedError(
                f"replay refused: scanner start state is {verdict.value}; a trace replay "
                f"is only permitted from the idle-homed state (reg 0x01 = 0x22). "
                f"{safety.NO_COMMANDS_SENT}")
        with io.session.operation("replay"):
            for i, op in enumerate(ops):
                io.session.phase = f"op{i}"
                if op["dt"] > args.pace:
                    time.sleep(min(op["dt"], 2.0))
                r.exec_op(i, op)
                if i % 200 == 0:
                    done = sum(len(b) for _, b in r.buffers) + len(r.cur)
                    print(f"\r  op {i}/{len(ops)}  data {done//1024} kB  "
                          f"mismatches {r.mismatch}   ", end="", flush=True)
        status = 0
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        print(io.session.describe_failure(), file=sys.stderr)
        status = 130
    except safety.SafetyError as e:
        print(f"\nrefused: {e}", file=sys.stderr)
    except Exception as e:
        print(f"\nerror at {io.session.phase}: {type(e).__name__}: {e}", file=sys.stderr)
        print(io.session.describe_failure(), file=sys.stderr)
    finally:
        r.save()
        print(json.dumps(io.session.snapshot(), indent=2, default=str), file=log)
        log.close()
        io.close()
    print(f"\nDone in {time.time()-t0:.0f} s, {len(r.buffers)} buffers, "
          f"{r.mismatch} mismatches, session {io.session.state.value}. Output in {outdir}/")
    return status


if __name__ == "__main__":
    sys.exit(main())
