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


def test_tiff_icc_profile_via_pillow():
    from PIL import Image

    arr = np.full((5, 7, 3), 12345, dtype="<u2")
    icc = image.srgb_icc()
    assert icc[36:40] == b"acsp", "srgb.icc is not an ICC profile"
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "positive.tiff"
        image.write_tiff16(arr, path, icc=icc)
        im = Image.open(path)
        assert im.info.get("icc_profile") == icc, "ICC profile not round-tripped"
        assert im.size == (7, 5) and im.tag_v2[258] == (16, 16, 16)
        # Pixel data must be untouched by the extra tag.
        raw = Path(path).read_bytes()
        assert raw[8:8 + arr.nbytes] == arr.tobytes()

        image.write_tiff16(arr, path)
        assert "icc_profile" not in Image.open(path).info
    print("test_tiff_icc_profile_via_pillow OK")


# ------------------------------------------------ digitize staging (Test 35)


def test_digitize_layout_and_paths():
    from of135i import digitize
    assert digitize.roll_dirname("", 1) == "roll-001"
    assert digitize.roll_dirname("boxA-", 12) == "boxA-roll-012"
    assert digitize.frame_path("/s", "boxA-", 3, 2) == \
        Path("/s") / "boxA-roll-003" / "f2.tiff"
    print("test_digitize_layout_and_paths OK")


def test_digitize_manifest_roundtrip_and_torn_line():
    from of135i import digitize
    with tempfile.TemporaryDirectory() as d:
        assert digitize.read_manifest(d) == []          # missing -> []
        digitize.append_manifest(d, {"roll": 1, "status": "ok"})
        digitize.append_manifest(d, {"roll": 2, "status": "failed"})
        # a torn/blank trailing line must not break resume
        with open(digitize.manifest_path(d), "a") as f:
            f.write("\n{not valid json")
        recs = digitize.read_manifest(d)
        assert [r["roll"] for r in recs] == [1, 2], recs
    print("test_digitize_manifest_roundtrip_and_torn_line OK")


def test_digitize_next_roll_and_done():
    from of135i import digitize
    with tempfile.TemporaryDirectory() as d:
        assert digitize.next_roll(d) == 1                 # empty
        digitize.append_manifest(d, {"roll": 1, "status": "ok"})
        digitize.append_manifest(d, {"roll": 2, "status": "failed"})
        # next is one past the HIGHEST seen (a failed roll's number is not
        # silently reused), and only 'ok' rolls count as done
        assert digitize.next_roll(d) == 3
        assert digitize.rolls_done(d) == {1}
        assert digitize.roll_is_done(d, 1) is True
        assert digitize.roll_is_done(d, 2) is False
    print("test_digitize_next_roll_and_done OK")


def test_digitize_records_failed_load():
    """_cmd_digitize records a failed load as a failed roll (stage 'load')
    and attempts no scan. Exercised without USB by stubbing the load flow
    (the scan success path reuses _cmd_scan's tested finishers)."""
    import argparse
    from of135i import cli, digitize, loadflow
    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(
            out=d, prefix="", roll=None, force=False, assume_loaded=False,
            dpi=3600, positive=False, rotate=0, ir=True, no_clean=False,
            no_diag=False, park="verbatim", warmup_budget=None)
        orig = loadflow.run
        loadflow.run = lambda ask=None: 130          # simulate a load abort
        try:
            rc = cli._cmd_digitize(args)
        finally:
            loadflow.run = orig
        assert rc == 130, rc
        recs = digitize.read_manifest(d)
        assert len(recs) == 1, recs
        assert recs[0]["status"] == "failed" and recs[0]["stage"] == "load"
        assert recs[0]["roll"] == 1
        # nothing scanned: no roll dir created
        assert not (Path(d) / "roll-001").exists()
    print("test_digitize_records_failed_load OK")


def main() -> int:
    tests = [
        test_assemble_shape_and_endianness,
        test_assemble_trims_partial_trailing_line,
        test_assemble_single_pixel,
        test_tiff_roundtrip_via_pillow,
        test_tiff_icc_profile_via_pillow,
        test_pnm_roundtrip_via_pillow,
        test_digitize_layout_and_paths,
        test_digitize_manifest_roundtrip_and_torn_line,
        test_digitize_next_roll_and_done,
        test_digitize_records_failed_load,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
