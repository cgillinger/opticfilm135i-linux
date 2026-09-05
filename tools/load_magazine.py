#!/usr/bin/env python3
"""Load the film magazine the way the vendor driver does.

Command-line wrapper around the vendor's insert flow (of135i/tables_load.py,
compiled from the 2026-09-05 clean-load capture, Test 14 in docs/test-log.md),
in the vendor's order:

  1. the vendor's device-open sequence (Scanner.initialize(prep=False)
     = tables_load.OPEN: app-start register table with the loader
     motor profile, AFE bring-up; on a cold scanner cold_init() runs
     first) -- magazine loose in the slot, as at the vendor's app start;
  2. the app-start JOG (Scanner.jog_magazine(): feed 6690, feed 6690,
     eject 3090). Every vendor load that engaged the cassette was
     preceded by it; every load of ours that did not engage lacked it
     (Tests 11b-15). The magazine may move during it;
  3. the operator takes the magazine FULLY OUT and reinserts it fresh,
     all the way to the mechanical stop, then presses Enter (the
     vendor app prompts the same; Test 14 latched from there);
  4. the LOAD (Scanner.load_magazine(): loader-sensor ack, the engaging
     feed with the vendor's full register block, the prescan traverse).

Every motor completion is verified with the masked completion test
(state class AND loader-sensor bit 0x08: jog moves 0xf855, engaging
feed 0xf455 = cassette pulled past the sensor, traverse 0xdc55) and the
flow stops -- session failed, power cycle -- at the first that does not
pass. The JOG + reinsert + LOAD flow is NOT yet hardware-verified.

Safety (docs/hardware-safety.md): the start-state guard in the driver
refuses to send anything unless reg 0x01 reads 0x22 (idle-homed) or
0x00 (cold, never homed -- initialize() then runs cold_init first).
Any failure or Ctrl-C leaves the hardware state unknown; the only
recovery is a power cycle. The loader sensor is checked BEFORE
initialize() because the base register table makes it unreliable
afterwards -- and a "present" sensor does not prove the magazine is
fed or latched: check the magazine by hand and the LED colour before
scanning.

The former --full flow (the 2026-08-30 preview-preparation capture)
was removed in the 2026-09-05 safety pass: its end state is one the
vendor never ejects from, and three ejects from it stalled the
transport (protocol-notes.md pass 14, eject addendum).

Usage:
    .venv/bin/python tools/load_magazine.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from of135i import loadflow  # noqa: E402
from of135i.loadflow import REINSERT_PROMPT  # noqa: E402,F401  (re-exported for tests)


def main(argv: list[str] | None = None, ask=input) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(f"usage: {sys.argv[0]} (no arguments; the same flow is `of135i load`)", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return loadflow.run(ask=ask)


if __name__ == "__main__":
    sys.exit(main())
