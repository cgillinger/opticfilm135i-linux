#!/usr/bin/env python3
"""Offline tests for digitize albums (issue #65) -- no hardware.

A film is several strips; `digitize` keeps one roll directory per strip
with the holder's frame numbers. `--album NAME` adds one directory per
film with the photographs numbered through the strips, 01..36,
gap-free, rebuilt from the manifest after every strip and by
`of135i album`. Covered here:

  - the numbering: strips in order, an empty opening takes no number, a
    scanned frame without a product reserves its number and is listed
    as missing, and is delivered once the file exists (recrop);
  - the directory: hard links to the roll products, the diag sidecar
    beside each, only numbered files are ever removed, index.json;
  - a --force re-run of a strip renumbers what follows; rolls of other
    albums or without one are left out; a roll failed at the load adds
    nothing;
  - the name check, the CLI validation before hardware (--film 110
    refused), the `album` command and the per-strip summary line.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_album.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import digitize as dg

ALBUM = "Rolle01"


def _strip(d, roll, frames, album=ALBUM, prefix="", status="ok", forced=False):
    """Record one strip: `frames` is [(frame, "ok"|"empty"|"fail"|"crash")].
    "ok" writes fN.tiff and fN.diag.json; "empty" is an opening without
    film; "fail" a coverage failure (overscan only); "crash" an entry
    left mid-write (stage set, nothing known)."""
    rd = dg.roll_dir(d, prefix, roll)
    rd.mkdir(parents=True, exist_ok=True)
    pf = []
    for f, st in frames:
        e = {"frame": f, "main": None, "ir": None, "preview": None, "stage": None}
        if st == "ok":
            (rd / f"f{f}.tiff").write_bytes(f"roll{roll} frame{f}".encode())
            (rd / f"f{f}.diag.json").write_text(json.dumps({"frame": f}))
            e.update(main=str(rd / f"f{f}.tiff"), coverage_verified=True,
                     coverage_reason="ok", film_present=True)
        elif st == "empty":
            e.update(coverage_verified=True, coverage_reason="empty aperture",
                     film_present=False)
        elif st == "fail":
            e.update(coverage_verified=False,
                     coverage_reason="IR: leading margin below min_margin_mm (0.15 mm)")
        elif st == "crash":
            e.update(stage="write")
        pf.append(e)
    rec = {"roll": roll, "prefix": prefix, "dir": str(rd), "status": status,
           "per_frame": pf, "frames": [f for f, _ in frames]}
    if album is not None:
        rec["album"] = album
    if forced:
        rec["forced"] = True
    dg.append_manifest(d, rec)
    return rd


def _numbered(d, album=ALBUM):
    return sorted(p.name for p in dg.album_dir(d, album).iterdir()
                  if dg._ALBUM_NUMBERED.match(p.name))


def test_name_check():
    for ok in ("Rolle01", "box-A_2026", "film 3"):
        assert dg.check_album_name(ok) == ok
    for bad in ("", "  ", ".", "..", "a/b", "a\\b", dg.MANIFEST_NAME, "roll-001", "x-roll-12"):
        try:
            dg.check_album_name(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad!r}")
    print("test_name_check OK")


def test_numbering_through_strips_short_strip_and_reserved_number():
    with tempfile.TemporaryDirectory() as d:
        _strip(d, 1, [(f, "ok") for f in range(1, 7)])
        _strip(d, 2, [(1, "ok"), (2, "ok"), (3, "ok"), (4, "ok"), (5, "ok"), (6, "empty")])
        _strip(d, 3, [(1, "ok"), (2, "ok"), (3, "ok"), (4, "fail"), (5, "ok"), (6, "ok")],
               status="failed")
        ix = dg.album_sync(d, ALBUM)
        assert [s["images"] for s in ix["strips"]] == [
            [1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11], [12, 13, 14, 15, 16, 17]], ix["strips"]
        assert ix["delivered"] == 16 and ix["missing"] == [15], (ix["delivered"], ix["missing"])
        miss = next(i for i in ix["images"] if i["number"] == 15)
        assert miss["status"] == "missing" and miss["roll"] == 3 and miss["frame"] == 4
        assert "leading margin" in miss["reason"]
        names = _numbered(d)
        assert "15.tiff" not in names and "16.tiff" in names and "01.diag.json" in names
        assert len([n for n in names if n.endswith(".tiff")]) == 16
        # hard links to the roll products, with the roll's own bytes
        a = dg.album_dir(d, ALBUM)
        assert os.path.samefile(a / "12.tiff", dg.roll_dir(d, "", 3) / "f1.tiff")
        assert (a / "11.tiff").read_bytes() == b"roll2 frame5"
        assert json.loads((a / "07.diag.json").read_text()) == {"frame": 1}
        # index.json on disk equals what sync returned (minus nothing)
        on_disk = json.loads((a / dg.ALBUM_INDEX_NAME).read_text())
        assert on_disk["missing"] == [15] and on_disk["album"] == ALBUM
        # recrop delivers roll 3 frame 4: the reserved number is filled
        (dg.roll_dir(d, "", 3) / "f4.tiff").write_bytes(b"roll3 frame4 recropped")
        ix = dg.album_sync(d, ALBUM)
        assert ix["missing"] == [] and ix["delivered"] == 17
        assert (a / "15.tiff").read_bytes() == b"roll3 frame4 recropped"
    print("test_numbering_through_strips_short_strip_and_reserved_number OK")


def test_force_rerun_renumbers_what_follows_and_removes_stale_links():
    with tempfile.TemporaryDirectory() as d:
        _strip(d, 1, [(f, "ok") for f in range(1, 7)])
        _strip(d, 2, [(1, "ok"), (2, "ok"), (3, "ok"), (4, "ok"), (5, "ok"), (6, "empty")])
        _strip(d, 3, [(1, "ok"), (2, "ok"), (3, "ok")])
        ix = dg.album_sync(d, ALBUM)
        assert [s["images"] for s in ix["strips"]] == [[1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11], [12, 13, 14]]
        a = dg.album_dir(d, ALBUM)
        (a / "notes.txt").write_text("keep")           # not a numbered file
        # strip 2 is re-run with --force and now has six images: 3 shifts by one
        rd2 = dg.roll_dir(d, "", 2)
        for f in range(1, 7):
            (rd2 / f"f{f}.tiff").write_bytes(f"roll2 again frame{f}".encode())
        _strip(d, 2, [(f, "ok") for f in range(1, 7)], forced=True)
        # (_strip rewrote the files; make the bytes distinguishable)
        for f in range(1, 7):
            (rd2 / f"f{f}.tiff").write_bytes(f"roll2 again frame{f}".encode())
        ix = dg.album_sync(d, ALBUM)
        assert [s["images"] for s in ix["strips"]] == [[1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11, 12], [13, 14, 15]]
        assert (a / "12.tiff").read_bytes() == b"roll2 again frame6"
        assert (a / "13.tiff").read_bytes() == b"roll3 frame1"
        assert len([n for n in _numbered(d) if n.endswith(".tiff")]) == 15
        assert (a / "notes.txt").read_text() == "keep"
    print("test_force_rerun_renumbers_what_follows_and_removes_stale_links OK")


def test_membership_and_rolls_without_frames():
    with tempfile.TemporaryDirectory() as d:
        _strip(d, 1, [(1, "ok"), (2, "ok")])
        _strip(d, 2, [(1, "ok")], album=None)              # no album
        _strip(d, 3, [(1, "ok")], album="other")
        dg.append_manifest(d, {"roll": 4, "prefix": "", "status": "failed", "stage": "load",
                               "album": ALBUM})          # failed at the load: no frames
        _strip(d, 5, [(1, "crash")], status="failed")   # left mid-write: reserved
        _strip(d, 6, [(1, "ok")], prefix="boxB-")        # another prefix, same album
        ix = dg.album_sync(d, ALBUM)
        rolls = [(s["prefix"], s["roll"]) for s in ix["strips"]]
        assert rolls == [("", 1), ("", 4), ("", 5), ("boxB-", 6)], rolls
        assert [s["images"] for s in ix["strips"]] == [[1, 2], [], [3], [4]]
        assert ix["missing"] == [3]
        assert dg.album_sync(d, "other")["delivered"] == 1
        assert not (dg.album_dir(d, "other") / "02.tiff").exists()
    print("test_membership_and_rolls_without_frames OK")


def test_cli_validation_command_and_summary():
    import argparse
    from of135i import cli
    parser = cli.build_parser()
    a = parser.parse_args(["digitize", "-o", "d", "--album", "Rolle01"])
    assert a.album == "Rolle01"
    assert parser.parse_args(["digitize", "-o", "d"]).album is None
    assert cli._validate_album_args(argparse.Namespace(album=None, film="110")) is None
    assert cli._validate_album_args(argparse.Namespace(album="x", film="135")) is None
    msg = cli._validate_album_args(argparse.Namespace(album="x", film="110"))
    assert msg and "110" in msg, msg
    msg = cli._validate_album_args(argparse.Namespace(album="a/b", film="135"))
    assert msg and "--album" in msg, msg
    # digitize refuses before any hardware or directory work
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli._cmd_digitize(parser.parse_args(
            ["digitize", "-o", "/nonexistent-dir", "--album", "a/b"]))
    assert rc == 2 and "--album" in err.getvalue(), (rc, err.getvalue())
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli._cmd_digitize(parser.parse_args(
            ["digitize", "-o", "/nonexistent-dir", "--film", "110", "--album", "x"]))
    assert rc == 2 and "110" in err.getvalue(), (rc, err.getvalue())
    with tempfile.TemporaryDirectory() as d:
        _strip(d, 1, [(1, "ok"), (2, "ok"), (3, "fail")], status="failed")
        _strip(d, 2, [(1, "ok")])
        # the per-strip summary after a roll's record
        out = io.StringIO()
        with redirect_stdout(out):
            cli._album_sync_after(argparse.Namespace(out=d, prefix="", album=ALBUM), 1)
        line = out.getvalue()
        assert "this strip is 01-03" in line and "reserved, not delivered: 03" in line, line
        assert "3 images" in line, line
        # the album command
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["album", "-o", d, ALBUM])
        assert rc == 0, (rc, err.getvalue())
        text = out.getvalue()
        assert "roll-001: 01-03 (failed)" in text and "roll-002: 04 (ok)" in text, text
        assert "reserved, not delivered: 03" in text, text
        assert (dg.album_dir(d, ALBUM) / "04.tiff").exists()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            assert cli.main(["album", "-o", d, "roll-007"]) == 2
            assert cli.main(["album", "-o", os.path.join(d, "nope"), ALBUM]) == 2
        # a sync failure is a warning, never an error of the run
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            cli._album_sync_after(argparse.Namespace(out=d, prefix="", album="a/b"), 1)
        assert "warning: album" in err.getvalue(), err.getvalue()
    print("test_cli_validation_command_and_summary OK")


def main():
    tests = [
        test_name_check,
        test_numbering_through_strips_short_strip_and_reserved_number,
        test_force_rerun_renumbers_what_follows_and_removes_stale_links,
        test_membership_and_rolls_without_frames,
        test_cli_validation_command_and_summary,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
