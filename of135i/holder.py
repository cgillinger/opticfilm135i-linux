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

import math
from dataclasses import dataclass

#: Motor unit: 1/7200 inch, in millimetres. Every ``_hwdpi`` quantity in
#: this module is in these units.
MM_PER_UNIT = 25.4 / 7200.0


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



#: The six apertures' measured lengths, in millimetres, frame 1..6, from
#: the vendor whole-holder sweep (docs/holder-geometry.md section 2). The
#: overscan geometry sizes the window per frame against these, not
#: against the mean, so the longest aperture (frame 1) sets the tightest
#: case.
STRIP_APERTURE_MM = (36.119, 36.047, 35.957, 35.797, 35.799, 35.860)


@dataclass(frozen=True)
class FiducialModel:
    """Where each aperture's trailing plastic edge lands, in motor units.

    A base plus a constant pitch, in 1/7200 inch from the load reference
    -- the *measured* end-to-end mapping from a commanded frame to where
    the aperture actually sits in the delivered image, not the vendor's
    nominal command grid. This is option A of docs/holder-position-
    design.md: the corrected mean mapping. It is deliberately a constant
    pitch, not a per-frame table (the per-frame means are linear to
    0.022 mm; a table would add nothing and would not touch the load-to-
    load variation either).

    ``fiducial(n)`` is the aperture *trailing* edge; the leading edge is
    that minus the frame's aperture length, so the window can be sized
    around the whole opening.
    """

    base_hwdpi: float      #: aperture-trailing position of frame 1
    pitch_hwdpi: float     #: constant step between consecutive frames
    aperture_mm: tuple[float, ...]  #: per-frame aperture length

    def trailing_hwdpi(self, frame: int) -> float:
        return self.base_hwdpi + (frame - 1) * self.pitch_hwdpi

    def leading_hwdpi(self, frame: int) -> float:
        return self.trailing_hwdpi(frame) - self.aperture_mm[frame - 1] / MM_PER_UNIT


#: The corrected mean mapping for the strip holder, fitted to the three
#: empty-holder loads of Test 56 (docs/holder-position-design.md
#: section 2.2: base 11678.3, pitch 10732.7, residual <= 0.022 mm).
#: Since the A+C migration (after Test 58's production acceptance) this
#: is THE plain-3600 positioning authority: every plain scan derives
#: its FEEDL and window from this model via overscan_geometry(). The
#: old grid (tables.FEEDL_FRAME1/FEEDL_PITCH, 6743/10760) is capture
#: ground truth and the SANE tables' interim source, not a runtime
#: alternative. Adopting this model reopened frames 2-4 for the
#: positioning requirement; the one-load 1-6 empty-holder regression
#: covers that (design doc section 9 step 3).
STRIP_FIDUCIAL = FiducialModel(
    base_hwdpi=11678.3,
    pitch_hwdpi=10732.7,
    aperture_mm=STRIP_APERTURE_MM,
)


@dataclass(frozen=True)
class OverscanGeometry:
    """The scan window for one frame, sized to contain the whole aperture
    plus an overscan margin on each side, ready to hand to the engine.

    ``feedl`` is the commanded positioning target (window centre, the
    driver's convention); ``wire_lines`` is the line count to program and
    ``chunks`` the number of image chunks to read; ``delivered_lines`` is
    what survives the colour-line crop. The ``*_margin_mm`` fields are the
    guaranteed slack between the aperture edge and the delivered window
    edge on the *mean* mapping -- the leading one is exact (set by FEEDL),
    the trailing one is >= the target (the line count rounds up to whole
    chunks). ``end_hwdpi`` is the furthest motor position the pass
    reaches, checked against the transport bound.
    """

    frame: int
    feedl: int
    wire_lines: int
    chunks: int
    delivered_lines: int
    leading_margin_mm: float
    trailing_margin_mm: float
    end_hwdpi: float


#: Default overscan slack per side, millimetres. Covers the +/-0.5 mm
#: design worst case (2x the observed load spread at n=3) with 0.25 mm to
#: spare; docs/holder-position-design.md section 4.
OVERSCAN_MM = 0.75


def overscan_geometry(
    frame: int,
    *,
    res_units_per_line: int,
    chunk_lines: int,
    colour_crop_lines: int,
    fiducial: FiducialModel | None = None,
    overscan_mm: float = OVERSCAN_MM,
    holder: Holder = DEFAULT,
) -> OverscanGeometry:
    """Size the scan window for ``frame`` to cover the whole aperture plus
    ``overscan_mm`` on each side, and return the engine parameters.

    ``res_units_per_line`` is 7200/dpi (motor units per delivered line),
    ``chunk_lines`` the image-chunk quantum (t.LINES_PER_CHUNK), and
    ``colour_crop_lines`` the per-side loss to align_channels (its
    ``shift``). The frame number is range-checked first, and the furthest
    motor position the pass reaches is range-checked against
    FEEDL_CEILING before the geometry is returned -- so a window that
    would drive past proven travel is refused before any write, exactly
    as a bad FEEDL is.

    Leading coverage is a FEEDL move (continuous); trailing coverage is a
    line-count move (rounded UP to whole chunks, which can only add
    margin). The two are computed separately so the mean-mapping centring
    is never miscounted as overscan. See docs/holder-position-design.md
    section 4 for the derivation this implements.
    """
    check_frame(frame, holder)
    fid = fiducial if fiducial is not None else STRIP_FIDUCIAL
    over = overscan_mm / MM_PER_UNIT

    lead = fid.leading_hwdpi(frame)
    trail = fid.trailing_hwdpi(frame)
    want_start = lead - over
    want_end = trail + over

    # Trailing side: line count sets the window end. The wire must carry
    # the delivered span plus the two crop strips align_channels eats.
    span = want_end - want_start
    delivered_needed = span / res_units_per_line
    wire_needed = delivered_needed + 2 * colour_crop_lines
    chunks = math.ceil(wire_needed / chunk_lines)
    wire_lines = chunks * chunk_lines
    delivered_lines = wire_lines - 2 * colour_crop_lines

    # Leading side: place window start exactly at want_start. The window
    # centre (= commanded FEEDL, driver convention) is start + half-window.
    half = delivered_lines / 2 * res_units_per_line
    feedl = round(want_start + half)
    end_hwdpi = want_start + delivered_lines * res_units_per_line

    # Guard the furthest reached position, not only the stop.
    check_feedl(round(end_hwdpi), holder)

    return OverscanGeometry(
        frame=frame,
        feedl=feedl,
        wire_lines=wire_lines,
        chunks=chunks,
        delivered_lines=delivered_lines,
        leading_margin_mm=overscan_mm,  # exact by construction (FEEDL move)
        trailing_margin_mm=(end_hwdpi - trail) * MM_PER_UNIT,
        end_hwdpi=end_hwdpi,
    )



def dual_overscan_geometry(
    frame: int,
    *,
    dpi: int,
    lines_per_chunk: int,
    colour_crop_lines: int,
    default_wire_lines: int,
    fiducial: FiducialModel | None = None,
    overscan_mm: float = OVERSCAN_MM,
    holder: Holder = DEFAULT,
) -> tuple[OverscanGeometry, int]:
    """A+C geometry for a dual-light profile: the same overscan_geometry,
    expressed in VISIBLE lines, plus the wire register line count.

    A dual scan interleaves one infrared and one visible line per
    physical line position, so the transport advances (7200/dpi)/2
    motor units per WIRE line and 7200/dpi per VISIBLE line -- the
    visible array has the nominal line density (protocol-notes.md pass
    18; verified against every module's DEFAULT_LINES <-> ~37 mm
    window). The window arithmetic is therefore the plain one in
    visible lines, with the chunk quantum halved: `lines_per_chunk`
    (the module's WIRE lines per image chunk) is even in every profile
    (98/48/16/16/8), so lines_per_chunk//2 visible lines per chunk is
    exact and any chunk multiple keeps the wire count even -- the
    IR/visible parity of the buffer is preserved by construction.

    Returns (geometry, wire_lines): `geometry` is in visible lines
    (delivered_lines is what align_channels leaves per channel);
    `wire_lines` is the alternating-line count to program into the
    24-bit line register and equals geometry.chunks * lines_per_chunk.
    The same STRIP_FIDUCIAL maps every profile: it was measured on the
    600 dpi dual profile (Test 56) and cross-validated on plain 3600
    (Tests 57-59); positions are motor units, profile-independent.

    FEEDL ANCHORING (empirical, dual passes 1-2 of 2026-09-10): the
    dual engine anchors the acquisition START at FEEDL minus a FIXED
    per-profile constant K equal to the captured default window's
    half-length -- it does NOT centre the commanded window the way the
    plain 3600 pass verifiably does (plain: the FEEDL-to-start offset
    grew with half the window growth across Tests 57-60; dual 600: it
    stayed at the default half, 5287 measured vs 5292 predicted, while
    the window grew 270 units, and dual 2400's measured 5322 also sits
    on its default half 5316). So the dual FEEDL here is
    want_start + K, K = default_wire_lines x units_per_wire_line / 2,
    with `default_wire_lines` the module's captured DEFAULT_LINES. The
    OverscanGeometry.feedl field is overridden accordingly; margins
    and the end position are starts/lengths and are unaffected.
    """
    if lines_per_chunk % 2:
        raise ValueError(
            f"lines_per_chunk {lines_per_chunk} is odd: a dual chunk must "
            f"hold whole IR/visible line pairs")
    if 7200 % dpi:
        raise ValueError(f"dpi {dpi} does not divide the 7200 dpi motor base")
    geom = overscan_geometry(
        frame,
        res_units_per_line=7200 // dpi,
        chunk_lines=lines_per_chunk // 2,
        colour_crop_lines=colour_crop_lines,
        fiducial=fiducial,
        overscan_mm=overscan_mm,
        holder=holder,
    )
    # Re-anchor FEEDL: want_start is what overscan_geometry placed the
    # window start at (centre convention); recover it and add K.
    units_per_vis = 7200 // dpi
    want_start = geom.feedl - geom.delivered_lines / 2 * units_per_vis
    k_units = default_wire_lines * (units_per_vis / 2) / 2
    feedl = round(want_start + k_units)
    check_feedl(feedl, holder)
    geom = OverscanGeometry(
        frame=geom.frame,
        feedl=feedl,
        wire_lines=geom.wire_lines,
        chunks=geom.chunks,
        delivered_lines=geom.delivered_lines,
        leading_margin_mm=geom.leading_margin_mm,
        trailing_margin_mm=geom.trailing_margin_mm,
        end_hwdpi=geom.end_hwdpi,
    )
    return geom, geom.chunks * lines_per_chunk
