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
LEADING_CONTINUATION_MM = 1.0
LEADING_CONTINUATION_FRAC = 0.8
CLEAR_BASE_PERCENTILE = 90.0

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
    """

    index: int
    perforation_line: float
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
HOLE_ZONE_FROM_MM = 0.3         # hole zone starts this far inside the per-line film edge ...
HOLE_ZONE_TO_MM = 3.0           # ... and ends this far inside it
HOLE_STUB_MIN_MM = 0.3          # a hole cut by the aperture boundary may be this short


def _track_edge(green: np.ndarray, band_edge: int, air: float,
                px_per_mm: float, edge_is_low: bool) -> np.ndarray:
    """Per-line column of the film's air-facing edge (float, NaN where no
    film is seen in the search zone -- e.g. beyond a free strip end)."""
    n_lines, width = green.shape
    search = max(1, round(EDGE_SEARCH_MM * px_per_mm))
    z_lo, z_hi = max(0, band_edge - search), min(width, band_edge + search)
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
               film_present: np.ndarray, px_per_mm: float, edge_is_low: bool):
    """Candidate perforation runs on one lateral edge of the film band,
    as (start, end_inclusive) line runs, plus the per-line LOCAL film
    edge (rolling median of the tracked edge) used for lateral
    prediction. A hole line is one whose zone [edge + HOLE_ZONE_FROM_MM,
    edge + HOLE_ZONE_TO_MM] (measured inward from THAT line's film edge)
    reaches air level."""
    n_lines = green.shape[0]
    band_edge = lo if edge_is_low else hi
    edge = _track_edge(green, band_edge, air, px_per_mm, edge_is_low)
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
    zone_max = np.max(green[np.arange(n_lines)[:, None], cols], axis=1)
    hole_bool = (zone_max >= HOLE_FRAC * air) & film_present & valid
    runs = _true_runs(hole_bool)
    min_len = HOLE_MIN_MM * px_per_mm
    max_len = HOLE_MAX_MM * px_per_mm
    stub_len = HOLE_STUB_MIN_MM * px_per_mm
    runs = [(s, e) for (s, e) in runs
            if (min_len <= (e - s + 1) <= max_len)
            or ((s == 0 or e == n_lines - 1) and stub_len <= (e - s + 1) <= max_len)]
    return runs, local


# Plausibility of a predicted image: the mean of the interior profile
# over the part of the window inside the aperture must be denser than
# this fraction of the clear-base level, else the window is clear film
# (a hole at the strip's cut end predicts an image that is not there).
IMAGE_DENSITY_FRAC = 0.85
IMAGE_VARIATION_FRAC = 0.10       # std/mean of the profile inside a window that is image, not base
RAIL_GUARD_MM = 0.4             # lateral refinement stays this far from the rail-side band edge
RIM_GUARD_MM = 1.0              # ... and this far inside the perforated film edge (rim 0.6 mm, hole 0.6-2.1 mm)
PRODUCT_PAD_MM = 0.25           # the CLI grows a whole image's crop by this on every side
MIN_OVERLAP_MM = 1.0            # a window overlapping the aperture by less carries nothing
MERGE_FRAC_OF_PITCH = 0.5       # two predictions closer than this are the same image


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
    if abs(band_width_mm - film.width_mm) > FILM_WIDTH_TOLERANCE_MM:
        return ApertureFind(False, (lo, hi), None, None, False, [],
                             f"no 110 film band ({band_width_mm:.1f} mm)")

    touches_air_lo = lo > 0 and bool(is_air[lo - 1])
    touches_air_hi = hi < width and bool(is_air[hi])

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

    if not (touches_air_lo or touches_air_hi):
        return ApertureFind(False, (lo, hi), film_line_start, film_line_end,
                             leading_continuation, [],
                             "film band not adjacent to air; perforations hidden")

    # d. Perforations: resolve the perforated edge and its hole runs.
    edge_is_low = None
    runs: list = []
    local_edge = None
    if touches_air_lo and not touches_air_hi:
        edge_is_low = True
        runs, local_edge = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, True)
    elif touches_air_hi and not touches_air_lo:
        edge_is_low = False
        runs, local_edge = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, False)
    else:
        runs_lo, local_lo = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, True)
        runs_hi, local_hi = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, False)
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
        return ApertureFind(False, (lo, hi), film_line_start, film_line_end,
                             leading_continuation, [], reason)

    perforated_edge_col = lo if edge_is_low else hi
    lat_len_px = film.image_mm[1] * px_per_mm
    offset_px = film.image_lateral_offset_mm * px_per_mm
    whole_margin_px = WHOLE_MARGIN_MM * px_per_mm
    pitch_px = film.pitch_mm * px_per_mm
    min_overlap_px = MIN_OVERLAP_MM * px_per_mm
    if film_present.any():
        clear_base = float(np.percentile(interior_profile[film_present],
                                          CLEAR_BASE_PERCENTILE))
    else:
        clear_base = air

    def _evaluate(reversed_: bool):
        """Refine and classify every plausible candidate under one
        lead/trail assignment; returns (frames, score) where score is
        the number of along-transport edges the data confirmed."""
        out = []
        score = 0
        for (line0, line1, perforation_line) in _candidates(runs, film, px_per_mm, reversed_):
            clo = max(0, int(np.floor(line0)))
            chi = min(n_lines, int(np.ceil(line1)))
            if chi - clo < min_overlap_px:
                continue
            # Plausibility: the window must look like an image -- denser
            # than clear film, OR varying along its length (image content
            # varies, clear film does not). The second test matters where
            # the aperture holds little clear film to estimate the base
            # from (a fogged leader plus one image, 2026-09-19 aperture 1).
            seg = interior_profile[clo:chi]
            seg_mean = float(np.mean(seg))
            seg_std = float(np.std(seg))
            if (seg_mean >= IMAGE_DENSITY_FRAC * clear_base
                    and seg_std < IMAGE_VARIATION_FRAC * max(seg_mean, 1.0)):
                continue

            r_line0, ok_l0 = _refine_edge(interior_profile, line0, px_per_mm, outward=-1)
            r_line1, ok_l1 = _refine_edge(interior_profile, line1, px_per_mm, outward=+1)
            score += int(ok_l0) + int(ok_l1)

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
            if edge_is_low:
                col0 = edge_here + offset_px
                col1 = col0 + lat_len_px
            else:
                col1 = edge_here - offset_px
                col0 = col1 - lat_len_px

            # Lateral refinement, both sides. The step direction is read
            # from the levels either side (see _refine_edge), so the
            # perforated side -- picture next to the dark printed border,
            # with the film's bright rim and the hole further out -- is
            # refined too, with the search kept RIM_GUARD_MM inside the
            # tracked film edge so the rim never enters the window (the
            # first version left this edge as the 2.0 mm model prediction,
            # which sat 0.2 mm inside the picture's foot on all four
            # production frames of 2026-09-19). On the rail side the
            # search stops RAIL_GUARD_MM short of the band so the rail's
            # own bright rim stays out.
            col_profile = np.median(green[clo:chi, :], axis=0)
            guard = RAIL_GUARD_MM * px_per_mm
            rim_guard = RIM_GUARD_MM * px_per_mm
            if edge_is_low:
                r_col0, ok_c0 = _refine_edge(col_profile, col0, px_per_mm, outward=-1,
                                             limit_lo=edge_here + rim_guard)
                r_col1, ok_c1 = _refine_edge(col_profile, col1, px_per_mm, outward=+1,
                                             limit_hi=hi - guard)
            else:
                r_col0, ok_c0 = _refine_edge(col_profile, col0, px_per_mm, outward=-1,
                                             limit_lo=lo + guard)
                r_col1, ok_c1 = _refine_edge(col_profile, col1, px_per_mm, outward=+1,
                                             limit_hi=edge_here - rim_guard)

            whole = (r_line0 >= whole_margin_px) and (r_line1 <= n_lines - whole_margin_px)
            if r_line0 < 0:
                split = "leading"
            elif r_line1 > n_lines:
                split = "trailing"
            else:
                split = None

            free_end_near = False
            for fend in (film_line_start, film_line_end):
                if fend is None:
                    continue
                if abs(r_line0 - fend) <= pitch_px or abs(r_line1 - fend) <= pitch_px:
                    free_end_near = True
                    break

            size_mm = ((r_line1 - r_line0) / px_per_mm, (r_col1 - r_col0) / px_per_mm)
            out.append(FrameFind(
                index=len(out) + 1,
                perforation_line=perforation_line,
                line0=r_line0, line1=r_line1,
                col0=r_col0, col1=r_col1,
                whole=whole, split=split,
                refined={"line0": ok_l0, "line1": ok_l1, "col0": ok_c0, "col1": ok_c1},
                free_end_near=free_end_near,
                size_mm=size_mm,
                orientation="hole-before-image" if not reversed_ else "hole-after-image",
            ))
        return out, score

    # The strip may be inserted either way round; the two assignments of
    # lead/trail differ by ~1 mm, more than the refinement window, so the
    # data decides: the assignment whose predicted along-transport edges
    # the profile confirms more often wins; a tie keeps the normal one.
    frames_normal, score_normal = _evaluate(False)
    frames_rev, score_rev = _evaluate(True)
    frames_out = frames_rev if score_rev > score_normal else frames_normal

    return ApertureFind(False, (lo, hi), film_line_start, film_line_end,
                         leading_continuation, frames_out, "")


def crop(image, find: FrameFind, *, dpi: float | None = None, pad_mm: float = 0.0):
    """Slice `image` to [find.line0:find.line1, find.col0:find.col1],
    grown outward by `pad_mm` on every side (needs `dpi`; both axes use
    dpi/25.4 px per mm, as `detect` does), clamped to the array bounds.
    Works on a 3-channel visible array and a 2-D IR array alike -- the
    SAME indices apply to both, which is what keeps a visible/IR pair
    registered through the crop (the dual scan path already relies on
    this: same crop indices, same grid).
    """
    if pad_mm and dpi is None:
        raise ValueError("pad_mm needs dpi")
    pad = pad_mm * dpi / MM_PER_INCH if pad_mm else 0.0
    arr = np.asarray(image)
    n_lines, n_cols = arr.shape[0], arr.shape[1]
    lo_l = int(np.floor(find.line0 - pad))
    hi_l = int(np.ceil(find.line1 + pad))
    lo_c = int(np.floor(find.col0 - pad))
    hi_c = int(np.ceil(find.col1 + pad))
    lo_l = min(max(lo_l, 0), n_lines)
    hi_l = min(max(hi_l, lo_l), n_lines)
    lo_c = min(max(lo_c, 0), n_cols)
    hi_c = min(max(hi_c, lo_c), n_cols)
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
