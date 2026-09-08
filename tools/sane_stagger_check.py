#!/usr/bin/env python3
"""Empirically decide the direction of the SANE ComponentShiftLines fix
for the OpticFilm 135i's colour-line stagger (docs/sane-hook5-frame.md
open question, Test 53).

Background: the sensor reads R, G, B on physically separate CCD lines.
The Python driver corrects this in of135i/image.py::align_channels by
rolling R by -12 and B by +12 (at 3600 dpi) and cropping 12 rows off
each end -- algebraically: out[k] = (R[k+24], G[k+12], B[k]).

The SANE core's ImagePipelineNodeComponentShiftLines node computes:
    out[k, c] = in[k + shift_c, c],   out height = in height - max(shift)
The model declared shift = (r=0, g=12, b=24) until 2026-09-08
("candidate A"); the driver's algebra implies shift = (r=24, g=12, b=0)
("candidate B"), which this script confirmed and the model now declares. This script measures the raw stagger directly on SANE
PNM captures, applies both candidates, shows which one collapses the
stagger to ~0 and is byte-identical to the driver's own correction, and
renders review crops.

Memory discipline (systemd-oomd has killed sessions before on this
machine): every full-frame array stays uint16 (never float64); all
correlation/registration math runs on small, explicitly-bounded bands
in int32/float32. Run under a memory cap:

    systemd-run --user --scope -p MemoryMax=3G \
        .venv/bin/python tools/sane_stagger_check.py
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from of135i import image as of135i_image  # noqa: E402  (path set up above)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

CANDIDATE_A = (0, 12, 24)   # currently declared model (ld_shift r/g/b)
CANDIDATE_B = (24, 12, 0)   # implied by the driver's align_channels algebra

# Central band used for stagger/registration measurement (rows, then a
# column slice subsampled by 4 as specified in the task).
BAND_ROWS = slice(500, 4500)
BAND_COLS = slice(800, 3000)
COL_SUBSAMPLE = 4

MAX_RAW_SHIFT = 40      # search range for the raw-stagger measurement
MAX_RESIDUAL_SHIFT = 40  # search range for the post-correction residual
MAX_REG = 8              # +/- rows/cols for SANE-vs-reference registration

CROP_SIZE = 700


# --------------------------------------------------------------------------
# File readers (memory-mapped, no full-frame float copies)
# --------------------------------------------------------------------------

def read_pnm16(path: Path) -> np.memmap:
    """Memory-map a binary PPM (P6, maxval 65535) as (H, W, 3) uint16,
    big-endian (the PNM spec's mandated sample order -- see
    of135i/image.py::write_pnm16). Tolerates a single '#' comment line
    after the magic, as scanimage emits ("# SANE data follows")."""
    with open(path, "rb") as f:
        header = f.read(256)
    if not header.startswith(b"P6"):
        raise ValueError(f"{path}: not a binary PPM (P6)")
    pos = 2
    tokens = []
    while len(tokens) < 3:
        # skip whitespace
        while header[pos:pos + 1].isspace():
            pos += 1
        if header[pos:pos + 1] == b"#":
            nl = header.index(b"\n", pos)
            pos = nl + 1
            continue
        start = pos
        while not header[pos:pos + 1].isspace():
            pos += 1
        tokens.append(int(header[start:pos]))
    pos += 1  # single whitespace byte after maxval, per PNM spec
    width, height, maxval = tokens
    if maxval != 65535:
        raise ValueError(f"{path}: expected maxval 65535, got {maxval}")
    arr = np.memmap(path, dtype=">u2", mode="r", offset=pos,
                     shape=(height, width, 3))
    return arr


def read_tiff16(path: Path) -> np.memmap:
    """Memory-map the driver's own minimal 16-bit RGB TIFF (see
    of135i/image.py::write_tiff16) as (H, W, 3) uint16, little-endian.

    NOTE: PIL/Pillow (12.3.0 here) parses this file's tags correctly
    (BitsPerSample=16,16,16 etc, confirmed via tag_v2) but its "RGB;16L"
    raw-tile unpacker silently DOWNCONVERTS to 8-bit on load -- there is
    no native Pillow image mode for 16-bit multi-channel data, so
    np.array(Image.open(...)) on this file returns uint8 (values 0-171
    instead of the true 16-bit range). This was verified directly on
    ref7-f1.tiff before writing this reader. The task asked for a PIL
    load; that would silently corrupt the comparison, so this function
    instead parses the (few, known) TIFF tags directly -- exactly the
    single-strip, uncompressed, 16-bit-LE layout write_tiff16 always
    produces -- and memory-maps the pixel data. This is the one
    deliberate deviation from the brief; flagged here and in the report.
    """
    with open(path, "rb") as f:
        head = f.read(8)
        byteorder, magic, ifd_off = struct.unpack_from("<2sHI", head, 0)
        if byteorder != b"II" or magic != 42:
            raise ValueError(f"{path}: not a classic little-endian TIFF")
        f.seek(ifd_off)
        n_entries = struct.unpack("<H", f.read(2))[0]
        tags = {}
        for _ in range(n_entries):
            tag, typ, cnt, val = struct.unpack("<HHII", f.read(12))
            tags[tag] = (typ, cnt, val)

    width = tags[256][2]
    height = tags[257][2]
    bits_off = tags[258][2]
    strip_offset = tags[273][2]
    photometric = tags[262][2]
    samples_per_pixel = tags[277][2]
    compression = tags[259][2]

    with open(path, "rb") as f:
        f.seek(bits_off)
        bits = struct.unpack("<HHH", f.read(6))

    if compression != 1:
        raise ValueError(f"{path}: expected uncompressed TIFF, got compression={compression}")
    if photometric != 2 or samples_per_pixel != 3:
        raise ValueError(f"{path}: expected RGB/3-samples-per-pixel TIFF")
    if bits != (16, 16, 16):
        raise ValueError(f"{path}: expected 16/16/16 bits-per-sample, got {bits}")

    arr = np.memmap(path, dtype="<u2", mode="r", offset=strip_offset,
                     shape=(height, width, 3))
    return arr


# --------------------------------------------------------------------------
# Small-band correlation helpers (int32/float32 only, never full-frame)
# --------------------------------------------------------------------------

def pearson(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation of two equal-shape small arrays. Sums are
    accumulated in float64 (scalars only -- cheap), the arrays
    themselves stay float32."""
    af = a.ravel().astype(np.float32)
    bf = b.ravel().astype(np.float32)
    af = af - af.mean()
    bf = bf - bf.mean()
    denom = np.sqrt((af * af).sum(dtype=np.float64)) * np.sqrt((bf * bf).sum(dtype=np.float64))
    if denom <= 0:
        return 0.0
    return float((af * bf).sum(dtype=np.float64) / denom)


def best_row_shift(ref_2d: np.ndarray, other_2d: np.ndarray, max_shift: int):
    """Find the integer row shift s in [-max_shift, max_shift] maximising
    the Pearson correlation of other_2d[s + k] against ref_2d[k].

    Sign convention: s > 0 means the content that sits at row k in
    ref_2d sits at row (k + s) in other_2d -- i.e. "other's content
    appears s rows LATER (larger row index) than ref's". Returns
    (best_s, best_corr, all_scores).
    """
    H = min(ref_2d.shape[0], other_2d.shape[0])
    ref_2d = ref_2d[:H]
    other_2d = other_2d[:H]
    scores = []
    for s in range(-max_shift, max_shift + 1):
        if s >= 0:
            o = other_2d[s:H]
            r = ref_2d[0:H - s]
        else:
            o = other_2d[0:H + s]
            r = ref_2d[-s:H]
        scores.append((s, pearson(o, r)))
    best_s, best_c = max(scores, key=lambda t: t[1])
    return best_s, best_c, scores


def row_gradient(band_2d: np.ndarray) -> np.ndarray:
    """Vertical (row-to-row) gradient, int32, of a small 2D band."""
    return np.diff(band_2d.astype(np.int32), axis=0)


def grad_energy_map_channel(chan_2d: np.ndarray) -> np.ndarray:
    """Combined |d/drow| + |d/dcol| gradient magnitude of a small 2D
    band, same shape as input (edge-padded), int32."""
    a = chan_2d.astype(np.int32)
    gr = np.abs(np.diff(a, axis=0, prepend=a[:1]))
    gc = np.abs(np.diff(a, axis=1, prepend=a[:, :1]))
    return gr + gc


def best_registration(ref_full: np.ndarray, row0: int, row1: int, col0: int, col1: int,
                       stride_row: int, stride_col: int, cand_band: np.ndarray, max_reg: int):
    """2D registration search: find (dy, dx) in [-max_reg, max_reg]^2 (full-
    resolution row/col units) maximising Pearson correlation of
    gradient-magnitude maps between cand_band (already sliced+subsampled
    at [row0:row1:stride_row, col0:col1:stride_col] with dy=dx=0) and a
    same-stride slice of ref_full re-sliced at [row0+dy:row1+dy:stride_row,
    col0+dx:col1+dx:stride_col] for every trial offset -- so the stride
    and the search offset never get conflated (each trial slices the
    full-resolution array fresh, at full-resolution precision, then
    subsamples). ref_full must have at least max_reg rows/cols of margin
    on every side of [row0:row1, col0:col1]. Returns (best_dy, best_dx,
    best_corr).
    """
    ch, cw = cand_band.shape
    cand_g = grad_energy_map_channel(cand_band).astype(np.float32)
    best = (0, 0, -2.0)
    for dy in range(-max_reg, max_reg + 1):
        for dx in range(-max_reg, max_reg + 1):
            ref_sub = ref_full[row0 + dy: row1 + dy: stride_row,
                                col0 + dx: col1 + dx: stride_col]
            rh, rw = min(ch, ref_sub.shape[0]), min(cw, ref_sub.shape[1])
            ref_g = grad_energy_map_channel(ref_sub[:rh, :rw]).astype(np.float32)
            c = pearson(cand_g[:rh, :rw], ref_g)
            if c > best[2]:
                best = (dy, dx, c)
    return best


# --------------------------------------------------------------------------
# Candidate application (pure slicing -- the node's exact semantics)
# --------------------------------------------------------------------------

def apply_shift_candidate(raw: np.ndarray, shift_rgb: tuple[int, int, int]) -> np.ndarray:
    """out[k, c] = in[k + shift_c, c]; out height = in height - max(shift).
    Implemented as pure slicing (no wrap), matching
    ImagePipelineNodeComponentShiftLines exactly."""
    sr, sg, sb = shift_rgb
    h_out = raw.shape[0] - max(shift_rgb)
    return np.stack([
        raw[sr:sr + h_out, :, 0],
        raw[sg:sg + h_out, :, 1],
        raw[sb:sb + h_out, :, 2],
    ], axis=-1)


# --------------------------------------------------------------------------
# Synthetic sanity test (no files touched)
# --------------------------------------------------------------------------

def synthetic_test(report: list[str]) -> bool:
    report.append("")
    report.append("=== Synthetic sanity test ===")
    H, W, EDGE = 200, 64, 100
    HIGH, LOW = 50000, 1000

    def scene(i: np.ndarray) -> np.ndarray:
        out = np.where(i >= EDGE, HIGH, LOW).astype(np.uint16)
        out = np.where(i < 0, 0, out)  # zero-fill before the start of the scene
        return out

    k = np.arange(H)
    # Sensor construction per the task: R sees content 24 rows "later"
    # (needs to look further back, i.e. R[k] holds the scene value that
    # was at physical row k-24), G at k-12, B at k (undelayed reference).
    r_row = scene(k - 24)
    g_row = scene(k - 12)
    b_row = scene(k)

    raw = np.zeros((H, W, 3), dtype=np.uint16)
    raw[..., 0] = r_row[:, None]
    raw[..., 1] = g_row[:, None]
    raw[..., 2] = b_row[:, None]

    def edge_row(channel_1d: np.ndarray) -> int:
        idx = np.nonzero(channel_1d >= (HIGH + LOW) // 2)[0]
        return int(idx[0]) if len(idx) else -1

    raw_edges = tuple(edge_row(raw[:, 0, c]) for c in range(3))
    report.append(f"raw synthetic per-channel edge rows (R,G,B) = {raw_edges} "
                   f"(expected staggered: R latest, G mid, B earliest)")
    direction_as_expected = raw_edges[0] > raw_edges[1] > raw_edges[2]
    if not direction_as_expected:
        report.append("LOUD WARNING: synthetic construction did not stagger in the "
                       "expected R-latest/B-earliest direction -- re-check before trusting "
                       "the candidate verdict below.")

    out_b = apply_shift_candidate(raw, CANDIDATE_B)
    out_a = apply_shift_candidate(raw, CANDIDATE_A)
    h_out = H - 24

    edges_b = tuple(edge_row(out_b[:, 0, c]) for c in range(3))
    edges_a = tuple(edge_row(out_a[:, 0, c]) for c in range(3))
    report.append(f"candidate B (24,12,0) output edge rows (R,G,B) = {edges_b}")
    report.append(f"candidate A (0,12,24) output edge rows (R,G,B) = {edges_a}")

    b_fixes = len(set(edges_b)) == 1 and edges_b[0] != -1
    a_breaks = len(set(edges_a)) != 1

    # No-wrap check: top and bottom output rows must equal the exact
    # expected slice values, and must never come from the far end of
    # the input (which would indicate a roll/wrap instead of a crop).
    sr, sg, sb = CANDIDATE_B
    top_expected = (int(r_row[0 + sr]), int(g_row[0 + sg]), int(b_row[0 + sb]))
    bot_k = h_out - 1
    bot_expected = (int(r_row[bot_k + sr]), int(g_row[bot_k + sg]), int(b_row[bot_k + sb]))
    top_actual = tuple(int(v) for v in out_b[0, 0, :])
    bot_actual = tuple(int(v) for v in out_b[-1, 0, :])
    no_wrap = (top_actual == top_expected) and (bot_actual == bot_expected)
    report.append(f"candidate B top row: actual={top_actual} expected={top_expected}")
    report.append(f"candidate B bottom row: actual={bot_actual} expected={bot_expected}")
    report.append(f"no-wrap check: {'PASS' if no_wrap else 'FAIL'}")

    ok = direction_as_expected and b_fixes and a_breaks and no_wrap
    report.append(f"candidate B fixes the edge (all channels aligned): {'PASS' if b_fixes else 'FAIL'}")
    report.append(f"candidate A does NOT fix the edge: {'PASS' if a_breaks else 'FAIL (unexpected)'}")
    report.append(f"SYNTHETIC TEST: {'PASS' if ok else 'FAIL'}")
    return ok


# --------------------------------------------------------------------------
# PNG output helpers (8-bit review images only; full 16-bit data never
# leaves this process)
# --------------------------------------------------------------------------

def percentile_stretch(img_u16: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    out = np.empty(img_u16.shape, dtype=np.uint8)
    for c in range(3):
        scale = 255.0 / max(float(hi[c] - lo[c]), 1e-6)
        ch = img_u16[..., c].astype(np.float32)
        ch = np.clip((ch - float(lo[c])) * scale, 0, 255)
        out[..., c] = ch.astype(np.uint8)
    return out


def positive_params(sample_u16: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-channel parameters of the driver's preview inversion
    (of135i.image.to_positive), computed once on a small full-frame
    sample so that crops and the full frame invert identically: film base
    from the 99.8th percentile, density black/white points at 0.5/99.5 %."""
    px = sample_u16.astype(np.float64)
    base = np.array([np.percentile(px[..., c], 99.8) for c in range(3)])
    dens = np.log10(np.clip(base, 1, None) / np.clip(px, 1.0, None))
    lo = np.array([np.percentile(dens[..., c], 0.5) for c in range(3)])
    hi = np.array([np.percentile(dens[..., c], 99.5) for c in range(3)])
    return base, lo, hi


def apply_positive(img_u16: np.ndarray, params, gamma: float = 2.2) -> np.ndarray:
    """to_positive's math with fixed parameters -> 8-bit RGB for review.
    Small inputs only (crops, 1/8 frames): float64 per call."""
    base, lo, hi = params
    px = img_u16.astype(np.float64)
    out = np.empty(px.shape, dtype=np.uint8)
    for c in range(3):
        dens = np.log10(max(base[c], 1.0) / np.clip(px[..., c], 1.0, None))
        v = np.clip((dens - lo[c]) / max(hi[c] - lo[c], 1e-9), 0, 1)
        out[..., c] = (v ** (1.0 / gamma) * 255.0).astype(np.uint8)
    return out


def vendor_orientation(img: np.ndarray) -> np.ndarray:
    """The vendor apps' orientation (of135i/cli.py: mirrored sensor image,
    rotated): rot90 x3 then horizontal mirror."""
    return np.ascontiguousarray(np.rot90(img, 3)[:, ::-1])


def save_side_by_side(panels: list[np.ndarray], path: Path, gap: int = 8) -> None:
    h = panels[0].shape[0]
    gap_col = np.full((h, gap, 3), 255, dtype=np.uint8)
    pieces = []
    for i, p in enumerate(panels):
        pieces.append(p)
        if i != len(panels) - 1:
            pieces.append(gap_col)
    combined = np.concatenate(pieces, axis=1)
    Image.fromarray(combined, mode="RGB").save(path)


def find_high_contrast_crop(g_channel: np.ndarray, row_lo: int, row_hi: int,
                             col_lo: int, col_hi: int, crop: int) -> tuple[int, int]:
    """Grid-search a crop-sized window inside [row_lo,row_hi)x[col_lo,col_hi)
    with the highest gradient energy (subsampled 8x for speed), returning
    its (row0, col0)."""
    best = (row_lo, col_lo, -1.0)
    step = 40
    sub = 8
    for r0 in range(row_lo, row_hi - crop, step):
        for c0 in range(col_lo, col_hi - crop, step):
            block = g_channel[r0:r0 + crop:sub, c0:c0 + crop:sub]
            e = grad_energy_map_channel(block).astype(np.float64).sum()
            if e > best[2]:
                best = (r0, c0, e)
    return best[0], best[1]


# --------------------------------------------------------------------------
# Per-frame processing
# --------------------------------------------------------------------------

def process_frame(frame_no: int, analysis_dir: Path, out_dir: Path,
                   report: list[str]) -> None:
    t0 = time.time()
    report.append("")
    report.append(f"=== Frame {frame_no} ===")

    sane_path = analysis_dir / f"frame{frame_no}-sane.pnm"
    ref_names = {1: "ref7-f1.tiff", 2: "ref-f2.tiff", 4: "ref-f4.tiff"}
    ref_path = analysis_dir / ref_names[frame_no]

    sane_mm = read_pnm16(sane_path)
    H, W, _ = sane_mm.shape
    report.append(f"SANE PNM: {sane_path.name}  shape={sane_mm.shape}")

    # One real (non-mmap) uint16 copy of the raw frame -- explicitly
    # allowed by the memory rule (uint16 full-frame is fine; float64
    # full-frame is not). ~116 MB.
    raw = np.array(sane_mm)
    del sane_mm

    ref_mm = read_tiff16(ref_path)
    ref = np.array(ref_mm)
    del ref_mm
    report.append(f"Reference TIFF: {ref_path.name}  shape={ref.shape}  "
                   f"(driver-corrected, {H} - 32 rows short of raw: 24 stagger crop "
                   f"+ 8 fewer chunks read)")

    # ---- Step 2: measure raw stagger directly ---------------------------
    def band(channel_idx: int) -> np.ndarray:
        return raw[BAND_ROWS, BAND_COLS.start:BAND_COLS.stop:COL_SUBSAMPLE, channel_idx]

    g_grad = row_gradient(band(1))
    r_grad = row_gradient(band(0))
    b_grad = row_gradient(band(2))

    shift_r_vs_g, corr_r, _ = best_row_shift(g_grad, r_grad, MAX_RAW_SHIFT)
    shift_b_vs_g, corr_b, _ = best_row_shift(g_grad, b_grad, MAX_RAW_SHIFT)
    report.append(f"Sign convention: shift = s means the *other* channel's content "
                   f"appears s rows LATER (larger row index) than the reference "
                   f"channel's (s<0 = earlier).")
    report.append(f"Measured raw stagger: shift_R_vs_G = {shift_r_vs_g:+d} rows "
                   f"(corr={corr_r:.4f}) -> R content appears "
                   f"{'LATER' if shift_r_vs_g > 0 else 'EARLIER' if shift_r_vs_g < 0 else 'at the same row'} "
                   f"than G")
    report.append(f"Measured raw stagger: shift_B_vs_G = {shift_b_vs_g:+d} rows "
                   f"(corr={corr_b:.4f}) -> B content appears "
                   f"{'LATER' if shift_b_vs_g > 0 else 'EARLIER' if shift_b_vs_g < 0 else 'at the same row'} "
                   f"than G")
    del g_grad, r_grad, b_grad

    # ---- Step 3: apply both candidates, compare to align_channels -------
    cand_a = apply_shift_candidate(raw, CANDIDATE_A)
    cand_b = apply_shift_candidate(raw, CANDIDATE_B)
    driver_corrected = of135i_image.align_channels(raw, dpi=3600)

    eq_a = np.array_equal(driver_corrected, cand_a)
    eq_b = np.array_equal(driver_corrected, cand_b)
    report.append(f"align_channels() output byte-identical to candidate A (0,12,24): {eq_a}")
    report.append(f"align_channels() output byte-identical to candidate B (24,12,0): {eq_b}")
    if eq_a == eq_b:
        report.append("ASSERTION FAILURE: align_channels output must match exactly one "
                       "candidate, not zero or both.")
        raise AssertionError("align_channels output did not match exactly one candidate")
    correct_label = "A" if eq_a else "B"
    correct_shift = CANDIDATE_A if eq_a else CANDIDATE_B
    report.append(f"=> Candidate {correct_label} {correct_shift} reproduces the driver's "
                   f"own correction exactly (wrap-free).")

    # ---- Step 4: residual stagger after correction, both candidates -----
    def residual(cand_arr: np.ndarray, label: str) -> tuple[int, int]:
        def cband(channel_idx: int) -> np.ndarray:
            return cand_arr[BAND_ROWS, BAND_COLS.start:BAND_COLS.stop:COL_SUBSAMPLE, channel_idx]
        gg = row_gradient(cband(1))
        rg = row_gradient(cband(0))
        bg = row_gradient(cband(2))
        s_r, c_r, _ = best_row_shift(gg, rg, MAX_RESIDUAL_SHIFT)
        s_b, c_b, _ = best_row_shift(gg, bg, MAX_RESIDUAL_SHIFT)
        report.append(f"Residual after candidate {label}: shift_R_vs_G = {s_r:+d} "
                       f"(corr={c_r:.4f}), shift_B_vs_G = {s_b:+d} (corr={c_b:.4f})")
        return s_r, s_b

    residual_a = residual(cand_a, "A")
    residual_b = residual(cand_b, "B")

    corrected = cand_b if correct_label == "B" else cand_a
    wrong = cand_a if correct_label == "B" else cand_b
    del cand_a, cand_b
    if correct_label == "B":
        report.append(f"Correct candidate B residual = {residual_b} (expect ~0,0); "
                       f"wrong candidate A residual = {residual_a} (expect roughly doubled stagger)")
    else:
        report.append(f"Correct candidate A residual = {residual_a} (expect ~0,0); "
                       f"wrong candidate B residual = {residual_b} (expect roughly doubled stagger)")
    del wrong

    # ---- Step 5: register corrected image against the driver reference --
    reg_row0, reg_row1 = 600, 4600
    reg_col0, reg_col1 = 600, 2900
    stride_row, stride_col = 2, 4
    cand_band_g = corrected[reg_row0:reg_row1:stride_row, reg_col0:reg_col1:stride_col, 1]
    dy, dx, reg_corr = best_registration(
        ref[:, :, 1], reg_row0, reg_row1, reg_col0, reg_col1,
        stride_row, stride_col, cand_band_g, MAX_REG)
    report.append(f"Registration (corrected vs reference), G-channel gradient search: "
                   f"dy={dy} dx={dx} rows/cols (full-resolution units; reference row "
                   f"reg_row0+dy aligns with corrected row reg_row0) corr={reg_corr:.4f}")

    # Per-channel correlation on raw pixel values at that registration,
    # over a slightly generous central band, clipped to what's in-bounds
    # in both images given dy (full resolution, so dy/dx map 1:1 here --
    # the search above only subsampled for speed, the offset itself is
    # in full-res rows/cols since MAX_REG=8 is the same unit both ways).
    corr_row0 = max(reg_row0, reg_row0 - dy)
    corr_row1 = min(reg_row1, reg_row1 - dy)
    corr_col0 = max(reg_col0, reg_col0 - dx)
    corr_col1 = min(reg_col1, reg_col1 - dx)
    corrected_band = corrected[corr_row0:corr_row1:4, corr_col0:corr_col1:4, :]
    ref_band_full = ref[corr_row0 + dy:corr_row1 + dy:4, corr_col0 + dx:corr_col1 + dx:4, :]

    chan_names = ("R", "G", "B")
    corrs_after = {}
    for c, name in enumerate(chan_names):
        corrs_after[name] = pearson(corrected_band[..., c], ref_band_full[..., c])
    report.append(f"Per-channel Pearson correlation, corrected vs reference: "
                   f"R={corrs_after['R']:.4f} G={corrs_after['G']:.4f} B={corrs_after['B']:.4f}")

    # Per-channel residual shift vs reference (should be ~0 once the
    # global registration dy is accounted for).
    for c, name in enumerate(chan_names):
        s, corr_c, _ = best_row_shift(
            ref[corr_row0 + dy:corr_row1 + dy:2, corr_col0:corr_col1:4, c],
            corrected[corr_row0:corr_row1:2, corr_col0:corr_col1:4, c],
            8,
        )
        report.append(f"  channel {name}: residual row shift vs reference (beyond the "
                       f"global dy={dy}) = {s:+d} (corr={corr_c:.4f})")

    # Uncorrected raw, same window, for the "Test 53 reproduces" check.
    raw_band = raw[corr_row0:corr_row1:4, corr_col0:corr_col1:4, :]
    ref_band_for_raw = ref[corr_row0 + dy:corr_row1 + dy:4, corr_col0 + dx:corr_col1 + dx:4, :]
    corrs_raw = {}
    for c, name in enumerate(chan_names):
        corrs_raw[name] = pearson(raw_band[..., c], ref_band_for_raw[..., c])
    report.append(f"Per-channel Pearson correlation, UNCORRECTED raw vs reference: "
                   f"R={corrs_raw['R']:.4f} G={corrs_raw['G']:.4f} B={corrs_raw['B']:.4f}")
    report.append(f"  (expect R/B visibly worse than G, and worse than the corrected "
                   f"figures above -- reproduces the Test 53 fringing finding)")
    del raw_band, ref_band_for_raw, corrected_band, ref_band_full, cand_band_g

    # ---- Step 7: review PNGs --------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)

    search_row_lo, search_row_hi = 700, min(corrected.shape[0] - 700, reg_row1)
    search_col_lo, search_col_hi = 700, min(W - 700, reg_col1)
    crop_row0, crop_col0 = find_high_contrast_crop(
        corrected[:, :, 1], search_row_lo, search_row_hi, search_col_lo, search_col_hi, CROP_SIZE)
    ref_row0 = min(max(crop_row0 + dy, 0), ref.shape[0] - CROP_SIZE)
    ref_col0 = min(max(crop_col0 + dx, 0), ref.shape[1] - CROP_SIZE)
    report.append(f"Review crop origin: corrected/raw (row={crop_row0}, col={crop_col0}), "
                   f"reference (row={ref_row0}, col={ref_col0})")

    ref_crop = ref[ref_row0:ref_row0 + CROP_SIZE, ref_col0:ref_col0 + CROP_SIZE, :]
    raw_crop = raw[crop_row0:crop_row0 + CROP_SIZE, crop_col0:crop_col0 + CROP_SIZE, :]
    corrected_crop = corrected[crop_row0:crop_row0 + CROP_SIZE, crop_col0:crop_col0 + CROP_SIZE, :]

    lo = np.array([np.percentile(ref_crop[..., c].astype(np.float32), 1) for c in range(3)])
    hi = np.array([np.percentile(ref_crop[..., c].astype(np.float32), 99) for c in range(3)])

    panel_ref = percentile_stretch(ref_crop, lo, hi)
    panel_raw = percentile_stretch(raw_crop, lo, hi)
    panel_corrected = percentile_stretch(corrected_crop, lo, hi)

    crop_path = out_dir / f"stagger-f{frame_no}-ref-vs-raw-vs-corrected.png"
    save_side_by_side([panel_ref, panel_raw, panel_corrected], crop_path)
    report.append(f"Wrote {crop_path}")
    del panel_ref, panel_raw, panel_corrected

    # Positive versions for the human eye (the driver's preview inversion,
    # parameters from the 1/8 reference frame so all panels invert alike).
    ref_eighth_sample = np.array(ref[::8, ::8, :])
    pos = positive_params(ref_eighth_sample)
    pos_crop_path = out_dir / f"stagger-f{frame_no}-ref-vs-raw-vs-corrected-POSITIVE.png"
    save_side_by_side([apply_positive(ref_crop, pos), apply_positive(raw_crop, pos),
                       apply_positive(corrected_crop, pos)], pos_crop_path)
    report.append(f"Wrote {pos_crop_path}")
    del ref_crop, raw_crop, corrected_crop, ref_eighth_sample

    # Full-frame 1/8 downscale (simple stride subsample -- cheap, keeps
    # memory tiny), reference vs corrected only.
    ref_eighth = ref[::8, ::8, :]
    corr_eighth = corrected[::8, ::8, :]
    h_min = min(ref_eighth.shape[0], corr_eighth.shape[0])
    w_min = min(ref_eighth.shape[1], corr_eighth.shape[1])
    ref_eighth = ref_eighth[:h_min, :w_min, :]
    corr_eighth = corr_eighth[:h_min, :w_min, :]
    lo_f = np.array([np.percentile(ref_eighth[..., c].astype(np.float32), 1) for c in range(3)])
    hi_f = np.array([np.percentile(ref_eighth[..., c].astype(np.float32), 99) for c in range(3)])
    full_path = out_dir / f"stagger-f{frame_no}-full-ref-vs-corrected-eighth.png"
    save_side_by_side([
        percentile_stretch(np.array(ref_eighth), lo_f, hi_f),
        percentile_stretch(np.array(corr_eighth), lo_f, hi_f),
    ], full_path)
    report.append(f"Wrote {full_path}")
    pos_full_path = out_dir / f"stagger-f{frame_no}-full-ref-vs-corrected-eighth-POSITIVE.png"
    save_side_by_side([
        vendor_orientation(apply_positive(np.array(ref_eighth), pos)),
        vendor_orientation(apply_positive(np.array(corr_eighth), pos)),
    ], pos_full_path)
    report.append(f"Wrote {pos_full_path}")

    report.append(f"Frame {frame_no} processed in {time.time() - t0:.1f} s")

    del raw, ref, corrected, driver_corrected, ref_eighth, corr_eighth


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--analysis-dir", type=Path,
                     default=Path("/home/christian/Dokument/plustek-135i-analys/hook5-20260908"))
    ap.add_argument("--out", type=Path,
                     default=Path("/home/christian/Bilder/opticfilm-granskning"))
    ap.add_argument("--frames", type=int, nargs="+", default=[1, 2, 4])
    args = ap.parse_args()

    report: list[str] = []
    report.append("SANE colour-line stagger direction check")
    report.append(f"analysis-dir = {args.analysis_dir}")
    report.append(f"out-dir      = {args.out}")
    report.append(f"frames       = {args.frames}")
    report.append(f"CANDIDATE_A (currently declared model) = shift(r,g,b) = {CANDIDATE_A}")
    report.append(f"CANDIDATE_B (implied by driver algebra) = shift(r,g,b) = {CANDIDATE_B}")

    ok = synthetic_test(report)

    failed_frame = None
    for n in args.frames:
        try:
            process_frame(n, args.analysis_dir, args.out, report)
        except Exception as e:
            failed_frame = n
            report.append(f"FRAME {n} FAILED: {type(e).__name__}: {e}")
            raise
        finally:
            pass

    report.append("")
    report.append("=== Summary ===")
    report.append(f"Synthetic test: {'PASS' if ok else 'FAIL'}")

    text = "\n".join(report)
    print(text)
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / "stagger-check-report.txt"
    report_path.write_text(text + "\n")
    print(f"\nReport written to {report_path}")

    return 0 if ok and failed_frame is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
