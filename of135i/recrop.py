"""Recrop: the aperture-registered products from a preserved overscan frame.

Every scan keeps its full overscan window beside the product
(`<stem>.overscan.tiff`, and `<stem>-ir.overscan.tiff` on the dual paths;
docs/holder-position-design.md section 5). When coverage did not verify,
only those files exist for the frame. Most such frames hold the whole
picture: the opening was captured, but one margin was under the 0.15 mm
floor (issue #61: a unit whose frames sit about 0.5 mm earlier than the
model, so the leading margin was 0.10-0.14 mm on frames 4 and 5). This
module makes the products from the preserved files, without scanning
again, with a margin floor the operator chooses.

What it does is exactly the delivery step of `scan`/`digitize` on the
arrays those commands wrote: un-rotate the overscan frame(s) so the strip
runs along axis 0, measure coverage (IR first on a dual frame, as
aperture_crop.measure_coverage_dual does), refuse an empty opening (the
`digitize` rule, aperture_crop.film_presence; `scan` itself does not
check for film), crop both channels to the same lines, rotate back and
write `<stem>.tiff` and `<stem>-ir.tiff`. The overscan files are never
altered. Dust removal is not repeated: the visible overscan frame of a
dual scan is already the cleaned image (it is written after
image.remove_dust). A record of the run goes to `<stem>.recrop.json`.

Not for a `scan --positive` frame: its overscan file is the positive
rendering (mirrored, inverted), on which the plastic edge detector is not
defined. The sidecar says so and the frame is refused. `digitize
--positive` only adds a preview file; its overscan frames are the
negative and are accepted (the sidecar's ``cli.command`` tells the two
apart; a sidecar from before that key was written is refused too when it
says positive).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import aperture_crop, image

OVERSCAN_TAG = ".overscan"


@dataclass(frozen=True)
class RecropPaths:
    """The files of one frame, derived from any one of its names."""

    overscan: Path          #: <stem>.overscan.<ext>
    ir_overscan: Path       #: <stem>-ir.overscan.tiff
    main: Path              #: <stem>.<ext>
    ir: Path                #: <stem>-ir.tiff
    sidecar: Path           #: <stem>.diag.json (written by the scan)
    record: Path            #: <stem>.recrop.json (written here)


def paths_for(name: str | Path) -> RecropPaths:
    """Derive a frame's file set from its overscan file, its product
    name or its bare stem: ``f4.overscan.tiff``, ``f4.tiff`` and ``f4``
    all give the same set. The IR files follow the scan commands'
    naming (``<stem>-ir.overscan.tiff``, ``<stem>-ir.tiff``)."""
    p = Path(name)
    suffix = p.suffix if p.suffix.lower() in (".tiff", ".tif") else ".tiff"
    stem = p.name[: -len(p.suffix)] if p.suffix else p.name
    if stem.endswith(OVERSCAN_TAG):
        stem = stem[: -len(OVERSCAN_TAG)]
    if stem.endswith("-ir"):
        stem = stem[: -len("-ir")]
    d = p.parent
    return RecropPaths(
        overscan=d / f"{stem}{OVERSCAN_TAG}{suffix}",
        ir_overscan=d / f"{stem}-ir{OVERSCAN_TAG}.tiff",
        main=d / f"{stem}{suffix}",
        ir=d / f"{stem}-ir.tiff",
        sidecar=d / f"{stem}.diag.json",
        record=d / f"{stem}.recrop.json",
    )


@dataclass(frozen=True)
class RecropSettings:
    """How the overscan frame was written, from the scan's sidecar
    (`cli` block) or the command line: the resolution it was scanned at,
    the rotation applied to the stored file, and whether it is a
    positive rendering (refused)."""

    dpi: int
    rotate: int
    positive: bool          #: the overscan file is a positive rendering
    source: str             #: "sidecar" or "arguments"
    slide: bool = False     #: written by `scan --holder slide` (refused)


def settings_from_sidecar(sidecar: Path) -> RecropSettings | None:
    """The settings from `<stem>.diag.json`'s ``cli`` block, or None when
    there is no sidecar or it has no such block."""
    try:
        d = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None
    cli = d.get("cli") if isinstance(d, dict) else None
    if not isinstance(cli, dict) or "dpi" not in cli:
        return None
    # Only `scan --positive` writes a positive overscan frame; digitize's
    # overscan frames are always the negative, whatever --positive did to
    # its preview. A sidecar without the "command" key (written before it
    # existed) is read as scan's when it says positive.
    positive = bool(cli.get("positive")) and cli.get("command") != "digitize"
    slide = cli.get("holder") == "slide" or d.get("holder") == "slide"
    return RecropSettings(dpi=int(cli["dpi"]), rotate=int(cli.get("rotate") or 0),
                          positive=positive, source="sidecar", slide=slide)


def axis_dpi(dpi: int, rotate: int) -> tuple[int, int]:
    """The (x, y) resolution to stamp on a product turned by ``rotate``
    degrees (any multiple of 90): the delivered strip runs along axis 0
    with the sensor-across direction on axis 1, so unrotated (x, y) =
    (across, along) = image.sampling_resolution(dpi), and an odd number of
    quarter turns swaps the two. The one rule for every product the
    driver writes (cli._axis_dpi adds the --positive turn and calls this)."""
    across, along = image.sampling_resolution(dpi)
    return (along, across) if ((rotate or 0) // 90) % 2 else (across, along)


@dataclass(frozen=True)
class RecropResult:
    """What happened to one frame. ``written`` is True only when the main
    product was written (and the IR product, when there was an IR
    overscan frame)."""

    overscan: str
    written: bool
    reason: str
    main: str | None
    ir: str | None
    verified: bool
    source: str | None
    leading_margin_mm: float | None
    trailing_margin_mm: float | None
    film_present: bool | None
    min_margin_mm: float
    dpi: int
    rotate: int
    settings_source: str
    offset_hint_mm: float | None
    record_error: str | None = None     #: why <stem>.recrop.json was not written


class RecropError(Exception):
    """A frame that cannot be recropped as asked (nothing written)."""


def _read_tiff(path: Path) -> np.ndarray:
    import tifffile
    return tifffile.imread(str(path))


def _unrotate(arr: np.ndarray, rotate: int) -> np.ndarray:
    """A view with the strip along axis 0 (no copy: the measurement and
    the crop work on views, only the written crop is made contiguous)."""
    k = (rotate or 0) // 90
    return np.rot90(arr, -k) if k else arr


def _rotate(arr: np.ndarray, rotate: int) -> np.ndarray:
    k = (rotate or 0) // 90
    return np.ascontiguousarray(np.rot90(arr, k) if k else arr)


def recrop(name: str | Path, *, min_margin_mm: float = 0.15,
           dpi: int | None = None, rotate: int | None = None,
           overwrite: bool = False, write_ir: bool = True,
           dry_run: bool = False) -> RecropResult:
    """Make `<stem>.tiff` (and `<stem>-ir.tiff`) from `<stem>.overscan.tiff`
    (and `<stem>-ir.overscan.tiff`), accepting margins down to
    ``min_margin_mm`` (0.15 is the scan commands' floor; 0 accepts any
    located edge).

    ``dpi`` and ``rotate`` default to the scan's sidecar (`<stem>.diag.json`,
    ``cli`` block); without a sidecar both must be given. A ``--positive``
    scan's overscan frame is refused. An existing product is not
    overwritten unless ``overwrite``. ``dry_run`` measures and reports
    but writes nothing.

    Raises RecropError for a frame that cannot be processed (file
    missing, settings unknown, positive, product in the way). A frame
    that is processed but whose coverage does not verify, or whose
    opening is empty, is a normal RecropResult with ``written`` False.
    """
    paths = paths_for(name)
    if not paths.overscan.is_file():
        raise RecropError(f"{paths.overscan} does not exist (the preserved "
                          f"overscan frame is the input)")
    if not np.isfinite(min_margin_mm) or min_margin_mm < 0:
        raise RecropError(f"min_margin_mm must be a finite number >= 0, got {min_margin_mm}")

    side = settings_from_sidecar(paths.sidecar)
    if side is not None and side.slide:
        raise RecropError(
            f"{paths.overscan} is a mounted-slide scan (`scan --holder slide`, "
            f"its sidecar says holder: slide): recrop uses the strip holder's "
            f"coverage check, not the slide crop, so it would not find or would "
            f"misplace the mount's aperture; nothing was written. Scan the "
            f"slide again with --holder slide")
    if side is not None and side.positive:
        raise RecropError(
            f"{paths.overscan} was written by `scan --positive` (or its sidecar "
            f"predates the command key and says positive): it is the positive "
            f"rendering (mirrored, inverted), which cannot be recropped; scan "
            f"the frame again without --positive")
    if side is None and (dpi is None or rotate is None):
        raise RecropError(
            f"no sidecar beside {paths.overscan} ({paths.sidecar.name}); give "
            f"both --dpi and --rotate as the frame was scanned (--rotate 0 "
            f"when it was not rotated)")
    if dpi is None:
        dpi = side.dpi
    if rotate is None:
        rotate = side.rotate
    if rotate not in (0, 90, 180, 270):
        raise RecropError(f"rotate must be 0, 90, 180 or 270, got {rotate}")
    settings_source = ("arguments" if side is None or dpi != side.dpi or rotate != side.rotate
                       else "sidecar")

    has_ir = paths.ir_overscan.is_file()
    if not overwrite and not dry_run:
        for product in ((paths.main,) + ((paths.ir,) if has_ir and write_ir else ())):
            if product.exists():
                raise RecropError(f"{product} already exists; --overwrite replaces it")

    visible = _read_tiff(paths.overscan)
    if visible.ndim != 3 or visible.shape[2] < 3:
        raise RecropError(f"{paths.overscan}: expected a 3-channel image, got shape {visible.shape}")
    visible = _unrotate(visible[:, :, :3], rotate)
    ir = None
    if has_ir:
        ir = _read_tiff(paths.ir_overscan)
        if ir.ndim == 3:
            ir = ir[:, :, 0]            # stored as the IR channel stacked x3
        ir = _unrotate(ir, rotate)
        if ir.shape[0] != visible.shape[0]:
            raise RecropError(
                f"{paths.ir_overscan} has {ir.shape[0]} lines, {paths.overscan} "
                f"{visible.shape[0]}: not the same scan")
        cov = aperture_crop.measure_coverage_dual(visible, ir, dpi=dpi,
                                                  min_margin_mm=min_margin_mm)
    else:
        cov = aperture_crop.measure_coverage(visible, dpi=dpi, min_margin_mm=min_margin_mm)

    hint = aperture_crop.position_offset_hint(cov)
    common = dict(overscan=str(paths.overscan), verified=cov.verified, source=cov.source,
                  leading_margin_mm=cov.leading_margin_mm,
                  trailing_margin_mm=cov.trailing_margin_mm,
                  min_margin_mm=float(min_margin_mm), dpi=int(dpi), rotate=int(rotate),
                  settings_source=settings_source,
                  offset_hint_mm=hint["offset_mm"] if hint else None)
    if not cov.verified:
        res = RecropResult(written=False, reason=cov.reason, main=None, ir=None,
                           film_present=None, **common)
        return _write_record(paths, res, dry_run)
    fp = aperture_crop.film_presence(visible, cov, dpi=dpi)
    if not fp.present:
        res = RecropResult(written=False, reason="empty aperture", main=None, ir=None,
                           film_present=False, **common)
        return _write_record(paths, res, dry_run)

    vis_c = aperture_crop.crop_to_aperture(visible, cov, dpi=dpi)
    ir_c = aperture_crop.crop_to_aperture(ir, cov, dpi=dpi) if ir is not None else None
    main_path = str(paths.main)
    ir_path = str(paths.ir) if (ir_c is not None and write_ir) else None
    if not dry_run:
        image.write_tiff16(_rotate(vis_c, rotate), paths.main, dpi=axis_dpi(dpi, rotate))
        if ir_path is not None:
            irr = _rotate(ir_c, rotate)
            image.write_tiff16(np.stack([irr, irr, irr], axis=-1), paths.ir,
                               dpi=axis_dpi(dpi, rotate))
    res = RecropResult(written=not dry_run, reason=cov.reason if dry_run else "ok",
                       main=main_path, ir=ir_path, film_present=True, **common)
    return _write_record(paths, res, dry_run)


def _write_record(paths: RecropPaths, res: RecropResult, dry_run: bool) -> RecropResult:
    """Write `<stem>.recrop.json` from ``res`` and return ``res``; a write
    failure is carried back in ``record_error`` (the products, if any, are
    already on disk, so the run is not failed for its record)."""
    if dry_run:
        return res
    rec = asdict(res)
    rec.pop("record_error", None)
    rec["finished_utc"] = datetime.now(timezone.utc).isoformat()
    try:
        paths.record.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        return replace(res, record_error=str(e))
    return res
