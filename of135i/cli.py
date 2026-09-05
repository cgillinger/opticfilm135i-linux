"""Command-line entry point for the of135i driver.

Layout per driver-design.md:

    of135i scan --frame N [--dpi 600|1200|2400|3600|7200] [--ir] -o out.tiff
    of135i scan --frames 1-4 [--ir] [--eject] -o out.tiff   # batch
    of135i eject
    of135i preview
    of135i status
    of135i watch
    of135i doctor

`scan`, `status`, `eject` and `doctor` are wired to device.py/diag.py.
`preview` has no captured trace to derive a phase list from yet
(driver-design.md open item) and stays a stub.

Hardware safety (safety.py, docs/hardware-safety.md): `status` and
`doctor` open a READ-ONLY session that can never write. `scan`,
`eject` and `watch` open a writing session whose first write is
preceded by the start-state check inside the driver; the CLI adds
nothing to those rules, it only reports the outcome. On any failure
or Ctrl-C the CLI prints the session's failure record and the power-
cycle instruction and exits -- it never sends a recovery command.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable

import usb.core

from . import diag, image, safety
from .device import SUPPORTED_DPIS, Scanner
from .usbio import InterruptOverflowError, Of135iError, UsbIo

log = logging.getLogger("of135i")

_STATUS_REGS = (0x01, 0x31, 0x32, 0x35)
_BUTTON_NAMES = {0x48: "eject", 0x04: "sensor"}


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        with UsbIo.open(readonly=True) as io:
            for reg in _STATUS_REGS:
                val = io.read_reg(reg)
                print(f"reg 0x{reg:02x} = 0x{val:02x}")
                if reg == 0x01:
                    print(f"  start state: {safety.classify_reg01(val).value}")
            reg101 = io.read_ext_reg(0x101)
            print(f"reg 0x101 = 0x{reg101:02x}")
            if reg101 & 0x08:
                print("magazine: sensor reports present (does not prove it is locked)")
            else:
                print("magazine: not detected")
            try:
                button = io.read_button()
            except InterruptOverflowError as e:
                print(f"button: unreadable -- {e}")
            else:
                if button is None:
                    print("button: idle")
                else:
                    name = _BUTTON_NAMES.get(button, f"0x{button:02x}")
                    print(f"button: {name}")
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    try:
        with UsbIo.open(readonly=True) as io:
            report = diag.collect_doctor(io)
            print(diag.format_doctor(report))
            if args.json:
                diag.write_sidecar(args.json, report)
                print(f"wrote {args.json}")
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _run_writing_session(body: Callable[[Scanner], int]) -> int:
    """Open a writing session, run `body(scanner)`, and report.

    Every failure path ends here with a printed explanation and an
    exit code -- and NOTHING sent to the scanner after the failure:
    the driver marks the session failed the moment an exception (or
    Ctrl-C) escapes a hardware operation, and Scanner.__exit__ only
    closes the USB handle.
    """
    scanner: Scanner | None = None
    try:
        with Scanner.open() as scanner:
            return body(scanner)
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        _print_session_failure(scanner)
        return 130
    except safety.SafetyError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    except (Of135iError, usb.core.USBError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        _print_session_failure(scanner)
        return 1
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        _print_session_failure(scanner)
        raise


def _print_session_failure(scanner: Scanner | None) -> None:
    if scanner is None:
        print(safety.POWER_CYCLE_INSTRUCTION, file=sys.stderr)
        return
    s = scanner.session
    if s.failure or s.refusal:
        print(s.describe_failure(), file=sys.stderr)
    elif s.writes:
        print(f"{s.writes} write(s) had been sent; the hardware state is unknown. "
              f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
              file=sys.stderr)
    else:
        print(safety.NO_COMMANDS_SENT, file=sys.stderr)


def _write_image(arr, out: str, positive: bool = False) -> None:
    """Write `arr`; a --positive TIFF gets an sRGB ICC profile embedded
    (the positive rendering targets the vendor app's sRGB output), a
    raw negative none (linear scanner data)."""
    if out.lower().endswith((".pnm", ".ppm")):
        image.write_pnm16(arr, out)
    else:
        image.write_tiff16(arr, out, icc=image.srgb_icc() if positive else None)


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
    if args.dpi not in SUPPORTED_DPIS:
        print(f"error: --dpi must be one of {', '.join(map(str, SUPPORTED_DPIS))}", file=sys.stderr)
        return 2
    # Every resolution other than 3600 exists only as a dual-light
    # (alternating IR/visible line) capture, so those always run the
    # dual flow; --ir then only decides whether the IR channel is used
    # (dust removal) and written out.
    dual = args.ir or args.dpi != 3600
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
    def body(scanner: Scanner) -> int:
        scanner.park_mode = args.park
        # Read-only start-state check up front, so an unsafe scanner
        # is reported before the magazine message (the driver would
        # refuse at the first write anyway).
        scanner.check_start_state()
        # Check the loader sensor BEFORE initialize() — the base
        # register table written by initialize() changes ext reg
        # 0x101 state, making the sensor bit unreliable after it.
        if not scanner.is_magazine_present():
            print("error: no magazine detected — insert the cassette "
                  "and run load_magazine.py first", file=sys.stderr)
            return 1
        for frame in frames:
            scanner.initialize(ir=dual, dpi=args.dpi)
            out = _frame_output(args.output, frame) if multi else args.output
            log.info("scanning frame %d @ %d dpi%s", frame, args.dpi,
                      " (dual-light pass)" if dual else "")
            if dual:
                raw, width, _meta = scanner.scan(frame=frame, ir=True, dpi=args.dpi)
                _finish_dual_scan(args, raw, width, out, write_ir=args.ir)
            else:
                raw, width = scanner.scan(frame=frame)
                _finish_plain_scan(args, raw, width, out)
            del raw
            _write_diag_sidecar(args, scanner, out, frame)
        if args.eject:
            scanner.eject()
            print("ejected")
        return 0

    return _run_writing_session(body)


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
    _write_image(arr, out, positive=args.positive)
    print(f"wrote {out} ({arr.shape[1]}x{arr.shape[0]}, 16-bit RGB)")


def _finish_dual_scan(args: argparse.Namespace, raw: bytes, width: int,
                      out: str, write_ir: bool = True) -> None:
    """Split a dual-light scan's raw buffer into visible/IR images and
    write <out> (visible, color) and, with write_ir, <out stem>-ir.tiff
    (the IR channel, replicated into R=G=B so it opens in any RGB
    viewer).

    Any orientation transform (the --positive mirror+rotate, and
    --rotate) is applied identically to both images so they stay
    pixel-aligned -- also what keeps them aligned for the dust-removal
    pass below, which runs before either transform (it only needs the
    two images on the same pixel grid, not any particular orientation).

    With write_ir (i.e. --ir), `visible` is cleaned of dust/scratches
    using the IR channel's dust map (image.remove_dust) before any
    further processing (including --positive) unless --no-clean is
    given. Without --ir (a non-3600 dpi scan, which is always a dual-
    light pass on the wire) the IR channel is discarded and the
    visible image written as-is.
    """
    import numpy as _np

    visible, ir = image.split_ir(raw, width=width)

    # Channel alignment BEFORE dust removal: the staggered R/G/B lines
    # give every dust speck a colored halo wider than its dark core;
    # cleaning on unaligned data leaves rainbow ghosts around the
    # inpainted area (observed 2026-08-30). The stagger in the de-
    # interleaved visible array is the full 24*dpi/7200 lines, the same
    # as in a plain scan: each physical line position yields one IR and
    # one visible raw line, so the visible array has the nominal line
    # density (measured on the 600/1200/2400 dpi captures: 2/4/8 lines,
    # docs/protocol-notes.md pass 18; the earlier code halved it). Crop
    # `ir` identically to keep the two images on the same pixel grid.
    _shift = round(24 * args.dpi / 7200)
    visible = image.align_channels(visible, dpi=args.dpi)
    if _shift:
        ir = ir[_shift:-_shift]

    if write_ir and not args.no_clean:
        visible = image.remove_dust(visible, ir)

    if args.positive:
        # Same orientation fix as the non-IR path; rot90/[:, ::-1] work
        # unchanged on ir's 2D (lines, width) shape too.
        visible = _np.ascontiguousarray(_np.rot90(visible, 3)[:, ::-1])
        ir = _np.ascontiguousarray(_np.rot90(ir, 3)[:, ::-1])

        # Per-frame preview inversion (image.to_positive); real colour
        # work starts from the raw negative written without --positive.
        visible = image.to_positive(visible)
    if args.rotate:
        visible = _np.ascontiguousarray(_np.rot90(visible, k=args.rotate // 90))
        ir = _np.ascontiguousarray(_np.rot90(ir, k=args.rotate // 90))

    _write_image(visible, out, positive=args.positive)
    print(f"wrote {out} ({visible.shape[1]}x{visible.shape[0]}, 16-bit RGB, visible)")

    if write_ir:
        ir_rgb = _np.stack([ir, ir, ir], axis=-1)
        out_path = Path(out)
        ir_out = str(out_path.with_name(out_path.stem + "-ir.tiff"))
        image.write_tiff16(ir_rgb, ir_out)
        print(f"wrote {ir_out} ({ir.shape[1]}x{ir.shape[0]}, 16-bit, IR channel)")


def _write_diag_sidecar(args: argparse.Namespace, scanner: Scanner, out: str, frame: int) -> None:
    """Write <out>'s .diag.json sidecar from scanner.last_diag (see
    diag.py/device.py) unless --no-diag was given, and log a one-line
    INFO summary of the per-frame calibration/health counters."""
    if args.no_diag or scanner.last_diag is None:
        return
    d = scanner.last_diag
    sidecar = dict(d)
    sidecar["output"] = out
    sidecar["cli"] = {
        "frame": frame,
        "frames": args.frames,
        "dpi": args.dpi,
        "ir": args.ir,
        "positive": args.positive,
        "rotate": args.rotate,
        "no_clean": args.no_clean,
    }
    path = diag.sidecar_path(out)
    diag.write_sidecar(path, sidecar)
    print(f"wrote {path}")
    gain_r, gain_g, gain_b = d.get("gain_codes", [0, 0, 0])
    off_r, off_g, off_b = d.get("offset_codes", [0, 0, 0])
    log.info(
        "frame %d diag: gain R=%#04x G=%#04x B=%#04x offset R=%#06x G=%#06x B=%#06x "
        "warmup_attempts=%s poll_timeouts=%s cr_mismatches=%s",
        frame, gain_r, gain_g, gain_b, off_r, off_g, off_b,
        d.get("warmup_attempts"), d.get("poll_timeouts"), d.get("cr_mismatches"),
    )


def _cmd_eject(args: argparse.Namespace) -> int:
    def body(scanner: Scanner) -> int:
        scanner.eject()
        print("ejected")
        return 0

    return _run_writing_session(body)


def _cmd_watch(args: argparse.Namespace) -> int:
    """Poll the button endpoint (reads only) and eject on the eject
    button. The start state is checked read-only up front so an
    unsafe scanner is refused immediately instead of at the first
    button press; a failed eject ends the watch (the session is
    failed; a further press must not send anything)."""
    def body(scanner: Scanner) -> int:
        scanner.check_start_state()
        print("watching for button events (Ctrl+C to stop)", flush=True)
        while True:
            try:
                button = scanner.io.read_button(timeout_ms=500)
            except KeyboardInterrupt:
                # Idle wait interrupted: nothing was in progress.
                print("\nstopped")
                return 0
            except InterruptOverflowError as e:
                print(f"cannot watch: {e}", file=sys.stderr)
                return 1
            if button is None:
                continue
            if button == 0x48:
                print("eject button pressed", flush=True)
                if scanner.is_magazine_present():
                    scanner.eject()
                    print("ejected", flush=True)
                else:
                    print("magazine not detected, ignoring", flush=True)
            elif button == 0x04:
                if scanner.is_magazine_present():
                    print("magazine inserted", flush=True)
                else:
                    print("magazine removed", flush=True)
            else:
                print(f"unknown event: 0x{button:02x}", flush=True)

    return _run_writing_session(body)


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
    p_scan.add_argument("--dpi", type=int, default=3600, choices=SUPPORTED_DPIS,
        help="scan resolution (default 3600; resolutions other than 3600 "
             "always run a dual-light pass)")
    p_scan.add_argument("--ir", action="store_true",
        help="capture an IR (dust/scratch) pass: write the IR channel and "
             "clean the visible image with it")
    p_scan.add_argument("--no-clean", action="store_true",
        help="skip IR-based dust/scratch removal on the visible image (--ir only)")
    p_scan.add_argument("--no-diag", action="store_true",
        help="skip writing the <output>.diag.json calibration/timing sidecar")
    p_scan.add_argument("--park", choices=("verbatim", "semantic"), default="verbatim",
        help="PARK phase implementation: verbatim replays the captured stream "
             "(default); semantic issues the same writes with real "
             "read-modify-write and condition waits instead of captured "
             "pacing (A/B test in progress, see docs/replay-analysis.md)")
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

    p_doctor = sub.add_parser(
        "doctor", help="read-only hardware health report (no motor/register writes)")
    p_doctor.add_argument("--json", metavar="PATH", help="also write the report as JSON to PATH")
    p_doctor.set_defaults(func=_cmd_doctor)

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
