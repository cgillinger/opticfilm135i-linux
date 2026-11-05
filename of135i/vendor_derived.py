"""Facts learned from static analysis of the vendor's Windows software.

It holds a few hypotheses so far (finding SA-002, at the end of the file).
The rules for what may live here (policy: docs/static-analysis.md):

  * It holds ONLY values, conditions and orderings learned from static
    analysis of the vendor's Windows software, done for interoperability.
    Every entry carries a comment pointing to its finding in
    docs/static-analysis.md (an ID such as SA-001).
  * Facts only: register values, conditions, orderings. Never copied code
    and never code structure.
  * Every entry is a HYPOTHESIS until confirmed on hardware.
  * This module must never be imported, directly or transitively, by
    tools/gen_sane_tables.py, and nothing under sane/ may reference it.
    A value moves to the SANE side only after it is confirmed by our own
    USB capture and a hardware test; the capture is then the source, and
    the value lives in the normal capture-derived modules.
  * The rules are enforced by tests/test_vendor_isolation.py.
"""

from __future__ import annotations

# Entries go below, each as:
#
#   # SA-001 (docs/static-analysis.md) -- hypothesis
#   NAME = value

# SA-002 (docs/static-analysis.md) -- hypothesis
REG101_PRESENT_BIT = 0x08

# SA-002 (docs/static-analysis.md) -- hypothesis
REG101_MOTOR_BIT = 0x01

# SA-002 (docs/static-analysis.md) -- hypothesis
WATCH_REGS = (0x31, 0x32, 0x35, 0x37, 0x100, 0x101)
