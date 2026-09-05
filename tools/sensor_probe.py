#!/usr/bin/env python3
"""Read-only loader-sensor probe: where does the sensor trip?

Polls, in a READ-ONLY session (no start-state arming, no writes, no
SET_CONFIGURATION -- exactly like `of135i status`/`doctor`), the
loader sensor (reg 0x101 bit 0x08), the full status byte, reg 0x32
and the interrupt endpoint, and prints every transition with a
timestamp. Purpose (docs/load-analysis.md, TODO 9b): with the scanner
powered on and NO driver activity, insert the magazine slowly by hand
and note where the sensor trips relative to the mechanical stop.
(Test 13 located it ~1 cm short of the stop; Test 14 then showed the
vendor loads from the STOP -- the insertion depth was never the
variable. Kept as a diagnostic.)

Zero USB writes by construction (safety.GuardedDevice refuses them in
a read-only session); proven offline in tests/test_safety.py.

    .venv/bin/python tools/sensor_probe.py [--seconds 60] [--hz 10]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from of135i.usbio import InterruptOverflowError, UsbIo, Of135iError  # noqa: E402

_BUTTON_NAMES = {0x48: "eject-button", 0x04: "sensor-event"}


def probe(io, seconds: float, hz: float, out=None) -> list[dict]:
    """Poll and print transitions; returns the transition log."""
    out = out or sys.stdout
    period = 1.0 / hz
    t0 = time.monotonic()
    last = None
    log: list[dict] = []

    def emit(t, **fields):
        rec = {"t": round(t, 2), **fields}
        log.append(rec)
        print("  ".join(f"{k}={v}" for k, v in rec.items()), file=out, flush=True)

    print(f"probing for {seconds:.0f}s at {hz:g} Hz (read-only). Insert the magazine "
          f"SLOWLY; note the position at each transition.", file=out, flush=True)
    overflow = False
    while True:
        t = time.monotonic() - t0
        r101 = io.read_ext_reg(0x101)
        r32 = io.read_reg(0x32)
        state = (bool(r101 & 0x08), r101, r32)
        if state != last:
            emit(t, present=state[0], reg101=f"0x{r101:02x}", reg32=f"0x{r32:02x}")
            last = state
        if not overflow:
            try:
                button = io.read_button(timeout_ms=10)
            except InterruptOverflowError as e:
                overflow = True
                emit(t, event=f"interrupt endpoint overflow ({e}); event reads stopped")
            else:
                if button is not None:
                    emit(t, event=_BUTTON_NAMES.get(button, f"0x{button:02x}"))
        if t >= seconds:
            break
        time.sleep(period)
    print(f"done: {len(log)} transition(s)/event(s); session state {io.session.state.value}, "
          f"writes {io.session.writes}", file=out, flush=True)
    return log


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--hz", type=float, default=10.0)
    args = ap.parse_args(argv)
    try:
        with UsbIo.open(readonly=True) as io:
            probe(io, args.seconds, args.hz)
    except KeyboardInterrupt:
        print("\ninterrupted (read-only session; nothing was written)", file=sys.stderr)
        return 130
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
