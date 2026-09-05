#!/usr/bin/env python3
"""Offline tests for tools/hwblock.py -- no hardware required.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_hwblock.py

Covers the pure analysis functions (image_stats, film_rows,
pair_rms_8bit, repro_summary, offset_summary, dpi_shift, render_report)
against synthetic data, plus argparse's handling of a missing
subcommand and a missing --out -- checked by calling build_parser()/
parse_args() directly (pure argument parsing; this never touches
hardware, see hwblock.py's own module docstring for why main() is
still safe to exercise this way).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import numpy as np

import hwblock


# --------------------------------------------------------------- film_rows


def _make_film_band(lines=400, width=300, band_lo=30, band_hi=370, dark=1000, bright=40000):
    """A synthetic (lines, width, 3) array: a bright "film" band between
    rows [band_lo, band_hi) on a dark border, all channels identical."""
    arr = np.full((lines, width, 3), dark, dtype=np.uint16)
    arr[band_lo:band_hi, :, :] = bright
    return arr


def test_film_rows_and_image_stats():
    arr = _make_film_band()
    rows = hwblock.film_rows(arr)
    assert abs(rows["film_start_row"] - 30) <= 12, rows
    assert abs(rows["film_end_row"] - 369) <= 12, rows
    assert rows["bottom_cut"] is False, rows

    stats = hwblock.image_stats(arr)
    assert stats["shape"] == [400, 300, 3], stats["shape"]
    assert stats["flat"] is False, stats
    print("test_film_rows_and_image_stats OK")


def test_film_rows_bottom_cut():
    # Film band running all the way to the bottom of the frame.
    arr = _make_film_band(lines=400, band_lo=30, band_hi=400)
    rows = hwblock.film_rows(arr)
    assert rows["bottom_cut"] is True, rows
    print("test_film_rows_bottom_cut OK")


# ------------------------------------------------------------- image_stats


def test_image_stats_flat():
    rng = np.random.default_rng(1)
    arr = (40000 + rng.normal(0, 300, size=(50, 40, 3))).astype(np.uint16)
    stats = hwblock.image_stats(arr)
    assert stats["flat"] is True, stats
    print("test_image_stats_flat OK")


# ----------------------------------------------------------- pair_rms_8bit


def test_pair_rms_8bit_identical_and_shifted():
    rng = np.random.default_rng(0)
    base = (rng.random((80, 64, 3)) * 60000).astype(np.uint16)
    assert hwblock.pair_rms_8bit(base, base) == 0.0

    shifted = np.roll(base, 16, axis=1)  # 2 downscaled (8 px) blocks
    rms = hwblock.pair_rms_8bit(base, shifted)
    assert rms > 0.0, rms
    print("test_pair_rms_8bit_identical_and_shifted OK")


# ------------------------------------------------------------- repro_summary


def test_repro_summary():
    stats = [
        {"channel_mean": [100.0, 200.0, 150.0], "flat": False, "bottom_cut": False,
         "pair_rms_to_prev": None},
        {"channel_mean": [101.0, 199.0, 151.0], "flat": False, "bottom_cut": False,
         "pair_rms_to_prev": 1.5},
        {"channel_mean": [102.0, 198.0, 152.0], "flat": False, "bottom_cut": True,
         "pair_rms_to_prev": 2.0},
    ]
    summary = hwblock.repro_summary(stats)
    assert summary["n_scans"] == 3
    assert summary["bottom_cut_any"] is True
    assert summary["flat_any"] is False
    assert summary["pair_rms"] == [1.5, 2.0]
    assert summary["pair_rms_max"] == 2.0
    print("test_repro_summary OK")


def test_repro_summary_empty():
    summary = hwblock.repro_summary([])
    assert summary == {"n_scans": 0}
    print("test_repro_summary_empty OK")


# ------------------------------------------------------------ offset_summary


def test_offset_summary():
    sidecars = [
        {"offset_codes": [264, 265, 264], "gain_codes": [0x2D, 0x21, 0x27]},
        {"offset_codes": [267, 266, 267], "gain_codes": [0x2D, 0x21, 0x27]},
        {"offset_codes": [269, 268, 269], "gain_codes": [0x2E, 0x21, 0x28]},
    ]
    result = hwblock.offset_summary(sidecars)
    assert result["n_scans"] == 3
    by_channel = {c["channel"]: c for c in result["channels"]}

    assert by_channel["R"]["offset_spread"] == 5, by_channel["R"]
    assert "unstable" in by_channel["R"]["flags"], by_channel["R"]
    assert by_channel["G"]["offset_spread"] == 3, by_channel["G"]
    assert "unstable" not in by_channel["G"]["flags"], by_channel["G"]
    assert by_channel["R"]["reference_offset"] == 0x010B
    assert abs(by_channel["R"]["delta_from_reference"]) < 1
    assert not by_channel["R"]["flags"] or "far from reference — review" not in by_channel["R"]["flags"]
    print("test_offset_summary OK")


# ----------------------------------------------------------------- dpi_shift


def test_dpi_shift():
    result = hwblock.dpi_shift(1085, [26, 26, 27], 3600)
    assert abs(result["shift_rows"] - 1059) < 1.0, result
    assert abs(result["shift_mm"] - 7.47) < 0.1, result
    print("test_dpi_shift OK")


# --------------------------------------------------------------- render_report


def test_render_report_partial_never_raises():
    partial = {"block": "warm", "steps": {"W0": {"state": "idle-homed", "magazine_present": True}},
               "status": "RUNNING"}
    text = hwblock.render_report(partial)
    assert isinstance(text, str) and text
    assert "Status:" in text

    # Even a totally empty summary must not raise.
    text_empty = hwblock.render_report({})
    assert isinstance(text_empty, str) and "Status:" in text_empty
    print("test_render_report_partial_never_raises OK")


def test_render_report_full_includes_findings():
    full = {
        "block": "warm",
        "started_utc": "2026-09-04T00:00:00+00:00",
        "finished_utc": "2026-09-04T01:00:00+00:00",
        "args": {"out": "/tmp/x", "frame": 1, "repeat": 10},
        "driver_revision": "abc1234",
        "steps": {
            "W0": {"state": "idle-homed", "magazine_present": True},
            "W2": {"n_scans": 10, "channels": [{"channel": "R", "flags": ["unstable"]}]},
        },
        "status": "COMPLETED",
    }
    text = hwblock.render_report(full)
    assert "Status: COMPLETED" in text
    assert "unstable" in text
    assert "abc1234" in text
    print("test_render_report_full_includes_findings OK")


# ------------------------------------------------------------------- argparse


def test_argparse_missing_subcommand():
    parser = hwblock.build_parser()
    args = parser.parse_args([])
    assert getattr(args, "command", None) is None
    print("test_argparse_missing_subcommand OK")


def test_argparse_missing_out():
    parser = hwblock.build_parser()
    try:
        parser.parse_args(["warm"])
        assert False, "expected SystemExit for missing --out"
    except SystemExit as e:
        assert e.code == 2, f"expected exit code 2, got {e.code}"
    print("test_argparse_missing_out OK")


def test_main_no_subcommand_returns_2_without_touching_hardware():
    # args.command is None here, so main() must return before any USB
    # access or filesystem creation is attempted.
    code = hwblock.main([])
    assert code == 2
    print("test_main_no_subcommand_returns_2_without_touching_hardware OK")


def test_cold_block_is_retired_and_touches_nothing():
    """`hwblock cold` exits 2 with the retirement note, creates no output
    directory and opens no device (Test 22: scanning straight after
    cold_init is not a vendor path)."""
    import io, contextlib, tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "never-created")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = hwblock.main(["cold", "--out", out])
        assert code == 2 and "retired" in err.getvalue().lower(), (code, err.getvalue())
        assert not os.path.exists(out)
    print("test_cold_block_is_retired_and_touches_nothing OK")


def test_main_repeat_too_low_rejected():
    code = hwblock.main(["warm", "--out", "/tmp/does-not-matter", "--repeat", "1"])
    assert code == 2
    print("test_main_repeat_too_low_rejected OK")


def main() -> int:
    tests = [
        test_film_rows_and_image_stats,
        test_film_rows_bottom_cut,
        test_image_stats_flat,
        test_pair_rms_8bit_identical_and_shifted,
        test_repro_summary,
        test_repro_summary_empty,
        test_offset_summary,
        test_dpi_shift,
        test_render_report_partial_never_raises,
        test_render_report_full_includes_findings,
        test_argparse_missing_subcommand,
        test_argparse_missing_out,
        test_main_no_subcommand_returns_2_without_touching_hardware,
        test_cold_block_is_retired_and_touches_nothing,
        test_main_repeat_too_low_rejected,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
