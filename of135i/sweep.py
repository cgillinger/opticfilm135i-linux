"""Whole-strip sweep post-processing: find the apertures in one long scan
and say where to cut it (docs/whole-strip-sweep-design.md section 5).

Pure and offline-testable: nothing here touches the scanner. The sweep
file is the raw wire buffer -- pixel-interleaved RGB16LE, alternating
lines, even = IR, odd = visible -- written chunk by chunk by the CLI.
It is 1.3-2 GB, so it is only ever read through a read-only memory map
and block-wise reductions; no whole-sweep array exists.

This module only decides WHERE to cut and WHICH aperture number a cut
carries. It never produces an image: every window is handed to the
existing per-frame writer (cli._finish_digitize_frame), which measures
coverage on the window again and refuses what does not verify.

Numbering rule (section 5, "Aperture numbering"): an aperture number
comes from the MEASURED run edges and the holder pitch relative to the
sweep's own commanded start -- never from position alone. Runs that do
not agree on one strip offset leave the sweep UNRESOLVED (no window, a
reason); the sweep is never numbered by a guess.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import aperture, aperture_crop, holder, image

#: All plausible runs must agree on one strip offset to within this, mm.
#: The load-to-load mapping varies +-0.24 mm and the pitch residual is
#: 0.022 mm (docs/holder-position-design.md); 1.0 mm leaves room for
#: the edge finder yet is far below any mis-numbering (a pitch is 38 mm).
SWEEP_OFFSET_SPREAD_MM = 1.0
#: The largest strip offset (measured minus predicted, mm) the sweep
#: accepts. The DPI-change shift is ~7.5 mm (docs/dpi-drift-analysis.md).
#: Beyond ~half a pitch (19 mm) the nearest-aperture assignment would be
#: off by a whole aperture and no run could tell; 12 mm keeps a wide
#: margin to that. Added by the implementation, not in the design doc.
SWEEP_MAX_OFFSET_MM = 12.0


@dataclass(frozen=True)
class SweepWindow:
    """One aperture's cut of the sweep, in WIRE lines [wire_start,
    wire_end) -- both even, so the IR/visible parity is kept."""

    aperture: int
    wire_start: int
    wire_end: int
    located_by: str            #: "edges" (a measured run) or "pitch"


@dataclass
class SweepResolution:
    resolved: bool
    reason: str
    offset_mm: float | None
    #: every lit run found: (start_line, end_line, length_mm), visible lines
    runs: list = field(default_factory=list)
    #: aperture number -> index into ``runs`` for each plausible run
    assigned: dict = field(default_factory=dict)
    windows: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "resolved": self.resolved, "reason": self.reason,
            "offset_mm": self.offset_mm,
            "runs": [list(r) for r in self.runs],
            "assigned": {str(k): v for k, v in self.assigned.items()},
            "windows": {str(k): asdict(w) for k, w in self.windows.items()},
        }


def open_raw(path: str | Path, width: int) -> np.memmap:
    """The sweep file as a read-only (lines, width, 3) '<u2' memory map.
    A trailing partial line is dropped, as image.assemble does."""
    line_bytes = width * 3 * 2
    size = Path(path).stat().st_size
    lines = size // line_bytes
    if lines == 0:
        raise ValueError(f"{path}: smaller than one line of width {width}")
    return np.memmap(path, dtype="<u2", mode="r", shape=(lines, width, 3))


def ir_profile(mm: np.ndarray, *, band: float = 0.5,
               block_lines: int = 1024) -> np.ndarray:
    """Along-strip profile of the IR lines (even wire lines): mean over
    the central ``band`` of the width and the three channels, block by
    block. Index = visible line index (wire line // 2). The colour
    stagger of the IR channels is irrelevant here (it matters only in the
    per-window coverage step, which runs the exact per-frame pipeline)."""
    n_wire, width = mm.shape[0], mm.shape[1]
    n_vis = n_wire // 2
    half = width * min(max(band, 0.0), 1.0) / 2.0
    lo = max(0, int(round(width / 2.0 - half)))
    hi = min(width, max(lo + 1, int(round(width / 2.0 + half))))
    out = np.empty(n_vis, dtype=np.float64)
    for v0 in range(0, n_vis, block_lines):
        v1 = min(n_vis, v0 + block_lines)
        blk = mm[2 * v0:2 * v1:2, lo:hi, :]
        out[v0:v1] = blk.mean(axis=(1, 2), dtype=np.float64)
    return out


def _even_floor(x: float) -> int:
    n = int(np.floor(x))
    return n - (n % 2)


def _even_ceil(x: float) -> int:
    n = int(np.ceil(x))
    return n + (n % 2)


def find_windows(profile: np.ndarray, geom, *, dpi: int,
                 overscan_mm: float | None = None) -> SweepResolution:
    """Locate the apertures in ``profile`` (visible-line IR profile of the
    sweep) and cut one window per aperture ``geom.first..geom.last``.

    ``geom`` needs ``first``, ``last``, ``predicted_edges`` (aperture ->
    (lead, trail) visible lines of the raw sweep). See the module
    docstring for the numbering rule and design doc section 5 for the
    steps. Returns an unresolved SweepResolution (no windows) when the
    runs do not agree.
    """
    ov = holder.OVERSCAN_MM if overscan_mm is None else overscan_mm
    lpm = dpi / aperture_crop.MM_PER_INCH
    n_vis = len(profile)

    # 1) Edge finder at ~600 dpi, exactly as measure_coverage does.
    factor = max(1, round(dpi / 600))
    if factor > 1:
        usable = (n_vis // factor) * factor
        binned = np.asarray(profile[:usable], dtype=np.float64).reshape(-1, factor).mean(axis=1)
        _, runs_b = aperture.apertures(binned, lpm / factor)
        raw_runs = [(a * factor, b * factor) for a, b in runs_b]
    else:
        _, raw_runs = aperture.apertures(profile, lpm)
    runs = [(float(a), float(b), (b - a) / lpm) for a, b in raw_runs]
    if not runs:
        return SweepResolution(
            False, "no lit run of at least "
            f"{aperture.MIN_APERTURE_MM:g} mm in the sweep", None, runs)

    # 2) Plausible = the holder's aperture length (mean of the six).
    mean_len = sum(holder.STRIP_APERTURE_MM) / len(holder.STRIP_APERTURE_MM)
    plausible = [i for i, r in enumerate(runs)
                 if abs(r[2] - mean_len) <= aperture_crop.SIDE_BAND_LENGTH_TOL_MM]
    if not plausible:
        return SweepResolution(
            False, f"no plausible run: {len(runs)} lit run(s) of "
            f"{', '.join(f'{r[2]:.1f}' for r in runs)} mm, none within "
            f"{aperture_crop.SIDE_BAND_LENGTH_TOL_MM:g} mm of the holder's "
            f"{mean_len:.1f} mm aperture", None, runs)

    # 3) Assign each plausible run to the aperture whose predicted leading
    #    edge is nearest its start; all must agree on one offset.
    apertures_ = list(range(geom.first, geom.last + 1))
    assigned: dict[int, int] = {}
    residual: dict[int, float] = {}
    for i in plausible:
        start = runs[i][0]
        k = min(apertures_, key=lambda a: abs(start - geom.predicted_edges[a][0]))
        res_mm = (start - geom.predicted_edges[k][0]) / lpm
        if k in assigned:
            return SweepResolution(
                False, f"two plausible runs (at {runs[assigned[k]][0]:.0f} and "
                f"{start:.0f} lines) claim aperture {k}", None, runs)
        assigned[k] = i
        residual[k] = res_mm
    order = sorted(assigned)
    if [assigned[k] for k in order] != sorted(assigned.values()):
        return SweepResolution(
            False, "plausible runs map to apertures out of order", None, runs)
    spread = max(residual.values()) - min(residual.values())
    if spread > SWEEP_OFFSET_SPREAD_MM:
        return SweepResolution(
            False, f"plausible runs disagree on the strip offset: spread "
            f"{spread:.2f} mm > {SWEEP_OFFSET_SPREAD_MM:g} mm "
            f"({', '.join(f'aperture {k}: {residual[k]:+.2f}' for k in order)} mm)",
            None, runs)
    offset_mm = float(np.median([residual[k] for k in order]))
    if abs(offset_mm) > SWEEP_MAX_OFFSET_MM:
        return SweepResolution(
            False, f"strip offset {offset_mm:+.2f} mm exceeds "
            f"{SWEEP_MAX_OFFSET_MM:g} mm: the aperture numbers cannot be "
            f"told from the runs", offset_mm, runs)

    # 4) A window for every aperture first..last.
    off_lines = offset_mm * lpm
    over = ov * lpm
    crop = image.align_shift(dpi)
    n_wire = 2 * n_vis
    windows: dict[int, SweepWindow] = {}
    for k in apertures_:
        if k in assigned:
            a, b = runs[assigned[k]][0], runs[assigned[k]][1]
            how = "edges"
        else:
            a = geom.predicted_edges[k][0] + off_lines
            b = geom.predicted_edges[k][1] + off_lines
            how = "pitch"
        v0, v1 = a - over - crop, b + over + crop
        w0 = max(0, _even_floor(2 * v0))
        w1 = min(n_wire - (n_wire % 2), _even_ceil(2 * v1))
        if w1 <= w0:
            return SweepResolution(
                False, f"aperture {k}'s window falls outside the sweep",
                offset_mm, runs, assigned)
        windows[k] = SweepWindow(k, w0, w1, how)
    return SweepResolution(True, "ok", offset_mm, runs, assigned, windows)


def window_bytes(mm: np.ndarray, w: SweepWindow) -> memoryview:
    """The window as a flat bytes-like view that image.assemble (and so
    image.split_ir) accepts unchanged; pages in lazily from the map."""
    return memoryview(mm[w.wire_start:w.wire_end]).cast("B")
