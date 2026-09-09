"""The film holders: how many frames one holds, and how far apart they are.

A frame number is not a free integer. It is an index into a physical
plastic holder with a fixed number of apertures, and every frame number
the driver accepts turns into an absolute motor target (FEEDL) that the
transport will drive to. An out-of-range frame number must therefore be
refused *before* the first write, not discovered by the carriage.

This module is the single authority on that: the holder's frame count,
and the ceiling no positioning target may exceed. Both the driver
(of135i/tables*.py, of135i/device.py) and the SANE backend
(sane/gl126_ops.cpp) enforce the same two limits.

The evidence
------------

**Six apertures, measured.** The vendor's whole-holder pass at 600 dpi
(`captures/20260902-vendor-600dpi.pcap`, image data in the private
analysis area) scans the entire magazine travel in one sweep, so the
holder's own plastic is imaged end to end. Measured with
tools/holder_geometry.py on the infrared pass, where the film base is
transparent and only the holder blocks light:

    aperture   length      crossbar after    pitch to next
       1       36.12 mm        1.90 mm         38.005 mm
       2       36.05 mm        1.92 mm         37.927 mm
       3       35.96 mm        1.97 mm         37.851 mm
       4       35.80 mm        1.97 mm         37.765 mm
       5       35.80 mm        2.00 mm         37.825 mm
       6       35.86 mm          --               --

Six apertures, evenly spaced to within 3.1 lines (0.13 mm) of a
constant pitch. Positions 5 and 6 were empty in that capture (the strip
held four frames), which is exactly why the aperture edges there are
clean: nothing but the holder was in the light path.

**The pitch, from the vendor's own positioning commands.** The vendor
apps position with a mode-0x18 motor batch carrying FEEDL in registers
0x3d/0x3e/0x3f. Their *nominal* grid -- the preview pass, before any
film-edge detection -- is a constant step, seen identically in three
independent captures:

    20260829-session3-vackning   6414, 17166, 27918, 38670
    20260829 02-preview-magasin  6414, 17166
    20260905-vuescan-3600-ir     6414, 17166, 27918, 38670

Every step is exactly 10752. The SilverFast trace's frame 3 (28052)
sits exactly on the same grid from its own base (6548 + 2 x 10752), and
the WIA batch capture drove to frames 5 and 6 at 49796 and 60174 -- the
latter exactly 6414 + 5 x 10752. The scan-pass values scatter by up to
30 steps (0.1 mm) around the grid because those apps re-detect the film
edge per frame; the grid itself does not move.

FEEDL_PITCH in the table modules is 10760, from the older pass-3
reading of that same SilverFast trace (frame 4 - frame 1 = 3 x 10760,
a film-detected value). The difference is 8 steps per frame, 0.028 mm
-- 0.14 mm accumulated at frame 6. Which of the two the driver should
use is decided by the empty-holder measurement, not by this comment.

**Frames 5 and 6 are inside the transport's proven envelope.** The
largest motor target this unit has ever been commanded to is the load
flow's traverse, FEEDL 71490 (~252 mm), run on every single load. Frame
6 is at most 60546 -- 11000 steps (39 mm) short of it. The WIA capture
additionally drove to frames 5 and 6 directly.

What is *not* established here is that our own driver's frame 5 and 6
land centred in their apertures. That is a geometry measurement on
hardware (an empty holder, one scan per position, the aperture edges
read straight out of each delivered frame), not something this module
can assert.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Holder:
    """A physical holder: what the driver may be asked to position to.

    ``frames`` is the number of apertures, i.e. the largest frame number
    the driver will accept. ``aperture_mm`` and ``pitch_mm`` are the
    measured geometry (None where it has not been measured).
    ``geometry_source`` says where those numbers came from, so a reader
    can tell a measurement from a nominal figure.
    """

    name: str
    frames: int
    aperture_mm: float | None = None
    pitch_mm: float | None = None
    pitch_hwdpi: int | None = None
    geometry_source: str = "not measured"


#: The standard 35 mm strip holder that ships with the scanner. Six
#: apertures, measured above.
#:
#: Note that ``pitch_hwdpi`` here (10752, the vendor's own nominal grid
#: step, seen exactly seven times) is NOT what the table modules use:
#: their ``FEEDL_PITCH`` is still 10760, and changing it is an open
#: decision, not a fix. This field records what the holder measures;
#: FEEDL_PITCH records what the driver currently commands. They differ
#: by 8 steps per frame, 0.14 mm accumulated at frame 6, and the
#: empty-holder run settles which is right (docs/holder-geometry.md
#: sections 5 and 8).
STRIP = Holder(
    name="35 mm strip holder",
    frames=6,
    aperture_mm=35.92,    # mean of the six measured apertures
    pitch_mm=37.947,      # 10752 / 7200 in; the optical mean is 37.875
    pitch_hwdpi=10752,
    geometry_source="tools/holder_geometry.py on the vendor 600 dpi "
                    "whole-holder pass, plus the vendor's own nominal "
                    "FEEDL grid (docs/holder-geometry.md)",
)

#: The mounted-slide holder that ships in the same box. Four openings
#: by inspection of the part; nothing about it has been captured,
#: measured or driven, so it carries no geometry. It is defined here
#: only so that a future holder option has somewhere to land -- the
#: driver does not select it and has never positioned with it.
SLIDE = Holder(
    name="mounted-slide holder",
    frames=4,
    geometry_source="none: no capture, no measurement, never loaded",
)

#: What the driver assumes when nothing says otherwise. There is no
#: holder-type detection on this scanner (no register has ever been
#: seen to report one), so this is an assumption, not a reading.
DEFAULT = STRIP

#: The largest absolute positioning target the driver will issue, in
#: 1/7200 inch from home. This is the load flow's traverse -- the
#: longest move the vendor has ever commanded on this unit, run on
#: every load -- so a target at or below it is inside travel that is
#: demonstrated, not assumed. It is an independent second guard: it
#: catches a bad FEEDL however it was computed, including one that came
#: from a valid frame number and a wrong table.
FEEDL_CEILING = 71490


def check_frame(frame: int, holder: Holder = DEFAULT) -> int:
    """Return ``frame`` if the holder has it; raise otherwise.

    Called from every feedl_for_frame() so that no code path can turn an
    out-of-range frame number into a motor target. Raises before any
    write, so nothing has been sent to the scanner when it does.
    """
    from .safety import FrameOutOfRangeError

    if not isinstance(frame, int) or isinstance(frame, bool):
        raise FrameOutOfRangeError(
            f"frame must be an integer, got {frame!r}",
            frame=frame, holder=holder.name, frames=holder.frames)
    if frame < 1 or frame > holder.frames:
        raise FrameOutOfRangeError(
            f"frame {frame} is outside the {holder.name}: it holds "
            f"{holder.frames} frames (1-{holder.frames}). Nothing was "
            f"sent to the scanner.",
            frame=frame, holder=holder.name, frames=holder.frames)
    return frame


def check_feedl(feedl: int, holder: Holder = DEFAULT) -> int:
    """Return ``feedl`` if it is inside the proven travel; raise otherwise.

    The second guard, independent of the frame number: a positioning
    target must be a forward move (>= 1) and must not exceed
    FEEDL_CEILING, the longest move this unit is known to make.
    """
    from .safety import FeedlOutOfRangeError

    if feedl < 1 or feedl > FEEDL_CEILING:
        raise FeedlOutOfRangeError(
            f"positioning target {feedl} is outside the proven travel "
            f"(1..{FEEDL_CEILING}, 1/7200 in from home). Nothing was "
            f"sent to the scanner.",
            feedl=feedl, ceiling=FEEDL_CEILING)
    return feedl


def frames(holder: Holder = DEFAULT) -> range:
    """Every frame the holder has, in order -- for batch defaults."""
    return range(1, holder.frames + 1)
