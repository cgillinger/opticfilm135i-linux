"""Per-scan aperture coverage verification and crop.

docs/holder-position-design.md section 5: overscan sizing (section 4) is
a bet on an assumed ±0.5 mm worst case, not a measured ceiling. The bet
is made safe by checking the aperture's own edges in every delivered
image, so each scan proves its own coverage instead of trusting the
budget. This module is that check, plus the deterministic crop that
follows once coverage is verified.

It shares ``of135i.aperture``'s edge detector (the same half-level,
locally-adaptive crossing finder ``tools/holder_geometry.py`` uses for
the offline holder measurement) so a delivered frame is registered
against the aperture the same way the geometry itself was measured --
one implementation of "where is the plastic edge", used both to build
the geometry and to check a scan against it.

Rule (section 5):
  - exactly one aperture found, both margins >= min_margin_mm of
    overscan beyond its edges -> coverage verified, crop to the
    aperture.
  - an edge missing, or found with less than min_margin_mm slack (or a
    run that runs into the image boundary, so a bounding edge is not
    provable) -> not verified. Never delivered as if it were complete.
  - zero apertures, or more than one, in the profile -> not verified;
    the window's contents cannot be resolved to a single aperture.

The raw scan is never altered by any of this: measure_coverage only
reads it, and crop_to_aperture returns a new array/view, leaving the
caller's original data untouched -- a flagged frame's raw overscan can
always be inspected, never just discarded (section 5).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import aperture

MM_PER_INCH = 25.4


@dataclass(frozen=True)
class ApertureCoverage:
    """Verdict from :func:`measure_coverage`.

    ``leading_line``/``trailing_line`` are the aperture's edge positions
    in fractional line coordinates (axis 0), or ``None`` if no single
    aperture could be resolved. ``*_margin_mm`` are the overscan present
    beyond each edge; negative would mean the edge is off the near side
    of the image, which the edge detector cannot report (an edge only
    exists where both a lit side and a plastic side were seen), so in
    practice a too-small margin shows up as a very small non-negative
    value or as no edge found at all.
    """

    verified: bool
    leading_line: float | None
    trailing_line: float | None
    leading_margin_mm: float | None
    trailing_margin_mm: float | None
    reason: str
    threshold: float


def along_strip_profile(image, *, channel="red", band=0.5) -> np.ndarray:
    """Mean intensity per line over the central `band` of the width, on
    one channel. `channel`: "red" (default), "green", "blue", or "lum"
    (mean of RGB). Red is the default because on a colour (C-41) negative
    the aperture edge falls on the clear inter-frame rebate, whose orange
    base passes strongest in red against the ~dark plastic; on an empty
    holder every channel is saturated so red is equally fine.
    """
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f"expected a (lines, width, channels>=3) image, got shape {arr.shape}")
    n_lines, width = arr.shape[0], arr.shape[1]

    band = min(max(float(band), 0.0), 1.0)
    half = width * band / 2.0
    centre = width / 2.0
    lo = int(round(centre - half))
    hi = int(round(centre + half))
    lo = min(max(lo, 0), width)
    hi = min(max(hi, lo + 1 if width > 0 else lo), width)
    if hi <= lo:
        lo, hi = 0, width

    window = arr[:, lo:hi, :].astype(np.float64)

    if channel == "red":
        chan = window[:, :, 0]
    elif channel == "green":
        chan = window[:, :, 1]
    elif channel == "blue":
        chan = window[:, :, 2]
    elif channel == "lum":
        chan = window[:, :, :3].mean(axis=2)
    else:
        raise ValueError(f"unknown channel {channel!r}")

    if chan.size == 0:
        return np.zeros(n_lines, dtype=np.float64)
    return chan.mean(axis=1)


def measure_coverage(image, *, dpi, min_margin_mm=0.15, channel="red") -> ApertureCoverage:
    """Locate the aperture's two edges in a delivered frame and decide
    whether the whole opening was captured with at least `min_margin_mm`
    of overscan beyond each edge. Rule (section 5):
      - exactly one aperture found, both margins >= min_margin_mm ->
        verified True.
      - one aperture but a margin < min_margin_mm (or an edge at the
        very boundary) -> verified False, reason names the clipped
        side.
      - zero apertures, or more than one -> verified False, reason says
        so.
    Never raises on image content; returns a verdict.
    """
    arr = np.asarray(image)
    n_lines = arr.shape[0] if arr.ndim >= 1 else 0

    profile = along_strip_profile(arr, channel=channel)
    lines_per_mm = dpi / MM_PER_INCH

    threshold, aps = aperture.apertures(profile, lines_per_mm)

    if len(aps) == 0:
        return ApertureCoverage(
            verified=False,
            leading_line=None,
            trailing_line=None,
            leading_margin_mm=None,
            trailing_margin_mm=None,
            reason="no aperture found in profile",
            threshold=threshold,
        )
    if len(aps) > 1:
        return ApertureCoverage(
            verified=False,
            leading_line=None,
            trailing_line=None,
            leading_margin_mm=None,
            trailing_margin_mm=None,
            reason=f"{len(aps)} apertures found, expected exactly one",
            threshold=threshold,
        )

    start, end = aps[0]
    leading_margin_mm = start / lines_per_mm
    trailing_margin_mm = (n_lines - 1 - end) / lines_per_mm

    clipped = []
    if leading_margin_mm < min_margin_mm:
        clipped.append("leading")
    if trailing_margin_mm < min_margin_mm:
        clipped.append("trailing")

    if clipped:
        sides = " and ".join(clipped)
        return ApertureCoverage(
            verified=False,
            leading_line=start,
            trailing_line=end,
            leading_margin_mm=leading_margin_mm,
            trailing_margin_mm=trailing_margin_mm,
            reason=f"{sides} margin below min_margin_mm ({min_margin_mm} mm)",
            threshold=threshold,
        )

    return ApertureCoverage(
        verified=True,
        leading_line=start,
        trailing_line=end,
        leading_margin_mm=leading_margin_mm,
        trailing_margin_mm=trailing_margin_mm,
        reason="ok",
        threshold=threshold,
    )


def crop_to_aperture(image, coverage: ApertureCoverage, *, dpi, pad_mm=0.0) -> np.ndarray:
    """Return image cropped along axis 0 to [leading, trailing] (+/- pad),
    clamped to the image. Deterministic. Raises ValueError if
    coverage.leading_line/trailing_line is None. Does not require
    verified=True (caller decides); the raw image is never modified.
    """
    if coverage.leading_line is None or coverage.trailing_line is None:
        raise ValueError("coverage has no located aperture edges to crop to")

    arr = np.asarray(image)
    n_lines = arr.shape[0]
    lines_per_mm = dpi / MM_PER_INCH
    pad_lines = pad_mm * lines_per_mm

    lo = int(np.floor(coverage.leading_line - pad_lines))
    hi = int(np.ceil(coverage.trailing_line + pad_lines)) + 1  # +1: end is inclusive

    lo = min(max(lo, 0), n_lines)
    hi = min(max(hi, lo), n_lines)

    return arr[lo:hi]
