#!/usr/bin/env python3
"""Load the film magazine the way the vendor driver does.

Thin command-line wrapper around Scanner.load_magazine() (the vendor's
standalone insert flow, compiled into of135i/tables_load.py from the
2026-09-05 clean-load capture, Test 14 in docs/test-log.md). The user
takes the magazine fully out and inserts the cassette fresh, all the
way to the mechanical stop -- that is where the vendor app loads from
(Test 14; the earlier trigger-point idea was wrong). The driver acks
the loader sensor (reg 0x32), then runs the engaging feed (mode 0x18,
FEEDL 0x1a22, the vendor's full register block, loader slope tables)
and the slow prescan traverse (mode 0x1c, FEEDL 71490). Nothing else.
Both motor completions and a final status read are verified with the
masked completion test (state class AND loader-sensor bit 0x08; feed
0xf455 = cassette pulled past the sensor, traverse 0xdc55) and the
flow stops -- session failed, power cycle -- at the first that does
not pass. This table is NOT yet hardware-verified as a load: the
previous one (cut from an eject capture, feed without its register
block) ran the transport without engaging the cassette (Tests 11b-13).

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


def main() -> int:
    if len(sys.argv) > 1:
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
            scanner.initialize()
            print("running the vendor load sequence...")
            scanner.load_magazine()      # raises LoadIncompleteError -> exit 1 below
            print("load sequence completed (status class and loader-sensor bit matched the "
                  "capture after the feed, the traverse and a final read). This sets "
                  "the vendor's 'loaded' indication only: check by hand that the magazine "
                  "is latched before scanning -- the sensor reports presence, not latching.")
            return 0
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
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
