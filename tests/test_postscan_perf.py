#!/usr/bin/env python3
"""Offline tests for the faster post-scan image code (no hardware).

The faster image code must write the SAME pixels and bytes as before:
`image.remove_dust` / `image._box_blur` are compared bit for bit against
frozen copies of the original implementation (kept below as the
reference), and `image.write_tiff16` against SHA-256 digests taken from the
original writer.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_postscan_perf.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import image

# ----------------------------------------------------------------------
# Frozen reference: the dust removal exactly as it was before the speed-up.


def _ref_box_blur(a, box):
    h, w = a.shape
    box_y, box_x = (box, box) if isinstance(box, int) else box
    ap = np.pad(a, ((box_y // 2, box_y - box_y // 2),
                    (box_x // 2, box_x - box_x // 2)), mode="edge").astype(np.float64)
    np.cumsum(ap, axis=0, out=ap)
    np.cumsum(ap, axis=1, out=ap)
    csum = np.pad(ap, ((1, 0), (1, 0)))
    y0 = np.arange(h)
    x0 = np.arange(w)
    y1 = y0 + box_y
    x1 = x0 + box_x
    total = csum[y1][:, x1] - csum[y0][:, x1] - csum[y1][:, x0] + csum[y0][:, x0]
    return (total / (box_y * box_x)).astype(np.float32)


def _ref_dust_mask(ir, sensitivity=1.0, *, dpi=3600):
    ir_f = np.asarray(ir, dtype=np.float32)
    bg_box = image._dust_scale(image._DUST_BG_BOX, dpi)
    bg = _ref_box_blur(ir_f, bg_box)
    border = bg < (image._DUST_BORDER_FRAC * float(np.median(bg)))
    border = _ref_box_blur(border.astype(np.float32), (bg_box[0] + 1, bg_box[1] + 1)) > 0
    t = min(image._DUST_T0 / max(sensitivity, 1e-6), 0.9)
    mask = (ir_f < bg * (1.0 - t)) & ~border
    return image._dilate_bool(mask, iterations=image._dust_scale(6, dpi))


# Master's constants, copied as literals (not read from the module under test).
_REF_KERNELS = (5, 17, 49)
_REF_MIN_WEIGHT = 4.0


def _ref_inpaint_channel(chan, mask, kernels):
    img = chan.astype(np.float32, copy=True)
    weight = (~mask).astype(np.float32)
    masked_vals = img * weight
    filled = img.copy()
    unresolved = mask.copy()
    for k in kernels:
        if not unresolved.any():
            break
        num = _ref_box_blur(masked_vals, k)
        den = _ref_box_blur(weight, k)
        area = k * k if isinstance(k, int) else k[0] * k[1]
        good = unresolved & (den * area > _REF_MIN_WEIGHT)
        if good.any():
            with np.errstate(divide="ignore", invalid="ignore"):
                filled[good] = num[good] / den[good]
            unresolved &= ~good
    if unresolved.any():
        fallback = float(img[~mask].mean()) if (~mask).any() else 0.0
        filled[unresolved] = fallback
    return filled


def _ref_remove_dust(visible, ir, sensitivity=1.0, *, dpi=3600):
    mask = _ref_dust_mask(ir, sensitivity=sensitivity, dpi=dpi)
    if not mask.any():
        return visible.copy()
    dtype = visible.dtype
    info = np.iinfo(dtype) if np.issubdtype(dtype, np.integer) else None
    out = np.empty_like(visible)
    kernels = tuple(image._dust_scale(k, dpi) for k in _REF_KERNELS)
    for c in range(visible.shape[2]):
        filled = _ref_inpaint_channel(visible[..., c], mask, kernels)
        if info is not None:
            filled = np.clip(np.rint(filled), info.min, info.max)
        out[..., c] = filled.astype(dtype)
    return out


def _scene(h, w, seed, specks=True, blob=False):
    """A smooth 16-bit RGB frame, its IR twin with dark specks (and
    optionally one speck bigger than the largest inpaint kernel)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 20000 + 12000 * np.sin(yy / 17.0) * np.cos(xx / 23.0)
    vis = np.stack([base + 1500 * c + rng.normal(0, 40, (h, w)) for c in range(3)], axis=-1)
    vis = np.clip(vis, 0, 65535).astype(np.uint16)
    ir = np.clip(40000 + rng.normal(0, 30, (h, w)), 0, 65535).astype(np.uint16)
    if specks:
        for _ in range(25):
            y, x = int(rng.integers(0, h)), int(rng.integers(0, w))
            r = int(rng.integers(1, 4))
            ir[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1] = 6000
    if blob:
        ir[h // 4:h // 4 + 130, w // 4:w // 4 + 130] = 2000
    return vis, ir


# ----------------------------------------------------------------------
# (1) pixel / byte identity


def test_box_blur_matches_the_reference_bit_for_bit():
    rng = np.random.default_rng(3)
    for shape in ((40, 33), (7, 90), (64, 5)):
        for dtype in (np.float32, np.uint16):
            a = (rng.random(shape) * 60000).astype(dtype)
            for box in (1, 4, 5, 17, 49, (3, 8), (64, 64), (130, 7)):
                got = image._box_blur(a, box)
                want = _ref_box_blur(a, box)
                assert got.dtype == want.dtype == np.float32
                assert got.tobytes() == want.tobytes(), (shape, dtype, box)
    print("test_box_blur_matches_the_reference_bit_for_bit OK")


def test_remove_dust_is_bit_identical_to_the_reference():
    cases = (
        (3600, 150, 170, 1, dict()),
        (600, 120, 90, 2, dict()),
        (2400, 140, 150, 3, dict()),                 # anisotropic profile
        (7200, 300, 260, 4, dict()),
        (600, 160, 160, 5, dict(blob=True)),         # speck wider than every kernel
        (3600, 90, 80, 6, dict(specks=False)),       # empty mask: returns a copy
    )
    for dpi, h, w, seed, kw in cases:
        vis, ir = _scene(h, w, seed, **kw)
        want = _ref_remove_dust(vis, ir, dpi=dpi)
        got = image.remove_dust(vis, ir, dpi=dpi)
        assert got.dtype == want.dtype and got.shape == want.shape
        assert np.array_equal(got, want), (dpi, kw)
        assert got is not vis
    # the mask itself too
    vis, ir = _scene(150, 170, 1)
    assert np.array_equal(image.dust_mask(ir, dpi=3600), _ref_dust_mask(ir, dpi=3600))
    print("test_remove_dust_is_bit_identical_to_the_reference OK")


def test_remove_dust_other_dtypes_match_the_reference():
    """Wider integer and float samples take the per-kernel-table path."""
    vis, ir = _scene(120, 110, 8)
    for dtype in (np.int32, np.uint8, np.float32):
        v = (vis // 256 if dtype is np.uint8 else vis).astype(dtype)
        want = _ref_remove_dust(v, ir, dpi=3600)
        got = image.remove_dust(v, ir, dpi=3600)
        assert got.dtype == want.dtype and np.array_equal(got, want), dtype
    print("test_remove_dust_other_dtypes_match_the_reference OK")


def test_inpaint_kernel_selection_is_pinned_to_the_old_constants():
    assert tuple(image._INPAINT_KERNELS) == _REF_KERNELS
    assert image._INPAINT_MIN_WEIGHT == _REF_MIN_WEIGHT
    print("test_inpaint_kernel_selection_is_pinned_to_the_old_constants OK")


def test_remove_dust_actually_changes_something():
    """Guard against a vacuous identity test: the fixtures have specks."""
    vis, ir = _scene(150, 170, 1)
    assert image.dust_mask(ir, dpi=3600).any()
    assert not np.array_equal(image.remove_dust(vis, ir, dpi=3600), vis)
    print("test_remove_dust_actually_changes_something OK")


def test_remove_dust_does_not_touch_its_input():
    vis, ir = _scene(100, 100, 9)
    v0, i0 = vis.copy(), ir.copy()
    image.remove_dust(vis, ir, dpi=3600)
    assert np.array_equal(vis, v0) and np.array_equal(ir, i0)
    print("test_remove_dust_does_not_touch_its_input OK")


# SHA-256 of the file the ORIGINAL writer produced for this array.
_TIFF_GOLDEN = {
    "plain": "4c88a2f7cbb74db3edf2ddc2e83738578a6778f4b07a80bf4fa0f3c957b431fa",
    "dpi": "b501507a8482168dbaffd2e527395f176cfe5534e786f19b60df6a6a9d03c618",
    "aniso": "b1b6aac159935f13c0f5a4455496b80b5e6d1b0cac1f4d3db0a0867dd797d1a4",
    "icc": "baaf7e3e6a5b876c0ae1228921d79e8ce94a7faaef50e39caec89e3a1d846dbe",
    "be": "5e67fc56937930fb138c0ed4083b02c6bc21971847be61d301a260a3ddb282a2",
    "noncontig": "acea74da6cdcb5529656e9a71a55265819c15b07514f9a660c4d2d73910d2d4f",
    # zero-size arrays: an empty strip, still a valid file
    "empty_h": "0d05fe7cd055155bb948a68d80fae30c2e49d5a9c6f4e522f85fa6ba94460aad",
    "empty_h_icc": "252164382e014251b262cafd8668aa85288f4f018d47847db0dc87ee3232815f",
    "empty_w": "d3fecb4875e0ea1c0ef76c4712c428dc6c1854ae115ed135005fb70ebbd63458",
    "empty_w_icc": "44225761e7a1ef81fdc3f2f47b3f8690457385be9a1d04aa8913b983a6a1ef68",
    "empty_hw": "a444ba6daa7100e000e6d17a6edbd16da6637374e1b5f6d531fe5bdbe79f6371",
    "empty_hw_icc": "fa34089e2b25168171714c6599875638ae0983e26afcda171ac3ca3ed03540b1",
}


def test_write_tiff16_bytes_are_unchanged():
    import hashlib
    import os
    import tempfile
    a = np.random.default_rng(7).integers(0, 65536, (7, 5, 3), dtype=np.uint16)
    cases = {
        "plain": (a, {}),
        "dpi": (a, dict(dpi=3600)),
        "aniso": (a, dict(dpi=(3600, 2400))),
        "icc": (a, dict(icc=image.srgb_icc(), dpi=7200)),
        "be": (np.ascontiguousarray(a).astype(">u2"), dict(dpi=600)),
        "noncontig": (a[::-1], dict(dpi=600)),
    }
    for tag, shape in (("h", (0, 5, 3)), ("w", (4, 0, 3)), ("hw", (0, 0, 3))):
        z = np.zeros(shape, np.uint16)
        cases["empty_" + tag] = (z, {})
        cases["empty_" + tag + "_icc"] = (z, dict(dpi=3600, icc=image.srgb_icc()))
    with tempfile.TemporaryDirectory() as d:
        for name, (arr, kw) in cases.items():
            p = os.path.join(d, name + ".tiff")
            image.write_tiff16(arr, p, **kw)
            got = hashlib.sha256(Path(p).read_bytes()).hexdigest()
            assert got == _TIFF_GOLDEN[name], name
    print("test_write_tiff16_bytes_are_unchanged OK")


def test_align_channels_leaves_its_input_alone():
    rng = np.random.default_rng(2)
    raw = rng.integers(0, 65536, (200, 30, 3), dtype=np.uint16)
    view = np.ascontiguousarray(raw)[::1]
    before = view.copy()
    out = image.align_channels(view, dpi=3600)
    assert np.array_equal(view, before)
    s = image.align_shift(3600)
    assert np.array_equal(out[..., 1], before[s:-s, :, 1])
    assert np.array_equal(out[..., 0], np.roll(before[..., 0], -s, axis=0)[s:-s])
    assert np.array_equal(out[..., 2], np.roll(before[..., 2], +s, axis=0)[s:-s])
    # a non-contiguous (every-other-line) input, as split_ir hands it over
    strided = rng.integers(0, 65536, (400, 30, 3), dtype=np.uint16)[1::2]
    keep = strided.copy()
    out2 = image.align_channels(strided, dpi=3600)
    assert np.array_equal(strided, keep) and out2.flags["C_CONTIGUOUS"]
    print("test_align_channels_leaves_its_input_alone OK")


def main():
    tests = [
        test_box_blur_matches_the_reference_bit_for_bit,
        test_remove_dust_is_bit_identical_to_the_reference,
        test_remove_dust_other_dtypes_match_the_reference,
        test_inpaint_kernel_selection_is_pinned_to_the_old_constants,
        test_remove_dust_actually_changes_something,
        test_remove_dust_does_not_touch_its_input,
        test_write_tiff16_bytes_are_unchanged,
        test_align_channels_leaves_its_input_alone,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
