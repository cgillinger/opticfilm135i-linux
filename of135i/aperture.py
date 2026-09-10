"""Finding the holder's aperture edges in an along-strip light profile.

The plastic holder blocks light; each open aperture is a lit run bounded
by two plastic edges. Given a per-line light profile (one value per scan
line, brighter = more light through), this module locates those edges to
a fraction of a line and returns the lit runs long enough to be frame
apertures.

It is the single source of that geometry. ``tools/holder_geometry.py``
uses it for the offline measurement of a whole holder; ``of135i/
aperture_crop.py`` uses it on a delivered frame to verify that the whole
aperture was captured and to crop to it (docs/holder-position-design.md
section 5). Keeping one implementation means the edge a delivered frame
is cropped against is found the same way as the edge the geometry was
measured against.

The method (why a global threshold does not survive real data, why the
floor is a low percentile, how a transition is placed against its local
light levels) is documented on ``edges`` below.
"""

from __future__ import annotations

import numpy as np

#: The shortest lit run that counts as a frame aperture, millimetres. A
#: 35 mm frame's aperture is ~36 mm; the crossbars are ~2 mm. This only
#: has to separate the two.
MIN_APERTURE_MM = 25.0

#: How far either side of a transition the local light levels are taken
#: from, in lines, and how many lines next to the transition itself are
#: skipped (the ramp is one or two lines wide).
LOCAL_SPAN = 20
LOCAL_SKIP = 2
#: A candidate edge must move at least this fraction of the local
#: lit-to-plastic range in a single line. The real ramp is two or three
#: lines wide on the vendor's own scans, so a single line carries about
#: a third of the drop; anything below a quarter is illumination.
EDGE_GRADIENT = 0.25
#: ... and the levels either side of the refined transition must really
#: be a lit plateau and the plastic, not two parts of the same plateau.
EDGE_CONTRAST = 0.5
#: One side of a holder edge is the opaque plastic, which reads at the
#: scan's black level. Film in the aperture produces its own strong
#: transitions -- a dark subject against a bright one -- and on the
#: first hardware run two frames had several of those mid-window. They
#: are not holder edges: neither of their sides is anywhere near black.
#: A transition counts only if one side is within this fraction of the
#: lit-to-black range of the floor.
PLASTIC_LEVEL = 0.15
#: Percentile taken as the black level. See the note in edges().
FLOOR_PERCENTILE = 1.0


def _rolling_median(prof, span, offset):
    """Median of `span` samples starting `offset` away from each index.

    Used to read the light level just to one side of a candidate edge
    without letting the edge itself into the window.
    """
    n = len(prof)
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        if offset < 0:
            lo, hi = max(0, i + offset - span + 1), max(1, i + offset + 1)
        else:
            lo, hi = min(n - 1, i + offset), min(n, i + offset + span)
        out[i] = np.median(prof[lo:hi]) if hi > lo else prof[i]
    return out


def edges(profile, threshold=None):
    """Sub-sample positions of the aperture edges, on local light levels.

    A global threshold does not survive real data. Illumination varies
    along a scan -- on an empty holder the driver's own per-frame gain
    calibration sees a blank field and lands somewhere different every
    time, and within one frame the lit level can fall by a third from
    one end to the other. A threshold taken from the whole profile then
    sits close to the dim end's plateau and reports an edge tens of
    lines away from the real one, or invents one in the middle of an
    open aperture. (Observed on the first empty-holder run: frame 6's
    trailing edge came out 21 lines early that way.)

    So each transition is found by its gradient and then placed against
    the light levels immediately on either side of it: threshold =
    halfway between the plateau before and the plateau after, crossing
    interpolated between the two samples that straddle it. Whether the
    aperture is lit to 8000 counts or 27000 makes no difference.

    Passing `threshold` forces the old global behaviour, for a caller
    that wants one fixed level.

    Returns (representative threshold, [(position, rising), ...]).
    """
    prof = np.asarray(profile, dtype=np.float64)
    n = len(prof)
    if n < 3:
        return 0.0, []
    if threshold is not None:
        above = prof > threshold
        out = []
        for i in range(1, n):
            if above[i] != above[i - 1]:
                y0, y1 = float(prof[i - 1]), float(prof[i])
                frac = (threshold - y0) / (y1 - y0) if y1 != y0 else 0.5
                out.append((i - 1 + frac, bool(above[i])))
        return threshold, out

    d = np.diff(prof)
    if float(prof.max() - prof.min()) <= 0:
        return 0.0, []
    # A real aperture edge moves most of the way from the lit level to
    # the plastic in one or two lines. "Most of the way" has to be
    # measured against the LOCAL lit level, not the profile's overall
    # range: on the vendor's whole-holder sweep an empty aperture reads
    # 39800 and a film-filled one 34500, and a global rule tuned to the
    # brightest part misses the edges of the dimmer ones entirely.
    # The scan's black level, read off the plastic. It has to come from
    # a percentile low enough to be inside the crossbar: in a
    # single-frame window the plastic is only three or four per cent of
    # the lines, so a fifth percentile lands in the picture instead and
    # every rule below is then measured against the wrong floor.
    floor = float(np.percentile(prof, FLOOR_PERCENTILE))
    lit = np.maximum(
        _rolling_median(prof, LOCAL_SPAN, -LOCAL_SKIP),
        _rolling_median(prof, LOCAL_SPAN, +LOCAL_SKIP))[:len(d)]
    cand = np.flatnonzero(np.abs(d) > EDGE_GRADIENT * np.maximum(lit - floor, 1.0))
    groups = []
    for i in cand:
        if groups and i - groups[-1][-1] <= 2:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])

    out, levels = [], []
    for g in groups:
        i0, i1 = g[0], g[-1]
        rising = bool(prof[min(i1 + 1, n - 1)] > prof[i0])
        left = prof[max(0, i0 - LOCAL_SKIP - LOCAL_SPAN):max(1, i0 - LOCAL_SKIP + 1)]
        right = prof[min(n - 1, i1 + 1 + LOCAL_SKIP):
                     min(n, i1 + 1 + LOCAL_SKIP + LOCAL_SPAN)]
        if left.size == 0 or right.size == 0:
            continue
        lo_lvl, hi_lvl = float(np.median(left)), float(np.median(right))
        local_lit = float(np.max(lit[max(0, i0 - 1):i1 + 2]))
        if abs(hi_lvl - lo_lvl) < EDGE_CONTRAST * max(local_lit - floor, 1.0):
            continue
        # One side must be the plastic itself, not merely darker.
        if min(lo_lvl, hi_lvl) > floor + PLASTIC_LEVEL * max(
                local_lit - floor, 1.0):
            continue
        th = (lo_lvl + hi_lvl) / 2.0
        levels.append(th)
        # Walk out from the transition to the pair of samples the
        # threshold falls between; the ramp is one or two lines wide.
        pos = None
        for i in range(max(1, i0 - 2), min(n, i1 + 4)):
            y0, y1 = float(prof[i - 1]), float(prof[i])
            if (y0 - th) * (y1 - th) <= 0 and y0 != y1:
                pos = i - 1 + (th - y0) / (y1 - y0)
                break
        if pos is not None:
            out.append((pos, rising))
    out.sort()
    return (float(np.median(levels)) if levels else 0.0), out


def apertures(profile, lines_per_mm, min_mm=MIN_APERTURE_MM, threshold=None):
    """The lit runs long enough to be frame apertures, as (start, end)."""
    threshold, ed = edges(profile, threshold)
    runs = []
    for (a, rising_a), (b, rising_b) in zip(ed, ed[1:]):
        if rising_a and not rising_b:
            runs.append((a, b))
    # a run that starts before the profile does, or ends after it, is
    # only bounded on one side; both bounds must be real edges.
    keep = [(a, b) for a, b in runs if (b - a) / lines_per_mm >= min_mm]
    return threshold, keep
