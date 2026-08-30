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


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.dpi != 3600:
        print("error: only --dpi 3600 is implemented so far", file=sys.stderr)
        return 2
    if args.ir:
        print("error: IR pass is not implemented yet", file=sys.stderr)
        return 2
    try:
        with Scanner.open() as scanner:
            scanner.initialize()
            log.info("scanning frame %d @ %d dpi", args.frame, args.dpi)
            raw, width = scanner.scan(frame=args.frame)
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
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
    if out.lower().endswith((".pnm", ".ppm")):
        image.write_pnm16(arr, out)
    else:
        image.write_tiff16(arr, out)
    print(f"wrote {out} ({arr.shape[1]}x{arr.shape[0]}, 16-bit RGB)")
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
