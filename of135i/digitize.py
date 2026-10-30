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

A 110 roll (``--film 110``, docs/film-110.md §11) takes as many runs
(passes) into the same directory as it needs: ``p<k>-f<K>.tiff`` (+
sidecars) per pass ``k`` and one ``image<N>.tiff`` / ``image<N>-ir.tiff``
per photograph.

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

from . import holder

MANIFEST_NAME = "manifest.jsonl"

# Every frame position the holder has (of135i/holder.py). This is the
# cleaner's sweep, not the scan list: `digitize --frames` decides what a
# run scans, and the cleaner has to remove whatever an *earlier* run
# left behind, which may be a longer strip than this one.
FRAMES = tuple(holder.frames())


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


#: Whole-strip sweep files (docs/whole-strip-sweep-design.md section 6).
SWEEP_RAW_NAME = "sweep.raw"
SWEEP_JSON_NAME = "sweep.json"


def clear_roll_outputs(out_dir: str, prefix: str, roll: int, *,
                       include_sweep: bool = True) -> list[str]:
    """Delete this roll's per-frame output files -- for each frame exactly
    ``fN.tiff``, ``fN-ir.tiff``, ``fN-preview.tiff`` and ``fN.diag.json`` --
    so a --force re-scan cannot leave a previous run's sidecars mixed in
    beside the new ones. Returns the names removed. The manifest is
    untouched (append-only; the re-run appends a fresh record).

    The names are built and matched EXACTLY, never globbed: an ``f*.tiff``
    pattern also takes an operator's own ``family.tiff`` out of the
    directory, and this function must only ever remove files the driver
    itself wrote.

    A whole-strip sweep's ``sweep.raw`` / ``sweep.json`` (exact names) are
    removed too, unless ``include_sweep=False`` -- the ``--from-sweep``
    rebuild clears the frame products but must never touch the sweep it
    rebuilds from."""
    d = roll_dir(out_dir, prefix, roll)
    if not d.is_dir():
        return []
    removed: list[str] = []
    names = []
    for frame in FRAMES:
        names += [f"f{frame}.tiff", f"f{frame}-ir.tiff",
                  f"f{frame}-preview.tiff", f"f{frame}.diag.json"]
    if include_sweep:
        names += [SWEEP_RAW_NAME, SWEEP_JSON_NAME]
    for name in names:
        f = d / name
        if f.is_file():
            f.unlink()
            removed.append(name)
    return removed


def roll_dirname(prefix: str, roll: int) -> str:
    return f"{prefix}roll-{roll:03d}"


def roll_dir(out_dir: str, prefix: str, roll: int) -> Path:
    return Path(out_dir) / roll_dirname(prefix, roll)


def frame_path(out_dir: str, prefix: str, roll: int, frame: int) -> Path:
    """Visible-scan path for one frame; the -ir.tiff and .diag.json
    sidecars sit beside it (written by the scan finishers)."""
    return roll_dir(out_dir, prefix, roll) / f"f{frame}.tiff"


def roll_records(out_dir: str, roll: int, prefix: str = "") -> list[dict]:
    """The manifest records for one roll of one prefix, in order."""
    return [r for r in read_manifest(out_dir)
            if r.get("roll") == roll and r.get("prefix", "") == prefix]


def roll_records_current(out_dir: str, roll: int, prefix: str = "") -> list[dict]:
    """The roll's records since the last `--force` redo: a record written by
    a forced run carries `forced: true` and starts a new epoch (the manifest
    is append-only, so earlier records stay but no longer describe what is
    on disk). A roll never forced yields all its records."""
    recs = roll_records(out_dir, roll, prefix)
    for i in range(len(recs) - 1, -1, -1):
        if recs[i].get("forced"):
            return recs[i:]
    return recs


def roll_is_done(out_dir: str, roll: int, prefix: str = "") -> bool:
    """True when the roll needs no further scan.

    135: any 'ok' record. 110 (a roll whose last record has film "110"):
    done when some 'ok' record numbered at least one image AND the latest
    'ok' record's advice (the roll's state after that run) lists no
    `missing` number (seen only split, never delivered) and no `free_end`
    number (delivered with the strip end free, docs/film-110.md §4). A
    roll without a single image is never done: the strip was not read
    (Test 103: slot 2's strip with its holes against the rail). The
    driver cannot know how many images a strip has; "done" is about what
    it has seen, and the operator may stop earlier. A record from before
    the pass-based advice has only `images`, which is read the same way."""
    recs = roll_records(out_dir, roll, prefix)
    if recs and recs[-1].get("film") == "110":
        recs = roll_records_current(out_dir, roll, prefix)
        oks = [r for r in recs
               if r.get("film") == "110" and r.get("status") == "ok"]
        if not any(r.get("images") for r in oks):
            return False
        adv = oks[-1].get("advice") or {}
        if "done" in adv:
            # The run computed it with the strip's ends in view (cli
            # _film110_fill_record): nothing missing, nothing free-ended,
            # and no room for an unseen image.
            return bool(adv["done"])
        missing = adv["missing"] if "missing" in adv else adv.get("images", [])
        return not missing and not adv.get("free_end")
    return roll in rolls_done(out_dir, prefix)


# ------------------------------------------------------------ 110 rolls
#
# A 110 roll takes as many `digitize --film 110` runs ("passes") as it needs,
# all into the SAME roll directory, until nothing is missing and nothing
# was delivered with the strip end free (docs/film-110.md §11). Frame
# files carry the pass as a prefix so the runs cannot collide: p1-f1.tiff,
# p2-f1.tiff, ... (the placement and the strip's orientation are in the
# manifest record). The photographs are image<N>.tiff / image<N>-ir.tiff,
# N = position on the strip; the tags a/b/x of earlier versions are still
# swept by --force.

FILM110_MAX_PASSES = 20
FILM110_FRAME_TAGS = tuple(f"p{k}" for k in range(1, FILM110_MAX_PASSES + 1)) \
    + ("a", "b", "x")
# Highest photograph number the clean-up sweeps: 6 apertures x 37.947 mm
# hold at most 9 images of 25.5 mm; 20 leaves room without scanning the
# disk for arbitrary names.
FILM110_MAX_IMAGE = 20
_FILM110_FRAME_SUFFIXES = (".tiff", "-ir.tiff", ".overscan.tiff",
                           "-ir.overscan.tiff", ".diag.json", ".cal.npz",
                           "-preview.tiff")


def frame_path_110(out_dir: str, prefix: str, roll: int, tag: str,
                   frame: int) -> Path:
    """Visible-scan path of one 110 frame: <roll>/<tag>-f<N>.tiff (tag
    "p1", "p2", ...); its sidecars sit beside it."""
    return roll_dir(out_dir, prefix, roll) / f"{tag}-f{frame}.tiff"


def film110_image_path(out_dir: str, prefix: str, roll: int, n: int,
                       ir: bool = False, uncertain: bool = False) -> Path:
    """Photograph path: <roll>/image<N>.tiff (or image<N>-ir.tiff); a copy
    whose number the driver could not pin to the roll's grid is
    image<N>-uncertain.tiff (and -uncertain-ir.tiff). N may be 0 or
    negative: an image found before the roll's first-seen one."""
    tag = "-uncertain" if uncertain else ""
    return roll_dir(out_dir, prefix, roll) / (
        f"image{n}{tag}-ir.tiff" if ir else f"image{n}{tag}.tiff")


def film110_placements_on_disk(rdir: Path) -> set[str]:
    """Which frame tags ("p1", "p2", ...; legacy "a", "b", "x") have at
    least one file in `rdir`."""
    found: set[str] = set()
    if not Path(rdir).is_dir():
        return found
    for tag in FILM110_FRAME_TAGS:
        for frame in FRAMES:
            if any((Path(rdir) / f"{tag}-f{frame}{suf}").exists()
                   for suf in _FILM110_FRAME_SUFFIXES):
                found.add(tag)
                break
    return found


def film110_next_pass(out_dir: str, prefix: str, roll: int) -> int:
    """Index of the next pass on this roll: one past the roll's 110 records
    since the last forced redo, and one past the highest p<k> frame tag on
    disk (a crash between scanning and recording still owns its pass)."""
    # A failed rebuild leaves one record that owns no pass.
    recs = [r for r in roll_records_current(out_dir, roll, prefix)
            if r.get("film") == "110"
            and not (r.get("source") == "frames" and r.get("status") == "failed")]
    nxt = len(recs) + 1
    for tag in film110_placements_on_disk(roll_dir(out_dir, prefix, roll)):
        if tag[0] == "p" and tag[1:].isdigit():
            nxt = max(nxt, int(tag[1:]) + 1)
    return nxt


def clear_film110_outputs(out_dir: str, prefix: str, roll: int) -> list[str]:
    """--force on a 110 roll: remove the files this driver wrote for
    BOTH placements (and the provisional tag) and the photograph products.
    Exact names only, never a glob (see clear_roll_outputs). The
    manifest is untouched."""
    d = roll_dir(out_dir, prefix, roll)
    if not d.is_dir():
        return []
    names = [f"{tag}-f{frame}{suf}" for tag in FILM110_FRAME_TAGS
             for frame in FRAMES for suf in _FILM110_FRAME_SUFFIXES]
    for n in range(1, FILM110_MAX_IMAGE + 1):
        names += [f"image{n}.tiff", f"image{n}-ir.tiff"]
    # unnumbered copies of a pass whose numbering could not be established
    names += [f"{tag}-a{ap}-{i}{suf}" for tag in FILM110_FRAME_TAGS
              for ap in FRAMES for i in range(1, 4) for suf in (".tiff", "-ir.tiff")]
    removed: list[str] = []
    for name in names:
        f = d / name
        if f.is_file():
            f.unlink()
            removed.append(name)
    return removed


def delivered_image(records: list[dict], roll: int, prefix: str,
                    n: int) -> dict | None:
    """The latest manifest `images` entry that delivered photograph `n`
    (a main file was written) for this roll, or None. Entries of a record
    list are searched oldest to newest; records of other rolls/prefixes
    are ignored (a record with no roll field -- the live, not yet
    recorded run -- is accepted)."""
    found = None
    for r in records:
        if "roll" in r and (r.get("roll") != roll or r.get("prefix", "") != prefix):
            continue
        for e in r.get("images") or []:
            if e.get("number") == n and e.get("main") and not e.get("uncertain"):
                found = e
    return found
