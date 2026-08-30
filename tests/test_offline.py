#!/usr/bin/env python3
"""Offline tests for of135i.image — no hardware required.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python driver/tests/test_offline.py
"""

import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import image


def _make_raw(lines: int, width: int, extra_bytes: bytes = b"") -> tuple[bytes, np.ndarray]:
    """Build a synthetic pixel-interleaved RGB16LE buffer with known,
    distinct per-pixel values, plus the expected assembled array."""
    expected = np.zeros((lines, width, 3), dtype="<u2")
    raw = bytearray()
    for y in range(lines):
        for x in range(width):
            r = (y * 1000 + x * 3 + 0) & 0xFFFF
            g = (y * 1000 + x * 3 + 1) & 0xFFFF
            b = (y * 1000 + x * 3 + 2) & 0xFFFF
            expected[y, x] = (r, g, b)
            raw += struct.pack("<HHH", r, g, b)
    raw += extra_bytes
    return bytes(raw), expected


def test_assemble_shape_and_endianness():
    width, lines = 5, 4
    raw, expected = _make_raw(lines, width)
    arr = image.assemble(raw, width)
    assert arr.shape == (lines, width, 3), f"shape {arr.shape}"
    assert arr.dtype == np.uint16, f"dtype {arr.dtype}"
    assert np.array_equal(arr, expected), "pixel values / endianness mismatch"
    print("test_assemble_shape_and_endianness OK")


def test_assemble_trims_partial_trailing_line():
    width, lines = 6, 3
    raw, expected = _make_raw(lines, width, extra_bytes=b"\x01\x02\x03")  # 3 stray bytes
    arr = image.assemble(raw, width)
    assert arr.shape == (lines, width, 3), f"shape {arr.shape} (trailing partial line not trimmed)"
    assert np.array_equal(arr, expected)
    print("test_assemble_trims_partial_trailing_line OK")


def test_assemble_single_pixel():
    raw = struct.pack("<HHH", 0x0001, 0x0203, 0x0405)
    arr = image.assemble(raw, width=1)
    assert arr.shape == (1, 1, 3)
    assert arr[0, 0, 0] == 0x0001
    assert arr[0, 0, 1] == 0x0203
    assert arr[0, 0, 2] == 0x0405
    print("test_assemble_single_pixel OK")


def test_tiff_roundtrip_via_pillow():
    from PIL import Image

    width, lines = 17, 9  # deliberately non-round dims
    xs = np.linspace(0, 65535, width, dtype="<u2")
    ys = np.linspace(0, 65535, lines, dtype="<u2")
    arr = np.zeros((lines, width, 3), dtype="<u2")
    arr[:, :, 0] = xs[None, :]                # R: horizontal gradient
    arr[:, :, 1] = ys[:, None]                # G: vertical gradient
    arr[:, :, 2] = (xs[None, :].astype(np.uint32) + ys[:, None].astype(np.uint32)) & 0xFFFF  # B: mix

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "gradient.tiff"
        image.write_tiff16(arr, path)

        im = Image.open(path)
        assert im.size == (width, lines), f"PIL size {im.size} != {(width, lines)}"
        assert im.mode == "RGB", f"unexpected PIL mode {im.mode}"
        # Sanity-check the TIFF tags Pillow parsed out of our IFD.
        assert im.tag_v2[258] == (16, 16, 16), f"BitsPerSample {im.tag_v2[258]}"
        assert im.tag_v2[262] == 2, f"PhotometricInterpretation {im.tag_v2[262]}"
        assert im.tag_v2[277] == 3, f"SamplesPerPixel {im.tag_v2[277]}"

        # Note: Pillow has no true 16-bit-per-channel RGB mode; its
        # "RGB;16L" rawmode decoder for 16-bit RGB TIFFs truncates each
        # sample to its high byte. That's still a real, independent
        # check that width/height/strip offsets/sample order in our
        # writer are correct — compare against the same truncation.
        got = np.array(im)
        assert got.shape == (lines, width, 3), f"PIL array shape {got.shape}"
        assert got.dtype == np.uint8, f"PIL array dtype {got.dtype}"
        expected_hi = (arr >> 8).astype(np.uint8)
        assert np.array_equal(got, expected_hi), "TIFF round-trip pixel mismatch (high byte)"
    print("test_tiff_roundtrip_via_pillow OK")


def test_pnm_roundtrip_via_pillow():
    from PIL import Image

    width, lines = 11, 6
    rng = np.random.default_rng(42)
    arr = rng.integers(0, 65536, size=(lines, width, 3), dtype=np.uint32).astype("<u2")

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "gradient.ppm"
        image.write_pnm16(arr, path)

        im = Image.open(path)
        assert im.size == (width, lines), f"PIL size {im.size} != {(width, lines)}"

        # Same Pillow limitation as the TIFF case: no true 16-bit RGB
        # mode. For a maxval=65535 PPM, Pillow's reader proportionally
        # rescales each big-endian sample to 0-255 (value/65535*255,
        # rounded) rather than truncating. Still a real, independent
        # check that our header + big-endian sample order are correct.
        got = np.array(im)
        assert got.shape == (lines, width, 3), f"PIL array shape {got.shape}"
        assert got.dtype == np.uint8, f"PIL array dtype {got.dtype}"
        expected = np.round(arr.astype(np.float64) / 65535 * 255).astype(np.uint8)
        assert np.array_equal(got, expected), "PNM round-trip pixel mismatch (rescaled)"
    print("test_pnm_roundtrip_via_pillow OK")


def main() -> int:
    tests = [
        test_assemble_shape_and_endianness,
        test_assemble_trims_partial_trailing_line,
        test_assemble_single_pixel,
        test_tiff_roundtrip_via_pillow,
        test_pnm_roundtrip_via_pillow,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
