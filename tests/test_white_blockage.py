#!/usr/bin/env python3
"""Offline tests for the white-measurement blockage guard
(calibrate.find_white_blockage / guard_white_blockage) and the clean
reference store (of135i.white_reference). Test 107: a 110 strip reaching
into the holder end at aperture 1 blocked the white measurement over the
film band's columns, and visible R and IR clipped in the photographs."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import calibrate, white_reference  # noqa: E402

W = 5184
LIT = (40, 5150)


def _profile(seed=0, scale=1.0, tilt=0.0):
    """A lamp-like white profile (width, 3): lit window, a smooth hump,
    the sensor's fixed per-column structure (0.4 %) and noise."""
    rng = np.random.default_rng(seed)
    x = np.linspace(-1.0, 1.0, W)
    hump = 1.0 - 0.18 * x ** 2 + tilt * x
    fixed = 1.0 + 0.004 * np.random.default_rng(99).standard_normal((W, 3))
    prof = np.zeros((W, 3))
    for c, level in enumerate((50000.0, 48000.0, 44000.0)):
        prof[:, c] = level * scale * hump * fixed[:, c]
    prof *= 1.0 + 0.001 * rng.standard_normal((W, 3))
    prof[:LIT[0]] = 300.0
    prof[LIT[1]:] = 300.0
    return prof


def _meas(prof, lines=128, seed=1):
    rng = np.random.default_rng(seed)
    m = prof[None, :, :] * (1.0 + 0.002 * rng.standard_normal((lines, W, 3)))
    return np.clip(np.rint(m), 0, 65535).astype(np.uint16)


def test_clean_measurement_is_untouched():
    m = _meas(_profile())
    out, blocks = calibrate.guard_white_blockage(m, None)
    assert blocks == [] and out is m
    # a dust speck (30 columns, 9 %) is the dip guard's, not a blockage
    p = _profile()
    p[3000:3030] *= 0.91
    assert calibrate.find_white_blockage(p) == []
    print("test_clean_measurement_is_untouched OK")


def test_film_band_is_found_and_filled_from_the_reference():
    truth = _profile(seed=2)
    blocked = truth.copy()
    blocked[1174:3418] *= 0.025                      # the 16 mm band, Test 107
    regions = calibrate.find_white_blockage(blocked)
    assert len(regions) == 1, regions
    s, e = regions[0]
    assert 1174 - 40 <= s <= 1174 and 3417 <= e <= 3417 + 40, regions
    ref = _profile(seed=3, scale=0.93, tilt=0.03)    # another session: other level, a tilt
    m = _meas(blocked, seed=4)
    out, blocks = calibrate.guard_white_blockage(m, ref)
    assert len(blocks) == 1 and blocks[0].source == "reference", blocks
    assert blocks[0].depth < 0.05, blocks
    got = out.astype(np.float64).mean(axis=0)[1100:3500]
    err = got / truth[1100:3500] - 1.0
    assert np.abs(err).max() < 0.01, np.abs(err).max()
    # the clear columns are not changed
    assert np.array_equal(out[:, :1100], m[:, :1100]) and np.array_equal(out[:, 3500:], m[:, 3500:])
    print("test_film_band_is_found_and_filled_from_the_reference OK")


def test_without_reference_the_band_is_interpolated():
    truth = _profile(seed=5)
    blocked = truth.copy()
    blocked[1500:3700] *= 0.3                        # clear base, not leader: still a blockage
    out, blocks = calibrate.guard_white_blockage(_meas(blocked, seed=6), None)
    assert len(blocks) == 1 and blocks[0].source == "interpolation", blocks
    got = out.astype(np.float64).mean(axis=0)[1500:3700]
    err = got / truth[1500:3700] - 1.0
    assert np.abs(err).max() < 0.06, np.abs(err).max()     # the hump's top is missed, no clipping
    print("test_without_reference_the_band_is_interpolated OK")


def test_partly_transmitting_film_edge_is_included():
    """The film edge passes part of the light (IR ~0.9 over ~150 columns
    beside the dense band, Test 107): the region grows over it."""
    truth = _profile(seed=7)
    blocked = truth.copy()
    blocked[1300:3300] *= 0.02
    blocked[1150:1300] *= 0.88
    blocked[3300:3420] *= 0.90
    ref = _profile(seed=8, scale=1.05)
    out, blocks = calibrate.guard_white_blockage(_meas(blocked, seed=9), ref)
    assert len(blocks) == 1 and blocks[0].start <= 1150 and blocks[0].stop >= 3419, blocks
    got = out.astype(np.float64).mean(axis=0)[1100:3480]
    err = got / truth[1100:3480] - 1.0
    assert np.abs(err).max() < 0.015, np.abs(err).max()
    print("test_partly_transmitting_film_edge_is_included OK")


def test_store_round_trip_and_update_rule():
    with tempfile.TemporaryDirectory() as d:
        store = white_reference.WhiteReferenceStore(d)
        key = white_reference.kind_key("dual-vis", 3600, W)
        assert key == "dual-vis-3600-5184"
        assert store.load(key) is None
        clean = _meas(_profile(seed=10))
        out, blocks = white_reference.guard(store, key, clean)
        assert blocks == [] and out is clean
        saved = store.load(key)
        assert saved is not None and saved.shape == (W, 3)
        assert np.allclose(saved, clean.astype(np.float64).mean(axis=0), rtol=1e-6)
        # a blocked measurement is filled from it and never replaces it
        b = _profile(seed=11)
        b[1200:3400] *= 0.02
        out, blocks = white_reference.guard(store, key, _meas(b, seed=12))
        assert blocks and blocks[0].source == "reference", blocks
        assert np.array_equal(store.load(key), saved)
        # no stray temporary files, a damaged file is no reference
        assert sorted(os.listdir(d)) == [f"white-{key}.npy"], os.listdir(d)
        Path(d, f"white-{key}.npy").write_bytes(b"not numpy")
        assert store.load(key) is None
        # a store that cannot be written never raises
        bad = white_reference.WhiteReferenceStore(Path(d, f"white-{key}.npy", "sub"))
        assert bad.save(key, saved) is False
    print("test_store_round_trip_and_update_rule OK")


def test_state_directory_follows_the_environment():
    old = {k: os.environ.get(k) for k in ("OF135I_STATE_DIR", "XDG_STATE_HOME")}
    try:
        os.environ["OF135I_STATE_DIR"] = "/x/y"
        assert white_reference.default_dir() == Path("/x/y")
        del os.environ["OF135I_STATE_DIR"]
        os.environ["XDG_STATE_HOME"] = "/s"
        assert white_reference.default_dir() == Path("/s/of135i")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("test_state_directory_follows_the_environment OK")


def main() -> int:
    tests = [
        test_clean_measurement_is_untouched,
        test_film_band_is_found_and_filled_from_the_reference,
        test_without_reference_the_band_is_interpolated,
        test_partly_transmitting_film_edge_is_included,
        test_store_round_trip_and_update_rule,
        test_state_directory_follows_the_environment,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
