"""Bulk-digitisation staging: directory layout, manifest, and resume.

The ``of135i digitize`` command (cli.py) drives one film strip per
invocation -- load, scan frames 1-4, eject -- into a staging tree, and
records each strip in an append-only manifest so a box of film can be
worked through strip by strip, resumably. This module is the pure,
hardware-free part: where files go, what the manifest holds, and which
roll comes next. It is unit-tested offline; cli.py adds the load/scan/
eject around it.

Layout::

    <out>/manifest.jsonl                  one JSON record per line, per roll
    <out>/<prefix>roll-<NNN>/f<K>.tiff     visible raw scan
    <out>/<prefix>roll-<NNN>/f<K>-ir.tiff  IR channel
    <out>/<prefix>roll-<NNN>/f<K>.diag.json

Raw (unclipped, un-inverted) TIFF is the archival product; colour
interpretation is the application's job. The manifest makes the run
auditable (per-frame gain/offset, dark_b substitution flag, status) and
lets a re-run skip rolls already done.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_NAME = "manifest.jsonl"


def manifest_path(out_dir: str) -> Path:
    return Path(out_dir) / MANIFEST_NAME


def read_manifest(out_dir: str) -> list[dict]:
    """Return the manifest records in order. Missing file -> []. Blank or
    malformed lines are skipped (the file is append-only; a torn last
    line from an interrupted write must not break resume)."""
    p = manifest_path(out_dir)
    if not p.exists():
        return []
    records: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def append_manifest(out_dir: str, record: dict) -> None:
    """Append one record as a JSON line. Creates the dir/file if needed."""
    p = manifest_path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def rolls_done(out_dir: str) -> set[int]:
    """Roll numbers already recorded with status 'ok'."""
    return {int(r["roll"]) for r in read_manifest(out_dir)
            if r.get("status") == "ok" and "roll" in r}


def next_roll(out_dir: str) -> int:
    """The next roll number to scan: one past the highest roll seen in the
    manifest (any status), or 1 for an empty manifest. Uses the highest
    seen -- not just 'ok' -- so a failed roll's number is not silently
    reused by the auto-increment; the operator can still target it
    explicitly with --roll."""
    rolls = [int(r["roll"]) for r in read_manifest(out_dir) if "roll" in r]
    return max(rolls) + 1 if rolls else 1


def roll_dirname(prefix: str, roll: int) -> str:
    return f"{prefix}roll-{roll:03d}"


def roll_dir(out_dir: str, prefix: str, roll: int) -> Path:
    return Path(out_dir) / roll_dirname(prefix, roll)


def frame_path(out_dir: str, prefix: str, roll: int, frame: int) -> Path:
    """Visible-scan path for one frame; the -ir.tiff and .diag.json
    sidecars sit beside it (written by the scan finishers)."""
    return roll_dir(out_dir, prefix, roll) / f"f{frame}.tiff"


def roll_is_done(out_dir: str, roll: int) -> bool:
    return roll in rolls_done(out_dir)
