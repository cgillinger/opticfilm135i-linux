#!/usr/bin/env python3
"""What a saved frame actually contains: dimensions, channels, bits per
channel -- and whether the low 8 bits carry data.

Why this exists: Pillow, which the rest of our host-side tooling uses,
hands back 8-bit RGB for a 16-bit RGB PNG or TIFF. That is fine for the
coverage verdict and for preview positives, but it cannot answer the WP-2
question "did digiKam save all 16 bits?". Neither can the file header
alone: an 8-bit image scaled up to 16 bits has a perfectly correct
`bit_depth 16` header and all-zero low bytes.

So this reads the pixels with readers that preserve 16-bit RGB:
  * PNG  -- parsed here (zlib + the five PNG filters), no dependency;
  * TIFF -- via tifffile, which keeps 16-bit RGB intact.

    tools/image_probe.py FILE
    tools/image_probe.py FILE --expect 3762x5335 --expect-bits 16 --expect-channels 3
    tools/image_probe.py FILE --no-pixels          # header only, instant
    tools/image_probe.py FILE --max-lines 0        # read every line (slow for PNG)

Exit 0 if the file parses and every --expect matches, 1 otherwise, 2 on a
read error. Read-only; never writes.

`low_byte_nonzero` is the fraction of sampled 16-bit samples whose low byte
is not zero. A real 16-bit scan sits near 1.0 (sensor noise alone fills the
low byte); an 8-bit image widened to 16 bits gives exactly 0.0, and so does
`value << 8` scaling. It is evidence, not proof of provenance: a heavily
quantised or synthetic image could also be low. Judge it together with the
header.
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# PNG colour type -> channel count
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class ProbeError(Exception):
    pass


# ------------------------------------------------------------------- PNG
def _png_chunks(data: bytes):
    pos = len(PNG_MAGIC)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        yield ctype, body
        pos += 12 + length                      # length + type + data + crc


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter_row(ftype: int, line: np.ndarray, prev: np.ndarray, bpp: int) -> np.ndarray:
    """One PNG scanline. None/Up are vectorised; the three filters with a
    left-neighbour dependency run byte by byte, which is why --max-lines
    exists."""
    if ftype == 0:
        return line
    if ftype == 2:
        return (line + prev).astype(np.uint8)
    cur = line.copy()
    n = cur.size
    if ftype == 1:                                          # Sub
        for i in range(bpp, n):
            cur[i] = (int(cur[i]) + int(cur[i - bpp])) & 0xFF
    elif ftype == 3:                                        # Average
        for i in range(n):
            left = int(cur[i - bpp]) if i >= bpp else 0
            cur[i] = (int(cur[i]) + ((left + int(prev[i])) >> 1)) & 0xFF
    elif ftype == 4:                                        # Paeth
        for i in range(n):
            left = int(cur[i - bpp]) if i >= bpp else 0
            upleft = int(prev[i - bpp]) if i >= bpp else 0
            cur[i] = (int(cur[i]) + _paeth(left, int(prev[i]), upleft)) & 0xFF
    else:
        raise ProbeError(f"unknown PNG filter type {ftype}")
    return cur


def probe_png(path: Path, read_pixels: bool, max_lines: int) -> dict:
    data = path.read_bytes()
    if data[:8] != PNG_MAGIC:
        raise ProbeError("not a PNG")
    ihdr = None
    idat = bytearray()
    for ctype, body in _png_chunks(data):
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body[:13])
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    if ihdr is None:
        raise ProbeError("PNG has no IHDR")
    width, height, depth, colour, comp, filt, interlace = ihdr
    channels = PNG_CHANNELS.get(colour)
    if channels is None:
        raise ProbeError(f"unknown PNG colour type {colour}")
    info = {"format": "PNG", "width": width, "height": height,
            "channels": channels, "bits_per_channel": depth,
            "interlaced": bool(interlace), "lines_read": 0,
            "low_byte_nonzero": None}
    if not read_pixels:
        return info
    if interlace:
        raise ProbeError("interlaced (Adam7) PNG: pixel probing not supported")
    if colour == 3:
        raise ProbeError("palette PNG: pixel probing not supported")
    if depth not in (8, 16):
        raise ProbeError(f"PNG bit depth {depth}: pixel probing not supported")

    bpp = channels * depth // 8                             # bytes per pixel
    stride = width * bpp
    raw = zlib.decompress(bytes(idat))
    want = height if max_lines in (0, None) else min(height, max_lines)
    prev = np.zeros(stride, dtype=np.uint8)
    rows = []
    pos = 0
    for _ in range(want):
        if pos + 1 + stride > len(raw):
            break
        ftype = raw[pos]
        line = np.frombuffer(raw, dtype=np.uint8, count=stride, offset=pos + 1).copy()
        pos += 1 + stride
        cur = _unfilter_row(ftype, line, prev, bpp)
        rows.append(cur)
        prev = cur
    if not rows:
        raise ProbeError("no scanlines could be read")
    block = np.vstack(rows)
    info["lines_read"] = len(rows)
    if depth == 16:
        samples = block.reshape(len(rows), -1, 2)           # big-endian pairs
        info["low_byte_nonzero"] = float((samples[:, :, 1] != 0).mean())
        info["pixels"] = (samples[:, :, 0].astype(np.uint16) << 8 |
                          samples[:, :, 1]).reshape(len(rows), width, channels)
    else:
        info["pixels"] = block.reshape(len(rows), width, channels)
    return info


# ------------------------------------------------------------------ TIFF
def probe_tiff(path: Path, read_pixels: bool, max_lines: int) -> dict:
    try:
        import tifffile
    except ImportError as e:
        raise ProbeError(f"TIFF probing needs tifffile ({e})")
    with tifffile.TiffFile(str(path)) as tf:
        page = tf.pages[0]
        bits = page.bitspersample
        if isinstance(bits, (tuple, list)):
            if len(set(bits)) != 1:
                raise ProbeError(f"mixed bits per channel: {bits}")
            bits = bits[0]
        info = {"format": "TIFF", "width": int(page.imagewidth),
                "height": int(page.imagelength),
                "channels": int(page.samplesperpixel),
                "bits_per_channel": int(bits),
                "compression": str(page.compression),
                "lines_read": 0, "low_byte_nonzero": None}
        if not read_pixels:
            return info
        arr = page.asarray()
    if arr.ndim == 2:
        arr = arr[:, :, None]
    want = arr.shape[0] if max_lines in (0, None) else min(arr.shape[0], max_lines)
    block = arr[:want]
    info["lines_read"] = int(want)
    if block.dtype == np.uint16:
        info["low_byte_nonzero"] = float(((block & 0xFF) != 0).mean())
    info["pixels"] = block
    return info


def probe(path, read_pixels: bool = True, max_lines: int = 256) -> dict:
    path = Path(path)
    head = path.read_bytes()[:8] if path.stat().st_size >= 8 else b""
    if head[:8] == PNG_MAGIC:
        return probe_png(path, read_pixels, max_lines)
    if head[:2] in (b"II", b"MM"):
        return probe_tiff(path, read_pixels, max_lines)
    raise ProbeError(f"{path}: not a PNG or TIFF (magic {head[:4]!r})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("--expect", help="WIDTHxHEIGHT, e.g. 3762x5335")
    ap.add_argument("--expect-bits", type=int, help="bits per channel")
    ap.add_argument("--expect-channels", type=int)
    ap.add_argument("--min-low-byte-nonzero", type=float, default=None,
                    help="fail if fewer than this fraction of samples have a "
                         "non-zero low byte (16-bit files only)")
    ap.add_argument("--no-pixels", action="store_true", help="header only")
    ap.add_argument("--max-lines", type=int, default=256,
                    help="lines to read for the pixel checks (0 = all)")
    args = ap.parse_args(argv)

    try:
        info = probe(args.file, read_pixels=not args.no_pixels,
                     max_lines=args.max_lines)
    except (OSError, ProbeError, zlib.error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"file={args.file}")
    print(f"format={info['format']} {info['width']}x{info['height']} "
          f"channels={info['channels']} bits_per_channel={info['bits_per_channel']}"
          + (f" compression={info['compression']}" if 'compression' in info else ""))
    if info["lines_read"]:
        lbn = info["low_byte_nonzero"]
        print(f"pixels read: {info['lines_read']} lines; low_byte_nonzero="
              + ("n/a (8-bit)" if lbn is None else f"{lbn:.4f}"))

    ok = True
    if args.expect:
        w, _, h = args.expect.partition("x")
        if (info["width"], info["height"]) != (int(w), int(h)):
            print(f"FAIL: expected {args.expect}, got "
                  f"{info['width']}x{info['height']}", file=sys.stderr)
            ok = False
    if args.expect_bits is not None and info["bits_per_channel"] != args.expect_bits:
        print(f"FAIL: expected {args.expect_bits} bits per channel, got "
              f"{info['bits_per_channel']}", file=sys.stderr)
        ok = False
    if args.expect_channels is not None and info["channels"] != args.expect_channels:
        print(f"FAIL: expected {args.expect_channels} channels, got "
              f"{info['channels']}", file=sys.stderr)
        ok = False
    if args.min_low_byte_nonzero is not None:
        lbn = info["low_byte_nonzero"]
        if lbn is None:
            print("FAIL: no 16-bit samples to check the low byte on", file=sys.stderr)
            ok = False
        elif lbn < args.min_low_byte_nonzero:
            print(f"FAIL: low_byte_nonzero {lbn:.4f} < {args.min_low_byte_nonzero} "
                  "-- the low 8 bits look empty (an 8-bit image widened to 16?)",
                  file=sys.stderr)
            ok = False
    print("OK" if ok else "NOT OK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
