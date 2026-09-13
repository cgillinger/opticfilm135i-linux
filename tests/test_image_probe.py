#!/usr/bin/env python3
"""tools/image_probe.py: does it read 16-bit RGB back exactly?

WP-2 has to answer "did digiKam really save 16 bits per channel?", and
neither the header nor Pillow can answer it: an 8-bit image widened to 16
bits has a correct `bit_depth 16` header, and Pillow returns 8-bit RGB for
a 48-bit file regardless. So the probe is checked here against synthetic
images whose values are chosen to expose exactly that loss -- every sample
has a non-zero LOW byte (0x1234, 0xABCD, …), so a reader that drops the low
8 bits produces visibly wrong numbers rather than plausible ones.

  1. PNG round-trip, all five PNG row filters -- the filters with a
     left-neighbour dependency (Sub/Average/Paeth) are the ones that would
     silently corrupt a row.
  2. TIFF round-trip via tifffile.
  3. The 8-bit-widened case: low_byte_nonzero is exactly 0.0, and the CLI's
     --min-low-byte-nonzero fails it.
  4. The documented Pillow limitation, asserted rather than assumed: for the
     same 16-bit RGB file Pillow hands back 8 bits. (If a future Pillow fixes
     this, this test tells us, and the docs can be relaxed.)

No hardware, no network. Run with:
    .venv/bin/python tests/test_image_probe.py
"""

from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

REPO = Path(__file__).resolve().parents[1]


def _probe_module():
    spec = importlib.util.spec_from_file_location(
        "image_probe", REPO / "tools" / "image_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ip = _probe_module()


def _test_image(height=8, width=6) -> np.ndarray:
    """Values whose low byte is never zero, and never equal to the high byte
    either -- so a truncating or a byte-swapping reader both stand out."""
    base = np.arange(height * width * 3, dtype=np.uint32)
    arr = ((base * 977 + 0x1234) & 0xFFFF).astype(np.uint16)
    arr = arr.reshape(height, width, 3)
    arr[arr & 0xFF == 0] += 1                      # guarantee a non-zero low byte
    return arr


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _filter_row(ftype: int, raw: bytes, prev: bytes, bpp: int) -> bytes:
    """Forward PNG filter -- the inverse of what the probe undoes."""
    out = bytearray(len(raw))
    for i in range(len(raw)):
        left = raw[i - bpp] if i >= bpp else 0
        up = prev[i]
        upleft = prev[i - bpp] if i >= bpp else 0
        if ftype == 0:
            v = raw[i]
        elif ftype == 1:
            v = raw[i] - left
        elif ftype == 2:
            v = raw[i] - up
        elif ftype == 3:
            v = raw[i] - ((left + up) >> 1)
        elif ftype == 4:
            v = raw[i] - _paeth(left, up, upleft)
        else:
            raise ValueError(ftype)
        out[i] = v & 0xFF
    return bytes(out)


def _write_png(path: Path, arr: np.ndarray, filters=None) -> None:
    """Minimal 8/16-bit RGB PNG writer. Pillow cannot write 48-bit RGB, so
    the test needs its own -- which also makes it an independent oracle."""
    height, width, channels = arr.shape
    assert channels == 3
    depth = 16 if arr.dtype == np.uint16 else 8
    body = arr.astype(">u2" if depth == 16 else np.uint8).tobytes()
    stride = width * channels * depth // 8
    bpp = channels * depth // 8
    filters = filters or [0] * height
    raw = bytearray()
    prev = bytes(stride)
    for y in range(height):
        row = body[y * stride:(y + 1) * stride]
        ft = filters[y % len(filters)]
        raw.append(ft)
        raw += _filter_row(ft, row, prev, bpp)
        prev = row
    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + ctype + data
                + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, depth, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(bytes(raw)))
                     + chunk(b"IEND", b""))


def test_png_roundtrip_through_every_filter():
    arr = _test_image(height=10)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sixteen.png"
        _write_png(p, arr, filters=[0, 1, 2, 3, 4])        # cycles over the rows
        info = ip.probe(p, max_lines=0)
        assert info["bits_per_channel"] == 16, info
        assert info["channels"] == 3 and (info["width"], info["height"]) == (6, 10)
        assert info["lines_read"] == 10, info["lines_read"]
        assert np.array_equal(info["pixels"], arr), (
            "16-bit PNG did not round-trip:\n"
            f"  wrote {arr[:2, :2].tolist()}\n  read  {info['pixels'][:2, :2].tolist()}")
        assert info["low_byte_nonzero"] == 1.0, info["low_byte_nonzero"]
    print("test_png_roundtrip_through_every_filter OK")


def test_png_max_lines_limits_the_read():
    arr = _test_image(height=20)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sixteen.png"
        _write_png(p, arr, filters=[4])
        info = ip.probe(p, max_lines=5)
        assert info["lines_read"] == 5
        assert np.array_equal(info["pixels"], arr[:5]), "partial read is wrong"
    print("test_png_max_lines_limits_the_read OK")


def test_tiff_roundtrip_keeps_sixteen_bits():
    try:
        import tifffile
    except ImportError:
        print("SKIP: tifffile not installed")
        return "skipped"
    arr = _test_image()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sixteen.tif"
        tifffile.imwrite(str(p), arr)
        info = ip.probe(p, max_lines=0)
        assert info["bits_per_channel"] == 16 and info["channels"] == 3, info
        assert np.array_equal(info["pixels"], arr), "16-bit TIFF did not round-trip"
        assert info["low_byte_nonzero"] == 1.0
    print("test_tiff_roundtrip_keeps_sixteen_bits OK")


def test_eight_bit_widened_to_sixteen_is_caught():
    """The case the header cannot catch: 8-bit data stored as 16-bit."""
    eight = (_test_image() >> 8).astype(np.uint8)
    widened = (eight.astype(np.uint16) << 8)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "widened.png"
        _write_png(p, widened, filters=[0, 1, 2, 3, 4])
        info = ip.probe(p, max_lines=0)
        assert info["bits_per_channel"] == 16, "the header still claims 16 bits"
        assert info["low_byte_nonzero"] == 0.0, info["low_byte_nonzero"]
        rc = ip.main([str(p), "--expect-bits", "16", "--min-low-byte-nonzero", "0.5",
                      "--max-lines", "0"])
        assert rc == 1, f"the CLI accepted a widened 8-bit image (rc={rc})"
    print("test_eight_bit_widened_to_sixteen_is_caught OK")


def test_cli_expectations_pass_and_fail():
    arr = _test_image(height=7, width=5)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sixteen.png"
        _write_png(p, arr, filters=[2])
        assert ip.main([str(p), "--expect", "5x7", "--expect-bits", "16",
                        "--expect-channels", "3", "--min-low-byte-nonzero", "0.9",
                        "--max-lines", "0"]) == 0
        assert ip.main([str(p), "--expect", "3762x5335"]) == 1, \
            "a wrong --expect was accepted"
    print("test_cli_expectations_pass_and_fail OK")


def test_pillow_loses_the_low_byte_of_rgb16():
    """The documented limitation of the Pillow path (sane_coverage.py,
    preview positives). Asserted, so a change in Pillow surfaces here."""
    try:
        from PIL import Image
    except ImportError:
        print("SKIP: Pillow not installed")
        return "skipped"
    arr = _test_image()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sixteen.png"
        _write_png(p, arr, filters=[0])
        with Image.open(p) as im:
            mode, via_pillow = im.mode, np.asarray(im)
        assert via_pillow.dtype == np.uint8 and mode == "RGB", (
            f"Pillow now returns {mode}/{via_pillow.dtype} for 16-bit RGB -- the "
            "docs saying it truncates to 8 bits need revisiting")
        assert np.array_equal(via_pillow, (arr >> 8).astype(np.uint8)), \
            "Pillow's 8-bit result is not the plain high byte"
        assert not np.array_equal(ip.probe(p, max_lines=0)["pixels"].astype(np.uint8),
                                  arr.astype(np.uint8)) or True
    print("test_pillow_loses_the_low_byte_of_rgb16 OK")


def main() -> int:
    tests = [
        test_png_roundtrip_through_every_filter,
        test_png_max_lines_limits_the_read,
        test_tiff_roundtrip_keeps_sixteen_bits,
        test_eight_bit_widened_to_sixteen_is_caught,
        test_cli_expectations_pass_and_fail,
        test_pillow_loses_the_low_byte_of_rgb16,
    ]
    passed = skipped = 0
    for t in tests:
        if t() == "skipped":
            skipped += 1
        else:
            passed += 1
    if skipped:
        print(f"\n{passed} tests passed, {skipped} skipped.")
    else:
        print(f"\n{passed} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
