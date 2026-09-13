#!/usr/bin/env python3
"""Host-side aperture coverage check for a SANE-delivered frame.

Lager 1 (docs/holder-position-design.md) leaves the aperture-registered
crop OUT of the backend: the SANE frontend receives the WHOLE overscan
window. This tool runs the SAME check the CLI driver uses
(of135i.aperture_crop.measure_coverage) on that delivered window, so a
SANE hardware run can be judged by the same coverage criterion as an
overscan CLI scan -- did the whole aperture land inside the window, with
overscan margin on both sides?

    tools/sane_coverage.py <frame.pnm|frame.png|frame.tif> --dpi 3600 \
        [--min-margin-mm 0.15]

Reads a binary PNM (P6, 8- or 16-bit; scanimage --format pnm) directly, and
anything Pillow opens (PNG/TIFF) for frames that reached us through a
frontend that writes those -- digiKam saves PNG or TIFF, not PNM, so the
same coverage verdict has to be available on its output (WP-2,
docs/sane-install.md). Exit 0 if coverage verified, 1 if not, 2 on a
read/parse error. It never crops or writes -- read-only, a verdict only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from of135i import aperture_crop  # noqa: E402


def read_pnm(path: str) -> np.ndarray:
    """Parse a binary P6 PNM into an (lines, width, 3) array; dtype uint8
    or big-endian uint16 by the file's maxval. Axis 0 is scan lines (the
    strip-travel direction measure_coverage profiles along)."""
    data = Path(path).read_bytes()
    if data[:2] != b"P6":
        raise ValueError(f"{path}: not a binary PPM (P6)")
    idx = 2
    toks: list[int] = []
    while len(toks) < 3:
        while idx < len(data) and data[idx:idx + 1].isspace():
            idx += 1
        if data[idx:idx + 1] == b"#":                 # comment to end of line
            while idx < len(data) and data[idx:idx + 1] != b"\n":
                idx += 1
            continue
        start = idx
        while idx < len(data) and not data[idx:idx + 1].isspace():
            idx += 1
        toks.append(int(data[start:idx]))
    width, height, maxval = toks
    idx += 1                                            # single whitespace after maxval
    body = data[idx:]
    dtype = ">u2" if maxval > 255 else np.uint8
    px = np.frombuffer(body, dtype=dtype)
    need = height * width * 3
    if px.size < need:
        raise ValueError(f"{path}: short pixel data ({px.size} < {need})")
    return px[:need].reshape(height, width, 3)


def read_image(path: str) -> np.ndarray:
    """A P6 PNM, or any image Pillow can open (digiKam saves PNG or TIFF).

    Returns (lines, width, 3). The PNM path is unchanged and byte-exact,
    16-bit included. Pillow's PNG/TIFF readers hand back 8-bit RGB even for
    a 48-bit file (verified on this repo's own 16-bit TIFFs), and a 16-bit
    grayscale file comes back 2-D -- both are fine here: measure_coverage
    bins to ~600 dpi and locates the aperture edge from the RELATIVE step
    between the two plateaus of a row-mean profile, so the verdict does not
    depend on the bit depth. Use the PNM when the pixel values themselves
    matter.
    """
    with open(path, "rb") as f:                           # magic only: a frame
        magic = f.read(2)                                 # is tens of megabytes
    if magic == b"P6":
        return read_pnm(path)
    try:
        from PIL import Image
    except ImportError as e:                              # pragma: no cover
        raise ValueError(f"{path}: not a P6 PNM and Pillow is unavailable ({e})")
    with Image.open(path) as im:
        arr = np.asarray(im)
    if arr.ndim == 2:                                     # grayscale: one plane
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f"{path}: not an image with three channels "
                         f"(shape {arr.shape})")
    return arr[:, :, :3]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pnm")
    ap.add_argument("--dpi", type=int, required=True)
    ap.add_argument("--min-margin-mm", type=float, default=0.15)
    ap.add_argument("--channel", default="red")
    args = ap.parse_args()

    try:
        img = read_image(args.pnm)
    except (OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    cov = aperture_crop.measure_coverage(
        img, dpi=args.dpi, min_margin_mm=args.min_margin_mm, channel=args.channel)
    print(f"file={args.pnm} shape={img.shape} dpi={args.dpi} "
          f"min_margin_mm={args.min_margin_mm}")
    print(f"verified={cov.verified}  reason={cov.reason!r}")
    print(f"aperture leading_line={cov.leading_line} trailing_line={cov.trailing_line}")
    print(f"margins  leading={cov.leading_margin_mm} mm  trailing={cov.trailing_margin_mm} mm")
    return 0 if cov.verified else 1


if __name__ == "__main__":
    sys.exit(main())
