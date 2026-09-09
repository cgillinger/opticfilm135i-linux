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


def test_tiff_resolution_tags():
    """XResolution/YResolution/ResolutionUnit are baseline-required fields
    and carry the scan's real dpi: without them a 3600 dpi scan is read as
    72 dpi and its physical size is lost. Checked through Pillow's tag
    parser, including the no-dpi fallback (unit 1 = no absolute unit, so
    the values state the pixel aspect ratio only)."""
    from PIL import Image

    arr = np.full((5, 7, 3), 4242, dtype="<u2")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "scan.tiff"
        image.write_tiff16(arr, path, dpi=3600)
        im = Image.open(path)
        assert im.tag_v2[282] == 3600 and im.tag_v2[283] == 3600, (
            im.tag_v2[282], im.tag_v2[283])
        assert im.tag_v2[296] == 2, f"ResolutionUnit {im.tag_v2[296]} != inch"
        assert im.info.get("dpi") == (3600, 3600), im.info.get("dpi")
        # The new out-of-line RATIONAL block must not have moved the pixels.
        assert Path(path).read_bytes()[8:8 + arr.nbytes] == arr.tobytes()

        # Both out-of-line blocks (resolution + ICC) in one file.
        image.write_tiff16(arr, path, icc=image.srgb_icc(), dpi=1200)
        im = Image.open(path)
        assert im.info.get("dpi") == (1200, 1200), im.info.get("dpi")
        assert im.info.get("icc_profile") == image.srgb_icc()

        # No dpi: TIFF's own "no absolute unit" -- still baseline-conformant.
        image.write_tiff16(arr, path)
        im = Image.open(path)
        assert im.tag_v2[296] == 1, f"ResolutionUnit {im.tag_v2[296]} != none"
        assert im.tag_v2[282] == 1 and im.tag_v2[283] == 1
    print("test_tiff_resolution_tags OK")


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


def test_digitize_append_after_torn_line():
    """A torn last line (an interrupted write with no trailing newline)
    must not swallow the next record: append writes a newline first, so the
    new record is readable and its roll number is seen. (Fix #1.)"""
    from of135i import digitize
    with tempfile.TemporaryDirectory() as d:
        digitize.append_manifest(d, {"roll": 1, "status": "ok"})
        # simulate an interrupted write: a partial JSON line, no newline
        with open(digitize.manifest_path(d), "a") as f:
            f.write('{"roll": 2, "status": "ok"')      # torn, no "}\n"
        digitize.append_manifest(d, {"roll": 3, "status": "ok"})
        recs = digitize.read_manifest(d)
        rolls = [r["roll"] for r in recs]
        assert 1 in rolls and 3 in rolls, rolls        # both valid records readable
        assert digitize.next_roll(d) == 4, digitize.next_roll(d)  # roll 3 counted
    print("test_digitize_append_after_torn_line OK")


def test_digitize_overwrite_guard():
    """A roll directory with a saved frame but no manifest entry must not be
    reused by auto-numbering, and --roll onto it without --force is refused
    without touching the file. (Fix #2.)"""
    import argparse
    from of135i import cli, digitize
    with tempfile.TemporaryDirectory() as d:
        rd = digitize.roll_dir(d, "", 1)
        rd.mkdir(parents=True)
        marker = rd / "f1.tiff"
        marker.write_bytes(b"existing")
        # auto-numbering skips roll 1 (disk-based), even with no manifest
        assert digitize.next_roll(d) == 2
        # targeting roll 1 without --force is refused, file untouched
        args = argparse.Namespace(
            out=d, prefix="", roll=1, force=False, assume_loaded=True,
            dpi=3600, positive=False, rotate=0, ir=True, no_clean=False,
            no_diag=True, park="verbatim", warmup_budget=None)
        rc = cli._cmd_digitize(args)
        assert rc == 2, rc
        assert marker.read_bytes() == b"existing"
        assert digitize.read_manifest(d) == []          # nothing recorded
    print("test_digitize_overwrite_guard OK")


def test_digitize_records_failed_on_write_error():
    """If the scan/write flow raises (e.g. OSError writing an image) after a
    frame was saved, the roll is recorded failed with what was saved, and a
    failing manifest write does not mask the original error. (Fix #3.)"""
    import argparse
    from of135i import cli, digitize

    class _Boom(Exception):
        pass

    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(
            out=d, prefix="", roll=1, force=True, assume_loaded=True,
            dpi=3600, positive=False, rotate=0, ir=True, no_clean=False,
            no_diag=True, park="verbatim", warmup_budget=None)
        # _run_writing_session re-raises a generic exception; simulate that
        orig = cli._run_writing_session
        cli._run_writing_session = lambda body: (_ for _ in ()).throw(_Boom("disk full"))
        try:
            raised = None
            try:
                cli._cmd_digitize(args)
            except _Boom as e:
                raised = e
            assert isinstance(raised, _Boom), "original error must propagate"
            recs = digitize.read_manifest(d)
            assert len(recs) == 1 and recs[0]["status"] == "failed", recs
            assert recs[0]["stage"] == "scan"
        finally:
            cli._run_writing_session = orig
    print("test_digitize_records_failed_on_write_error OK")


def test_digitize_manifest_error_does_not_mask_original():
    """If the manifest write ITSELF fails while recording a failed roll, the
    original scan error must still propagate (the manifest failure is
    swallowed with a warning, not raised over the real cause). (Fix B.)"""
    import argparse
    from of135i import cli, digitize

    class _Boom(Exception):
        pass

    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(
            out=d, prefix="", roll=1, force=True, assume_loaded=True,
            dpi=3600, positive=False, rotate=0, ir=True, no_clean=False,
            no_diag=True, park="verbatim", warmup_budget=None)
        orig_rws = cli._run_writing_session
        orig_app = digitize.append_manifest
        cli._run_writing_session = lambda body: (_ for _ in ()).throw(_Boom("disk full"))
        digitize.append_manifest = lambda out, rec: (_ for _ in ()).throw(
            OSError("manifest unwritable"))
        try:
            raised = None
            try:
                cli._cmd_digitize(args)
            except _Boom as e:
                raised = e
            except OSError as e:      # the manifest error must NOT surface here
                raised = e
            assert isinstance(raised, _Boom), (
                "the original scan error must propagate, not the manifest error")
        finally:
            cli._run_writing_session = orig_rws
            digitize.append_manifest = orig_app
    print("test_digitize_manifest_error_does_not_mask_original OK")


def test_digitize_success_tolerates_manifest_error():
    """A manifest write that fails on the SUCCESS path must not turn a good
    scan into a crash: the images are on disk, so digitize warns and returns
    the scan's rc (0) instead of raising. (Fix F.)"""
    import argparse
    from of135i import cli, digitize

    class _MockScanner:
        park_mode = "verbatim"
        warmup_budget_s = 60.0
        last_diag = {"gain_codes": None, "offset_codes": None,
                     "dark_b_substituted": False}

        def check_start_state(self): pass
        def is_magazine_present(self): return True
        def initialize(self, ir, dpi): pass
        def scan(self, frame, ir=None, dpi=None): return (b"", 0)
        def eject(self): pass

    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(
            out=d, prefix="", roll=1, force=True, assume_loaded=True,
            dpi=3600, positive=False, rotate=0, ir=False, no_clean=False,
            no_diag=True, park="verbatim", warmup_budget=None)
        orig_rws = cli._run_writing_session
        orig_fin = cli._finish_digitize_frame
        orig_app = digitize.append_manifest
        cli._run_writing_session = lambda body: body(_MockScanner())
        cli._finish_digitize_frame = (
            lambda a, raw, w, out, dual, progress=None: (out, None, None, False))
        digitize.append_manifest = lambda out, rec: (_ for _ in ()).throw(
            OSError("manifest unwritable"))
        try:
            rc = cli._cmd_digitize(args)     # must not raise
        finally:
            cli._run_writing_session = orig_rws
            cli._finish_digitize_frame = orig_fin
            digitize.append_manifest = orig_app
        assert rc == 0, rc
    print("test_digitize_success_tolerates_manifest_error OK")


def test_digitize_dispatch_plain_on_no_ir():
    """--no-ir + 3600 uses the PLAIN flow: initialize(ir=False) and scan()
    without ir=True (matching the scan command). Verified through
    _cmd_digitize's body with a mock scanner. (Fix #4.)"""
    import argparse
    from of135i import cli
    calls: list = []

    class _MockScanner:
        park_mode = "verbatim"
        warmup_budget_s = 60.0
        last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6],
                     "dark_b_substituted": False}

        def check_start_state(self): pass
        def is_magazine_present(self): return True
        def initialize(self, ir, dpi): calls.append(("init", ir, dpi))
        def scan(self, frame, ir=None, dpi=None):
            calls.append(("scan", frame, ir, dpi))
            return (b"", 0)
        def eject(self): calls.append(("eject",))

    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(
            out=d, prefix="", roll=1, force=True, assume_loaded=True,
            dpi=3600, positive=False, rotate=0, ir=False, no_clean=False,
            no_diag=True, park="verbatim", warmup_budget=None)
        orig_rws = cli._run_writing_session
        orig_fin = cli._finish_digitize_frame
        cli._run_writing_session = lambda body: body(_MockScanner())
        cli._finish_digitize_frame = (
            lambda a, raw, w, out, dual, progress=None: (out, None, None, False))
        try:
            rc = cli._cmd_digitize(args)
        finally:
            cli._run_writing_session = orig_rws
            cli._finish_digitize_frame = orig_fin
        assert rc == 0, rc
        inits = [c for c in calls if c[0] == "init"]
        scans = [c for c in calls if c[0] == "scan"]
        assert inits and all(c[1] is False for c in inits), inits   # plain init
        assert scans and all(c[2] is None for c in scans), scans    # scan() no ir=True
    print("test_digitize_dispatch_plain_on_no_ir OK")


def test_digitize_preview_does_not_alter_main():
    """With --positive the main image stays the raw negative and a separate
    positive preview is written; the preview does not change the main
    pixels. (Fix #5.) Captures the arrays via _write_image."""
    import argparse
    from of135i import cli, image
    # H large enough to survive align_channels' stagger crop (~12 rows at 3600)
    W, H = 8, 60
    arr = (np.arange(H * W * 3, dtype="<u2") % 60000).reshape(H, W, 3)
    raw = np.ascontiguousarray(arr).tobytes()
    args = argparse.Namespace(dpi=3600, ir=False, no_clean=False, rotate=0,
                              positive=True)
    written: dict = {}
    orig = cli._write_image
    cli._write_image = lambda a, out, positive=False, dpi=None: written.__setitem__(
        out, (a.copy(), positive))
    try:
        main, irf, prev, cleaned = cli._finish_digitize_frame(args, raw, W, "f1.tiff", dual=False)
    finally:
        cli._write_image = orig
    assert irf is None and prev is not None and cleaned is False
    main_arr, main_pos = written[main]
    prev_arr, prev_pos = written[prev]
    expected = image.align_channels(image.assemble(raw, W), dpi=3600)
    # The preview carries the vendor orientation (mirror + rot90(·,3)) applied
    # BEFORE to_positive, matching `scan --positive`; rotate=0 here so no extra
    # rotation follows. The main image is the raw negative, unrotated/mirrored.
    expected_prev = image.to_positive(
        np.ascontiguousarray(np.rot90(expected, 3)[:, ::-1]))
    assert main_pos is False, "main must not be written as positive"
    assert np.array_equal(main_arr, expected), "main must be the raw negative, unchanged"
    assert np.array_equal(prev_arr, expected_prev), "preview is the oriented positive"
    assert not np.array_equal(main_arr, prev_arr), "preview must differ from main"
    print("test_digitize_preview_does_not_alter_main OK")


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


def test_digitize_force_clears_stale_outputs():
    """clear_roll_outputs removes a roll's f*.tiff and f*.diag.json (visible,
    IR, preview and diag), so a --force re-scan can't leave a previous run's
    sidecars mixed in. Unrelated files are left alone; the manifest is not
    touched here. (Audit note 1.)"""
    from of135i import digitize
    with tempfile.TemporaryDirectory() as d:
        rd = digitize.roll_dir(d, "boxA-", 1)
        rd.mkdir(parents=True)
        for name in ("f1.tiff", "f1-ir.tiff", "f1-preview.tiff", "f1.diag.json",
                     "f2.tiff"):
            (rd / name).write_bytes(b"x")
        (rd / "notes.txt").write_bytes(b"keep me")     # unrelated, must stay
        removed = digitize.clear_roll_outputs(d, "boxA-", 1)
        assert set(removed) == {"f1.tiff", "f1-ir.tiff", "f1-preview.tiff",
                                "f1.diag.json", "f2.tiff"}, removed
        assert (rd / "notes.txt").exists()
        assert not any(rd.glob("f*.tiff")) and not any(rd.glob("f*.diag.json"))
        # a missing dir is a no-op, not an error
        assert digitize.clear_roll_outputs(d, "boxA-", 9) == []
    print("test_digitize_force_clears_stale_outputs OK")


def test_digitize_prefix_sequences_are_independent():
    """Two prefixes in one --out are independent roll sequences: next_roll,
    rolls_done and roll_is_done are all filtered on prefix, so boxA's rolls
    don't advance or 'done'-mark boxB. (Fix C.)"""
    from of135i import digitize
    with tempfile.TemporaryDirectory() as d:
        digitize.append_manifest(d, {"roll": 1, "status": "ok", "prefix": "boxA-"})
        digitize.append_manifest(d, {"roll": 2, "status": "ok", "prefix": "boxA-"})
        # boxB has nothing recorded: its sequence starts at 1, not 3
        assert digitize.next_roll(d, "boxB-") == 1, digitize.next_roll(d, "boxB-")
        assert digitize.next_roll(d, "boxA-") == 3
        # done-ness is per prefix
        assert digitize.rolls_done(d, "boxA-") == {1, 2}
        assert digitize.rolls_done(d, "boxB-") == set()
        assert digitize.roll_is_done(d, 1, "boxA-") is True
        assert digitize.roll_is_done(d, 1, "boxB-") is False
        # a record with no prefix field counts as prefix ""
        digitize.append_manifest(d, {"roll": 5, "status": "ok"})
        assert digitize.roll_is_done(d, 5, "") is True
        assert digitize.roll_is_done(d, 5, "boxA-") is False
    print("test_digitize_prefix_sequences_are_independent OK")


def test_digitize_frames_defaults_to_1_4_and_validates_before_hardware():
    """--frames on digitize defaults to "1-4" (verified via a spy on
    cli._parse_frames -- _run_writing_session is stubbed so the default
    can be observed without a Scanner ever being constructed) and an
    out-of-range spec exits 2 before the roll directory, the load flow
    or _run_writing_session are ever reached."""
    import argparse
    from of135i import cli

    calls = []
    orig_parse = cli._parse_frames
    orig_run = cli._run_writing_session
    cli._parse_frames = lambda spec: (calls.append(spec), orig_parse(spec))[1]
    cli._run_writing_session = lambda body: 0
    try:
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(
                out=d, prefix="", roll=1, force=True, assume_loaded=True,
                dpi=3600, positive=False, rotate=0, ir=True, no_clean=False,
                no_diag=True, park="verbatim", warmup_budget=None, frames=None)
            cli._cmd_digitize(args)
    finally:
        cli._parse_frames = orig_parse
        cli._run_writing_session = orig_run
    assert calls == ["1-4"], calls

    with tempfile.TemporaryDirectory() as d:
        for spec in ("1-7", "0-2"):
            args = argparse.Namespace(
                out=d, prefix="", roll=1, force=True, assume_loaded=True,
                dpi=3600, positive=False, rotate=0, ir=True, no_clean=False,
                no_diag=True, park="verbatim", warmup_budget=None, frames=spec)
            rc = cli._cmd_digitize(args)
            assert rc == 2, (spec, rc)
    print("test_digitize_frames_defaults_to_1_4_and_validates_before_hardware OK")


def test_clear_roll_outputs_only_frame_files():
    """--force clears the roll's own frame outputs and NOTHING else. The
    names are matched exactly, so an operator's own file in the same
    directory survives -- an `f*.tiff` glob would delete family.tiff."""
    from of135i import digitize
    with tempfile.TemporaryDirectory() as d:
        rd = digitize.roll_dir(d, "", 1)
        rd.mkdir(parents=True)
        ours = ["f1.tiff", "f2-ir.tiff", "f3-preview.tiff", "f4.diag.json"]
        theirs = ["family.tiff", "favourite.diag.json", "f10.tiff",
                  "f1-crop.tiff", "notes.txt"]
        for name in ours + theirs:
            (rd / name).write_bytes(b"x")
        removed = digitize.clear_roll_outputs(d, "", 1)
        assert sorted(removed) == sorted(ours), removed
        left = sorted(p.name for p in rd.iterdir())
        assert left == sorted(theirs), left
    print("test_clear_roll_outputs_only_frame_files OK")


def test_digitize_records_partial_frame_progress():
    """A failure part-way through a frame must still record which frame fell,
    what had been saved and which step was running: per_frame is filled in as
    each file lands, not only once the whole frame succeeded."""
    import argparse
    from of135i import cli, digitize

    class _Boom(Exception):
        pass

    class _MockScanner:
        park_mode = "verbatim"
        warmup_budget_s = 60.0
        last_diag = {"gain_codes": None, "offset_codes": None,
                     "dark_b_substituted": False}

        def check_start_state(self): pass
        def is_magazine_present(self): return True
        def initialize(self, ir, dpi): pass
        def scan(self, frame, ir=None, dpi=None): return (b"", 0)
        def eject(self): pass

    def _fake_finish(a, raw, w, out, dual, progress=None):
        if progress is not None:      # the main image landed, the rest did not
            progress["main"] = out
        return out, None, None, False

    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(
            out=d, prefix="", roll=1, force=True, assume_loaded=True,
            dpi=3600, positive=False, rotate=0, ir=False, no_clean=False,
            no_diag=False, park="verbatim", warmup_budget=None)
        orig = (cli._run_writing_session, cli._finish_digitize_frame,
                cli._write_diag_sidecar)
        cli._run_writing_session = lambda body: body(_MockScanner())
        cli._finish_digitize_frame = _fake_finish
        cli._write_diag_sidecar = lambda a, s, out, frame: (
            _ for _ in ()).throw(_Boom("sidecar unwritable"))
        try:
            raised = None
            try:
                cli._cmd_digitize(args)
            except _Boom as e:
                raised = e
        finally:
            (cli._run_writing_session, cli._finish_digitize_frame,
             cli._write_diag_sidecar) = orig
        assert isinstance(raised, _Boom), "the original error must propagate"
        recs = digitize.read_manifest(d)
        assert len(recs) == 1 and recs[0]["status"] == "failed", recs
        pf = recs[0]["per_frame"]
        assert len(pf) == 1, pf                    # the frame is recorded...
        assert pf[0]["frame"] == 1
        assert pf[0]["main"].endswith("f1.tiff"), pf[0]   # ...with what was saved
        assert pf[0]["stage"] == "diag", pf[0]            # ...and where it fell
    print("test_digitize_records_partial_frame_progress OK")


# ------------------------------------------------ SANE port (stage 2 tables)


def test_sane_tables_generated_and_current():
    """The generated C++ register tables must match of135i/tables*.py. They
    are the SANE backend's only source of register values, so a table change
    that is not regenerated would leave the backend writing stale values --
    silently, since nothing else reads them."""
    import subprocess
    rc = subprocess.run(
        [sys.executable, "tools/gen_sane_tables.py", "--check"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    print("test_sane_tables_generated_and_current OK")


def test_sane_tables_injections_land_on_value_bytes():
    """Every injection point must address the VALUE byte of the right
    register: the captured byte belongs to the reference unit, so a
    mis-indexed injection would make the backend scan with another unit's
    gain or offset -- which looks like a working scan, not like an error."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import gen_sane_tables as gen
    from of135i import tables

    by_name = {p.name: p for p in tables.PHASES}
    # (phase, injection) -> the register it must write
    expected = {
        ("cal_gain_check_a", "gain_r"): 0x5E,
        ("cal_gain_check_a", "gain_g"): 0x5E,
        ("cal_gain_check_a", "gain_b"): 0x5E,
        ("cal_shading_measure", "offset_r_hi"): 0x5D,
        ("cal_shading_measure", "offset_r_lo"): 0x5E,
        ("position", "feedl_hi"): 0x3D,
        ("position", "feedl_mid"): 0x3E,
        ("position", "feedl_lo"): 0x3F,
        ("scan", "lines_hi"): 0x26,
        ("scan", "lines_lo"): 0x27,
    }
    for (phase_name, inj), want_reg in expected.items():
        pt = gen.decode_phase(by_name[phase_name])
        idx = pt.injections[inj]
        reg, _val = pt.pairs[idx]
        assert reg == want_reg, (
            f"{phase_name}/{inj} lands on reg {reg:#04x}, expected {want_reg:#04x}")

    # Every profile must decode without the decoder's own consistency
    # checks firing (odd-length register batches, injections on a register
    # number instead of a value, a bulk write with no descriptor).
    for _key, mod, _dpi, _doc in gen.PROFILES:
        for phase in mod.PHASES:
            gen.decode_phase(phase)
    print("test_sane_tables_injections_land_on_value_bytes OK")


def test_holder_geometry_measures_a_synthetic_holder():
    """tools/holder_geometry.py reads a holder's aperture back out of a
    scan: a square-wave profile with known aperture edges must come back
    with those edges, and a single-frame window with a known
    displacement must report that displacement."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "holder_geometry",
        Path(__file__).resolve().parents[1] / "tools" / "holder_geometry.py")
    hg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hg)

    # A six-aperture holder at 600 dpi: 850 lit lines, 46 dark, repeated.
    dpi, lit, dark = 600, 850, 46
    prof = np.full(400, 800.0)
    for _ in range(6):
        prof = np.concatenate([prof, np.full(lit, 39800.0),
                               np.full(dark, 800.0)])
    prof = np.concatenate([prof, np.full(300, 800.0)])
    lines_per_mm = dpi / hg.MM_PER_INCH
    _, aps = hg.apertures(prof, lines_per_mm)
    assert len(aps) == 6, len(aps)
    pitches = [((b + a) / 2) for a, b in aps]
    steps = [round(y - x, 1) for x, y in zip(pitches, pitches[1:])]
    assert all(abs(s - (lit + dark)) < 0.01 for s in steps), steps
    for a, b in aps:
        assert abs((b - a) - lit) < 1.5, (a, b)

    # One frame's window, aperture displaced 10 lines from the centre.
    win = 882
    frame = np.full(win, 800.0)
    start = (win - lit) // 2 + 10
    frame[start:start + lit] = 39800.0
    args = type("A", (), {"min_aperture_mm": hg.MIN_APERTURE_MM, "dual": False})()
    rep, _, _ = hg.report_frame(frame, dpi, args)
    assert rep["aperture_found"] is True
    assert abs(rep["centre_offset_lines"] - 10) < 1.0, rep["centre_offset_lines"]


def test_holder_geometry_ignores_edges_that_are_not_the_plastic():
    """Film in the aperture makes its own strong transitions -- a dark
    subject against a bright one -- and the first hardware run had
    several of them mid-window. A holder edge has the opaque plastic on
    one side; a picture edge has neither side anywhere near black."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "holder_geometry",
        Path(__file__).resolve().parents[1] / "tools" / "holder_geometry.py")
    hg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hg)

    n, plastic, edge = 878, 1600.0, 840
    prof = np.full(n, 20000.0)
    prof[300:420] = 6000.0            # a dark subject: a real step, not plastic
    prof[299] = 13000.0
    prof[420] = 13000.0
    prof[edge:] = plastic             # the crossbar, 38 lines: 4 % of the window
    prof[edge - 1] = (20000.0 + plastic) / 2
    _, ed = hg.edges(prof)
    falls = [q for q, rising in ed if not rising]
    assert len(ed) == 1, ed
    assert abs(falls[0] - (edge - 1.5)) < 1.5, ed


def test_holder_geometry_summary_recovers_the_true_pitch():
    """The summary must tell 10752 from 10760 out of six per-frame
    reports. Built both ways: with the holder truly on each pitch while
    the driver commands 10760, the recommendation has to name the pitch
    the holder was built with, and list exactly the frames whose
    commanded FEEDL would change if the constant moved."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "holder_geometry",
        Path(__file__).resolve().parents[1] / "tools" / "holder_geometry.py")
    hg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hg)

    frames = list(range(1, 7))
    commanded = [6746 + (n - 1) * 10760 for n in frames]
    for truth in (10752, 10760):
        reports = []
        for n in frames:
            abs_centre = 6746 + (n - 1) * truth
            off = abs_centre - commanded[n - 1]
            reports.append({
                "aperture_found": True,
                "centre_offset_hwdpi": off,
                "centre_offset_mm": off / 7200 * hg.MM_PER_INCH,
                "aperture_length_mm": 35.92,
            })
        out = hg.summarise(reports, frames, commanded)
        rec = out["recommendation"]
        assert rec["better_candidate"] == truth, (truth, rec)
        assert rec["verdict"] == "candidate", (truth, rec)
        assert abs(out["free_pitch_fit"]["pitch_hwdpi"] - truth) < 1.0, out
        expect = [2, 3, 4, 5, 6] if truth != 10760 else []
        assert rec["frames_whose_feedl_changes"] == expect, rec
        # The model that is wrong must be visibly worse, not marginally.
        wrong = str(10760 if truth == 10752 else 10752)
        assert out["fixed_pitch_models"][wrong]["max_abs_residual_mm"] > 0.05

    # A holder on neither candidate must not be reported as one of them.
    off_grid = 10735
    reports = []
    for n in frames:
        off = (6746 + (n - 1) * off_grid) - commanded[n - 1]
        reports.append({"aperture_found": True, "centre_offset_hwdpi": off,
                        "centre_offset_mm": off / 7200 * hg.MM_PER_INCH,
                        "aperture_length_mm": 35.92})
    rec = hg.summarise(reports, frames, commanded)["recommendation"]
    assert rec["verdict"] == "neither-candidate", rec
    assert abs(rec["measured_pitch"] - off_grid) < 1.0, rec


def test_holder_geometry_edges_survive_uneven_illumination():
    """The edge finder must not be fooled by a lit level that falls
    across the window. On the first empty-holder run frame 6's lit
    plateau dropped by a third from one end to the other, and a global
    threshold put its trailing edge 21 lines early. The threshold has to
    come from the levels either side of the transition itself."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "holder_geometry",
        Path(__file__).resolve().parents[1] / "tools" / "holder_geometry.py")
    hg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hg)

    n, edge = 878, 840
    prof = np.linspace(15000.0, 7000.0, n)      # lit, falling by half
    prof[edge:] = 1600.0                        # the plastic
    prof[edge - 1] = (prof[edge - 2] + 1600.0) / 2   # a one-line ramp
    _, ed = hg.edges(prof)
    falls = [q for q, rising in ed if not rising]
    assert len(falls) == 1, ed
    assert abs(falls[0] - (edge - 1.5)) < 1.5, falls


def main() -> int:
    tests = [
        test_assemble_shape_and_endianness,
        test_assemble_trims_partial_trailing_line,
        test_assemble_single_pixel,
        test_tiff_roundtrip_via_pillow,
        test_tiff_icc_profile_via_pillow,
        test_tiff_resolution_tags,
        test_pnm_roundtrip_via_pillow,
        test_digitize_layout_and_paths,
        test_digitize_manifest_roundtrip_and_torn_line,
        test_digitize_next_roll_and_done,
        test_digitize_records_failed_load,
        test_digitize_append_after_torn_line,
        test_digitize_overwrite_guard,
        test_digitize_records_failed_on_write_error,
        test_digitize_manifest_error_does_not_mask_original,
        test_digitize_success_tolerates_manifest_error,
        test_digitize_dispatch_plain_on_no_ir,
        test_digitize_preview_does_not_alter_main,
        test_digitize_force_clears_stale_outputs,
        test_digitize_prefix_sequences_are_independent,
        test_digitize_frames_defaults_to_1_4_and_validates_before_hardware,
        test_clear_roll_outputs_only_frame_files,
        test_digitize_records_partial_frame_progress,
        test_sane_tables_generated_and_current,
        test_sane_tables_injections_land_on_value_bytes,
        test_holder_geometry_measures_a_synthetic_holder,
        test_holder_geometry_summary_recovers_the_true_pitch,
        test_holder_geometry_edges_survive_uneven_illumination,
        test_holder_geometry_ignores_edges_that_are_not_the_plastic,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
