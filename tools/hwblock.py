#!/usr/bin/env python3
"""Autonomous hardware test block for the of135i driver.

Purpose: the scanner is a single irreplaceable unit and the human
should not be used as a test operator. This runs a long block of
ALREADY-VERIFIED operations (the same ones docs/test-log.md's Test 1,
2/9 and 7 exercised by hand) behind ONE human action, records
everything (images, .diag.json sidecars, summary.json, report.md), and
stops on the first anomaly. It never invents a new hardware sequence.

HARD SAFETY RULES (see the module's own code, not just this comment):
  - The only hardware operations used are Scanner.is_magazine_loaded(),
    Scanner.initialize(), Scanner.scan(), Scanner.eject(),
    diag.collect_doctor() (read-only) and UsbIo.open()/close(). Nothing
    else -- no home(), no cold_init() (initialize() triggers that
    itself when needed), no _motor_run, no register writes.
  - The first exception anywhere in a running block (including a USB
    timeout) stops the block immediately: no retries of anything that
    moves the motor. The report is written with status FAILED and the
    traceback, and the process exits non-zero.
  - Nothing touches USB until argument parsing has validated the
    command line AND --out has been created on disk.

Usage:
    .venv/bin/python tools/hwblock.py warm --out DIR [--frame N]
        [--repeat N] [--eject] [--skip-dpi-change]
    .venv/bin/python tools/hwblock.py cold --out DIR [--frame N] [--eject]

Run with -h/--help for the full option list; that is handled by
argparse before any of this module's own code runs, and the script
does nothing at all without an explicit "warm" or "cold" subcommand.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from of135i import diag, image  # noqa: E402
from of135i.device import Scanner  # noqa: E402
from of135i.usbio import Of135iError, UsbIo  # noqa: E402

log = logging.getLogger("of135i.hwblock")


# =====================================================================
# Pure analysis functions -- no USB, no side effects, fully unit-testable
# (see tests/test_hwblock.py). Every one of these takes numpy arrays or
# plain dicts and returns a plain dict (JSON-serializable via
# diag.write_sidecar's numpy-aware encoder).
# =====================================================================


def image_stats(visible: np.ndarray) -> dict:
    """Per-channel mean/std and a FLAT verdict for a (lines, width, 3)
    visible-light array.

    FLAT (the known signature of a broken/blank scan -- see docs/
    test-log.md Test 9's cold-start scans) is all three channels'
    std/mean ratio below 0.20; real negatives measured ~0.6-0.8 in
    that same test.
    """
    arr = np.asarray(visible)
    if arr.ndim != 3:
        raise ValueError(f"expected (lines, width, channels) array, got shape {arr.shape}")
    lines, width, channels = arr.shape
    px = arr.astype(np.float64)
    means = px.mean(axis=(0, 1))
    stds = px.std(axis=(0, 1))
    ratios = np.divide(stds, means, out=np.zeros_like(means), where=means != 0)
    return {
        "shape": [int(lines), int(width), int(channels)],
        "channel_mean": [float(x) for x in means],
        "channel_std": [float(x) for x in stds],
        "std_mean_ratio": [float(x) for x in ratios],
        "flat": bool(np.all(ratios < 0.20)),
    }


_FILM_ROW_SMOOTH_WINDOW = 21


def _moving_average(x: np.ndarray, window: int) -> np.ndarray:
    """Centered moving average, edge-padded so the output has the same
    length as the input (numpy-only, via cumsum)."""
    x = np.asarray(x, dtype=np.float64)
    if window <= 1 or x.size == 0:
        return x.copy()
    pad_lo = window // 2
    pad_hi = window - 1 - pad_lo
    xp = np.pad(x, (pad_lo, pad_hi), mode="edge")
    csum = np.cumsum(np.insert(xp, 0, 0.0))
    return (csum[window:] - csum[:-window]) / window


def film_rows(visible: np.ndarray) -> dict:
    """Film-edge row detection from a visible-light array's G channel:
    smooth the per-row mean, threshold at the midpoint between the 5th
    and 95th percentile, and report the first/last row above it.

    `bottom_cut` flags when the detected film end is within 5 rows of
    the bottom of the frame -- i.e. the frame may have been cropped
    before the film actually ended (see docs/test-log.md Test 7, where
    a DPI-change position shift produced exactly this).
    """
    arr = np.asarray(visible)
    if arr.ndim != 3 or arr.shape[2] < 2:
        raise ValueError(f"expected (lines, width, >=2 channels) array, got shape {arr.shape}")
    lines = arr.shape[0]
    g_row_mean = arr[..., 1].astype(np.float64).mean(axis=1)
    smoothed = _moving_average(g_row_mean, _FILM_ROW_SMOOTH_WINDOW)
    p5, p95 = np.percentile(smoothed, [5, 95])
    threshold = (p5 + p95) / 2.0
    above = np.nonzero(smoothed > threshold)[0]
    if above.size == 0:
        film_start_row, film_end_row = -1, -1
    else:
        film_start_row, film_end_row = int(above[0]), int(above[-1])
    return {
        "film_start_row": film_start_row,
        "film_end_row": film_end_row,
        "bottom_cut": bool(film_end_row >= lines - 5),
    }


def _downscale8(a: np.ndarray) -> np.ndarray:
    """Block-mean downscale by /8 in both spatial axes (a (H, W) or
    (H, W, C) array), then rescaled to an 8-bit-ish float range
    (assumes 16-bit input). Cheap and precise enough for a stability
    check, not for anything photometric."""
    a = np.asarray(a, dtype=np.float64)
    h, w = a.shape[0], a.shape[1]
    h8, w8 = h // 8, w // 8
    if h8 == 0 or w8 == 0:
        raise ValueError(f"array too small to downscale by 8: shape {a.shape}")
    if a.ndim == 3:
        c = a.shape[2]
        cropped = a[: h8 * 8, : w8 * 8, :]
        small = cropped.reshape(h8, 8, w8, 8, c).mean(axis=(1, 3))
    else:
        cropped = a[: h8 * 8, : w8 * 8]
        small = cropped.reshape(h8, 8, w8, 8).mean(axis=(1, 3))
    return small / 257.0  # 16-bit -> ~8-bit scale


def pair_rms_8bit(a: np.ndarray, b: np.ndarray) -> float:
    """RMS pixel difference between two same-shape images, computed on
    an 8-bit-scale /8 block-mean downscale of each (see _downscale8) --
    cheap enough to run on every consecutive pair of a reproducibility
    set (W1)."""
    da, db = _downscale8(a), _downscale8(b)
    if da.shape != db.shape:
        raise ValueError(f"shape mismatch after downscale: {da.shape} vs {db.shape}")
    diff = da - db
    return float(np.sqrt(np.mean(diff * diff)))


def repro_summary(list_of_stats: list) -> dict:
    """Cross-scan summary of a reproducibility (W1) set.

    `list_of_stats` is one dict per scan, in scan order, each carrying
    at least "channel_mean" (from image_stats), and optionally "flat",
    "bottom_cut" (from film_rows) and "pair_rms_to_prev" (RMS vs. the
    previous scan, from pair_rms_8bit -- None/absent for the first
    scan). Never raises on an empty list.
    """
    if not list_of_stats:
        return {"n_scans": 0}
    means = np.array([s["channel_mean"] for s in list_of_stats], dtype=np.float64)  # (n, 3)
    ch_max, ch_min = means.max(axis=0), means.min(axis=0)
    ch_avg = means.mean(axis=0)
    spread_pct = np.divide(ch_max - ch_min, ch_avg, out=np.zeros_like(ch_avg), where=ch_avg != 0) * 100.0
    drift_pct = np.divide(
        means[-1] - means[0], means[0], out=np.zeros_like(means[0]), where=means[0] != 0
    ) * 100.0
    pair_rms = [s["pair_rms_to_prev"] for s in list_of_stats
                if s.get("pair_rms_to_prev") is not None]
    return {
        "n_scans": len(list_of_stats),
        "channel_mean_spread_pct": [float(x) for x in spread_pct],
        "drift_first_to_last_pct": [float(x) for x in drift_pct],
        "pair_rms": [float(x) for x in pair_rms],
        "pair_rms_max": float(max(pair_rms)) if pair_rms else None,
        "flat_any": any(bool(s.get("flat")) for s in list_of_stats),
        "bottom_cut_any": any(bool(s.get("bottom_cut")) for s in list_of_stats),
    }


# Reference-unit vendor AFE offset codes (calibrate.py's offset_codes()
# docstring / docs/test-log.md): R=0x010B, G=0x010A, B=0x010B.
_REFERENCE_OFFSET_CODES = (0x010B, 0x010A, 0x010B)
_OFFSET_SPREAD_UNSTABLE = 4       # codes
_OFFSET_REFERENCE_FAR = 64        # codes


def offset_summary(list_of_sidecars: list) -> dict:
    """Per-channel AFE offset/gain code summary across a set of scan
    sidecars (each a dict with "offset_codes": [r, g, b] and
    "gain_codes": [r, g, b], as written into Scanner.last_diag).

    Reports min/max/spread and the mean's distance from the reference-
    unit vendor codes; flags a channel "unstable" if its spread exceeds
    4 codes, and "far from reference -- review" if the mean is more
    than 64 codes from the reference. Does not otherwise judge pass/
    fail -- a driver run on a different unit is expected to differ.
    """
    offsets = np.array([sc["offset_codes"] for sc in list_of_sidecars], dtype=np.float64)
    gains = np.array([sc["gain_codes"] for sc in list_of_sidecars], dtype=np.float64)
    channels = []
    for ch, name in enumerate(("R", "G", "B")):
        off_col, gain_col = offsets[:, ch], gains[:, ch]
        lo, hi = float(off_col.min()), float(off_col.max())
        spread = hi - lo
        ref = _REFERENCE_OFFSET_CODES[ch]
        delta = float(off_col.mean()) - ref
        flags = []
        if spread > _OFFSET_SPREAD_UNSTABLE:
            flags.append("unstable")
        if abs(delta) > _OFFSET_REFERENCE_FAR:
            flags.append("far from reference — review")
        channels.append({
            "channel": name,
            "offset_min": lo, "offset_max": hi, "offset_spread": spread,
            "gain_min": float(gain_col.min()), "gain_max": float(gain_col.max()),
            "reference_offset": ref, "delta_from_reference": delta,
            "flags": flags,
        })
    return {"n_scans": len(list_of_sidecars), "channels": channels}


def _warmup_flags(list_of_sidecars: list) -> dict:
    """W3: lamp-warmup counters across a set of scan sidecars."""
    attempts = [sc.get("warmup_attempts") for sc in list_of_sidecars]
    exhausted = [bool(sc.get("warmup_exhausted")) for sc in list_of_sidecars]
    clipped = [tuple(sc.get("gain_codes") or ()) == (0x3F, 0x3F, 0x3F) for sc in list_of_sidecars]
    return {
        "warmup_attempts": attempts,
        "warmup_exhausted": exhausted,
        "gain_clipped": clipped,
        "any_exhausted": any(exhausted),
        "any_gain_clipped": any(clipped),
    }


def dpi_shift(start_row: int, reference_rows: list, dpi: int) -> dict:
    """Position shift of a frame's detected film-start row against a
    reference set (typically the W1 reproducibility set's film_start_
    row values, all taken at the same dpi), in rows and millimeters.

    docs/test-log.md Test 7 measured +1059 rows / 7.5 mm at 3600 dpi
    when a DPI change shifted the carriage's PARK position.
    """
    ref_median = float(np.median(reference_rows)) if reference_rows else float(start_row)
    shift_rows = float(start_row) - ref_median
    shift_mm = shift_rows * 25.4 / dpi
    return {
        "reference_median_row": ref_median,
        "shift_rows": shift_rows,
        "shift_mm": shift_mm,
    }


def _render_step_table(lines: list, data) -> None:
    """Append a small markdown table (or a plain line) for one step's
    data to `lines`. Never raises -- an unexpected shape just renders
    as str()."""
    if isinstance(data, dict):
        lines.append("| field | value |")
        lines.append("|---|---|")
        for k, v in data.items():
            try:
                v_str = json.dumps(v, default=str) if isinstance(v, (list, dict)) else str(v)
            except Exception:
                v_str = str(v)
            if len(v_str) > 300:
                v_str = v_str[:300] + "…"
            lines.append(f"| {k} | {v_str} |")
    else:
        lines.append(str(data))


def _collect_findings(summary: dict) -> list:
    """Walk a summary dict and collect every flag it raised, as plain
    strings prefixed with the step path they came from. Defensive
    throughout (isinstance/.get only) so a partial or oddly-shaped
    summary never raises."""
    findings: list = []
    bool_flags = (
        ("flat", "image flat"),
        ("bottom_cut", "bottom of frame cut off"),
        ("warmup_exhausted", "lamp warmup retries exhausted"),
        ("flat_any", "at least one flat image in the set"),
        ("bottom_cut_any", "at least one bottom-cut image in the set"),
        ("any_exhausted", "lamp warmup retries exhausted on at least one scan"),
        ("any_gain_clipped", "gain clipped to maximum on at least one scan"),
    )

    def walk(path: str, obj) -> None:
        if isinstance(obj, dict):
            flags = obj.get("flags")
            if isinstance(flags, list):
                for f in flags:
                    findings.append(f"{path}: {f}")
            for key, word in bool_flags:
                if obj.get(key) is True:
                    findings.append(f"{path}: {word}")
            if obj.get("magazine_loaded") is False:
                findings.append(f"{path}: no magazine detected")
            if obj.get("enumerated") is False:
                findings.append(f"{path}: scanner did not re-enumerate")
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    walk(f"{path}.{k}", v)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(f"{path}[{i}]", item)

    for step_name, step_data in (summary.get("steps") or {}).items():
        walk(step_name, step_data)
    return findings


def render_report(summary: dict) -> str:
    """Render a summary dict (see the module docstring / _new_summary)
    as a Markdown report. Never raises on a partial summary -- e.g.
    one where only W0 has been recorded so far, as written mid-block
    after every step."""
    lines: list = []
    lines.append("# of135i hardware test block report")
    lines.append("")
    lines.append(f"- Block: {summary.get('block', '?')}")
    lines.append(f"- Started (UTC): {summary.get('started_utc', '?')}")
    lines.append(f"- Finished (UTC): {summary.get('finished_utc') or 'in progress'}")
    args = summary.get("args") or {}
    if isinstance(args, dict) and args:
        lines.append("- Arguments: " + ", ".join(f"{k}={v}" for k, v in sorted(args.items())))
    lines.append(f"- Driver revision: {summary.get('driver_revision') or 'unknown'}")
    lines.append("")

    steps = summary.get("steps") or {}
    for name in sorted(steps.keys()):
        lines.append(f"## {name}")
        lines.append("")
        _render_step_table(lines, steps[name])
        lines.append("")

    lines.append("## Findings")
    lines.append("")
    findings = _collect_findings(summary)
    if findings:
        for f in findings:
            lines.append(f"- {f}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Status")
    lines.append("")
    lines.append(f"Status: {summary.get('status', 'UNKNOWN')}")
    if summary.get("traceback"):
        lines.append("")
        lines.append("```")
        lines.append(str(summary["traceback"]))
        lines.append("```")

    return "\n".join(lines)


# =====================================================================
# Report plumbing (thin)
# =====================================================================


def _new_summary(block: str, args: argparse.Namespace) -> dict:
    return {
        "block": block,
        "args": {k: v for k, v in vars(args).items() if k != "command"},
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "finished_utc": None,
        "driver_revision": None,
        "steps": {},
        "status": "RUNNING",
    }


def _save(out_dir: Path, summary: dict) -> None:
    """Write summary.json and report.md, and print the report to
    stdout. Called after every step so a crash mid-block leaves a
    partial report on disk."""
    diag.write_sidecar(str(out_dir / "summary.json"), summary)
    (out_dir / "report.md").write_text(render_report(summary))


_SAFETY_STOP_MESSAGE = (
    "\n*** BLOCK FAILED -- STOPPED. ***\n"
    "Check the scanner visually and LISTEN for abnormal sounds before "
    "doing anything else. Do not run any further scan/eject/motor "
    "commands until you have confirmed the transport is not stalled.\n"
)


# =====================================================================
# warm block
# =====================================================================


def _print_warm_steps(args: argparse.Namespace) -> None:
    dpi_line = (
        "  - Skip the DPI-change position test."
        if args.skip_dpi_change else
        "  - Change to 2400 dpi and back to 3600 dpi once, scanning "
        f"frame {args.frame} at each (position-shift check, no further "
        "DPI changes after)."
    )
    eject_line = (
        "  - Eject the magazine at the very end."
        if args.eject else
        "  - Leave the magazine loaded at the end."
    )
    print(textwrap.dedent(f"""
        ============================================================
        of135i AUTONOMOUS HARDWARE TEST BLOCK -- WARM
        ============================================================
        Before you continue, confirm:
          1. The film magazine is already loaded (tools/load_magazine.py
             has already been run) and the scanner is idle/homed.
          2. Nothing else is using the scanner (no VM, no other process
             holding the USB device).
          3. You will stay nearby for the whole block to listen for
             abnormal sounds.

        STOP RULE: a scraping or grinding noise means cut the power
        immediately. Do not wait to see what happens next.

        This block will then run, WITHOUT further prompts:
          - Read a baseline health report (no motor movement).
          - Scan frame {args.frame} at 3600 dpi {args.repeat} times in a
            row (reproducibility check).
          - Scan frames 1-4 as a batch.
        {dpi_line}
        {eject_line}

        All output goes to: {args.out}
        ============================================================
        """))


def run_warm(args: argparse.Namespace, out_dir: Path) -> int:
    summary = _new_summary("warm", args)
    _save(out_dir, summary)

    _print_warm_steps(args)
    input("Press Enter to begin once you have completed the steps above... ")

    step = "W0"
    repro_stats: list = []
    try:
        with Scanner.open() as scanner:
            scanner.park_mode = args.park
            # ---- W0: precheck (read-only + is_magazine_loaded) --------
            doctor = diag.collect_doctor(scanner.io)
            print(diag.format_doctor(doctor))
            diag.write_sidecar(str(out_dir / "doctor-baseline.json"), doctor)
            summary["driver_revision"] = (doctor.get("host") or {}).get("driver_revision")
            state_name = (doctor.get("state") or {}).get("name", "unknown")
            loaded = scanner.is_magazine_loaded()
            summary["steps"]["W0"] = {"state": state_name, "magazine_loaded": loaded}
            _save(out_dir, summary)

            if not loaded and not args.assume_loaded:
                print("error: no magazine detected -- insert the cassette and "
                      "run tools/load_magazine.py first (or, if a person has "
                      "visually confirmed the magazine is loaded and locked and "
                      "the sensor bit is stale because a previous session already "
                      "ran initialize(), re-run with --assume-loaded)",
                      file=sys.stderr)
                summary["status"] = "FAILED at step W0 (no magazine detected)"
                _save(out_dir, summary)
                return 1
            if not loaded:
                print("WARNING: loader sensor reads 'not loaded' but --assume-loaded "
                      "given (human confirmed magazine loaded and locked); continuing")
                summary["steps"]["W0"]["assume_loaded"] = True
                _save(out_dir, summary)
            if state_name == "cold-never-homed":
                print("error: scanner reports cold-never-homed -- the warm "
                      "block expects an already-homed scanner. Run the "
                      "'cold' block instead.", file=sys.stderr)
                summary["status"] = "FAILED at step W0 (cold scanner -- use 'cold' block)"
                _save(out_dir, summary)
                return 1
            if state_name != "idle-homed":
                # 2026-09-04: starting a session while reg 0x01 read 0x23
                # (scan bit still set from an aborted calibration) ended in
                # a motor whine and a firmware hang (USB enumerated, every
                # control read timing out) -- the only recovery was a
                # power cycle.  An engine left running by an aborted
                # session must not be re-initialized on top of; refuse.
                raw01 = (doctor.get("state") or {}).get("raw")
                print(f"error: reg 0x01 = {raw01} is neither idle-homed (0x22) nor "
                      "cold (0x00). The scanner is in an undefined state (typically "
                      "an aborted session left the scan engine running). Do NOT "
                      "start a new session on top of it: power-cycle the scanner, "
                      "then run the 'cold' block or load_magazine.py.", file=sys.stderr)
                summary["status"] = f"FAILED at step W0 (reg 0x01 = {raw01}, undefined state -- power-cycle first)"
                _save(out_dir, summary)
                return 1

            # ---- W1: reproducibility ------------------------------------
            step = "W1"
            prev_visible = None
            for i in range(args.repeat):
                scanner.initialize(ir=True, dpi=3600)
                raw, width, _meta = scanner.scan(frame=args.frame, ir=True, dpi=3600)
                visible, _ir = image.split_ir(raw, width=width)
                visible = image.align_channels(visible, dpi=3600)
                del raw, _ir

                tag = f"repro-{i:02d}"
                image.write_tiff16(visible, str(out_dir / f"{tag}.tiff"))
                sidecar = dict(scanner.last_diag)
                diag.write_sidecar(str(out_dir / f"{tag}.diag.json"), sidecar)

                stats = image_stats(visible)
                stats.update(film_rows(visible))
                stats["_sidecar"] = sidecar
                if prev_visible is not None:
                    stats["pair_rms_to_prev"] = pair_rms_8bit(prev_visible, visible)
                else:
                    stats["pair_rms_to_prev"] = None
                repro_stats.append(stats)
                prev_visible = visible

                summary["steps"].setdefault("W1", {})["scans_done"] = i + 1
                _save(out_dir, summary)
            del prev_visible
            summary["steps"]["W1"] = {
                "scans_done": args.repeat,
                "stats": [{k: v for k, v in s.items() if k != "_sidecar"} for s in repro_stats],
            }
            _save(out_dir, summary)

            # ---- W2: AFE offset check (from W1 sidecars, no new scans) --
            step = "W2"
            sidecars = [s["_sidecar"] for s in repro_stats]
            summary["steps"]["W2"] = offset_summary(sidecars)
            _save(out_dir, summary)

            # ---- W3: warmup counters (from W1 sidecars) ------------------
            step = "W3"
            summary["steps"]["W3"] = _warmup_flags(sidecars)
            _save(out_dir, summary)

            # ---- W4: cross-scan image analysis (from W1 stats) -----------
            step = "W4"
            summary["steps"]["W4"] = repro_summary(repro_stats)
            _save(out_dir, summary)

            # ---- W5: batch, frames 1-4 -----------------------------------
            step = "W5"
            batch_frames = []
            for frame in range(1, 5):
                scanner.initialize(ir=True, dpi=3600)
                raw, width, _meta = scanner.scan(frame=frame, ir=True, dpi=3600)
                visible, _ir = image.split_ir(raw, width=width)
                visible = image.align_channels(visible, dpi=3600)
                del raw, _ir

                tag = f"batch-frame-{frame}"
                image.write_tiff16(visible, str(out_dir / f"{tag}.tiff"))
                diag.write_sidecar(str(out_dir / f"{tag}.diag.json"), dict(scanner.last_diag))

                frame_stats = image_stats(visible)
                frame_stats.update(film_rows(visible))
                frame_stats["frame"] = frame
                batch_frames.append(frame_stats)
                del visible

                summary["steps"].setdefault("W5", {})["frames_done"] = frame
                _save(out_dir, summary)
            summary["steps"]["W5"] = {"frames_done": 4, "frames": batch_frames}
            _save(out_dir, summary)

            # ---- W7 (only here if the DPI-change test is skipped) --------
            if args.skip_dpi_change and args.eject:
                step = "W7"
                scanner.eject()
                summary["steps"]["W7"] = {"ejected": True}
                _save(out_dir, summary)

        # ---- W6: DPI-change position test, a NEW session ------------------
        if not args.skip_dpi_change:
            step = "W6"
            with Scanner.open() as scanner:
                scanner.park_mode = args.park
                scanner.initialize(ir=True, dpi=2400)
                raw, width, _meta = scanner.scan(frame=args.frame, ir=True, dpi=2400)
                visible_2400, _ir = image.split_ir(raw, width=width)
                visible_2400 = image.align_channels(visible_2400, dpi=2400)
                del raw, _ir
                image.write_tiff16(visible_2400, str(out_dir / "dpichange-2400.tiff"))
                diag.write_sidecar(str(out_dir / "dpichange-2400.diag.json"), dict(scanner.last_diag))
                del visible_2400

                scanner.initialize(ir=True, dpi=3600)
                raw, width, _meta = scanner.scan(frame=args.frame, ir=True, dpi=3600)
                visible_3600, _ir = image.split_ir(raw, width=width)
                visible_3600 = image.align_channels(visible_3600, dpi=3600)
                del raw, _ir
                image.write_tiff16(visible_3600, str(out_dir / "dpichange-3600.tiff"))
                diag.write_sidecar(str(out_dir / "dpichange-3600.diag.json"), dict(scanner.last_diag))

                fr = film_rows(visible_3600)
                del visible_3600
                reference_rows = [s["film_start_row"] for s in repro_stats]
                shift = dpi_shift(fr["film_start_row"], reference_rows, 3600)
                summary["steps"]["W6"] = {**fr, **shift}
                _save(out_dir, summary)

                if args.eject:
                    step = "W7"
                    scanner.eject()
                    summary["steps"]["W7"] = {"ejected": True}
                    _save(out_dir, summary)

    except Exception:
        summary["status"] = f"FAILED at step {step}"
        summary["traceback"] = traceback.format_exc()
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        _save(out_dir, summary)
        print(_SAFETY_STOP_MESSAGE, file=sys.stderr)
        print(render_report(summary))
        return 1

    summary["status"] = "COMPLETED"
    summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
    _save(out_dir, summary)
    print(render_report(summary))
    print("\nReload the magazine before doing anything else.")
    return 0


# =====================================================================
# cold block
# =====================================================================


def _print_cold_steps(args: argparse.Namespace) -> None:
    eject_line = (
        "  - Eject the magazine at the very end."
        if args.eject else
        "  - Leave the magazine loaded at the end."
    )
    print(textwrap.dedent(f"""
        ============================================================
        of135i AUTONOMOUS HARDWARE TEST BLOCK -- COLD
        ============================================================
        Before you continue:
          1. Power the scanner OFF, wait 10 seconds, then power it back ON.
          2. Do NOT touch the film magazine.
          3. Press Enter below once the scanner is powered back on.

        STOP RULE: a scraping or grinding noise means cut the power
        immediately. Do not wait to see what happens next.

        This block will then run, WITHOUT further prompts:
          - Wait for the scanner to re-enumerate on the USB bus (up to
            90 seconds; this only enumerates, no register writes).
          - Read a baseline health report (no motor movement).
          - Run the normal cold-start path (initialize() triggers
            cold_init() itself) and scan frame {args.frame} at 3600 dpi
            -- this exercises the lamp-warmup retry and the dark-
            bracket AFE offset.
          - Scan frame {args.frame} again in the same session (lamp now
            warmer).
        {eject_line}

        All output goes to: {args.out}
        ============================================================
        """))


def run_cold(args: argparse.Namespace, out_dir: Path) -> int:
    summary = _new_summary("cold", args)
    _save(out_dir, summary)

    _print_cold_steps(args)
    input("Press Enter once the scanner has been power-cycled... ")

    step = "C0"
    try:
        # ---- C0: wait for re-enumeration (UsbIo.open()/close() only) ----
        print("Waiting for the scanner to re-enumerate on the USB bus (up to 90s)...")
        deadline = time.monotonic() + 90.0
        enumerated = False
        while time.monotonic() < deadline:
            try:
                io = UsbIo.open()
                io.close()
                enumerated = True
                break
            except Of135iError:
                time.sleep(2.0)
        summary["steps"]["C0"] = {"enumerated": enumerated}
        _save(out_dir, summary)
        if not enumerated:
            summary["status"] = "FAILED at step C0 (scanner did not re-enumerate within 90s)"
            summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
            _save(out_dir, summary)
            print(_SAFETY_STOP_MESSAGE, file=sys.stderr)
            print(render_report(summary))
            return 1

        with Scanner.open() as scanner:
            scanner.park_mode = args.park
            # ---- C1: doctor (read-only) -----------------------------------
            step = "C1"
            doctor = diag.collect_doctor(scanner.io)
            print(diag.format_doctor(doctor))
            diag.write_sidecar(str(out_dir / "doctor-cold.json"), doctor)
            summary["driver_revision"] = (doctor.get("host") or {}).get("driver_revision")
            state_name = (doctor.get("state") or {}).get("name", "unknown")
            c1 = {"state": state_name}
            if state_name == "idle-homed":
                c1["note"] = "scanner was not actually power-cycled?"
                print("note: scanner reports idle-homed -- was it actually "
                      "power-cycled? Continuing anyway.")
            summary["steps"]["C1"] = c1
            _save(out_dir, summary)

            # ---- C2: magazine check -----------------------------------------
            step = "C2"
            loaded = scanner.is_magazine_loaded()
            summary["steps"]["C2"] = {"magazine_loaded": loaded}
            _save(out_dir, summary)
            if not loaded:
                summary["status"] = "FAILED at step C2 (no magazine detected)"
                summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
                _save(out_dir, summary)
                return 1

            # ---- C3: cold-start scan (initialize() triggers cold_init) -----
            step = "C3"
            scanner.initialize(ir=True, dpi=3600)
            raw, width, _meta = scanner.scan(frame=args.frame, ir=True, dpi=3600)
            visible, _ir = image.split_ir(raw, width=width)
            visible = image.align_channels(visible, dpi=3600)
            del raw, _ir
            image.write_tiff16(visible, str(out_dir / "cold-scan.tiff"))
            d1 = dict(scanner.last_diag)
            diag.write_sidecar(str(out_dir / "cold-scan.diag.json"), d1)
            stats1 = image_stats(visible)
            stats1.update(film_rows(visible))
            del visible
            gain1 = tuple(d1.get("gain_codes") or ())
            summary["steps"]["C3"] = {
                "warmup_attempts": d1.get("warmup_attempts"),
                "warmup_gain_history": d1.get("warmup_gain_history"),
                "warmup_exhausted": d1.get("warmup_exhausted"),
                "gain_codes": d1.get("gain_codes"),
                "offset_codes": d1.get("offset_codes"),
                "image_stats": stats1,
                "verdict_warmup_retry": "triggered" if (d1.get("warmup_attempts") or 0) > 1 else "not triggered",
                "verdict_gain_clipped": "yes" if gain1 == (0x3F, 0x3F, 0x3F) else "no",
                "verdict_image": "flat" if stats1["flat"] else "real",
            }
            _save(out_dir, summary)

            # ---- C4: second scan, same session (lamp now warm) -------------
            step = "C4"
            scanner.initialize(ir=True, dpi=3600)
            raw, width, _meta = scanner.scan(frame=args.frame, ir=True, dpi=3600)
            visible2, _ir = image.split_ir(raw, width=width)
            visible2 = image.align_channels(visible2, dpi=3600)
            del raw, _ir
            image.write_tiff16(visible2, str(out_dir / "cold-scan-2.tiff"))
            d2 = dict(scanner.last_diag)
            diag.write_sidecar(str(out_dir / "cold-scan-2.diag.json"), d2)
            stats2 = image_stats(visible2)
            stats2.update(film_rows(visible2))
            del visible2
            gain2 = tuple(d2.get("gain_codes") or ())
            summary["steps"]["C4"] = {
                "warmup_attempts": d2.get("warmup_attempts"),
                "gain_codes": d2.get("gain_codes"),
                "offset_codes": d2.get("offset_codes"),
                "image_stats": stats2,
                "verdict_gain_clipped": "yes" if gain2 == (0x3F, 0x3F, 0x3F) else "no",
                "verdict_image": "flat" if stats2["flat"] else "real",
            }
            _save(out_dir, summary)

            # ---- C5: optional eject -----------------------------------------
            if args.eject:
                step = "C5"
                scanner.eject()
                summary["steps"]["C5"] = {"ejected": True}
                _save(out_dir, summary)

    except Exception:
        summary["status"] = f"FAILED at step {step}"
        summary["traceback"] = traceback.format_exc()
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        _save(out_dir, summary)
        print(_SAFETY_STOP_MESSAGE, file=sys.stderr)
        print(render_report(summary))
        return 1

    summary["status"] = "COMPLETED"
    summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
    _save(out_dir, summary)
    print(render_report(summary))
    return 0


# =====================================================================
# argument handling
# =====================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hwblock",
        description=(
            "Autonomous hardware test block for the of135i driver -- runs "
            "a long block of already-verified scan/eject operations behind "
            "one human action and stops on the first anomaly."
        ),
    )
    # No `required=True` here: main() checks args.command itself and
    # prints help + exits 2 when it's missing, per spec (rather than
    # argparse's own terser "the following arguments are required" error).
    sub = parser.add_subparsers(dest="command")

    p_warm = sub.add_parser(
        "warm",
        help="reproducibility + batch + DPI-change test "
             "(magazine already loaded, scanner idle/homed)",
    )
    p_warm.add_argument("--out", required=True, metavar="DIR", help="output directory (required)")
    p_warm.add_argument("--frame", type=int, default=1, help="frame number to scan (default 1)")
    p_warm.add_argument("--repeat", type=int, default=10,
                         help="number of reproducibility scans (default 10, minimum 2)")
    p_warm.add_argument("--eject", action="store_true", help="eject the magazine at the very end")
    p_warm.add_argument("--assume-loaded", action="store_true",
                        help="skip the loader-sensor precheck when a person has visually "
                             "confirmed the magazine is loaded and locked (the sensor bit "
                             "is unreliable once a previous session has run initialize())")
    p_warm.add_argument("--skip-dpi-change", action="store_true",
                         help="skip the 2400->3600 dpi position-shift test (W6)")
    p_warm.add_argument("--park", choices=("verbatim", "semantic"), default="verbatim",
                         help="PARK phase implementation (default verbatim; see "
                              "of135i scan --park's help / docs/replay-analysis.md)")

    p_cold = sub.add_parser(
        "cold",
        help="verify cold-start lamp warmup + AFE offset after a power cycle",
    )
    p_cold.add_argument("--out", required=True, metavar="DIR", help="output directory (required)")
    p_cold.add_argument("--frame", type=int, default=1, help="frame number to scan (default 1)")
    p_cold.add_argument("--eject", action="store_true", help="eject the magazine at the very end")
    p_cold.add_argument("--park", choices=("verbatim", "semantic"), default="verbatim",
                         help="PARK phase implementation (default verbatim; see "
                              "of135i scan --park's help / docs/replay-analysis.md)")

    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    if args.command == "warm" and args.repeat < 2:
        print("error: --repeat must be at least 2", file=sys.stderr)
        return 2

    # Nothing above touches USB. --out is created here, still before any
    # hardware access.
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "warm":
        return run_warm(args, out_dir)
    return run_cold(args, out_dir)


if __name__ == "__main__":
    sys.exit(main())
