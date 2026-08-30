#!/usr/bin/env python3
"""Decode OpticFilm 135i raw scan data to PNG.

Format (verified 2026-08-30 against captured 3600 dpi scan):
pixel-interleaved RGB, 16-bit LE, 3762 px/line, line count = reg 0x26:0x27
(0x1411 = 5137 at 3600 dpi single frame). Usage:

  decode_image.py RAW OUT.png [--positive] [--lines N] [--width W]
"""
import argparse
import numpy as np
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("raw"); ap.add_argument("out")
ap.add_argument("--positive", action="store_true")
ap.add_argument("--width", type=int, default=3762)
ap.add_argument("--lines", type=int, default=0)
ap.add_argument("--shrink", type=int, default=1)
a = ap.parse_args()

d = np.fromfile(a.raw, dtype="<u2")
lines = a.lines or len(d) // (a.width * 3)
img = d[: lines * a.width * 3].reshape(lines, a.width, 3).astype(np.float32)
for c in range(3):
    ch = img[..., c]
    lo, hi = np.percentile(ch, 1), np.percentile(ch, 99.5)
    img[..., c] = np.clip((ch - lo) / (hi - lo), 0, 1)
if a.positive:
    img = 1.0 - img
img = (img[:: a.shrink, :: a.shrink] ** (1 / 2.2) * 255).astype(np.uint8)
Image.fromarray(img).save(a.out)
print(a.out, img.shape)
