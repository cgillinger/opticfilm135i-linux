#!/usr/bin/env python3
"""Offline tests for the GL126 magazine options and state machine (WP-4).

docs/sane-wp4-magazine.md. The backend now loads and ejects the film
magazine itself -- Christian's condition for an upstream submission -- in
two steps, because the vendor's insert flow needs the operator to take
the magazine out and reseat it to the mechanical stop in the MIDDLE of
the sequence and SANE has no way for a backend to ask for that during
sane_start. "Load film" releases, the next scan loads.

What the other suites already cover: tests/test_sane_ops.py proves every
one of the five programs (cold_init, open, jog, load, eject) puts exactly
the Python driver's transfers on the wire, in the driver's order, and
tests/test_sane_lock.py proves the on-disk "a release is pending" mark.
What is left, and what this file covers, is the part that only exists
inside the built backend:

  1. the three options exist, with the right types, and are inactive on
     every other genesys chip;
  2. the state machine's refusals happen BEFORE anything reaches the
     wire, and say what to do instead;
  3. a scan with no release pending does not touch the magazine at all;
  4. a stale or foreign mark cannot reach the load feed;
  5. SET on the status line (settable since 2026-09-27, so KSane renders
     it enabled -- Test 91) is a documented no-op: it never moves the
     state machine or the mark, and an unlisted value is rejected by
     SANE core before the handler ever runs.

It drives the REAL public flow (sane_open -> sane_control_option ->
sane_start) against the BUILT backend in genesys's test mode, through
tests/gl126_magazine_probe.cpp. In that mode every control IN reads as
zeroes, so a program that does reach the wire stops at its first
unacknowledged register write (or, for the cold start, at its first
fail-closed motor completion) -- which is exactly the observation for
the paths that are SUPPOSED to reach it.

Needs g++, the sane-backends checkout (SANE_BACKENDS_DIR, else a sibling
`sane-backends/`), and a built backend/.libs/libsane-genesys.so. Skips
cleanly (not fails) when any is absent, like tests/test_sane_open_params.py.

Run with:
    .venv/bin/python tests/test_sane_magazine.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE_SRC = REPO / "tests" / "gl126_magazine_probe.cpp"

_probe_bin = None
_build_attempted = False
_skip_reason = None

GL124_DEVICE = "04a9:1909"   # a GL124 model in the backend's USB tables
GL126_DEVICE = "07b3:1436"   # the default; named here for the mode-switch test

# sane.h's SANE_Status enum, in order.
SANE_STATUS_GOOD = 0
SANE_STATUS_UNSUPPORTED = 1
SANE_STATUS_CANCELLED = 2
SANE_STATUS_DEVICE_BUSY = 3
SANE_STATUS_INVAL = 4
SANE_STATUS_EOF = 5
SANE_STATUS_JAMMED = 6
SANE_STATUS_NO_DOCS = 7
SANE_STATUS_COVER_OPEN = 8
SANE_STATUS_IO_ERROR = 9

SANE_TYPE_INT = 1
SANE_TYPE_STRING = 3
SANE_TYPE_BUTTON = 4
SANE_TYPE_GROUP = 5

# sane.h's *info bits.
SANE_INFO_RELOAD_OPTIONS = 1 << 1


def _sane_backends_dir():
    env = os.environ.get("SANE_BACKENDS_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(REPO.parent / "sane-backends")
    for c in candidates:
        if (c / "backend" / "genesys" / "low.h").exists():
            return c
    return None


def _built_so(sb):
    for name in ("libsane-genesys.so", "libsane-genesys.so.1.4.0"):
        p = sb / "backend" / ".libs" / name
        if p.exists():
            return p
    return None


def _build_probe():
    global _probe_bin, _build_attempted, _skip_reason
    if _build_attempted:
        return _probe_bin
    _build_attempted = True

    gxx = shutil.which("g++")
    if gxx is None:
        _skip_reason = "g++ not on PATH"
        return None
    sb = _sane_backends_dir()
    if sb is None:
        _skip_reason = ("sane-backends checkout not found "
                        "(set SANE_BACKENDS_DIR or clone as a sibling)")
        return None
    so = _built_so(sb)
    if so is None:
        _skip_reason = f"backend not built ({sb}/backend/.libs/libsane-genesys.so missing)"
        return None

    g = sb / "backend" / "genesys"
    libs = sb / "backend" / ".libs"
    binary = str(Path(tempfile.mkdtemp(prefix="gl126-magazine-")) / "magprobe")
    cmd = [gxx, "-std=gnu++11", "-Wall", "-Wextra",
           "-DHAVE_CONFIG_H", "-DBACKEND_NAME=genesys",
           "-I", str(sb), "-I", str(g), "-I", str(sb / "backend"),
           "-I", str(sb / "include"), "-I", str(sb / "include" / "sane"),
           str(PROBE_SRC),
           "-L", str(libs), "-lsane-genesys", f"-Wl,-rpath,{libs}",
           "-o", binary]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("failed to build the gl126 magazine probe:\n"
                             f"{' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    _probe_bin = binary
    return _probe_bin


def _skip(name):
    print(f"{name} SKIPPED ({_skip_reason})")
    return "skipped"


def _run(probe, *args, lock_dir=None, poll_cap_ms=5):
    """Run the probe with an isolated HOME (so no real calibration file is
    read) and an isolated lock/mark path (so a test can never disturb the
    real one, nor a real session a test).

    `poll_cap_ms` defaults to 5: the mock never answers a poll, so every
    best-effort site would otherwise burn its real budget -- the cold-start
    program alone carries the driver's 1.5 s and 30 s waits at nineteen poll
    sites (ten best-effort, nine fail-closed motor completions). The cap
    only ever shortens a wait (sane/gl126_ops.h RunPolicy, and the WP-5
    magazine edge wait's own equivalent gate in gl126.cpp). A handful of
    edge-wait tests need the wait to actually run its scripted sequence to
    completion (100 ms/poll, up to ~8 polls) and pass a larger value."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, HOME=tmp)
        env.pop("SANE_DEBUG_GENESYS", None)
        env["OF135I_LOCK_FILE"] = str(Path(lock_dir or tmp) / "of135i.lock")
        env["OF135I_SANE_POLL_CAP_MS"] = str(poll_cap_ms)
        r = subprocess.run([probe, *args], capture_output=True, text=True,
                           env=env, timeout=180)
    assert r.returncode == 0, f"probe exit {r.returncode}: {r.stdout}\n{r.stderr}"
    out = {"statuses": [], "optstatuses": [], "options": {}, "text": None,
           "mark": None, "progress": None, "key": None, "start": None}
    for line in r.stdout.splitlines():
        if line.startswith("OPT "):
            parts = line.split()
            fields = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
            out["options"][parts[1]] = fields if len(parts) > 2 else "MISSING"
        elif line.startswith("STATUS "):
            rest = line[len("STATUS "):]
            code, _, msg = rest.partition(" ")
            out["statuses"].append((int(code), msg.strip()))
        elif line.startswith("VALUE "):
            out.setdefault("values", []).append(line[len("VALUE "):].strip())
        elif line.startswith("OPTSTATUS "):
            out["optstatuses"].append(int(line.split()[1]))
        elif line.startswith("OPTINFO "):
            out.setdefault("optinfos", []).append(int(line.split()[1]))
        elif line.startswith("STARTSTATUS "):
            rest = line[len("STARTSTATUS "):]
            code, _, msg = rest.partition(" ")
            out["start"] = (int(code), msg.strip())
        elif line.startswith("PROGRESS "):
            out["progress"] = line[len("PROGRESS "):].strip()
        elif line.startswith("TEXT "):
            out["text"] = line[len("TEXT "):].strip()
        elif line.startswith("KEY "):
            out["key"] = line[len("KEY "):].strip()
        elif line.startswith("MARK "):
            out["mark"] = line[len("MARK "):].strip()
        elif line.startswith("ITEM "):
            body, _, title = line[len("ITEM "):].partition(" TITLE ")
            parts = body.split()
            fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            fields["title"] = title
            out.setdefault("items", []).append((int(parts[0]), fields))
        elif line.startswith("DEFAULT_MODE "):
            out["default_mode"] = line[len("DEFAULT_MODE "):].strip()
        elif line.startswith("DEFAULT_COLOR_FILTER "):
            out["default_color_filter"] = line[len("DEFAULT_COLOR_FILTER "):].strip()
        elif line.startswith("RESVALUE "):
            out.setdefault("resolution_values", []).append(int(line[len("RESVALUE "):].strip()))
        elif line.startswith("MAGVALUE "):
            out.setdefault("magazine_values", []).append(line[len("MAGVALUE "):].strip())
        elif line.startswith("EDGEWRITE "):
            out["edgewrite"] = int(line[len("EDGEWRITE "):].strip())
    return out


# ------------------------------------------------------------ 1. options


def test_magazine_options_exist_only_for_gl126():
    """The three buttons and the status line are declared for the 135i
    and inactive on every other genesys chip -- no other model in this
    backend has a magazine to load."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_magazine_options_exist_only_for_gl126")

    r = _run(probe, "options")
    opts = r["options"]
    for name in ("load-film", "eject-film", "check-status", "magazine"):
        assert name in opts and opts[name] != "MISSING", (name, opts)
        assert opts[name]["inactive"] == "0", (name, opts[name])
    assert int(opts["load-film"]["type"]) == SANE_TYPE_BUTTON, opts["load-film"]
    assert int(opts["eject-film"]["type"]) == SANE_TYPE_BUTTON, opts["eject-film"]
    assert int(opts["check-status"]["type"]) == SANE_TYPE_BUTTON, opts["check-status"]
    assert int(opts["magazine"]["type"]) == SANE_TYPE_STRING, opts["magazine"]
    # 2026-09-27: settable now (SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT,
    # 0x04 | 0x01 = 0x05), not read-only -- KSaneWidgets renders a
    # SOFT_DETECT-only option disabled/greyed (Test 91), so the option is
    # made settable purely to make the status line legible; the SET
    # handler is a documented no-op (genesys.cpp, case OPT_MAGAZINE in
    # set_option_value()).
    cap = int(opts["magazine"]["cap"], 16)
    assert cap & 0x04, opts["magazine"]
    assert cap & 0x01, opts["magazine"]
    assert int(opts["magazine"]["size"]) >= 128, opts["magazine"]

    other = _run(probe, "options", GL124_DEVICE)
    for name in ("load-film", "eject-film", "check-status", "magazine"):
        assert other["options"][name]["inactive"] == "1", (name, other["options"][name])

    print("test_magazine_options_exist_only_for_gl126 OK "
          "(4 options active on GL126, all inactive on GL124)")


def test_the_status_line_is_readable_and_comes_first():
    """Test 76 found the status line unusable in digiKam: the value was a
    full sentence, KSane draws an unconstrained string option as an
    editable combo scrolled to the END of its content, and the option sat
    BELOW the two buttons it describes. So the operator saw the tail of
    some advice, after acting, with Add/Remove buttons beside it.

    Three properties fix that and are pinned here: every value is short
    enough to fit and leads with the state word, the option is
    constrained to its own values so the widget is a plain combo, and its
    index places it ahead of the buttons — option order is display
    order."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_the_status_line_is_readable_and_comes_first")

    r = _run(probe, "options")
    opts = r["options"]

    # Ahead of both buttons.
    assert int(opts["magazine"]["index"]) < int(opts["load-film"]["index"]), opts
    assert int(opts["magazine"]["index"]) < int(opts["eject-film"]["index"]), opts

    # Constrained to a string list (SANE_CONSTRAINT_STRING_LIST == 3).
    assert int(opts["magazine"]["constraint"]) == 3, opts["magazine"]

    values = r.get("values") or []
    # WP-5: twelve now -- the seven from the ordinary state machine plus
    # three cross-process/retry variants and four Check-status-only
    # diagnostic snapshots (docs/sane-wp5-load-button.md section 3.6);
    # the exact list is pinned in test_magazine_state_values_are_pinned.
    assert len(values) == 12, values
    for v in values:
        assert len(v) <= 40, (len(v), v)          # fits the widget
        assert v[0].islower(), v                   # state word leads
    # The states an operator must be able to tell apart, each present.
    # ("unknown" is the internal MagazineState name; its text says "not
    # loaded" instead, 2026-09-27 task 2.)
    joined = " | ".join(values)
    for word in ("not loaded", "released", "loaded", "ejected", "failed"):
        assert word in joined, (word, values)
    print(f"test_the_status_line_is_readable_and_comes_first OK "
          f"({len(values)} values, longest {max(len(v) for v in values)} chars, "
          f"index {opts['magazine']['index']} before the buttons)")


def test_setting_the_status_line_is_a_no_op():
    """Task 1 (2026-09-27): OPT_MAGAZINE is settable (SANE_CAP_SOFT_SELECT
    added) purely so KSaneWidgets renders its label and value enabled
    (black) instead of the disabled grey it draws for a SOFT_DETECT-only
    option -- Test 91 found that rendering unreadable. But nothing the
    option reports can actually be commanded, so a SET must never move
    the magazine state machine or touch the on-disk mark, and a GET
    right after must still return the TRUE text, not whatever was set.

    Drives the real sane_control_option(SET) path, not the handler
    directly, so this also exercises SANE core's own constraint check
    (sanei_constrain_value, called from sane_control_option_impl before
    set_option_value() runs): a value outside the string-list constraint
    must be rejected by core and never reach the no-op handler at all."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_setting_the_status_line_is_a_no_op")

    r = _run(probe, "scenario", "magazine-set-accepts-a-listed-value-as-a-no-op")
    assert r["text"].startswith("ejected -- swap strip"), r["text"]
    assert r["optstatuses"] == [SANE_STATUS_GOOD], r["optstatuses"]
    assert r["optinfos"] == [SANE_INFO_RELOAD_OPTIONS], r["optinfos"]
    assert r["mark"].startswith("present ejected"), r["mark"]

    r2 = _run(probe, "scenario", "magazine-set-rejects-an-unlisted-value")
    assert r2["optstatuses"] == [SANE_STATUS_INVAL], r2["optstatuses"]
    # info stays 0: sanei_constrain_value's STRING_LIST case throws (via
    # TIE) before set_option_value() -- and its SANE_INFO_RELOAD_OPTIONS
    # bit -- ever runs.
    assert r2["optinfos"] == [0], r2["optinfos"]
    assert r2["text"].startswith("ejected -- swap strip"), r2["text"]
    assert r2["mark"].startswith("present ejected"), r2["mark"]
    print("test_setting_the_status_line_is_a_no_op OK "
          "(a listed value: GOOD+RELOAD_OPTIONS, state/mark unchanged; "
          "an unlisted value: INVAL from core, handler never ran)")


def test_the_status_line_reports_a_load_pending_from_another_process():
    """Test 77: digiKam was restarted between the release and the scan,
    and the status line said "unknown" while a load was genuinely
    pending — it read only this process's memory. The mark survives a
    process by design (that is what lets `scanimage` load in two
    invocations), so the line must consult it."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_the_status_line_reports_a_load_pending_from_another_process")

    r = _run(probe, "scenario", "state-mark-pending")
    assert r["text"].startswith("released earlier"), r["text"]
    assert r["mark"].startswith("present"), r["mark"]
    # And with no mark at all it still says "not loaded", not a false
    # pending.
    r2 = _run(probe, "scenario", "state-initial")
    assert r2["text"].startswith("not loaded"), r2["text"]
    print("test_the_status_line_reports_a_load_pending_from_another_process OK")


def test_a_scan_after_eject_refuses_no_matter_what_the_hardware_says():
    """WP-5 (docs/sane-wp5-load-button.md section 3.4): Scan never loads
    the magazine any more -- load_document() is a pure checker of the
    in-process state and the marks, and does not read the hardware at
    all. An Ejected magazine (in-process, from the "nothing to do" eject
    used throughout this suite) refuses NO_DOCS "press Load film first"
    regardless of the sensor and register seeds this scenario carries
    forward from its WP-4 name -- they used to matter to load_document()
    itself; now they only matter to the Load film button (see
    test_the_edge_resolving_lets_the_ejected_kind_load, below)."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_scan_after_eject_refuses_no_matter_what_the_hardware_says")

    r = _run(probe, "scenario", "load-after-eject")
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, load = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert load[0] == SANE_STATUS_NO_DOCS, load
    assert "press Load film first" in load[1], load[1]
    assert "Nothing was written" in load[1], load[1]
    # A refusal, not a failure: the mark survives, and so does the pending
    # load it represents.
    assert r["mark"].startswith("present ejected"), r["mark"]
    assert r["text"].startswith("ejected"), r["text"]
    print("test_a_scan_after_eject_refuses_no_matter_what_the_hardware_says OK "
          "(NO_DOCS, mark kept, nothing read)")


def test_the_edge_resolving_lets_the_ejected_kind_load():
    """The WP-4 register preconditions this used to live in load_document()
    (Test 90's regs 0x3b/0x3c finding included) moved to the "Load film"
    button itself (magazine_load_film_impl()), checked AFTER the edge wait
    resolves -- the wait's own "Seen" outcome already proves the sensor
    precondition WP-4 section 10.3 used a single read for. Scripted so the
    wait actually completes (present -> clear -> clear -> present x5,
    hence the larger poll cap): "open" then reaches the wire and fails
    closed on the mock, and the checkpoint proves not one of the wait's
    own polls wrote anything (EDGEWRITE 0)."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_the_edge_resolving_lets_the_ejected_kind_load")

    r = _run(probe, "scenario", "load-film-edge-seen-ejected", poll_cap_ms=2000)
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, release = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert release[0] == SANE_STATUS_IO_ERROR, release
    assert "magazine open sequence" in release[1], release[1]
    assert "register write not acknowledged" in release[1], release[1]
    assert "not in the state" not in release[1], release[1]
    assert r["edgewrite"] == 0, r
    assert r["mark"].startswith("present failed"), r["mark"]
    assert r["text"].startswith("failed"), r["text"]
    print("test_the_edge_resolving_lets_the_ejected_kind_load OK "
          "(0x02/0x00 accepted, edge resolved with no writes, open reached the wire)")


def test_the_regs_check_after_the_edge_still_refuses_the_base_table_state():
    """The one 0x3b/0x3c state the Ejected kind still refuses: 0xff/0xff,
    the base-table-only state eject itself refuses from (Test 44). Now
    checked by "Load film" right after the edge resolves, not by
    load_document() -- read-only INVAL, no "open" attempted, Failed, mark
    cleared."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_the_regs_check_after_the_edge_still_refuses_the_base_table_state")

    r = _run(probe, "scenario", "load-film-edge-seen-bad-regs", poll_cap_ms=2000)
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, release = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert release[0] == SANE_STATUS_INVAL, release
    assert "0xff/0xff" in release[1], release[1]
    assert "the eject leaves it in" in release[1], release[1]
    assert "Nothing further was written" in release[1], release[1]
    assert r["edgewrite"] == 0, r
    assert r["mark"].startswith("present failed"), r["mark"]
    assert r["text"].startswith("failed"), r["text"]
    print("test_the_regs_check_after_the_edge_still_refuses_the_base_table_state OK "
          "(refused read-only, after the edge resolved)")


def test_a_scan_after_eject_without_a_magazine_refuses_and_keeps_the_mark():
    """The strip has not been pushed back in yet (or was never taken out).
    load_document() no longer reads the sensor to say so (WP-5) -- it is
    the SAME read-only NO_DOCS refusal as every other Ejected/Released
    scan-gate case, and the mark is KEPT: pressing Load film (which now
    does its own wait) is the whole fix."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_scan_after_eject_without_a_magazine_refuses_and_keeps_the_mark")

    r = _run(probe, "scenario", "load-after-eject-no-magazine")
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, load = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert load[0] == SANE_STATUS_NO_DOCS, load
    assert "press Load film first" in load[1], load[1]
    assert "Nothing was written" in load[1], load[1]
    assert r["mark"].startswith("present"), r["mark"]
    assert r["text"].startswith("ejected"), r["text"]
    print("test_a_scan_after_eject_without_a_magazine_refuses_and_keeps_the_mark OK "
          "(NO_DOCS, mark kept)")


def test_a_cold_read_forces_the_full_release_path_even_from_ejected():
    """WP-5: the Ejected/Released shortcuts (no jog, straight to the edge
    wait) assume the transport is still homed and positioned from the
    SAME power-on as the eject that produced them -- exactly WP-4 section
    10.3's reasoning for refusing a cold next-strip load. So a cold reg
    0x01 at "Load film" time forces the FULL path (cold_init, then open,
    then jog) even from an in-process Ejected state, never the no-jog
    shortcut. Proven by the shape of the failure: the cold-start program's
    own fail-closed motor completion is reached (identical to
    test_release_from_cold_stops_at_the_first_motor_completion) -- an
    Ejected-kind press would have gone straight to the read-only edge
    wait instead, with no motor completion to fail on at all."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_cold_read_forces_the_full_release_path_even_from_ejected")

    r = _run(probe, "scenario", "load-film-cold-after-eject-forces-fresh-path")
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, release = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert release[0] == SANE_STATUS_DEVICE_BUSY, release
    assert "magazine cold_init sequence" in release[1], release[1]
    assert "a motor move did not complete" in release[1], release[1]
    assert r["mark"].startswith("present failed"), r["mark"]
    assert r["text"].startswith("failed"), r["text"]
    print("test_a_cold_read_forces_the_full_release_path_even_from_ejected OK "
          "(cold_init ran, not the no-jog shortcut)")


def test_an_ejected_mark_from_another_process_refuses_the_scan():
    """The cross-process case the mark was built for in the first place:
    `scanimage --eject-film=yes` in one process, a plain scan in the
    next. Two SEPARATE probe invocations sharing one OF135I_LOCK_FILE --
    the first writes an Ejected mark and exits (no in-process state
    survives that), the second knows nothing except what it reads from
    disk, and (WP-5) load_document() refuses NO_DOCS regardless -- the
    same "press Load film first" every Ejected/Released mark produces.

    This also proves the mark's device key round-trips a name WITH SPACES
    for the Ejected kind specifically (the backend's own test-mode device
    is named "test device:0x07b3:0x1436"): if the space truncated the key
    on either write or read, the mark would name a device that never
    matches and the refusal would not happen -- the scan would proceed
    with a warning instead. (The Released kind's equivalent round trip is
    covered by tests/test_sane_lock.py's test_magazine_mark_round_trip,
    unaffected by this change.)"""
    probe = _build_probe()
    if probe is None:
        return _skip("test_an_ejected_mark_from_another_process_refuses_the_scan")

    with tempfile.TemporaryDirectory() as lock_dir:
        r1 = _run(probe, "scenario", "state-mark-ejected-pending", lock_dir=lock_dir)
        assert r1["mark"].startswith("present ejected"), r1["mark"]
        assert " " in r1["key"], r1["key"]   # the test device's name has a space

        r2 = _run(probe, "scenario", "load-mark-ejected-crossproc", lock_dir=lock_dir)
    assert r2["key"] == r1["key"], (r1["key"], r2["key"])
    status, msg = r2["statuses"][0]
    assert status == SANE_STATUS_NO_DOCS, r2["statuses"]
    assert "press Load film first" in msg, msg
    assert r2["mark"].startswith("present ejected"), r2["mark"]
    print("test_an_ejected_mark_from_another_process_refuses_the_scan OK "
          "(two processes, one shared mark, key with spaces round-tripped)")


def test_an_ejected_mark_for_another_device_is_ignored_and_cleared():
    """Mirrors test_a_mark_for_another_device_is_ignored_and_cleared for
    the Ejected kind: a mark naming a different device (or this one under
    a pre-power-cycle address) is not ours and never will be -- ignored,
    removed, and the scan proceeds normally into calibration."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_an_ejected_mark_for_another_device_is_ignored_and_cleared")

    r = _run(probe, "scenario", "start-mark-ejected-other-device")
    assert r["progress"] == "offset_calibration", r
    assert r["mark"] == "absent", r["mark"]
    print("test_an_ejected_mark_for_another_device_is_ignored_and_cleared OK")


def test_load_film_from_ejected_waits_instead_of_jogging():
    """WP-5 section 3.2's Ejected row: pressed again after an eject, "Load
    film" no longer re-releases (WP-4's old rule) -- it WAITS for the edge
    first, no jog. The eject step here left the sensor reading clear, and
    nothing scripts a change, so the wait times out having SEEN a clear
    (debounced -- review finding I -- so this needs a poll_cap_ms large
    enough for 2 consecutive clear reads, not just one): Released
    (in-process state and status text), no motor write of any kind, GOOD
    (a timeout is not an error). Review finding G: the ON-DISK MARK stays
    "ejected", not "released" -- the origin (no jog needed, lenient regs
    rule) has to survive a retry in a FRESH process the same way
    magazine_wait_needs_open() survives one in this process
    (test_an_ejected_origin_retry_keeps_running_open)."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_load_film_from_ejected_waits_instead_of_jogging")

    r = _run(probe, "scenario", "release-after-eject", poll_cap_ms=300)
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, release = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert release == (SANE_STATUS_GOOD, ""), release
    assert r["edgewrite"] == 0, r
    assert r["mark"].startswith("present ejected"), r["mark"]
    assert r["text"].startswith("press Load film"), r["text"]
    print("test_load_film_from_ejected_waits_instead_of_jogging OK "
          "(no jog, timed out, Released in-process, mark stays 'ejected')")


def test_magazine_text_starts_unknown():
    """Before anything is driven, the backend says what it actually knows
    -- nothing -- and names the button to press. It does not read the
    loader sensor to guess: that bit reports presence, not latching, and
    a register read behind an option query is a read the operator did not
    ask for."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_magazine_text_starts_unknown")

    r = _run(probe, "scenario", "state-initial")
    assert r["text"].startswith("not loaded"), r["text"]
    assert "Load film" in r["text"], r["text"]
    assert r["mark"] == "absent", r["mark"]
    print(f"test_magazine_text_starts_unknown OK ({r['text']!r})")


# -------------------------------------------------- 2. the refusal paths


def test_release_refuses_an_unknown_start_state():
    """The driver's safety model, unchanged: a scanner whose reg 0x01 is
    neither 0x22 (idle-homed) nor 0x00 (cold) is a state nobody has
    named, and nothing is written from it -- no recovery, no homing to
    "fix" it. There is one unit in existence."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_release_refuses_an_unknown_start_state")

    r = _run(probe, "scenario", "release-unknown-state")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_INVAL, r["statuses"]
    assert "unknown start state" in msg, msg
    assert "0x17" in msg, msg
    assert "No registers were written" in msg, msg
    # Refused, not failed: the state machine never moved.
    assert r["text"].startswith("not loaded"), r["text"]
    assert r["mark"] == "absent", r["mark"]
    print("test_release_refuses_an_unknown_start_state OK (INVAL, nothing written)")


def test_release_from_idle_runs_the_open_and_jog_programs():
    """From the idle-homed state the release half really does reach the
    wire. On the test interface every control IN reads zero, so the
    device-open program stops at its first unacknowledged register write
    -- which is the proof that it ran, and that it fails closed with the
    power-cycle instruction rather than pressing on."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_release_from_idle_runs_the_open_and_jog_programs")

    r = _run(probe, "scenario", "release-idle")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_IO_ERROR, r["statuses"]
    assert "register write not acknowledged" in msg, msg
    assert "magazine open sequence" in msg, msg
    assert "no recovery" in msg, msg
    assert "session is blocked" in msg, msg
    assert "read the log" in msg.lower(), msg
    # A failure after writes must NOT tell the operator to try again: the
    # transport state is unknown, so the next motor command is a decision
    # someone takes after reading the log, not a reflex the message
    # prompts (Astra review 2026-09-13, second round). This is the guard
    # against that language coming back.
    for prompt in ("try again", "press load film", "start over", "retry"):
        assert prompt not in msg.lower(), (prompt, msg)
    # A failed magazine sequence is terminal for the session, and the
    # status line says so instead of inviting another press. Review
    # finding E (2026-09-27): the mark now PERSISTS the failure (was
    # cleared before), so a second PROCESS also refuses.
    assert r["text"].startswith("failed"), r["text"]
    assert r["mark"].startswith("present failed"), r["mark"]
    print("test_release_from_idle_runs_the_open_and_jog_programs OK "
          "(reached the wire, failed closed)")


def test_release_from_cold_stops_at_the_first_motor_completion():
    """A cold unit is brought up first -- and the cold-start program's
    motor completions are fail-closed (offline 2026-09-15): on the test
    interface the status word never reads the done value 0xf8, so the
    program must stop at its FIRST motor completion, with the op index
    the generator says that wait sits at, the session failed and no
    pending load left behind. The reg 0x01 = 0x22 check after a
    completed cold start is four lines further down in gl126.cpp and is
    pinned on the Python driver's identical check
    (tests/test_safety.py); it cannot be reached on a mock that answers
    no poll.

    Before 2026-09-15 the same scenario ran all nine moves against the
    silent mock and only the closing reg 0x01 check refused -- which is
    precisely the gap: eight motor starts after an unconfirmed one."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_release_from_cold_stops_at_the_first_motor_completion")

    sys.path.insert(0, str(REPO / "tools"))
    import gen_sane_tables
    prog = gen_sane_tables.build_cold_init_program()
    masked = [i for i, e in enumerate(prog) if e.kind == "PollMasked"]
    assert len(masked) == 9, masked
    first = masked[0]
    # The first completion follows the first execute pulse (0x0f = 0x01)
    # and only best-effort waits precede it.
    execs = [i for i, e in enumerate(prog)
             if e.kind == "Write" and e.data == bytes([0x0F, 0x01])]
    assert execs[0] < first < execs[1], (execs[:2], first)
    assert all(e.kind != "PollMasked" for e in prog[:first])

    r = _run(probe, "scenario", "release-cold")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_DEVICE_BUSY, r["statuses"]
    assert "a motor move did not complete" in msg, msg
    assert f"cold_init sequence at op {first} " in msg, (first, msg)
    assert "Nothing further was written" in msg, msg
    assert "cold-start sequence completed" not in msg, msg
    assert r["text"].startswith("failed"), r["text"]
    assert r["mark"].startswith("present failed"), r["mark"]
    print("test_release_from_cold_stops_at_the_first_motor_completion OK "
          f"(PollTimeout at op {first}, the first of nine; failed mark persists)")


def test_a_usb_failure_mid_sequence_fails_the_session():
    """The hole Astra's 2026-09-13 review found: only `OpsError` marked the
    session failed, but a real USB failure arrives as a plain
    `SaneException` from the device layer -- and so do the register reads
    that follow a motor sequence. Such an exception used to leave the
    magazine state Released and the pending-load mark on disk AFTER a real
    failure, so the next scan would have driven the loader again.

    Injected through genesys's own test checkpoint (a no-op on the USB
    interface): for the release, at the moment the guard is armed (the
    cold-start program no longer completes on the silent mock, see
    test_release_from_cold_stops_at_the_first_motor_completion); for the
    eject, after the eject program has written. Whatever the exception,
    the session must end up failed and any pending load must be gone."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_usb_failure_mid_sequence_fails_the_session")

    for scenario in ("release-usb-failure", "eject-usb-failure"):
        r = _run(probe, "scenario", scenario)
        status, msg = r["statuses"][0]
        assert status == SANE_STATUS_IO_ERROR, (scenario, r["statuses"])
        assert "injected" in msg, (scenario, msg)
        assert r["text"].startswith("failed"), (scenario, r["text"])
        assert r["mark"].startswith("present failed"), (scenario, r["mark"])
    print("test_a_usb_failure_mid_sequence_fails_the_session OK "
          "(release and eject: failed, mark now PERSISTS the failure)")


def test_load_document_never_looks_at_the_frame_number():
    """WP-5: load_document() cannot move the magazine on an impossible
    scan request any more (Astra's 2026-09-13 concern), because it cannot
    move the magazine AT ALL -- it is a pure checker (section 3.4). A
    pending Released mark refuses NO_DOCS "press Load film first" no
    matter what dev->settings.frame holds, frame 9 (past the holder's six
    apertures) included; the request validation Astra's review was about
    still runs, unchanged, inside offset_calibration() at the actual scan,
    not here."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_load_document_never_looks_at_the_frame_number")

    r = _run(probe, "scenario", "load-mark-frame-does-not-matter")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_NO_DOCS, r["statuses"]
    assert "press Load film first" in msg, msg
    assert "Nothing was written" in msg, msg
    assert not r["text"].startswith("failed"), r["text"]
    assert r["mark"].startswith("present"), r["mark"]
    print("test_load_document_never_looks_at_the_frame_number OK "
          "(NO_DOCS regardless of the frame value, mark kept)")


def test_a_failed_sequence_is_terminal():
    """After a failure the transport state is unknown, so a second press
    refuses with zero transfers rather than driving the motor again from
    a state nobody can name."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_failed_sequence_is_terminal")

    r = _run(probe, "scenario", "release-twice-after-failure")
    assert len(r["statuses"]) == 2, r["statuses"]
    first, second = r["statuses"]
    assert first[0] == SANE_STATUS_IO_ERROR, first
    assert second[0] == SANE_STATUS_INVAL, second
    assert "failed earlier in this session" in second[1], second[1]
    assert "Nothing was written" in second[1], second[1]
    print("test_a_failed_sequence_is_terminal OK (second press refused)")


def test_eject_refuses_from_cold_and_sends_you_to_load_film():
    """The driver runs cold_init() before an eject; this backend does
    not. On a cold unit the jog behind Load film IS the vendor's own
    release, and it is the one path verified from that state -- so the
    operator is sent there instead of bringing up a second cold motor
    path that nothing has ever exercised."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_eject_refuses_from_cold_and_sends_you_to_load_film")

    r = _run(probe, "scenario", "eject-cold")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_INVAL, r["statuses"]
    assert "cold" in msg, msg
    assert "Load film" in msg, msg
    assert "Nothing was written" in msg, msg

    # And an unnameable state refuses there too, before any write.
    r2 = _run(probe, "scenario", "eject-unknown-state")
    assert r2["statuses"][0][0] == SANE_STATUS_INVAL, r2["statuses"]
    assert "unknown start state" in r2["statuses"][0][1], r2["statuses"]
    print("test_eject_refuses_from_cold_and_sends_you_to_load_film OK")


def test_eject_with_no_magazine_does_nothing():
    """The loader sensor clear means there is nothing in the slot. The
    driver logs "nothing to do" and returns without a motor command; so
    does this."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_eject_with_no_magazine_does_nothing")

    r = _run(probe, "scenario", "eject-no-magazine")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_GOOD, r["statuses"]
    assert r["text"].startswith("ejected"), r["text"]
    print("test_eject_with_no_magazine_does_nothing OK (GOOD, no motor command)")


def test_eject_refuses_the_base_table_state():
    """Test 44's state: regs 0x3b/0x3c reading 0xff/0xff is the scan-
    session base table with no scan phase after it. The driver's eject
    stalled 2/2 from there and no vendor flow ejects from it. Two
    register reads, no write."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_eject_refuses_the_base_table_state")

    r = _run(probe, "scenario", "eject-base-table-state")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_INVAL, r["statuses"]
    assert "0xff/0xff" in msg, msg
    assert "stalled" in msg, msg
    assert "Nothing was written" in msg, msg
    print("test_eject_refuses_the_base_table_state OK (refused read-only)")


# ------------------------------------------------- 3. the load half


def test_the_option_handlers_reach_the_hooks():
    """The three buttons really are wired to the magazine hooks: Load film
    and Eject film, pressed from a start state nobody can name, each
    return the refusal only those hooks produce (the text itself is
    asserted on the direct calls elsewhere -- the backend's public entry
    points wrap every exception into a bare status code, so the message
    never escapes the library). Check status never refuses (it only
    reads), so its wiring proof is simply that it succeeds from a state
    that refused the other two."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_the_option_handlers_reach_the_hooks")

    for scenario in ("wiring-load", "wiring-eject"):
        r = _run(probe, "scenario", scenario)
        assert r["optstatuses"] == [SANE_STATUS_INVAL], (scenario, r["optstatuses"])

    r = _run(probe, "scenario", "wiring-check-status")
    assert r["optstatuses"] == [SANE_STATUS_GOOD], r["optstatuses"]
    # reg 0x01 = 0x17 (not idle, not cold) with reg 0x101 at its
    # constructor default (0x00, sensor clear): the "no magazine" row.
    assert r["text"].startswith("no magazine"), r["text"]
    print("test_the_option_handlers_reach_the_hooks OK "
          "(load-film and eject-film refuse; check-status succeeds)")


def test_a_scan_with_no_release_pending_never_touches_the_magazine():
    """The load half hangs off sane_start, so this is the case that must
    cost nothing: with no mark and no in-process release, load_document()
    returns before reading a single register, and the scan proceeds into
    calibration exactly as it did before WP-4.

    Two observations. First the hook directly, on a device seeded with a
    start state it WOULD refuse: it returns GOOD, which is only possible
    if it never looked. Then the whole flow, observed the way
    tests/test_sane_calibration_cache.py observes it -- the core records
    the progress message "offset_calibration" just before the offset hook
    runs, so reaching it proves nothing diverted the scan."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_scan_with_no_release_pending_never_touches_the_magazine")

    r = _run(probe, "scenario", "load-no-mark")
    assert r["statuses"] == [(SANE_STATUS_GOOD, "")], r["statuses"]
    assert r["text"].startswith("not loaded"), r["text"]

    r2 = _run(probe, "scenario", "start-no-mark")
    assert r2["progress"] == "offset_calibration", r2
    assert r2["text"].startswith("not loaded"), r2["text"]
    print("test_a_scan_with_no_release_pending_never_touches_the_magazine OK "
          "(hook returns without reading, calibration entered)")


def test_a_scan_after_a_failed_magazine_sequence_refuses():
    """load_document() checks the in-process FAILED state first, before it
    asks about the mark at all -- the mark now agrees anyway (review
    finding E: a failure WRITES a "failed" mark instead of clearing it),
    but the in-process check must not depend on that ordering."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_scan_after_a_failed_magazine_sequence_refuses")

    r = _run(probe, "scenario", "load-after-failure")
    assert len(r["statuses"]) == 2, r["statuses"]
    release, load = r["statuses"]
    assert release[0] == SANE_STATUS_IO_ERROR, release
    assert load[0] == SANE_STATUS_INVAL, load
    assert "failed earlier in this session" in load[1], load[1]
    assert "not started on top of it" in load[1], load[1]
    assert "Nothing was written" in load[1], load[1]
    print("test_a_scan_after_a_failed_magazine_sequence_refuses OK")


def test_a_mark_for_another_device_is_ignored_and_cleared():
    """The mark carries the SANE device name, which contains the USB
    address. A power cycle re-enumerates the unit, so a mark written
    before one names a device that no longer exists -- and must never
    authorise a feed. It is ignored and removed, and the scan proceeds
    normally."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_mark_for_another_device_is_ignored_and_cleared")

    r = _run(probe, "scenario", "start-mark-other-device")
    assert r["progress"] == "offset_calibration", r
    assert r["mark"] == "absent", r["mark"]
    print("test_a_mark_for_another_device_is_ignored_and_cleared OK")


def test_a_pending_load_refuses_and_keeps_the_mark_no_matter_what_the_hardware_says():
    """WP-5: a Released mark refuses read-only NO_DOCS "press Load film
    first" and KEEPS the mark -- pressing Load film again (which now
    does its own wait) is the whole fix; losing the mark would force
    starting over. Two DIFFERENT hardware seed combinations (one that
    used to mean "sensor clear", one that used to mean "wrong register
    state") produce the IDENTICAL refusal now, because load_document()
    reads neither any more.

    Also checked through sane_start, which is where this really happens:
    the refusal must land BEFORE calibration, so nothing is written."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_pending_load_refuses_and_keeps_the_mark_no_matter_what_the_hardware_says")

    for scenario in ("load-mark-no-magazine", "load-mark-bad-state"):
        r = _run(probe, "scenario", scenario)
        status, msg = r["statuses"][0]
        assert status == SANE_STATUS_NO_DOCS, (scenario, r["statuses"])
        assert "press Load film first" in msg, (scenario, msg)
        assert "Nothing was written" in msg, (scenario, msg)
        assert r["mark"].startswith("present"), (scenario, r["mark"])
        assert not r["text"].startswith("failed"), (scenario, r["text"])

    r2 = _run(probe, "scenario", "start-mark-no-magazine")
    assert r2["start"][0] == SANE_STATUS_NO_DOCS, r2["start"]
    assert r2["progress"] != "offset_calibration", r2
    assert r2["mark"].startswith("present"), r2["mark"]
    print("test_a_pending_load_refuses_and_keeps_the_mark_no_matter_what_the_hardware_says OK "
          "(NO_DOCS before calibration, mark kept, hardware irrelevant)")


def test_a_loaded_mark_lets_a_scan_through():
    """Section 3.3/3.4: `scanimage -n --load-film` completes the load and
    exits; a SEPARATE `scanimage` invocation that scans needs to know the
    magazine is loaded from the mark alone, exactly as it already needed
    to know a release/eject was pending. The mark is NOT consumed by a
    mere check -- only an eject, a failure, or a cold read clears it."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_loaded_mark_lets_a_scan_through")

    r = _run(probe, "scenario", "load-mark-loaded")
    assert r["statuses"] == [(SANE_STATUS_GOOD, "")], r["statuses"]
    assert r["mark"].startswith("present loaded"), r["mark"]

    r2 = _run(probe, "scenario", "start-mark-loaded")
    assert r2["progress"] == "offset_calibration", r2
    assert r2["mark"].startswith("present loaded"), r2["mark"]
    assert r2["text"].startswith("loaded"), r2["text"]
    print("test_a_loaded_mark_lets_a_scan_through OK "
          "(hook proceeds, calibration entered, mark not consumed)")


# ---------------------------------------------- 4. the one-button load (WP-5)


def test_the_edge_wait_times_out_without_writing_anything():
    """Section 3.2/3.6's two timeout shapes, both from an Ejected start
    (no jog either way): the sensor never clears at all ("did not come
    loose"), or it clears immediately and just stays that way ("clear
    only" -- section 6's name for it). Either way: no motor write of any
    kind (EDGEWRITE 0), Released (in-process state and status text),
    SANE_STATUS_GOOD -- the operator having done nothing, or not
    finished, is not itself an error. Review finding G: since both start
    from Ejected, the ON-DISK MARK is "ejected" in both cases, not
    "released" -- see test_load_film_from_ejected_waits_instead_of_jogging."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_the_edge_wait_times_out_without_writing_anything")

    r = _run(probe, "scenario", "load-film-edge-present-only")
    assert len(r["statuses"]) == 2, r["statuses"]
    assert r["statuses"][1] == (SANE_STATUS_GOOD, ""), r["statuses"]
    assert r["edgewrite"] == 0, r
    assert r["mark"].startswith("present ejected"), r["mark"]
    assert r["text"].startswith("did not come loose"), r["text"]

    # poll_cap_ms=300: saw_clear is debounced (review finding I), so this
    # needs 2 consecutive clear reads' worth of budget, not just one.
    r2 = _run(probe, "scenario", "release-after-eject", poll_cap_ms=300)
    assert r2["statuses"][1] == (SANE_STATUS_GOOD, ""), r2["statuses"]
    assert r2["edgewrite"] == 0, r2
    assert r2["mark"].startswith("present ejected"), r2["mark"]
    assert r2["text"].startswith("press Load film, then take out"), r2["text"]
    print("test_the_edge_wait_times_out_without_writing_anything OK "
          "(both timeout shapes: Released in-process, mark stays 'ejected', no writes, GOOD)")


def test_an_ejected_origin_retry_never_jogs_either_way():
    """Review finding G, both retry sub-cases: an EJECTED-origin press
    that times out -- whether or not that wait ever saw a clear -- is
    retried by a second press that still does not jog (Ejected never
    jogs) and, once that second wait resolves, still runs "open" before
    "load" (the lenient regs rule). Before the fix, the ORIGIN was
    forgotten the moment state became Released: a no-clear retry re-ran
    open+jog from scratch, and a clear-seen retry skipped "open" entirely
    and used the STRICT regs rule -- either way wrong for a magazine that
    was ejected, never jogged.

    (Test 51's literal double jog -- a RELEASED-origin retry, re-running
    open+jog because the FIRST press already succeeded once as an
    ordinary release -- is not reachable through this probe at all:
    OPEN's first acknowledgement always fails on this always-zero-
    answering mock, so a timed-out Released state is only ever reachable
    here through an Ejected origin. Coverage gap, documented in
    docs/sane-wp5-load-button.md section 9.3.)"""
    probe = _build_probe()
    if probe is None:
        return _skip("test_an_ejected_origin_retry_never_jogs_either_way")

    r = _run(probe, "scenario", "load-film-retry-no-clear-rejogs", poll_cap_ms=2000)
    assert len(r["statuses"]) == 3, r["statuses"]
    press1, press2 = r["statuses"][1], r["statuses"][2]
    assert press1 == (SANE_STATUS_GOOD, ""), press1
    assert press2[0] == SANE_STATUS_IO_ERROR, press2
    assert "magazine open sequence" in press2[1], press2[1]
    assert r["edgewrite"] == 0, r

    r2 = _run(probe, "scenario", "load-film-retry-with-clear-then-edge", poll_cap_ms=2000)
    assert len(r2["statuses"]) == 3, r2["statuses"]
    press1b, press2b = r2["statuses"][1], r2["statuses"][2]
    assert press1b == (SANE_STATUS_GOOD, ""), press1b
    assert press2b[0] == SANE_STATUS_IO_ERROR, press2b
    assert "magazine open sequence" in press2b[1], press2b[1]
    assert r2["edgewrite"] == 0, r2
    print("test_an_ejected_origin_retry_never_jogs_either_way OK "
          "(both retry sub-cases: no jog, open still runs, lenient regs rule)")


# ------------------------------------------------------ 5. Check status (WP-5)


def test_check_status_reads_the_hardware_and_updates_the_line():
    """Section 3.5's table, row by row. Only the cold row is a real
    MagazineState transition (Unknown, marks cleared as stale); every
    other row is a read-only snapshot that overrides the status line
    without moving the state machine. Hardware evidence alone never
    promotes anything to Loaded (the sensor cannot tell "loaded" from
    "loose in the slot") -- the Loaded row is reached here THROUGH a
    cross-process "loaded" mark, not invented from the register state."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_check_status_reads_the_hardware_and_updates_the_line")

    r = _run(probe, "scenario", "check-status-cold")
    assert r["statuses"] == [(SANE_STATUS_GOOD, "")], r["statuses"]
    assert r["text"].startswith("cold"), r["text"]
    assert r["mark"] == "absent", r["mark"]   # cleared as stale

    r2 = _run(probe, "scenario", "check-status-no-magazine")
    assert r2["text"] == "no magazine in the slot", r2["text"]

    r3 = _run(probe, "scenario", "check-status-present-not-loaded")
    assert r3["text"].startswith("magazine present, not loaded"), r3["text"]

    r4 = _run(probe, "scenario", "check-status-loaded-mark")
    assert r4["text"].startswith("loaded"), r4["text"]
    assert r4["mark"].startswith("present loaded"), r4["mark"]   # unaffected

    r5 = _run(probe, "scenario", "check-status-unknown-hw")
    assert r5["text"].startswith("unknown state"), r5["text"]

    print("test_check_status_reads_the_hardware_and_updates_the_line OK "
          "(all five rows of section 3.5's table)")


def test_magazine_state_values_are_pinned():
    """docs/sane-wp5-load-button.md section 3.6: all twelve values --
    seven from the ordinary state machine, three cross-process/retry
    variants, and the four that only Check status can ever produce -- are
    each at most 40 characters (the KSaneWidgets combo limit, Test 76) and
    all mutually distinct, exactly as the "magazine" option's
    SANE_CONSTRAINT_STRING_LIST must be."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_magazine_state_values_are_pinned")

    r = _run(probe, "layout")
    values = r.get("magazine_values") or []
    expected = [
        "not loaded -- press Load film",
        "released earlier -- Load film again",
        "ejected earlier -- press Load film",
        "press Load film, then take out, push in",
        "did not come loose? Load film again",
        "loaded -- set Frame, press Scan",
        "ejected -- swap strip, then Load film",
        "failed -- power-cycle, then Load film",
        "cold -- press Load film",
        "no magazine in the slot",
        "magazine present, not loaded? Load film",
        "unknown state -- power-cycle, Load film",
    ]
    assert values == expected, values
    assert len(set(values)) == len(values), values
    for v in values:
        assert len(v) <= 40, (len(v), v)
    print(f"test_magazine_state_values_are_pinned OK "
          f"({len(values)} values, longest {max(len(v) for v in values)} chars)")


# --------------------------------------------- 6. review round (2026-09-27)


def test_saw_clear_is_debounced():
    """Review finding I: saw_clear (which drives both the status wording
    and, cross-process, the mark's kind on a timeout) is a DEBOUNCED fact
    -- true only once the sensor reads clear for 2 CONSECUTIVE polls, not
    on a single glitchy read. One script absorbs a single-poll glitch and
    then genuinely clears, proving the glitch does not block a real
    resolve (open still reaches the wire); the other has the identical
    glitch with nothing genuine after it, proving the glitch alone does
    NOT set saw_clear (times out with "did not come loose", not the
    default wording a real clear would produce)."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_saw_clear_is_debounced")

    r = _run(probe, "scenario", "load-film-edge-debounce-glitch-then-resolve", poll_cap_ms=2000)
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, release = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert release[0] == SANE_STATUS_IO_ERROR, release
    assert "magazine open sequence" in release[1], release[1]
    assert r["edgewrite"] == 0, r

    r2 = _run(probe, "scenario", "load-film-edge-debounce-single-glitch-times-out",
              poll_cap_ms=500)
    assert len(r2["statuses"]) == 2, r2["statuses"]
    eject2, release2 = r2["statuses"]
    assert eject2[0] == SANE_STATUS_GOOD, eject2
    assert release2 == (SANE_STATUS_GOOD, ""), release2
    assert r2["edgewrite"] == 0, r2
    assert r2["text"].startswith("did not come loose"), r2["text"]
    print("test_saw_clear_is_debounced OK "
          "(a single-poll glitch neither blocks a real resolve nor counts by itself)")


def test_the_post_edge_check_catches_a_late_class_or_presence_change():
    """Review finding A: the edge wait only watches bit 0x08 (present/
    clear); it does not by itself prove the status byte is in the DONE
    class Tests 75-77/90 loaded from (0xf8-shaped). A magazine pulled
    back out during the 600 ms settle, or a present-but-busy class, must
    not reach LOAD -- both refused the same way the pre-existing regs
    check already refuses, no "open" attempted either time."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_the_post_edge_check_catches_a_late_class_or_presence_change")

    r = _run(probe, "scenario", "load-film-edge-seen-then-not-idle", poll_cap_ms=2000)
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, release = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert release[0] == SANE_STATUS_INVAL, release
    assert "0xd8" in release[1], release[1]
    assert "the loader sensor present in the idle class" in release[1], release[1]
    assert r["edgewrite"] == 0, r
    assert r["mark"].startswith("present failed"), r["mark"]

    r2 = _run(probe, "scenario", "load-film-edge-seen-then-clear", poll_cap_ms=2000)
    assert len(r2["statuses"]) == 2, r2["statuses"]
    eject2, release2 = r2["statuses"]
    assert eject2[0] == SANE_STATUS_GOOD, eject2
    assert release2[0] == SANE_STATUS_INVAL, release2
    assert "0xf0" in release2[1], release2[1]
    assert r2["mark"].startswith("present failed"), r2["mark"]
    print("test_the_post_edge_check_catches_a_late_class_or_presence_change OK "
          "(present-but-busy and clear-after-settle both refused, no open)")


def test_a_killed_process_leaves_a_refusing_mark():
    """Review finding B: a process that dies WHILE WAITING (Ctrl-C on
    `scanimage -n --load-film`, "Terminate" on a frozen digiKam) must
    leave a mark that makes the next scan refuse, never one that lets it
    proceed against a jogged/ejected-but-unloaded magazine. Modelled by
    throwing at the wait's own first poll; the unwinding
    MagazineFailGuard has the last word (a "failed" mark, not the
    "released"/"ejected" one written just before the wait started)."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_killed_process_leaves_a_refusing_mark")

    r = _run(probe, "scenario", "load-film-killed-mid-wait", poll_cap_ms=2000)
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, release = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert release[0] == SANE_STATUS_IO_ERROR, release
    assert "injected" in release[1], release[1]
    assert r["mark"].startswith("present failed"), r["mark"]
    assert r["text"].startswith("failed"), r["text"]
    print("test_a_killed_process_leaves_a_refusing_mark OK (failed mark survives the kill)")


def test_a_cross_process_loaded_mark_blocks_load_film():
    """Review finding D: a "loaded" mark from an EARLIER PROCESS blocks a
    second "Load film" press exactly like the in-process Loaded state
    already does -- read-only refusal, mark untouched (nothing here ever
    reaches check_start_state, let alone the guard)."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_cross_process_loaded_mark_blocks_load_film")

    r = _run(probe, "scenario", "load-film-blocked-by-loaded-mark")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_INVAL, r["statuses"]
    assert "already loaded" in msg, msg
    assert "Nothing was written" in msg, msg
    assert r["mark"].startswith("present loaded"), r["mark"]
    assert r["text"].startswith("loaded"), r["text"]
    print("test_a_cross_process_loaded_mark_blocks_load_film OK")


def test_a_failed_mark_persists_and_blocks_everything():
    """Review finding E: a magazine sequence that fails leaves the
    transport in a state nobody can name, and that fact must survive the
    failing process's exit -- a SECOND process has no other way to know.
    A "failed" mark now blocks Scan, Load film and Eject film alike, in a
    FRESH process that never saw the failure itself; only a cold reg 0x01
    read clears it (test_check_status_reads_the_hardware_and_updates_the_line's
    cold row, and Load film's own start-of-call precheck)."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_failed_mark_persists_and_blocks_everything")

    r = _run(probe, "scenario", "load-film-failed-mark-blocks-scan")
    assert len(r["statuses"]) == 2, r["statuses"]
    release, load = r["statuses"]
    assert release[0] == SANE_STATUS_IO_ERROR, release
    assert load[0] == SANE_STATUS_INVAL, load
    assert "failed earlier" in load[1], load[1]
    assert r["mark"].startswith("present failed"), r["mark"]

    with tempfile.TemporaryDirectory() as lock_dir:
        r1 = _run(probe, "scenario", "state-mark-failed-pending", lock_dir=lock_dir)
        assert r1["mark"].startswith("present failed"), r1["mark"]
        assert r1["text"].startswith("failed"), r1["text"]

        r2 = _run(probe, "scenario", "load-mark-failed-crossproc", lock_dir=lock_dir)
    assert len(r2["statuses"]) == 3, r2["statuses"]
    for status, msg in r2["statuses"]:
        assert status == SANE_STATUS_INVAL, r2["statuses"]
        assert "failed earlier" in msg, msg
    assert r2["mark"].startswith("present failed"), r2["mark"]
    print("test_a_failed_mark_persists_and_blocks_everything OK "
          "(same process and a fresh one, Scan/Load film/Eject film all refuse)")


def test_an_ejected_origin_retry_keeps_running_open():
    """Review finding G, the bug it found: an Ejected-origin press that
    times out with a clear seen, retried, must STILL run "open" (no jog
    either time) and use the LENIENT regs rule. Before the fix, the
    second press forgot the origin the moment state became Released,
    used the strict 0x00/0x00 rule, and would have refused Failed on
    exactly the regs (0x02/0x00) a 600 dpi scan leaves -- for a magazine
    that was never actually jogged loose in the first place."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_an_ejected_origin_retry_keeps_running_open")

    r = _run(probe, "scenario", "load-film-ejected-origin-retry-keeps-open", poll_cap_ms=2000)
    assert len(r["statuses"]) == 3, r["statuses"]
    eject, press1, press2 = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert press1 == (SANE_STATUS_GOOD, ""), press1
    assert press2[0] == SANE_STATUS_IO_ERROR, press2
    assert "magazine open sequence" in press2[1], press2[1]
    assert "not in the state" not in press2[1], press2[1]
    print("test_an_ejected_origin_retry_keeps_running_open OK "
          "(open reached the wire on the retry, lenient regs rule applied)")


def test_a_power_cycle_inside_one_process_resets_loaded():
    """Review finding H: neither an in-process Loaded/Failed claim nor a
    matching mark survives a power cycle happening INSIDE one process's
    lifetime, between one Load film press and the next. Modelled through
    a "loaded" mark (a real LOAD never completes on this always-zero-
    answering mock, so the in-process state is not directly reachable in
    a probe scenario, but the precheck treats the two identically --
    "had_mark || had_state"): the cold read must fall into the fresh
    cold_init path, not refuse "already loaded"."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_power_cycle_inside_one_process_resets_loaded")

    r = _run(probe, "scenario", "load-film-loaded-then-cold", poll_cap_ms=2000)
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_DEVICE_BUSY, r["statuses"]
    assert "magazine cold_init sequence" in msg, msg
    assert "already loaded" not in msg, msg
    print("test_a_power_cycle_inside_one_process_resets_loaded OK "
          "(cold_init ran, not the already-loaded refusal)")


def test_check_status_reports_loaded_despite_a_busy_class():
    """Review finding C: right after LOAD completes, or during
    calibration, reg 0x101 reads 0xdc/0xd8-shaped -- NEITHER is the idle
    class (0xf0-shaped) -- and a genuinely loaded magazine must be
    reported as such, not "unknown state -- power-cycle", just because of
    that. "unknown state" is now reserved for reg 0x01 outside
    {0x22, 0x00}."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_check_status_reports_loaded_despite_a_busy_class")

    r = _run(probe, "scenario", "check-status-loaded-non-idle")
    assert r["text"].startswith("loaded"), r["text"]
    print("test_check_status_reports_loaded_despite_a_busy_class OK")


# ------------------------------------------------------------ 8. dialog surface (2026-09-27)


def test_dead_options_are_inactive_and_film_group_is_placed_and_ordered():
    """The digiKam review (docs/ROADMAP.md, "digiKam dialog usability"):
    every genesys option that does nothing on GL126 is hidden, so KSane's
    "Scanner Specific Options" tab only shows what actually works; the
    film magazine controls sit in their own "Film" group, right after
    "Enhancement" and before "Extras"; the scan-area options stay active
    (KSane's preview canvas depends on them); and the frontend's very
    first impression -- the mode and colour filter it opens with -- are a
    capture this backend actually performs.

    Also pins the two 2026-09-27 libksane workarounds (Test 91,
    docs/sane-install.md S6): the resolution word list is ascending with
    600 (the backend default) first, and the magazine status values are
    the untranslated English constants."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_dead_options_are_inactive_and_film_group_is_placed_and_ordered")

    r = _run(probe, "layout")
    items = r["items"]
    by_name = {f["name"]: (idx, f) for idx, f in items if "name" in f}

    dead = ["scan-exposure-time", "brightness", "contrast", "lamp-off-time",
            "lamp-off-scan", "color-filter", "calibration-file", "expiration-time",
            "clear-calibration", "force-calibration", "ignore-internal-offsets"]
    for name in dead:
        assert name in by_name, (name, sorted(by_name))
        _, f = by_name[name]
        assert f["inactive"] == "1", (name, f)

    # Scan-area (geometry) options: KSane's preview canvas depends on
    # them, so they must stay active even though they do nothing either.
    for name in ("tl-x", "tl-y", "br-x", "br-y"):
        assert name in by_name, name
        assert by_name[name][1]["inactive"] == "0", by_name[name]

    for name in ("magazine", "load-film", "eject-film", "check-status", "frame"):
        assert name in by_name, name
        assert by_name[name][1]["inactive"] == "0", by_name[name]

    magazine_idx = by_name["magazine"][0]
    load_idx = by_name["load-film"][0]
    eject_idx = by_name["eject-film"][0]
    check_status_idx = by_name["check-status"][0]
    frame_idx = by_name["frame"][0]
    # In this order, and consecutive -- no other option sits between the
    # group and Frame.
    assert load_idx == magazine_idx + 1, by_name
    assert eject_idx == load_idx + 1, by_name
    assert check_status_idx == eject_idx + 1, by_name
    assert frame_idx == check_status_idx + 1, by_name

    # The group immediately above them: a GROUP item at magazine_idx - 1,
    # titled "Film".
    group_idx = magazine_idx - 1
    group_fields = next((f for idx, f in items if idx == group_idx), None)
    assert group_fields is not None, items
    assert group_fields["type"] == str(SANE_TYPE_GROUP), group_fields
    assert group_fields["title"] == "Film", group_fields

    # And that group comes right after "Enhancement", before "Extras": the
    # two groups on either side of it, in the item list, in order.
    titles = [f["title"] for _, f in items if f["type"] == str(SANE_TYPE_GROUP)]
    i = titles.index("Film")
    assert titles[i - 1] == "Enhancement", titles
    assert titles[i + 1] == "Extras", titles

    assert r["default_mode"] == "Color", r["default_mode"]
    assert r["default_color_filter"] == "None", r["default_color_filter"]

    # Two libksane display bugs, worked around backend-side (docs/
    # sane-install.md S6): the resolution list must be ascending with 600
    # (the backend's own default) first, because LabeledCombo's
    # constructor matches setCurrentText's unit-less number against each
    # item's unit-bearing TEXT and finds nothing, leaving index 0
    # selected regardless of the backend's actual value -- ascending order
    # makes that stale index 0 agree with the truth. And the magazine
    # status values must be the plain English constants, untranslated:
    # LabeledCombo's setValue (fed by KSaneCore::Option::valueChanged)
    # matches itemData (this string-list, i.e. the INTERNAL value) against
    # a TRANSLATED value whenever the current msgid has a catalog
    # translation, so a translated value-list option never follows a
    # backend-side change (Test 91, 2026-09-27: the status line stayed on
    # "okänt" after Load film moved the state to Released).
    resolution_values = r.get("resolution_values") or []
    assert resolution_values == sorted(resolution_values), resolution_values
    assert resolution_values[0] == 600, resolution_values
    assert resolution_values == [600, 1200, 2400, 3600, 7200], resolution_values

    expected_magazine_values = [
        "not loaded -- press Load film",
        "released earlier -- Load film again",
        "ejected earlier -- press Load film",
        "press Load film, then take out, push in",
        "did not come loose? Load film again",
        "loaded -- set Frame, press Scan",
        "ejected -- swap strip, then Load film",
        "failed -- power-cycle, then Load film",
        "cold -- press Load film",
        "no magazine in the slot",
        "magazine present, not loaded? Load film",
        "unknown state -- power-cycle, Load film",
    ]
    assert r.get("magazine_values") == expected_magazine_values, r.get("magazine_values")

    # The gate is asic_type == GL126, not a blanket change: on another chip
    # (GL124) the film group/options stay inactive as before, and two
    # options with no OTHER conditional disabling anywhere in genesys.cpp
    # (brightness, contrast -- unlike calibration-file, which the core
    # itself disables for root) must still be ACTIVE, proving GL126's new
    # DISABLE block did not leak into other models.
    other = _run(probe, "layout", GL124_DEVICE)
    other_by_name = {f["name"]: (idx, f) for idx, f in other["items"] if "name" in f}
    for name in ("brightness", "contrast"):
        assert other_by_name[name][1]["inactive"] == "0", (name, other_by_name[name])
    for name in ("magazine", "load-film", "eject-film", "check-status", "frame"):
        assert other_by_name[name][1]["inactive"] == "1", (name, other_by_name[name])
    other_group_idx = other_by_name["magazine"][0] - 1
    other_group = next((f for idx, f in other["items"] if idx == other_group_idx), None)
    assert other_group is not None and other_group["title"] == "Film", other_group
    assert other_group["inactive"] == "1", other_group

    print("test_dead_options_are_inactive_and_film_group_is_placed_and_ordered OK "
          f"(Film group between Enhancement and Extras at index {group_idx}; "
          f"magazine={magazine_idx} load={load_idx} eject={eject_idx} "
          f"check-status={check_status_idx} frame={frame_idx}; "
          "default mode Color, colour filter None)")


def test_switching_to_gray_does_not_reopen_hidden_options_on_gl126():
    """2026-09-27 fix: set_option_value's OPT_MODE handler unconditionally
    ENABLEd color-filter on a plain switch to Gray (and OPT_BIT_DEPTH's
    handler unconditionally ENABLEd brightness/contrast at depth <= 8) --
    both are now gated off for GL126, alongside init_options' own default.
    A frontend switching Color -> Gray must not see color-filter,
    brightness or contrast reappear (with color-filter's stock default,
    Green on this model, which gl126::calculate_scan_session refuses
    before any device I/O). On GL124 the pre-existing behaviour --
    color-filter re-enabled on Gray -- must be unchanged."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_switching_to_gray_does_not_reopen_hidden_options_on_gl126")

    r = _run(probe, "layout", GL126_DEVICE, "Gray")
    by_name = {f["name"]: (idx, f) for idx, f in r["items"] if "name" in f}
    assert r["default_mode"] == "Gray", r["default_mode"]
    for name in ("color-filter", "brightness", "contrast"):
        assert by_name[name][1]["inactive"] == "1", (name, by_name[name])

    other = _run(probe, "layout", GL124_DEVICE, "Gray")
    other_mode = other["default_mode"]
    assert other_mode == "Gray", other_mode
    other_by_name = {f["name"]: (idx, f) for idx, f in other["items"] if "name" in f}
    # Unchanged pre-existing behaviour: GL124 is not GL646+cis, so the
    # original condition still re-enables color-filter on Gray.
    assert other_by_name["color-filter"][1]["inactive"] == "0", other_by_name["color-filter"]

    print("test_switching_to_gray_does_not_reopen_hidden_options_on_gl126 OK "
          "(GL126: color-filter/brightness/contrast stay inactive, mode reads Gray; "
          "GL124: color-filter still re-enabled on Gray, unchanged)")


def main() -> int:
    tests = [
        test_magazine_options_exist_only_for_gl126,
        test_magazine_text_starts_unknown,
        test_the_status_line_is_readable_and_comes_first,
        test_setting_the_status_line_is_a_no_op,
        test_the_status_line_reports_a_load_pending_from_another_process,
        test_a_scan_after_eject_refuses_no_matter_what_the_hardware_says,
        test_the_edge_resolving_lets_the_ejected_kind_load,
        test_the_regs_check_after_the_edge_still_refuses_the_base_table_state,
        test_a_scan_after_eject_without_a_magazine_refuses_and_keeps_the_mark,
        test_a_cold_read_forces_the_full_release_path_even_from_ejected,
        test_an_ejected_mark_from_another_process_refuses_the_scan,
        test_an_ejected_mark_for_another_device_is_ignored_and_cleared,
        test_load_film_from_ejected_waits_instead_of_jogging,
        test_release_refuses_an_unknown_start_state,
        test_release_from_idle_runs_the_open_and_jog_programs,
        test_release_from_cold_stops_at_the_first_motor_completion,
        test_a_failed_sequence_is_terminal,
        test_a_usb_failure_mid_sequence_fails_the_session,
        test_load_document_never_looks_at_the_frame_number,
        test_eject_refuses_from_cold_and_sends_you_to_load_film,
        test_eject_with_no_magazine_does_nothing,
        test_eject_refuses_the_base_table_state,
        test_the_option_handlers_reach_the_hooks,
        test_a_scan_with_no_release_pending_never_touches_the_magazine,
        test_a_scan_after_a_failed_magazine_sequence_refuses,
        test_a_mark_for_another_device_is_ignored_and_cleared,
        test_a_pending_load_refuses_and_keeps_the_mark_no_matter_what_the_hardware_says,
        test_a_loaded_mark_lets_a_scan_through,
        test_the_edge_wait_times_out_without_writing_anything,
        test_an_ejected_origin_retry_never_jogs_either_way,
        test_check_status_reads_the_hardware_and_updates_the_line,
        test_magazine_state_values_are_pinned,
        test_saw_clear_is_debounced,
        test_the_post_edge_check_catches_a_late_class_or_presence_change,
        test_a_killed_process_leaves_a_refusing_mark,
        test_a_cross_process_loaded_mark_blocks_load_film,
        test_a_failed_mark_persists_and_blocks_everything,
        test_an_ejected_origin_retry_keeps_running_open,
        test_a_power_cycle_inside_one_process_resets_loaded,
        test_check_status_reports_loaded_despite_a_busy_class,
        test_dead_options_are_inactive_and_film_group_is_placed_and_ordered,
        test_switching_to_gray_does_not_reopen_hidden_options_on_gl126,
    ]
    passed = 0
    skipped = 0
    for t in tests:
        if t() == "skipped":
            skipped += 1
        else:
            passed += 1
    if skipped:
        print(f"\n{passed} tests passed, {skipped} skipped.")
    else:
        print(f"\n{passed} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
