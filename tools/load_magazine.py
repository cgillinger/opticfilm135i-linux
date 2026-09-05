#!/usr/bin/env python3
"""Load the film magazine the way the vendor driver does.

Command-line wrapper around the vendor's insert flow (of135i/tables_load.py,
compiled from the 2026-09-05 clean-load capture, Test 14 in docs/test-log.md),
in the vendor's order:

  1. base register table + AFE values (Scanner.initialize(prep=False);
     on a cold scanner cold_init() runs first) -- magazine loose in the
     slot, as at the vendor's app start;
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

from of135i.device import Scanner  # noqa: E402
from of135i.safety import POWER_CYCLE_INSTRUCTION, SafetyError  # noqa: E402
from of135i.usbio import Of135iError  # noqa: E402


REINSERT_PROMPT = (
    "\nJOG done. Now take the magazine FULLY OUT of the slot, then insert it "
    "fresh all the way to the mechanical stop.\nPress Enter when it is at the "
    "stop (Ctrl-C aborts; a power cycle is then required): "
)


def main(argv: list[str] | None = None, ask=input) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(f"usage: {sys.argv[0]} (no arguments; --full was removed, see the "
              "module docstring)", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    scanner = None
    try:
        with Scanner.open() as scanner:
            # Loader sensor BEFORE initialize() -- the register table
            # written by initialize() changes reg 0x101 so the sensor
            # bit is unreliable after it. Reads only.
            if not scanner.is_magazine_present():
                print("error: no magazine present in the slot (loader sensor clear)",
                      file=sys.stderr)
                return 1
            scanner.initialize(prep=False)
            print("running the vendor app-start jog (feed, feed, eject)...")
            scanner.jog_magazine()       # raises StrictPollTimeoutError -> exit 1 below
            reg32_before = scanner.io.read_reg(0x32)
            ask(REINSERT_PROMPT)
            reg32_after = scanner.io.read_reg(0x32)
            print(f"reg 0x32 before/after the reinsert: {reg32_before:#04x} / {reg32_after:#04x}")
            print("running the vendor load sequence...")
            scanner.load_magazine()      # raises LoadIncompleteError -> exit 1 below
            print("load sequence completed (status class and loader-sensor bit matched the "
                  "capture after the feed, the traverse and a final read). This sets "
                  "the vendor's 'loaded' indication only: check by hand that the magazine "
                  "is latched before scanning -- the sensor reports presence, not latching.")
            return 0
    except KeyboardInterrupt:
        # Even at the reinsert prompt (jog complete, session armed) the
        # rule holds: no new session on top of an aborted one.
        print(f"\ninterrupted. {POWER_CYCLE_INSTRUCTION}", file=sys.stderr)
        _report(scanner)
        return 130
    except SafetyError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        _report(scanner)
        return 1
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        _report(scanner)
        return 1


def _report(scanner) -> None:
    if scanner is not None:
        print(scanner.session.describe_failure(), file=sys.stderr)
    else:
        print(POWER_CYCLE_INSTRUCTION, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
