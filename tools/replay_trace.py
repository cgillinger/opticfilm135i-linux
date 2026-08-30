#!/usr/bin/env python3
"""Replay a compiled usbmon trace against the OpticFilm 135i.

Executes the exact command stream a compiled segment contains (see
compile_trace.py): control writes, status polls, bulk OUT (slope tables,
shading data) and bulk IN (calibration + image data). Data read from the
scanner is stored per buffer-descriptor in --out.

Usage:
  sudo .venv/bin/python replay_trace.py TRACE.json.gz --out OUTDIR [--dry-run]

Safety: the trace is replayed verbatim; motor moves are the captured
ones (calibration passes + frame feed + scan). Run with the film
magazine in the same state as the capture (loaded, frame 1).
USB errors are logged and skipped (stalls cleared), not fatal — the
capture contains enumeration debris that a live replay cannot satisfy.
"""

import argparse
import gzip
import json
import struct
import sys
import time
from pathlib import Path

import usb.core
import usb.util

VID, PID = 0x07B3, 0x1436


def open_dev():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("Scanner not on host bus (VM holds it? standby?).")
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
    dev.set_configuration()
    return dev


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
            dev.write(0x02, bytes.fromhex(op["data"]), timeout=5000)
        elif t == "bi":
            try:
                data = dev.read(0x81, op["len"], timeout=15000)
            except usb.core.USBTimeoutError:
                self.mismatch += 1
                print(f"op{i} bi len={op['len']}: TIMEOUT", file=self.log)
                return
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
        return

    dev = open_dev()
    log = open(outdir / "replay.log", "w")
    r = Replayer(dev, outdir, log)
    t0 = time.time()
    consecutive = 0
    try:
        for i, op in enumerate(ops):
            if op["dt"] > args.pace:
                time.sleep(min(op["dt"], 2.0))
            try:
                r.exec_op(i, op)
                consecutive = 0
            except usb.core.USBError as e:
                r.mismatch += 1
                consecutive += 1
                print(f"op{i} {op['t']}: USB error {e} — continuing",
                      file=log)
                if getattr(e, "errno", None) == 32:      # EPIPE: clear stall
                    for ep in (0x81, 0x02):
                        try:
                            dev.clear_halt(ep)
                        except usb.core.USBError:
                            pass
                if consecutive > 25:
                    print(f"op{i}: too many consecutive USB errors, "
                          f"aborting", file=log)
                    break
            if i % 200 == 0:
                done = sum(len(b) for _, b in r.buffers) + len(r.cur)
                print(f"\r  op {i}/{len(ops)}  data {done//1024} kB  "
                      f"mismatches {r.mismatch}   ", end="", flush=True)
    finally:
        r.save()
        log.close()
        usb.util.dispose_resources(dev)
    print(f"\nDone in {time.time()-t0:.0f} s, {len(r.buffers)} buffers, "
          f"{r.mismatch} mismatches. Output in {outdir}/")


if __name__ == "__main__":
    main()
