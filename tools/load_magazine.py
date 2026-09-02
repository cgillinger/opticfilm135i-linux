#!/usr/bin/env python3
"""Load the film magazine the way the vendor driver does.

Default (verified end-to-end 2026-09-02: load -> scan -> eject): the
vendor's plain insert flow as captured that day -- the user pushes the
cassette in to the stop, the driver acks the loader sensor (reg 0x32),
then feed (mode 0x18, FEEDL 0x1a22, loader speed regs + loader slope
tables) and the slow prescan traverse (mode 0x1c, FEEDL 71490). Nothing
else. Ejecting from this state works; scanning from it works.

--full: the 2026-08-30 capture's longer flow (feed, traverse, six mode-
0x78 loader pulses, calibration pulses). That turned out to be the
vendor app's PREVIEW preparation, and its end state -- preparation done
but no preview pass run -- is one the vendor never ejects from: three
ejects from it stalled the transport (protocol-notes.md pass 14, eject
addendum). Kept for reference only.

A bare 0x18 feed alone (our first attempt) drags the film in
mechanically but leaves the loader state wrong -- the next scan then
runs its motor without ever streaming data.
"""
import gzip, json, sys, time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from of135i.device import Scanner
from of135i.tables import Op

# Default: the 2026-09-02 capture (feed + traverse). --full: the 2026-08-30
# preview-preparation flow. See the module docstring.
LITE = "--full" not in sys.argv
if LITE:
    raw = json.load(gzip.open(_REPO / "traces" / "20260902-vendor-eject-from-loaded.trace.json.gz", "rt"))
else:
    raw = json.load(gzip.open(_REPO / "traces" / "load-only-fixed.trace.json.gz", "rt"))

def mkop(o):
    return Op(o["t"], o.get("dt", 0.0),
              bm=o.get("bm") or 0, br=o.get("br") or 0,
              wv=o.get("wv") or 0, wi=o.get("wi") or 0,
              data=bytes.fromhex(o["data"]) if o.get("data") else b"",
              length=o.get("len") or 0,
              resp=bytes.fromhex(o["resps"][-1] if o.get("resps") else o.get("resp", "") or ""),
              dur=o.get("dur") or 0.0)

LOAD = [mkop(o) for o in (raw[291:640] if LITE else raw[790:3280])]

with Scanner.open() as sc:
    sc.initialize()
    # Check loader sensor before starting — the cassette must be
    # physically pushed in to the stop before running this script.
    if not sc.is_magazine_loaded():
        print("error: no magazine detected (push cassette in to the stop first)",
              file=sys.stderr)
        sys.exit(1)
    print("kör leverantörens laddsekvens%s..." % ("" if LITE else " (full)"))
    sc._exec_ops(LOAD)
    print("laddning klar — knappen ska vara blå")
