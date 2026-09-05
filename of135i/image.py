"""Raw image assembly and file output for the of135i driver.

The scanner delivers pixel-interleaved RGB, 16-bit little-endian,
straight (no planar split) — see protocol-notes.md pass 5. This module
turns that raw byte stream into a numpy array and writes it out as
16-bit TIFF or PPM (PNM), without any image processing (no inversion,
no orange-mask removal, no gamma) — that is the application's job.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

import numpy as np

log = logging.getLogger("of135i")

_CHANNELS = 3
_BYTES_PER_SAMPLE = 2
_BYTES_PER_PIXEL = _CHANNELS * _BYTES_PER_SAMPLE


def assemble(raw: bytes, width: int) -> np.ndarray:
    """Assemble a raw scan buffer into an (lines, width, 3) uint16 array.

    The input is pixel-interleaved RGB16LE: for each pixel, R, G, B
    samples in that order, each a 16-bit little-endian value; pixels
    run left-to-right, lines top-to-bottom, with no padding between
    lines. A trailing partial line (fewer than `width` full pixels) is
    dropped with a warning rather than raising.
    """
    line_bytes = width * _BYTES_PER_PIXEL
    n_full_lines, remainder = divmod(len(raw), line_bytes)
    if remainder:
        log.warning(
            "assemble: dropping trailing partial line (%d of %d bytes)",
            remainder, line_bytes,
        )
    usable = raw[: n_full_lines * line_bytes]
    arr = np.frombuffer(usable, dtype="<u2")
    arr = arr.reshape(n_full_lines, width, _CHANNELS)
    return arr


# --------------------------------------------------------------- TIFF writer

# Minimal classic (non-BigTIFF) little-endian writer using only stdlib
# struct — no Pillow dependency in the driver package.

_TAG_TYPE_SHORT = 3
_TAG_TYPE_LONG = 4


def _ifd_entry(tag: int, typ: int, count: int, value_bytes: bytes) -> bytes:
    assert len(value_bytes) == 4, "IFD entry value/offset field must be 4 B"
    return struct.pack("<HHI", tag, typ, count) + value_bytes


_TAG_TYPE_UNDEFINED = 7

# A plain sRGB ICC profile (588 B, generated once with Little-CMS via
# Pillow's ImageCms.createProfile("sRGB")), embedded in --positive
# output: the positive rendering is fitted to the vendor app's sRGB
# JPEGs, so that is the space the pixels are in. Raw negatives get no
# profile -- they are linear scanner data, not a display colour space.
SRGB_ICC_PATH = Path(__file__).parent / "data" / "srgb.icc"


def srgb_icc() -> bytes:
    return SRGB_ICC_PATH.read_bytes()


def write_tiff16(arr: np.ndarray, path: str | Path, icc: bytes | None = None) -> None:
    """Write an (lines, width, 3) uint16 array as an uncompressed 16-bit
    RGB TIFF, using only stdlib struct. `icc`, if given, is embedded as
    the ICCProfile tag (34675)."""
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected (lines, width, 3) array, got shape {arr.shape}")
    height, width, _ = arr.shape

    # Pixel data, forced to little-endian, row-major, RGB-interleaved —
    # exactly the raw wire order.
    pixel_data = np.ascontiguousarray(arr, dtype="<u2").tobytes()

    data_offset = 8  # right after the 8-byte header
    extra_offset = data_offset + len(pixel_data)  # always even (u16 * 3)

    # Out-of-line arrays for tags whose value doesn't fit in 4 bytes.
    bits_per_sample = struct.pack("<HHH", 16, 16, 16)
    bits_per_sample_offset = extra_offset
    sample_format = struct.pack("<HHH", 1, 1, 1)
    sample_format_offset = bits_per_sample_offset + len(bits_per_sample)

    icc_data = bytes(icc) if icc else b""
    icc_offset = sample_format_offset + len(sample_format)
    if len(icc_data) % 2:
        icc_data += b"\x00"  # keep the IFD word-aligned

    ifd_offset = icc_offset + len(icc_data)

    def short1(v: int) -> bytes:
        return struct.pack("<HH", v, 0)

    def long1(v: int) -> bytes:
        return struct.pack("<I", v)

    entries = [
        _ifd_entry(256, _TAG_TYPE_LONG, 1, long1(width)),       # ImageWidth
        _ifd_entry(257, _TAG_TYPE_LONG, 1, long1(height)),      # ImageLength
        _ifd_entry(258, _TAG_TYPE_SHORT, 3, long1(bits_per_sample_offset)),  # BitsPerSample
        _ifd_entry(259, _TAG_TYPE_SHORT, 1, short1(1)),         # Compression = none
        _ifd_entry(262, _TAG_TYPE_SHORT, 1, short1(2)),         # PhotometricInterpretation = RGB
        _ifd_entry(273, _TAG_TYPE_LONG, 1, long1(data_offset)),  # StripOffsets
        _ifd_entry(277, _TAG_TYPE_SHORT, 1, short1(3)),         # SamplesPerPixel
        _ifd_entry(278, _TAG_TYPE_LONG, 1, long1(height)),      # RowsPerStrip (single strip)
        _ifd_entry(279, _TAG_TYPE_LONG, 1, long1(len(pixel_data))),  # StripByteCounts
        _ifd_entry(339, _TAG_TYPE_SHORT, 3, long1(sample_format_offset)),  # SampleFormat = uint
    ]
    if icc:
        entries.append(_ifd_entry(34675, _TAG_TYPE_UNDEFINED, len(icc), long1(icc_offset)))  # ICCProfile
    entries.sort(key=lambda e: struct.unpack_from("<H", e)[0])  # tags must ascend

    ifd = struct.pack("<H", len(entries))
    for e in entries:
        ifd += e
    ifd += struct.pack("<I", 0)  # no next IFD

    header = b"II" + struct.pack("<H", 42) + struct.pack("<I", ifd_offset)
    blob = header + pixel_data + bits_per_sample + sample_format + icc_data + ifd

    Path(path).write_bytes(blob)


# ---------------------------------------------------------------- PNM writer


def write_pnm16(arr: np.ndarray, path: str | Path) -> None:
    """Write an (lines, width, 3) uint16 array as a binary PPM (P6),
    maxval 65535. The PPM spec mandates big-endian samples regardless
    of the platform or the source byte order."""
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected (lines, width, 3) array, got shape {arr.shape}")
    height, width, _ = arr.shape

    header = f"P6\n{width} {height}\n65535\n".encode("ascii")
    pixel_data = np.ascontiguousarray(arr, dtype=">u2").tobytes()

    with open(path, "wb") as f:
        f.write(header)
        f.write(pixel_data)


def to_positive(arr, gamma: float = 2.2):
    """Convert a raw negative scan to a display-ready PREVIEW positive.

    Density-domain inversion (the same math a darkroom print performs),
    computed per frame from the frame itself: film base level per
    channel from the 99.8th percentile (the unexposed rebate is the
    brightest thing in a negative), density = log10(base/pixel), then
    per-channel black/white points at the 0.5/99.5 percentiles -- which
    also cancels the orange mask of whatever film this is -- and a
    print gamma. Returns uint16 (lines, width, 3).

    This is deliberately a convenience, not a colour pipeline: the
    driver's product is the raw linear negative (calibrated, aligned,
    unclipped); interpretation -- inversion, mask, white balance, tone
    -- is per image and per photographer and belongs in the
    application (darktable's negadoctor, VueScan, a SANE frontend). A
    per-channel LUT learned against the vendor app's rendering of ONE
    frame (2026-08-30) was shipped until 2026-09-05; it baked in that
    film's mask and exposure and rendered another strip with a strong
    red cast (test log, Test 19). Per-frame inversion adapts instead of
    matching one reference.
    """
    px = np.asarray(arr, dtype=np.float64)
    base = np.array([np.percentile(px[..., c], 99.8) for c in range(3)])
    dens = np.log10(np.clip(base, 1, None) / np.clip(px, 1.0, None))
    out = np.empty_like(dens)
    for c in range(3):
        lo, hi = np.percentile(dens[..., c], [0.5, 99.5])
        out[..., c] = np.clip((dens[..., c] - lo) / max(hi - lo, 1e-9), 0, 1)
    return (out ** (1.0 / gamma) * 65535.0).astype(np.uint16)


# ----------------------------------------------------------------- IR split


def split_ir(raw: bytes, width: int = 5184) -> tuple[np.ndarray, np.ndarray]:
    """De-interleave an IR-enabled scan's raw buffer into (visible, ir).

    The IR-enabled scan mode (--ir; see device.py's Scanner.scan(ir=True)
    and ../cal-data/ir/ir-analysis.md) captures visible and IR light on
    ALTERNATING physical lines at the raw sensor width (5184 px, not the
    3762 the plain visible-only scan windows to): even line index (0, 2,
    4, ...) = IR pass (R, G, B samples near-identical -- the raw pipe
    broadcasts one photodiode reading into all three channel slots),
    odd line index (1, 3, 5, ...) = visible pass (normal RGB negative,
    clear R/G/B separation).

    `raw` is the same pixel-interleaved RGB16LE buffer assemble() takes
    (this calls assemble() itself). Returns (visible, ir):
      visible: (lines // 2, width, 3) uint16 -- the odd lines, unchanged.
      ir: (lines // 2, width) uint16 -- the even lines, reduced to a
        single channel (round of the per-pixel R/G/B mean; the three
        channels already carry the same broadcast value up to noise).
    """
    arr = assemble(raw, width)
    n_lines = arr.shape[0]
    if n_lines % 2:
        log.warning("split_ir: dropping odd trailing line (%d total lines)", n_lines)
        arr = arr[: n_lines - 1]
    ir_rgb = arr[0::2]        # even lines: IR pass
    visible = arr[1::2]       # odd lines: visible pass
    ir = np.rint(ir_rgb.astype(np.float64).mean(axis=2)).astype(np.uint16)
    return visible, ir


def _box_blur(a: np.ndarray, box: int) -> np.ndarray:
    """Box blur of a 2D array via a summed-area table (cumsum twice),
    numpy-only (no scipy). Edge-padded so the output covers the full
    input shape. The cumsum accumulator is float64 (precision matters:
    box sums over ~4000 terms lose too much accuracy in float32 once
    two nearby large partial sums are subtracted) but is a single
    transient array, freed before returning a float32 result -- kept
    off the memory budget for the caller's own full-frame arrays.
    """
    h, w = a.shape
    pad_lo = box // 2
    pad_hi = box - pad_lo
    ap = np.pad(a, ((pad_lo, pad_hi), (pad_lo, pad_hi)), mode="edge").astype(np.float64)
    np.cumsum(ap, axis=0, out=ap)
    np.cumsum(ap, axis=1, out=ap)
    csum = np.pad(ap, ((1, 0), (1, 0)))
    del ap
    y0 = np.arange(h)
    x0 = np.arange(w)
    y1 = y0 + box
    x1 = x0 + box
    total = csum[y1][:, x1] - csum[y0][:, x1] - csum[y1][:, x0] + csum[y0][:, x0]
    del csum
    return (total / (box * box)).astype(np.float32)


def _dilate_bool(mask: np.ndarray, iterations: int = 2) -> np.ndarray:
    """Binary dilation (4-neighborhood) via np.roll ORs, `iterations`
    times -- each pass grows the mask by one pixel in each direction."""
    out = mask
    for _ in range(iterations):
        out = (
            out
            | np.roll(out, 1, axis=0)
            | np.roll(out, -1, axis=0)
            | np.roll(out, 1, axis=1)
            | np.roll(out, -1, axis=1)
        )
    return out


# Calibrated on the real IR test pair (frame4-ir-test3*.tiff): at
# sensitivity=1.0 this covers ~0.1% of pixels with dust/scratch specks
# (see ../cal-data/ir/verify_dust.py), comfortably inside the
# 0.05-0.5% target band.
_DUST_BG_BOX = 64
_DUST_T0 = 0.12
_DUST_BORDER_FRAC = 0.3  # local bg below this fraction of the image's
                          # typical (median) bg is film-holder border,
                          # not scannable frame -- never masked as dust.


def dust_mask(ir: np.ndarray, sensitivity: float = 1.0) -> np.ndarray:
    """Detect dust/scratch specks in an IR channel image.

    `ir` is a (lines, width) array (any integer/float dtype) from the
    IR pass of an --ir scan: a near-uniform bright field where dust and
    scratches on the film or platen show up as small, sharply darker
    specks (IR light passes through the film's dye layers but is
    blocked by opaque debris). Returns a boolean mask, True where a
    pixel is judged part of a speck.

    Method: estimate the local IR background with a coarse (~64 px) box
    blur, then flag pixels significantly darker than that local
    background (`ir < background * (1 - t)`, t scaled inversely by
    `sensitivity` -- higher sensitivity masks more). The dark
    film-holder borders (present in full-sensor-width IR-mode scans;
    see ir-analysis.md) have a near-black local background themselves
    and are excluded outright, including a halo around the border/frame
    edge as wide as the blur box -- inside that halo the box blur mixes
    border and frame content, which would otherwise register as bogus
    "specks" right along the border. The mask is then dilated 2-3 px
    since a speck's faint penumbra usually extends past where the
    hard threshold first trips.
    """
    ir_f = np.asarray(ir, dtype=np.float32)
    bg = _box_blur(ir_f, _DUST_BG_BOX)

    border = bg < (_DUST_BORDER_FRAC * float(np.median(bg)))
    # Dilate the border exclusion by the same box radius: _box_blur's
    # boxcar average blends border and frame content within half the
    # box width of the true edge, so bg is depressed there too, wide
    # enough to otherwise register as a "speck" band along the border.
    border = _box_blur(border.astype(np.float32), _DUST_BG_BOX + 1) > 0

    t = min(_DUST_T0 / max(sensitivity, 1e-6), 0.9)
    mask = (ir_f < bg * (1.0 - t)) & ~border
    del ir_f, bg, border

    # 6 px: wide enough that the inpaint rim sits in clean film beyond
    # the stagger-colored halo around a speck (rainbow fill observed
    # with 2 px, 2026-08-30).
    return _dilate_bool(mask, iterations=6)


_INPAINT_KERNELS = (5, 17, 49)  # increasing box sizes, numpy-only via
                                 # the existing cumsum box filter
_INPAINT_MIN_WEIGHT = 4.0        # a scale's fill only counts once its box
                                  # holds at least this many valid pixels


def _inpaint_channel(chan: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Fill `mask`-True pixels of a single 2D channel by multi-scale
    normalized convolution: for a box of size k,

        fill = boxblur(image * weight, k) / boxblur(weight, k)

    with weight = 1 on unmasked pixels and 0 on masked ones -- a
    weighted local average that only ever draws on real (unmasked)
    values, however few or many fall in the box (the `boxblur(image *
    weight)` and `boxblur(weight)` calls both divide by k*k internally;
    that factor cancels in the ratio, leaving exactly the weighted
    mean of the valid pixels in the box).

    A single fixed kernel size forces a choice between over-blurring
    flat areas (a box big enough to always find valid pixels even deep
    inside a large speck) and leaving a speck's center under-supported
    (a box small enough to stay local everywhere else). Instead each
    masked pixel is filled from the SMALLEST of `_INPAINT_KERNELS`
    whose box already contains at least `_INPAINT_MIN_WEIGHT` valid
    pixels -- tight, local averaging near a speck's edge, widening only
    as far as actually needed toward its center. This also removes the
    prior flood-fill approach's directional bias (a pixel's fill value
    depended on which edge of the speck a fill wave reached it from
    first, leaving flat plateaus and color-banded smudges in larger
    specks); normalized convolution instead averages every valid pixel
    the box reaches, from every direction, in one step per scale.

    Unmasked pixels are never read into a fill value and never
    written -- only `filled[good]` assignments touch the output, always
    restricted to (still-unresolved) masked positions.
    """
    img = chan.astype(np.float32, copy=True)
    weight = (~mask).astype(np.float32)
    masked_vals = img * weight  # zero at masked positions regardless of
                                 # the speck's own (dust-darkened) value

    filled = img.copy()
    unresolved = mask.copy()

    for k in _INPAINT_KERNELS:
        if not unresolved.any():
            break
        num = _box_blur(masked_vals, k)
        den = _box_blur(weight, k)
        good = unresolved & (den * (k * k) > _INPAINT_MIN_WEIGHT)
        if good.any():
            with np.errstate(divide="ignore", invalid="ignore"):
                filled[good] = num[good] / den[good]
            unresolved &= ~good
        del num, den, good

    if unresolved.any():
        # Only reachable for a masked region wider than the largest
        # kernel with no valid pixel anywhere inside it -- well past the
        # ~20 px specks this is calibrated for. Falls back to the
        # frame's overall valid-pixel mean rather than leaving the raw
        # dust-darkened value in place.
        fallback = float(img[~mask].mean()) if (~mask).any() else 0.0
        filled[unresolved] = fallback

    return filled


def remove_dust(visible: np.ndarray, ir: np.ndarray, sensitivity: float = 1.0) -> np.ndarray:
    """Remove dust/scratches from `visible` using the IR channel's dust
    map (see `dust_mask`). `visible` is (lines, width, 3), `ir` is the
    same-shape-minus-channel (lines, width) IR image, pixel-aligned
    (the alternating-line pair from image.split_ir; see that function
    and cli.py's --ir path for how alignment is kept in sync through
    orientation transforms). Returns a same-shape, same-dtype array
    with masked pixels replaced per channel from unmasked neighbors
    (`_inpaint_channel`); everything else is untouched.

    Processes one channel at a time (rather than all three at once) to
    keep peak memory to a few hundred MB instead of ~1 GB for a full
    5184x5272x3 frame.
    """
    if visible.shape[:2] != ir.shape:
        raise ValueError(
            f"visible/ir line-grid mismatch: {visible.shape[:2]} vs {ir.shape}"
        )
    mask = dust_mask(ir, sensitivity=sensitivity)
    if not mask.any():
        return visible.copy()

    dtype = visible.dtype
    info = np.iinfo(dtype) if np.issubdtype(dtype, np.integer) else None
    out = np.empty_like(visible)
    for c in range(visible.shape[2]):
        filled = _inpaint_channel(visible[..., c], mask)
        if info is not None:
            filled = np.clip(np.rint(filled), info.min, info.max)
        out[..., c] = filled.astype(dtype)
        del filled
    return out


def align_channels(arr, dpi: int = 3600):
    """Correct the staggered color-line offset of the sensor.

    The CCD reads R, G and B on physically separate lines (vendor ini
    LineSpace=-24 at 7200 dpi base): in raw scan data R lags G by
    24*dpi/7200 lines and B leads by the same amount (measured -12/+12
    at 3600 dpi, residual 0 after correction). Without this, edges show
    strong RGB fringing. The shifted-in edge lines (wrap artifacts) are
    cropped away.
    """
    shift = round(24 * dpi / 7200)
    if shift == 0:
        return arr
    out = np.ascontiguousarray(arr)
    out = out.copy()
    out[..., 0] = np.roll(out[..., 0], -shift, axis=0)
    out[..., 2] = np.roll(out[..., 2], +shift, axis=0)
    return out[shift:-shift]
