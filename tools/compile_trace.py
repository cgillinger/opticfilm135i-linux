#!/usr/bin/env python3
"""Compile a usbmon pcap segment into a replayable command trace (JSON.gz).

Input: two tshark field dumps (see extract_dumps below for exact fields):
  <name>-ctrl.psv   — everything except bulk IN (EP 0x81)
  <name>-bulkin.psv — bulk IN completions (frame, epoch, len)

Output ops (ordered by frame number):
  cw   control write  {bm, br, wv, wi, data}
  cr   control read   {bm, br, wv, wi, len, resp}
  poll coalesced run of identical cr:s {bm, br, wv, wi, len, n, resps, dur}
  bo   bulk OUT       {data}
  bi   bulk IN        {len}
Every op carries dt = seconds since previous op (for pacing/timeouts).

Usage: compile_trace.py <segment.pcap> [outdir]
Runs tshark itself if the .psv dumps are missing.
"""

import gzip
import json
import subprocess
import sys
from pathlib import Path

FIELDS_CTRL = ["frame.number", "frame.time_epoch", "usb.urb_type",
               "usb.transfer_type", "usb.endpoint_address",
               "usb.bmRequestType", "usb.setup.bRequest",
               "usb.setup.wValue", "usb.setup.wIndex", "usb.data_len",
               "usb.data_fragment", "usb.control.Response", "usb.capdata"]
FIELDS_BULKIN = ["frame.number", "frame.time_epoch", "usb.data_len"]


def extract_dumps(pcap, outdir):
    base = outdir / pcap.stem
    ctrl, bulkin = base.with_suffix(".ctrl.psv"), base.with_suffix(".bulkin.psv")
    for path, filt, fields in (
            (ctrl, "!(usb.endpoint_address == 0x81)", FIELDS_CTRL),
            (bulkin, "usb.endpoint_address == 0x81 && usb.urb_type == 'C'",
             FIELDS_BULKIN)):
        if path.exists():
            continue
        print(f"tshark -> {path.name} ...", file=sys.stderr)
        cmd = ["tshark", "-r", str(pcap), "-Y", filt,
               "-T", "fields", "-E", "separator=|"]
        for f in fields:
            cmd += ["-e", f]
        with open(path, "w") as fh:
            subprocess.run(cmd, stdout=fh, stderr=subprocess.DEVNULL,
                           check=True)
    return ctrl, bulkin


def num(s):
    return int(s, 0) if s else 0


def parse(ctrl_psv, bulkin_psv):
    ops = []
    pending_read = None
    for line in open(ctrl_psv):
        c = line.rstrip("\n").split("|")
        frame, epoch, urb = int(c[0]), float(c[1]), c[2].strip("'")
        ep, bm = c[4], c[5]
        if ep == "0x83":                       # interrupt EP: skip
            continue
        if ep in ("0x80", "0x00"):             # control
            if urb == "S":
                rec = dict(bm=num(bm), br=num(c[6]), wv=num(c[7]),
                           wi=num(c[8]), frame=frame, ts=epoch)
                if num(bm) & 0x80:
                    pending_read = rec
                else:
                    rec["t"] = "cw"
                    rec["data"] = c[10]
                    ops.append(rec)
            elif urb == "C" and pending_read is not None:
                pending_read.update(t="cr", len=num(c[9]), resp=c[11])
                ops.append(pending_read)
                pending_read = None
        elif ep == "0x02" and urb == "S" and c[12]:   # bulk OUT w/ payload
            ops.append(dict(t="bo", data=c[12], frame=frame, ts=epoch))
    for line in open(bulkin_psv):
        c = line.rstrip("\n").split("|")
        ops.append(dict(t="bi", len=int(c[2]), frame=int(c[0]),
                        ts=float(c[1])))
    ops.sort(key=lambda o: o["frame"])
    return ops


def coalesce_polls(ops):
    out = []
    for op in ops:
        prev = out[-1] if out else None
        if (op["t"] == "cr" and prev is not None
                and prev["t"] in ("cr", "poll")
                and all(prev.get(k) == op.get(k)
                        for k in ("bm", "br", "wv", "wi"))):
            if prev["t"] == "cr":
                prev.update(t="poll", n=1, resps=[prev.pop("resp")],
                            dur=0.0, ts0=prev["ts"])
            prev["n"] += 1
            if prev["resps"][-1] != op["resp"]:
                prev["resps"].append(op["resp"])
            prev["dur"] = op["ts"] - prev["ts0"]
            prev["ts"] = op["ts"]
        else:
            out.append(op)
    return out


def add_dt(ops):
    last = None
    for op in ops:
        op["dt"] = round(op["ts"] - last, 4) if last is not None else 0.0
        last = op["ts"]
        for k in ("frame", "ts", "ts0"):
            op.pop(k, None)
    return ops


def main():
    pcap = Path(sys.argv[1])
    outdir = Path(sys.argv[2]) if len(sys.argv) > 2 else pcap.parent
    ctrl, bulkin = extract_dumps(pcap, outdir)
    ops = add_dt(coalesce_polls(parse(ctrl, bulkin)))
    trace = outdir / (pcap.stem + ".trace.json.gz")
    with gzip.open(trace, "wt") as fh:
        json.dump(ops, fh)
    stats = {}
    for op in ops:
        stats[op["t"]] = stats.get(op["t"], 0) + 1
    bi_total = sum(op["len"] for op in ops if op["t"] == "bi")
    print(f"{trace}: {len(ops)} ops {stats}, bulk-in total {bi_total} B")


if __name__ == "__main__":
    main()
