#!/usr/bin/env python3
"""Offline tests for the per-session calibration cache (2026-09-17).

No hardware. Covers:
  - a second plain-3600 frame in the same session reuses the first
    frame's calibration: no CAL_DARK_A/B/WHITE/GAIN_CHECK_A/B/
    SHADING_* phase runs, but tables.CAL_REWRITE does, patched with
    the cached gain codes;
  - --recalibrate (Scanner.recalibrate) forces a full calibration on
    every frame, cache or not;
  - a (dpi, dual) key change forces a fresh calibration;
  - an exception after calibration but before the scan completes
    clears the cache, so the NEXT scan calibrates fresh rather than
    trusting a cache a failed scan did not earn;
  - the diag sidecar's "calibration" field is "fresh"/"cached";
  - the CLI's --park default is "semantic" for both scan and digitize.

Plain asserts, no pytest. Run with:
    .venv/bin/python tests/test_calibration_cache.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import tables
from tests.test_overscan import _RecordingScanner

_FULL_CAL_NAMES = {
    "cal_dark_a", "cal_dark_b", "cal_gain_check_a", "cal_gain_check_b",
    "cal_shading_measure", "cal_shading_upload",
}


def _names(runs):
    return [ph.name for ph, _inj in runs]


def test_batch_second_frame_reuses_cache_and_skips_full_calibration():
    rec = _RecordingScanner()
    rec.s._scan_plain(frame=1)
    first_names = set(_names(rec.runs))
    assert _FULL_CAL_NAMES <= first_names, first_names
    assert "cal_rewrite" not in first_names, first_names
    assert rec.s.last_diag["calibration"] == "fresh"
    assert rec.s.last_diag["gain_codes"] == [0x2E, 0x21, 0x29]
    assert rec.s._cal_cache is not None
    assert rec.s._cal_cache["key"] == (3600, False)
    assert rec.s._cal_cache["gain"] == (0x2E, 0x21, 0x29)

    rec.runs.clear()
    rec.s._scan_plain(frame=2)
    second_names = set(_names(rec.runs))
    assert not (_FULL_CAL_NAMES & second_names), second_names
    assert "cal_rewrite" in second_names, second_names
    rewrite_inject = next(inj for ph, inj in rec.runs if ph.name == "cal_rewrite")
    assert rewrite_inject == {
        "gain_r": bytes([0x2E]), "gain_g": bytes([0x21]), "gain_b": bytes([0x29]),
    }, rewrite_inject
    assert rec.s.last_diag["calibration"] == "cached"
    # The cached diag fields still describe the (unchanged) calibration.
    assert rec.s.last_diag["gain_codes"] == [0x2E, 0x21, 0x29]
    assert tuple(rec.s.last_diag["offset_codes"]) == rec.s._cal_cache["offset"]
    print("test_batch_second_frame_reuses_cache_and_skips_full_calibration OK")


def test_recalibrate_forces_full_calibration_every_frame():
    rec = _RecordingScanner()
    rec.s.recalibrate = True
    rec.s._scan_plain(frame=1)
    assert _FULL_CAL_NAMES <= set(_names(rec.runs))
    assert rec.s.last_diag["calibration"] == "fresh"
    # A cache WAS recorded (a successful full calibration always caches),
    # but --recalibrate must still ignore it on the next frame.
    assert rec.s._cal_cache is not None

    rec.runs.clear()
    rec.s._scan_plain(frame=2)
    names = set(_names(rec.runs))
    assert _FULL_CAL_NAMES <= names, names
    assert "cal_rewrite" not in names, names
    assert rec.s.last_diag["calibration"] == "fresh"
    print("test_recalibrate_forces_full_calibration_every_frame OK")


def test_cache_key_mismatch_forces_fresh_calibration():
    rec = _RecordingScanner()
    rec.s._scan_plain(frame=1)
    assert rec.s._cal_cache["key"] == (3600, False)
    # Simulate a different (dpi, dual) kind having run in between (dual
    # scans clear the cache themselves -- see _scan_dual -- this pokes
    # the key directly to isolate the mismatch check in _scan_plain).
    rec.s._cal_cache["key"] = (600, True)

    rec.runs.clear()
    rec.s._scan_plain(frame=2)
    names = set(_names(rec.runs))
    assert _FULL_CAL_NAMES <= names, names
    assert "cal_rewrite" not in names, names
    assert rec.s.last_diag["calibration"] == "fresh"
    assert rec.s._cal_cache["key"] == (3600, False)
    print("test_cache_key_mismatch_forces_fresh_calibration OK")


def test_exception_after_calibration_clears_cache():
    rec = _RecordingScanner()
    rec.s._scan_plain(frame=1)
    assert rec.s._cal_cache is not None

    real_run_phase = rec._run_phase

    def _failing_run_phase(phase, **inject):
        if phase.name == tables.POSITION.name:
            raise RuntimeError("simulated position failure")
        return real_run_phase(phase, **inject)

    rec.s._run_phase = _failing_run_phase  # type: ignore[method-assign]
    try:
        rec.s._scan_plain(frame=2)
    except RuntimeError as e:
        assert "simulated position failure" in str(e)
    else:
        raise AssertionError("expected the simulated failure to propagate")
    assert rec.s._cal_cache is None, "a failed scan must not leave a cache"

    # Restore the recorder and confirm the NEXT scan calibrates fresh
    # rather than trusting anything from the failed attempt.
    rec.s._run_phase = real_run_phase  # type: ignore[method-assign]
    rec.runs.clear()
    rec.s._scan_plain(frame=3)
    names = set(_names(rec.runs))
    assert _FULL_CAL_NAMES <= names, names
    assert "cal_rewrite" not in names, names
    assert rec.s.last_diag["calibration"] == "fresh"
    print("test_exception_after_calibration_clears_cache OK")


def test_dual_scan_clears_any_plain_cache():
    """A dual scan's own calibration overwrites the shading table in
    scanner RAM; a same-key plain cache from earlier in the session must
    not survive it (see _scan_dual's opening comment). _scan_dual clears
    the cache as its very first action, before touching any dual table
    -- so a deliberately incomplete fake table still exercises the guard:
    whatever _scan_dual raises next (AttributeError, from the fake
    lacking real phases) is irrelevant here."""
    rec = _RecordingScanner()
    rec.s._scan_plain(frame=1)
    assert rec.s._cal_cache is not None

    class _DummyTable:
        IMAGE_WIDTH = 1
        DPI = 3600

    try:
        rec.s._scan_dual(_DummyTable)
    except Exception:
        pass
    assert rec.s._cal_cache is None
    print("test_dual_scan_clears_any_plain_cache OK")


def test_cli_park_default_is_semantic():
    from of135i import cli
    parser = cli.build_parser()
    scan_defaults = parser.parse_args(["scan", "-o", "/tmp/x.tiff"])
    assert scan_defaults.park == "semantic", scan_defaults.park
    assert scan_defaults.recalibrate is False
    dig_defaults = parser.parse_args(["digitize", "-o", "/tmp/roll"])
    assert dig_defaults.park == "semantic", dig_defaults.park
    assert dig_defaults.recalibrate is False
    print("test_cli_park_default_is_semantic OK")


def main():
    tests = [
        test_batch_second_frame_reuses_cache_and_skips_full_calibration,
        test_recalibrate_forces_full_calibration_every_frame,
        test_cache_key_mismatch_forces_fresh_calibration,
        test_exception_after_calibration_clears_cache,
        test_dual_scan_clears_any_plain_cache,
        test_cli_park_default_is_semantic,
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
