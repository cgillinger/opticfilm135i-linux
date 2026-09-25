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

Next-strip mode (``next_strip=True``, ``of135i load --next-strip``):
loads the NEXT strip after an eject, in the SAME power-on as the
previous load/scan/eject -- skips the jog and the reinsert prompt
entirely. Evidence: a vendor USB capture made 2026-09-25
(``20260925-vendor-next-strip.pcap``, private analysis area, not in
this repo; docs/protocol-notes.md Pass 14 addendum 4) shows QuickScan
running its app-start jog only ONCE, when the app opens. The between-
strip load it runs after an eject button press -- operator swaps the
strip, pushes it in to the stop -- is just the LOAD table again: no
jog, no OPEN register table, no reinsert prompt. It ran with the
speed registers and slope table the PRECEDING scan pass left in place
(not the loader profile the jog would have set) and still latched
normally (same motor sound the owner heard on the first load).

This mode therefore runs ``initialize(prep=False)`` (replays
tables_load.OPEN -- our loader-profile register context, not the
vendor's post-scan leftover, per the owner's decision: same LOAD
table as the jog path, not the vendor's variant) followed directly by
``load_magazine()``, with no jog and no ``ask``. It refuses up front,
before any write, if the scanner is COLD (reg 0x01 = 0x00): a power
cycle happened since the previous strip and only the full ``of135i
load`` (with the jog and the reinsert prompt) is valid from there --
next-strip mode assumes the transport is already homed and positioned
from a prior load/scan/eject in this power-on, exactly as the vendor's
between-strip load assumes.

Hardware-verified once (docs/test-log.md Test 89, 2026-09-25): straight
after a driver eject, feed completion 0xf455 and traverse 0xdc55 on the
first poll, reg 0x32 read 0xbf beforehand (LOAD's literal 0x1d ack did
not matter for the grip), and the following scan positioned exactly as
the strip before it. Not combined with ``release_only`` or
``double_jog``.
"""

from __future__ import annotations

import sys

from .device import Scanner
from .safety import POWER_CYCLE_INSTRUCTION, SafetyError, SessionState
from .usbio import Of135iError

REINSERT_PROMPT = (
    "\nJOG done. Now take the magazine FULLY OUT of the slot, then insert it "
    "fresh all the way to the mechanical stop.\nPress Enter when it is at the "
    "stop (Ctrl-C aborts; a power cycle is then required): "
)

#: What the operator sees when the load feed does not engage -- by far
#: most often because the magazine was not taken out and reinserted to
#: the stop at the prompt (Tests 48/49: 2/2 the same benign signature).
#: The session is still failed and a power cycle is still required (the
#: safety model is unchanged); only the explanation is human.
FEED_NOT_ENGAGED_MSG = """
The load feed did not engage. This is almost always because the magazine
was not taken fully out and reinserted to the mechanical stop when the
prompt asked for it -- the scanner is fine and nothing is stuck.

To try again:
  1. Power the scanner OFF, wait until its light is out, then ON again.
  2. Run `of135i load` and, at the prompt, take the magazine FULLY OUT,
     push it back in to the stop, and THEN press Enter.

If you did reinsert it correctly and still see this, the feed genuinely
failed to grab; the same power cycle resets that safely too."""

#: Refusal for --next-strip on a cold scanner (checked before any write).
NEXT_STRIP_COLD_MSG = """
--next-strip refused: the scanner is COLD (reg 0x01 = 0x00) -- a power cycle
happened since the last load/scan/eject. Next-strip mode skips the jog and
the reinsert prompt on the assumption that the transport is already homed
and positioned from earlier in this power-on, the same assumption the
vendor's own between-strip load makes (docs/protocol-notes.md Pass 14
addendum 4).

Run the FULL `of135i load` instead (it runs the jog and asks you to take
the magazine fully out and reinsert it to the stop)."""

#: What the operator sees when the load feed does not engage in
#: --next-strip mode -- a different likely cause than the plain flow's
#: FEED_NOT_ENGAGED_MSG, because there is no reinsert step to have skipped.
NEXT_STRIP_FEED_NOT_ENGAGED_MSG = """
The next-strip load feed did not engage. This path skips the jog and the
reinsert prompt, so it depends on the new strip already being pushed in
to the stop before the command ran -- if it was not, or the mechanism was
left somewhere unexpected, the feed can fail to grab.

To recover:
  1. Power the scanner OFF, wait until its light is out, then ON again.
  2. Run the FULL `of135i load` (with the jog and the reinsert prompt),
     not `--next-strip`.

--next-strip has been hardware-verified once (docs/test-log.md Test
89); if the strip really was at the stop and this still happens, report
it and use the full load."""


def run(ask=input, release_only: bool = False, double_jog: bool = False,
        next_strip: bool = False) -> int:
    """Run the vendor's magazine insert flow end to end; returns the
    process exit code (0 loaded, 1 failed/refused, 2 bad combination of
    modes, 130 interrupted). ``ask`` is called with the reinsert prompt
    and must block until the operator has reinserted the magazine
    (input() in a real terminal).

    ``release_only=True`` runs only the device-open sequence and the
    app-start jog, then returns 0 without calling ``ask`` and without
    ``load_magazine()`` -- see the module docstring.

    ``double_jog=True`` is the A/B EXPERIMENT for the latched-magazine
    start (docs/test-log.md Test 49 exit): after the first jog and the
    operator's reinsert, run the app-start jog a second time -- now from
    the loose position, the state the vendor's jog runs from at every
    app open -- ask for a second reinsert, then load. UNVERIFIED on
    hardware; one power cycle instead of two if it engages. Not combined
    with ``release_only``.

    ``next_strip=True`` loads the next strip after an eject in the same
    power-on -- no jog, no ``ask`` -- see the module docstring. Refused
    (exit 1) if the scanner reads COLD before any write. Not combined
    with ``release_only`` or ``double_jog`` (raises ValueError -- the
    CLI turns that into exit 2 before calling in)."""
    if next_strip and (release_only or double_jog):
        raise ValueError("next_strip is not combined with release_only or double_jog")
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
            if next_strip:
                return _run_next_strip(scanner)
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
            try:
                scanner.load_magazine()  # raises LoadIncompleteError/StrictPollTimeoutError
            except SafetyError as e:
                # The one failure an operator commonly causes themselves:
                # translate it, keep the technical cause to one short
                # line, and skip the raw session dump that reads like a
                # crash. The session is failed exactly as before.
                print(FEED_NOT_ENGAGED_MSG, file=sys.stderr)
                cause = str(e).split(". ")[0]
                if len(cause) > 120:
                    cause = cause[:117] + "..."
                print(f"\n(technical cause: {type(e).__name__}: {cause})",
                      file=sys.stderr)
                return 1
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


def _run_next_strip(scanner) -> int:
    """--next-strip's body, run from inside ``run()``'s ``Scanner.open()``
    block, after the magazine-presence check: refuse (exit 1) on a COLD
    scanner, else ``initialize(prep=False)`` -> ``load_magazine()``
    directly -- no jog, no ``ask``. See the module docstring and
    docs/protocol-notes.md Pass 14 addendum 4."""
    scanner.check_start_state()   # read-only; arms/marks the session, no writes
    if scanner.session.state is SessionState.COLD:
        print(NEXT_STRIP_COLD_MSG, file=sys.stderr)
        return 1
    print("next-strip load: no jog, no reinsert prompt -- magazine must already be "
          "at the stop.")
    # Read-only evidence for Test 89: LOAD writes reg 0x32 as the literal
    # 0x1d (see the TODO in Scanner.load_magazine); the vendor's between-
    # strip load wrote 0x9d from a read of 0x9f (read & ~0x02). Log it so
    # a feed that does not engage can be judged against this register.
    print(f"reg 0x32 before the next-strip load: {scanner.io.read_reg(0x32):#04x}")
    scanner.initialize(prep=False)
    try:
        scanner.load_magazine()  # raises LoadIncompleteError/StrictPollTimeoutError
    except SafetyError as e:
        print(NEXT_STRIP_FEED_NOT_ENGAGED_MSG, file=sys.stderr)
        cause = str(e).split(". ")[0]
        if len(cause) > 120:
            cause = cause[:117] + "..."
        print(f"\n(technical cause: {type(e).__name__}: {cause})", file=sys.stderr)
        return 1
    print(f"interrupt events after the load: {scanner.io.drain_events()}")
    print("load sequence completed (status class and loader-sensor bit matched the "
          "capture after the feed, the traverse and a final read). This sets "
          "the vendor's 'loaded' indication only: check by hand that the magazine "
          "is latched before scanning -- the sensor reports presence, not latching.")
    return 0


def _report(scanner) -> None:
    if scanner is not None:
        print(scanner.session.describe_failure(), file=sys.stderr)
    else:
        print(POWER_CYCLE_INSTRUCTION, file=sys.stderr)


