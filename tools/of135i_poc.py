#!/usr/bin/env python3
"""Replay-PoC for Plustek OpticFilm 135i (GL126) — protocol validation.

Based on protokoll-anteckningar.md (2026-08-29). Stages, in rising risk:

  probe   passive: open device, read status registers. No writes.
  init    write the ini's "0" register table + AFE table, verify readback.
  eject   replay the decoded eject sequence (motor moves!).
  home    homing sequence (motor mode 0x30, FEEDL=1).

Usage:
  sudo .venv/bin/python of135i_poc.py probe
  sudo .venv/bin/python of135i_poc.py init
  sudo .venv/bin/python of135i_poc.py eject

The device must be attached to the HOST (disconnect from the VM first:
VMware > Removable Devices > Film Scanner > Disconnect).
"""

import re
import sys
import time
from pathlib import Path

import usb.core
import usb.util

VID, PID = 0x07B3, 0x1436
INI = Path(__file__).parent / "Scanapi_07b3_1436.ini"

# ---------------------------------------------------------------- transport


def open_dev():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("Scanner not found on host bus. Released from the VM? "
                 "In standby? (5 min after release -> needs physical wake)")
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
    dev.set_configuration()
    return dev


def write_regs(dev, pairs):
    """Batch register write: 0x40/0x04/0x0083, (reg,val) byte pairs,
    max 64 B (32 pairs) per transfer."""
    data = bytes(b for p in pairs for b in p)
    for i in range(0, len(data), 64):
        n = dev.ctrl_transfer(0x40, 0x04, 0x0083, 0, data[i:i + 64])
        assert n == len(data[i:i + 64]), f"short write at offset {i}"


def read_reg(dev, reg):
    """Register read: 0xc0/0x04/0x008e, wIndex = (reg<<8)|0x22, 2 B reply."""
    ret = dev.ctrl_transfer(0xC0, 0x04, 0x008E, (reg << 8) | 0x22, 2)
    return bytes(ret)


# ---------------------------------------------------------------- ini parsing


def ini_hex_table(name):
    """Parse a REGEDIT4 multi-line hex value, e.g. "0"=hex:01,22,02,78,..."""
    text = INI.read_text(encoding="latin-1")
    m = re.search(rf'^"{re.escape(name)}"=hex:((?:[0-9a-f]{{2}},?\\?\s*)+)',
                  text, re.M)
    if not m:
        sys.exit(f'ini table "{name}" not found in {INI}')
    blob = re.sub(r"[\\,\s]", " ", m.group(1)).split()
    vals = bytes(int(b, 16) for b in blob)
    return list(zip(vals[0::2], vals[1::2]))


# ---------------------------------------------------------------- stages


def stage_probe(dev):
    print(f"Device: bus {dev.bus} addr {dev.address}, "
          f"bcdDevice {dev.bcdDevice:#06x}, "
          f"product '{usb.util.get_string(dev, dev.iProduct)}'")
    for reg in (0x01, 0x31, 0x32, 0x35):
        val = read_reg(dev, reg)
        print(f"  reg 0x{reg:02x} = {val.hex()}")
    print("probe OK — control-EP read path verified.")


def stage_init(dev):
    init_pairs = ini_hex_table("0")
    print(f"Writing init table: {len(init_pairs)} (reg,val) pairs ...")
    write_regs(dev, init_pairs)

    # AFE programming: AfeWriteReg = 0x51 (addr), 0x5d (hi), 0x5e (lo);
    # values from the ini's "@" table: pairs of (afe_reg, value16?) —
    # captured sequence was: 51 <adr> 5d <hi> 5e <lo> per AFE register.
    afe = ini_hex_table("@")
    print(f"Programming AFE: {len(afe)} registers ...")
    for adr, val in afe:
        write_regs(dev, [(0x51, adr), (0x5D, 0x00), (0x5E, val)])

    # Readback verification of a handful of init values.
    expect = dict(init_pairs)
    ok = True
    for reg in (0x03, 0x05, 0x06, 0x1E, 0x31, 0xB9):
        got = read_reg(dev, reg)
        want = expect[reg]
        mark = "OK" if got and got[0] == want else "MISMATCH"
        if mark != "OK":
            ok = False
        print(f"  reg 0x{reg:02x}: wrote 0x{want:02x}, read {got.hex()} {mark}")
    print("init", "VERIFIED — register write/read path works." if ok
          else "readback mismatch — check read encoding (wIndex |0x22).")


def motor_run(dev, mode, feedl, label):
    """Decoded motor sequence: 02=mode, 3d..3f=FEEDL (24-bit BE per capture
    order 3d,3e,3f), 0f=01 executes."""
    print(f"{label}: mode=0x{mode:02x} FEEDL={feedl}")
    write_regs(dev, [(0x09, 0x08)])
    write_regs(dev, [
        (0x02, mode), (0xAE, 0x00), (0xAF, 0xFF),
        (0x3D, (feedl >> 16) & 0xFF),
        (0x3E, (feedl >> 8) & 0xFF),
        (0x3F, feedl & 0xFF),
    ])
    write_regs(dev, [(0x0F, 0x01)])
    # Poll status while the motor runs.
    for _ in range(100):
        time.sleep(0.2)
        st = read_reg(dev, 0x01)
        print(f"  reg 0x01 = {st.hex()}", end="\r")
    print()
    write_regs(dev, [(0x09, 0x00)])
    print(f"{label} sequence sent.")


def stage_eject(dev):
    # 06-eject: FEEDL = 0x000c12 = 3090 steps (verified in pass 3).
    motor_run(dev, 0x18, 3090, "eject")


def stage_home(dev):
    motor_run(dev, 0x30, 1, "home")


# ---------------------------------------------------------------- main

STAGES = {"probe": stage_probe, "init": stage_init,
          "eject": stage_eject, "home": stage_home}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STAGES:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(STAGES)}}}")
    dev = open_dev()
    try:
        STAGES[sys.argv[1]](dev)
    finally:
        usb.util.dispose_resources(dev)
