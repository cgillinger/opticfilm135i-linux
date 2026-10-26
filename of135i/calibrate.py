"""Calibration formulas for the of135i driver.

Implements the three calibration computations documented in
cal-data/cal-analysis.md (read that file's "Resolution" section first
-- it is the final, solved word on the shading-table format):

  - gain_codes():   AFE gain (regs 2/3/4) from a white-line measurement.
  - offset_codes(): AFE offset (regs 5/6/7) from the dark bracket
                     measurements (linear extrapolation from the
                     [0x80, 0xff] two-point bracket).
  - shading_table(): the 45,856 B per-pixel shading-correction upload
                     from a 128-line dark-current-free measurement.

All three operate on plain numpy arrays; no device I/O here (except
logging, which is side-effect-only diagnostics).
"""

from __future__ import annotations

import logging
import struct

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from typing import NamedTuple

log = logging.getLogger("of135i")

# ----------------------------------------------------------------- gain

# Best-fit target level for AFE gain, from cal-analysis.md section 2:
# actual_gain = code / 32, target = white-line peak (99.9th percentile
# per channel) ~= 31673 counts. Confidence: moderate (2-4% residual,
# best of all tested forms).
_GAIN_TARGET = 31673
_GAIN_DIVISOR = 32
_GAIN_MAX_CODE = 63


def gain_codes(white_line: np.ndarray, clamp_nonpositive: bool = False) -> tuple[int, int, int]:
    """Compute AFE gain codes (regs 2/3/4, R/G/B) from a white-line scan.

    `white_line` is (N, 3) uint16, pixel-interleaved RGB (one scanned
    line, gain=0, final applied offset -- see cal_white in tables.py).
    Per channel: peak = 99.9th percentile (robust against single hot
    pixels/outliers vs. a bare max), code = round(32 * target / peak),
    clamped to the AFE gain register's 6-bit range [0, 63].

    A non-positive peak (an all-zero white line) normally raises: on
    the plain calibration path it means something is wrong. With
    clamp_nonpositive=True it instead maps to the maximum gain code
    (a dark white line physically calls for maximum gain), so the
    cold-start warmup loop can treat a zero read as "lamp not ready
    yet" and retry rather than crash -- see Scanner._gain_with_warmup.

    Validated against cal-data/capture/cal-frame00501-len31104.bin:
    expect (0x2e, 0x21, 0x29), +/-1 per channel.
    """
    arr = np.asarray(white_line)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError(f"expected (N, 3) array, got shape {arr.shape}")

    codes = []
    for ch in range(3):
        peak = np.percentile(arr[:, ch].astype(np.float64), 99.9)
        if peak <= 0:
            if clamp_nonpositive:
                codes.append(_GAIN_MAX_CODE)
                continue
            raise ValueError(f"channel {ch}: non-positive peak level ({peak})")
        code = round(_GAIN_DIVISOR * _GAIN_TARGET / peak)
        codes.append(int(min(max(code, 0), _GAIN_MAX_CODE)))
    return tuple(codes)  # type: ignore[return-value]


# --------------------------------------------------------------- offset

# Bracket codes used by the vendor's dark calibration (gain=0):
_OFFSET_BRACKET_LO = 0x80   # offset code for dark_a measurement
_OFFSET_BRACKET_HI = 0xFF   # offset code for dark_b measurement

# Dark-level margin (raw counts at gain=0) above the dark_b
# measurement. Derived from the reference unit's vendor capture:
# the vendor's final codes (R=0x010b=267, G=0x010a=266, B=0x010b=267)
# are 12/11/12 code steps above 0xff.  Multiplied by the reference
# unit's per-channel slope (~17.6/18.0/17.9 counts per code step)
# this gives a margin of ~211/198/215 dark-level counts above the
# 0xff measurement.
#
# On a unit with the same slope this reproduces the vendor codes
# exactly. On a unit with a different slope (different AFE voltage-
# per-step) the code count adapts proportionally while targeting the
# same dark-level increment.
#
# Limitation: the margin values themselves are from a single unit.
# Cross-unit testing is needed to validate this model.
_OFFSET_LEVEL_MARGIN = (211.0, 198.0, 215.0)   # R, G, B

# Fallback: the vendor's empirically observed final codes, used when
# the bracket slope is too low to be meaningful (e.g. all-zero dark
# buffers from a mock, or a malfunctioning AFE).
_OFFSET_DEFAULT = (0x010B, 0x010A, 0x010B)

# Minimum plausible slope (counts per offset code step). The reference
# unit measures ~17.6–18.0; anything below 1.0 is anomalous and should
# fall back to the hardcoded default.
_OFFSET_MIN_SLOPE = 1.0

# A healthy dark buffer is sensor noise: hundreds to thousands of distinct
# raw values (~2500 in the reference measurements). A residual dark_b --
# returned by the device without a real exposure on a later batch frame --
# is a short repeating block, ~8 distinct values (docs/test-log.md Test 32:
# the frame's own dark_a tail, repeated). This threshold sits far below any
# healthy buffer and far above any residual, so the classification has a
# wide margin either way.
_DARK_MIN_UNIQUE = 32


def dark_is_residual(arr: np.ndarray) -> bool:
    """True when a dark buffer looks like residual/canned data rather than
    a real measurement: a short repeating block of a few distinct values.
    ``arr`` is a uint16 dark buffer, (N, 3) or flat. Content-based, not
    timing-based (timing is host-dependent). See Test 32.

    Range is ``1 < unique < _DARK_MIN_UNIQUE``: the proven residual (Test
    32, the dark_a tail repeated) has ~8 distinct values, well inside it.
    A *single* distinct value (unique == 1: an all-constant buffer, e.g. a
    dead AFE or a zero-filled mock) is deliberately NOT treated as residual
    -- offset_codes()'s slope<1 fallback already owns that case, and
    substituting there would change long-standing behaviour."""
    a = np.asarray(arr)
    return 1 < int(np.unique(a).size) < _DARK_MIN_UNIQUE


def offset_codes(
    dark_a: np.ndarray, dark_b: np.ndarray
) -> tuple[int, int, int]:
    """AFE offset codes (regs 5/6/7, R/G/B), each a 16-bit AFE-indirect
    value (hi byte via reg 0x5d, lo byte via reg 0x5e).

    Computed from the two-point dark bracket: `dark_a` (gain=0,
    offset=0x80) and `dark_b` (gain=0, offset=0xff), both (N, 3)
    uint16.  Per channel, the slope of dark level vs offset code is
    measured, and the final code is 0xff + margin/slope, where the
    margin in dark-level space is the empirically derived
    _OFFSET_LEVEL_MARGIN.

    On the reference unit this reproduces the vendor's codes exactly
    (R=0x010b, G=0x010a, B=0x010b).  Falls back to those hardcoded
    codes if the bracket slope is abnormal (< 1 count per code step).
    """
    arr_a = np.asarray(dark_a, dtype=np.float64)
    arr_b = np.asarray(dark_b, dtype=np.float64)
    if arr_a.ndim != 2 or arr_a.shape[1] != 3:
        raise ValueError(f"dark_a: expected (N, 3) array, got shape {arr_a.shape}")
    if arr_b.ndim != 2 or arr_b.shape[1] != 3:
        raise ValueError(f"dark_b: expected (N, 3) array, got shape {arr_b.shape}")

    codes: list[int] = []
    ch_names = "RGB"
    for ch in range(3):
        mean_a = float(np.mean(arr_a[:, ch]))
        mean_b = float(np.mean(arr_b[:, ch]))
        slope = (mean_b - mean_a) / (_OFFSET_BRACKET_HI - _OFFSET_BRACKET_LO)

        if slope < _OFFSET_MIN_SLOPE:
            log.debug(
                "offset ch %s: bracket slope %.2f < %.1f — "
                "using fallback %#06x",
                ch_names[ch], slope, _OFFSET_MIN_SLOPE, _OFFSET_DEFAULT[ch],
            )
            codes.append(_OFFSET_DEFAULT[ch])
            continue

        margin_codes = round(_OFFSET_LEVEL_MARGIN[ch] / slope)
        code = _OFFSET_BRACKET_HI + margin_codes
        code = max(0, min(code, 0xFFFF))

        log.debug(
            "offset ch %s: slope=%.2f, margin=%d codes, code=%#06x",
            ch_names[ch], slope, margin_codes, code,
        )
        codes.append(code)

    return tuple(codes)  # type: ignore[return-value]


# -------------------------------------------------------------- shading

# Per cal-data/cal-analysis.md "Resolution" section:
#   - 512 B blocks of 128 (offset, gain) u16 LE pairs.
#   - The LAST TWO pairs of every *full* block are zero trailer/filler.
#   - After stripping trailers: width*3 payload pairs, pixel-interleaved
#     RGB in the same order as the image line format (width=3762 for
#     the plain visible-only scan -- 11286 payload pairs).
#   - offset field = per-pixel mean of the measurement.
#   - gain field = 0x4000 (1.0 in Q2.14) for every payload pixel.
#   - Full blocks are 126 payload + 2 trailer pairs, no header, payload
#     contiguous; the final block is a shorter unpadded tail.
#
# 2026-08-30: parameterized by `width` for the IR-enabled scan mode
# (traces/04-singel-3600-IRpa.trace.json.gz), whose shading uploads are
# at the raw sensor width 5184 (not the windowed 3762 the plain visible
# scan uses) -- see driver/gen_tables.py's main_ir() and
# ../cal-data/ir/ir-analysis.md. width=3762 (the default) reproduces
# the exact previous behavior/output (45,856 B); width=5184 produces
# the IR trace's own observed upload length (63,192 B per address --
# verified against traces/04-singel-3600-IRpa.trace.json.gz directly).

_SHADING_GAIN = 0x4000
_PAIRS_PER_BLOCK = 128
_PAYLOAD_PAIRS_PER_FULL_BLOCK = 126
_TRAILER_PAIRS_PER_FULL_BLOCK = 2
SHADING_UPLOAD_LEN = 45856          # width=3762 (plain visible-only scan)
SHADING_UPLOAD_LEN_IR = 63192       # width=5184 (IR-enabled scan), per address


def _shading_upload_len(width: int) -> int:
    """Expected _pack_shading() output length for `width`, computed the
    same way _pack_shading builds it (126 payload + 2 trailer pairs per
    full 512 B block, final block unpadded)."""
    n_pairs = width * 3
    total = 0
    i = 0
    while i < n_pairs:
        remaining = n_pairs - i
        if remaining >= _PAYLOAD_PAIRS_PER_FULL_BLOCK:
            n_payload, n_trailer = _PAYLOAD_PAIRS_PER_FULL_BLOCK, _TRAILER_PAIRS_PER_FULL_BLOCK
        else:
            n_payload, n_trailer = remaining, 0
        total += (n_payload + n_trailer) * 4
        i += n_payload
    return total


def shading_table(measurement: np.ndarray, width: int = 3762) -> bytes:
    """Build the shading-correction upload payload from a multi-line
    measurement.

    `measurement` is (lines, width, 3) uint16, pixel-interleaved RGB
    (the cal_shading_measure/cal_shading_verify bulk-read buffer,
    reshaped). `width` defaults to 3762 (the plain visible-only scan's
    windowed width); pass 5184 for the IR-enabled scan's raw-width
    measurement (see the module note above) -- and, for that mode,
    pass only ONE light source's de-interleaved lines (e.g.
    image.split_ir()'s visible or IR half), not the full alternating
    buffer, or the two passes' very different signal levels would be
    averaged together. Returns _shading_upload_len(width) bytes, ready
    for UsbIo.buf_write(<address>, payload).
    """
    arr = np.asarray(measurement)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected (lines, width, 3) array, got shape {arr.shape}")
    lines, got_width, _ = arr.shape
    if got_width != width:
        raise ValueError(f"expected width {width}, got {got_width}")

    # Per-pixel mean over the measurement lines, rounded to u16,
    # flattened in native pixel-interleaved order (R,G,B,R,G,B,...) --
    # the same order as the scanned image line.
    mean = arr.astype(np.float64).mean(axis=0)          # (width, 3)
    offsets = np.rint(mean).astype(np.uint16).reshape(-1)  # (width*3,)
    gains = np.full_like(offsets, _SHADING_GAIN)
    return _pack_shading(offsets, gains, width=width)


# Per-channel white targets for the second (white-uniformity) upload,
# width=3762 (plain visible-only scan). Reversed from the captured
# upload #2: gain = T_c * 0x4000 / (white - offset) reproduces the
# vendor table with cv 0.0003-0.0004 per channel.
SHADING2_TARGETS = (81752, 83490, 87083)


def shading_table2(
    white_meas: np.ndarray, dark_meas: np.ndarray, width: int = 3762,
    targets: tuple[float, float, float] = SHADING2_TARGETS,
) -> bytes:
    """Build the SECOND shading upload (white uniformity correction).

    The vendor flow uploads shading twice: first a dark map (per-pixel
    offset, gain fixed 1.0 -- shading_table()), then, after a white
    measurement, this table: the SAME offsets plus a varying per-pixel
    gain gain = T_c * 0x4000 / (white_mean - offset), with per-channel
    targets T_c (`targets`, default SHADING2_TARGETS). Getting this
    wrong by uploading white means as offsets subtracts away the whole
    signal (observed 2026-08-30: all-zero images).

    `width` follows shading_table()'s convention (default 3762). This
    is the PLAIN (visible-only) scan's formula; the dual-light modes
    use shading_table2_dual() below.
    """
    w = np.asarray(white_meas).astype(np.float64).mean(axis=0)   # (width, 3)
    f0 = np.rint(np.asarray(dark_meas).astype(np.float64).mean(axis=0))
    denom = np.clip(w - f0, 1.0, None)
    t = np.asarray(targets, dtype=np.float64)
    gains = np.clip(np.rint(t * 0x4000 / denom), 1, 65535)
    return _pack_shading(f0.astype(np.uint16).reshape(-1),
                         gains.astype(np.uint16).reshape(-1), width=width)


# Second-upload (white-uniformity) targets for the DUAL-LIGHT (IR-
# enabled) scan mode, one per shading table. Established 2026-09-02
# against the vendor's own upload #2 payloads in ALL five dual-light
# captures (3600 dpi trace 04 and the 600/1200/2400/7200 dpi strip
# captures, docs/protocol-notes.md pass 18): the table uploaded to
# 0x10014000 (A) is computed from the EVEN (IR-pass) lines of the
# measurements, the one at 0x10034000 (B) from the ODD (visible) lines,
# and the gain is gain = T * 0x4000 / white_mean with NO offset
# subtraction (the verify measurement is taken with upload #1's dark
# offsets already applied by the scanner) and a single target per
# table, the same for all three channels and every dpi:
#   A: 61440 = 0xf000,  B: 90112 = 0x16000
# Within the illuminated window this reproduces the vendor tables with
# cv 0.0003 (vs. 0.034-0.043 for the earlier per-channel, offset-
# subtracting fit, which had paired each table with the WRONG line
# subset). Outside the window (unlit pixels) the vendor's gain
# saturates at 0xffff, as the clip below does.
SHADING2_TARGET_A = 61440.0   # 0x10014000; scanner applies to ODD (visible) lines
SHADING2_TARGET_B = 90112.0   # 0x10034000; scanner applies to EVEN (IR) lines


def shading_table2_dual(
    white_meas: np.ndarray, dark_meas: np.ndarray, width: int, target: float,
) -> bytes:
    """Second (white-uniformity) shading upload for one dual-light table.

    `white_meas` / `dark_meas`: (lines, width, 3) uint16, ONE line
    subset each (odd/visible lines for table A, even/IR for table B)
    of the verify / shading measurement.  Note the cross-connection:
    address A is applied by the scanner to the ODD (visible) scan
    lines, address B to the EVEN (IR) lines.  offsets = per-pixel
    mean of `dark_meas` (same as upload #1), gain = target * 0x4000 /
    mean of `white_meas`, clipped to [1, 0xffff] -- the vendor
    formula, see SHADING2_TARGET_A/_B.
    """
    w = np.asarray(white_meas).astype(np.float64).mean(axis=0)   # (width, 3)
    f0 = np.rint(np.asarray(dark_meas).astype(np.float64).mean(axis=0))
    gains = np.clip(np.rint(float(target) * 0x4000 / np.clip(w, 1.0, None)), 1, 65535)
    return _pack_shading(f0.astype(np.uint16).reshape(-1),
                         gains.astype(np.uint16).reshape(-1), width=width)


# ---- white-measurement dip guard -------------------------------------
# The per-pixel gain is computed from one white measurement per frame.
# A dust speck in the CALIBRATION light path (not the image path) dims a
# few columns of that measurement; the gain then over-corrects them, and
# the frame carries a bright line through its whole height, visible and
# IR alike. First production series on this code (2026-10-20, ten strips
# of one roll): one speck about 15 columns (0.1 mm) wide, 2-4 % deep,
# wandering between strips (columns ~3430, ~3520, ~3635), bright lines
# of +2-4 % in 32 of 40 frames. The sensor has its own dim pixels (up to
# ~3.5 %, one or nine columns wide) which the gain table corrects
# correctly and which must NOT be guarded; they are single-channel (the
# CCD's colour lines are separate), while a shadow is achromatic: in the
# same 40 exports every dust dip showed in all three channels (the
# channels' column positions offset by up to ~40 columns), every sensor
# dip in exactly one. So a dip is guarded only when BOTH other channels
# show a dip of at least `companion_width` columns within
# `companion_distance` of it. The guard replaces a guarded dip by the
# running median of its neighbourhood before the gain is computed;
# everything else is passed through bit for bit. The lateral lit/unlit
# edges of the window are left alone (`edge_margin`), as is anything
# wider than `max_width`.
WHITE_DIP_MAX_WIDTH = 40       # columns; the speck was 16-30 per channel
WHITE_DIP_MIN_DEPTH = 0.015    # relative to the running median
WHITE_DIP_WINDOW = 81          # running-median window (robust to <= 40 outliers)
WHITE_DIP_EDGE_MARGIN = 64     # columns kept clear of the lit/unlit edges
WHITE_DIP_COMPANION_WIDTH = 5  # a companion dip in another channel is this wide
WHITE_DIP_COMPANION_DISTANCE = 50   # ... and this close (columns between the runs)
WHITE_DIP_SHOULDER = 8         # a guarded dip grows by up to this many columns
                               # each side while still below min_depth/3


class WhiteDip(NamedTuple):
    """One guarded dip: `channel` 0-2, columns `start`..`stop` inclusive,
    `depth` = how much darker than the running median (relative, > 0)."""
    channel: int
    start: int
    stop: int
    depth: float

    def as_dict(self) -> dict:
        return {"channel": self.channel, "start": self.start, "stop": self.stop,
                "depth": round(self.depth, 4)}


def _running_median(x: np.ndarray, window: int) -> np.ndarray:
    pad = window // 2
    p = np.pad(x, pad, mode="edge")
    return np.median(sliding_window_view(p, window), axis=-1)


def _runs(idx: np.ndarray) -> list[tuple[int, int]]:
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) != 1)
    starts = np.concatenate([[idx[0]], idx[breaks + 1]])
    stops = np.concatenate([idx[breaks], [idx[-1]]])
    return [(int(s), int(e)) for s, e in zip(starts, stops)]


def guard_white_dips(
    white_meas: np.ndarray, *, max_width: int = WHITE_DIP_MAX_WIDTH,
    min_depth: float = WHITE_DIP_MIN_DEPTH, window: int = WHITE_DIP_WINDOW,
    edge_margin: int = WHITE_DIP_EDGE_MARGIN,
    companion_width: int = WHITE_DIP_COMPANION_WIDTH,
    companion_distance: int = WHITE_DIP_COMPANION_DISTANCE,
    shoulder: int = WHITE_DIP_SHOULDER,
) -> tuple[np.ndarray, list[WhiteDip]]:
    """Mean the white measurement over its lines and guard narrow dips.

    `white_meas` is (lines, width, 3) as shading_table2()/
    shading_table2_dual() take it. Returns ``(guarded, dips)`` where
    `guarded` is (1, width, 3) float64 -- the per-column mean, with each
    guarded dip replaced by the running median -- which those functions
    accept in place of the raw measurement (the mean of one line is that
    line, so an input without dips yields byte-identical tables).

    A dip is a run of columns, no wider than `max_width`, whose mean lies
    more than `min_depth` below the `window`-column running median, at
    least `edge_margin` columns inside the lit window (columns above half
    the central level). It is guarded only when each of the other two
    channels has a dip at least `companion_width` wide within
    `companion_distance` columns of it (an achromatic shadow); a dip in
    one channel alone is the sensor's own and is left to the gain table.
    A guarded dip then grows by up to `shoulder` columns on each side
    while those columns are still more than `min_depth / 3` below the
    median (the shadow's soft edges; first hardware run of the guard
    left +1.5-2 % at such shoulder columns).
    """
    arr = np.asarray(white_meas).astype(np.float64)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected (lines, width, 3) array, got shape {arr.shape}")
    w = arr.mean(axis=0)                     # (width, 3)
    width = w.shape[0]
    out = w.copy()
    pos = np.arange(width)
    lo = np.clip(pos - edge_margin, 0, width)
    hi = np.clip(pos + edge_margin + 1, 0, width)
    # Pass 1: candidate runs per channel.
    candidates: list[list[tuple[int, int, float]]] = [[], [], []]
    medians = []
    rels = []
    for c in range(3):
        col = w[:, c]
        med = _running_median(col, window)
        medians.append(med)
        level = float(np.median(col[width // 4: 3 * width // 4]))
        if level <= 0.0:
            continue
        lit = col > 0.5 * level
        cs = np.concatenate([[0], np.cumsum(lit)])
        valid = (cs[hi] - cs[lo]) == (hi - lo)    # lit throughout the margin
        rel = (col - med) / np.maximum(med, 1.0)
        rels.append(rel)
        flagged = np.flatnonzero(valid & (rel < -min_depth))
        for start, stop in _runs(flagged):
            if stop - start + 1 > max_width:
                continue
            candidates[c].append((start, stop, float(-rel[start:stop + 1].min())))

    def _near(start: int, stop: int, other: int) -> bool:
        for s, e, _ in candidates[other]:
            if e - s + 1 >= companion_width and max(0, s - stop, start - e) <= companion_distance:
                return True
        return False

    # Pass 2: guard the achromatic ones, shoulders included.
    dips: list[WhiteDip] = []
    for c in range(3):
        others = [o for o in range(3) if o != c]
        rel = rels[c] if len(rels) == 3 else None
        for start, stop, depth in candidates[c]:
            if not all(_near(start, stop, o) for o in others):
                continue
            if rel is not None:
                soft = -min_depth / 3.0
                for _ in range(shoulder):
                    if start > 0 and rel[start - 1] < soft:
                        start -= 1
                    if stop < width - 1 and rel[stop + 1] < soft:
                        stop += 1
            out[start:stop + 1, c] = medians[c][start:stop + 1]
            dips.append(WhiteDip(c, start, stop, depth))
    return out[None, :, :], dips


# ---- white-measurement blockage guard -------------------------------
# The white reference is measured in front of aperture 1. A 110 strip can
# reach in there (its front end lies a little into the holder's end), and
# then the film band blocks the white measurement over its columns (Test
# 107, 2026-10-26: 2-3 % of the clean level over ~2240 columns, the 16 mm
# band); the gain table over-corrects those columns by 30-50x and the
# frame's visible R and IR clip inside the photographs. The owner's rule:
# where the strip lies is never the operator's problem -- the calibration
# copes. A blockage is a WIDE region bounded by steep steps: over 389
# stored calibrations the steepest step of a clean measurement (smoothed
# over 31 columns, compared 2 x 48 columns apart) is 0.87 (a dust speck,
# 30 columns, guarded by guard_white_dips), every blocked one 0.03 or less.
# The blocked columns are filled from a clean reference profile of the same
# kind (white_reference), scaled by the measurement's own ratio to it on the
# clear columns either side (interpolated across), or -- without a
# reference -- interpolated linearly between those sides.
WHITE_BLOCK_STEP = 0.70         # a step: the level falls below this fraction of the level 2*half_span earlier
WHITE_BLOCK_HALF_SPAN = 48      # columns either side of a step test
WHITE_BLOCK_SMOOTH = 31         # running-median window before the step test (dust, sensor pixels)
WHITE_BLOCK_MIN_WIDTH = 100     # columns; narrower is a speck (guard_white_dips' business)
WHITE_BLOCK_EDGE_MARGIN = 64    # columns kept clear of the lit window's ends
WHITE_BLOCK_PAD = 24            # columns added on each side of a blocked region (the film edge's blur, ~16)
WHITE_BLOCK_SIDE = 200          # columns either side used to scale the reference
WHITE_BLOCK_CLEAR = 0.97        # a region grows while its edge columns read below this x the clear level
WHITE_BLOCK_CLEAR_FAR = (250, 400)   # ... measured this many columns beyond the step
WHITE_BLOCK_GROW_MAX = 240      # columns a region may grow on each side


class WhiteBlock(NamedTuple):
    """One filled blockage: columns `start`..`stop` inclusive, `depth` =
    the blocked level as a fraction of the clear level beside it (median
    over channels), `source` = "reference" or "interpolation"."""
    start: int
    stop: int
    depth: float
    source: str

    def as_dict(self) -> dict:
        return {"start": self.start, "stop": self.stop,
                "depth": round(self.depth, 4), "source": self.source}


def find_white_blockage(profile: np.ndarray) -> list[tuple[int, int]]:
    """Blocked column ranges (start, stop inclusive) of a white profile
    (width, 3): per channel, a region that opens with a steep fall and
    closes with a steep rise (or runs to the lit window's end), at least
    WHITE_BLOCK_MIN_WIDTH wide, grown over the film edge's partial
    transmission; the channels' regions are merged. A band that reaches the
    lit window's end cannot be told from the unlit end and is not found
    (the 16 mm band lies inside the 36 mm window)."""
    w = np.asarray(profile, dtype=np.float64)
    width = w.shape[0]
    h = WHITE_BLOCK_HALF_SPAN
    marks = np.zeros(width, dtype=bool)
    for c in range(3):
        sm = _running_median(w[:, c], WHITE_BLOCK_SMOOTH)
        top = float(np.max(sm)) if sm.size else 0.0
        if top <= 0.0:
            continue
        lit = np.flatnonzero(sm > 0.2 * top)
        if lit.size == 0:
            continue
        a, b = int(lit[0]) + WHITE_BLOCK_EDGE_MARGIN, int(lit[-1]) - WHITE_BLOCK_EDGE_MARGIN
        if b - a <= 2 * h:
            continue
        cols = np.arange(a + h, b - h)
        before, after = sm[cols - h], sm[cols + h]
        down = cols[after < WHITE_BLOCK_STEP * before]
        up = cols[before < WHITE_BLOCK_STEP * after]
        # A step's test fires over about 2 x half_span columns centred on
        # the edge: the edge is the middle of the run.
        downs = [((s + e) // 2, (s + e) // 2) for s, e in _runs(down)]
        ups = [((s + e) // 2, (s + e) // 2) for s, e in _runs(up)]
        events = sorted([(s, e, "down") for s, e in downs] + [(s, e, "up") for s, e in ups])
        open_at = None
        regions = []
        for s, e, kind in events:
            if kind == "down":
                if open_at is None:
                    open_at = s
            elif open_at is not None:
                regions.append((open_at, e))
                open_at = None
            elif regions:                       # a step split in two: the same rise
                regions[-1] = (regions[-1][0], e)
            else:                               # blocked from the lit window's start
                regions.append((a, e))
        if open_at is not None:
            regions.append((open_at, b))
        for s, e in regions:
            if e - s + 1 < WHITE_BLOCK_MIN_WIDTH:
                continue
            # The film's own edge passes part of the light (IR: ~0.9 over
            # ~150 columns, Test 107): grow the region while the level stays
            # below WHITE_BLOCK_CLEAR of the clear level just beyond.
            far = WHITE_BLOCK_CLEAR_FAR
            if s - a > far[1]:
                clear = float(np.median(sm[s - far[1]:s - far[0]]))
                for _ in range(WHITE_BLOCK_GROW_MAX):
                    if s <= a or sm[s - 1] >= WHITE_BLOCK_CLEAR * clear:
                        break
                    s -= 1
            if b - e > far[1]:
                clear = float(np.median(sm[e + far[0]:e + far[1]]))
                for _ in range(WHITE_BLOCK_GROW_MAX):
                    if e >= b or sm[e + 1] >= WHITE_BLOCK_CLEAR * clear:
                        break
                    e += 1
            marks[max(0, s - WHITE_BLOCK_PAD):min(width, e + WHITE_BLOCK_PAD + 1)] = True
    return _runs(np.flatnonzero(marks))


def guard_white_blockage(white_meas: np.ndarray, reference: "np.ndarray | None" = None
                         ) -> tuple[np.ndarray, list[WhiteBlock]]:
    """Fill the blocked columns of a white measurement (lines, width, 3).
    Returns ``(guarded, blocks)``: `guarded` is the input itself when
    nothing is blocked (bit for bit), else a copy with every line of the
    blocked columns replaced by the filled level. `reference` is a clean
    (width, 3) profile of the same kind, or None."""
    arr = np.asarray(white_meas)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected (lines, width, 3) array, got shape {arr.shape}")
    prof = arr.astype(np.float64).mean(axis=0)
    regions = find_white_blockage(prof)
    if not regions:
        return white_meas, []
    width = prof.shape[0]
    ref = None
    if reference is not None:
        ref = np.asarray(reference, dtype=np.float64)
        if ref.shape != prof.shape or not np.all(np.isfinite(ref)):
            ref = None
    blocked = np.zeros(width, dtype=bool)
    for s, e in regions:
        blocked[s:e + 1] = True
    out = arr.astype(np.float64).copy()
    blocks = []
    for s, e in regions:
        left = np.arange(max(0, s - WHITE_BLOCK_SIDE), s)
        right = np.arange(e + 1, min(width, e + 1 + WHITE_BLOCK_SIDE))
        left, right = left[~blocked[left]], right[~blocked[right]]
        cols = np.arange(s, e + 1)
        depths = []
        source = "reference" if ref is not None else "interpolation"
        for c in range(3):
            x = prof[:, c]
            if ref is not None:
                rr = x / np.maximum(ref[:, c], 1.0)
                kl = float(np.median(rr[left])) if left.size else None
                kr = float(np.median(rr[right])) if right.size else None
                base = ref[cols, c]
            else:
                kl = float(np.median(x[left])) if left.size else None
                kr = float(np.median(x[right])) if right.size else None
                base = np.ones(cols.size)
            if kl is None and kr is None:
                continue
            kl = kr if kl is None else kl
            kr = kl if kr is None else kr
            t = (cols - s + 0.5) / (e - s + 1)
            fill = base * (kl + (kr - kl) * t)
            clear = float(np.median(fill)) if fill.size else 0.0
            if clear > 0:
                depths.append(float(np.median(x[cols])) / clear)
            out[:, cols, c] = fill[None, :]
        blocks.append(WhiteBlock(int(s), int(e), float(np.median(depths)) if depths else 0.0, source))
    if np.issubdtype(arr.dtype, np.integer):
        out = np.clip(np.rint(out), 0, np.iinfo(arr.dtype).max).astype(arr.dtype)
    return out, blocks


def _pack_shading(offsets: np.ndarray, gains: np.ndarray, width: int = 3762) -> bytes:
    """Pack (offset, gain) u16 pairs into the wire block format:
    126 payload pairs + 2 zero trailer pairs per 512-byte block,
    final partial block unpadded."""
    n_pairs = offsets.shape[0]
    expected_pairs = width * 3
    if n_pairs != expected_pairs:
        raise ValueError(f"expected {expected_pairs} pairs for width={width}, got {n_pairs}")
    out = bytearray()
    i = 0
    while i < n_pairs:
        remaining = n_pairs - i
        if remaining >= _PAYLOAD_PAIRS_PER_FULL_BLOCK:
            n_payload = _PAYLOAD_PAIRS_PER_FULL_BLOCK
            n_trailer = _TRAILER_PAIRS_PER_FULL_BLOCK
        else:
            # Final partial block: unpadded, no trailer pairs.
            n_payload = remaining
            n_trailer = 0
        for off, g in zip(offsets[i:i + n_payload], gains[i:i + n_payload]):
            out += struct.pack("<HH", int(off), int(g))
        for _ in range(n_trailer):
            out += struct.pack("<HH", 0, 0)
        i += n_payload

    payload = bytes(out)
    expected_len = _shading_upload_len(width)
    assert len(payload) == expected_len, (
        f"_pack_shading: built {len(payload)} B, expected "
        f"{expected_len} B (n_pairs={n_pairs}, width={width})"
    )
    return payload
