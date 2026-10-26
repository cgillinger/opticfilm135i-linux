"""110 (Pocket Instamatic) frame detector.

docs/film-110-proposal.md: 110 support is a film model
(``holder.FILM_110``) plus an image-side detector on top of the existing
aperture pipeline. The motor side is untouched -- this module only reads
an already aperture-registered image (the output of
``aperture_crop.crop_to_aperture``) and finds where, inside that single
physical aperture, the 110 strip's own images and perforations fall.

Why perforations are the primary fiducial: thresholding the image content
itself against the clear film base fails on thin (shadow) frames and on
skies alike -- both look too much like the base at a naive threshold.
The perforation is the one feature that is reliably at the air level
(nothing between the sensor and the lamp there), so every image position
is *predicted* from a nearby hole and only *refined* (by a bounded
amount) against the image content -- never re-derived from it.

Axis convention (matching ``aperture_crop``/``aperture``): axis 0 of the
input image is along the transport (the line axis), axis 1 is lateral
(across the film, the column axis). All internal positions are in those
same units (fractional lines / fractional columns), the same convention
``ApertureCoverage.leading_line`` uses -- millimetres are only used for
constants and for the caller-facing ``size_mm`` field.

Every threshold below is relative to levels measured on the image itself
(the "air" level, mainly), not a fixed absolute count, so the detector is
insensitive to per-frame gain/exposure the same way ``of135i.aperture``
is. Never raises on image content: a frame that cannot be resolved comes
back with ``reason`` set and ``frames`` empty; the aperture product this
sits on top of is unaffected either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import warnings

import numpy as np

from .holder import Film, FILM_110, STRIP

MM_PER_INCH = 25.4

# --------------------------------------------------------------- thresholds
#
# All are the proposal's own numbers (docs/film-110-proposal.md, and the
# task spec that refined it) except where noted "(tuned)" -- those are
# genuinely mine, made against the fixtures, and documented as such.

AIR_PERCENTILE = 99.5          # column-median percentile defining "air"
AIR_FRAC = 0.95                 # >= this fraction of air -> "air" column
PLASTIC_FRAC = 0.05              # <= this fraction of air -> "plastic" column (real rail: 0.033)
PLASTIC_MIN_RUN_MM = 2.0         # shorter dark runs are film (the 110 printed border reads 0.066)
FILM_WIDTH_TOLERANCE_MM = 1.0
# A 16 mm strip lies loose across the 24 mm opening; its far edge can slide
# under the rail on that side (Test 103, 2026-10-21: 0.6 mm for one strip,
# 2.1 mm for the other), so the visible band is narrower than the film by
# that much. Accepted up to this, when the narrow side abuts plastic (the
# perforated edge must still face air); the hidden width is reported as
# `rail_hidden_mm` and the picture is clipped by whatever of its own margin
# lies under the rail.
RAIL_HIDDEN_MAX_MM = 3.0
FILM_PRESENT_FRAC = 0.85        # < this fraction of air -> film present (a line)
HOLE_EDGE_MARGIN_MM = 2.5        # lateral search band next to the perforated edge
HOLE_FRAC = 0.95                # >= this fraction of air -> hole (air-level) line
HOLE_MIN_MM = 0.8
HOLE_MAX_MM = 4.0
# (tuned) A run of "film present" shorter than this is noise, not a real
# free end or continuation: the aperture crop's very first (and last)
# line can be a single-line partial-pixel blend of plastic and air right
# at the crop boundary, which reads dark enough to look like "film
# present" for exactly one line (observed on real hardware data,
# 2026-09-19: svep600-f1.tiff's line 0 alone, followed by ~300 genuinely
# clear-film lines). Below this, a "touches line 0 / touches the last
# line" run is not trusted.
FILM_PRESENT_MIN_RUN_MM = 0.15
REFINE_WINDOW_MM = 0.5
REFINE_SMOOTH_MM = 0.1
REFINE_NEIGHBOURHOOD_MM = 3.0
REFINE_GRADIENT_RATIO = 3.0
FOOT_WALK_MM = 0.6              # how far a refined edge may walk out to the transition's foot
FOOT_STOP_FRAC = 0.10           # ... while the step per pixel stays above this fraction of the peak
LEVEL_NEAR_MM = 0.3             # the inside/outside levels that decide an edge's step direction
LEVEL_FAR_MM = 1.0              # ... are medians over this band either side of the predicted edge
LEVEL_MIN_CONTRAST = 0.08       # below this relative level difference nothing is refined (real 110 edges: > 0.6)
WHOLE_MARGIN_MM = 0.3
EDGE_CUT_MAX_MM = 0.5            # a near-whole copy hides at most this much under the plastic
LEADING_CONTINUATION_MM = 1.0
LEADING_CONTINUATION_FRAC = 0.8
CLEAR_BASE_PERCENTILE = 99.0

# Design choices not dictated by the spec, made here and documented:
#  - all "levels" (air, film presence, holes, gradients) are read from the
#    GREEN channel, the same choice step (a) makes explicit for the "air"
#    level -- kept for every other level so one aperture is judged on one
#    consistent scale.
#  - the ambiguous case (film band touching air on BOTH lateral edges):
#    each edge's hole search is tried and the one that finds valid runs
#    wins; if both do (not expected on a real 16 mm-in-24 mm-aperture
#    strip), the low-column edge wins -- documented, not exercised by any
#    known fixture.
#  - candidate sub-pixel position at a refined edge is the midpoint of
#    the strongest-gradient sample pair (index + 0.5), which is accurate
#    to about one line/column -- comfortably inside the +/-0.3 mm
#    acceptance band at both 600 and 3600 dpi.


@dataclass
class FrameFind:
    """One predicted 110 image inside a single aperture.

    ``index`` is 1-based, in perforation order, *within this aperture
    only* (a running image number across a whole scan is the CLI's job,
    not this module's -- see cli.py). ``line0``/``line1``/``col0``/
    ``col1`` are fractional positions in the input image's own axes
    (line0/line1 may lie outside ``[0, n_lines)`` for a split image).
    ``refined`` maps each edge name ("line0", "line1", "col0", "col1") to
    whether local refinement moved it (False = the perforation-based
    prediction stands, unrefined).

    ``located_by`` says which locator found the image: "holes" (today's
    perforation-based prediction), "frame" (the printed border between
    pictures only; ``perforation_line`` is then None and ``orientation``
    "unknown") or "holes+frame" (both agree).
    """

    index: int
    perforation_line: "float | None"
    line0: float
    line1: float
    col0: float
    col1: float
    whole: bool
    split: "str | None"
    refined: dict
    free_end_near: bool
    size_mm: tuple
    orientation: str
    col_clamp: "tuple | None" = None   # (lo, hi): the film band minus RAIL_EDGE_INSET_MM; `crop` never pads past it
    located_by: str = "holes"          # "holes" | "frame" (printed border only) | "holes+frame"
    # Near-whole: not `whole` only because ONE along-transport edge lies within
    # [-EDGE_CUT_MAX_MM, WHOLE_MARGIN_MM) of the aperture boundary (the other
    # confirmed): `cut_mm` is how much of the image is hidden under the plastic
    # (boundary minus predicted edge, >= 0). None = not near-whole. The CLI
    # delivers such a copy (docs/film-110.md section 15.6).
    cut_mm: "float | None" = None
    # Distance (mm) from a free strip end seen in this aperture to this image's
    # nearest edge, when the end lies within a film pitch of the image.
    end_gap_mm: "float | None" = None


@dataclass
class ApertureFind:
    """The detector's verdict for one aperture."""

    empty: bool
    film_cols: "tuple | None"
    film_line_start: "float | None"
    film_line_end: "float | None"
    leading_continuation: bool
    frames: list = field(default_factory=list)
    reason: str = ""
    rail_hidden_mm: float = 0.0        # film width hidden under a rail (0 = fully visible)
    film_runs: list = field(default_factory=list)   # [(first_line, last_line)] of film present
    # Unexposed frame positions the frame locator saw: [(line0, line1)] of a
    # window between printed-border gaps that reads as clear film (plausibility
    # test failed), lying wholly on film. They consume a number, never a product.
    blanks: list = field(default_factory=list)


# ------------------------------------------------------------- small helpers


def _longest_true_run(mask: np.ndarray):
    """(start, end_exclusive) of the longest run of True in `mask`, or
    None if there is none."""
    best = None
    n = len(mask)
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if best is None or (j - i) > (best[1] - best[0]):
                best = (i, j)
            i = j
        else:
            i += 1
    return best


def _true_runs(mask: np.ndarray):
    """[(start, end_inclusive), ...] of every run of True in `mask`."""
    runs = []
    n = len(mask)
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    return runs


def _box_smooth(profile: np.ndarray, box: int) -> np.ndarray:
    prof = np.asarray(profile, dtype=np.float64)
    if box <= 1 or prof.size == 0:
        return prof
    kernel = np.ones(box, dtype=np.float64) / box
    return np.convolve(prof, kernel, mode="same")


def _refine_edge(profile: np.ndarray, predicted: float, px_per_mm: float,
                 outward: int, limit_lo: float | None = None,
                 limit_hi: float | None = None) -> tuple:
    """Refine one edge within +/-REFINE_WINDOW_MM of `predicted` against
    `profile` (a 1-D line- or column-level profile). `outward` says on
    which side of the edge the OUTSIDE of the image lies: < 0 at lower
    indices (the image starts here), > 0 at higher indices (the image
    ends here). `limit_lo`/`limit_hi` clip the search window. Returns
    (position, refined_bool).

    The direction of the step is NOT assumed -- it is read from the
    profile itself: the median level LEVEL_NEAR_MM..LEVEL_FAR_MM inside
    the predicted edge against the same band outside it. On 110 film the
    picture sits inside a pre-exposed dark printed border on all four
    sides, so leaving the picture gets DARKER; on a 35 mm-like clear
    surround it gets brighter. The first version of this function assumed
    the clear case and, on the real 110 strip, locked onto the fall-off
    of the bright gate-edge halo just INSIDE the picture on every edge
    (0.1-0.4 mm of picture cut on the trailing edges; found by Astra's
    review, measured 2026-09-20). If the two levels do not differ by at
    least LEVEL_MIN_CONTRAST of the larger one there is no edge to refine
    and the prediction stands.

    The position returned is the OUTER FOOT of the transition, not its
    steepest point: from the strongest step of the expected direction the
    search walks outward while the profile keeps changing that way, for
    at most FOOT_WALK_MM, so the soft camera-gate ramp stays inside the
    crop.
    """
    n = len(profile)
    if n == 0 or predicted < 0 or predicted > n - 1:
        return predicted, False
    box = max(1, round(REFINE_SMOOTH_MM * px_per_mm))
    smoothed = _box_smooth(profile, box)

    # Which way does the profile step at this edge? Compare the level a
    # little way inside the predicted edge with the level the same
    # distance outside it.
    near = LEVEL_NEAR_MM * px_per_mm
    far = LEVEL_FAR_MM * px_per_mm
    if outward > 0:
        in_lo, in_hi = predicted - far, predicted - near
        out_lo, out_hi = predicted + near, predicted + far
    else:
        in_lo, in_hi = predicted + near, predicted + far
        out_lo, out_hi = predicted - far, predicted - near
    in_lo, in_hi = max(0, int(round(in_lo))), min(n, int(round(in_hi)))
    out_lo, out_hi = max(0, int(round(out_lo))), min(n, int(round(out_hi)))
    if in_hi - in_lo < 2 or out_hi - out_lo < 2:
        return predicted, False
    inside = float(np.median(smoothed[in_lo:in_hi]))
    outside = float(np.median(smoothed[out_lo:out_hi]))
    contrast = abs(outside - inside)
    if contrast < LEVEL_MIN_CONTRAST * max(inside, outside, 1.0):
        return predicted, False
    # Expected sign of the step along increasing index.
    step_sign = (1 if outside > inside else -1) * (1 if outward > 0 else -1)

    window_px = max(1, round(REFINE_WINDOW_MM * px_per_mm))
    lo = max(0, int(np.floor(predicted)) - window_px)
    hi = min(n - 1, int(np.ceil(predicted)) + window_px)
    if limit_lo is not None:
        lo = max(lo, int(np.ceil(limit_lo)))
    if limit_hi is not None:
        hi = min(hi, int(np.floor(limit_hi)))
    if hi <= lo:
        return predicted, False
    grad = np.diff(smoothed[lo:hi + 1]) * step_sign
    if grad.size == 0:
        return predicted, False
    i = int(np.argmax(grad))
    g = float(grad[i])
    if g <= 0:
        return predicted, False
    candidate = lo + i  # index of the steepest step (between i and i+1)

    nb_px = max(2, round(REFINE_NEIGHBOURHOOD_MM * px_per_mm))
    nlo = max(0, int(round(predicted - nb_px / 2)))
    nhi = min(n - 1, int(round(predicted + nb_px / 2)))
    if nhi - nlo < 2:
        return predicted, False
    nb_grad = np.abs(np.diff(smoothed[nlo:nhi + 1]))
    if nb_grad.size == 0:
        return predicted, False
    scale = float(np.median(nb_grad))
    if scale <= 0:
        return predicted, False
    if g < REFINE_GRADIENT_RATIO * scale:
        return predicted, False

    # Walk to the outer foot: keep going outward while the profile keeps
    # stepping in the expected direction by at least FOOT_STOP_FRAC of
    # the peak step.
    walk_px = max(1, round(FOOT_WALK_MM * px_per_mm))
    floor = FOOT_STOP_FRAC * g
    if outward < 0:
        k = candidate
        while k - 1 >= max(0, candidate - walk_px) and \
                (smoothed[k] - smoothed[k - 1]) * step_sign >= floor:
            k -= 1
        pos = k
    else:
        k = candidate + 1
        while k + 1 <= min(n - 1, candidate + 1 + walk_px) and \
                (smoothed[k + 1] - smoothed[k]) * step_sign >= floor:
            k += 1
        pos = k
    return float(pos) + 0.5, True


LATERAL_CONTRAST_NEAR_MM = 0.1   # the bands beside a lateral step candidate ...
LATERAL_CONTRAST_FAR_MM = 0.4    # ... run from NEAR to FAR on each side of it
LATERAL_MIN_CONTRAST = 0.04      # relative level difference across the step (second strip: 0.05-0.12)
LATERAL_CANDIDATE_FRAC = 0.5     # a lateral step candidate is at least this fraction of the window's strongest


def _refine_lateral(profile: np.ndarray, predicted: float, px_per_mm: float,
                    outward: int, limit_lo: float | None = None,
                    limit_hi: float | None = None) -> tuple:
    """Direction-free variant of `_refine_edge` for the lateral edges
    (see the caller): the strongest step of either sign within
    +/-REFINE_WINDOW_MM of `predicted` (clipped to the limits), accepted
    when the median levels LATERAL_CONTRAST_NEAR_MM..FAR_MM on its two
    sides differ by at least LATERAL_MIN_CONTRAST of the larger and the
    step is >= REFINE_GRADIENT_RATIO x the neighbourhood's median step;
    then walked outward to the foot exactly as `_refine_edge` does.
    Returns (position, refined_bool)."""
    n = len(profile)
    if n == 0 or predicted < 0 or predicted > n - 1:
        return predicted, False
    box = max(1, round(REFINE_SMOOTH_MM * px_per_mm))
    smoothed = _box_smooth(profile, box)
    window_px = max(1, round(REFINE_WINDOW_MM * px_per_mm))
    lo = max(0, int(np.floor(predicted)) - window_px)
    hi = min(n - 1, int(np.ceil(predicted)) + window_px)
    if limit_lo is not None:
        lo = max(lo, int(np.ceil(limit_lo)))
    if limit_hi is not None:
        hi = min(hi, int(np.floor(limit_hi)))
    if hi <= lo:
        return predicted, False
    grad = np.diff(smoothed[lo:hi + 1])
    if grad.size == 0:
        return predicted, False
    peak = float(np.max(np.abs(grad)))
    if peak <= 0:
        return predicted, False

    nb_px = max(2, round(REFINE_NEIGHBOURHOOD_MM * px_per_mm))
    nlo = max(0, int(round(predicted - nb_px / 2)))
    nhi = min(n - 1, int(round(predicted + nb_px / 2)))
    nb_grad = np.abs(np.diff(smoothed[nlo:nhi + 1]))
    scale = float(np.median(nb_grad)) if nb_grad.size else 0.0

    # Candidates: local maxima of |step| at least LATERAL_CANDIDATE_FRAC of
    # the strongest step in the window and REFINE_GRADIENT_RATIO x the
    # neighbourhood's median step. Taken INNERMOST first (nearest the
    # picture): a border's outer edge (border -> clear film) can be the
    # stronger step, and the picture's edge is the first step outward
    # from the picture that has real contrast on both sides. Picture
    # content steps inside the frame fail the contrast test (their
    # neighbourhoods are picture on both sides) and are passed over.
    near = LATERAL_CONTRAST_NEAR_MM * px_per_mm
    far = LATERAL_CONTRAST_FAR_MM * px_per_mm
    order = range(grad.size) if outward > 0 else range(grad.size - 1, -1, -1)
    candidate = None
    for i in order:
        g = float(grad[i])
        if abs(g) < LATERAL_CANDIDATE_FRAC * peak:
            continue
        if scale > 0 and abs(g) < REFINE_GRADIENT_RATIO * scale:
            continue
        if (i > 0 and abs(grad[i - 1]) > abs(g)) or \
                (i + 1 < grad.size and abs(grad[i + 1]) > abs(g)):
            continue  # not a local maximum
        c = lo + i
        a_lo, a_hi = int(round(c - far)), int(round(c - near))
        b_lo, b_hi = int(round(c + 1 + near)), int(round(c + 1 + far))
        a_lo, b_hi = max(0, a_lo), min(n, b_hi)
        if a_hi - a_lo < 2 or b_hi - b_lo < 2:
            continue
        level_a = float(np.median(smoothed[a_lo:a_hi]))
        level_b = float(np.median(smoothed[b_lo:b_hi]))
        if abs(level_b - level_a) < LATERAL_MIN_CONTRAST * max(level_a, level_b, 1.0):
            continue
        candidate = c
        break
    if candidate is None:
        return predicted, False
    g = float(grad[candidate - lo])
    step_sign = 1 if g > 0 else -1

    walk_px = max(1, round(FOOT_WALK_MM * px_per_mm))
    floor = FOOT_STOP_FRAC * abs(g)
    if outward < 0:
        k = candidate
        while k - 1 >= max(0, candidate - walk_px) and \
                (smoothed[k] - smoothed[k - 1]) * step_sign >= floor:
            k -= 1
        pos = k
    else:
        k = candidate + 1
        while k + 1 <= min(n - 1, candidate + 1 + walk_px) and \
                (smoothed[k + 1] - smoothed[k]) * step_sign >= floor:
            k += 1
        pos = k
    return float(pos) + 0.5, True


# Per-line tracking of the film's air-facing edge. The band edge from the
# column medians is the MEDIAN edge over the whole aperture; on real data
# (2026-09-19, 3600 dpi) the free lateral edge of a 16 mm strip wandered
# by 1-2 mm along one aperture, so a search zone fixed next to the median
# edge saw plain air as "holes" wherever the film had moved inward. The
# hole zone is therefore placed relative to the edge found on EACH line.
# The perforation itself does not break the film edge: on this film the
# hole sits ~0.8 mm inside the edge, in a dark printed border, with an
# intact ~0.6 mm rim outside it (measured 2026-09-19), so a hole is an
# air-level region INSIDE the band next to the edge, not a jump of the
# edge.
EDGE_TRANSITION_FRAC = 0.6      # green < this x air = film, scanning in from the air side
EDGE_SEARCH_MM = 4.0            # per-line search zone either side of the median band edge
EDGE_TRACK_WINDOW_MM = 10.0     # rolling-median window for the local edge (> 2 x HOLE_MAX_MM)
RAIL_TRANSITION_MM = 0.5        # the rail's blurred edge, excluded from the edge tracker's zone
HOLE_ZONE_FROM_MM = 0.3         # hole zone starts this far inside the per-line film edge ...
HOLE_ZONE_TO_MM = 3.0           # ... and ends this far inside it
HOLE_STUB_MIN_MM = 0.3          # a hole cut by the aperture boundary may be this short
# A hole is AIR ACROSS the zone, not one bright pixel in it: Test 107's strip
# has an unexposed edge rim reading 0.85-0.95 x air (earlier strips: ~0.1),
# so the zone's brightest pixel reached air level in rim noise (false
# "holes" 1.5 mm long, carrying no air across) and around the real hole
# (one 4.7-5.0 mm air run, too long, rejected). Measured as the fraction of
# the zone's columns at air level: real holes 0.55 (earlier strips, flat over
# the hole) and up to ~1 (Test 107), false rim runs 0.01-0.05.
HOLE_LATERAL_MIN = 0.25         # a run whose best line has less of the zone at air is not a hole
HOLE_CORE_FRAC = 0.5            # the holes are the stretches at >= this fraction of the run's best line


def _track_edge(green: np.ndarray, band_edge: int, air: float,
                px_per_mm: float, edge_is_low: bool,
                outer_limit: "int | None" = None) -> np.ndarray:
    """Per-line column of the film's air-facing edge (float, NaN where no
    film is seen in the search zone -- e.g. beyond a free strip end).
    `outer_limit` clips the search zone on the air side, short of the
    rail (a strip lying free in the channel, air on both sides, has the
    rail within EDGE_SEARCH_MM of its edge, and the rail and its blurred
    edge are as dark as film -- Test 104's first pass tracked the rail as
    the film edge and found no holes)."""
    n_lines, width = green.shape
    search = max(1, round(EDGE_SEARCH_MM * px_per_mm))
    z_lo, z_hi = max(0, band_edge - search), min(width, band_edge + search)
    if outer_limit is not None:
        if edge_is_low:
            z_lo = max(z_lo, outer_limit)
        else:
            z_hi = min(z_hi, outer_limit)
    if z_hi <= z_lo:
        return np.full(n_lines, np.nan)
    zone = green[:, z_lo:z_hi]
    if not edge_is_low:
        zone = zone[:, ::-1]
    film_mask = zone < EDGE_TRANSITION_FRAC * air
    has = film_mask.any(axis=1)
    first = np.argmax(film_mask, axis=1).astype(np.float64)
    if edge_is_low:
        edge = z_lo + first
    else:
        edge = (z_hi - 1) - first
    return np.where(has, edge, np.nan)


def _rolling_nanmedian(x: np.ndarray, window: int) -> np.ndarray:
    """Rolling median ignoring NaN, same length as x (edges padded by
    repetition). Window is forced odd. All-NaN windows give NaN."""
    n = len(x)
    if n == 0:
        return x
    window = max(1, min(window | 1, n | 1))
    half = window // 2
    padded = np.concatenate([np.full(half, x[0]), x, np.full(half, x[-1])])
    view = np.lib.stride_tricks.sliding_window_view(padded, window)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        med = np.nanmedian(view, axis=1)
    return med[:n]


def _hole_runs(green: np.ndarray, lo: int, hi: int, width: int, air: float,
               film_present: np.ndarray, px_per_mm: float, edge_is_low: bool,
               outer_limit: "int | None" = None, against_plastic: bool = False):
    """Candidate perforation runs on one lateral edge of the film band,
    as (start, end_inclusive) line runs, plus the per-line LOCAL film
    edge (rolling median of the tracked edge) used for lateral
    prediction. A hole line is one whose zone [edge + HOLE_ZONE_FROM_MM,
    edge + HOLE_ZONE_TO_MM] (measured inward from THAT line's film edge)
    reaches air level."""
    n_lines = green.shape[0]
    band_edge = lo if edge_is_low else hi
    if against_plastic:
        # The film edge rests against the rail, no air outside it: there is
        # no air->film step to track, but the holes still read as air
        # inside the band (the light comes through them). The band edge
        # is the edge on every line (Test 104, roll 7: four photographs
        # with their hole edge flush against the rail, holes plainly
        # visible, "no perforation found").
        edge = np.full(n_lines, float(band_edge))
    else:
        edge = _track_edge(green, band_edge, air, px_per_mm, edge_is_low, outer_limit)
    local = _rolling_nanmedian(edge, max(3, round(EDGE_TRACK_WINDOW_MM * px_per_mm)))
    ref = np.where(np.isnan(edge), local, edge)
    valid = ~np.isnan(ref)
    ref_i = np.where(valid, ref, band_edge).astype(np.int64)
    offs = np.arange(round(HOLE_ZONE_FROM_MM * px_per_mm),
                     max(round(HOLE_ZONE_FROM_MM * px_per_mm) + 1,
                         round(HOLE_ZONE_TO_MM * px_per_mm)))
    if edge_is_low:
        cols = ref_i[:, None] + offs[None, :]
    else:
        cols = ref_i[:, None] - offs[None, :]
    cols = np.clip(cols, 0, width - 1)
    zone = green[np.arange(n_lines)[:, None], cols]
    at_air = (zone >= HOLE_FRAC * air).mean(axis=1)
    hole_bool = (at_air > 0) & film_present & valid
    runs = []
    for (s, e) in _true_runs(hole_bool):
        if against_plastic:
            # The band edge stands in for the film edge and part of each
            # hole lies under the rail: the fraction reads 0.13-0.16 on
            # real holes (Test 103, edge flush against the rail). Taken
            # as it is, as before.
            runs.append((s, e))
            continue
        frac = at_air[s:e + 1]
        best = float(frac.max())
        if best < HOLE_LATERAL_MIN:
            continue
        runs.extend((s + cs, s + ce) for (cs, ce) in _true_runs(frac >= HOLE_CORE_FRAC * best))
    min_len = HOLE_MIN_MM * px_per_mm
    max_len = HOLE_MAX_MM * px_per_mm
    stub_len = HOLE_STUB_MIN_MM * px_per_mm
    # A hole may be cut short by the aperture's boundary OR by the strip's
    # own cut end (Test 104: a strip cut through a hole, starting inside
    # the aperture): a stub touching either counts.
    present_runs = _true_runs(film_present)
    cut = {0, n_lines - 1}
    for (ps, pe) in present_runs:
        cut.update((ps, pe))
    slack = max(1, round(HOLE_STUB_MIN_MM * px_per_mm))

    def at_cut(s, e):
        return any(abs(s - c) <= slack or abs(e - c) <= slack for c in cut)

    runs = [(s, e) for (s, e) in runs
            if (min_len <= (e - s + 1) <= max_len)
            or (at_cut(s, e) and stub_len <= (e - s + 1) <= max_len)]
    return runs, local


# Plausibility of a predicted image: the mean of the interior profile
# over the part of the window inside the aperture must be denser than
# this fraction of the clear-base level, else the window is clear film
# (a hole at the strip's cut end predicts an image that is not there).
IMAGE_DENSITY_FRAC = 0.85
IMAGE_VARIATION_FRAC = 0.10       # std/mean of the profile inside a window that is image, not base
PLAUSIBILITY_TRIM_MM = 1.0        # ... measured on the window's interior: its ends may hold the
                                  # aperture edge or the next frame's border (Test 104: a clear
                                  # leader passed as "varying" on its first dark lines alone)
RAIL_GUARD_MM = 0.4             # lateral refinement stays this far from the rail-side band edge
RIM_GUARD_MM = 1.0              # ... and this far inside the perforated film edge (rim 0.6 mm, hole 0.6-2.1 mm)
PRODUCT_PAD_MM = 0.25           # the CLI grows a whole image's crop by this on every side
MIN_OVERLAP_MM = 1.0            # a window overlapping the aperture by less carries nothing
MERGE_FRAC_OF_PITCH = 0.5       # two predictions closer than this are the same image


RAIL_EDGE_INSET_MM = 0.05         # `crop` never pads past the film band minus this (rail rim, light gap, air)


def _candidates(runs, film: Film, px_per_mm: float, reversed_: bool):
    """Every hole predicts the image AFTER it (hole end + lead) and the
    image BEFORE it (hole start - trail); consecutive holes predict the
    same image twice and those are merged. `reversed_` swaps lead and
    trail (the strip inserted the other way round). Returns
    [(line0, line1, perforation_line), ...] sorted by line0."""
    lead = film.perforation_lead_mm * px_per_mm
    trail = film.perforation_trail_mm * px_per_mm
    if reversed_:
        lead, trail = trail, lead
    img = film.image_mm[0] * px_per_mm
    cands = []
    for (s, e) in runs:
        cands.append((e + lead, e + lead + img, float(e)))
        cands.append((s - trail - img, s - trail, float(s)))
    cands.sort()
    merged: list = []
    tol = MERGE_FRAC_OF_PITCH * film.pitch_mm * px_per_mm
    for c in cands:
        if merged and abs(c[0] - merged[-1][0]) < tol:
            m = merged[-1]
            merged[-1] = ((m[0] + c[0]) / 2, (m[1] + c[1]) / 2, m[2])
        else:
            merged.append(c)
    return merged


# ------------------------------------------------------------ frame locator
#
# Measured on real 3600-dpi frames (Test 103/104 rolls): between two
# pictures the band-interior profile reads a FLAT run ~8 mm long at
# 0.057-0.080 x air (the printed border), std inside 0.5 mm bins ~0.0003
# of air; pictures read 0.13-0.36 and vary. The gap's ends coincide with the
# hole-predicted image edges within ~0.2 mm. Flat regions inside pictures
# exist (sky, 0.09-0.13, <= 5 mm); a dense leader read 0.03 flat for 11 mm.
FRAME_LOCATOR_ENABLED = True       # module switch, used only by tests (I-A3)
FRAME_FLAT_WINDOW_MM = 0.5
FRAME_FLAT_MAX_STD = 0.003         # (tuned) rolling std / air; real gaps ~0.0003, noise-free fixtures ~0.0005
FRAME_FLAT_DEV = 0.0015            # a gap is flat when 95 % of a 0.5 mm window lies this close (x air) to its median (real gaps 0.0003, picture grain 0.003)
FRAME_OUTLIER = 0.01               # a line this far (x air) from the 0.3 mm median is a scratch or speck, not texture
FRAME_BORDER_MAX_FRAC = 0.16       # a gap reads below this x air
FRAME_BORDER_BASE_FRAC = 0.5       # a gap reads below this fraction of the aperture's clear film base
FRAME_BORDER_MIN_FRAC = 0.04       # (tuned) a full gap below this x air (dense leader 0.03) never sets the border level
FRAME_GAP_MIN_MM = 6.5
FRAME_GAP_MAX_MM = 9.5
FRAME_PARTIAL_MIN_MM = 1.0
FRAME_LEVEL_TOL = 0.30             # a gap's level agrees with the border level within +/- this fraction
FRAME_MATCH_MM = 3.0               # (tuned) a frame-located image this close to a hole-located one IS that image
FRAME_MAX_OVERLAP_MM = 2.0         # (tuned) pictures never overlap more than this
FRAME_WEAK_PARTIAL_MIN_MM = 3.0    # a partial gap in an aperture with no full gap (weaker rule)
FRAME_WEAK_LEVEL = (0.03, 0.16)
FRAME_MEDIAN_MM = 0.3              # flatness is judged on the profile median-filtered over this (scratches, dust)
FRAME_EXTEND_MAX_MM = 2.0          # an accepted gap is extended outward to the level step, at most this far per side
FRAME_EXTEND_MIN_RUN_MM = 3.0      # only a flat run at least this long is extended (a short flat blob in texture stays short)
                                   # (an extension that makes the gap longer than FRAME_GAP_MAX_MM is undone:
                                   # Test 107, a dense white fence at the picture's edge read 0.145-0.16 against
                                   # the border's 0.122, inside the tolerance, and the 8 mm gap grew to 10 mm)
BLANK_END_ZONE_MM = 2.5            # gap lines this close to a window's ends are left out of its clear-film test
BLANK_EDGE_TOL_MM = 1.0            # a blank window may reach this far past the aperture's lines (clipped)


def _frame_gaps(rel: np.ndarray, film_present: np.ndarray, px_per_mm: float,
                image_len_mm: float, n_lines: int, base_rel: "float | None" = None) -> list:
    """The printed-border gaps along transport. `rel` is the band-interior
    profile divided by air. Returns [(start, end, touches_start,
    touches_end)] in line units (end exclusive); a gap that touches the
    aperture boundary or a free film end predicts no image on that side."""
    w = max(2, int(round(FRAME_FLAT_WINDOW_MM * px_per_mm)))
    if n_lines < w + 1:
        return []
    x_raw = np.where(film_present, rel, 0.0)
    from numpy.lib.stride_tricks import sliding_window_view
    fw = max(3, int(round(FRAME_MEDIAN_MM * px_per_mm)) | 1)
    pad = fw // 2
    # Levels, steps and the outward extension use the median-filtered profile.
    x = np.median(sliding_window_view(np.pad(x_raw, (pad, pad), mode="edge"), fw), axis=1)
    # Flatness: a thin bright scratch or a speck of dust (a NARROW outlier
    # from the filtered profile) is replaced first; the grain of a picture's
    # own texture is kept, since it is what tells a picture from the perfectly
    # flat printed border (real gaps: 0.0003, texture: 0.003 x air). A window
    # is flat when 95 % of its lines lie within FRAME_FLAT_DEV of its median.
    clean = np.where(np.abs(x_raw - x) > FRAME_OUTLIER, x, x_raw)
    win = sliding_window_view(clean, w)
    med = np.median(win, axis=1)
    dev = np.percentile(np.abs(win - med[:, None]), 95, axis=1)
    cp = np.concatenate(([0], np.cumsum(film_present.astype(np.int64))))
    starts = np.arange(0, n_lines - w + 1)
    all_present = (cp[starts + w] - cp[starts]) == w
    flat_win = (all_present & (dev < FRAME_FLAT_DEV)
                & (med < FRAME_BORDER_MAX_FRAC))
    if base_rel:
        # the printed border is dark against the film's clear base (real
        # strips 0.1-0.15, fixtures 0.42); a flat stretch at more than half
        # the base is a picture's smooth region, not a border
        flat_win &= med < FRAME_BORDER_BASE_FRAC * base_rel
    # a line is flat when any flat window covers it
    diff = np.zeros(n_lines + 1, dtype=np.int64)
    for s in np.flatnonzero(flat_win):
        diff[s] += 1
        diff[s + w] -= 1
    flat_line = np.cumsum(diff[:n_lines]) > 0
    # Two constant levels side by side (a gap against a picture's flat half)
    # would join into one flat run: cut the runs at every level step. (The
    # step used to be marked by the camera-gate halo, which the median
    # filter removes.)
    h = max(1, w // 2)
    idx = np.arange(n_lines)
    left, right = x[np.clip(idx - h, 0, n_lines - 1)], x[np.clip(idx + h, 0, n_lines - 1)]
    flat_line &= ~(np.abs(right - left) > np.maximum(FRAME_LEVEL_TOL * np.minimum(left, right), 0.004))
    cands = []
    ext_max = int(round(FRAME_EXTEND_MAX_MM * px_per_mm))
    for (s, e) in _true_runs(flat_line):
        # Extend the run outward while the filtered level stays within the
        # tolerance of the run's own level: the gap's ends then land on the
        # real level step, also where a scratch or dust cut the flat run short.
        lvl = float(np.median(x[s:e + 1]))
        s0, e0 = s, e
        if lvl > 0 and (e - s + 1) >= FRAME_EXTEND_MIN_RUN_MM * px_per_mm:
            while (s0 > s - ext_max and s0 > 0 and bool(film_present[s0 - 1])
                   and abs(x[s0 - 1] - lvl) <= FRAME_LEVEL_TOL * lvl):
                s0 -= 1
            while (e0 < e + ext_max and e0 + 1 < n_lines and bool(film_present[e0 + 1])
                   and abs(x[e0 + 1] - lvl) <= FRAME_LEVEL_TOL * lvl):
                e0 += 1
        if (e0 - s0 + 1) / px_per_mm <= FRAME_GAP_MAX_MM or (e - s + 1) / px_per_mm > FRAME_GAP_MAX_MM:
            s, e = s0, e0
        g0, g1 = float(s), float(e + 1)
        touch0 = s == 0 or not bool(film_present[s - 1])
        touch1 = e + 1 >= n_lines or not bool(film_present[e + 1])
        length = (g1 - g0) / px_per_mm
        level = float(np.median(x[s:e + 1]))
        cands.append((g0, g1, touch0, touch1, length, level))
    full = [c for c in cands if not c[2] and not c[3]
            and FRAME_GAP_MIN_MM <= c[4] <= FRAME_GAP_MAX_MM]
    ref_pool = [c[5] for c in full if c[5] >= FRAME_BORDER_MIN_FRAC]
    ref = min(ref_pool) if ref_pool else None

    def agrees(level):
        return abs(level - ref) <= FRAME_LEVEL_TOL * ref

    out = []
    for (g0, g1, t0, t1, length, level) in cands:
        if not t0 and not t1:
            if ref is not None and FRAME_GAP_MIN_MM <= length <= FRAME_GAP_MAX_MM and agrees(level):
                out.append((g0, g1, t0, t1))
        else:
            if length > FRAME_GAP_MAX_MM:
                continue
            if ref is not None:
                if length >= FRAME_PARTIAL_MIN_MM and agrees(level):
                    out.append((g0, g1, t0, t1))
            elif (length >= FRAME_WEAK_PARTIAL_MIN_MM
                  and FRAME_WEAK_LEVEL[0] <= level <= FRAME_WEAK_LEVEL[1]):
                out.append((g0, g1, t0, t1))
    return out


def detect(image, *, dpi: float, film: Film = FILM_110) -> ApertureFind:
    """Find every 110 image predicted inside one aperture-registered
    image. Never raises on image content -- an aperture the detector
    cannot resolve comes back with `reason` set and `frames` empty; the
    caller's aperture product is unaffected either way.
    """
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return ApertureFind(False, None, None, None, False, [],
                             f"expected a (lines, width, channels>=3) image, got shape {arr.shape}")
    n_lines, width = arr.shape[0], arr.shape[1]
    if n_lines == 0 or width == 0:
        return ApertureFind(True, None, None, None, False, [], "empty aperture")

    px_per_mm = dpi / MM_PER_INCH
    green = arr[:, :, 1].astype(np.float64)

    # a. Levels and column classes.
    col_medians = np.median(green, axis=0)
    air = float(np.percentile(col_medians, AIR_PERCENTILE))
    if air <= 0:
        return ApertureFind(True, None, None, None, False, [], "empty aperture")
    is_air = col_medians >= AIR_FRAC * air
    # Plastic only where a dark run is at least PLASTIC_MIN_RUN_MM wide:
    # the film's own dark printed border (~1.2 mm, 0.066 x air on the
    # 2026-09-19 strip) must stay inside the band.
    plastic_raw = col_medians <= PLASTIC_FRAC * air
    is_plastic = np.zeros_like(plastic_raw)
    min_plastic = PLASTIC_MIN_RUN_MM * px_per_mm
    for (ps, pe) in _true_runs(plastic_raw):
        if (pe - ps + 1) >= min_plastic:
            is_plastic[ps:pe + 1] = True
    is_film = ~is_air & ~is_plastic
    if not is_film.any():
        return ApertureFind(True, None, None, None, False, [], "empty aperture")

    # b. Film band: the longest run of film columns.
    lo, hi = _longest_true_run(is_film)
    band_width_mm = (hi - lo) / px_per_mm

    touches_air_lo = lo > 0 and bool(is_air[lo - 1])
    touches_air_hi = hi < width and bool(is_air[hi])
    touches_plastic_lo = lo > 0 and bool(is_plastic[lo - 1])
    touches_plastic_hi = hi < width and bool(is_plastic[hi])
    rail_hidden_mm = 0.0
    if abs(band_width_mm - film.width_mm) > FILM_WIDTH_TOLERANCE_MM:
        hidden = film.width_mm - band_width_mm
        under_rail = (touches_plastic_lo != touches_plastic_hi) and (touches_air_lo or touches_air_hi)
        if not (0.0 < hidden <= RAIL_HIDDEN_MAX_MM and under_rail):
            return ApertureFind(False, (lo, hi), None, None, False, [],
                                 f"no 110 film band ({band_width_mm:.1f} mm)")
        rail_hidden_mm = float(hidden)

    # Interior columns for the along-transport profile: the band with the
    # perforation search margin trimmed off BOTH sides, so a hole (which
    # can only ever be within HOLE_EDGE_MARGIN_MM of ONE edge) never
    # biases the per-line median regardless of which edge that turns out
    # to be.
    margin_px = max(1, round(HOLE_EDGE_MARGIN_MM * px_per_mm))
    int_lo, int_hi = lo + margin_px, hi - margin_px
    if int_hi <= int_lo:
        int_lo, int_hi = lo, hi
    interior_profile = np.median(green[:, int_lo:int_hi], axis=1)

    # c. Film presence along transport, and the free-end positions. Runs
    # shorter than FILM_PRESENT_MIN_RUN_MM are discarded as crop-boundary
    # noise before deciding whether film reaches line 0 / the last line.
    film_present = interior_profile < FILM_PRESENT_FRAC * air
    min_run_px = max(1, round(FILM_PRESENT_MIN_RUN_MM * px_per_mm))
    present_runs = [(s, e) for (s, e) in _true_runs(film_present)
                     if (e - s + 1) >= min_run_px]
    if not present_runs:
        film_line_start = film_line_end = None
    else:
        first_s, _first_e = present_runs[0]
        _last_s, last_e = present_runs[-1]
        film_line_start = None if first_s == 0 else float(first_s)
        film_line_end = None if last_e == n_lines - 1 else float(last_e)

    # leading_continuation: the aperture already opens on image content
    # (no free end at line 0), and that content is dense (dark) rather
    # than clear film -- i.e. a split image's tail is continuing in.
    if film_line_start is not None:
        leading_continuation = False
    else:
        first_n = min(n_lines, max(1, round(LEADING_CONTINUATION_MM * px_per_mm)))
        first_profile = interior_profile[:first_n]
        if film_present.any():
            clear_base = float(np.percentile(interior_profile[film_present],
                                              CLEAR_BASE_PERCENTILE))
        else:
            clear_base = air
        leading_continuation = bool(
            first_profile.size and np.median(first_profile) < LEADING_CONTINUATION_FRAC * clear_base
        )

    pitch_px = film.pitch_mm * px_per_mm
    whole_margin_px = WHOLE_MARGIN_MM * px_per_mm
    min_overlap_px = MIN_OVERLAP_MM * px_per_mm
    lat_len_px = film.image_mm[1] * px_per_mm
    offset_px = film.image_lateral_offset_mm * px_per_mm
    if film_present.any():
        clear_base = float(np.percentile(interior_profile[film_present],
                                          CLEAR_BASE_PERCENTILE))
    else:
        clear_base = air
    film_runs_out = [(int(a), int(b)) for a, b in present_runs]
    rim_guard = RIM_GUARD_MM * px_per_mm
    rail_guard = RAIL_GUARD_MM * px_per_mm

    # The printed-border gaps of this aperture (frame locator). Their lines
    # are not picture: a window's clear-film test leaves them out, so a window
    # that runs a little into a gap (a prediction 1 mm off) still reads as the
    # clear frame it is, and is never taken for a picture because of the dark
    # gap lines in it.
    gaps = (_frame_gaps(interior_profile / air, film_present, px_per_mm,
                        film.image_mm[0], n_lines, clear_base / air)
            if FRAME_LOCATOR_ENABLED else [])
    gap_mask = np.zeros(n_lines, dtype=bool)
    for (g0, g1, _t0, _t1) in gaps:
        gap_mask[max(0, int(np.floor(g0))):min(n_lines, int(np.ceil(g1)))] = True

    def _refine_candidate(line0, line1, perforation_line, orientation, lateral):
        """Plausibility, along-transport refinement, lateral prediction and
        refinement and whole/split classification of ONE predicted image,
        shared by the hole locator and the frame locator. `lateral` is
        (edge_is_low, perforated_edge_col, local_edge) when the perforated
        edge is known, None when it is not (frame locator without holes:
        the picture is centred on the band and both sides are searched
        RIM_GUARD_MM inside the band edges). Returns (FrameFind | None,
        confirmed_edge_count)."""
        clo = max(0, int(np.floor(line0)))
        chi = min(n_lines, int(np.ceil(line1)))
        if chi - clo < min_overlap_px:
            return None, 0
        # Plausibility: the window must look like an image -- denser
        # than clear film, OR varying along its length (image content
        # varies, clear film does not). The second test matters where
        # the aperture holds little clear film to estimate the base
        # from (a fogged leader plus one image, 2026-09-19 aperture 1).
        trim = int(round(PLAUSIBILITY_TRIM_MM * px_per_mm))
        if chi - clo > 3 * trim:
            a_i, b_i = clo + trim, chi - trim
        else:
            a_i, b_i = clo, chi
        seg = interior_profile[a_i:b_i]
        # Only gap lines near the window's ENDS are left out (where a
        # prediction a millimetre or two off runs into a gap); a flat run
        # inside the picture is the picture's own.
        zone = int(round(BLANK_END_ZONE_MM * px_per_mm))
        lines = np.arange(a_i, b_i)
        near_end = (lines < clo + zone) | (lines >= chi - zone)
        kept = seg[~(gap_mask[a_i:b_i] & near_end) & film_present[a_i:b_i]]
        if kept.size >= min_overlap_px and float(film_present[clo:chi].mean()) >= 0.9:
            # (a window that reaches off the film -- a leader ghost -- is judged as before)
            seg = kept
        seg_mean = float(np.mean(seg))
        seg_std = float(np.std(seg))
        if (seg_mean >= IMAGE_DENSITY_FRAC * clear_base
                and seg_std < IMAGE_VARIATION_FRAC * max(seg_mean, 1.0)):
            return None, -1        # reads as clear film (a blank, for the frame locator)

        r_line0, ok_l0 = _refine_edge(interior_profile, line0, px_per_mm, outward=-1)
        r_line1, ok_l1 = _refine_edge(interior_profile, line1, px_per_mm, outward=+1)
        score = int(ok_l0) + int(ok_l1)

        col_profile = np.median(green[clo:chi, :], axis=0)
        if lateral is None:
            centre = (lo + hi) / 2.0
            col0 = centre - lat_len_px / 2.0
            col1 = centre + lat_len_px / 2.0
            r_col0, ok_c0 = _refine_lateral(col_profile, col0, px_per_mm, outward=-1,
                                            limit_lo=lo + rim_guard)
            r_col1, ok_c1 = _refine_lateral(col_profile, col1, px_per_mm, outward=+1,
                                            limit_hi=hi - rim_guard)
        else:
            edge_is_low_, perforated_edge_col, local_edge = lateral
            # Lateral prediction from the LOCAL film edge over this image's
            # own lines (the tracked edge's rolling median), not the
            # aperture-wide median edge: the free edge of a 16 mm strip can
            # wander 1-2 mm along one aperture (2026-09-19), and the image
            # follows the film.
            edge_here = perforated_edge_col
            if local_edge is not None:
                seg = local_edge[clo:chi]
                seg = seg[~np.isnan(seg)]
                if seg.size:
                    edge_here = float(np.median(seg))
            if edge_is_low_:
                col0 = edge_here + offset_px
                col1 = col0 + lat_len_px
            else:
                col1 = edge_here - offset_px
                col0 = col1 - lat_len_px

            # Lateral refinement, both sides, direction-free: the border
            # beside the picture is DARKER than it on the first real strip
            # (a pre-exposed printed border, 2026-09-19) and LIGHTER on the
            # second (a ~0.5 mm fogged margin of density ~0.8 next to a
            # denser picture, 2026-09-20), and a level comparison around
            # the prediction straddles border and picture when the
            # prediction is 0.4 mm off. So the strongest step of EITHER
            # sign in the window is taken, its contrast checked in the
            # bands right beside it, and the edge walked outward to the
            # foot. The search stays RIM_GUARD_MM inside the perforated
            # film edge (rim and hole) and RAIL_GUARD_MM short of the rail
            # side of the band (the rail's bright rim).
            if edge_is_low_:
                r_col0, ok_c0 = _refine_lateral(col_profile, col0, px_per_mm, outward=-1,
                                                limit_lo=edge_here + rim_guard)
                r_col1, ok_c1 = _refine_lateral(col_profile, col1, px_per_mm, outward=+1,
                                                limit_hi=hi - rail_guard)
            else:
                r_col0, ok_c0 = _refine_lateral(col_profile, col0, px_per_mm, outward=-1,
                                                limit_lo=lo + rail_guard)
                r_col1, ok_c1 = _refine_lateral(col_profile, col1, px_per_mm, outward=+1,
                                                limit_hi=edge_here - rim_guard)

        inset = RAIL_EDGE_INSET_MM * px_per_mm
        col_clamp = (lo + inset, hi - inset)

        # Not whole = split, by the same margin: an image whose start
        # (end) lies within WHOLE_MARGIN_MM of the aperture's edge has
        # that edge under the bar as far as the product is concerned
        # (Test 103: image 4 seated at +0.03 mm was "not whole" with
        # no split side, and the hook reported it split by no bar).
        whole = (r_line0 >= whole_margin_px) and (r_line1 <= n_lines - whole_margin_px)
        if r_line0 < whole_margin_px:
            split = "leading"
        elif r_line1 > n_lines - whole_margin_px:
            split = "trailing"
        else:
            split = None

        # Near-whole: one edge within the cut tolerance of the boundary, the
        # other inside and confirmed by the data.
        cut_mm = None
        cut_px = EDGE_CUT_MAX_MM * px_per_mm
        if (not whole and split == "leading" and r_line0 >= -cut_px
                and r_line1 <= n_lines - whole_margin_px and ok_l1):
            cut_mm = max(0.0, -r_line0) / px_per_mm
        elif (not whole and split == "trailing" and r_line1 <= n_lines + cut_px
                and r_line0 >= whole_margin_px and ok_l0):
            cut_mm = max(0.0, r_line1 - n_lines) / px_per_mm

        free_end_near = False
        end_gap_mm = None
        for fend, is_start in ((film_line_start, True), (film_line_end, False)):
            if fend is None:
                continue
            if abs(r_line0 - fend) <= pitch_px or abs(r_line1 - fend) <= pitch_px:
                free_end_near = True
                gap = ((r_line0 - fend) if is_start else (fend - r_line1)) / px_per_mm
                if gap >= -EDGE_CUT_MAX_MM and (end_gap_mm is None or gap < end_gap_mm):
                    end_gap_mm = max(0.0, gap)

        size_mm = ((r_line1 - r_line0) / px_per_mm, (r_col1 - r_col0) / px_per_mm)
        return FrameFind(
            index=0,
            perforation_line=perforation_line,
            line0=r_line0, line1=r_line1,
            col0=r_col0, col1=r_col1,
            whole=whole, split=split,
            refined={"line0": ok_l0, "line1": ok_l1, "col0": ok_c0, "col1": ok_c1},
            free_end_near=free_end_near,
            size_mm=size_mm,
            orientation=orientation,
            col_clamp=col_clamp,
            cut_mm=cut_mm, end_gap_mm=end_gap_mm,
        ), score

    # d. Perforations: resolve the perforated edge and its hole runs. The
    # edge tracker's zone stops at the first plastic column beyond the
    # band on either side (a strip lying free in the channel has air AND
    # the rail within reach of its edge).
    # The edge tracker's zone must stop short of the rail: the rail is as
    # dark as film, and so is its blurred edge, which begins about
    # RAIL_TRANSITION_MM before the first column classed plastic. (It must
    # NOT stop at the first non-air column: where a strip covers only
    # part of the aperture's lines, the columns of its rim and holes are
    # air in the median and the band ends short of the real film edge.)
    plastic_cols = np.flatnonzero(is_plastic)
    transition = int(round(RAIL_TRANSITION_MM * px_per_mm))
    before = plastic_cols[plastic_cols < lo]
    after = plastic_cols[plastic_cols >= hi]
    limit_lo = int(before[-1]) + 1 + transition if before.size else None
    limit_hi = int(after[0]) - transition if after.size else None
    edge_is_low = None
    runs: list = []
    local_edge = None
    reason = ""
    if not (touches_air_lo or touches_air_hi):
        reason = "film band not adjacent to air; perforations hidden"
    else:
        # Holes are looked for on BOTH edges; an edge resting against the rail
        # (no air outside) is taken as it is, the holes reading as air inside.
        if touches_air_lo and not touches_air_hi and not touches_plastic_hi:
            edge_is_low = True
            runs, local_edge = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, True, limit_lo)
        elif touches_air_hi and not touches_air_lo and not touches_plastic_lo:
            edge_is_low = False
            runs, local_edge = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, False, limit_hi)
        else:
            runs_lo, local_lo = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, True,
                                           limit_lo, against_plastic=touches_plastic_lo)
            runs_hi, local_hi = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, False,
                                           limit_hi, against_plastic=touches_plastic_hi)
            if runs_lo and not runs_hi:
                edge_is_low, runs, local_edge = True, runs_lo, local_lo
            elif runs_hi and not runs_lo:
                edge_is_low, runs, local_edge = False, runs_hi, local_hi
            elif runs_lo and runs_hi:
                edge_is_low, runs, local_edge = True, runs_lo, local_lo  # tie-break: documented above
            else:
                edge_is_low, runs, local_edge = None, [], None
        if not runs:
            reason = ("no perforation found near either film edge" if edge_is_low is None
                      else "no perforation found in the film band")

    frames_out: list = []
    hole_clear: list = []
    blanks_out: list = []
    lateral_known = None
    if runs:
        perforated_edge_col = lo if edge_is_low else hi
        lateral_known = (edge_is_low, perforated_edge_col, local_edge)

        def _evaluate(reversed_: bool):
            """Refine and classify every plausible candidate under one
            lead/trail assignment; returns (frames, score) where score is
            the number of along-transport edges the data confirmed."""
            out = []
            clear = []
            score = 0
            for (line0, line1, perforation_line) in _candidates(runs, film, px_per_mm, reversed_):
                fr, sc = _refine_candidate(
                    line0, line1, perforation_line,
                    "hole-before-image" if not reversed_ else "hole-after-image",
                    lateral_known)
                if fr is None:
                    if sc < 0:
                        clear.append((line0, line1))     # a clear frame, not a picture
                    continue
                score += sc
                fr.index = len(out) + 1
                out.append(fr)
            return out, score, clear

        # The strip may be inserted either way round; the two assignments of
        # lead/trail differ by ~1 mm, more than the refinement window, so the
        # data decides: the assignment whose predicted along-transport edges
        # the profile confirms more often wins; a tie keeps the normal one.
        frames_normal, score_normal, clear_normal = _evaluate(False)
        frames_rev, score_rev, clear_rev = _evaluate(True)
        if score_rev > score_normal:
            frames_out, hole_clear = frames_rev, clear_rev
        else:
            frames_out, hole_clear = frames_normal, clear_normal

    # e. The frame locator (independent of the holes): the printed border
    # between two pictures reads as a flat, dark gap along transport.
    if FRAME_LOCATOR_ENABLED:
        img_px = film.image_mm[0] * px_per_mm
        preds = []
        for (g0, g1, touch0, touch1) in gaps:
            if not touch0:
                preds.append((g0 - img_px, g0))        # the image BEFORE the gap
            if not touch1:
                preds.append((g1, g1 + img_px))        # the image AFTER it
        preds.sort()
        tol = MERGE_FRAC_OF_PITCH * pitch_px
        merged: list = []
        for p in preds:
            if merged and abs(p[0] - merged[-1][0]) < tol:
                m = merged[-1]
                merged[-1] = ((m[0] + p[0]) / 2, (m[1] + p[1]) / 2)
            else:
                merged.append(p)
        hole_frames = list(frames_out)
        match_px = FRAME_MATCH_MM * px_per_mm
        overlap_px = FRAME_MAX_OVERLAP_MM * px_per_mm
        found = []
        blank_windows = list(hole_clear)
        for (line0, line1) in merged:
            fr, sc = _refine_candidate(line0, line1, None, "unknown", lateral_known)
            if fr is not None:
                found.append((sc, fr))
            elif sc < 0:
                blank_windows.append((line0, line1))
        found.sort(key=lambda t: (-t[0], t[1].line0))   # the better-confirmed first
        added = []
        for _sc, fr in found:
            match = next((h for h in hole_frames if abs(h.line0 - fr.line0) < match_px), None)
            if match is not None:
                match.located_by = "holes+frame"
                continue
            # Two pictures never overlap: an image the frame locator finds
            # on top of one already accepted (the hole locator's, or a
            # better-confirmed frame-located one) is a disagreement, and the
            # hole locator's frames stand unchanged.
            if any(min(fr.line1, o.line1) - max(fr.line0, o.line0) > overlap_px
                   for o in hole_frames + added):
                continue
            fr.located_by = "frame"
            added.append(fr)
        if added:
            frames_out = sorted(frames_out + added, key=lambda f: f.line0)
            for i, f in enumerate(frames_out):
                f.index = i + 1
        # Blank frame positions: a window the gaps predict that is clear film,
        # lying wholly on film (the clear leader beyond a free film end is not
        # one: that window reaches lines without film) and not on top of an
        # image that was found.
        edge_px = BLANK_EDGE_TOL_MM * px_per_mm
        for (b0, b1) in sorted(blank_windows):
            # A window predicted by a hole may begin inside a gap (or end in
            # one): the clear frame starts where the gap ends.
            for (g0, g1, _t0, _t1) in gaps:
                if g0 <= b0 < g1:
                    b0, b1 = g1, g1 + img_px
                    break
                if g0 < b1 <= g1:
                    b0, b1 = g0 - img_px, g0
                    break
            if b0 < -edge_px or b1 > n_lines + edge_px:
                continue
            b0, b1 = max(0.0, b0), min(float(n_lines), b1)
            i0, i1 = int(np.ceil(b0)), int(np.floor(b1))
            if i1 <= i0 or not bool(film_present[i0:i1].all()):
                continue
            if any(min(b1, o.line1) - max(b0, o.line0) > overlap_px for o in frames_out):
                continue
            if any(min(b1, e1) - max(b0, e0) > 0.5 * (b1 - b0) for (e0, e1) in blanks_out):
                continue
            blanks_out.append((float(b0), float(b1)))
    if frames_out or blanks_out:
        reason = ""
    return ApertureFind(False, (lo, hi), film_line_start, film_line_end,
                         leading_continuation, frames_out, reason, rail_hidden_mm,
                         film_runs=film_runs_out, blanks=blanks_out)


def product_bounds(find: FrameFind, *, dpi: float | None = None, pad_mm: float = 0.0,
                   shape=None) -> tuple:
    """The integer slice bounds (line_lo, line_hi, col_lo, col_hi) `crop`
    uses: [find.line0:find.line1, find.col0:find.col1] grown outward by
    `pad_mm` on every side (needs `dpi`; both axes use dpi/25.4 px per
    mm, as `detect` does), the columns never padded past
    `find.col_clamp` (the film band minus RAIL_EDGE_INSET_MM: beyond it
    lie the rail's bright rim, the light gap at the film's cut edge, or
    air -- nothing a picture can contain), and everything clamped to
    `shape` (lines, cols) when given. Shared by `crop` and by
    tools/film110_check.py --edge-check so the check measures the
    product that is actually written.
    """
    if pad_mm and dpi is None:
        raise ValueError("pad_mm needs dpi")
    pad = pad_mm * dpi / MM_PER_INCH if pad_mm else 0.0
    lo_l = int(np.floor(find.line0 - pad))
    hi_l = int(np.ceil(find.line1 + pad))
    lo_c = int(np.floor(find.col0 - pad))
    hi_c = int(np.ceil(find.col1 + pad))
    if find.col_clamp is not None:
        c_lo, c_hi = find.col_clamp
        lo_c = max(lo_c, int(np.ceil(c_lo)))
        hi_c = min(hi_c, int(np.floor(c_hi)))
    if shape is not None:
        n_lines, n_cols = shape[0], shape[1]
        lo_l = min(max(lo_l, 0), n_lines)
        hi_l = min(max(hi_l, lo_l), n_lines)
        lo_c = min(max(lo_c, 0), n_cols)
        hi_c = min(max(hi_c, lo_c), n_cols)
    return lo_l, hi_l, lo_c, hi_c


def crop(image, find: FrameFind, *, dpi: float | None = None, pad_mm: float = 0.0):
    """Slice `image` to `product_bounds(find, dpi, pad_mm)`. Works on a
    3-channel visible array and a 2-D IR array alike -- the SAME indices
    apply to both, which is what keeps a visible/IR pair registered
    through the crop (the dual scan path already relies on this: same
    crop indices, same grid).
    """
    arr = np.asarray(image)
    lo_l, hi_l, lo_c, hi_c = product_bounds(find, dpi=dpi, pad_mm=pad_mm, shape=arr.shape)
    return arr[lo_l:hi_l, lo_c:hi_c]


# ---------------------------------------------------- numbering (protocol)
# Under the two-placement protocol (docs/film-110.md §3) image 1's start
# sits PLACEMENT_A_PHASE_MM inside aperture 1 in placement A, and half a
# film pitch further on in placement B. An image's number is then its
# position along the strip, whichever placement and apertures it was
# scanned in -- so the same photograph gets the same number in A and B.
# Measured on the 2026-09-19 strip (n = 1 strip, one operator): A image 1
# at 3.53 / 3.58 mm (3600 / 600 dpi); B image 1 at 15.73 mm against the
# derived 16.25. Rounding tolerates +/- PLACEMENT_PHASE_TOLERANCE_MM.
PLACEMENT_A_PHASE_MM = 3.5
PLACEMENT_PHASE_TOLERANCE_MM = 6.0


def placement_phase_mm(placement: str, film: Film = FILM_110) -> float:
    """Image 1's start inside aperture 1 for placement "A" or "B"
    (docs/film-110.md §3): A is the strip loaded flush (a hair of clear
    film before image 1, exactly like a 35 mm strip); B is shifted half a
    film pitch further on, so the bar that split image 2 in A falls
    between images 1 and 2 instead."""
    if placement == "A":
        return PLACEMENT_A_PHASE_MM
    if placement == "B":
        return PLACEMENT_A_PHASE_MM + film.pitch_mm / 2
    raise ValueError(f'placement must be "A" or "B", got {placement!r}')


def image_number(aperture: int, line0: float, dpi: float, placement: str,
                 film: Film = FILM_110, holder=STRIP) -> tuple:
    """Strip-position number of the image whose start is `line0` (px,
    aperture-registered) in `aperture` (1-based), and the residual in mm
    between that position and the nearest phase-grid position (signed,
    |residual| <= pitch/2). The caller refuses when |residual| >
    PLACEMENT_PHASE_TOLERANCE_MM or the number is < 1.

    `global_mm` is `line0`'s position measured from aperture 1's own
    start, along the holder's own aperture grid (`holder.pitch_mm`) --
    NOT the film's pitch, since apertures and film images do not share
    one grid. `k` is then how many film pitches past this placement's
    phase that position is; the image number is `k` rounded to the
    nearest integer, offset by one (image 1 is k == 0)."""
    px_per_mm = dpi / MM_PER_INCH
    global_mm = (aperture - 1) * holder.pitch_mm + line0 / px_per_mm
    k = (global_mm - placement_phase_mm(placement, film)) / film.pitch_mm
    n = round(k) + 1
    residual = (k - round(k)) * film.pitch_mm
    return n, residual


# The two placements' phases are half a film pitch (12.75 mm) apart, so a
# strip seated by the visual rule of docs/film-110.md §3 lies within a few
# mm of exactly one of them. An image whose position is farther than this
# from BOTH phases is midway between them (the farthest possible is
# pitch/4 = 6.4 mm from each) and the data cannot say which placement the
# operator meant. 4.0 mm sits inside the +/-6 mm rounding tolerance used
# for numbering, so an inferred placement never trips the phase check.
PLACEMENT_INFER_TOLERANCE_MM = 4.0


def infer_placement(aperture: int, line0: float, dpi: float,
                    film: Film = FILM_110, holder=STRIP) -> tuple:
    """Which placement ("A" or "B") a strip is in, from one image's
    position alone: the placement whose phase grid the image lies closer
    to, and that signed residual in mm. (None, residual) when even the
    closer residual exceeds PLACEMENT_INFER_TOLERANCE_MM -- the image is
    midway between the two phases and the operator must say. Pure."""
    best = None
    for placement in ("A", "B"):
        _n, residual = image_number(aperture, line0, dpi, placement,
                                    film=film, holder=holder)
        if best is None or abs(residual) < abs(best[1]):
            best = (placement, residual)
    placement, residual = best
    if abs(residual) > PLACEMENT_INFER_TOLERANCE_MM:
        return None, residual
    return placement, residual


# ------------------------------------------------------------ two strips
#
# `digitize --film 110 --strips 2` (docs/film-110.md §12) loads two 110
# strips side by side in the six-aperture holder: slot 1 = apertures 1-3,
# slot 2 = apertures 4-6. Slot 1 is numbered exactly like a lone strip
# (`image_number`, phase from the holder's left end). Slot 2's strip lies
# against the holder's RIGHT end, so its start depends on the strip's
# length and leader: its numbering is anchored by the data instead -- its
# first numbered image is image 1, every later one is numbered by its
# distance from that one in film pitches (`image_number_anchored`).
# Nothing here assumes which lateral rail a strip lies against.
SLOT_APERTURES = 3
# An image cannot start before the film does; an image predicted this far
# before a free film start (px_per_mm * mm) is a hole's ghost, not an image.
FILM_START_TOLERANCE_MM = 1.0


def slot_of(aperture: int, strips: int = 1) -> int:
    """1-based slot of a holder aperture: always 1 when `strips == 1`;
    with two strips, apertures 1-3 are slot 1 and 4-6 slot 2."""
    if strips <= 1:
        return 1
    return (aperture - 1) // SLOT_APERTURES + 1


def slot_apertures(slot: int) -> range:
    """The holder apertures of a slot (two-strip loads)."""
    first = (slot - 1) * SLOT_APERTURES + 1
    return range(first, first + SLOT_APERTURES)


def global_position_mm(aperture: int, line0: float, dpi: float,
                       holder=STRIP) -> float:
    """`line0` (px, aperture-registered) as a position in mm from
    aperture 1's start along the holder's own aperture grid -- the same
    coordinate `image_number` works in."""
    return (aperture - 1) * holder.pitch_mm + line0 / (dpi / MM_PER_INCH)


def image_number_anchored(line0_mm_global: float, anchor_mm: float,
                          film: Film = FILM_110) -> tuple:
    """Number (1-based) and phase residual in mm (signed, |r| <= pitch/2)
    of the image starting at `line0_mm_global`, counted from the image
    that starts at `anchor_mm` (image 1). The anchor itself is (1, 0.0).
    A number < 1 means the image lies before the anchor; the caller
    refuses it. Pure."""
    k = (line0_mm_global - anchor_mm) / film.pitch_mm
    n = round(k) + 1
    return n, (k - round(k)) * film.pitch_mm


# ------------------------------------------------------- free passes (shift)
#
# `digitize --film 110` runs on an existing roll are not tied to the two
# fixed placements: the strip may be seated anywhere, slid or turned. From
# the second run on, numbering therefore works from the roll's OWN earlier
# data. The strip moved as one piece between two runs, so every image's
# position differs from the recorded one by the same amount (Test 103:
# within 0.3 mm); that amount is the SHIFT. Because the film is periodic a
# shift is only known modulo the film pitch from positions alone, so the
# shift is never taken from positions: it is the one at which the images
# LOOK like the recorded ones (`decide_orientation`, below), and a run in
# which no image can be recognised is refused rather than guessed.
SHIFT_AGREE_MM = 2.0            # pairings within this of each other are one shift
SHIFT_RESIDUAL_MM = 5.0         # an image farther off the shifted grid than this is
                                # "uncertain" (delivered, flagged). Real film pitch runs
                                # 24.85-25.5 mm against the model's 25.5 (Tests 103, 104):
                                # three pitches from the nearest certain sighting is 2 mm
                                # off by itself; 5 mm stays far below half a pitch (12.75)


def number_by_shift(position_mm: float, shift_mm: float, recorded,
                    film: Film = FILM_110, direction: int = 1) -> tuple:
    """Number (< 1 means before the roll's first-seen image) and residual
    in mm (signed, |r| <= pitch/2) of the image starting at
    `position_mm`, counted from the recorded image (number, position_mm)
    that lies nearest once the strip's `shift_mm` is taken off.
    `direction` is +1 when the recorded grid's numbers grow along the
    holder, -1 when the reference record was itself TURNED against pass
    1 (its numbers then fall along the holder: Test 104, load 2, pass 3
    numbered a three-image strip's middle image "4" and another's "2"
    twice). `recorded` must not be empty. Pure."""
    num, pos = min(recorded,
                   key=lambda e: abs(position_mm - e[1] - shift_mm))
    k = (position_mm - pos - shift_mm) / film.pitch_mm
    return num + direction * round(k), (k - round(k)) * film.pitch_mm


def bar_spans_mm(slot: int = 1, strips: int = 2, holder=STRIP) -> list:
    """The plastic that can split an image of a slot's strip, as (start,
    end) mm along the holder: the crossbar after each of its apertures,
    and the holder's end wall before aperture 1 (slot 1, or a lone strip)
    -- taken as one bar wide, so centring it in a gap works like any bar.
    (Test 104: image 1 seated at 0.0 mm was "split by the leading bar"
    and the advice found no bar to centre.) A lone strip (`strips` 1)
    owns all six apertures; slot 2's last bar is the holder's far end."""
    aps = slot_apertures(slot) if strips == 2 else range(1, 7)
    bar = holder.pitch_mm - holder.aperture_mm
    spans = [((j - 1) * holder.pitch_mm + holder.aperture_mm,
              j * holder.pitch_mm) for j in aps]
    if aps[0] == 1:
        spans.insert(0, (-bar, 0.0))
    return spans


def bar_centering_shifts(position_mm: float, bars, film: Film = FILM_110) -> list:
    """Shifts (mm, signed along the holder) that put a bar which splits the
    image starting at `position_mm` into the gap between films -- the gap
    after the image or the gap before it -- at the gap's middle. The gap is
    a pitch minus an image long (8.3 mm), the bar about 2 mm, so the
    centring leaves about 3 mm either way. Pure."""
    length = film.image_mm[0]
    gap = film.pitch_mm - length
    out = []
    for b0, b1 in bars:
        # A bar within WHOLE_MARGIN_MM of the image splits it, as the
        # detector's whole/split rule says (an image seated exactly at
        # the holder's end wall touches it without overlapping it).
        if b1 > position_mm - WHOLE_MARGIN_MM and b0 < position_mm + length + WHOLE_MARGIN_MM:
            c = (b0 + b1) / 2
            out.append(c - (position_mm + length + gap / 2))
            out.append(c - (position_mm - gap / 2))
    return out


# ------------------------------------------- slid or turned? (image content)
#
# Between two runs the operator may also TURN the strip 180 degrees in its
# plane (docs/film-110.md): the images then lie in reverse order along the
# holder and each picture is rotated 180 degrees in the scan. With a
# regular image pitch the positions alone cannot tell a turned strip from a
# slid one (a three-image strip turned is geometrically a slide), so the
# decision is made on what the images look like: small, normalised
# thumbnails of the new images are correlated with thumbnails of the
# recorded ones, once as they are (SLIDE) and once with the new thumbnail
# rotated 180 degrees (TURNED). The numbers stay with the photographs.
THUMB_SIZE = (96, 72)           # (lines, columns): a 17.2 x 13.0 mm image, ~96 px long
THUMB_INSET = 0.12              # fraction of each side cut off before binning: the
                                # product pad, the gate halo and the printed border
                                # are the same on every image and would make any
                                # two images correlate; only the picture decides
ORIENT_MIN_CORR = 0.5           # the winning hypothesis' mean correlation at least ...
ORIENT_MARGIN = 0.15            # ... and this far above the other one, OR
ORIENT_EVIDENCE_MARGIN = 1.0    # ... its evidence (the sum over DISTINCT new sightings of each one's
                                # best correlation in the cluster) this far above the runner-up's: a strip
                                # of near-duplicate shots (Test 106: 0.96 against 0.84) is decided by how
                                # many different pictures agree, not by one pair
END_AGREE_MM = 3.0              # a slide must agree with a free film end seen in both runs
RANGE_SLACK_MM = 3.0            # a hypothesis may put a recorded image this far outside the
                                # slot's range (edge refinement and the wall's exact place:
                                # Test 104, roll 6, a strip turned about its own centre put
                                # photograph 4 at -17.7 mm against the range's -17.2 and the
                                # true hypothesis was pruned)
PATCH_PX_PER_MM = 8.0           # the working copy of a sighting kept for the content check
OVERLAP_MIN_MM = 6.0            # two sightings of an image are compared on at least this much


def thumbnail(arr, size=THUMB_SIZE, inset=THUMB_INSET):
    """`arr` ((lines, cols) or (lines, cols, channels)), its outer `inset`
    fraction on every side cut off, averaged into `size` bins and
    normalised to zero mean and unit variance (all zeros for a flat
    patch). Pure; the area average makes the thumbnail independent of the
    scan resolution."""
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 3:
        a = a.mean(axis=2)
    if inset:
        dy, dx = int(a.shape[0] * inset), int(a.shape[1] * inset)
        a = a[dy:a.shape[0] - dy, dx:a.shape[1] - dx]
    h, w = size
    if a.shape[0] < h or a.shape[1] < w:
        raise ValueError(f"crop {a.shape} is smaller than the thumbnail {size}")
    rows = np.linspace(0, a.shape[0], h + 1).astype(int)
    cols = np.linspace(0, a.shape[1], w + 1).astype(int)
    a = np.add.reduceat(a, rows[:-1], axis=0) / np.diff(rows)[:, None]
    a = np.add.reduceat(a, cols[:-1], axis=1) / np.diff(cols)[None, :]
    a = a - a.mean()
    sd = a.std()
    return (a / sd).astype(np.float32) if sd > 1e-9 else np.zeros_like(a, dtype=np.float32)


def patch(arr, px_per_mm: float):
    """A sighting's working copy for the content check: `arr` ((lines,
    cols[, channels]), the VISIBLE part of an image as scanned) averaged
    over channels and area-binned down to about PATCH_PX_PER_MM, as
    float32. Small enough to hold for every sighting of a strip, fine
    enough for a THUMB_SIZE thumbnail of a 6 mm overlap. Pure."""
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 3:
        a = a.mean(axis=2)
    f = max(1, int(round(px_per_mm / PATCH_PX_PER_MM)))
    if f > 1:
        h, w = (a.shape[0] // f) * f, (a.shape[1] // f) * f
        if h == 0 or w == 0:
            return a.astype(np.float32)
        a = a[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))
    return a.astype(np.float32)


def pair_correlation(new_item, old_item, kind: str, film: Film = FILM_110):
    """How much two sightings look like the same photograph. `new_item` =
    (position_mm, u0, u1, patch), `old_item` = (number, position_mm, u0,
    u1, patch): u0..u1 is the part of the image the sighting shows, in mm
    from the image's own start (a whole image is 0..length, a split shows
    less), `patch` from `patch()`. Under `kind` "same" the two show the
    same part where their u-ranges overlap; under "turned" the new
    sighting's u runs the other way (u' = length - u) and its picture is
    rotated 180 degrees. The overlapping parts are thumbnailed alike and
    correlated. None when they overlap by less than OVERLAP_MIN_MM. Pure."""
    _p, nu0, nu1, npatch = new_item
    _n, _r, ou0, ou1, opatch = old_item
    length = film.image_mm[0]
    if kind == "turned":
        nu0, nu1 = length - nu1, length - nu0
    a, b = max(nu0, ou0), min(nu1, ou1)
    if b - a < OVERLAP_MIN_MM:
        return None

    def rows(pch, u0, u1, lo, hi):
        n = pch.shape[0]
        r0 = int(round((lo - u0) / (u1 - u0) * n))
        r1 = int(round((hi - u0) / (u1 - u0) * n))
        return pch[max(0, r0):max(r0 + 1, min(n, r1))]

    size = (max(16, int(round(THUMB_SIZE[0] * (b - a) / length))), THUMB_SIZE[1])
    try:
        if kind == "turned":
            sub = rows(npatch, nu0, nu1, a, b)[::-1, ::-1]
        else:
            sub = rows(npatch, nu0, nu1, a, b)
        tn = thumbnail(sub, size)
        to = thumbnail(rows(opatch, ou0, ou1, a, b), size)
    except ValueError:
        return None
    return thumb_correlation(tn, to)


def thumb_correlation(a, b) -> float:
    """Pearson correlation of two thumbnails of the same shape (both
    produced by `thumbnail`). Pure."""
    return float(np.mean(np.asarray(a, np.float64) * np.asarray(b, np.float64)))


def _best_cluster(pairs):
    """pairs: [(key_mm, correlation)]. The group of pairs whose keys lie
    within SHIFT_AGREE_MM/2 of an anchor pair's, best by (members with a
    usable correlation, mean correlation). Returns (median key, mean
    correlation, members) or None."""
    best = None
    tol = SHIFT_AGREE_MM / 2
    for key0, _c in pairs:
        members = [(k, c) for k, c in pairs if abs(k - key0) <= tol]
        mean = sum(c for _k, c in members) / len(members)
        good = sum(1 for _k, c in members if c >= ORIENT_MIN_CORR)
        score = (good, mean)
        if best is None or score > best[0]:
            keys = sorted(k for k, _c in members)
            best = (score, (keys[len(keys) // 2], mean, len(members)))
    return None if best is None else best[1]


def decide_orientation(new, old, film: Film = FILM_110, *,
                       pos_range=None, end_shift=None):
    """Did the strip SLIDE or was it TURNED since an earlier record, and by
    how much? `new`: [(position_mm, u0, u1, patch)] -- every sighting of
    this run, whole or split; `old`: [(number, position_mm, u0, u1,
    patch)] of the record's sightings (see `pair_correlation` for the
    items).

    Every (new, old) pair that overlaps by OVERLAP_MIN_MM is a hypothesis:
    SLIDE by d = p - r, compared as they are; TURNED about c = p + r, the
    new picture rotated 180 degrees. Pairs that agree on d (or c) within
    SHIFT_AGREE_MM/2 form a cluster, as good as its best pair. Clusters are first pruned by what is
    physically possible -- `pos_range` (lo, hi): every recorded image
    must still lie inside it after the move -- and by the strip's own
    free end when one was seen in both runs (`end_shift` = its move, mm:
    a slide must agree with it within END_AGREE_MM; a turn is not
    checked against it). Among the clusters left, the best -- most
    evidence (the sum over distinct new sightings of each one's best
    correlation in the cluster), then most usable pairs, then best
    correlation -- wins when its best pair is at least ORIENT_MIN_CORR
    and either ORIENT_MARGIN above every other remaining cluster's best
    pair, of either kind, or ORIENT_EVIDENCE_MARGIN above the best
    rival's evidence. No window on the shift: a move of
    more than half a pitch, or of whole apertures, is told from a smaller
    one by the pictures (and the free end), which positions alone cannot
    do -- the film is periodic. Returns {"orientation": "same"|"turned",
    "key": mm, "corr": r, "other": r, "pairs": n} or None when no two
    sightings overlap or the pictures do not decide it. Pure."""
    slide, turned = [], []
    for ni, item in enumerate(new):
        p = item[0]
        for oitem in old:
            r = oitem[1]
            c = pair_correlation(item, oitem, "same", film)
            if c is not None:
                slide.append((p - r, c, ni))
            c = pair_correlation(item, oitem, "turned", film)
            if c is not None:
                turned.append((p + r, c, ni))
    if not slide and not turned:
        return None
    length = film.image_mm[0]
    recorded = [o[1] for o in old]

    def possible(kind, key):
        if pos_range is not None:
            lo, hi = pos_range
            moved = ([r + key for r in recorded] if kind == "same"
                     else [key - r - length for r in recorded])
            if any(not (lo - RANGE_SLACK_MM <= m <= hi + RANGE_SLACK_MM) for m in moved):
                return False
        if (end_shift is not None and kind == "same"
                and abs(key - end_shift) > END_AGREE_MM):
            return False
        return True

    tol = SHIFT_AGREE_MM / 2
    clusters = []
    for kind, pairs in (("same", slide), ("turned", turned)):
        for key0, _c, _i in pairs:
            members = [(k, c, i) for k, c, i in pairs if abs(k - key0) <= tol]
            keys = sorted(k for k, _c, _i in members)
            key = keys[len(keys) // 2]
            if not possible(kind, key):
                continue
            # A cluster is as good as its best pair: a split sighting with
            # a few millimetres of flat overlap correlates with nothing,
            # and must not drag down the cluster its geometry puts it in
            # (Test 104, load 2: 0.84 and -0.19 in the true cluster).
            best = max(c for _k, c, _i in members)
            good = sum(1 for _k, c, _i in members if c >= ORIENT_MIN_CORR)
            # Evidence: each DISTINCT new sighting counts once, by its best
            # correlation in the cluster; a member that correlates with
            # nothing adds nothing (never subtracts).
            per_new: dict = {}
            for _k, c, i in members:
                per_new[i] = max(per_new.get(i, 0.0), c)
            evidence = sum(max(0.0, v) for v in per_new.values())
            clusters.append(((evidence, good, best), kind, key, best, len(members), evidence))
    if not clusters:
        return None
    clusters.sort(key=lambda x: x[0], reverse=True)
    _score, kind, key, best, n, evidence = clusters[0]
    rivals = [c for c in clusters[1:] if c[1] != kind or abs(c[2] - key) > tol]
    other = max((c[3] for c in rivals), default=-1.0)
    runner_up = max((c[5] for c in rivals), default=0.0)
    if best < ORIENT_MIN_CORR:
        return None
    if best - other < ORIENT_MARGIN and evidence - runner_up < ORIENT_EVIDENCE_MARGIN:
        return None
    return {"orientation": kind, "key": key, "corr": best, "other": other,
            "pairs": n, "evidence": evidence, "runner_up_evidence": runner_up}


def slot_position_range(slot: int = 1, strips: int = 1, film: Film = FILM_110,
                        holder=STRIP) -> tuple:
    """(lo, hi) mm along the holder where an image START of this slot's
    strip can lie: from one image length before the slot's first aperture
    (a leading split) to the end of its last one. A lone strip owns the
    whole holder. Pure."""
    aps = slot_apertures(slot) if strips == 2 else range(1, 7)
    lo = (aps[0] - 1) * holder.pitch_mm - film.image_mm[0]
    hi = aps[-1] * holder.pitch_mm
    return lo, hi


def number_turned(position_mm: float, c_mm: float, recorded,
                  film: Film = FILM_110, direction: int = 1) -> tuple:
    """`number_by_shift` for a strip TURNED since the reference record:
    the new image starting at `position_mm` is the photograph that
    started at c - position_mm in the record's frame (c from
    `decide_orientation`); `direction` is the record's own, as in
    `number_by_shift`. Pure."""
    return number_by_shift(c_mm - position_mm, 0.0, recorded, film, direction)
