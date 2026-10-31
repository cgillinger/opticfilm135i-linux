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
import contextlib
import errno
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

import usb.core

from . import diag, holder, image, safety, tables
from .device import SUPPORTED_DPIS, Scanner
from .usbio import InterruptOverflowError, Of135iError, UsbIo

EXIT_BUSY = safety.EXIT_BUSY   # another of135i process holds the scanner lock

log = logging.getLogger("of135i")

#: Sanity ceiling on --overscan (mm). The real per-frame bound is the
#: transport ceiling, checked by holder.overscan_geometry frame by frame;
#: this only rejects absurd input early.
_MAX_OVERSCAN_MM = 5.0


def _validate_overscan(overscan: float, frames, dpi: int = 3600,
                       dual: bool = False) -> str | None:
    """Check a --overscan value and its whole resulting geometry BEFORE any
    hardware write. Returns an error message, or None if every frame's
    window (FEEDL, scan start/end, line and chunk count) is inside the
    proven transport travel. A bad argument is thus caught before the
    device is even opened, not after calibration."""
    import math
    if not math.isfinite(overscan):
        return f"--overscan must be a finite number, got {overscan}"
    if overscan <= 0:
        return f"--overscan must be greater than 0 mm, got {overscan}"
    if overscan > _MAX_OVERSCAN_MM:
        return (f"--overscan {overscan} mm exceeds the {_MAX_OVERSCAN_MM} mm "
                f"sanity limit")
    for frame in frames:
        try:
            if dual:
                from .device import dual_tables
                t = dual_tables(dpi)
                holder.dual_overscan_geometry(
                    frame,
                    dpi=dpi,
                    lines_per_chunk=t.LINES_PER_CHUNK,
                    colour_crop_lines=image.align_shift(dpi),
                    default_wire_lines=t.DEFAULT_LINES,
                    overscan_mm=overscan,
                )
            else:
                holder.overscan_geometry(
                    frame,
                    res_units_per_line=7200 // 3600,
                    chunk_lines=tables.IMAGE_CHUNK_LINES,
                    colour_crop_lines=image.align_shift(3600),
                    overscan_mm=overscan,
                )
        except (safety.FrameOutOfRangeError, safety.FeedlOutOfRangeError) as e:
            return (f"--overscan {overscan} mm is out of range for frame "
                    f"{frame}: {e}")
    return None

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
    except safety.ScannerBusyError as e:
        print(f"refused: {e}", file=sys.stderr)
        print(safety.BUSY_NO_RECOVERY_MSG, file=sys.stderr)
        return EXIT_BUSY
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


def _busy_not_started(roll) -> int:
    """The scanner lock was held by another process: nothing was sent, no
    manifest record is written and the roll's number is not consumed."""
    print(f"roll {roll} not started: the scanner is busy (another of135i "
          f"process holds the lock). {safety.BUSY_NO_RECOVERY_MSG}",
          file=sys.stderr)
    return EXIT_BUSY


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


def _axis_dpi(args, positive: bool):
    """The (x_dpi, y_dpi) to stamp on the TIFF, accounting for the profile's
    anisotropy and the orientation transform applied before writing.

    The delivered strip runs along axis 0 (height) with the sensor-across
    direction on axis 1 (width): so unrotated, (x, y) = (across, along) =
    image.sampling_resolution(dpi). The 2400 dpi dual profile is the only
    anisotropic one (3600 across / 2400 along). Every 90 deg turn swaps the
    axes; the positive path adds a rot90(,3) (the horizontal mirror does
    not swap axes), --rotate adds args.rotate // 90. An even number of
    quarter-turns keeps (across, along); an odd number swaps it."""
    across, along = image.sampling_resolution(args.dpi)
    quarter = ((3 if positive else 0) + (getattr(args, "rotate", 0) or 0) // 90) % 2
    return (along, across) if quarter else (across, along)


def _write_image(arr, out: str, positive: bool = False, dpi=None) -> None:
    """Write `arr`; a --positive TIFF gets an sRGB ICC profile embedded
    (the positive rendering targets the vendor app's sRGB output), a
    raw negative none (linear scanner data). `dpi` becomes the TIFF's
    resolution tags, so the file states the scale it was scanned at; pass
    an (x_dpi, y_dpi) tuple for the anisotropic 2400 dpi dual profile (see
    _axis_dpi / image.sampling_resolution). PPM has no such field."""
    if out.lower().endswith((".pnm", ".ppm")):
        image.write_pnm16(arr, out)
    else:
        image.write_tiff16(arr, out, icc=image.srgb_icc() if positive else None,
                           dpi=dpi)


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
    if not frames:
        raise ValueError(f"invalid frame spec {spec!r}")
    # The holder's aperture count is the bound (of135i/holder.py). The
    # driver refuses an out-of-range frame again at feedl_for_frame(),
    # before any write; checking here as well turns it into a usage
    # error that never opens the device.
    bad = [f for f in frames if f < 1 or f > holder.DEFAULT.frames]
    if bad:
        raise ValueError(
            f"frame(s) {', '.join(str(f) for f in bad)} outside the "
            f"{holder.DEFAULT.name}: it holds {holder.DEFAULT.frames} "
            f"frames (1-{holder.DEFAULT.frames})")
    return frames


def _frame_output(out: str, frame: int) -> str:
    """Per-frame output path for a batch scan: insert -f<N> before the
    suffix (out.tiff -> out-f2.tiff)."""
    p = Path(out)
    return str(p.with_name(f"{p.stem}-f{frame}{p.suffix}"))


def _validate_film110_args(args: argparse.Namespace) -> "str | None":
    """--placement only applies with --film 110. With --film 110 it is
    optional: images are numbered by their position on the strip
    (of135i.film110.image_number), which needs the placement
    (docs/film-110.md §3), and when it is not given it is INFERRED from the
    first numbered image's position (of135i.film110.infer_placement); an
    image midway between the two phases is refused with a request to give
    it. Checked before any device I/O, like the neighbouring
    --dpi/--overscan checks."""
    film = getattr(args, "film", "135")
    placement = getattr(args, "placement", None)
    if film != "110" and placement is not None:
        return "--placement only applies with --film 110"
    if film != "110" and getattr(args, "strips", 1) != 1:
        return "--strips 2 only applies with --film 110"
    return None


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.dpi not in SUPPORTED_DPIS:
        print(f"error: --dpi must be one of {', '.join(map(str, SUPPORTED_DPIS))}", file=sys.stderr)
        return 2
    err = _validate_film110_args(args)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    # Every resolution other than 3600 exists only as a dual-light
    # (alternating IR/visible line) capture, so those always run the
    # dual flow; --ir then only decides whether the IR channel is used
    # (dust removal) and written out.
    dual = args.ir or args.dpi != 3600
    if getattr(args, "overscan", None) is None:
        # The production default on BOTH paths: the A+C contract
        # (corrected mapping + overscan + per-scan coverage + registered
        # crop), accepted in Test 58 for plain and carried to the dual
        # profiles in visible-line units. --overscan only tunes the
        # margin; there is no fixed-window path any more.
        args.overscan = holder.OVERSCAN_MM
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
        try:
            frames = _parse_frames(str(args.frame))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    multi = args.frames is not None

    # Validate --overscan and its whole geometry before opening the device,
    # so a bad value is refused with zero hardware activity (Astra point 3).
    if getattr(args, "overscan", None) is not None:
        err = _validate_overscan(args.overscan, frames, dpi=args.dpi, dual=dual)
        if err is not None:
            print(f"error: {err}", file=sys.stderr)
            return 2

    # Validate the output location BEFORE any hardware: a scan whose
    # files cannot be written is a completed pass thrown away (seen
    # 2026-09-10: an empty shell variable made the path '/f.tiff' --
    # the scan and PARK ran to completion and the data was lost in the
    # host-side write). Fail here instead, with zero writes sent.
    import os as _os
    for f in frames:
        o = _frame_output(args.output, f) if multi else args.output
        parent = Path(o).resolve().parent
        if not parent.is_dir():
            print(f"error: output directory {parent} does not exist "
                  f"(for {o}); nothing was sent to the scanner", file=sys.stderr)
            return 2
        if not _os.access(parent, _os.W_OK):
            print(f"error: output directory {parent} is not writable "
                  f"(for {o}); nothing was sent to the scanner", file=sys.stderr)
            return 2

    # One device session for the whole batch, initialize() per frame:
    # the post-scan PARK phase turns the lamp off and tears the scan
    # state down, and the vendor re-runs the PREP/AFE_BASE equivalent
    # before every frame (protocol-notes.md pass 14). initialize()
    # writes the power-on base table only on its first call per
    # session, matching the vendor.
    def body(scanner: Scanner) -> int:
        scanner.park_mode = args.park
        scanner.recalibrate = bool(getattr(args, 'recalibrate', False))
        if getattr(args, 'warmup_budget', None) is not None:
            scanner.warmup_budget_s = float(args.warmup_budget)
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
        incomplete: list[int] = []
        # --film 110: one running counter/state object for the WHOLE
        # command (shared across every aperture scanned), not per frame.
        film110_state = (Film110State(getattr(args, "placement", None))
                         if getattr(args, "film", "135") == "110" else None)
        for frame in frames:
            scanner.initialize(ir=dual, dpi=args.dpi)
            out = _frame_output(args.output, frame) if multi else args.output
            log.info("scanning frame %d @ %d dpi%s", frame, args.dpi,
                      " (dual-light pass)" if dual else "")
            cov = None
            if dual:
                raw, width, _meta = scanner.scan(
                    frame=frame, ir=True, dpi=args.dpi,
                    overscan_mm=getattr(args, "overscan", None))
                cov = _finish_dual_scan(args, raw, width, out, write_ir=args.ir,
                                        frame=frame, film110_state=film110_state)
            else:
                raw, width = scanner.scan(
                    frame=frame, overscan_mm=getattr(args, "overscan", None))
                cov = _finish_plain_scan(args, raw, width, out, frame=frame,
                                         film110_state=film110_state)
            del raw
            if cov is not None and not cov.verified:
                incomplete.append(frame)
            _write_diag_sidecar(args, scanner, out, frame, coverage=cov)
        if args.eject:
            scanner.eject()
            print("ejected")
        if incomplete:
            # Fail closed on image integrity: a coverage failure is not a
            # normal complete scan. The full overscan raw is preserved, but
            # no aperture-registered product was written and the exit status
            # says so (Astra point 2).
            print(f"error: aperture coverage NOT verified for frame(s) "
                  f"{', '.join(map(str, incomplete))} — see the .diag.json "
                  f"and the .overscan raw file(s); no registered product was "
                  f"written for them", file=sys.stderr)
            return 4
        if film110_state is not None:
            if film110_state.placement is None:
                print("110: placement undecided (no numbered image found)")
            else:
                print(f"110: placement {film110_state.placement} "
                      f"({'given' if film110_state.placement_given else 'inferred'})")
        if film110_state is not None and film110_state.problems:
            # A refused overwrite or an image off the chosen placement's
            # phase grid: the aperture/overscan products above are fine,
            # but at least one 110 product was NOT written -- fail closed,
            # the same way an unverified aperture does.
            print("error: 110 products refused for: " +
                  "; ".join(film110_state.problems), file=sys.stderr)
            return 5
        return 0

    return _run_writing_session(body)


def _orient_plain(args: argparse.Namespace, arr):
    """The plain-scan orientation transform: the vendor mirror + rotate for
    --positive (then the density inversion), and --rotate. Applied to a
    delivered image whose strip runs along axis 0."""
    import numpy as _np
    if args.positive:
        # Match the vendor apps' orientation: the sensor image is
        # mirrored (vendor ini HorizontalMirror=1) and rotated.
        arr = _np.ascontiguousarray(_np.rot90(arr, 3)[:, ::-1])
        arr = image.to_positive(arr)
    if args.rotate:
        arr = _np.ascontiguousarray(_np.rot90(arr, k=args.rotate // 90))
    return arr


def _orient_pair(args: argparse.Namespace, vis, irr):
    """Like `_orient_plain`, applied identically to a visible/IR pair so
    they stay pixel-aligned through the transform (`irr` may be None, in
    which case it passes through unchanged). Used by the dual-scan path
    and by the --film 110 hook, which needs the SAME transform the
    aperture product gets, on each individual 110 image crop."""
    import numpy as _np
    if args.positive:
        vis = _np.ascontiguousarray(_np.rot90(vis, 3)[:, ::-1])
        if irr is not None:
            irr = _np.ascontiguousarray(_np.rot90(irr, 3)[:, ::-1])
        vis = image.to_positive(vis)
    if args.rotate:
        vis = _np.ascontiguousarray(_np.rot90(vis, k=args.rotate // 90))
        if irr is not None:
            irr = _np.ascontiguousarray(_np.rot90(irr, k=args.rotate // 90))
    return vis, irr


class Film110State:
    """Per-command state for --film 110 (scan and digitize). Images are
    numbered by their position on the strip, not by a running counter, so
    the same photograph gets the same number in every pass over it.

    PASS 1 of a roll numbers by the protocol (docs/film-110.md §3): slot 1
    from the holder's phase (`placement` given with --placement, otherwise
    inferred from the first numbered image, of135i.film110.infer_placement),
    slot 2 anchored on its own first image (image_number_anchored).
    LATER PASSES (`ref` set: the roll's newest record with numbered images)
    number from the roll's own data: the strip may have SLID or been TURNED
    180 degrees since, which the image content decides (`finalize`, see
    _film110_finalize); numbering then waits for the end of the slot, the
    whole images of every aperture being held in `deferred` until then.
    `orientation` is the strip's turn relative to pass 1 ("same" or
    "turned"); products of a turned pass are rotated back.

    `placement` is the command's placement label ("A"/"B", or "free" when
    a later pass's strip lies on neither phase); it labels the record and
    never refuses a run. `problems` collects every 110 product this
    command declined to write; `images` has one entry per numbered image
    (digitize writes it to the manifest); `aborted` is set when numbering
    could not be established (nothing is written, exit 5).

    Two-strip loads (digitize --strips 2, docs/film-110.md §12) use one
    state per slot; `first_aperture`/`last_aperture` are the slot's
    apertures; `strips` is 1 for every other command."""

    def __init__(self, placement: "str | None" = None, slot: int = 1,
                 strips: int = 1):
        self.placement = placement
        self.placement_given = placement is not None
        self.placement_source: "str | None" = "given" if placement else None
        self.slot = slot
        self.strips = strips
        self.anchor_mm: "float | None" = None
        # A strip seated between the two placements' phases (pass 1, front end
        # not seen): numbered from its first image on the A grid's numbers,
        # with this phase instead of A's (`_film110_protocol_number`).
        self.phase_mm: "float | None" = None
        # (aperture, [(g0, g1) mm]): images a hole of `aperture` predicts
        # past its end, tried in the next aperture (film110.detect carried).
        self.carry: "tuple | None" = None
        from . import film110
        self.first_aperture = (slot - 1) * film110.SLOT_APERTURES + 1
        self.last_aperture = self.first_aperture + film110.SLOT_APERTURES - 1
        self.announced = False
        self.problems: list = []
        self.images: list = []
        self.aborted: "str | None" = None
        # later passes
        self.pass_index = 1
        self.ref: "dict | None" = None          # the reference manifest record
        self.deferred: list = []                # [(aperture, parts, items)]
        self.numbering: "dict | None" = None    # {"kind","key","entries"}
        self.orientation = "same"
        self.numbering_basis = "protocol"       # "protocol" | "content"
        self.film_start_mm: "float | None" = None
        self.film_end_mm: "float | None" = None
        self.next_strip_start_mm: "float | None" = None   # slot 1: strip 2's start seen in its last aperture
        self.finalized = False
        # Frame-position numbering from the strip's front end (docs/film-110.md
        # §15). `blanks` = unexposed frame positions seen this pass (never a
        # product); `number_shift` is added to the protocol numbers of a pass
        # that numbers without a reference (pass 1): decided from the front
        # end when it is in view (`shift_decided`), or handed in by a rebuild
        # that already knows it (`shift_forced`).
        self.blanks: list = []
        self.unidentified: list = []       # whole images written unnumbered (numbering not established)
        self.number_shift = 0
        self.shift_decided = False
        self.shift_forced = False


def _film110_ref_entries(ref: dict) -> list:
    """[(number, position_mm)] of a reference record's numbered images."""
    return [(e["number"], float(e["position_mm"])) for e in ref.get("images") or []
            if e.get("number") is not None and e.get("position_mm") is not None
            and not e.get("uncertain")]


def _film110_pick_ref(records: list) -> "dict | None":
    """The newest record that numbered at least one image with a position
    (uncertain sightings never serve as a reference: their number is a
    guess, and number 0 is a valid reference)."""
    for r in reversed(records):
        if r.get("film") == "110" and _film110_ref_entries(r):
            return r
    return None


def _film110_image_path(out: str, n: int, ir: bool = False) -> str:
    """<stem>-image<N>.tiff / <stem>-image<N>-ir.tiff beside `out`, always
    .tiff regardless of -o's own extension -- the same convention the
    dual path's -ir.tiff sidecar already uses."""
    p = Path(out)
    suffix = f"-image{n}-ir.tiff" if ir else f"-image{n}.tiff"
    return str(p.with_name(f"{p.stem}{suffix}"))


def _film110_filter_frames(args: argparse.Namespace, state: Film110State,
                           aperture: int, find) -> list:
    """The detector's predictions of one aperture that can be images, in
    order, with the ones dropped before numbering reported as messages:
    [{"msg": text} | {"fr": FrameFind}]. Nothing here depends on how the
    images are numbered (so a later pass can hold them until it knows).

    Rules, all unchanged from the two-strip work (docs/film-110.md §12):
    the strip-end ghosts (an image predicted before the next strip's first
    hole, or before the strip's film begins), the leader's ghost (a
    leading prediction with no confirmed end edge), and the trailing split
    under slot 1's last bar (the next strip's leading image)."""
    from . import film110

    px_per_mm = args.dpi / film110.MM_PER_INCH
    anchored = state.strips == 2 and state.slot == 2
    two = state.strips == 2
    frames = (sorted(find.frames, key=lambda f: f.line0) if anchored
              else find.frames)
    # Slot 1's last aperture, two strips: the first hole of the next strip
    # predicts BOTH its image after it (the trailing split under the bar)
    # and an image before it, which on a strip's first hole is no image at
    # all but strip 1's trailing leader / the gap. That predecessor is
    # recognised by sharing the hole (FrameFind.perforation_line) with the
    # cross-slot split; it is reported and dropped.
    # (FrameFind.perforation_line is the hole's END for the image after it
    # and its START for the image before it: "the same hole" = within
    # HOLE_MAX_MM.)
    ghost_holes = []
    if two and not anchored and aperture == state.last_aperture:
        ghost_holes = [f.perforation_line for f in find.frames
                       if f.split == "trailing"
                       and f.orientation == "hole-before-image"
                       and f.perforation_line is not None
                       and f.line0 > f.perforation_line]
    hole_px = film110.HOLE_MAX_MM * px_per_mm
    out = []
    for fr in frames:
        # The hole-based ghost rule does not apply to an image located by
        # the printed frame (no hole, perforation_line None).
        if (ghost_holes and fr.orientation == "hole-before-image"
                and fr.perforation_line is not None
                and fr.line0 < fr.perforation_line
                and not (fr.refined.get("line0") and fr.refined.get("line1"))
                and any(0 <= g - fr.perforation_line <= hole_px
                        for g in ghost_holes)):
            # A ghost has no edges where an image would have them. An
            # image with BOTH its along-transport edges confirmed is real
            # even here: Test 104, load 2 -- strip 1 turned and reaching
            # through aperture 3 into slot 2's first aperture, its image 2
            # whole in aperture 3 sharing a hole with its image 1's
            # trailing split, was dropped as "no strip lies there".
            out.append({"msg": "image predicted before the next strip's first "
                               "hole: no strip lies there (strip end / gap), "
                               "ignored"})
            continue
        if ((anchored or not two) and find.film_line_start is not None
                and fr.line0 < find.film_line_start
                - film110.FILM_START_TOLERANCE_MM * px_per_mm):
            # The film begins inside this aperture and the predicted image
            # starts before it: the first hole of the strip predicted an
            # image BEFORE itself (strip 1's end / the gap), which cannot
            # exist. Without this the ghost would become the anchor. On a
            # lone strip it is the clear leader (a leading split whose lines
            # before the film are air and after it clear leader): reported
            # as the leader, not as a problem.
            out.append({"msg": ("image predicted before the strip's film begins "
                                "(strip end / gap before it): no image, ignored")
                        if two else
                        "the strip's leader lies here (the film begins inside "
                        "this aperture): not an image, ignored"})
            continue
        if (not two and find.film_line_end is not None
                and fr.line1 > find.film_line_end
                + film110.FILM_START_TOLERANCE_MM * px_per_mm):
            # The mirror of the rule above: the film ENDS inside this aperture
            # and the predicted image reaches past it (the last hole predicts
            # an image after itself). On a lone strip it is the clear tail or,
            # on a turned strip, the leader at the front end.
            out.append({"msg": "the strip's tail lies here (the film ends inside "
                               "this aperture): not an image, ignored"})
            continue
        if fr.split == "leading" and not fr.refined.get("line1"):
            # Every hole predicts BOTH its neighbours, so the strip's
            # first hole also predicts an image before itself: the
            # leader. A leading image that is really there has its
            # visible END edge confirmed (every one of Test 103's three);
            # a prediction without one is that ghost and is ignored. (A
            # leading split that IS an image is numbered later and, when
            # the previous aperture already numbered it as a trailing
            # split, dropped there as already counted.)
            out.append({"msg": "leading lines predicted by a hole with no image "
                               "edge confirmed: the strip's leader, not an "
                               "image, ignored"})
            continue
        if (two and not anchored and aperture == state.last_aperture
                and fr.split == "trailing"):
            # A strip of up to four images ends before slot 1's last bar
            # in any seating (docs/film-110.md §12), so an image running
            # under it belongs to the next strip, whose first image is
            # counted from slot 2's side. It neither decides the
            # placement nor is a problem.
            out.append({"msg": "trailing split under the bar towards the next "
                               "strip: the next strip's leading image, counted "
                               "there"})
            continue
        out.append({"fr": fr})
    return out


def _film110_process_aperture(args: argparse.Namespace, state: Film110State,
                              aperture: int, vis_crop, ir_crop, emit) -> None:
    """The 110 detection + numbering + cropping shared by `scan` and
    `digitize` (one detector call per aperture, one implementation of
    numbering). `emit(n, fr, crops)` is the caller's writer, called for
    every WHOLE, correctly numbered image: `crops()` returns the padded
    (visible, IR-or-None) crops in the aperture's own, un-oriented axes;
    `emit` returns a dict {"main", "ir", "replaced_free_end_copy",
    "kept", "refused", "message"} -- `refused` set means nothing was
    written for the image (a problem), `kept` that the roll already holds
    a copy that is as good or better (information, not a problem).

    In a later pass of a roll (`state.ref`) the aperture's whole images
    are copied and held until `_film110_finalize` knows how the strip lies."""
    from . import film110, holder as _holder
    import numpy as _np

    if state.placement is None and getattr(args, "placement", None):
        state.placement = args.placement
        state.placement_given = True
        state.placement_source = "given"

    ppm = args.dpi / film110.MM_PER_INCH
    carried = []
    if state.carry is not None and state.carry[0] == aperture - 1:
        base = film110.global_position_mm(aperture, 0.0, args.dpi)
        carried = [((g0 - base) * ppm, (g1 - base) * ppm) for (g0, g1) in state.carry[1]]
    find = film110.detect(vis_crop, dpi=args.dpi, film=_holder.FILM_110, carried=carried)
    state.carry = (aperture, [(film110.global_position_mm(aperture, l0, args.dpi),
                               film110.global_position_mm(aperture, l1, args.dpi))
                              for (l0, l1) in find.beyond])
    if find.empty:
        print(f"110: aperture {aperture} — empty")
        return
    # Where the strip's own ends lie (for the advice of a later pass, and
    # for "done": no room for an unseen image).
    if find.film_line_end is not None and state.film_end_mm is None:
        state.film_end_mm = film110.global_position_mm(
            aperture, find.film_line_end, args.dpi)
    if find.film_line_start is not None and state.film_start_mm is None:
        state.film_start_mm = film110.global_position_mm(
            aperture, find.film_line_start, args.dpi)
    # Two strips, slot 1's last aperture holding pieces of BOTH strips
    # (strip 1's tail from line 0, a gap, strip 2's start to the last
    # line): the first run's end is strip 1's end, the last run's start
    # is strip 2's -- handed to slot 2 by digitize (next_strip_start_mm).
    runs = getattr(find, "film_runs", None) or []
    if (state.strips == 2 and state.slot == 1 and aperture == state.last_aperture
            and len(runs) >= 2 and runs[0][0] == 0
            and runs[-1][1] >= vis_crop.shape[0] - 1):
        if state.film_end_mm is None:
            state.film_end_mm = film110.global_position_mm(aperture, runs[0][1], args.dpi)
        state.next_strip_start_mm = film110.global_position_mm(
            aperture, runs[-1][0], args.dpi)

    if not find.frames and not getattr(find, "blanks", None):
        print(f"110: aperture {aperture} — {find.reason or 'no images predicted'}")
        return

    px_per_mm = args.dpi / film110.MM_PER_INCH
    parts = []
    if find.rail_hidden_mm:
        # The strip's far edge lies under the rail; the picture's own
        # margin on that side is width - lateral offset - image width.
        margin = (_holder.FILM_110.width_mm - _holder.FILM_110.image_lateral_offset_mm
                  - _holder.FILM_110.image_mm[1])
        clip = max(0.0, find.rail_hidden_mm - margin)
        parts.append(f"film edge {find.rail_hidden_mm:.1f} mm under the rail"
                     + (f" -> picture clipped by {clip:.1f} mm on that side; "
                        f"nudge the strip towards the open side" if clip > 0.05
                        else ""))
    pad = film110.PRODUCT_PAD_MM
    items = []
    for it in _film110_filter_frames(args, state, aperture, find):
        if "fr" not in it:
            items.append(it)
            continue
        fr = it["fr"]
        # Grown by PRODUCT_PAD_MM: a safety margin of dark printed border
        # around an edge that lies within 0.06 mm of the independently
        # measured foot (docs/film-110.md §9.1) -- not a correction for a
        # biased edge.
        it["bounds"] = [int(x) for x in film110.product_bounds(
            fr, dpi=args.dpi, pad_mm=pad, shape=vis_crop.shape)]
        # The sighting as scanned (no pad), and which part of the image it
        # shows (mm from the image's own start): a later pass recognises
        # the photograph on this, whole or split.
        vb = [int(x) for x in film110.product_bounds(
            fr, dpi=args.dpi, pad_mm=0.0, shape=vis_crop.shape)]
        it["vis_bounds"] = vb
        it["n_lines"] = int(vis_crop.shape[0])
        it["extent"] = [(vb[0] - fr.line0) / px_per_mm, (vb[1] - fr.line0) / px_per_mm]

        def crops(fr=fr):
            v = film110.crop(vis_crop, fr, dpi=args.dpi, pad_mm=pad)
            i = (film110.crop(ir_crop, fr, dpi=args.dpi, pad_mm=pad)
                 if ir_crop is not None else None)
            return v, i
        it["crops"] = crops
        it["patch"] = None
        items.append(it)

    for (b0, b1) in getattr(find, "blanks", None) or []:
        items.append({"blank": (float(b0), float(b1))})

    if state.ref is not None:
        # A later pass: hold the whole images (copies: a view would keep
        # the whole aperture alive) until the slot is complete.
        for it in items:
            if "fr" not in it:
                continue
            vb = it["vis_bounds"]
            if vb[1] > vb[0] and vb[3] > vb[2]:
                it["patch"] = film110.patch(vis_crop[vb[0]:vb[1], vb[2]:vb[3]], px_per_mm)
            if it["fr"].whole or it["fr"].cut_mm is not None:
                v, i = it["crops"]()
                v = _np.ascontiguousarray(v)
                i = _np.ascontiguousarray(i) if i is not None else None
                it["crops"] = lambda v=v, i=i: (v, i)
        state.deferred.append((aperture, parts, items))
        print(f"110: aperture {aperture} — {sum(1 for x in items if 'fr' in x)} "
              f"image(s) seen; numbering waits for the end of the strip")
    else:
        _film110_number_aperture(args, state, aperture, parts, items, emit)
    if find.leading_continuation:
        print(f"110: aperture {aperture} — leading lines continue a split "
              f"image from the previous aperture")


def _film110_protocol_number(state: "Film110State", aperture: int, line0: float,
                             dpi: float) -> tuple:
    """`film110.image_number` for the state's placement; a strip numbered
    from its own first image (`state.phase_mm`, placement "free") uses that
    phase on the same grid."""
    from . import film110
    if state.phase_mm is None:
        return film110.image_number(aperture, line0, dpi, state.placement)
    g = film110.global_position_mm(aperture, line0, dpi)
    k = (g - state.phase_mm) / film110.FILM_110.pitch_mm
    return round(k) + 1, (k - round(k)) * film110.FILM_110.pitch_mm


def _film110_apply_front_shift(state: Film110State, n: int, g: float) -> int:
    """Protocol number -> frame-position number counted from the strip's
    front end (docs/film-110.md §15). Only a pass that numbers by the
    protocol (no reference) and a lone strip use it. The shift is decided
    once, from the first sighting of the pass, when the strip's front end is
    already in view (the low holder end of pass 1, whose apertures arrive in
    order): number 1 is the first frame position whose window lies on the
    film. Without a visible front end the numbers stay provisional (shift 0).
    A rebuild that already knows the shift has set `shift_forced`."""
    from . import film110_report as rep
    if state.strips != 1:
        return n
    if not state.shift_decided:
        state.shift_decided = True
        if state.film_start_mm is not None and not state.shift_forced:
            front, _rear = rep.strip_bounds([(n, g)], (state.film_start_mm, None), False)
            if front is not None:
                state.number_shift = 1 - int(front)
    return n + state.number_shift


def _film110_number_blank(args: argparse.Namespace, state: Film110State,
                          aperture: int, window: tuple, parts: list) -> None:
    """Number an unexposed frame position (ApertureFind.blanks) like an image
    would be, record it in `state.blanks` and say so in `parts`. It consumes
    a number but never makes a product. A blank that cannot be numbered
    (placement undecided, off the grid, two-strip slot 2) is skipped."""
    from . import film110
    if state.strips == 2 and state.slot == 2:
        return
    g = film110.global_position_mm(aperture, window[0], args.dpi)
    nm = state.numbering
    if nm is not None:
        if nm["kind"] == "turned":
            n, residual = film110.number_turned(g, nm["key"], nm["entries"],
                                                direction=nm["direction"])
        else:
            n, residual = film110.number_by_shift(g, nm["key"], nm["entries"],
                                                  direction=nm["direction"])
        if abs(residual) > film110.SHIFT_RESIDUAL_MM:
            return
    else:
        if state.placement is None:
            inferred, _res = film110.infer_placement(aperture, window[0], args.dpi)
            if inferred is None:
                return
            state.placement, state.placement_source = inferred, "inferred"
        n, residual = _film110_protocol_number(state, aperture, window[0], args.dpi)
        if abs(residual) > film110.PLACEMENT_PHASE_TOLERANCE_MM or n < 1:
            return
        n = _film110_apply_front_shift(state, n, g)
    if any(b["number"] == n for b in state.blanks) or any(e["number"] == n for e in state.images):
        return
    state.blanks.append({"number": n, "aperture": aperture, "position_mm": round(float(g), 2),
                         "located_by": "frame"})
    parts.append(f"frame position {n} is blank (clear film between two printed "
                 f"borders): consumes a number, no file")


def _film110_write_off_grid(args: argparse.Namespace, state: Film110State,
                            aperture: int, it: dict, emit, why: str) -> str:
    """A WHOLE (or near-whole) image that the pass's numbering cannot place
    (off the placement's phase, between the two phases, off slot 2's grid)
    is still a scanned, complete photograph: it is written UNNUMBERED as
    p<k>-a<aperture>-<i>.tiff (+ -ir), recorded in `state.unidentified`
    (number null, identified false -- never a reference, never changes
    done/missing/free_end) and the run ends with exit 5 (Test 107: a whole
    photograph lying free in aperture 3 was dropped without a file).
    Returns the text for the aperture line; a split image gets `why` and
    "nothing written" (a later pass delivers it)."""
    from . import film110
    fr = it["fr"]
    if not (fr.whole or fr.cut_mm is not None):
        return f"{why}; split, nothing written"
    idx = 1 + sum(1 for e in state.unidentified if e["aperture"] == aperture)
    g = film110.global_position_mm(aperture, fr.line0, args.dpi)
    entry = {"number": None, "identified": False, "aperture": aperture,
             "index": idx, "whole": bool(fr.whole), "split": fr.split,
             "free_end_near": bool(fr.free_end_near),
             "located_by": getattr(fr, "located_by", "holes"),
             "position_mm": round(float(g), 2), "crop": it["vis_bounds"],
             "extent_mm": [round(float(it["extent"][0]), 2),
                           round(float(it["extent"][1]), 2)],
             "main": None, "ir": None, "kept": False, "uncertain": False,
             "size_mm": [round(float(fr.size_mm[0]), 2),
                         round(float(fr.size_mm[1]), 2)],
             "replaced_free_end_copy": False, "unnumbered_reason": why}
    if fr.cut_mm is not None:
        entry["cut_mm"] = round(float(fr.cut_mm), 2)
    if fr.end_gap_mm is not None:
        entry["end_gap_mm"] = round(float(fr.end_gap_mm), 2)
    writer = getattr(emit, "unnumbered", None)
    if writer is None:                  # `scan --film 110` has no roll directory
        return f"{why}; whole image NOT numbered, no 110 product written"
    res = writer(state.pass_index, aperture, idx, it["crops"])
    entry["main"], entry["ir"] = res.get("main"), res.get("ir")
    state.unidentified.append(entry)
    return (f"{why}; whole image written UNNUMBERED as "
            f"p{state.pass_index}-a{aperture}-{idx}.tiff")


def _film110_number_aperture(args: argparse.Namespace, state: Film110State,
                             aperture: int, parts: list, items: list,
                             emit) -> None:
    """Number and write the images of one aperture (the filtered `items`).
    Pass 1 numbers by the protocol; a later pass by the numbering that
    `_film110_finalize` fixed (`state.numbering`)."""
    from . import film110, film110_report as rep

    parts = list(parts)
    anchored = state.strips == 2 and state.slot == 2
    nm = state.numbering
    px_per_mm = args.dpi / film110.MM_PER_INCH
    for it in items:
        if "blank" in it:
            _film110_number_blank(args, state, aperture, it["blank"], parts)
            continue
        if "fr" not in it:
            parts.append(it["msg"])
            continue
        fr = it["fr"]
        deliver = bool(fr.whole or fr.cut_mm is not None)   # near-whole is delivered
        pos_mm = fr.line0 / px_per_mm
        note = ""
        g = film110.global_position_mm(aperture, fr.line0, args.dpi)
        uncertain = False
        if nm is not None:
            if state.placement is None:
                inferred, _res = film110.infer_placement(aperture, fr.line0, args.dpi)
                state.placement = inferred or "free"
                state.placement_source = "inferred" if inferred else "free"
            if not state.announced:
                note = (f"pass {state.pass_index}, strip "
                        f"{'turned' if state.orientation == 'turned' else 'not turned'} "
                        f"({state.numbering_basis}); ")
                state.announced = True
            if nm["kind"] == "turned":
                n, residual = film110.number_turned(g, nm["key"], nm["entries"],
                                                    direction=nm["direction"])
            else:
                n, residual = film110.number_by_shift(g, nm["key"], nm["entries"],
                                                      direction=nm["direction"])
            uncertain = abs(residual) > film110.SHIFT_RESIDUAL_MM
            if (n < 1 or uncertain) and not (fr.whole or fr.cut_mm is not None):
                # A split prediction before the roll's first image (the
                # leader, on this side after a turn) or off the grid:
                # nothing a product could be made of anyway, ignored.
                parts.append(note + f"image predicted at {pos_mm:.1f} mm "
                             f"({'before the roll\'s first image' if n < 1 else f'off the grid by {residual:+.1f} mm'}, "
                             f"not whole): nothing to write, ignored")
                continue
            # The system serves the photographs, not the other way round
            # (Christian, Test 104): a WHOLE image is never refused for
            # its numbering. One before the roll's first image (an earlier
            # pass missed it -- Test 104, load 2: strip 2's first
            # photograph lay under strip 1's cassette end) is numbered
            # 0, -1, ...; one that does not fit the grid is delivered as
            # image<N>-uncertain.tiff, flagged, and does not count as N.
            if n < 1 and not uncertain:
                parts.append(note + f"image at {pos_mm:.1f} mm lies before the "
                             f"roll's first image (number {n}): an earlier pass "
                             f"missed it")
                note = ""
            if uncertain:
                msg = (f"image at {pos_mm:.1f} mm does not fit the roll's "
                       f"earlier images (off by {residual:+.1f} mm, nearest "
                       f"number {n}) -> delivered as image{n}-uncertain.tiff; "
                       f"check it by eye")
                parts.append(note + msg)
                note = ""
                state.problems.append(msg)
        elif anchored:
            if state.placement is None:      # cannot happen: digitize hands it over
                raise RuntimeError("slot 2 has no placement")
            if not state.announced:
                note = (f"slot 2, placement {state.placement} "
                        f"({state.placement_source}); ")
                state.announced = True
            if state.anchor_mm is None:
                state.anchor_mm = g
            n, residual = film110.image_number_anchored(g, state.anchor_mm)
            if (abs(residual) > film110.PLACEMENT_PHASE_TOLERANCE_MM
                    or n < 1):
                msg = _film110_write_off_grid(
                    args, state, aperture, it, emit,
                    f"image at {pos_mm:.1f} mm does not fit the slot's "
                    f"film grid (off by {residual:+.1f} mm from the "
                    f"first image, number {n}) -> check the strip")
                parts.append(note + msg)
                state.problems.append(msg)
                continue
        else:
            if state.placement is None:
                inferred, _res = film110.infer_placement(aperture, fr.line0, args.dpi)
                if inferred is None and not deliver:
                    # A split (or a hole's ghost before the strip) between
                    # the phases anchors nothing: as before, a later whole
                    # image decides.
                    _na, res_a = film110.image_number(aperture, fr.line0, args.dpi, "A")
                    _nb, res_b = film110.image_number(aperture, fr.line0, args.dpi, "B")
                    msg = _film110_write_off_grid(
                        args, state, aperture, it, emit,
                        f"image at {pos_mm:.1f} mm lies between the two "
                        f"placements' phases (A off by {res_a:+.1f} mm, B off "
                        f"by {res_b:+.1f} mm)")
                    parts.append(msg)
                    state.problems.append(msg)
                    continue
                if inferred is None:
                    # Midway between the two phases (Test 108/110: three strips
                    # in one evening, all seated ~4.5 mm before A, front end
                    # out of view): number from this image on A's grid of
                    # numbers instead of refusing -- the numbers are
                    # provisional until the front end is seen, which then
                    # renumbers the roll (number_basis), and a later pass
                    # recognises the strip by content against them.
                    n_a, res_a = film110.image_number(aperture, fr.line0, args.dpi, "A")
                    _nb, res_b = film110.image_number(aperture, fr.line0, args.dpi, "B")
                    state.placement = "free"
                    state.placement_source = "between phases"
                    state.phase_mm = g - (n_a - 1) * film110.FILM_110.pitch_mm
                    note = (f"placement free (between the phases: A off by "
                            f"{res_a:+.1f} mm, B off by {res_b:+.1f} mm; numbered "
                            f"from this image); ")
                    state.announced = True
                else:
                    state.placement = inferred
                    state.placement_source = "inferred"
            if not state.announced:
                how = "given" if state.placement_given else "inferred"
                note = f"placement {state.placement} ({how}); "
                state.announced = True

            n, residual = _film110_protocol_number(state, aperture, fr.line0, args.dpi)
            if (abs(residual) > film110.PLACEMENT_PHASE_TOLERANCE_MM
                    or n < 1):
                msg = _film110_write_off_grid(
                    args, state, aperture, it, emit,
                    f"image at {pos_mm:.1f} mm is not at placement "
                    f"{state.placement}'s phase (off by {residual:+.1f} mm) -> check the placement")
                parts.append(note + msg)
                state.problems.append(msg)
                continue
            n = _film110_apply_front_shift(state, n, g)

        if fr.split == "leading" and any(e["number"] == n for e in state.images):
            # The tail of the image the previous aperture numbered as a
            # trailing split: counted there, not again. Numbering decides
            # this, not aperture order: Test 103 had image 1 seated flush
            # with aperture 1's edge (-0.1 mm) and image 4 at +0.03 mm
            # in aperture 3 with no trailing split before it, both
            # genuine first sightings of their images.
            parts.append(f"leading lines continue the previous aperture's "
                         f"split image {n} (already counted)")
            continue

        entry = {"number": n, "aperture": aperture, "whole": bool(fr.whole),
                 "split": fr.split, "free_end_near": bool(fr.free_end_near),
                 "located_by": getattr(fr, "located_by", "holes"),
                 "position_mm": round(float(g), 2),
                 "crop": it["vis_bounds"],
                 "extent_mm": [round(float(it["extent"][0]), 2),
                               round(float(it["extent"][1]), 2)],
                 "main": None, "ir": None, "kept": False,
                 "uncertain": bool(uncertain),
                 "size_mm": ([round(float(fr.size_mm[0]), 2),
                              round(float(fr.size_mm[1]), 2)]
                             if deliver else None),
                 "replaced_free_end_copy": False}
        if fr.cut_mm is not None:
            entry["cut_mm"] = round(float(fr.cut_mm), 2)
        if fr.end_gap_mm is not None:
            entry["end_gap_mm"] = round(float(fr.end_gap_mm), 2)
        if not fr.whole:
            # How much of the image lies under the plastic (predicted edge
            # against the aperture boundary) and which plastic it is.
            n_lines = it.get("n_lines")
            hid = (max(0.0, -fr.line0) if fr.split == "leading"
                   else max(0.0, fr.line1 - n_lines) if n_lines else 0.0)
            entry["hidden_mm"] = round(float(hid) / px_per_mm, 2)
            entry["under"] = rep.under_text(rep.split_where(entry))
        state.images.append(entry)

        if deliver:
            res = emit(n, fr, it["crops"], uncertain=uncertain)
            entry["main"] = res.get("main")
            entry["ir"] = res.get("ir")
            entry["replaced_free_end_copy"] = bool(res.get("replaced_free_end_copy"))
            entry["kept"] = bool(res.get("kept"))
            if res.get("refused"):
                entry["refused"] = res["refused"]
                parts.append(note + res["refused"])
                state.problems.append(res["refused"])
                continue
            parts.append(note + res.get("message",
                         f"image {n} {'whole' if fr.whole else 'near-whole'} "
                         f"({fr.size_mm[0]:.1f} x {fr.size_mm[1]:.1f} mm)"))
        else:
            # The image runs under a bar: not written. A later pass with
            # the strip seated differently delivers it.
            parts.append(note + f"image {n} split by the {fr.split} bar -> "
                         f"not delivered; seat the strip differently and "
                         f"run the roll again")
        if fr.free_end_near:
            parts[-1] += " [free strip end nearby: sharpness untrusted]"

    print(f"110: aperture {aperture} — " + "; ".join(parts))


def _film110_load_ref_patches(args: argparse.Namespace, roll: int,
                              ref: dict) -> list:
    """[(number, position_mm, u0, u1, patch)] of the reference record's
    sightings, whole or split, re-extracted from that pass's frame file on
    disk (p<k>-f<N>.tiff holds the aperture-registered crop, rotated by
    the run's --rotate; the entry's `crop` bounds are in the un-rotated
    axes, `extent_mm` says which part of the image they show). One frame
    file is loaded at a time and freed; a missing file or an entry without
    bounds simply contributes nothing."""
    from . import digitize, film110
    import numpy as _np
    out = []
    k = int(ref.get("rotate") or 0) // 90
    px_per_mm = float(ref.get("dpi") or args.dpi) / film110.MM_PER_INCH
    by_ap: dict = {}
    for e in ref.get("images") or []:
        if (e.get("crop") and e.get("extent_mm") and e.get("number") is not None
                and e.get("position_mm") is not None and not e.get("uncertain")):
            by_ap.setdefault(e["aperture"], []).append(e)
    for ap, es in sorted(by_ap.items()):
        path = digitize.frame_path_110(args.out, args.prefix, roll,
                                       f"p{ref.get('pass')}", ap)
        if not ref.get("pass") or not path.exists():
            continue
        import tifffile
        arr = tifffile.imread(str(path))
        if k:
            arr = _np.rot90(arr, -k)
        for e in es:
            l0, l1, c0, c1 = e["crop"]
            if l1 <= l0 or c1 <= c0:
                continue
            u0, u1 = e["extent_mm"]
            out.append((e["number"], float(e["position_mm"]), float(u0), float(u1),
                        film110.patch(arr[l0:l1, c0:c1], px_per_mm)))
        del arr
    return out


def _film110_write_unidentified(args: argparse.Namespace, state: Film110State,
                                deferred: list, emit) -> None:
    """Numbering could not be established for this pass (a later pass whose
    pictures cannot be matched to the earlier ones): the driver must not
    discard images that were scanned and complete. Every WHOLE (and
    near-whole) image of the pass is written UNNUMBERED as
    p<k>-a<aperture>-<i>.tiff (+ -ir), i = order within the aperture. Nothing
    that carries a number is written or replaced; the entries (number null,
    identified false) never serve as a reference and never change
    done/missing/free_end."""
    from . import film110
    writer = getattr(emit, "unnumbered", None)
    state.numbering_basis = "unidentified"
    for aperture, _parts, items in deferred:
        idx = 0
        for it in items:
            fr = it.get("fr")
            if fr is None or not (fr.whole or fr.cut_mm is not None):
                continue
            idx += 1
            g = film110.global_position_mm(aperture, fr.line0, args.dpi)
            entry = {"number": None, "identified": False, "aperture": aperture,
                     "index": idx, "whole": bool(fr.whole), "split": fr.split,
                     "free_end_near": bool(fr.free_end_near),
                     "located_by": getattr(fr, "located_by", "holes"),
                     "position_mm": round(float(g), 2), "crop": it["vis_bounds"],
                     "extent_mm": [round(float(it["extent"][0]), 2),
                                   round(float(it["extent"][1]), 2)],
                     "main": None, "ir": None, "kept": False, "uncertain": False,
                     "size_mm": [round(float(fr.size_mm[0]), 2),
                                 round(float(fr.size_mm[1]), 2)],
                     "replaced_free_end_copy": False}
            if fr.cut_mm is not None:
                entry["cut_mm"] = round(float(fr.cut_mm), 2)
            if fr.end_gap_mm is not None:
                entry["end_gap_mm"] = round(float(fr.end_gap_mm), 2)
            if writer is not None:
                res = writer(state.pass_index, aperture, idx, it["crops"])
                entry["main"], entry["ir"] = res.get("main"), res.get("ir")
            state.unidentified.append(entry)
    print(f"110: {len(state.unidentified)} whole image(s) of this pass written "
          f"UNNUMBERED (p{state.pass_index}-a<aperture>-<i>.tiff): numbering was "
          f"not established")


def _film110_finalize(args: argparse.Namespace, state: Film110State, roll: int,
                      emit) -> None:
    """The end of a slot in a later pass: decide how the strip lies --
    slid or turned since the reference record (image content, see
    of135i.film110.decide_orientation) -- number the held images and write
    the whole ones. When it cannot be decided nothing is written and
    `state.aborted` says why (exit 5) -- also when no image is whole in
    both this run and the reference: the shift is never taken from
    positions alone (the film is periodic; a move of more than half a
    pitch would number every image one off without anyone noticing)."""
    from . import film110
    import numpy as _np
    if state.finalized:
        return
    state.finalized = True
    if state.ref is None or not state.deferred:
        return
    deferred, state.deferred = state.deferred, []
    ref = state.ref
    entries = _film110_ref_entries(ref)
    positions = []
    new = []
    for aperture, _parts, items in deferred:
        for it in items:
            if "fr" in it:
                g = film110.global_position_mm(aperture, it["fr"].line0, args.dpi)
                positions.append(g)
                if it.get("patch") is not None:
                    new.append((g, it["extent"][0], it["extent"][1], it["patch"]))
    if not positions:
        return
    old = _film110_load_ref_patches(args, roll, ref) if new else []
    ref_orientation = ref.get("orientation") or "same"
    if not new or not old:
        state.aborted = (
            f"nothing of this run's images can be compared with pass "
            f"{ref.get('pass', '?')} (no sighting on one side), so the "
            f"strip's move cannot be read from the pictures -> the whole "
            f"images are written unnumbered; seat the strip so that an image "
            f"seen before is seen again, or --force")
        _film110_write_unidentified(args, state, deferred, emit)
        return
    # What is physically possible: every recorded image must still lie in
    # the slot after the move; and when the strip's own free end was seen
    # in both runs, a slide must agree with that end's move.
    end_shift = None
    for key in ("film_end_mm", "film_start_mm"):
        was, now = ref.get(key), getattr(state, key)
        if was is not None and now is not None:
            e = float(now) - float(was)
            if end_shift is not None and abs(e - end_shift) > film110.END_AGREE_MM:
                end_shift = None            # the two ends disagree: no evidence
                break
            end_shift = e
    # Black-and-white (both passes without IR -- colour always has it) is
    # compared in density: its pictures sit near black (Test 110).
    log = not getattr(args, "ir", True) and ref.get("ir") is False
    dec = film110.decide_orientation(
        new, old, pos_range=film110.slot_position_range(state.slot, state.strips),
        end_shift=end_shift, log=log)
    if dec is None:
        state.aborted = (
            f"could not tell whether the strip slid or was turned since "
            f"pass {ref.get('pass', '?')} (the images do not match the "
            f"earlier ones clearly enough, or none overlaps an earlier "
            f"sighting by {film110.OVERLAP_MIN_MM:g} mm) -> the whole "
            f"images are written unnumbered; seat the strip so that an image "
            f"seen before is seen again, or --force")
        _film110_write_unidentified(args, state, deferred, emit)
        return
    turned = dec["orientation"] == "turned"
    # The reference record's grid runs backwards along the holder when
    # that record was itself turned against pass 1.
    state.numbering = {"kind": "turned" if turned else "slide",
                       "key": dec["key"], "entries": entries,
                       "direction": -1 if ref_orientation == "turned" else 1}
    state.numbering_basis = "content"
    state.orientation = (ref_orientation if not turned else
                         "same" if ref_orientation == "turned" else "turned")
    for aperture, parts, items in deferred:
        _film110_number_aperture(args, state, aperture, parts, items, emit)
    deferred.clear()


def _film110_hook(args: argparse.Namespace, state: "Film110State | None",
                  aperture: int, out: str, vis_crop, ir_crop) -> None:
    """`scan --film 110`: detect 110 images inside one aperture-registered
    crop and write `<stem>-image<N>.tiff` per WHOLE one, numbered by strip
    position so the same photograph gets the same number whether it was
    scanned in placement A or B. Called from both `_finish_plain_scan` and
    `_finish_dual_scan`, on the SAME aperture-registered arrays those
    write their own product from (before orientation -- the strip runs
    along axis 0 here), so the plain and dual paths cannot drift apart.
    A no-op when --film is not "110" (state is None in that case) or when
    coverage did not verify (the caller only invokes this once it has).

    The placement is `args.placement` when given, otherwise inferred from
    the first numbered image (see `_film110_process_aperture`). An image
    that does not land on the placement's phase grid, is ambiguous, or
    whose product file already exists, writes nothing and is recorded in
    `state.problems`; `_cmd_scan` turns a non-empty list into a failing
    exit code once the whole command has finished.

    `ir_crop` is None on the plain path and when --ir was not given on
    the dual path -- the 110 IR sidecar is then simply not written,
    matching the aperture product's own --ir behaviour.
    """
    if state is None:
        return

    def emit(n, fr, crops, uncertain=False):
        vis_path = _film110_image_path(out, n)
        if Path(vis_path).exists():
            return {"refused": f"{vis_path} exists -> not overwritten (use "
                               f"one -o stem per placement, docs/film-110.md §7)"}
        vis_img, ir_img = crops()
        vis_img, ir_img = _orient_pair(args, vis_img, ir_img)
        image.write_tiff16(vis_img, vis_path,
                           icc=image.srgb_icc() if args.positive else None,
                           dpi=_axis_dpi(args, args.positive))
        print(f"wrote {vis_path} ({vis_img.shape[1]}x{vis_img.shape[0]}, "
              f"16-bit RGB, 110 image {n})")
        ir_path = None
        if ir_img is not None:
            import numpy as _np
            ir_path = _film110_image_path(out, n, ir=True)
            image.write_tiff16(_np.stack([ir_img, ir_img, ir_img], axis=-1),
                               ir_path, dpi=_axis_dpi(args, args.positive))
            print(f"wrote {ir_path} ({ir_img.shape[1]}x{ir_img.shape[0]}, "
                  f"16-bit, 110 image {n} IR channel)")
        return {"main": vis_path, "ir": ir_path}

    _film110_process_aperture(args, state, aperture, vis_crop, ir_crop, emit)


def _overscan_raw_path(out: str) -> str:
    """The filename for the preserved full overscan frame beside `out`."""
    p = Path(out)
    return str(p.with_name(f"{p.stem}.overscan{p.suffix}"))


def _finish_plain_scan(args: argparse.Namespace, raw: bytes, width: int,
                       out: str, frame: int = 1, film110_state=None):
    """Write a plain scan's output.

    Without --overscan: `out` is the channel-aligned delivered image, as
    before. Returns None.

    With --overscan there are three distinct artefacts, never conflated:
      1. the raw scanner buffer (`raw`, not written here);
      2. the channel-aligned full overscan frame -> `<out>.overscan.<ext>`,
         always written (the raw-data principle);
      3. the aperture-registered production image -> `out`, cropped to the
         plastic edges detected in THIS scan -- written only when coverage
         verifies. On a coverage failure `out` is deliberately NOT written,
         so nothing that looks like a finished scan exists for an
         unverified frame (fail closed on image integrity).
    Returns the ApertureCoverage in the overscan case.

    `film110_state`, when not None (--film 110), runs the 110 hook on
    the SAME aperture-registered crop right after it is computed, before
    orientation -- see `_film110_hook`.
    """
    arr = image.assemble(raw, width)
    arr = image.align_channels(arr, dpi=args.dpi)
    if getattr(args, "overscan", None) is None:
        arr = _orient_plain(args, arr)
        _write_image(arr, out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
        print(f"wrote {out} ({arr.shape[1]}x{arr.shape[0]}, 16-bit RGB)")
        return None

    # Coverage on the delivered (colour-cropped) image, before orientation
    # (the strip runs along axis 0 here). docs/holder-position-design.md
    # section 5.
    from . import aperture_crop
    cov = aperture_crop.measure_coverage(arr, dpi=args.dpi)

    # (2) preserve the full overscan frame regardless of the verdict.
    raw_out = _overscan_raw_path(out)
    full = _orient_plain(args, arr)
    _write_image(full, raw_out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
    print(f"wrote {raw_out} ({full.shape[1]}x{full.shape[0]}, full overscan frame)")
    del full

    if cov.verified:
        # (3) the aperture-registered product, cropped to the detected edges.
        crop = aperture_crop.crop_to_aperture(arr, cov, dpi=args.dpi)
        _film110_hook(args, film110_state, frame, out, crop, None)
        crop = _orient_plain(args, crop)
        _write_image(crop, out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
        print(f"aperture coverage: VERIFIED — margins lead "
              f"{cov.leading_margin_mm:.3f} mm, trail {cov.trailing_margin_mm:.3f} mm")
        print(f"wrote {out} ({crop.shape[1]}x{crop.shape[0]}, aperture-registered)")
    else:
        print(f"aperture coverage: NOT verified — {cov.reason}. Kept the full "
              f"overscan at {raw_out}; no aperture-registered product written "
              f"for this frame.", file=sys.stderr)
    return cov


def _finish_dual_scan(args: argparse.Namespace, raw: bytes, width: int,
                      out: str, write_ir: bool = True, frame: int = 1,
                      film110_state=None):
    """Split a dual-light scan's raw buffer into visible/IR images and
    write <out> (visible, color) and, with write_ir, <out stem>-ir.tiff
    (the IR channel, replicated into R=G=B so it opens in any RGB
    viewer). Returns the frame's ApertureCoverage.

    The dual path carries the same A+C production contract as plain
    (Test 58): coverage is measured on the aligned visible frame, the
    full overscan frame(s) are always preserved (`<out>.overscan.<ext>`,
    and with write_ir `<stem>-ir.overscan.tiff`), and the products are
    written only when coverage verifies -- cropped to the SAME line
    indices in both channels, so visible and IR stay exactly registered
    through the crop (they are on one pixel grid from the alignment
    step; dust removal runs before the crop on that same grid).

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

    `film110_state`, when not None (--film 110), runs the 110 hook on
    the SAME aperture-registered visible/IR crop pair right after they
    are computed, before orientation -- see `_film110_hook`.
    """
    import numpy as _np

    visible, ir = image.split_ir(raw, width=width, dpi=args.dpi)

    # Channel alignment BEFORE dust removal: the staggered R/G/B lines
    # give every dust speck a colored halo wider than its dark core;
    # cleaning on unaligned data leaves rainbow ghosts around the
    # inpainted area (observed 2026-08-30). The stagger in the de-
    # interleaved visible array is the full 24*dpi/7200 lines, the same
    # as in a plain scan: each physical line position yields one IR and
    # one visible raw line, so the visible array has the nominal line
    # density (measured on the 600/1200/2400 dpi captures: 2/4/8 lines,
    # docs/protocol-notes.md pass 18; the earlier code halved it). Crop
    # `ir` identically to keep the two images on the same pixel grid
    # (split_ir has already rolled the IR channels into register; its
    # wrapped edge rows are exactly the rows cropped here).
    _shift = round(24 * args.dpi / 7200)
    visible = image.align_channels(visible, dpi=args.dpi)
    if _shift:
        ir = ir[_shift:-_shift]

    if write_ir and not args.no_clean:
        visible = image.remove_dust(visible, ir)

    from . import aperture_crop
    # `visible` (aligned, cropped by the colour shift at both ends) and `ir`
    # (split_ir rolled it into register, then cropped by the same shift
    # above) share one line grid; measure_coverage_dual checks that. The IR
    # array is used for coverage even when no IR file is written.
    cov = aperture_crop.measure_coverage_dual(visible, ir, dpi=args.dpi)

    # _orient_pair is the module-level helper above (shared with the
    # --film 110 hook): same orientation fix as the plain path, applied
    # identically to both channels so they stay pixel-aligned.

    def _write_ir_file(arr, path):
        # The IR channel is oriented IDENTICALLY to the visible image by
        # _orient_pair (the --positive rot90(,3) mirror and --rotate are
        # applied to both), so its delivered axes match the visible ones
        # -- stamp the SAME per-axis dpi (args.positive), not _axis_dpi(
        # ..., False). Anything else made a --positive 2400 dpi IR frame
        # claim the un-rotated (across, along) while its pixels were turned
        # like the visible frame (the dual-path metadata bug). This differs
        # from digitize, where the IR is only --rotate'd (positive=False is
        # correct there).
        image.write_tiff16(_np.stack([arr, arr, arr], axis=-1), path,
                           dpi=_axis_dpi(args, args.positive))

    # Preserve the full overscan frame(s) regardless of the verdict
    # (raw-data principle), oriented like the products.
    raw_out = _overscan_raw_path(out)
    ov_vis, ov_ir = _orient_pair(args, visible, ir if write_ir else None)
    _write_image(ov_vis, raw_out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
    print(f"wrote {raw_out} ({ov_vis.shape[1]}x{ov_vis.shape[0]}, full "
          f"overscan frame, visible)")
    if write_ir:
        out_path = Path(out)
        ir_over = str(out_path.with_name(out_path.stem + "-ir.overscan.tiff"))
        _write_ir_file(ov_ir, ir_over)
        print(f"wrote {ir_over} ({ov_ir.shape[1]}x{ov_ir.shape[0]}, full "
              f"overscan frame, IR channel)")
    del ov_vis, ov_ir

    if not cov.verified:
        print(f"aperture coverage: NOT verified — {cov.reason}. Kept the full "
              f"overscan frame(s); no registered product written for this "
              f"frame.", file=sys.stderr)
        return cov

    print(f"aperture coverage: VERIFIED (source: {cov.source}) — margins lead "
          f"{cov.leading_margin_mm:.3f} mm, trail {cov.trailing_margin_mm:.3f} mm")
    vis_c = aperture_crop.crop_to_aperture(visible, cov, dpi=args.dpi)
    ir_c = aperture_crop.crop_to_aperture(ir, cov, dpi=args.dpi) if write_ir else None
    _film110_hook(args, film110_state, frame, out, vis_c, ir_c)
    vis_c, ir_c = _orient_pair(args, vis_c, ir_c)

    _write_image(vis_c, out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
    print(f"wrote {out} ({vis_c.shape[1]}x{vis_c.shape[0]}, 16-bit RGB, "
          f"visible, aperture-registered)")

    if write_ir:
        out_path = Path(out)
        ir_out = str(out_path.with_name(out_path.stem + "-ir.tiff"))
        _write_ir_file(ir_c, ir_out)
        print(f"wrote {ir_out} ({ir_c.shape[1]}x{ir_c.shape[0]}, 16-bit, "
              f"IR channel, aperture-registered)")
    return cov


def _write_diag_sidecar(args: argparse.Namespace, scanner: Scanner, out: str,
                        frame: int, coverage=None, film=None,
                        extra: dict | None = None) -> None:
    """Write <out>'s .diag.json sidecar from scanner.last_diag (see
    diag.py/device.py) unless --no-diag was given, and log a one-line
    INFO summary of the per-frame calibration/health counters.

    When `coverage` is given (the overscan path), the sidecar records
    whether the aperture was fully captured and, if not, why -- so an
    automated workflow can tell a verified frame from an incomplete one
    without re-reading the image (Astra point 2)."""
    if args.no_diag or scanner.last_diag is None:
        return
    d = scanner.last_diag
    sidecar = dict(d)
    sidecar["output"] = out
    if coverage is not None:
        sidecar["coverage"] = {
            "verified": coverage.verified,
            "reason": coverage.reason,
            "leading_line": coverage.leading_line,
            "trailing_line": coverage.trailing_line,
            "leading_margin_mm": coverage.leading_margin_mm,
            "trailing_margin_mm": coverage.trailing_margin_mm,
            "source": getattr(coverage, "source", "visible"),
        }
        if film is not None:
            # digitize only: film presence inside the verified aperture.
            sidecar["film"] = {"present": bool(film["present"]),
                               "saturated_share": film["saturated_share"]}
            if not film["present"]:
                sidecar["coverage"]["reason"] = "empty aperture"
    sidecar["cli"] = {
        "frame": frame,
        "frames": args.frames,
        "dpi": args.dpi,
        "ir": args.ir,
        "positive": args.positive,
        "rotate": args.rotate,
        "no_clean": args.no_clean,
        "overscan": getattr(args, "overscan", None),
    }
    # Per-pixel calibration data (device.Scanner.last_cal_data): only a
    # fresh calibration has any, and --no-cal-data skips it. Decided and
    # written BEFORE the sidecar so the sidecar records the outcome.
    cal_path = None
    cal_data = getattr(scanner, "last_cal_data", None)
    if cal_data is not None and not getattr(args, "no_cal_data", False):
        import numpy as _np
        try:
            cal_path = diag.cal_data_path(out)
            _np.savez_compressed(cal_path, **cal_data)
            print(f"wrote {cal_path}")
        except OSError as e:
            log.warning("could not write the calibration data file: %s", e)
            cal_path = None
    sidecar["cal_data"] = cal_path
    if extra:
        # whole-strip sweep (docs/whole-strip-sweep-design.md section 6):
        # window, sweep diag, shared calibration file name.
        sidecar.update(extra)
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


def _cmd_load(args: argparse.Namespace) -> int:
    """The magazine load flow (of135i.loadflow). Interactive: it asks the
    operator to take the magazine out and reinsert it to the stop, so it
    needs a real terminal (a piped stdin ends it at the prompt, exit 130).
    ``--release`` stops after the jog and never asks. ``--next-strip``
    loads the next strip after an eject in the same power-on: no jog, no
    reinsert prompt, and it never asks either."""
    from . import loadflow
    if args.release and args.double_jog:
        print("error: --release and --double-jog exclude each other", file=sys.stderr)
        return 2
    if args.next_strip and (args.release or args.double_jog):
        print("error: --next-strip excludes --release and --double-jog", file=sys.stderr)
        return 2
    return loadflow.run(ask=input, release_only=args.release, double_jog=args.double_jog,
                        next_strip=args.next_strip)


def _opening_is_holders(coverage, dpi) -> bool:
    """A verified coverage's opening has the holder's aperture length
    (aperture_crop.SIDE_BAND_LENGTH_TOL_MM): the 110 plain-path check that
    a film edge did not pose as the plastic's."""
    from . import aperture_crop, holder
    length = ((coverage.trailing_line - coverage.leading_line)
              * aperture_crop.MM_PER_INCH / dpi)
    return abs(length - holder.STRIP.aperture_mm) <= aperture_crop.SIDE_BAND_LENGTH_TOL_MM


def _finish_digitize_frame(args: argparse.Namespace, raw: bytes, width: int,
                           out: str, dual: bool, progress: dict | None = None,
                           film110=None):
    """digitize's per-frame writer. The MAIN image (`out`, fN.tiff) is
    always the raw NEGATIVE -- never inverted -- so it is the archival
    product. With --positive a SEPARATE preview (fN-preview.tiff, positive,
    sRGB) is written from the SAME scan (no extra hardware pass). This
    differs from `scan`, where --positive replaces the main image; here the
    negative is preserved and the preview is a side file.

    Note: with IR (and unless --no-clean) the main negative is
    dust-cleaned; it is calibrated, channel-aligned linear data, not an
    untouched sensor dump. Returns (main, ir_file, preview_file,
    dust_cleaned, coverage) -- coverage is the frame's ApertureCoverage.

    Both paths carry the same A+C production contract as `scan`
    (Test 58): the device delivers the overscan window; the full frame
    is always preserved as `<out stem>.overscan.<ext>` (and, with IR,
    `<stem>-ir.overscan.tiff`), and the MAIN negative is written only
    when aperture coverage verifies, cropped to the edges detected in
    THIS scan -- on the dual path the IR channel is cropped by the SAME
    line indices, keeping the channels exactly registered. On a
    coverage failure main is None -- nothing that looks like a finished
    frame exists for an unverified frame.

    `progress`, if given, is filled in as each file lands ("main", "ir",
    "preview"), so a failure part-way through a frame leaves a record of
    what was actually written.

    `film110`, if given (digitize --film 110), is called as
    film110(visible, ir_or_None) with the aperture-registered crops -- the
    same arrays the main negative is written from, before rotation or the
    preview -- and only for a frame whose coverage verified and whose
    aperture holds film. It runs the shared 110 detector/numbering code
    (`_film110_process_aperture`) with digitize's own writer."""
    import numpy as _np

    def _note(key: str, value) -> None:
        if progress is not None:
            progress[key] = value

    def _note_film(arr, cov) -> bool:
        """Film presence on the array coverage was measured on; records
        it in `progress` and returns True when the aperture is empty
        (no product is then written; the overscan frame already is)."""
        fp = aperture_crop.film_presence(arr, cov, dpi=args.dpi)
        _note("film_present", fp.present)
        _note("saturated_share", fp.saturated_share)
        if not fp.present:
            print(f"aperture EMPTY — no film in this opening "
                  f"({fp.saturated_share:.1%} of sampled pixels saturated in "
                  f"all channels); no main negative written, the overscan "
                  f"frame is kept")
        return not fp.present

    def _note_margin(cov) -> None:
        """Flag (never fail) a verified frame whose margin is implausibly
        wide -- the opening found is shorter than the holder's, e.g. a
        film end opaque to the measuring channel (aperture_crop
        MAX_PLAUSIBLE_MARGIN_MM). Recorded as `coverage_warning`."""
        w = aperture_crop.margin_warning(cov)
        if w:
            _note("coverage_warning", w)
            print(f"aperture coverage: WARNING — {w}", file=sys.stderr)

    from . import aperture_crop
    ir_file = None
    preview_file = None
    coverage = None
    if dual:
        visible, ir = image.split_ir(raw, width=width, dpi=args.dpi)
        _shift = round(24 * args.dpi / 7200)
        visible = image.align_channels(visible, dpi=args.dpi)
        if _shift:
            ir = ir[_shift:-_shift]
        if args.ir and not args.no_clean:
            visible = image.remove_dust(visible, ir)
        # visible and ir share one line grid (both trimmed by the same
        # colour shift above); the IR array is used for coverage even when
        # no IR file is written (dpi != 3600 without --ir).
        coverage = aperture_crop.measure_coverage_dual(visible, ir, dpi=args.dpi)
        # Preserve the full overscan frame(s) regardless of the verdict
        # (raw-data principle) -- rotated like the main negative, never
        # mirrored.
        over_v, over_i = visible, (ir if args.ir else None)
        if args.rotate:
            k = args.rotate // 90
            over_v = _np.ascontiguousarray(_np.rot90(visible, k=k))
            if over_i is not None:
                over_i = _np.ascontiguousarray(_np.rot90(over_i, k=k))
        raw_out = _overscan_raw_path(out)
        _write_image(over_v, raw_out, positive=False, dpi=_axis_dpi(args, False))
        _note("overscan", raw_out)
        print(f"wrote {raw_out} ({over_v.shape[1]}x{over_v.shape[0]}, full "
              f"overscan frame, visible)")
        if over_i is not None:
            ir_over = str(Path(out).with_name(Path(out).stem + "-ir.overscan.tiff"))
            image.write_tiff16(_np.stack([over_i, over_i, over_i], axis=-1),
                               ir_over, dpi=_axis_dpi(args, False))
            _note("ir_overscan", ir_over)
            print(f"wrote {ir_over} ({over_i.shape[1]}x{over_i.shape[0]}, "
                  f"full overscan frame, IR channel)")
        del over_v, over_i
        if not coverage.verified:
            print(f"aperture coverage: NOT verified — {coverage.reason}. "
                  f"Kept the full overscan frame(s); no main negative "
                  f"written for this frame.", file=sys.stderr)
            return None, None, None, bool(args.ir and not args.no_clean), coverage
        print(f"aperture coverage: VERIFIED (source: {coverage.source}) — "
              f"margins lead {coverage.leading_margin_mm:.3f} mm, trail "
              f"{coverage.trailing_margin_mm:.3f} mm")
        _note_margin(coverage)
        if _note_film(visible, coverage):
            return None, None, None, bool(args.ir and not args.no_clean), coverage
        visible = aperture_crop.crop_to_aperture(visible, coverage, dpi=args.dpi)
        ir = aperture_crop.crop_to_aperture(ir, coverage, dpi=args.dpi)
        if film110 is not None:
            film110(visible, ir)
    else:
        full = image.align_channels(image.assemble(raw, width), dpi=args.dpi)
        coverage = aperture_crop.measure_coverage(full, dpi=args.dpi)
        if film110 is not None and coverage.verified and not _opening_is_holders(coverage, args.dpi):
            # Dense film against a crossbar can leave a lit run of 25 mm or
            # more: the central verdict then "verifies" on the film's edge,
            # not the plastic's, and the crop would cut the film off. For
            # 110 the opening must be the holder's own length, as for the
            # side bands below.
            length = ((coverage.trailing_line - coverage.leading_line)
                      * aperture_crop.MM_PER_INCH / args.dpi)
            from dataclasses import replace as _replace
            coverage = _replace(coverage, verified=False,
                                reason=f"opening {length:.1f} mm is not the holder's aperture")
        if film110 is not None and not coverage.verified:
            # 110 on the plain path (e.g. --no-ir for black-and-white,
            # whose silver image blocks IR): a 16 mm strip fills most of
            # the central band the normal verdict reads, and dense film
            # against a crossbar hides the edge. Measure again on the bare
            # air beside the strip (aperture_crop.measure_coverage_side_band;
            # accepts only the holder's own opening length). 135 and the
            # dual paths never come here.
            side = aperture_crop.measure_coverage_side_band(full, dpi=args.dpi)
            if side.verified:
                coverage = side
            else:
                from dataclasses import replace as _replace
                coverage = _replace(
                    coverage, reason=f"visible: {coverage.reason}; {side.reason}")
        # Preserve the full overscan frame regardless of the verdict
        # (raw-data principle) -- rotated like the main negative, never
        # mirrored.
        over = full
        if args.rotate:
            over = _np.ascontiguousarray(_np.rot90(full, k=args.rotate // 90))
        raw_out = _overscan_raw_path(out)
        _write_image(over, raw_out, positive=False, dpi=_axis_dpi(args, False))
        _note("overscan", raw_out)
        print(f"wrote {raw_out} ({over.shape[1]}x{over.shape[0]}, full "
              f"overscan frame)")
        del over
        if not coverage.verified:
            print(f"aperture coverage: NOT verified — {coverage.reason}. "
                  f"Kept the full overscan at {raw_out}; no main negative "
                  f"written for this frame.", file=sys.stderr)
            return None, None, None, False, coverage
        print(f"aperture coverage: VERIFIED"
              f"{' (' + coverage.reason + ')' if coverage.source != 'visible' else ''}"
              f" — margins lead "
              f"{coverage.leading_margin_mm:.3f} mm, trail "
              f"{coverage.trailing_margin_mm:.3f} mm")
        _note_margin(coverage)
        if _note_film(full, coverage):
            return None, None, None, False, coverage
        visible = aperture_crop.crop_to_aperture(full, coverage, dpi=args.dpi)
        del full
        ir = None
        if film110 is not None:
            film110(visible, None)

    # Dust removal already ran in the dual branch above (before the
    # coverage measurement, on the uncropped grid).
    dust_cleaned = bool(dual and args.ir and not args.no_clean)

    # Preview (positive): built with the SAME vendor orientation the scan
    # command applies before to_positive -- mirror + rot90(·,3) (vendor ini
    # HorizontalMirror=1). Without it the preview is mirrored and text reads
    # backwards. Built from the un-rotated visible; --rotate is applied to
    # it below too, so it matches `scan --positive` on the same raw data.
    prev = None
    if args.positive:
        prev = _np.ascontiguousarray(_np.rot90(visible, 3)[:, ::-1])
        prev = image.to_positive(prev)

    # Rotate the archival negative (and IR, and the preview) if requested.
    # The negative is only rotated, never mirrored -- the mirror belongs to
    # the positive path -- matching scan's raw output.
    if args.rotate:
        k = args.rotate // 90
        visible = _np.ascontiguousarray(_np.rot90(visible, k=k))
        if ir is not None:
            ir = _np.ascontiguousarray(_np.rot90(ir, k=k))
        if prev is not None:
            prev = _np.ascontiguousarray(_np.rot90(prev, k=k))

    _write_image(visible, out, positive=False, dpi=_axis_dpi(args, False))  # raw negative, never inverted
    _note("main", out)
    print(f"wrote {out} ({visible.shape[1]}x{visible.shape[0]}, 16-bit RGB "
          f"negative{', dust-cleaned' if dust_cleaned else ''})")

    if dual and args.ir:
        ir_rgb = _np.stack([ir, ir, ir], axis=-1)
        ir_file = str(Path(out).with_name(Path(out).stem + "-ir.tiff"))
        image.write_tiff16(ir_rgb, ir_file, dpi=_axis_dpi(args, False))
        _note("ir", ir_file)
        print(f"wrote {ir_file} ({ir.shape[1]}x{ir.shape[0]}, 16-bit, IR channel)")

    if prev is not None:
        preview_file = str(Path(out).with_name(Path(out).stem + "-preview.tiff"))
        _write_image(prev, preview_file, positive=True, dpi=_axis_dpi(args, True))
        _note("preview", preview_file)
        print(f"wrote {preview_file} ({prev.shape[1]}x{prev.shape[0]}, "
              f"16-bit RGB, positive preview)")

    return out, ir_file, preview_file, dust_cleaned, coverage


def _film110_digitize_emit(args: argparse.Namespace, state: Film110State,
                           roll: int, stage: "Path | None" = None,
                           records_fn=None):
    """The photograph writer `digitize --film 110` hands to
    `_film110_number_aperture`: <roll_dir>/image<N>.tiff (+ image<N>-ir.tiff
    with --ir), N = strip position, oriented like pass 1's products (a
    turned pass is rotated back by 180 degrees first), then like the
    frame's main negative (rotated if asked, never inverted or mirrored).

    The best copy of a photograph wins (docs/film-110.md §4): an existing
    image<N>.tiff is replaced only when the manifest (or this run) shows it
    was delivered with `free_end_near` and the new one is not
    (`replaced_free_end_copy`); otherwise the existing one is kept and the
    result says `kept` -- information, not a problem."""
    from . import digitize
    import numpy as _np

    def emit(n, fr, crops, uncertain=False):
        vis_path = digitize.film110_image_path(args.out, args.prefix, roll, n,
                                               uncertain=uncertain)
        ir_path = digitize.film110_image_path(args.out, args.prefix, roll, n, ir=True,
                                              uncertain=uncertain)
        if stage is not None:
            # A rebuild writes into its staging directory only; the roll
            # directory is touched when every pass has succeeded.
            vis_path, ir_path = Path(stage) / vis_path.name, Path(stage) / ir_path.name
        replaced = False
        if uncertain and vis_path.exists():
            return {"kept": True,
                    "message": f"image {n}: {vis_path.name} exists -> kept"}
        if vis_path.exists():
            # state.images[-1] is the entry being written right now.
            records = (list(records_fn()) if records_fn is not None else
                       digitize.roll_records_current(args.out, roll, args.prefix))
            records.append({"images": state.images[:-1]})
            prev = digitize.delivered_image(records, roll, args.prefix, n)
            from . import film110_report as rep
            mine = {"whole": fr.whole, "cut_mm": fr.cut_mm, "free_end_near": fr.free_end_near}
            if prev is not None and rep.copy_rank(mine) > rep.copy_rank(prev):
                replaced = True
            else:
                return {"kept": True,
                        "message": f"image {n}: {vis_path.name} exists and is "
                                   f"as good or better -> kept"}
        vis_img, ir_img = crops()
        if state.orientation == "turned":
            vis_img = _np.ascontiguousarray(_np.rot90(vis_img, k=2))
            if ir_img is not None:
                ir_img = _np.ascontiguousarray(_np.rot90(ir_img, k=2))
        if args.rotate:
            k = args.rotate // 90
            vis_img = _np.ascontiguousarray(_np.rot90(vis_img, k=k))
            if ir_img is not None:
                ir_img = _np.ascontiguousarray(_np.rot90(ir_img, k=k))
        _write_image(vis_img, str(vis_path), positive=False,
                     dpi=_axis_dpi(args, False))
        print(f"wrote {vis_path} ({vis_img.shape[1]}x{vis_img.shape[0]}, "
              f"16-bit RGB negative, 110 image {n}"
              f"{', replaced the free-end copy' if replaced else ''})")
        ir_out = None
        if ir_img is not None:
            image.write_tiff16(_np.stack([ir_img, ir_img, ir_img], axis=-1),
                               str(ir_path), dpi=_axis_dpi(args, False))
            ir_out = str(ir_path)
            print(f"wrote {ir_path} ({ir_img.shape[1]}x{ir_img.shape[0]}, "
                  f"16-bit, 110 image {n} IR channel)")
        return {"main": str(vis_path), "ir": ir_out,
                "replaced_free_end_copy": replaced,
                "message": f"image {n} {'whole' if fr.whole else 'near-whole'} ({fr.size_mm[0]:.1f} x "
                           f"{fr.size_mm[1]:.1f} mm)"
                           f"{' [replaced the free-end copy]' if replaced else ''}"}

    def unnumbered(k, aperture, i, crops):
        base = Path(stage) if stage is not None else digitize.roll_dir(args.out, args.prefix, roll)
        vis_path = base / f"p{k}-a{aperture}-{i}.tiff"
        ir_path = base / f"p{k}-a{aperture}-{i}-ir.tiff"
        vis_img, ir_img = crops()
        if args.rotate:
            kk = args.rotate // 90
            vis_img = _np.ascontiguousarray(_np.rot90(vis_img, k=kk))
            if ir_img is not None:
                ir_img = _np.ascontiguousarray(_np.rot90(ir_img, k=kk))
        _write_image(vis_img, str(vis_path), positive=False, dpi=_axis_dpi(args, False))
        print(f"wrote {vis_path} ({vis_img.shape[1]}x{vis_img.shape[0]}, 16-bit RGB "
              f"negative, UNNUMBERED image of pass {k})")
        ir_out = None
        if ir_img is not None:
            image.write_tiff16(_np.stack([ir_img, ir_img, ir_img], axis=-1),
                               str(ir_path), dpi=_axis_dpi(args, False))
            ir_out = str(ir_path)
        return {"main": str(vis_path), "ir": ir_out}

    emit.unnumbered = unnumbered
    return emit


def _film110_suggest(state: Film110State, prior: list, missing: list,
                     free_end: list) -> str:
    """One short measured recommendation for the next pass of this strip
    (advice, not a requirement). For each number seen only split: the move
    (mm, "further in" = towards the holder's middle) that puts the bar
    which splits it in the middle of the gap beside the image. For each
    delivered-with-the-end-free number: whether moving the strip puts its
    end under a bar. Positions are those of the latest sighting (carried
    over by the pass's shift when only an earlier pass saw the number)."""
    from . import film110
    if not missing and not free_end:
        if not state.images and not any(r.get("images") for r in (prior or [])):
            return ("no image found: check that the strip's holes are visible "
                    "(a gap between the hole edge and the rail) and that the "
                    "film lies in the apertures, then seat it again and run "
                    "the roll again")
        return "nothing missing; every delivered image is supported"
    pos = {}
    ref = state.ref
    if ref is not None and state.numbering is not None and state.numbering["kind"] == "slide":
        for num, p in _film110_ref_entries(ref):
            pos[num] = p + state.numbering["key"]
    for e in state.images:
        if e.get("position_mm") is not None:
            pos[e["number"]] = e["position_mm"]
    bars = film110.bar_spans_mm(state.slot, state.strips)
    inward = -1.0 if state.slot == 2 else 1.0
    label = f"strip {state.slot}: " if state.strips == 2 else ""

    def move(s):
        d = s * inward
        return (f"{abs(d):.1f} mm further in" if d >= 0
                else f"{abs(d):.1f} mm back out")

    # A move is offered when every known image (and the strip's seen ends)
    # stays inside the slot; both directions when both do -- the driver
    # cannot know whether the strip already rests against an end wall
    # (Test 104: "back out" was offered to a strip against the wall, the
    # 13.8 mm inward move being 0.05 mm outside an earlier fixed window).
    lo_r, hi_r = film110.slot_position_range(state.slot, state.strips)
    known = list(pos.values())
    known += [x for x in (state.film_start_mm, state.film_end_mm) if x is not None]

    def feasible(s):
        return all(lo_r <= x + s <= hi_r for x in known)

    def directions(cands):
        ins = [s for s in cands if feasible(s) and s * inward > 0]
        outs = [s for s in cands if feasible(s) and s * inward < 0]
        parts = []
        if ins:
            parts.append(move(min(ins, key=abs)))
        if outs:
            parts.append(move(min(outs, key=abs)))
        return parts

    texts = []
    for n in missing:
        if n not in pos:
            texts.append(f"image {n}: not seen in the latest pass, position unknown")
            continue
        parts = directions(film110.bar_centering_shifts(pos[n], bars))
        if not parts:
            texts.append(f"image {n}: no move keeps every image inside the slot")
        else:
            texts.append(f"image {n}: move " + " or ".join(parts))
    for n in free_end:
        p = pos.get(n)
        length = film110.FILM_110.image_mm[0]
        s = None
        end = state.film_end_mm
        start = state.film_start_mm
        if p is not None and end is not None and 0 <= end - (p + length) < film110.FILM_110.pitch_mm:
            nxt = [b for b in bars if b[0] >= end - 0.5]
            if nxt:
                s = (nxt[0][0] + nxt[0][1]) / 2 - end
        elif p is not None and start is not None and 0 <= p - start < film110.FILM_110.pitch_mm:
            prv = [b for b in bars if b[1] <= start + 0.5]
            if prv:
                s = (prv[-1][0] + prv[-1][1]) / 2 - start
        elif p is None or (end is None and start is None):
            texts.append(f"image {n}: strip end free, no leader/tail measured")
            continue
        if s is not None and feasible(s):
            texts.append(f"image {n}: moving {move(s)} puts the strip end under a "
                         f"bar (may split other images)")
        else:
            texts.append(f"image {n}: strip end free, no seating puts it under "
                         f"plastic -- accept the free-end copy or stop")
    return label + "; ".join(texts)


def _film110_now_text(n: int, report: list, windows: dict, unseen: set) -> str:
    """Where image `n` lies now, for the next-pass sentence."""
    from . import film110_report as rep
    if n in unseen:
        return "not seen so far"
    for r in report:
        if r["number"] == n:
            if r["state"] == "split":
                return "now split by " + rep.where_text(r["where"])
            if r["state"] == "whole_free_end":
                return "now whole, with the strip end free"
    w = rep.locate_window(windows[n]) if n in windows else None
    if w is not None and w["kind"] != "aperture":
        return "now split by " + rep.where_text(w)
    return "now in " + (rep.where_text(w) if w else "the holder")


def _film110_plan(state: Film110State, prior: list, report: list, missing: list,
                  free_end: list, unseen: list, seen_all: set, sight: list,
                  turned: bool, optional: tuple = ()):
    """advice.next for a lone strip: the joint placement (film110_report.
    plan_next_pass) over the positions this pass measured, carried over the
    film grid to the frame positions it did not see."""
    from . import film110, film110_report as rep
    if not sight:
        gaps0 = {}
        for n in free_end:
            for r in reversed(list(prior)):
                m = next((x.get("end_gap_mm") for x in r.get("images") or []
                          if x.get("number") == n and x.get("end_gap_mm") is not None), None)
                if m is not None:
                    gaps0[n] = float(m)
                    break
        return None, gaps0
    measured = dict(sight)
    L = film110.FILM_110.image_mm[0]

    def window(n):
        if n in measured:
            return measured[n]
        ref = min(sight, key=lambda t: abs(t[0] - n))
        return rep.window_of(n, ref, turned)

    known = {n: window(n) for n in seen_all}
    extra = {n: window(n) for n in unseen}
    ends = [e for e in (state.film_start_mm, state.film_end_mm) if e is not None]
    # Where the strip end beyond a free-end image lies in THIS pass: the
    # distance beyond the image was measured in the pass that saw that end
    # (newest first) and is carried over, flipped when the strip lies turned
    # against that pass.
    pitch = film110.FILM_110.pitch_mm
    recs = list(prior) + [{"images": state.images, "film_start_mm": state.film_start_mm,
                           "film_end_mm": state.film_end_mm,
                           "orientation": state.orientation}]
    end_beyond = {}
    for n in list(free_end) + list(optional):
        w = known.get(n)
        if w is None:
            continue
        for r in reversed(recs):
            e = next((x for x in r.get("images") or []
                      if x.get("number") == n and x.get("position_mm") is not None
                      and not x.get("uncertain")), None)
            if e is None:
                continue
            pos = float(e["position_mm"])
            hit = None
            end_hi, end_lo = r.get("film_end_mm"), r.get("film_start_mm")
            if end_hi is not None and 0 <= end_hi - (pos + L) < pitch:
                hit = ("hi", end_hi - (pos + L))
            elif end_lo is not None and 0 <= pos - end_lo < pitch:
                hit = ("lo", pos - end_lo)
            if hit is None:
                continue
            side, gap = hit
            if (r.get("orientation") or "same") != state.orientation:
                side = "lo" if side == "hi" else "hi"
            end_beyond[n] = (w + L + gap) if side == "hi" else (w - gap)
            break
    needed = {n: "split" for n in missing}
    needed.update({n: "free_end" for n in free_end})
    needed.update({n: "unseen" for n in unseen})
    # Optional improvements (a near-whole copy that a whole supported one
    # would replace): with the strip end known beyond the image the end must
    # rest on plastic too, else only the window matters.
    needed.update({n: ("free_end" if n in end_beyond else "split") for n in optional})
    windows = dict(extra)
    windows.update(known)
    now = {n: _film110_now_text(n, report, windows, set(unseen)) for n in needed if n in windows}
    # How far the strip's end lies beyond each free-end image (None = not seen).
    gaps = {}
    for n in free_end:
        e, w = end_beyond.get(n), known.get(n)
        gaps[n] = (None if e is None or w is None
                   else (e - (w + L) if e >= w + L else w - e))
        for r in reversed(recs):                      # measured in the pass that saw it
            m = next((x.get("end_gap_mm") for x in r.get("images") or []
                      if x.get("number") == n and x.get("end_gap_mm") is not None), None)
            if m is not None:
                gaps[n] = float(m)
                break
    return rep.plan_next_pass(known, needed, ends, end_beyond, now, extra), gaps


def _film110_fill_record(record: dict, state: Film110State, rdir: Path,
                         prior: "list | None" = None) -> None:
    """Write the 110 fields of a digitize roll record: the pass, the
    placement label ("A"/"B"/"free"/null), whether it was given, the
    strip's orientation relative to pass 1, one `images` entry per
    numbered image, the refusals, the pass report in holder language
    (`pass_report`, `blanks`, `number_basis`) and `advice` over the WHOLE
    roll (the records before this run plus this run):

        {"missing": [n], "free_end": [n], "unseen": [n], "suggest": "<text>",
         "next": {...} | null, "not_done": [{"reason", "numbers"}],
         "done": bool, "images": sorted(missing + free_end),
         "other_placement": ...}

    `missing` = numbers seen only split and never delivered; `free_end` =
    numbers whose delivered copy is one with the strip end free (the sag
    rule, docs/film-110.md §4); `unseen` = frame positions between the
    strip's ends that no pass has seen; `next` = the ONE joint placement
    for the next pass (docs/film-110.md §15); `images` and
    `other_placement` are the older keys kept for the application ("A"/"B"
    swapped, null when the placement is "free" or undecided). `done` never
    refuses a run; it is information."""
    from . import digitize, film110, film110_report as rep
    record["film"] = "110"
    record["pass"] = state.pass_index
    record["placement"] = state.placement
    record["placement_given"] = bool(state.placement_given)
    record["orientation"] = state.orientation
    record["numbering"] = state.numbering_basis
    record["images"] = state.images + state.unidentified
    record["blanks"] = [dict(b) for b in state.blanks]
    record["pass_report"] = rep.build_pass_report(state.images + state.unidentified,
                                                  state.blanks)
    if state.aborted and state.numbering_basis == "unidentified":
        record["numbering_note"] = state.aborted
    record["problems"] = list(state.problems)
    # The strip's own ends, when seen (global mm): a later pass pins its
    # move on them, and the advice reads whether an end can reach plastic.
    record["film_start_mm"] = (None if state.film_start_mm is None
                               else round(float(state.film_start_mm), 2))
    record["film_end_mm"] = (None if state.film_end_mm is None
                             else round(float(state.film_end_mm), 2))
    prior = list(prior or [])
    roll, prefix = record.get("roll"), record.get("prefix", "")
    records = prior + [{"roll": roll, "prefix": prefix, "images": state.images}]
    numbers = sorted({e["number"] for r in records for e in r.get("images") or []
                      if e.get("number")})
    missing, free_end = [], []
    for n in numbers:
        got = digitize.delivered_image(records, roll, prefix, n)
        if got is None:
            if not (Path(rdir) / f"image{n}.tiff").exists():
                missing.append(n)
        elif got.get("free_end_near"):
            free_end.append(n)
    other = (None if state.placement in (None, "free") else
             "B" if state.placement == "A" else "A")
    unseen: list = []
    nxt = None
    not_done: list = []
    # The measured distance from a free strip end to the image beside it (the
    # newest measurement of each number).
    end_gaps: dict = {}
    for r in records:
        for e in r.get("images") or []:
            if e.get("number") is not None and e.get("end_gap_mm") is not None:
                end_gaps[str(e["number"])] = e["end_gap_mm"]
    positions_total: list = list(numbers)
    positions_have: list = [n for n in numbers if n not in missing]
    if state.strips == 1:
        # Numbers count frame positions from the strip's front end
        # (docs/film-110.md §15): "strip_start" once any pass has seen it.
        turned = state.orientation == "turned"
        sight = [(e["number"], float(e["position_mm"])) for e in state.images
                 if e.get("number") is not None and e.get("position_mm") is not None
                 and not e.get("uncertain")]
        sight += [(b["number"], float(b["position_mm"])) for b in state.blanks]
        front, rear = rep.strip_bounds(sight, (state.film_start_mm, state.film_end_mm), turned)
        anchored_prior = any(r.get("number_basis") == "strip_start" for r in prior)
        basis = "strip_start" if (anchored_prior or front is not None) else "provisional"
        record["number_basis"] = basis
        if front is not None and not anchored_prior and front != 1:
            record["anchor_shift"] = 1 - int(front)
        if rear is not None:
            record["rear_number"] = int(rear)
        rear_n = int(rear) if rear is not None else next(
            (r["rear_number"] for r in reversed(prior) if r.get("rear_number") is not None), None)
        blank_nums = ({b["number"] for r in prior for b in r.get("blanks") or []}
                      | {b["number"] for b in state.blanks})
        seen_all = set(numbers) | blank_nums
        universe: list = []
        if seen_all:
            lo = 1 if basis == "strip_start" else min(seen_all)
            hi = max(seen_all | ({rear_n} if rear_n is not None else set()))
            universe = list(range(lo, hi + 1))
            for m in universe:
                if m in seen_all:
                    continue
                if not (Path(rdir) / f"image{m}.tiff").exists():
                    unseen.append(m)
        positions_total = [m for m in universe if m not in blank_nums]
        positions_have = [m for m in positions_total
                          if digitize.delivered_image(records, roll, prefix, m) is not None
                          or (Path(rdir) / f"image{m}.tiff").exists()]
        done = bool(numbers) and not missing and not free_end and not unseen
        positions = [e["position_mm"] for r in records for e in r.get("images") or []
                     if e.get("position_mm") is not None]
        at_left = bool(positions) and min(positions) <= film110.FILM_110.pitch_mm
        at_right = bool(positions) and (max(positions) + film110.FILM_110.image_mm[0]
                                        >= 6 * film110.STRIP.pitch_mm - film110.FILM_110.pitch_mm)
        if done:
            # An end wall hides what lies beyond it: a strip whose outermost
            # image lies within a film pitch of the holder's end needs no
            # sighting of that end (Test 104). A front end never seen leaves
            # the numbers provisional.
            done = (basis == "strip_start" or at_left) and (rear_n is not None or at_right)
        if not done:
            if not numbers:
                not_done.append({"reason": "no_image", "numbers": []})
            else:
                if basis != "strip_start" and not at_left:
                    not_done.append({"reason": "front_end_not_seen", "numbers": []})
                if rear_n is None and not at_right:
                    not_done.append({"reason": "rear_end_not_seen", "numbers": []})
                if missing:
                    not_done.append({"reason": "seen_only_split", "numbers": list(missing)})
                if free_end:
                    not_done.append({"reason": "strip_end_free", "numbers": list(free_end)})
                if unseen:
                    not_done.append({"reason": "not_seen", "numbers": list(unseen)})
        # Near-whole copies that are the best copy and supported: delivered and
        # counted for `done`, but a whole supported copy would be better.
        optional = []
        if not (missing or free_end or unseen):
            for n in numbers:
                got = digitize.delivered_image(records, roll, prefix, n)
                if got is not None and got.get("cut_mm") is not None and not got.get("free_end_near"):
                    optional.append((n, got))
        nxt, gaps = _film110_plan(state, prior, record["pass_report"], missing, free_end,
                                  unseen, seen_all, sight, turned,
                                  tuple(n for n, _g in optional))
        if nxt is not None and optional and not (missing or free_end or unseen):
            got = dict(optional)
            hit = [n for n in nxt["rescues"] if n in got]
            if hit:
                n = hit[0]
                e = got[n]
                under = e.get("under") or "plastic"
                under = ("the " + under) if under.startswith("end wall") else under
                nxt["optional"] = True
                nxt["text"] = (f"Optional: move image {n} whole into an aperture; this would "
                               f"replace its near-whole copy ({e.get('cut_mm', 0):.2f} mm under "
                               f"{under}) with a whole, supported one. Keep at least one image "
                               f"already scanned in an aperture, so that the pass can recognise "
                               f"the strip.")
            else:
                nxt = None
        why_none: list = []
        unid = state.numbering_basis == "unidentified"
        if nxt is None and (missing or free_end or unseen) and not unid:
            # Why no placement helps, from the geometry: an image delivered
            # with the strip end free can only be rescued with the end resting
            # on plastic, which needs the end at least the aperture-edge margin
            # (a bar is as wide) beyond the image.
            for n in free_end:
                g = gaps.get(n)
                if g is None:
                    why_none.append(("free_end_unmeasured", n, None))
                elif g < rep.FREE_END_EDGE_MIN_MM:
                    why_none.append(("free_end_too_close", n, g))
                else:
                    why_none.append(("no_placement", n, None))
            for n in list(missing) + list(unseen):
                why_none.append(("no_placement", n, None))
            for code in dict.fromkeys(c for c, _n, _g in why_none):
                nums = [n for c, n, _g in why_none if c == code]
                for item in not_done:
                    if item.get("reason") == "strip_end_free" and code != "no_placement":
                        item["detail"] = code
                not_done.append({"reason": code, "numbers": nums})
        if unid:
            suggest = ("numbering was not established in this pass (its whole images "
                       "were written unnumbered): seat the strip so that an image "
                       "seen before is seen again")
        elif nxt is not None:
            suggest = nxt["text"]
        elif not numbers:
            suggest = ("no image found: check that the film lies in the apertures "
                       "and that its printed borders and the edge with the small "
                       "holes are visible, then seat the strip again and run the "
                       "roll again")
        elif missing or free_end or unseen:
            bits = []
            for code, n, g in why_none:
                if code == "free_end_too_close":
                    bits.append(f"image {n} begins {g:.1f} mm from the strip's end: no "
                                f"placement keeps it whole with the end resting on plastic")
                elif code == "free_end_unmeasured":
                    bits.append(f"the strip's end beyond image {n} was always under "
                                f"plastic, so its distance is not known; a pass with "
                                f"that end visible in an aperture will measure it")
                else:
                    bits.append(f"no placement within the holder brings image {n} "
                                f"whole into one aperture")
            suggest = ("; ".join(bits) + ": accept the delivered copies, or stop")
        else:
            suggest = "nothing missing; every delivered image is supported"
    else:
        suggest = _film110_suggest(state, prior, missing, free_end)
        # `done`: what digitize.roll_is_done will say once this record is
        # appended -- at least one image numbered in some pass, nothing
        # missing, nothing with the strip end free, AND no room for an image
        # the driver has never seen: each end of the strip must have been
        # seen in some pass, unless the strip's outermost image on that side
        # lies within a film pitch of the holder's end wall (then the end is
        # under the wall and nothing can lie beyond it). Test 104, load 2:
        # strip 2's first photograph was covered by strip 1's cassette end in
        # pass 1, the roll was declared done with three of four delivered.
        done = bool(numbers) and not missing and not free_end
        if done:
            pitch = film110.FILM_110.pitch_mm
            length = film110.FILM_110.image_mm[0]
            holder_end = 6 * film110.STRIP.pitch_mm
            positions = [e["position_mm"] for r in records for e in r.get("images") or []
                         if e.get("position_mm") is not None]
            start_seen = any(r.get("film_start_mm") is not None for r in records) \
                or state.film_start_mm is not None
            end_seen = any(r.get("film_end_mm") is not None for r in records) \
                or state.film_end_mm is not None
            left_wall = (state.strips != 2 or state.slot == 1)
            right_wall = (state.strips != 2 or state.slot == 2)
            if positions:
                at_left = left_wall and min(positions) <= pitch
                at_right = right_wall and max(positions) + length >= holder_end - pitch
                done = (start_seen or at_left) and (end_seen or at_right)
    record["advice"] = {"missing": missing, "free_end": free_end,
                        "unseen": unseen,
                        "suggest": suggest,
                        "next": ({k: v for k, v in nxt.items() if k != "text"}
                                 if nxt else None),
                        "not_done": not_done,
                        "end_gaps": end_gaps,
                        "frame_positions": {"delivered": len(positions_have),
                                            "of": len(positions_total),
                                            "needed": sorted(set(positions_total) - set(positions_have))},
                        "done": done,
                        "images": sorted(set(missing) | set(free_end)),
                        "other_placement": other}
    if state.strips == 2:
        record["placement_source"] = state.placement_source


def _film110_roll_guard(args: argparse.Namespace, roll: int, rdir: Path,
                        check_done: bool = True) -> "str | None":
    """Before any hardware: may this 110 run write into `rdir`? None = yes.
    A 110 roll takes as many runs as it needs (docs/film-110.md §11, §13);
    the guard refuses only a roll directory that holds output which is not
    a recorded 110 run. Neither the placement nor "done" refuses a run."""
    from . import digitize
    # A roll recorded done is NOT refused (`check_done` is kept for the
    # signature only): the bookkeeping serves the operator, not the other
    # way round (Christian, Test 104, load 2: "roll 3 pass 4" must never
    # stop a strip from being scanned). A pass on a done roll keeps every
    # copy and changes nothing.
    if not digitize.roll_dir_has_output(args.out, args.prefix, roll):
        return None
    recs = digitize.roll_records(args.out, roll, args.prefix)
    if not recs or recs[-1].get("film") != "110":
        return (f"roll {roll} already has output in {rdir} that is not a "
                f"recorded 110 run; use --roll for a different one, or "
                f"--force to redo it")
    return None


def _film110_pair_guard(args: argparse.Namespace, roll: int) -> "str | None":
    """Two-strip runs (§12) own the pair of rolls N, N+1. Before hardware:
    a roll that already has records must be one half of a two-strip pair
    with each other as companion, otherwise this run would mix a lone
    strip's roll into a pair."""
    from . import digitize
    for r, other, slot in ((roll, roll + 1, 1), (roll + 1, roll, 2)):
        recs = digitize.roll_records(args.out, r, args.prefix)
        if not recs:
            continue
        last = recs[-1]
        if (last.get("strips") != 2 or last.get("slot") != slot
                or last.get("companion_roll") != other):
            return (f"roll {r} was not recorded as slot {slot} of a two-strip "
                    f"pair with roll {other}; use --roll for a different pair, "
                    f"or --force to redo both rolls")
    return None


def _film110_run_order_placement(args: argparse.Namespace, roll: int) -> str:
    """The placement label of a first-pass two-strip run when slot 1 could
    not decide it (no image numbered, none given): "A" when roll N has no
    110 record yet, else "B" -- the run order."""
    from . import digitize
    recs = [r for r in digitize.roll_records_current(args.out, roll, args.prefix)
            if r.get("film") == "110"]
    return "B" if recs else "A"


def _film110_set_pass(state: Film110State, pass_index: int, prior: list) -> None:
    """Which pass of the roll `state` is, and the record it is numbered
    against (the newest of `prior` that numbered an image). Shared by the
    live digitize run and the rebuild from stored frames."""
    state.pass_index = pass_index
    state.ref = _film110_pick_ref(prior)


def _film110_feed_aperture(args: argparse.Namespace, state: Film110State,
                           frame: int, vis, irr, emit) -> None:
    """One aperture-registered crop into the shared 110 code. `irr` is the
    IR crop or None; it is only used with --ir. The live digitize hook and
    the rebuild (`--from-frames`) both come through here."""
    _film110_process_aperture(args, state, frame, vis,
                              irr if args.ir else None, emit)


_NOT_DONE_TEXT = {
    "no_image": "no image was found",
    "front_end_not_seen": "the strip's front end has not been seen: seat it so that "
                          "its first frame lies in aperture 1",
    "rear_end_not_seen": "the strip's rear end has not been seen: seat it so that "
                         "its last frame lies inside an aperture",
}


def _film110_not_done_text(adv: dict) -> str:
    """The reasons a roll is not done, from advice.not_done."""
    bits = []
    for item in adv.get("not_done") or []:
        r, nums = item["reason"], item.get("numbers") or []
        lst = ", ".join(f"image {n}" for n in nums)
        if r in _NOT_DONE_TEXT:
            bits.append(_NOT_DONE_TEXT[r])
        elif r == "seen_only_split":
            bits.append(f"{lst} seen only split by a bar")
        elif r == "strip_end_free":
            bits.append(f"{lst} delivered with the strip end free")
        elif r in ("free_end_too_close", "free_end_unmeasured", "no_placement"):
            continue
        elif r == "not_seen":
            bits.append("frame position " + ", ".join(str(n) for n in nums)
                        + " between the strip's ends has not been seen")
    return "; ".join(bits)


def _film110_print_roll_summary(roll: int, state: Film110State, per_frame: list,
                                rdir, adv: dict, note: str = "",
                                record: "dict | None" = None) -> None:
    """The closing report of a single-strip 110 run (live or rebuilt): the
    convention legend, the pass report in holder language, what has been
    delivered, and either "done" or why not plus the ONE next placement
    (docs/film-110.md section 15)."""
    from . import film110_report as rep
    pl = state.placement or "undecided"
    record = record or {}
    report = record.get("pass_report") or []
    print(rep.legend())
    head = f"Pass {state.pass_index} report"
    if record.get("number_basis") == "provisional":
        head += (" (the numbers are provisional: they count from the strip's "
                 "front end once it has been seen)")
    print(head + ":")
    for ln in rep.format_pass_report(report):
        print(ln)
    if not report:
        print("  no frame position found in this pass")
    fp = adv.get("frame_positions")
    if fp:
        need = ", ".join(f"image {n}" for n in fp["needed"])
        print(f"Delivered after this pass: {fp['delivered']} of {fp['of']} frame "
              f"positions" + (f"; still needed: {need}" if need else "") + ".")
    if adv.get("done"):
        nxt = (f"Every frame position between the strip's ends is accounted for "
               f"(delivered or blank). Next strip: roll {roll + 1}."
               + (f" {adv['suggest']}" if (adv.get("next") or {}).get("optional") else ""))
    else:
        why = _film110_not_done_text(adv)
        wants = adv.get("next") or adv.get("images") or adv.get("unseen") \
            or not adv.get("frame_positions", {}).get("of")
        sug = adv["suggest"].rstrip(".")
        sug = sug[:1].upper() + sug[1:]
        nxt = ((f"Not done: {why}. " if why else "")
               + (f"{sug}. " if wants else "")
               + f"Re-seat the strip (slide or turn it) and run "
                 f"'of135i digitize --film 110 --roll {roll}' again, or stop here.")
    word = "done" if adv.get("done") else "scanned, not done"
    print(f"roll {roll} {word} (110, placement {pl}): {len(per_frame)} "
          f"frames, {sum(1 for e in state.images if e['main'])} "
          f"photograph(s) -> {rdir}{note}. {nxt}")


class _LinePrefixer:
    """A write-only text stream that prefixes every line with `prefix`
    (used to mark a rebuilt pass's output)."""

    def __init__(self, target, prefix: str):
        self._t, self._p, self._at_start = target, prefix, True

    def write(self, text: str) -> int:
        for part in text.splitlines(keepends=True):
            if self._at_start:
                self._t.write(self._p)
            self._t.write(part)
            self._at_start = part.endswith("\n")
        return len(text)

    def flush(self) -> None:
        self._t.flush()


_FILM110_PRODUCT_RE = re.compile(
    r"^(image-?\d+(-uncertain)?(-ir)?|p\d+-a\d+-\d+(-ir)?)\.tiff$")


def _film110_move_products(rdir: Path, stamp: str) -> "tuple[Path | None, list]":
    """Move every photograph product (image<N>[-uncertain][-ir].tiff) of the
    roll into <rdir>/superseded-<stamp>/ (os.replace, same filesystem).
    Nothing is deleted. Returns (the directory or None, the names moved)."""
    names = sorted(p.name for p in Path(rdir).iterdir()
                   if p.is_file() and _FILM110_PRODUCT_RE.match(p.name))
    if not names:
        return None, []
    dest = Path(rdir) / f"superseded-{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = Path(rdir) / f"superseded-{stamp}-{n}"
    dest.mkdir()
    for name in names:
        os.replace(Path(rdir) / name, dest / name)
    return dest, names


def _digitize_from_frames(args: argparse.Namespace) -> int:
    """`digitize --film 110 --roll N --from-frames`: rebuild a roll's
    photographs, numbering and manifest records from its stored frame files
    (p<k>-f<N>.tiff, -ir.tiff) without scanning (docs/film-110.md §14).
    Every stored pass is replayed in order through the SAME 110 code a live
    pass uses; the roll's current photographs are first MOVED into
    superseded-<UTC>/ (never deleted). Opens no Scanner, touches no USB.

    Exit 0 ok, 2 refused (nothing touched), 5 when a pass cannot be
    recognised (that pass writes nothing, earlier passes stay)."""
    import numpy as _np
    import tifffile
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from . import digitize

    def refuse(msg: str) -> int:
        print(f"error: {msg}", file=sys.stderr)
        return 2

    if getattr(args, "film", "135") != "110":
        return refuse("--from-frames only applies with --film 110")
    if args.roll is None:
        return refuse("--from-frames needs --roll N")
    if int(getattr(args, "strips", 1) or 1) != 1:
        return refuse("--from-frames does not support --strips 2 in this "
                      "version (a two-strip load cannot be rebuilt offline yet)")
    roll = args.roll
    rdir = digitize.roll_dir(args.out, args.prefix, roll)
    recs = [r for r in digitize.roll_records_current(args.out, roll, args.prefix)
            if r.get("film") == "110"]
    if not recs:
        return refuse(f"roll {roll} has no 110 records in {args.out}")
    if any((r.get("strips") or 1) != 1 for r in recs):
        return refuse(f"roll {roll} was recorded as part of a two-strip load; "
                      f"--from-frames does not support --strips 2 in this version")
    for key in ("dpi", "ir"):
        vals = {(bool(r.get(key)) if key == "ir" else r.get(key)) for r in recs}
        if len(vals) != 1 or None in vals:
            return refuse(f"roll {roll}'s stored records disagree on {key} "
                          f"({sorted(map(str, vals))}); a rebuild needs one value")
    dpi, ir = recs[0]["dpi"], bool(recs[0].get("ir"))

    by_pass: dict = {}
    for r in recs:
        k = r.get("pass")
        if isinstance(k, int):
            by_pass[k] = r              # the newest record of a pass wins
    for tag in digitize.film110_placements_on_disk(rdir):
        if len(tag) > 1 and tag[0] == "p" and tag[1:].isdigit():
            by_pass.setdefault(int(tag[1:]), None)   # a crash left frames, no record
    plan = []
    for k in sorted(by_pass):
        frames = [f for f in digitize.FRAMES
                  if digitize.frame_path_110(args.out, args.prefix, roll,
                                             f"p{k}", f).is_file()]
        if frames:
            plan.append((k, by_pass[k], frames))
    if not plan:
        return refuse(f"roll {roll} has no stored frame files (p<k>-f<N>.tiff) "
                      f"in {rdir}; nothing to rebuild from")

    # ---- validated. The replay is ATOMIC for the roll: products are written
    # into a staging directory inside the roll directory and the records are
    # collected in memory. Only when every pass succeeded are the current
    # photographs moved to superseded-<stamp>/, the staged ones moved into
    # place and the records appended. A stop (exit 5) or an exception leaves
    # the roll's photographs and its current epoch untouched; ONE failed
    # record (no new epoch, no pass of its own) says why, and the staging
    # directory is kept for inspection.
    #
    # A rebuild may also have to run twice: when the strip's front end is
    # first seen in a pass after the first, the numbers of the passes before
    # it were provisional; the replay then restarts with the shift that makes
    # the first frame position number 1 (docs/film-110.md section 15).
    from datetime import datetime, timezone
    shift = int(getattr(args, "_anchor_shift", 0) or 0)
    attempt = 0
    while True:
        attempt += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stage = rdir / f".rebuild-{stamp}" if attempt == 1 else rdir / f".rebuild-{stamp}-{attempt}"
        n = 1
        while stage.exists():
            n += 1
            stage = rdir / f".rebuild-{stamp}-{attempt}-{n}"
        try:
            rc, last, restart, records, why, unident = _rebuild_attempt(
                args, roll, rdir, plan, recs[0], dpi, ir, shift,
                attempt == 1 and not shift, stage)
        except Exception as e:
            rc, last, restart, records, unident = 1, None, None, [], []
            why = f"{type(e).__name__}: {e}"
        if restart is not None:
            shutil.rmtree(stage, ignore_errors=True)    # our own scratch, nothing of the roll's
            shift = restart
            print(f"=== the strip's front end is first seen in a later pass: "
                  f"renumbering roll {roll} from pass 1 (shift {shift:+d}) ===")
            continue
        break
    if rc != 0 or last is None:
        now = datetime.now(timezone.utc).isoformat()
        digitize.append_manifest(args.out, {
            "roll": roll, "prefix": args.prefix, "dir": str(rdir), "film": "110",
            "dpi": dpi, "ir": ir, "status": "failed", "stage": "rebuild",
            "source": "frames", "stop_reason": why, "images": [],
            "staging_dir": str(stage), "rebuilt_utc": now, "finished_utc": now})
        print(f"error: the rebuild of roll {roll} stopped: {why}. The roll's "
              f"photographs and records are unchanged; the partial result is in "
              f"{stage} (kept for inspection).", file=sys.stderr)
        return rc or 1
    # Commit: nothing below can be undone by a failure above.
    dest, moved = _film110_move_products(rdir, stamp)
    if moved:
        print(f"moved {len(moved)} existing photograph file(s) to {dest.name}/ "
              f"(nothing is deleted)")
    for f in sorted(stage.iterdir()):
        os.replace(f, rdir / f.name)
    try:
        stage.rmdir()
    except OSError:
        pass
    for record in records:
        digitize.append_manifest(args.out, record)
    state, record, per_frame = last
    _film110_print_roll_summary(roll, state, per_frame, rdir, record["advice"],
                                record=record)
    if unident:
        print(f"roll {roll}: pass(es) {', '.join(map(str, unident))} have whole "
              f"photographs that could not be numbered; they were written "
              f"UNNUMBERED (p<pass>-a<aperture>-<i>.tiff), everything else is "
              f"numbered and rebuilt.", file=sys.stderr)
        return 5
    return 0


def _rebuild_attempt(args, roll, rdir, plan, default_rec, dpi, ir, shift, may_restart, stage):
    """One replay of the stored passes into `stage` (see
    `_digitize_from_frames`). Writes nothing in the roll directory and
    appends nothing to the manifest. Returns (rc, last, restart_shift,
    records, why): `restart_shift` is not None when a pass reported that the
    front end was first seen there and the replay must start over with that
    shift (only when `may_restart`); `why` says what stopped it (rc 5)."""
    import numpy as _np
    import tifffile
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from . import digitize

    print(f"=== rebuild roll {roll} from stored frames -> {rdir} "
          f"(passes {', '.join(str(k) for k, _r, _f in plan)}; staging in {stage.name}) ===")
    stage.mkdir()

    prior: list = []                    # the rebuilt records so far (a new epoch)
    last = None                         # (state, record, per_frame)
    unidentified: list = []             # passes whose numbering could not be established
    for k, rec, frames in plan:
        src = rec or default_rec
        rotate = int(src.get("rotate") or 0)
        pargs = SimpleNamespace(**vars(args))
        pargs.dpi, pargs.ir, pargs.rotate = dpi, ir, rotate
        pargs.no_clean = bool(src.get("no_clean"))
        pargs.film, pargs.strips, pargs.frames = "110", 1, None
        placement = (rec.get("placement") if rec and rec.get("placement_given")
                     and rec.get("placement") in ("A", "B") else
                     getattr(args, "placement", None))
        state = Film110State(placement, slot=1, strips=1)
        if shift:
            state.number_shift, state.shift_decided, state.shift_forced = shift, True, True
        _film110_set_pass(state, k, prior)
        emit = _film110_digitize_emit(pargs, state, roll, stage=stage,
                                      records_fn=lambda: list(prior))
        started = datetime.now(timezone.utc).isoformat()
        per_frame = []
        out = _LinePrefixer(sys.stdout, f"rebuild pass {k}: ")
        with contextlib.redirect_stdout(out):
            for frame in frames:
                path = digitize.frame_path_110(args.out, args.prefix, roll,
                                               f"p{k}", frame)
                vis = tifffile.imread(str(path))
                if rotate:
                    vis = _np.ascontiguousarray(_np.rot90(vis, -(rotate // 90)))
                irr = None
                irp = path.with_name(path.stem + "-ir.tiff")
                if ir and irp.is_file():
                    irr = tifffile.imread(str(irp))
                    if irr.ndim == 3:
                        irr = irr[:, :, 0]      # stored as the IR channel stacked x3
                    if rotate:
                        irr = _np.ascontiguousarray(_np.rot90(irr, -(rotate // 90)))
                _film110_feed_aperture(pargs, state, frame, vis, irr, emit)
                per_frame.append({"frame": frame, "main": str(path),
                                  "ir": str(irp) if irr is not None else None,
                                  "stage": None})
                del vis, irr
                if state.aborted:
                    print(f"stopping: {state.aborted}")
                    break
            if not state.aborted:
                _film110_finalize(pargs, state, roll, emit)
        if rec and rec.get("per_frame"):
            per_frame = [dict(e) for e in rec["per_frame"]]
        now = datetime.now(timezone.utc).isoformat()
        record = {"roll": roll, "prefix": args.prefix, "dir": str(rdir),
                  "started_utc": started, "dpi": dpi,
                  "positive": bool(src.get("positive")), "ir": ir,
                  "frames": frames, "no_clean": pargs.no_clean, "rotate": rotate}
        if not prior:
            record["forced"] = True        # a new epoch: only the rebuild counts
        _film110_fill_record(record, state, stage, prior)
        if state.aborted:
            unidentified.append(k)             # written unnumbered; the rebuild goes on
        else:
            if state.unidentified:
                unidentified.append(k)         # whole images off the numbering, unnumbered
            if may_restart and record.get("anchor_shift"):
                return 0, last, int(record["anchor_shift"]), prior, None, unidentified
        record.update(per_frame=per_frame, finished_utc=now, status="ok", stage=None,
                      source="frames", from_pass=k, rebuilt_utc=now)
        prior.append(record)
        last = (state, record, per_frame)
    return 0, last, None, prior, None, unidentified


# ------------------------------------------------ whole-strip sweep (digitize)
#
# docs/whole-strip-sweep-design.md section 6. One calibration, one
# positioning, one pass, one park into <roll>/sweep.raw; the frames are cut
# in software and every cut goes through the EXISTING per-frame writer
# (_finish_digitize_frame), so nothing about a product differs from the
# per-frame cycle. Implemented offline; not yet hardware-verified. The
# dual sweep (IR on, or any dpi other than 3600) has run on hardware; the
# plain-3600 sweep (--no-ir, black-and-white film) is implemented offline
# only and awaits its first hardware run (design doc, "Hardware plan").


def _sweep_validate(args: argparse.Namespace) -> "tuple[list[int] | None, str | None]":
    """Every --sweep argument check, BEFORE any hardware step (exit 2).
    Returns (frames, None) or (None, message)."""
    if not getattr(args, "sweep", False):
        return None, "--keep-sweep and --from-sweep need --sweep"
    if getattr(args, "film", "135") != "135":
        return None, "--sweep is for 135 only; --film 110 keeps the per-frame cycle"
    if getattr(args, "from_frames", False):
        return None, "--from-frames (110) cannot be combined with --sweep"
    if int(getattr(args, "strips", 1) or 1) != 1:
        return None, "--sweep does not support --strips 2"
    if getattr(args, "placement", None) is not None:
        return None, "--placement only applies with --film 110"
    try:
        frames = _parse_frames(getattr(args, "frames", None) or "1-4")
    except ValueError as e:
        return None, str(e)
    if frames != list(range(frames[0], frames[-1] + 1)):
        return None, ("--sweep needs --frames to be one contiguous range a-b "
                      f"(got {args.frames!r}): the pass covers every aperture "
                      f"between its first and last")
    if getattr(args, "from_sweep", False) and args.roll is None:
        return None, "--from-sweep needs --roll N"
    try:
        from .device import dual_tables
        dual = bool(args.ir or args.dpi != 3600)
        t = dual_tables(args.dpi) if dual else tables
        Scanner._sweep_geometry(t, frames[0], frames[-1],
                                getattr(args, "overscan", None) or holder.OVERSCAN_MM)
    except (safety.FrameOutOfRangeError, safety.FeedlOutOfRangeError, ValueError) as e:
        return None, f"the sweep window is not valid: {e}"
    return frames, None


def _sweep_meta(geom, width: int, dpi: int, overscan_mm: float,
                dual: bool = True) -> dict:
    """The sweep's geometry as sweep.json stores it (and the window finder
    reads it back, see _sweep_geom_from_meta)."""
    return {
        "first": geom.first, "last": geom.last, "dpi": dpi, "feedl": geom.feedl,
        "end_hwdpi": geom.end_hwdpi, "wire_lines": geom.wire_lines,
        "chunk_count": geom.chunks, "overscan_mm": overscan_mm, "width": width,
        "lines_per_mm": geom.lines_per_mm,
        "dual": bool(dual), "wire_per_vis": 2 if dual else 1,
        "predicted_edges": {str(k): list(v) for k, v in geom.predicted_edges.items()},
    }


def _sweep_geom_from_meta(meta: dict):
    from types import SimpleNamespace
    return SimpleNamespace(
        first=int(meta["first"]), last=int(meta["last"]),
        lines_per_mm=float(meta["lines_per_mm"]),
        predicted_edges={int(k): tuple(v) for k, v in meta["predicted_edges"].items()})


def _sweep_kept_note(raw_path: Path) -> None:
    """Whenever sweep.raw stays on disk, say so in plain words with its
    size and path (design doc section 6)."""
    try:
        gb = raw_path.stat().st_size / 1e9
    except OSError:
        return
    print(f"sweep.raw kept ({gb:.1f} GB): {raw_path} — delete it when the "
          f"strip is accepted, or keep it for --from-sweep", file=sys.stderr)


def _sweep_resolve(raw_path: Path, meta: dict):
    """Memory-map sweep.raw and run the window finder. Returns (mm, res)."""
    from . import sweep as _sweep
    mm = _sweep.open_raw(raw_path, int(meta["width"]))
    # sweep.json files written before the plain sweep existed carry no
    # "dual" key: they are dual sweeps.
    dual = bool(meta.get("dual", True))
    if dual:
        prof = _sweep.ir_profile(mm, dpi=int(meta["dpi"]))
    else:
        prof = _sweep.visible_profile(mm, dpi=int(meta["dpi"]))
    res = _sweep.find_windows(prof, _sweep_geom_from_meta(meta), dpi=int(meta["dpi"]),
                              overscan_mm=float(meta["overscan_mm"]),
                              wire_per_vis=int(meta.get("wire_per_vis", 2 if dual else 1)))
    return mm, res


def _sweep_cut(args: argparse.Namespace, rdir: Path, mm, res, meta: dict,
               scanlike, per_frame: list, *, rebuild: bool) -> list:
    """Cut every aperture of a resolved sweep through _finish_digitize_frame
    and fill ``per_frame`` (appended to as it goes, so a failure part-way
    leaves a record). Returns the frames whose coverage did not verify.

    Calibration data: ``f<first>.cal.npz`` is written once by the first
    sidecar and HARD-LINKED (copy if linking fails) as ``fN.cal.npz`` for
    every other frame, so each sidecar names its own file exactly as the
    per-frame path does (design doc section 6). On a rebuild the existing
    ``fN.cal.npz`` files are kept and referenced."""
    from . import digitize, sweep as _sweep
    width = int(meta["width"])
    first = int(meta["first"])
    incomplete: list[int] = []
    first_cal = diag.cal_data_path(str(digitize.frame_path(args.out, args.prefix,
                                                           args.roll_n, first)))
    for k in range(first, int(meta["last"]) + 1):
        out = str(digitize.frame_path(args.out, args.prefix, args.roll_n, k))
        entry: dict = {"frame": k, "main": None, "ir": None, "preview": None,
                       "dust_cleaned": None, "stage": "write"}
        per_frame.append(entry)
        if not res.resolved:
            entry.update(stage=None, coverage_verified=False,
                         coverage_reason=f"sweep unresolved: {res.reason}")
            incomplete.append(k)
            continue
        w = res.windows[k]
        entry["located_by"] = w.located_by
        print(f"cutting frame {k} from the sweep (wire lines {w.wire_start}-{w.wire_end}, "
              f"located by {w.located_by})")
        raw = _sweep.window_bytes(mm, w)
        main, irf, prev, cleaned, cov = _finish_digitize_frame(
            args, raw, width, out, bool(meta.get("dual", True)), progress=entry)
        entry.update(main=main, ir=irf, preview=prev, dust_cleaned=cleaned)
        film = None
        if cov is not None:
            entry["coverage_verified"] = bool(cov.verified)
            entry["coverage_reason"] = cov.reason
            entry["coverage_source"] = cov.source
            if not cov.verified:
                incomplete.append(k)
            elif "film_present" in entry:
                film = {"present": entry["film_present"],
                        "saturated_share": entry["saturated_share"]}
                if not film["present"]:
                    entry["coverage_reason"] = "empty aperture"
        del raw
        entry["stage"] = "diag"
        extra = {"sweep_window": {"aperture": k, "wire_start": w.wire_start,
                                  "wire_end": w.wire_end, "located_by": w.located_by},
                 "sweep": (scanlike.last_diag or {}).get("sweep") or meta,
                 "cal_data_shared": True}
        if k != first or rebuild:
            own = diag.cal_data_path(out)
            if not rebuild and not getattr(args, "no_diag", False) and os.path.exists(first_cal):
                if os.path.exists(own):
                    os.unlink(own)
                try:
                    os.link(first_cal, own)
                except OSError:
                    shutil.copy2(first_cal, own)
            extra["cal_data"] = own if os.path.exists(own) else None
        kw = {"film": film} if film is not None else {}
        _write_diag_sidecar(args, scanlike, out, k, coverage=cov, extra=extra, **kw)
        scanlike.last_cal_data = None       # written once, next to frame `first`
        d = scanlike.last_diag or {}
        entry.update(stage=None, calibration=d.get("calibration"),
                     gain_codes=d.get("gain_codes"), offset_codes=d.get("offset_codes"),
                     dark_b_substituted=d.get("dark_b_substituted"))
    return incomplete


def _sweep_record_fields(meta: dict, res, diag_d: dict, raw_kept: bool) -> dict:
    ps = (diag_d or {}).get("phase_seconds") or {}
    return {"sweep": {
        "first": meta["first"], "last": meta["last"], "offset_mm": res.offset_mm,
        "resolved": res.resolved, "reason": res.reason,
        "located_by": {str(k): w.located_by for k, w in res.windows.items()},
        "seconds": {
            "position": ps.get("position"), "scan": ps.get("scan"),
            "park": ps.get("park"),
            "calibration": sum(v for n, v in ps.items() if n.startswith("cal_")),
            "total": (diag_d or {}).get("total_seconds"),
        },
        "raw_kept": raw_kept,
    }}


def _digitize_sweep(args: argparse.Namespace) -> int:
    """`digitize --sweep`: see the block comment above and
    docs/whole-strip-sweep-design.md. Exit 0 ok, 2 refused before any
    hardware, 4 coverage/resolution failure (products absent or partial,
    sweep.raw kept), other codes as the per-frame flow."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from . import digitize, loadflow

    frames, err = _sweep_validate(args)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if getattr(args, "from_sweep", False):
        return _digitize_from_sweep(args, frames)
    first, last = frames[0], frames[-1]
    roll = args.roll if args.roll is not None else digitize.next_roll(args.out, args.prefix)
    args.roll_n = roll
    rdir = digitize.roll_dir(args.out, args.prefix, roll)
    raw_path = rdir / digitize.SWEEP_RAW_NAME
    json_path = rdir / digitize.SWEEP_JSON_NAME
    if not args.force and (digitize.roll_dir_has_output(args.out, args.prefix, roll)
                           or digitize.roll_is_done(args.out, roll, args.prefix)):
        print(f"error: roll {roll} already has output in {rdir} (or is recorded "
              f"done); use --roll for a different one, or --force to redo it.",
              file=sys.stderr)
        return 2
    print(f"=== digitize --sweep roll {roll} -> {rdir} (apertures {first}-{last}) ===")
    if args.force:
        removed = digitize.clear_roll_outputs(args.out, args.prefix, roll)
        if removed:
            print(f"--force: removed {len(removed)} existing file(s) in {rdir} "
                  f"before re-scanning")
    args.eject = True
    started = datetime.now(timezone.utc).isoformat()
    record = {"roll": roll, "prefix": args.prefix, "dir": str(rdir),
              "started_utc": started, "dpi": args.dpi, "positive": args.positive,
              "ir": args.ir, "frames": frames, "no_clean": args.no_clean,
              "rotate": args.rotate}

    def record_failed(stage: str, **extra) -> None:
        try:
            record.update(status="failed", stage=stage,
                          finished_utc=datetime.now(timezone.utc).isoformat(), **extra)
            digitize.append_manifest(args.out, record)
        except Exception as me:
            log.warning("manifest write failed while recording a failed roll "
                        "(original error preserved): %s", me)

    def keep_note() -> None:
        if raw_path.exists():
            _sweep_kept_note(raw_path)

    if not args.assume_loaded:
        rc = loadflow.run(ask=input)
        if rc == EXIT_BUSY:
            return _busy_not_started(roll)
        if rc != 0:
            record_failed("load", load_rc=rc)
            print(f"load failed (rc {rc}); roll {roll} recorded as failed. "
                  f"Power-cycle before retrying.", file=sys.stderr)
            return rc
    rdir.mkdir(parents=True, exist_ok=True)

    state: dict = {}

    def body(scanner: Scanner) -> int:
        scanner.park_mode = args.park
        scanner.recalibrate = bool(getattr(args, "recalibrate", False))
        if getattr(args, "warmup_budget", None) is not None:
            scanner.warmup_budget_s = float(args.warmup_budget)
        scanner.check_start_state()
        if not scanner.is_magazine_present():
            print("error: no magazine detected after load", file=sys.stderr)
            return 1
        dual = bool(args.ir or args.dpi != 3600)    # the per-frame rule
        scanner.initialize(ir=dual, dpi=args.dpi)
        log.info("sweeping apertures %d-%d @ %d dpi (one pass, %s)", first, last,
                 args.dpi, "dual" if dual else "plain")
        overscan = getattr(args, "overscan", None) or holder.OVERSCAN_MM
        with open(raw_path, "wb") as fh:
            width, smeta = scanner.sweep(first, last, dpi=args.dpi,
                                         overscan_mm=overscan, sink=fh.write, ir=dual)
        d = dict(scanner.last_diag or {})
        meta = _sweep_meta(smeta["geometry"], width, args.dpi, overscan, dual=dual)
        state.update(meta=meta, diag=d, cal=scanner.last_cal_data)
        _sweep_write_json(json_path, meta, None, d)
        print(f"sweep done: {raw_path.stat().st_size / 1e9:.2f} GB written")
        # Eject now: the carriage is parked and the scanner idle; the
        # post-processing below takes minutes and the scanner drops USB
        # after ~5 min of standby.
        scanner.eject()
        print("ejected")
        return 0

    try:
        rc = _run_writing_session(body)
    except BaseException as e:
        record_failed("sweep", error=repr(e))
        keep_note()
        raise
    if rc == EXIT_BUSY:
        return _busy_not_started(roll)
    if rc != 0:
        record_failed("sweep", rc=rc)
        keep_note()
        extra = (" The sweep itself completed and is on disk; rebuild with "
                 "--from-sweep." if "meta" in state else "")
        print(f"roll {roll} FAILED at the sweep (rc {rc}); recorded. "
              f"Power-cycle before the next strip.{extra}", file=sys.stderr)
        return rc

    meta, d = state["meta"], state["diag"]
    scanlike = SimpleNamespace(last_diag=d, last_cal_data=state["cal"])
    per_frame: list = []
    try:
        mm, res = _sweep_resolve(raw_path, meta)
        _sweep_write_json(json_path, meta, res, d)
        incomplete = _sweep_cut(args, rdir, mm, res, meta, scanlike, per_frame,
                                rebuild=False)
        del mm
    except BaseException as e:
        record_failed("write", error=repr(e), per_frame=per_frame)
        keep_note()
        raise
    return _sweep_finish(args, record, rdir, raw_path, meta, res, d, per_frame,
                         incomplete, rc_extra={})


def _sweep_write_json(path: Path, meta: dict, res, diag_d: dict) -> None:
    """sweep.json: geometry, resolution (None until the finder has run),
    timing and the device diag of the one calibration."""
    ps = diag_d.get("phase_seconds") if diag_d else None
    doc = {"geometry": meta,
           "resolution": res.to_dict() if res is not None else None,
           "timing": {"phase_seconds": ps,
                      "total_seconds": (diag_d or {}).get("total_seconds"),
                      "started_utc": (diag_d or {}).get("started_utc"),
                      "finished_utc": (diag_d or {}).get("finished_utc")},
           "diag": diag_d}
    diag.write_sidecar(str(path), doc)


def _sweep_finish(args, record: dict, rdir: Path, raw_path: Path, meta: dict, res,
                  d: dict, per_frame: list, incomplete: list, *, rc_extra: dict) -> int:
    """Close a sweep roll: status, raw retention, manifest, messages."""
    from datetime import datetime, timezone
    from . import digitize
    rebuilt = bool(rc_extra.get("rebuilt"))
    ok = not incomplete
    keep = (not ok) or bool(getattr(args, "keep_sweep", False)) or rebuilt
    if not keep:
        try:
            raw_path.unlink()
        except OSError as e:
            log.warning("could not remove %s: %s", raw_path, e)
            keep = True
    record.update(finished_utc=datetime.now(timezone.utc).isoformat(),
                  per_frame=per_frame, status="ok" if ok else "failed",
                  stage=None if ok else "scan",
                  **_sweep_record_fields(meta, res, d, keep))
    if rebuilt:
        record["rebuilt_from_sweep"] = True
    try:
        digitize.append_manifest(args.out, record)
    except Exception as me:
        print(f"warning: roll {record['roll']} processed but the manifest write "
              f"failed ({me!r}); the images are saved in {rdir}.", file=sys.stderr)
    if keep and raw_path.exists():
        _sweep_kept_note(raw_path)
    if not ok:
        why = (f"sweep unresolved: {res.reason}" if not res.resolved else
               f"aperture coverage NOT verified for frame(s) "
               f"{', '.join(map(str, incomplete))}")
        print(f"error: {why} — no main negative was written for them; see the "
              f".diag.json and .overscan file(s), and sweep.json", file=sys.stderr)
        print(f"roll {record['roll']} recorded FAILED. Re-run with --force, or "
              f"rebuild from the kept sweep with --from-sweep.", file=sys.stderr)
        return 4
    subs = [pf["frame"] for pf in per_frame if pf.get("dark_b_substituted")]
    note = f" (dark_b substituted on frame(s) {subs})" if subs else ""
    print(f"roll {record['roll']} done: {len(per_frame)} frames -> {rdir}{note}. "
          f"Next: insert the next strip and run 'of135i digitize --sweep' again "
          f"(roll {record['roll'] + 1}).")
    return 0


def _digitize_from_sweep(args: argparse.Namespace, frames: list) -> int:
    """`digitize --sweep --from-sweep --roll N`: rebuild the roll offline
    from its sweep.raw + sweep.json (no scanner, no load, no eject). Clears
    the roll's frame products first, never the sweep files; appends a
    manifest record with ``rebuilt_from_sweep: true``."""
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from . import digitize

    def refuse(msg: str) -> int:
        print(f"error: {msg}", file=sys.stderr)
        return 2

    roll = args.roll
    args.roll_n = roll
    rdir = digitize.roll_dir(args.out, args.prefix, roll)
    raw_path = rdir / digitize.SWEEP_RAW_NAME
    json_path = rdir / digitize.SWEEP_JSON_NAME
    if not raw_path.is_file() or not json_path.is_file():
        return refuse(f"--from-sweep needs both {raw_path} and {json_path}")
    try:
        doc = json.loads(json_path.read_text())
        meta = doc["geometry"]
        meta["first"], meta["predicted_edges"], meta["width"]   # shape check
    except (OSError, ValueError, KeyError, TypeError) as e:
        return refuse(f"{json_path} is unreadable ({e!r})")
    if int(meta["dpi"]) != args.dpi:
        print(f"note: using the sweep's own dpi {meta['dpi']} (not --dpi {args.dpi})")
        args.dpi = int(meta["dpi"])
    removed = digitize.clear_roll_outputs(args.out, args.prefix, roll, include_sweep=False)
    if removed:
        print(f"--from-sweep: removed {len(removed)} frame file(s) in {rdir} before the rebuild")
    args.eject = True
    record = {"roll": roll, "prefix": args.prefix, "dir": str(rdir),
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "dpi": args.dpi, "positive": args.positive, "ir": args.ir,
              "frames": list(range(int(meta["first"]), int(meta["last"]) + 1)),
              "no_clean": args.no_clean, "rotate": args.rotate,
              "rebuilt_from_sweep": True}
    d = doc.get("diag") or {}
    scanlike = SimpleNamespace(last_diag=d, last_cal_data=None)
    per_frame: list = []
    try:
        mm, res = _sweep_resolve(raw_path, meta)
        incomplete = _sweep_cut(args, rdir, mm, res, meta, scanlike, per_frame,
                                rebuild=True)
        del mm
    except BaseException as e:
        try:
            record.update(status="failed", stage="write", error=repr(e),
                          per_frame=per_frame,
                          finished_utc=datetime.now(timezone.utc).isoformat())
            digitize.append_manifest(args.out, record)
        except Exception as me:
            log.warning("manifest write failed: %s", me)
        _sweep_kept_note(raw_path)
        raise
    return _sweep_finish(args, record, rdir, raw_path, meta, res, d, per_frame,
                         incomplete, rc_extra={"rebuilt": True})


def _cmd_digitize(args: argparse.Namespace) -> int:
    """Bulk-digitise one film strip into a resumable staging tree
    (of135i.digitize): pick the roll number, load the magazine, scan
    frames 1-4 to <out>/<prefix>roll-NNN/, eject, and record the roll in
    an append-only manifest. Run once per strip; resume is BETWEEN strips
    -- the roll number advances past the highest recorded or on-disk roll,
    and an incomplete strip is recorded failed and re-run whole (there is
    no mid-strip resume). Interactive (the load flow prompts), so it needs
    a real terminal. The main image is the raw negative (the archival
    starting point); colour interpretation is the application's job.

    `--film 110 --strips 2` (docs/film-110.md §12) scans two strips of one
    load: apertures 1-3 are slot 1 (roll N), 4-6 slot 2 (roll N+1), each
    with its own directory, products and manifest record. Everything below
    is written once for a list of per-slot contexts; a lone strip is a
    single context holding every frame."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from . import digitize, film110 as _film110, loadflow

    if (getattr(args, "sweep", False) or getattr(args, "from_sweep", False)
            or getattr(args, "keep_sweep", False)):
        # Whole-strip sweep (docs/whole-strip-sweep-design.md): its own
        # flow, validated before any hardware step.
        return _digitize_sweep(args)

    if getattr(args, "from_frames", False):
        # Offline: never reaches the load flow or a Scanner.
        return _digitize_from_frames(args)

    strips = int(getattr(args, "strips", 1) or 1)
    # --frames decides the strip length. 135 defaults to 1-4 (a longer
    # default would scan empty apertures for everyone with a shorter
    # strip). 110 defaults to 1-6: a long 110 strip (up to ~8 images)
    # reaches apertures 5-6, and a short one costs nothing because the
    # stop-at-empty rule ends the run after the first empty aperture that
    # follows film. Validated here, before the load prompt and any hardware.
    try:
        dig_frames = _parse_frames(
            getattr(args, "frames", None)
            or ("1-6" if (strips == 2 or getattr(args, "film", "135") == "110")
                else "1-4"))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    err = _validate_film110_args(args)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    film110 = getattr(args, "film", "135") == "110"
    if strips == 2:
        for sl in (1, 2):
            if not any(f in _film110.slot_apertures(sl) for f in dig_frames):
                first = _film110.slot_apertures(sl)
                print(f"error: --strips 2 needs --frames to cover at least one "
                      f"aperture of each slot (slot {sl} = apertures "
                      f"{first[0]}-{first[-1]}); got "
                      f"{', '.join(map(str, dig_frames))}", file=sys.stderr)
                return 2

    roll = args.roll if args.roll is not None else digitize.next_roll(args.out, args.prefix)
    rolls = [roll, roll + 1] if strips == 2 else [roll]
    ctxs = []
    for i, r in enumerate(rolls):
        st = (Film110State(getattr(args, "placement", None), slot=i + 1,
                           strips=strips) if film110 else None)
        frames_i = ([f for f in dig_frames
                     if _film110.slot_of(f, 2) == i + 1] if strips == 2
                    else list(dig_frames))
        prior = ([] if args.force else
                 [x for x in digitize.roll_records_current(args.out, r, args.prefix)
                  if x.get("film") == "110"]) if film110 else []
        if film110:
            _film110_set_pass(st, (1 if args.force else
                                   digitize.film110_next_pass(args.out, args.prefix, r)),
                              prior)
        ctxs.append(SimpleNamespace(
            slot=i + 1, roll=r, rdir=digitize.roll_dir(args.out, args.prefix, r),
            state=st, frames=frames_i, per_frame=[], record={}, prior=prior,
            seen_film=False, incomplete=[], stopped=False))
    ctx_of = {f: c for c in ctxs for f in c.frames}

    # Overwrite guard, BEFORE any hardware: refuse a roll that already has a
    # scan on disk (checked directly, not just via the manifest, so a crash
    # between scanning and recording is caught) or is recorded done, unless
    # --force.
    if film110:
        # A 110 roll takes as many runs (passes) as it needs; the guard knows it.
        if not args.force:
            if strips == 2:
                msg = _film110_pair_guard(args, roll)
                if msg is not None:
                    print(f"error: {msg}.", file=sys.stderr)
                    return 2
            if strips == 2 and all(digitize.roll_is_done(args.out, c.roll, args.prefix)
                                   for c in ctxs):
                print(f"error: rolls {roll} and {roll + 1} are both recorded "
                      f"done; use --roll for a new pair, or --force to redo "
                      f"them.", file=sys.stderr)
                return 2
            for c in ctxs:
                # A pair runs on while either roll still needs a pass; a
                # finished roll just sees all its copies kept.
                msg = _film110_roll_guard(args, c.roll, c.rdir,
                                          check_done=strips != 2)
                if msg is not None:
                    print(f"error: {msg}.", file=sys.stderr)
                    return 2
    elif not args.force and (digitize.roll_dir_has_output(args.out, args.prefix, roll)
                             or digitize.roll_is_done(args.out, roll, args.prefix)):
        print(f"error: roll {roll} already has output in {ctxs[0].rdir} (or is recorded "
              f"done); use --roll for a different one, or --force to redo it.",
              file=sys.stderr)
        return 2
    for c in ctxs:
        print(f"=== digitize roll {c.roll} -> {c.rdir}"
              f"{f' (slot {c.slot})' if strips == 2 else ''} ===")

    # --force redoes a roll: clear the previous run's frame outputs first, so
    # the dir can't end up a mix of two runs (e.g. an old f1-preview.tiff left
    # beside a new negative scanned without --positive). Done before hardware.
    if args.force:
        for c in ctxs:
            removed = digitize.clear_roll_outputs(args.out, args.prefix, c.roll)
            if film110:
                removed += digitize.clear_film110_outputs(args.out, args.prefix, c.roll)
            if removed:
                print(f"--force: removed {len(removed)} existing file(s) in {c.rdir} "
                      f"before re-scanning")

    args.eject = True

    started = datetime.now(timezone.utc).isoformat()
    for c in ctxs:
        c.record = {"roll": c.roll, "prefix": args.prefix, "dir": str(c.rdir),
                    "started_utc": started, "dpi": args.dpi,
                    "positive": args.positive, "ir": args.ir,
                    "frames": c.frames,
                    "no_clean": args.no_clean, "rotate": args.rotate}
        if strips == 2:
            c.record.update(strips=2, slot=c.slot,
                            companion_roll=rolls[1 - (c.slot - 1)])
        if args.force and film110:
            c.record["forced"] = True       # starts a new epoch of the roll's records
        if film110:
            _film110_fill_record(c.record, c.state, c.rdir, c.prior)

    def record_failed(stage: str, **extra) -> None:
        """Record every roll of this run as failed, tolerating a manifest
        write that itself fails -- never let that mask the original error."""
        for c in ctxs:
            try:
                if film110:
                    _film110_fill_record(c.record, c.state, c.rdir, c.prior)
                if stage == "scan":
                    extra["per_frame"] = c.per_frame
                c.record.update(status="failed", stage=stage,
                                finished_utc=datetime.now(timezone.utc).isoformat(),
                                **extra)
                digitize.append_manifest(args.out, c.record)
            except Exception as me:
                log.warning("manifest write failed while recording a failed roll "
                            "(original error preserved): %s", me)

    # 1) Load (its own session; interactive). A failure here leaves the
    #    scanner in an unknown state -- power cycle -- so we record and stop.
    if not args.assume_loaded:
        rc = loadflow.run(ask=input)
        if rc == EXIT_BUSY:
            return _busy_not_started(roll)
        if rc != 0:
            record_failed("load", load_rc=rc)
            print(f"load failed (rc {rc}); roll {roll} recorded as failed. "
                  f"Power-cycle before retrying.", file=sys.stderr)
            return rc

    # 2) Scan the requested frames to the roll dir (a fresh writing
    #    session). Same
    #    plain/dual dispatch as `scan`: non-3600 dpi is always a dual-light
    #    pass, and --ir on 3600 selects the dual flow; --no-ir on 3600 uses
    #    the plain flow.
    for c in ctxs:
        c.rdir.mkdir(parents=True, exist_ok=True)
    dual = args.ir or args.dpi != 3600

    def hand_over_placement() -> None:
        """Two strips, first frame of slot 2: the run has ONE placement
        label. Slot 1's decides it; when slot 1 numbered nothing and none
        was given, a first pass is labelled by the run order ("A" for the
        first run on the roll, else "B"), a later pass is "free"."""
        c1, c2 = ctxs
        s1, s2 = c1.state, c2.state
        if s1.placement is None:
            if s1.ref is not None:
                s1.placement, s1.placement_source = "free", "free"
            else:
                s1.placement = _film110_run_order_placement(args, c1.roll)
                s1.placement_source = "run order"
            s1.placement_given = False
        s2.placement = s1.placement
        s2.placement_given = s1.placement_given
        s2.placement_source = ("given" if s1.placement_given
                               else "slot 1 inferred"
                               if s1.placement_source == "inferred"
                               else s1.placement_source)
        # Strip 2's start seen in slot 1's last aperture belongs to slot 2.
        if s2.film_start_mm is None and s1.next_strip_start_mm is not None:
            s2.film_start_mm = s1.next_strip_start_mm

    def body(scanner: Scanner) -> int:
        scanner.park_mode = args.park
        scanner.recalibrate = bool(getattr(args, "recalibrate", False))
        if getattr(args, "warmup_budget", None) is not None:
            scanner.warmup_budget_s = float(args.warmup_budget)
        scanner.check_start_state()
        if not scanner.is_magazine_present():
            print("error: no magazine detected after load", file=sys.stderr)
            return 1
        handed_over = False
        emits = {c.slot: (_film110_digitize_emit(args, c.state, c.roll)
                          if film110 else None) for c in ctxs}

        for idx, frame in enumerate(dig_frames):
            c = ctx_of[frame]
            if strips == 2 and c.slot == 2 and not handed_over:
                handed_over = True
                hand_over_placement()
            if c.stopped:
                continue
            state, per_frame = c.state, c.per_frame
            scanner.initialize(ir=dual, dpi=args.dpi)
            if film110:
                out = str(digitize.frame_path_110(args.out, args.prefix, c.roll,
                                                  f"p{state.pass_index}", frame))
            else:
                out = str(digitize.frame_path(args.out, args.prefix, c.roll, frame))
            log.info("scanning frame %d @ %d dpi%s", frame, args.dpi,
                     " (dual-light pass)" if dual else "")
            # The frame is recorded BEFORE its writes and filled in as each
            # file lands, so a failure part-way through still says which
            # frame fell, what had been saved, and which step was running
            # (`stage`); an entry that completed has stage None.
            entry: dict = {"frame": frame, "main": None, "ir": None,
                           "preview": None, "dust_cleaned": None,
                           "stage": "scan"}
            per_frame.append(entry)
            if dual:
                raw, width, _meta = scanner.scan(frame=frame, ir=True, dpi=args.dpi)
            else:
                raw, width = scanner.scan(frame=frame)
            entry["stage"] = "write"
            if film110:
                def hook(vis, irr, frame=frame, state=state, emit=emits[c.slot]):
                    _film110_feed_aperture(args, state, frame, vis, irr, emit)
                main, irf, prev, cleaned, cov = _finish_digitize_frame(
                    args, raw, width, out, dual, progress=entry, film110=hook)
            else:
                main, irf, prev, cleaned, cov = _finish_digitize_frame(
                    args, raw, width, out, dual, progress=entry)
            entry.update(main=main, ir=irf, preview=prev, dust_cleaned=cleaned)
            film = None
            if cov is not None:
                entry["coverage_verified"] = bool(cov.verified)
                entry["coverage_reason"] = cov.reason
                entry["coverage_source"] = cov.source
                if not cov.verified:
                    c.incomplete.append(frame)
                elif "film_present" in entry:
                    film = {"present": entry["film_present"],
                            "saturated_share": entry["saturated_share"]}
                    if not film["present"]:
                        # Not a coverage failure: the opening was captured
                        # whole, there is just nothing in it.
                        entry["coverage_reason"] = "empty aperture"
            del raw
            entry["stage"] = "diag"
            if film is not None:
                _write_diag_sidecar(args, scanner, out, frame, coverage=cov, film=film)
            else:
                _write_diag_sidecar(args, scanner, out, frame, coverage=cov)
            d = scanner.last_diag or {}
            entry.update(stage=None,
                         calibration=d.get("calibration"),
                         gain_codes=d.get("gain_codes"),
                         offset_codes=d.get("offset_codes"),
                         dark_b_substituted=d.get("dark_b_substituted"))
            if film110 and state.aborted:
                print(f"stopping: {state.aborted}", file=sys.stderr)
                break
            if film is not None:
                if film["present"]:
                    c.seen_film = True
                elif c.seen_film and not getattr(args, "no_stop_at_empty", False):
                    # Empty aperture after film in this strip: the strip has
                    # ended; skip the rest of ITS frames (no hardware work).
                    # A two-strip load keeps going with the other slot, and
                    # still ejects at the end.
                    skipped = [f for f in dig_frames[idx + 1:] if ctx_of[f] is c]
                    c.stopped = True
                    if skipped:
                        c.record["skipped_frames"] = skipped
                        c.record["stop_reason"] = f"empty aperture at frame {frame}"
                        print(f"stopping: empty aperture at frame {frame} after film; "
                              f"skipping frame(s) {', '.join(map(str, skipped))} "
                              f"(--no-stop-at-empty scans them anyway)")
            if film110 and (c.stopped or frame == c.frames[-1]):
                _film110_finalize(args, state, c.roll, emits[c.slot])
                if state.aborted:
                    print(f"stopping: {state.aborted}", file=sys.stderr)
                    break
            if c.stopped and strips == 1:
                break
        if film110:
            for c in ctxs:
                _film110_finalize(args, c.state, c.roll, emits[c.slot])
        scanner.eject()
        print("ejected")
        if film110 and any(c.state.aborted for c in ctxs):
            return 5
        incomplete = [f for c in ctxs for f in c.incomplete]
        if incomplete:
            # Fail closed on image integrity, exactly as `scan` does: a
            # coverage failure is not a complete frame. The roll is
            # recorded failed (per_frame says which frames and why) and
            # re-run whole, digitize's existing resume unit.
            print(f"error: aperture coverage NOT verified for frame(s) "
                  f"{', '.join(map(str, incomplete))} — see the .diag.json "
                  f"and .overscan file(s); no main negative was written for "
                  f"them", file=sys.stderr)
            return 4
        if film110 and any(c.state.unidentified for c in ctxs):
            return 5                    # whole images written unnumbered
        return 0

    # Even if the scan/write flow raises (e.g. an OSError writing an image),
    # record the roll as failed -- with what was saved -- before the error
    # propagates, so its number is not silently reused. No further scan,
    # eject or recovery runs after a failure.
    try:
        rc = _run_writing_session(body)
    except BaseException as e:
        record_failed("scan", error=repr(e))
        raise
    if rc == EXIT_BUSY:
        return _busy_not_started(roll)
    finished = datetime.now(timezone.utc).isoformat()
    for c in ctxs:
        ok = (rc == 0 or (rc == 4 and not c.incomplete)
              or (rc == 5 and film110 and (c.state.numbering_basis == "unidentified"
                                           or c.state.unidentified)))
        c.record.update(finished_utc=finished, per_frame=c.per_frame,
                        status="ok" if ok else "failed",
                        stage=None if ok else "scan")
        if film110:
            _film110_fill_record(c.record, c.state, c.rdir, c.prior)
        if strips == 2:
            c.record["film_found"] = any(e.get("film_present")
                                         for e in c.per_frame)
        # The images are already on disk; a manifest write that fails here must
        # not turn a successful scan into a crash. Warn loudly instead -- the
        # disk-based overwrite guard still protects the roll on the next run.
        try:
            digitize.append_manifest(args.out, c.record)
        except Exception as me:
            print(f"warning: roll {c.roll} scanned OK but the manifest write failed "
                  f"({me!r}); the images are saved in {c.rdir}. The manifest is out "
                  f"of date -- record it by hand or re-run with --force.",
                  file=sys.stderr)

    c0 = ctxs[0]
    if (film110 and strips == 1 and rc in (0, 5) and not c0.state.aborted
            and c0.record.get("status") == "ok" and c0.record.get("anchor_shift")):
        # The strip's front end was seen for the first time in this pass, so
        # the numbers of the passes before it were provisional. The scanner
        # is ejected and idle: replay the roll from its stored frames (no
        # hardware, nothing deleted -- the old photographs go to superseded-*/)
        # so that every record and product carries its final number.
        print(f"The strip's front end is in view for the first time: renumbering "
              f"roll {roll} from its stored frames (no scanning; the earlier "
              f"photographs are moved to superseded-*/, not deleted).")
        rargs = SimpleNamespace(**vars(args))
        rargs.from_frames, rargs.roll = True, roll
        rargs._anchor_shift = int(c0.record["anchor_shift"])
        rrc = _digitize_from_frames(rargs)
        if rrc not in (0, 5):
            print(f"warning: the automatic renumbering of roll {roll} stopped "
                  f"(exit {rrc}); the pass itself is recorded. Run 'of135i "
                  f"digitize --film 110 --roll {roll} --from-frames' again once "
                  f"the cause is clear; earlier photographs are in "
                  f"superseded-*/.", file=sys.stderr)
        return rrc
    if rc == 0:
        subs = [pf["frame"] for c in ctxs for pf in c.per_frame
                if pf.get("dark_b_substituted")]
        note = f" (dark_b substituted on frame(s) {subs})" if subs else ""
        if film110 and strips == 2:
            _film110_print_pair_summary(args, ctxs, note)
        elif film110:
            _film110_print_roll_summary(roll, c0.state, c0.per_frame, c0.rdir,
                                        c0.record["advice"], note, c0.record)
        else:
            print(f"roll {roll} done: {len(c0.per_frame)} frames -> {c0.rdir}{note}. "
                  f"Next: insert the next strip and run 'of135i digitize' again "
                  f"(roll {roll + 1}).")
    elif rc == 5 and film110 and not any(c.state.aborted for c in ctxs):
        # Numbering held, but whole image(s) did not fit it: the numbered
        # ones are written as usual, the others unnumbered.
        if strips == 2:
            _film110_print_pair_summary(args, ctxs, "")
        else:
            _film110_print_roll_summary(roll, c0.state, c0.per_frame, c0.rdir,
                                        c0.record["advice"], "", c0.record)
        n_un = sum(len(c.state.unidentified) for c in ctxs)
        print(f"roll {roll}: {n_un} whole photograph(s) did not fit the roll's "
              f"numbering and were written UNNUMBERED (p<pass>-a<aperture>-<i>.tiff); "
              f"see 'problems'. The frame files are kept (re-run with --from-frames "
              f"once the cause is clear).", file=sys.stderr)
    elif rc == 5 and film110:
        from . import film110_report as rep
        why = next(c.state.aborted for c in ctxs if c.state.aborted)
        n_un = sum(len(c.state.unidentified) for c in ctxs)
        if strips == 1:
            print(rep.legend())
            print(f"Pass {c0.state.pass_index} report (numbering NOT established):")
            for ln in rep.format_pass_report(c0.record.get("pass_report") or []):
                print(ln)
        print(f"roll {roll}: numbering NOT established: {why}. {n_un} whole "
              f"photograph(s) were written UNNUMBERED (p<pass>-a<aperture>-<i>.tiff); "
              f"nothing numbered was written or replaced. The roll's earlier "
              f"photographs and its reference are unchanged; the frame files of "
              f"this pass are kept (re-run with --from-frames once the cause is "
              f"clear).", file=sys.stderr)
    elif rc == 4:
        # Coverage failure: the pass and eject completed normally -- the
        # transport needs no recovery, the roll just is not complete.
        print(f"roll {roll} recorded FAILED: aperture coverage did not "
              f"verify on every frame (see per_frame in the manifest). "
              f"Re-run the roll with --force.", file=sys.stderr)
    else:
        print(f"roll {roll} FAILED at scan (rc {rc}); recorded. "
              f"Power-cycle before the next strip.", file=sys.stderr)
    return rc


def _film110_print_pair_summary(args: argparse.Namespace, ctxs: list,
                                note: str) -> None:
    """The closing message of a two-strip run (§12): both rolls, what each
    delivered and what each still needs."""
    r1, r2 = ctxs[0].roll, ctxs[1].roll
    pl = ctxs[0].state.placement
    lines = []
    wants = False
    for c in ctxs:
        adv = c.record["advice"]
        got = sum(1 for e in c.state.images if e["main"])
        found = ("" if c.record.get("film_found") else " (no film found)")
        if adv["images"]:
            need = (f"missing {adv['missing']}, strip end free {adv['free_end']}; "
                    f"{adv['suggest']}")
        elif adv.get("done"):
            need = "nothing missing"
        else:
            need = f"no image numbered; {adv['suggest']}"
        wants = wants or bool(adv["images"]) or not adv.get("done")
        lines.append(f"  roll {c.roll} (slot {c.slot}): {got} photograph(s) -> "
                     f"{c.rdir}{found}; {need}")
    if wants:
        nxt = (f"Re-seat the strips as suggested (slide or turn) and run "
               f"'of135i digitize --film 110 --strips 2 --roll {r1}' again, "
               f"or stop here.")
    else:
        nxt = f"Nothing missing. Next pair: roll {r2 + 1}."
    print(f"rolls {r1} and {r2} done (110, two strips, placement {pl}){note}:")
    for ln in lines:
        print(ln)
    print(nxt)


def _cmd_version(args: argparse.Namespace) -> int:
    from . import __version__, diag
    host = diag._collect_host()
    rev = host.get("driver_revision") or "unknown"
    print(f"of135i {__version__} (driver revision {rev}, python {host.get('python')}, pyusb {host.get('pyusb')})")
    return 0


def _cmd_eject(args: argparse.Namespace) -> int:
    def body(scanner: Scanner) -> int:
        scanner.eject()
        print("ejected")
        return 0

    return _run_writing_session(body)


def _cmd_watch(args: argparse.Namespace) -> int:
    """Poll the buttons (reads only) and eject on the eject button.
    Presses are seen via the EP 0x83 interrupt and via reg 0x31 (Test
    97). The start state is checked read-only up front so an unsafe
    scanner is refused immediately instead of at the first button press;
    a failed eject ends the watch (the session is failed; a further
    press must not send anything). Only "eject" ever acts, and only
    with a magazine present; other buttons are printed."""
    def body(scanner: Scanner) -> int:
        scanner.check_start_state()
        print("watching for button events (Ctrl+C to stop)", flush=True)
        previous: set[str] = set()
        while True:
            try:
                pressed = scanner.poll_buttons(strict_interrupts=True)
                time.sleep(0.05)
            except KeyboardInterrupt:
                # Idle wait interrupted: nothing was in progress.
                print("\nstopped")
                return 0
            except InterruptOverflowError as e:
                print(f"cannot watch: {e}", file=sys.stderr)
                return 1
            for name in pressed:
                if name in previous:
                    continue  # same held press, already handled
                if name.startswith("unknown-"):
                    print(f"other event 0x{name[len('unknown-0x'):]}", flush=True)
                elif name == "eject":
                    print("eject button pressed", flush=True)
                    if scanner.is_magazine_present():
                        scanner.eject()
                        print("ejected", flush=True)
                    else:
                        print("magazine not detected, ignoring", flush=True)
                else:
                    print(f"{name} button pressed (no action)", flush=True)
            previous = set(pressed)

    return _run_writing_session(body)


def _cmd_buttons(args: argparse.Namespace) -> int:
    """Read-only: print each front-panel button press with a timestamp
    until Ctrl-C or --duration seconds. Works on a cold scanner too."""
    try:
        with Scanner(UsbIo.open(readonly=True)) as scanner:
            print("reading buttons (Ctrl+C to stop)", flush=True)
            end = None if args.duration is None else time.monotonic() + args.duration
            previous: set[str] = set()
            try:
                while end is None or time.monotonic() < end:
                    pressed = scanner.poll_buttons()
                    for name in pressed:
                        if name not in previous:
                            print(f"{time.strftime('%H:%M:%S')} {name}", flush=True)
                    previous = set(pressed)
                    time.sleep(0.05)
            except KeyboardInterrupt:
                print("\nstopped")
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
    p_scan.add_argument("--frame", type=int,
                        help=f"frame number, 1-{holder.DEFAULT.frames} "
                             f"({holder.DEFAULT.name})")
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
    p_scan.add_argument("--overscan", type=float, default=None, metavar="MM",
        help="overscan margin per side around the whole aperture (default "
             "0.75, the production geometry accepted in Test 58; "
             "docs/holder-position-design.md). Applies to plain and "
             "dual-light scans alike: the window uses the corrected mean "
             "mapping, the aperture-registered image goes to -o (with --ir "
             "also <stem>-ir.tiff, cropped to the same lines) and the full "
             "overscan frame(s) to <o>.overscan.<ext> (and "
             "<stem>-ir.overscan.tiff); on a coverage failure only the "
             "overscan raw is kept and the exit status is non-zero")
    p_scan.add_argument("--no-diag", action="store_true",
        help="skip writing the <output>.diag.json calibration/timing sidecar")
    p_scan.add_argument("--no-cal-data", action="store_true",
        help="do not write the <output>.cal.npz per-pixel calibration data "
             "(white line, dark/shading measurements, uploaded tables; about 13 "
             "MB per frame; written only when the scan measured a fresh "
             "calibration, and only with the .diag.json sidecar)")
    p_scan.add_argument("--warmup-budget", type=float, default=None, metavar="SECONDS",
                        help="total time to wait for the lamp after a cold start before "
                             "failing the scan (default: the driver's bounded default, 60 s)")
    p_scan.add_argument("--park", choices=("verbatim", "semantic"), default="semantic",
        help="PARK phase implementation: semantic issues the same motor "
             "sequence as verbatim but replaces the captured trace's fixed "
             "pacing with real read-modify-write and condition waits "
             "(default since 2026-09-17: 3.7-3.8 s vs verbatim's captured "
             "13.6-16.9 s; hardware-confirmed via the SANE backend's PARK "
             "program, docs/park-completion-analysis.md); verbatim replays "
             "the captured op stream byte for byte, for A/B comparison")
    p_scan.add_argument("--recalibrate", action="store_true",
        help="force a full dark/white/gain/shading calibration on every "
             "frame (today's behaviour). Default since 2026-09-17: reuse "
             "this session's first successful calibration on later frames "
             "of the same (dpi, dual) kind, re-applying only the AFE gain "
             "codes -- the vendor does the same on a skip-calibration "
             "frame (docs/protocol-notes.md pass 14) and the shading table "
             "survives in scanner RAM across PARK. Plain 3600 dpi only; "
             "dual/IR always recalibrates (docs/driver-design.md)")
    p_scan.add_argument("--film", choices=("135", "110"), default="135",
        help="film format inside the strip holder (default 135, today's "
             "behaviour, byte-identical). 110 (Pocket Instamatic) runs the "
             "110 frame detector on each aperture-registered crop and "
             "writes <out>-image<N>.tiff per whole image found (plus "
             "-ir.tiff with --ir); the aperture product and overscan are "
             "still written exactly as today. docs/film-110.md")
    p_scan.add_argument("--placement", choices=("A", "B"), default=None,
        help="which of the two 110 placements the strip is in "
             "(docs/film-110.md §3). Optional with --film 110: when "
             "omitted it is inferred from the first image's position "
             "(an image midway between the two phases is refused with a "
             "request to give it); given, it overrides the inference. "
             "Images are numbered by their position on the strip, so the "
             "same photograph gets the same number in both placements")
    p_scan.add_argument("-o", "--output", required=True, help="output file path (.tiff or .pnm)")
    p_scan.set_defaults(func=_cmd_scan)

    p_preview = sub.add_parser("preview", help="run a quick preview sweep")
    p_preview.add_argument("-o", "--output", help="output file path")
    p_preview.set_defaults(func=_not_wired_yet)

    p_load = sub.add_parser("load", help="load the film magazine (the vendor's insert flow; interactive)")
    p_load.add_argument("--release", action="store_true",
        help="only release a latched magazine (cold init + the app-start jog), then "
             "stop: the first cycle of the two-cycle exit after a power cycle with "
             "the magazine latched (docs/test-log.md Test 49)")
    p_load.set_defaults(func=_cmd_load)
    p_load.add_argument("--double-jog", action="store_true",
        help="EXPERIMENT (docs/test-log.md Test 49 exit, unverified): after the first "
             "jog and reinsert, run the app-start jog a second time from the loose "
             "position and reinsert again before loading -- the A/B for loading in one "
             "power cycle from a latched magazine")
    p_load.add_argument("--next-strip", action="store_true",
        help="load the next strip after an eject in the same power-on: no jog, no "
             "reinsert prompt; the magazine must already be pushed in to the stop with "
             "the new strip. Refused on a cold (power-cycled) scanner. Hardware-"
             "verified once (docs/test-log.md Test 89)")

    p_version = sub.add_parser("version", help="print the driver version and git revision")
    p_version.set_defaults(func=_cmd_version)

    p_eject = sub.add_parser("eject", help="eject the film magazine")
    p_eject.set_defaults(func=_cmd_eject)

    p_status = sub.add_parser("status", help="read and print status registers")
    p_status.set_defaults(func=_cmd_status)

    p_watch = sub.add_parser("watch", help="poll buttons; eject on the eject button")
    p_watch.set_defaults(func=_cmd_watch)

    p_buttons = sub.add_parser(
        "buttons", help="read-only: print front-panel button presses")
    p_buttons.add_argument("--duration", type=float, metavar="S",
        help="stop after S seconds (default: until Ctrl-C)")
    p_buttons.set_defaults(func=_cmd_buttons)

    p_doctor = sub.add_parser(
        "doctor", help="read-only hardware health report (no motor/register writes)")
    p_doctor.add_argument("--json", metavar="PATH", help="also write the report as JSON to PATH")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_dig = sub.add_parser(
        "digitize",
        help="bulk-digitise one strip into a resumable staging tree "
             "(load, scan frames 1-4, eject, record in a manifest); interactive")
    p_dig.add_argument("-o", "--out", required=True, metavar="DIR",
        help="staging directory; rolls go in <out>/<prefix>roll-NNN/, logged in "
             "<out>/manifest.jsonl")
    p_dig.add_argument("--prefix", default="",
        help="roll-directory name prefix, e.g. 'boxA-' -> boxA-roll-001/ (default none)")
    p_dig.add_argument("--roll", type=int, default=None,
        help="roll number (default: one past the highest for this prefix, "
             "counting BOTH the manifest and existing roll dirs on disk)")
    p_dig.add_argument("--force", action="store_true",
        help="scan even if this roll already has files on disk or is "
             "recorded done; clears the roll's previous f*.tiff/.diag.json "
             "first so the dir is not a mix of two runs (--film 110: "
             "both placements' files and the image<N> photographs)")
    p_dig.add_argument("--frames", default=None, metavar="SPEC",
        help=f"which frames of the strip to scan, e.g. 1-4 (default for "
             f"135; 1-6 with --film 110, where the stop-at-empty rule ends "
             f"a short strip early), 1-6 for a full-length strip, or "
             f"1,3-4. The holder holds {holder.DEFAULT.frames}; the 135 "
             f"default stays 1-4 so a shorter strip does not scan empty "
             f"apertures.")
    p_dig.add_argument("--from-frames", action="store_true",
        help="--film 110 --roll N only: rebuild the roll offline from its "
             "stored frame files (p<k>-f<N>.tiff): every pass is replayed in "
             "order through the same code a live pass uses, photographs are "
             "renumbered and rewritten, the old image<N>.tiff files are MOVED "
             "to superseded-<UTC>/ (never deleted), new manifest records "
             "follow. No scanner is touched; dpi/ir/rotate come from the "
             "stored records (docs/film-110.md §14)")
    p_dig.add_argument("--sweep", action="store_true",
        help="135: scan the whole strip in ONE pass "
             "(one calibration, one positioning, one park) into <roll>/sweep.raw "
             "and cut the frames from it in software, each judged by the same "
             "aperture coverage check (docs/whole-strip-sweep-design.md). "
             "With --no-ir (plain 3600, black-and-white film) the pass is "
             "limited to --frames 1-4 until its anchoring is hardware-verified "
             "(implemented offline, awaiting its first hardware run). "
             "--frames must be a contiguous range")
    p_dig.add_argument("--keep-sweep", action="store_true",
        help="--sweep: keep sweep.raw (1.3-2 GB) after a fully successful "
             "roll (default: removed; always kept on any failure)")
    p_dig.add_argument("--from-sweep", action="store_true",
        help="--sweep --roll N: rebuild the roll offline from its sweep.raw + "
             "sweep.json (no scanner, no load/eject); clears the roll's frame "
             "products first, never the sweep files")
    p_dig.add_argument("--assume-loaded", action="store_true",
        help="skip the load flow (the magazine is already latched)")
    p_dig.add_argument("--dpi", type=int, default=3600, choices=SUPPORTED_DPIS,
        help="scan resolution (default 3600)")
    p_dig.add_argument("--positive", action="store_true",
        help="also write a positive preview as a SEPARATE file "
             "(fN-preview.tiff); the main fN.tiff always stays the raw "
             "negative (default off)")
    p_dig.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270),
        help="rotate output counter-clockwise (degrees; default 0)")
    p_dig.add_argument("--film", choices=("135", "110"), default="135",
        help="film format in the strip holder (default 135). 110 (Pocket "
             "Instamatic): a strip takes TWO runs, one per placement "
             "(docs/film-110.md §3), both into the same roll directory "
             "(--roll N for the second). Frame files get the placement as a "
             "prefix (a-f1.tiff, b-f1.tiff) and every whole photograph is "
             "written once as image<N>.tiff (N = position on the strip), "
             "never overwritten except a free-end copy by the supported one")
    p_dig.add_argument("--placement", choices=("A", "B"), default=None,
        help="which 110 placement this run is (docs/film-110.md §3). "
             "Optional with --film 110: inferred from the first image's "
             "position; given, it overrides the inference")
    p_dig.add_argument("--strips", type=int, choices=(1, 2), default=1,
        help="--film 110 only: how many strips the holder carries (default "
             "1). 2 = two strips of up to four images, strip 1 against the "
             "holder's left end (apertures 1-3), strip 2 against its right "
             "end (apertures 4-6), one load; written as two rolls, N and "
             "N+1 (docs/film-110.md §12). Use 1 for a single strip")
    p_dig.add_argument("--no-ir", dest="ir", action="store_false",
        help="skip the IR pass and dust removal (non-3600 profiles still "
             "run a dual-light capture; only the IR output is dropped)")
    p_dig.add_argument("--no-stop-at-empty", action="store_true",
                       help="scan every requested frame even after an empty "
                            "aperture that follows film (default: the strip "
                            "has ended, the remaining frames are skipped)")
    p_dig.add_argument("--no-clean", action="store_true",
        help="skip IR-based dust/scratch removal on the visible image")
    p_dig.add_argument("--no-diag", action="store_true",
        help="skip writing per-frame .diag.json sidecars")
    p_dig.add_argument("--no-cal-data", action="store_true",
        help="do not write the per-frame .cal.npz per-pixel calibration data "
             "(white line, dark/shading measurements, uploaded tables; about 13 "
             "MB per frame; written only for frames that measured a fresh "
             "calibration, and only with the .diag.json sidecars)")
    p_dig.add_argument("--park", choices=("verbatim", "semantic"), default="semantic",
        help="PARK implementation: semantic (default since 2026-09-17, "
             "3.7-3.8 s, hardware-confirmed via the SANE backend) or "
             "verbatim (replays the captured stream byte for byte)")
    p_dig.add_argument("--recalibrate", action="store_true",
        help="force a full calibration on every frame (today's behaviour) "
             "instead of reusing this roll's first successful plain-3600 "
             "calibration on later frames (default since 2026-09-17; the "
             "vendor does the same, docs/protocol-notes.md pass 14). "
             "Dual/IR always recalibrates")
    p_dig.add_argument("--warmup-budget", type=float, default=None, metavar="SECONDS",
        help="lamp warmup budget after a cold start (default 60 s)")
    p_dig.set_defaults(func=_cmd_digitize, ir=True)

    return parser


class _ResilientStream:
    """A console stream that can never kill a scan.

    A print to a stdout/stderr pipe whose reader has gone away raises
    BrokenPipeError; unhandled, that aborted a scanning session
    mid-strip (production telemetry, 2026-09/10, 711 frames). This
    wrapper catches BrokenPipeError and OSError(EPIPE) in write/flush,
    marks the stream broken, points the underlying file descriptor at
    os.devnull (the standard recipe, so the interpreter's flush at exit
    cannot fail either) and drops all further output silently. Everything
    else (fileno, isatty, other attributes) delegates to the wrapped
    stream. KeyboardInterrupt and every other error pass through.
    """

    def __init__(self, stream, *, redirect_fd: bool = True):
        self._stream = stream
        self._redirect_fd_on_break = redirect_fd
        self.broken = False

    @staticmethod
    def _is_pipe_error(exc: BaseException) -> bool:
        return isinstance(exc, BrokenPipeError) or (
            isinstance(exc, OSError) and exc.errno == errno.EPIPE)

    def _redirect_fd(self) -> None:
        """Point the wrapped stream's fd at os.devnull. Overridable (the
        unit test does not touch real descriptors)."""
        try:
            fd = self._stream.fileno()
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, fd)
            finally:
                os.close(devnull)
        except Exception:
            pass

    def _mark_broken(self) -> None:
        self.broken = True
        if self._redirect_fd_on_break:
            self._redirect_fd()

    def write(self, data):
        if self.broken:
            return len(data)
        try:
            return self._stream.write(data)
        except OSError as e:
            if not self._is_pipe_error(e):
                raise
            self._mark_broken()
            return len(data)

    def flush(self):
        if self.broken:
            return None
        try:
            return self._stream.flush()
        except OSError as e:
            if not self._is_pipe_error(e):
                raise
            self._mark_broken()
            return None

    def __getattr__(self, name):
        # Only reached for attributes not defined above.
        return getattr(self._stream, name)


def _install_resilient_console() -> None:
    """Wrap sys.stdout/sys.stderr (once) so console output cannot abort
    a command. Must run before logging is configured, so the logging
    StreamHandler binds to the wrapped stderr."""
    if not isinstance(sys.stdout, _ResilientStream):
        sys.stdout = _ResilientStream(sys.stdout)
    if not isinstance(sys.stderr, _ResilientStream):
        sys.stderr = _ResilientStream(sys.stderr)


def main(argv: list[str] | None = None) -> int:
    _install_resilient_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
