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


def write_tiff16(arr: np.ndarray, path: str | Path) -> None:
    """Write an (lines, width, 3) uint16 array as an uncompressed 16-bit
    RGB TIFF, using only stdlib struct."""
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

    ifd_offset = sample_format_offset + len(sample_format)

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
    entries.sort(key=lambda e: struct.unpack_from("<H", e)[0])  # tags must ascend

    ifd = struct.pack("<H", len(entries))
    for e in entries:
        ifd += e
    ifd += struct.pack("<I", 0)  # no next IFD

    header = b"II" + struct.pack("<H", 42) + struct.pack("<I", ifd_offset)
    blob = header + pixel_data + bits_per_sample + sample_format + ifd

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


_LUT_PATH = __import__("pathlib").Path(__file__).parent / "data" / "negative-color-lut.npy"


def to_positive(arr, gamma: float = 1.8):
    """Convert a raw negative scan to a display-ready positive.

    Density-domain inversion (the same math a darkroom print performs):
    film base level per channel from the 99.8th percentile (the
    unexposed rebate is the brightest thing in a negative), density =
    log10(base/pixel), then per-channel black/white points at the
    0.5/99.5 percentiles -- which also cancels the orange mask -- and a
    print gamma. Returns uint16 (lines, width, 3).

    This is a convenience for everyday use; serious color work should
    start from the raw negative in a dedicated tool (e.g. darktable's
    negadoctor).
    """
    # Preferred path: a learned per-channel LUT (raw u16 -> display u8),
    # fitted once against a reference rendering of the same frame
    # (vendor-app output). Captures inversion, orange mask and tone
    # curve in one mapping. Falls back to the generic density inversion
    # below when no LUT file is shipped.
    if _LUT_PATH.exists():
        luts = np.load(_LUT_PATH)
        a = np.asarray(arr)
        out8 = np.stack([luts[c][a[..., c]] for c in range(3)], axis=-1)
        return (np.clip(out8, 0, 255) * 257.0).astype(np.uint16)

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
