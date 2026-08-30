"""Command-line entry point for the of135i driver.

Layout per driver-design.md:

    of135i scan --frame N --dpi 3600 [--ir] -o out.tiff
    of135i eject
    of135i preview
    of135i status

`scan`, `status` and `eject` are wired to device.py (scan sequencing).
`preview` has no captured trace to derive a phase list from yet
(driver-design.md open item) and stays a stub.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import image
from .device import Scanner
from .usbio import Of135iError, UsbIo

log = logging.getLogger("of135i")

_STATUS_REGS = (0x01, 0x31, 0x32, 0x35)


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        with UsbIo.open() as io:
            for reg in _STATUS_REGS:
                val = io.read_reg(reg)
                print(f"reg 0x{reg:02x} = 0x{val:02x}")
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _write_image(arr, out: str) -> None:
    if out.lower().endswith((".pnm", ".ppm")):
        image.write_pnm16(arr, out)
    else:
        image.write_tiff16(arr, out)


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.dpi != 3600:
        print("error: only --dpi 3600 is implemented so far", file=sys.stderr)
        return 2
    try:
        with Scanner.open() as scanner:
            scanner.initialize(ir=args.ir)
            log.info("scanning frame %d @ %d dpi%s", args.frame, args.dpi,
                      " (IR pass)" if args.ir else "")
            if args.ir:
                raw, width, _meta = scanner.scan(frame=args.frame, ir=True)
            else:
                raw, width = scanner.scan(frame=args.frame)
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.ir:
        return _finish_ir_scan(args, raw, width)

    arr = image.assemble(raw, width)
    arr = image.align_channels(arr, dpi=args.dpi)
    if args.positive:
        # Match the vendor apps' orientation: the sensor image is
        # mirrored (vendor ini HorizontalMirror=1) and rotated.
        import numpy as _np
        arr = _np.ascontiguousarray(_np.rot90(arr, 3)[:, ::-1])
        arr = image.to_positive(arr)
    if args.rotate:
        import numpy as _np
        arr = _np.ascontiguousarray(_np.rot90(arr, k=args.rotate // 90))
    out = args.output
    _write_image(arr, out)
    print(f"wrote {out} ({arr.shape[1]}x{arr.shape[0]}, 16-bit RGB)")
    return 0


def _finish_ir_scan(args: argparse.Namespace, raw: bytes, width: int) -> int:
    """Split an IR-enabled scan's raw buffer into visible/IR images and
    write both -- <out> (visible, color) and <out stem>-ir.tiff (the IR
    channel, replicated into R=G=B so it opens in any RGB viewer).

    Any orientation transform (the --positive mirror+rotate, and
    --rotate) is applied identically to both images so they stay
    pixel-aligned -- useful for a future dust-removal pass that needs
    to overlay the IR dust/scratch map onto the visible image.
    """
    import numpy as _np

    visible, ir = image.split_ir(raw, width=width)

    if args.positive:
        # Channel alignment FIRST (same order as the non-IR path: it
        # operates in raw line-index space, before any display-
        # orientation rotation). The R/G/B line-stagger correction
        # (image.align_channels) was measured on the plain (non-
        # alternating) scan, where every raw line is a color sample. In
        # IR mode, consecutive VISIBLE lines are already every OTHER
        # raw line (the alternating IR lines sit between them), so the
        # de-interleaved visible array has half the physical line
        # density of the plain scan -- halving the effective dpi given
        # to align_channels halves its computed shift accordingly
        # (12 -> 6 lines at 3600 dpi). NOT independently verified on
        # hardware (inferred by symmetry, not measured) -- open
        # question for hardware validation. align_channels() also crops
        # `shift` rows off each end; crop `ir` by the same amount (no
        # channel stagger to correct there, just keeping the two images
        # pixel-aligned for a future dust-overlay pass).
        shift = round(24 * (args.dpi // 2) / 7200)
        visible = image.align_channels(visible, dpi=args.dpi // 2)
        if shift:
            ir = ir[shift:-shift]

        # Same orientation fix as the non-IR path; rot90/[:, ::-1] work
        # unchanged on ir's 2D (lines, width) shape too.
        visible = _np.ascontiguousarray(_np.rot90(visible, 3)[:, ::-1])
        ir = _np.ascontiguousarray(_np.rot90(ir, 3)[:, ::-1])

        # LUT-based to_positive() was fitted at width 3762 (the plain
        # scan's windowed width); this IR-mode image is 5184 px wide
        # (the raw, unwindowed sensor width -- see ir-analysis.md), so
        # the same per-channel u16->u8 mapping is applied to pixel
        # VALUES it was never fitted against for this width -- colors
        # here are an approximation, not the fitted vendor-matched
        # rendering the plain --positive path gives. Good enough for a
        # first look; real color work should use the raw negative.
        visible = image.to_positive(visible)
    if args.rotate:
        visible = _np.ascontiguousarray(_np.rot90(visible, k=args.rotate // 90))
        ir = _np.ascontiguousarray(_np.rot90(ir, k=args.rotate // 90))

    out = args.output
    _write_image(visible, out)

    ir_rgb = _np.stack([ir, ir, ir], axis=-1)
    out_path = Path(out)
    ir_out = str(out_path.with_name(out_path.stem + "-ir.tiff"))
    image.write_tiff16(ir_rgb, ir_out)

    print(
        f"wrote {out} ({visible.shape[1]}x{visible.shape[0]}, 16-bit RGB, visible)\n"
        f"wrote {ir_out} ({ir.shape[1]}x{ir.shape[0]}, 16-bit, IR channel)"
    )
    return 0


def _cmd_eject(args: argparse.Namespace) -> int:
    try:
        with Scanner.open() as scanner:
            scanner.eject()
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("ejected")
    return 0


def _not_wired_yet(args: argparse.Namespace) -> int:
    print(f"'{args.command}' is not wired yet (needs device.py).", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="of135i", description="Userspace driver for the Plustek OpticFilm 135i."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan a frame to a raw 16-bit RGB image")
    p_scan.add_argument("--positive", action="store_true",
        help="convert the raw negative to a display-ready positive")
    p_scan.add_argument("--rotate", type=int, default=0,
        choices=(0, 90, 180, 270),
        help="rotate output counter-clockwise (degrees)")
    p_scan.add_argument("--frame", type=int, required=True, help="frame number (1-based)")
    p_scan.add_argument("--dpi", type=int, default=3600, help="scan resolution (default 3600)")
    p_scan.add_argument("--ir", action="store_true", help="capture an IR (dust/scratch) pass")
    p_scan.add_argument("-o", "--output", required=True, help="output file path (.tiff or .pnm)")
    p_scan.set_defaults(func=_cmd_scan)

    p_preview = sub.add_parser("preview", help="run a quick preview sweep")
    p_preview.add_argument("-o", "--output", help="output file path")
    p_preview.set_defaults(func=_not_wired_yet)

    p_eject = sub.add_parser("eject", help="eject the film magazine")
    p_eject.set_defaults(func=_cmd_eject)

    p_status = sub.add_parser("status", help="read and print status registers")
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
