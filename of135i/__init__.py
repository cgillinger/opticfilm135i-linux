"""Userspace driver for the Plustek OpticFilm 135i (GL126) film scanner."""

from .usbio import UsbIo

__version__ = "0.2.11"
__codename__ = "Second Scanner Gamble"

__all__ = ["UsbIo", "__version__", "__codename__"]
