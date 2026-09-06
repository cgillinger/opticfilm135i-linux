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

Auto-numbering and the overwrite guard consult BOTH the manifest and the
directories on disk: a scan writes its frames before it appends a manifest
record, so a run that crashed in between still owns its roll number and its
files are protected. Each ``prefix`` is an independent roll sequence within
one ``--out``.
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
    """Append one record as a JSON line. Creates the dir/file if needed.

    If the file's last line was torn (an interrupted write with no trailing
    newline), a newline is written first so the new record lands on its own
    clean line -- otherwise it would merge with the broken tail, making
    BOTH lines unreadable and risking a reused roll number."""
    p = manifest_path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > 0:
        with open(p, "rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                with open(p, "a") as fa:
                    fa.write("\n")
    with open(p, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def rolls_done(out_dir: str, prefix: str = "") -> set[int]:
    """Roll numbers already recorded with status 'ok' for this ``prefix``.
    Each prefix is its own roll sequence inside a shared --out, so the
    manifest is filtered on it (a record without a prefix field counts as
    prefix "")."""
    return {int(r["roll"]) for r in read_manifest(out_dir)
            if r.get("status") == "ok" and "roll" in r
            and r.get("prefix", "") == prefix}


def existing_roll_numbers(out_dir: str, prefix: str = "") -> set[int]:
    """Roll numbers that already have an output directory on disk (matching
    ``<prefix>roll-NNN``), regardless of the manifest. A scan can write
    frames before its manifest record is appended (or crash in between), so
    disk state must be consulted for auto-numbering, not just the manifest."""
    d = Path(out_dir)
    if not d.is_dir():
        return set()
    out: set[int] = set()
    want = f"{prefix}roll-"
    for child in d.iterdir():
        if child.is_dir() and child.name.startswith(want):
            tail = child.name[len(want):]
            if tail.isdigit():
                out.add(int(tail))
    return out


def next_roll(out_dir: str, prefix: str = "") -> int:
    """The next roll number to scan for this ``prefix``: one past the highest
    roll seen in EITHER the manifest (any status, this prefix) or an existing
    ``<prefix>roll-NNN`` directory, or 1 if none. Consulting both means a scan
    that wrote frames but never recorded a manifest entry (a crash between the
    two) does not get its number reused and its files overwritten. A failed
    roll's number is not silently reused either; the operator can still target
    one with --roll. Filtering on prefix keeps two prefixes in one --out as
    independent sequences."""
    rolls = {int(r["roll"]) for r in read_manifest(out_dir)
             if "roll" in r and r.get("prefix", "") == prefix}
    rolls |= existing_roll_numbers(out_dir, prefix)
    return max(rolls) + 1 if rolls else 1


def roll_dir_has_output(out_dir: str, prefix: str, roll: int) -> bool:
    """True if this roll's directory already exists and is non-empty -- the
    check that guards against overwriting existing results. Any content
    counts (not just ``*.tiff``): a partial or crashed run may have left a
    ``.diag.json`` or a half-written file, and clobbering that is exactly
    what the guard exists to prevent."""
    d = roll_dir(out_dir, prefix, roll)
    return d.is_dir() and any(d.iterdir())


def clear_roll_outputs(out_dir: str, prefix: str, roll: int) -> list[str]:
    """Delete this roll's per-frame output files (``f*.tiff`` -- visible, IR
    and preview -- and ``f*.diag.json``) so a --force re-scan cannot leave a
    previous run's sidecars mixed in beside the new ones. Returns the names
    removed. The manifest is untouched (append-only; the re-run appends a
    fresh record). Only frame outputs are removed, not unrelated files an
    operator may have put in the dir."""
    d = roll_dir(out_dir, prefix, roll)
    if not d.is_dir():
        return []
    removed: list[str] = []
    for pat in ("f*.tiff", "f*.diag.json"):
        for f in sorted(d.glob(pat)):
            if f.is_file():
                f.unlink()
                removed.append(f.name)
    return removed


def roll_dirname(prefix: str, roll: int) -> str:
    return f"{prefix}roll-{roll:03d}"


def roll_dir(out_dir: str, prefix: str, roll: int) -> Path:
    return Path(out_dir) / roll_dirname(prefix, roll)


def frame_path(out_dir: str, prefix: str, roll: int, frame: int) -> Path:
    """Visible-scan path for one frame; the -ir.tiff and .diag.json
    sidecars sit beside it (written by the scan finishers)."""
    return roll_dir(out_dir, prefix, roll) / f"f{frame}.tiff"


def roll_is_done(out_dir: str, roll: int, prefix: str = "") -> bool:
    return roll in rolls_done(out_dir, prefix)
