"""Exception base class for the of135i driver.

Kept in its own module so that both the transport layer (usbio.py)
and the hardware-safety layer (safety.py) can derive from it without
importing each other.
"""


class Of135iError(RuntimeError):
    """Raised for driver-level failures specific to this scanner."""
