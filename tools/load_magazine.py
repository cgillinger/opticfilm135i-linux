#!/usr/bin/env python3
"""Load the film magazine the way the vendor driver does.

The complete, captured insert-to-ready flow (load-only capture,
2026-08-30 evening):

1. The cassette must be INSERTED BY THE USER while the driver waits --
   reg 0x32 flips 0x1f -> 0x5b on insertion (loader sensor).
2. The driver acks with reg 0x32=0x1d, then runs: feed (mode 0x18,
   FEEDL 0x1a22, speed regs + slope tables), slow prescan traverse
   (mode 0x1c, FEEDL 71490), six loader pulses (mode 0x78, FEEDL 1),
   and drains the ~16 MB preview readout the traverse produced.

A bare 0x18 feed alone (our first attempt) drags the film in
mechanically but leaves the loader state wrong -- the next scan then
runs its motor without ever streaming data.
"""
import gzip, json, sys, time
sys.path.insert(0, "driver")
from of135i.device import Scanner
from of135i.tables import Op

raw = json.load(gzip.open("traces/load-only-fixed.trace.json.gz", "rt"))

def mkop(o):
    return Op(o["t"], o.get("dt", 0.0),
              bm=o.get("bm") or 0, br=o.get("br") or 0,
              wv=o.get("wv") or 0, wi=o.get("wi") or 0,
              data=bytes.fromhex(o["data"]) if o.get("data") else b"",
              length=o.get("len") or 0,
              resp=bytes.fromhex(o["resps"][-1] if o.get("resps") else o.get("resp", "") or ""),
              dur=o.get("dur") or 0.0)

LOAD = [mkop(o) for o in raw[790:3280]]

with Scanner.open() as sc:
    sc.initialize()
    # Loader-sensor detection is an open question (reg 0x101 bit 0x08
    # per the vendor config; reg 0x32 mirrors it only in the vendor's
    # own configured state). v1: the user confirms insertion by
    # starting this script with the cassette already pushed in.
    print("kör leverantörens laddsekvens...")
    sc._exec_ops(LOAD)
    print("laddning klar — knappen ska vara blå")
