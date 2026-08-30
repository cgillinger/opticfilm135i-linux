"""Calibration formulas for the of135i driver.

Implements the three calibration computations documented in
cal-data/cal-analysis.md (read that file's "Resolution" section first
-- it is the final, solved word on the shading-table format):

  - gain_codes():   AFE gain (regs 2/3/4) from a white-line measurement.
  - offset_codes(): AFE offset (regs 5/6/7) -- currently a documented
                     constant; the bracket-measurement-based formula
                     did not resolve (see cal-analysis.md section 3).
  - shading_table(): the 45,856 B per-pixel shading-correction upload
                     from a 128-line dark-current-free measurement.

All three operate on plain numpy arrays; no device I/O here.
"""

from __future__ import annotations

import struct

import numpy as np

# ----------------------------------------------------------------- gain

# Best-fit target level for AFE gain, from cal-analysis.md section 2:
# actual_gain = code / 32, target = white-line peak (99.9th percentile
# per channel) ~= 31673 counts. Confidence: moderate (2-4% residual,
# best of all tested forms).
_GAIN_TARGET = 31673
_GAIN_DIVISOR = 32
_GAIN_MAX_CODE = 63


def gain_codes(white_line: np.ndarray) -> tuple[int, int, int]:
    """Compute AFE gain codes (regs 2/3/4, R/G/B) from a white-line scan.

    `white_line` is (N, 3) uint16, pixel-interleaved RGB (one scanned
    line, gain=0, final applied offset -- see cal_white in tables.py).
    Per channel: peak = 99.9th percentile (robust against single hot
    pixels/outliers vs. a bare max), code = round(32 * target / peak),
    clamped to the AFE gain register's 6-bit range [0, 63].

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
            raise ValueError(f"channel {ch}: non-positive peak level ({peak})")
        code = round(_GAIN_DIVISOR * _GAIN_TARGET / peak)
        codes.append(int(min(max(code, 0), _GAIN_MAX_CODE)))
    return tuple(codes)  # type: ignore[return-value]


# --------------------------------------------------------------- offset

# cal-analysis.md section 3: no clean small-target linear model fits
# the two-point [0x80, 0xff] bracket -- both directions extrapolate
# the wrong way. What *is* solid is the empirically observed final
# applied codes from the capture (trace op 351/353/355 in
# tables.CAL_SHADING_MEASURE, right before the shading measurement
# run): R=0x010b, G=0x010a, B=0x010b.
#
# TODO(offset mechanism): the dark_a/dark_b measurement pairs are
# accepted as arguments for future diagnostic/derivation use (e.g. a
# 3rd bracket point, or a different fit once one is captured), but are
# NOT currently used to compute the return value -- confidence in any
# formula derived from just two bracket points was assessed "low /
# inconclusive" in cal-analysis.md. Ship the known-good constant.
_OFFSET_DEFAULT = (0x010B, 0x010A, 0x010B)


def offset_codes(
    dark_a: np.ndarray, dark_b: np.ndarray
) -> tuple[int, int, int]:
    """AFE offset codes (regs 5/6/7, R/G/B), each a 16-bit AFE-indirect
    value (hi byte via reg 0x5d, lo byte via reg 0x5e).

    `dark_a` (gain=0, offset=0x80) and `dark_b` (gain=0, offset=0xff)
    are (N, 3) uint16 dark-current measurements, accepted for future
    use/diagnostics -- see the module TODO above. The current
    implementation returns the empirically observed final codes
    unconditionally.
    """
    for name, arr in (("dark_a", dark_a), ("dark_b", dark_b)):
        arr = np.asarray(arr)
        if arr.ndim != 2 or arr.shape[1] != 3:
            raise ValueError(f"{name}: expected (N, 3) array, got shape {arr.shape}")
    return _OFFSET_DEFAULT


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

    `width` follows shading_table()'s convention (default 3762; pass
    5184 + one light source's de-interleaved lines for IR mode). No
    IR-specific targets are known yet (v1: same SHADING2_TARGETS
    formula/values applied to whichever channel's measurement is
    passed in -- see calibrate.SHADING2_TARGETS_IR for the current
    placeholder and device.py for how it's used).
    """
    w = np.asarray(white_meas).astype(np.float64).mean(axis=0)   # (width, 3)
    f0 = np.rint(np.asarray(dark_meas).astype(np.float64).mean(axis=0))
    denom = np.clip(w - f0, 1.0, None)
    t = np.asarray(targets, dtype=np.float64)
    gains = np.clip(np.rint(t * 0x4000 / denom), 1, 65535)
    return _pack_shading(f0.astype(np.uint16).reshape(-1),
                         gains.astype(np.uint16).reshape(-1), width=width)


# Second-upload (white-uniformity) targets for IR MODE, per pass.
# Derived 2026-08-30 from the vendor's own upload #2 payloads in trace
# 04 vs the extracted 256-line dark/white shading measurements:
# T_c = gain * (white - offset) / 0x4000, cv 0.034-0.043 per channel.
# The IR pass gets ~1.8x gain (its target is far above the visible
# pass's) -- without this the IR channel comes out ~7x too dark.
SHADING2_TARGETS_IRMODE_VISIBLE = (53928.0, 74096.0, 61728.0)
SHADING2_TARGETS_IRMODE_IR = (100287.0, 72574.0, 87480.0)


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
