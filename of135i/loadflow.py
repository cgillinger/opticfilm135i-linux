"""The magazine load flow, shared by `of135i load` and tools/load_magazine.py.

The vendor's insert flow in the vendor's order (docs/test-log.md Tests
14-23; docs/hardware-safety.md):

  1. Scanner.initialize(prep=False): the vendor's device-open sequence
     (cold_init first on a cold scanner) -- magazine loose in the slot;
  2. Scanner.jog_magazine(): the app-start jog (feed, feed, eject);
  3. the operator takes the magazine FULLY OUT and reinserts it fresh,
     all the way to the mechanical stop, then presses Enter;
  4. Scanner.load_magazine(): the engaging feed and the prescan traverse.

Every motor completion is verified with the masked completion test and
the flow stops -- session failed, power cycle -- at the first that does
not pass. Verified on hardware 7/7 from power-on (2026-09-05). The
reinsert prompt needs a real terminal: run it in a terminal window,
not through a piped or captured stdin.

Release-only mode (``release_only=True``, ``of135i load --release``):
runs steps 1-2 only (the device-open sequence and the app-start jog)
and stops -- no reinsert prompt, no ``load_magazine()``. This exists
for the two-cycle exit documented in docs/test-log.md Tests 48/49:
after a power cycle with the magazine LATCHED, the first `of135i load`
reproducibly (2/2) stops at the load feed with status 0xfc55 -- the
cold init + jog release the magazine, but the feed that follows does
not engage. Running the feed on that first cycle only produces a
failed session and an alarming "hardware state UNKNOWN" message for a
benign, expected outcome; ``--release`` does just the part that is
known to work (cold_init + jog) and returns 0, so the operator can
power-cycle again and run the plain ``of135i load`` from the now-loose
magazine.
"""

from __future__ import annotations

import sys

from .device import Scanner
from .safety import POWER_CYCLE_INSTRUCTION, SafetyError
from .usbio import Of135iError

REINSERT_PROMPT = (
    "\nJOG done. Now take the magazine FULLY OUT of the slot, then insert it "
    "fresh all the way to the mechanical stop.\nPress Enter when it is at the "
    "stop (Ctrl-C aborts; a power cycle is then required): "
)


def run(ask=input, release_only: bool = False, double_jog: bool = False) -> int:
    """Run the vendor's magazine insert flow end to end; returns the
    process exit code (0 loaded, 1 failed/refused, 130 interrupted).
    ``ask`` is called with the reinsert prompt and must block until the
    operator has reinserted the magazine (input() in a real terminal).

    ``release_only=True`` runs only the device-open sequence and the
    app-start jog, then returns 0 without calling ``ask`` and without
    ``load_magazine()`` -- see the module docstring.

    ``double_jog=True`` is the A/B EXPERIMENT for the latched-magazine
    start (docs/test-log.md Test 49 exit): after the first jog and the
    operator's reinsert, run the app-start jog a second time -- now from
    the loose position, the state the vendor's jog runs from at every
    app open -- ask for a second reinsert, then load. UNVERIFIED on
    hardware; one power cycle instead of two if it engages. Not combined
    with ``release_only``."""
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
            print(f"interrupt events after the jog: {scanner.io.drain_events()}")
            if release_only:
                print("magazine released (jog complete). Take the magazine out of the "
                      "slot, power-cycle the scanner, then run `of135i load` from the "
                      "loose position. Nothing else was sent.")
                return 0
            reg32_before = scanner.io.read_reg(0x32)
            ask(REINSERT_PROMPT)
            reg32_after = scanner.io.read_reg(0x32)
            print(f"interrupt events after the reinsert: {scanner.io.drain_events()}")
            print(f"reg 0x32 before/after the reinsert: {reg32_before:#04x} / {reg32_after:#04x}")
            if double_jog:
                print("EXPERIMENT: running the app-start jog a second time, from the loose position...")
                scanner.jog_magazine()
                print(f"interrupt events after the second jog: {scanner.io.drain_events()}")
                reg32_before = scanner.io.read_reg(0x32)
                ask(REINSERT_PROMPT)
                reg32_after = scanner.io.read_reg(0x32)
                print(f"interrupt events after the second reinsert: {scanner.io.drain_events()}")
                print(f"reg 0x32 before/after the second reinsert: {reg32_before:#04x} / {reg32_after:#04x}")
            print("running the vendor load sequence...")
            scanner.load_magazine()      # raises LoadIncompleteError -> exit 1 below
            print(f"interrupt events after the load: {scanner.io.drain_events()}")
            print("load sequence completed (status class and loader-sensor bit matched the "
                  "capture after the feed, the traverse and a final read). This sets "
                  "the vendor's 'loaded' indication only: check by hand that the magazine "
                  "is latched before scanning -- the sensor reports presence, not latching.")
            return 0
    except (KeyboardInterrupt, EOFError):
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


