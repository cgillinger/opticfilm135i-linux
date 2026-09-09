#!/usr/bin/env python3
"""Measure a film holder's aperture geometry from a scan.

The holder is a plastic frame with rectangular apertures separated by
opaque crossbars. In a transmissive scan the apertures pass light and
the crossbars do not, so the along-strip intensity profile is a square
wave whose edges are the physical aperture edges. This tool turns that
profile into numbers: aperture start/end/centre/length, crossbar width,
pitch between apertures, and -- for a single-frame scan -- where the
aperture sits inside the scan window.

Two modes:

  strip  A whole-holder pass (the vendor's traverse, or any scan long
         enough to cover every aperture). Reports one row per aperture
         plus the pitch table and its residuals against a constant-pitch
         model.

  frame  One frame's own scan. Reports the aperture edges inside the
         window and the offset between the aperture centre and the
         window centre -- the registration error for that frame, in the
         scan's own units and in mm.

Input is raw scanner data: 16-bit little-endian, pixel-interleaved RGB,
`--width` pixels per line. Dual-light data (every profile except plain
3600 dpi) has alternating infrared and visible lines; `--dual` splits
them and measures on the infrared pass, where the film base is
transparent and only the holder blocks light.

Memory: the raw file is memory-mapped and reduced to a 1-D profile in
row blocks, so a 1.3 GB scan costs a few MB of RAM (see the note on
systemd-oomd in docs/test-log.md).

Usage:
  holder_geometry.py strip RAW --width 876 --dpi 600 --dual [--json OUT]
  holder_geometry.py frame RAW --width 876 --dpi 600 --dual [--json OUT]
  holder_geometry.py profile PROFILE.npy --dpi 600 --dual ...

`--control PNG` writes a control image with the measured edges, aperture
centre and window centre drawn on it, for human inspection when the
numbers alone are not convincing.
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

# The scan's own along-strip sample pitch. A plain scan puts one line
# per 1/dpi inch; a dual-light scan interleaves an infrared and a
# visible line per position, so a single light's line pitch is still
# 1/dpi inch after de-interleaving.
MM_PER_INCH = 25.4

# An aperture is at least this many mm long; anything shorter in the
# profile is a hole in the holder's tab, a lead-in gap or noise, not a
# frame aperture. A 35 mm frame aperture is ~36 mm.
MIN_APERTURE_MM = 25.0


def read_pnm16(path, band=0.5):
    """Mean intensity per line of a 16-bit binary PPM/PGM.

    `of135i scan -o x.pnm` writes exactly this, so a holder scan can be
    measured straight from the driver's own output with no conversion
    step and no extra dependency. 16-bit PNM is big-endian by
    specification. Returns (profile, width).
    """
    with open(path, "rb") as fh:
        magic = fh.read(2)
        if magic not in (b"P5", b"P6"):
            raise SystemExit(f"{path}: not a binary PGM/PPM")
        fields = []
        while len(fields) < 3:
            tok = b""
            while True:
                c = fh.read(1)
                if not c:
                    raise SystemExit(f"{path}: truncated header")
                if c == b"#":
                    while fh.read(1) not in (b"\n", b""):
                        pass
                    continue
                if c.isspace():
                    if tok:
                        break
                    continue
                tok += c
            fields.append(int(tok))
        width, height, maxval = fields
        if maxval < 256:
            raise SystemExit(f"{path}: 8-bit PNM; the tool needs 16-bit")
        chans = 3 if magic == b"P6" else 1
        data = np.frombuffer(fh.read(), dtype=">u2")
    want = width * height * chans
    if len(data) < want:
        raise SystemExit(f"{path}: {len(data)} samples, expected {want}")
    a = data[:want].reshape(height, width, chans)
    lo = int(width * (1 - band) / 2)
    hi = width - lo
    prof = a[:, lo:hi, :].sum(axis=(1, 2), dtype=np.uint64) / ((hi - lo) * chans)
    return prof.astype(np.float64), width


def read_profile(path, width, dual, block_lines=4096, band=0.5):
    """Mean intensity per line over the central `band` of the width.

    Reads in blocks of `block_lines` and keeps only the running sums, so
    the peak resident size does not depend on the file size. Returns the
    profile as float64 (one value per wire line, infrared and visible
    still interleaved when `dual`).
    """
    data = np.memmap(path, dtype="<u2", mode="r")
    per_line = width * 3
    lines = len(data) // per_line
    if lines == 0:
        raise SystemExit(f"{path}: shorter than one {width}px line")
    lo = int(width * (1 - band) / 2)
    hi = width - lo
    out = np.empty(lines, dtype=np.float64)
    for start in range(0, lines, block_lines):
        stop = min(start + block_lines, lines)
        blk = np.asarray(data[start * per_line:stop * per_line])
        blk = blk.reshape(stop - start, width, 3)[:, lo:hi, :]
        # sum in uint64, not float: no full-size float copy of the block
        out[start:stop] = blk.sum(axis=(1, 2), dtype=np.uint64) / (
            (hi - lo) * 3)
    del data
    return out


def split_light(profile, dual):
    """The pass the holder is measured on.

    Dual-light data alternates infrared (even wire lines) and visible
    (odd). The infrared pass is the one to measure: colour film base is
    near-transparent to infrared, so the only thing that blocks light is
    the holder itself, and the aperture edges stay square whatever is
    (or is not) mounted in them.
    """
    return profile[0::2] if dual else profile


def edges(profile, threshold=None):
    """Sub-sample edge positions where the profile crosses the threshold.

    The threshold defaults to halfway between the profile's dark floor
    (2nd percentile = opaque plastic) and its lit level (90th percentile
    = open aperture). Each crossing is placed by linear interpolation
    between the two samples that straddle it, so an edge is located to a
    fraction of a line rather than to the nearest line.

    Returns (threshold, [(position, rising), ...]).
    """
    if threshold is None:
        dark = float(np.percentile(profile, 2))
        lit = float(np.percentile(profile, 90))
        threshold = (dark + lit) / 2.0
    above = profile > threshold
    out = []
    for i in range(1, len(profile)):
        if above[i] != above[i - 1]:
            y0, y1 = float(profile[i - 1]), float(profile[i])
            frac = (threshold - y0) / (y1 - y0) if y1 != y0 else 0.5
            out.append((i - 1 + frac, bool(above[i])))
    return threshold, out


def apertures(profile, lines_per_mm, min_mm=MIN_APERTURE_MM, threshold=None):
    """The lit runs long enough to be frame apertures, as (start, end)."""
    threshold, ed = edges(profile, threshold)
    runs = []
    for (a, rising_a), (b, rising_b) in zip(ed, ed[1:]):
        if rising_a and not rising_b:
            runs.append((a, b))
    # a run that starts before the profile does, or ends after it, is
    # only bounded on one side; both bounds must be real edges.
    keep = [(a, b) for a, b in runs if (b - a) / lines_per_mm >= min_mm]
    return threshold, keep


def report_strip(prof, dpi, args):
    lines_per_mm = dpi / MM_PER_INCH
    threshold, aps = apertures(prof, lines_per_mm, args.min_aperture_mm)
    if not aps:
        raise SystemExit("no aperture found: check --width/--dual/--dpi")
    centres = np.array([(a + b) / 2 for a, b in aps])
    lengths = np.array([b - a for a, b in aps])
    gaps = np.array([aps[i + 1][0] - aps[i][1] for i in range(len(aps) - 1)])
    pitch = np.diff(centres)

    rows = []
    for i, ((a, b), c, ln) in enumerate(zip(aps, centres, lengths), 1):
        rows.append({
            "aperture": i,
            "start_line": round(float(a), 2),
            "end_line": round(float(b), 2),
            "centre_line": round(float(c), 2),
            "length_lines": round(float(ln), 2),
            "length_mm": round(float(ln / lines_per_mm), 3),
            "pitch_to_next_lines": (round(float(pitch[i - 1]), 2)
                                    if i <= len(pitch) else None),
            "pitch_to_next_mm": (round(float(pitch[i - 1] / lines_per_mm), 3)
                                 if i <= len(pitch) else None),
            "pitch_to_next_hwdpi": (round(float(pitch[i - 1] * 7200 / dpi), 1)
                                    if i <= len(pitch) else None),
            "crossbar_after_mm": (round(float(gaps[i - 1] / lines_per_mm), 3)
                                  if i <= len(gaps) else None),
        })

    # A constant-pitch model fitted to every centre: the residuals say
    # whether one pitch describes the holder or a position table is
    # needed.
    idx = np.arange(len(centres), dtype=float)
    slope, intercept = (np.polyfit(idx, centres, 1) if len(centres) > 1
                        else (float("nan"), centres[0]))
    resid = centres - (intercept + slope * idx)

    summary = {
        "mode": "strip",
        "dpi": dpi,
        "dual": bool(args.dual),
        "threshold": round(threshold, 1),
        "aperture_count": len(aps),
        "aperture_length_mm": {
            "min": round(float(lengths.min() / lines_per_mm), 3),
            "max": round(float(lengths.max() / lines_per_mm), 3),
            "mean": round(float(lengths.mean() / lines_per_mm), 3),
        },
        "crossbar_mm": ({
            "min": round(float(gaps.min() / lines_per_mm), 3),
            "max": round(float(gaps.max() / lines_per_mm), 3),
        } if len(gaps) else None),
        "pitch": ({
            "mean_lines": round(float(pitch.mean()), 2),
            "mean_mm": round(float(pitch.mean() / lines_per_mm), 4),
            "mean_hwdpi": round(float(pitch.mean() * 7200 / dpi), 1),
            "sd_lines": (round(float(pitch.std(ddof=1)), 2)
                         if len(pitch) > 1 else 0.0),
            "min_hwdpi": round(float(pitch.min() * 7200 / dpi), 1),
            "max_hwdpi": round(float(pitch.max() * 7200 / dpi), 1),
        } if len(pitch) else None),
        "constant_pitch_fit": {
            "pitch_lines": round(float(slope), 3),
            "pitch_hwdpi": round(float(slope * 7200 / dpi), 1),
            "max_abs_residual_lines": round(float(np.abs(resid).max()), 2),
            "max_abs_residual_mm": round(
                float(np.abs(resid).max() / lines_per_mm), 4),
            "residuals_mm": [round(float(r / lines_per_mm), 4) for r in resid],
        },
        "apertures": rows,
    }
    return summary, aps, threshold


def report_frame(prof, dpi, args):
    lines_per_mm = dpi / MM_PER_INCH
    threshold, aps = apertures(prof, lines_per_mm, args.min_aperture_mm)
    window_lines = len(prof)
    window_centre = (window_lines - 1) / 2.0
    if not aps:
        # No full aperture inside the window: report what edges there are
        # so the operator can see whether the window missed entirely or
        # the aperture simply runs past both ends.
        _, ed = edges(prof, threshold)
        return {
            "mode": "frame",
            "dpi": dpi,
            "dual": bool(args.dual),
            "threshold": round(threshold, 1),
            "window_lines": window_lines,
            "window_mm": round(window_lines / lines_per_mm, 3),
            "aperture_found": False,
            "edges_in_window": [round(float(p), 2) for p, _ in ed],
            "note": "no run long enough to be an aperture; the window may "
                    "be inside the aperture (no edge) or off it entirely",
        }, aps, threshold
    a, b = max(aps, key=lambda r: r[1] - r[0])
    centre = (a + b) / 2
    offset = centre - window_centre
    return {
        "mode": "frame",
        "dpi": dpi,
        "dual": bool(args.dual),
        "threshold": round(threshold, 1),
        "window_lines": window_lines,
        "window_mm": round(window_lines / lines_per_mm, 3),
        "aperture_found": True,
        "aperture_start_line": round(float(a), 2),
        "aperture_end_line": round(float(b), 2),
        "aperture_centre_line": round(float(centre), 2),
        "aperture_length_mm": round(float((b - a) / lines_per_mm), 3),
        "window_centre_line": round(window_centre, 2),
        "centre_offset_lines": round(float(offset), 2),
        "centre_offset_mm": round(float(offset / lines_per_mm), 4),
        "centre_offset_hwdpi": round(float(offset * 7200 / dpi), 1),
        "margin_before_mm": round(float(a / lines_per_mm), 3),
        "margin_after_mm": round(float((window_lines - 1 - b) / lines_per_mm),
                                 3),
    }, aps, threshold


def write_control(path, prof, aps, threshold, mode):
    """A profile plot with the measured edges drawn on it."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("control image needs Pillow; skipped", file=sys.stderr)
        return
    h = 320
    w = min(len(prof), 2000)
    step = len(prof) / w
    lo, hi = float(prof.min()), float(prof.max())
    span = (hi - lo) or 1.0
    img = Image.new("RGB", (w, h), (16, 16, 16))
    d = ImageDraw.Draw(img)
    pts = []
    for x in range(w):
        v = float(prof[min(int(x * step), len(prof) - 1)])
        pts.append((x, h - 1 - int((v - lo) / span * (h - 12)) - 6))
    d.line(pts, fill=(220, 220, 220), width=1)
    ty = h - 1 - int((threshold - lo) / span * (h - 12)) - 6
    d.line([(0, ty), (w, ty)], fill=(90, 130, 200), width=1)
    for a, b in aps:
        for e in (a, b):
            x = int(e / step)
            d.line([(x, 0), (x, h)], fill=(230, 90, 60), width=1)
        cx = int((a + b) / 2 / step)
        d.line([(cx, 0), (cx, h)], fill=(90, 210, 120), width=1)
    if mode == "frame":
        wx = int(((len(prof) - 1) / 2) / step)
        d.line([(wx, 0), (wx, h)], fill=(240, 210, 60), width=1)
    img.save(path)
    print(f"control image -> {path}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("strip", "frame", "profile"))
    ap.add_argument("path", help="raw 16-bit RGB scan, or .npy profile "
                                 "in `profile` mode")
    ap.add_argument("--width", type=int, default=876,
                    help="pixels per line of the raw data (default 876 = "
                         "the vendor's 600 dpi strip width)")
    ap.add_argument("--dpi", type=int, required=True,
                    help="the scan's nominal resolution; sets mm per line")
    ap.add_argument("--dual", action="store_true",
                    help="alternating infrared/visible lines (every "
                         "profile except plain 3600 dpi); measures the "
                         "infrared pass")
    ap.add_argument("--band", type=float, default=0.5,
                    help="fraction of the width averaged into the "
                         "profile, centred (default 0.5)")
    ap.add_argument("--min-aperture-mm", type=float, default=MIN_APERTURE_MM,
                    help=f"shortest lit run treated as an aperture "
                         f"(default {MIN_APERTURE_MM} mm)")
    ap.add_argument("--sub-mode", choices=("strip", "frame"), default="strip",
                    help="in `profile` mode, which report to produce")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--control", help="write a control image here")
    a = ap.parse_args(argv)

    if a.mode == "profile":
        prof = np.load(a.path).astype(np.float64)
        mode = a.sub_mode
    elif a.path.lower().endswith((".pnm", ".ppm", ".pgm")):
        # The driver's own output: already de-interleaved to one line per
        # position, so --dual does not apply to it.
        prof, _ = read_pnm16(a.path, band=a.band)
        mode = a.mode
        a.dual = False
    else:
        prof = read_profile(a.path, a.width, a.dual, band=a.band)
        mode = a.mode
    prof = split_light(prof, a.dual)

    if mode == "strip":
        summary, aps, threshold = report_strip(prof, a.dpi, a)
    else:
        summary, aps, threshold = report_frame(prof, a.dpi, a)

    print(json.dumps(summary, indent=2))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(summary, fh, indent=2)
        print(f"report -> {a.json}", file=sys.stderr)
    if a.control:
        write_control(a.control, prof, aps, threshold, mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
