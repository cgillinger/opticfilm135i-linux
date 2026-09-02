"""Command-line entry point for the of135i driver.

Layout per driver-design.md:

    of135i scan --frame N --dpi 3600 [--ir] -o out.tiff
    of135i scan --frames 1-4 [--ir] [--eject] -o out.tiff   # batch
    of135i eject
    of135i preview
    of135i status
    of135i watch

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
_BUTTON_NAMES = {0x48: "eject", 0x04: "sensor"}


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        with UsbIo.open() as io:
            for reg in _STATUS_REGS:
                val = io.read_reg(reg)
                print(f"reg 0x{reg:02x} = 0x{val:02x}")
            reg101 = io.read_ext_reg(0x101)
            print(f"reg 0x101 = 0x{reg101:02x}")
            if reg101 & 0x08:
                print("magazine: loaded")
            else:
                print("magazine: not detected")
            button = io.read_button()
            if button is None:
                print("button: idle")
            else:
                name = _BUTTON_NAMES.get(button, f"0x{button:02x}")
                print(f"button: {name}")
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _write_image(arr, out: str) -> None:
    if out.lower().endswith((".pnm", ".ppm")):
        image.write_pnm16(arr, out)
    else:
        image.write_tiff16(arr, out)


def _parse_frames(spec: str) -> list[int]:
    """Parse a --frames spec: comma-separated frame numbers and/or
    inclusive ranges, e.g. "1-4", "2", "1,3-4". Order is preserved,
    duplicates are not removed (scanning a frame twice is a valid,
    if unusual, request)."""
    frames: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                raise ValueError(f"descending range {part!r}")
            frames.extend(range(lo, hi + 1))
        else:
            frames.append(int(part))
    if not frames or any(f < 1 for f in frames):
        raise ValueError(f"invalid frame spec {spec!r}")
    return frames


def _frame_output(out: str, frame: int) -> str:
    """Per-frame output path for a batch scan: insert -f<N> before the
    suffix (out.tiff -> out-f2.tiff)."""
    p = Path(out)
    return str(p.with_name(f"{p.stem}-f{frame}{p.suffix}"))


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.dpi != 3600:
        print("error: only --dpi 3600 is implemented so far", file=sys.stderr)
        return 2
    if (args.frame is None) == (args.frames is None):
        print("error: give exactly one of --frame or --frames", file=sys.stderr)
        return 2
    if args.frames is not None:
        try:
            frames = _parse_frames(args.frames)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    else:
        frames = [args.frame]
    multi = args.frames is not None

    # One device session for the whole batch, initialize() per frame:
    # the post-scan PARK phase turns the lamp off and tears the scan
    # state down, and the vendor re-runs the PREP/AFE_BASE equivalent
    # before every frame (protocol-notes.md pass 14). initialize()
    # writes the power-on base table only on its first call per
    # session, matching the vendor.
    try:
        with Scanner.open() as scanner:
            for frame in frames:
                scanner.initialize(ir=args.ir)
                out = _frame_output(args.output, frame) if multi else args.output
                log.info("scanning frame %d @ %d dpi%s", frame, args.dpi,
                          " (IR pass)" if args.ir else "")
                if args.ir:
                    raw, width, _meta = scanner.scan(frame=frame, ir=True)
                    _finish_ir_scan(args, raw, width, out)
                else:
                    raw, width = scanner.scan(frame=frame)
                    _finish_plain_scan(args, raw, width, out)
                del raw
            if args.eject:
                scanner.eject()
                print("ejected")
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _finish_plain_scan(args: argparse.Namespace, raw: bytes, width: int,
                       out: str) -> None:
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
    _write_image(arr, out)
    print(f"wrote {out} ({arr.shape[1]}x{arr.shape[0]}, 16-bit RGB)")


def _finish_ir_scan(args: argparse.Namespace, raw: bytes, width: int,
                    out: str) -> None:
    """Split an IR-enabled scan's raw buffer into visible/IR images and
    write both -- <out> (visible, color) and <out stem>-ir.tiff (the IR
    channel, replicated into R=G=B so it opens in any RGB viewer).

    Any orientation transform (the --positive mirror+rotate, and
    --rotate) is applied identically to both images so they stay
    pixel-aligned -- also what keeps them aligned for the dust-removal
    pass below, which runs before either transform (it only needs the
    two images on the same pixel grid, not any particular orientation).

    By default, `visible` is cleaned of dust/scratches using the IR
    channel's dust map (image.remove_dust) before any further
    processing (including --positive); pass --no-clean to skip that and
    write the raw split visible image unchanged. The `-ir.tiff` file
    written at the end is always the raw (uncleaned) IR channel.
    """
    import numpy as _np

    visible, ir = image.split_ir(raw, width=width)

    # Channel alignment BEFORE dust removal: the staggered R/G/B lines
    # give every dust speck a colored halo wider than its dark core;
    # cleaning on unaligned data leaves rainbow ghosts around the
    # inpainted area (observed 2026-08-30). Half the plain-scan shift
    # (visible lines are every other raw line in IR mode). Crop `ir`
    # identically to keep the two images on the same pixel grid.
    _shift = round(24 * (args.dpi // 2) / 7200)
    visible = image.align_channels(visible, dpi=args.dpi // 2)
    if _shift:
        ir = ir[_shift:-_shift]

    if not args.no_clean:
        visible = image.remove_dust(visible, ir)

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
        # (Channel alignment already applied above, before cleaning.)

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

    _write_image(visible, out)

    ir_rgb = _np.stack([ir, ir, ir], axis=-1)
    out_path = Path(out)
    ir_out = str(out_path.with_name(out_path.stem + "-ir.tiff"))
    image.write_tiff16(ir_rgb, ir_out)

    print(
        f"wrote {out} ({visible.shape[1]}x{visible.shape[0]}, 16-bit RGB, visible)\n"
        f"wrote {ir_out} ({ir.shape[1]}x{ir.shape[0]}, 16-bit, IR channel)"
    )


def _cmd_eject(args: argparse.Namespace) -> int:
    try:
        with Scanner.open() as scanner:
            scanner.eject()
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("ejected")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    try:
        with Scanner.open() as scanner:
            print("watching for button events (Ctrl+C to stop)", flush=True)
            try:
                while True:
                    button = scanner.io.read_button(timeout_ms=500)
                    if button is None:
                        continue
                    if button == 0x48:
                        print("eject button pressed", flush=True)
                        if scanner.is_magazine_loaded():
                            scanner.eject()
                            print("ejected", flush=True)
                        else:
                            print("magazine not detected, ignoring", flush=True)
                    elif button == 0x04:
                        if scanner.is_magazine_loaded():
                            print("magazine inserted", flush=True)
                        else:
                            print("magazine removed", flush=True)
                    else:
                        print(f"unknown event: 0x{button:02x}", flush=True)
            except KeyboardInterrupt:
                print("\nstopped")
                return 0
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
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
    p_scan.add_argument("--frame", type=int, help="frame number (1-based)")
    p_scan.add_argument("--frames",
        help="batch scan: comma/range spec of frames, e.g. '1-4' or '1,3'; "
             "output files get a -f<N> suffix per frame")
    p_scan.add_argument("--eject", action="store_true",
        help="eject the film magazine after the last frame")
    p_scan.add_argument("--dpi", type=int, default=3600, help="scan resolution (default 3600)")
    p_scan.add_argument("--ir", action="store_true", help="capture an IR (dust/scratch) pass")
    p_scan.add_argument("--no-clean", action="store_true",
        help="skip IR-based dust/scratch removal on the visible image (--ir only)")
    p_scan.add_argument("-o", "--output", required=True, help="output file path (.tiff or .pnm)")
    p_scan.set_defaults(func=_cmd_scan)

    p_preview = sub.add_parser("preview", help="run a quick preview sweep")
    p_preview.add_argument("-o", "--output", help="output file path")
    p_preview.set_defaults(func=_not_wired_yet)

    p_eject = sub.add_parser("eject", help="eject the film magazine")
    p_eject.set_defaults(func=_cmd_eject)

    p_status = sub.add_parser("status", help="read and print status registers")
    p_status.set_defaults(func=_cmd_status)

    p_watch = sub.add_parser("watch", help="poll buttons and eject on button press")
    p_watch.set_defaults(func=_cmd_watch)

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
