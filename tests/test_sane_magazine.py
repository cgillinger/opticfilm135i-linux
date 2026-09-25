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
  4. a stale or foreign mark cannot reach the load feed.

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

SANE_TYPE_STRING = 3
SANE_TYPE_BUTTON = 4


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


def _run(probe, *args, lock_dir=None):
    """Run the probe with an isolated HOME (so no real calibration file is
    read) and an isolated lock/mark path (so a test can never disturb the
    real one, nor a real session a test)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, HOME=tmp)
        env.pop("SANE_DEBUG_GENESYS", None)
        env["OF135I_LOCK_FILE"] = str(Path(lock_dir or tmp) / "of135i.lock")
        # The mock never answers a poll, so every best-effort site would
        # otherwise burn its real budget -- the cold-start program alone
        # carries the driver's 1.5 s and 30 s waits at nineteen poll sites (ten
        # best-effort, nine fail-closed motor completions). The
        # cap only ever shortens a wait (sane/gl126_ops.h RunPolicy).
        env["OF135I_SANE_POLL_CAP_MS"] = "5"
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
    return out


# ------------------------------------------------------------ 1. options


def test_magazine_options_exist_only_for_gl126():
    """The two buttons and the read-only status line are declared for the
    135i and inactive on every other genesys chip -- no other model in
    this backend has a magazine to load."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_magazine_options_exist_only_for_gl126")

    r = _run(probe, "options")
    opts = r["options"]
    for name in ("load-film", "eject-film", "magazine"):
        assert name in opts and opts[name] != "MISSING", (name, opts)
        assert opts[name]["inactive"] == "0", (name, opts[name])
    assert int(opts["load-film"]["type"]) == SANE_TYPE_BUTTON, opts["load-film"]
    assert int(opts["eject-film"]["type"]) == SANE_TYPE_BUTTON, opts["eject-film"]
    assert int(opts["magazine"]["type"]) == SANE_TYPE_STRING, opts["magazine"]
    # The status line is read-only: SANE_CAP_SOFT_DETECT (0x04) set,
    # SANE_CAP_SOFT_SELECT (0x01) clear, so a frontend renders it as text
    # rather than offering to set it.
    cap = int(opts["magazine"]["cap"], 16)
    assert cap & 0x04, opts["magazine"]
    assert not (cap & 0x01), opts["magazine"]
    assert int(opts["magazine"]["size"]) >= 128, opts["magazine"]

    other = _run(probe, "options", GL124_DEVICE)
    for name in ("load-film", "eject-film", "magazine"):
        assert other["options"][name]["inactive"] == "1", (name, other["options"][name])

    print("test_magazine_options_exist_only_for_gl126 OK "
          "(3 options active on GL126, all inactive on GL124)")


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
    assert len(values) >= 5, values
    for v in values:
        assert len(v) <= 40, (len(v), v)          # fits the widget
        assert v[0].islower(), v                   # state word leads
        assert " -- " in v or v.startswith("reseat"), v
    # The states an operator must be able to tell apart, each present.
    joined = " | ".join(values)
    for word in ("unknown", "released", "loaded", "ejected", "failed"):
        assert word in joined, (word, values)
    print(f"test_the_status_line_is_readable_and_comes_first OK "
          f"({len(values)} values, longest {max(len(v) for v in values)} chars, "
          f"index {opts['magazine']['index']} before the buttons)")


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
    assert r["text"].startswith("reseat"), r["text"]
    assert r["mark"].startswith("present"), r["mark"]
    # And with no mark at all it still says unknown, not a false pending.
    r2 = _run(probe, "scenario", "state-initial")
    assert r2["text"].startswith("unknown"), r2["text"]
    print("test_the_status_line_reports_a_load_pending_from_another_process OK")


def test_a_scan_after_eject_runs_open_then_load():
    """Section 10 (2026-09-25): an eject is no longer a dead end for the
    next scan. The vendor's own between-strip load (docs/protocol-notes.md
    Pass 14 addendum 4) just swaps the strip, pushes it to the stop, and
    loads -- no jog, no reinsert prompt -- so the backend now does the
    same: ejected is a PENDING next-strip load, of the Ejected kind.

    That kind replays the device-open table first (nothing has written it
    since the eject), then the bare load. On the test interface "open"
    reaches the wire and stops at its first unacknowledged write -- the
    same proof-of-reaching-the-wire the release path's "open" run gives
    (test_release_from_idle_runs_the_open_and_jog_programs) -- which fails
    the session closed exactly as every other magazine-sequence failure
    does: nothing further written, no recovery, mark dropped."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_scan_after_eject_runs_open_then_load")

    r = _run(probe, "scenario", "load-after-eject")
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, load = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert load[0] == SANE_STATUS_IO_ERROR, load
    assert "magazine open sequence" in load[1], load[1]
    assert "register write not acknowledged" in load[1], load[1]
    # Failed closed like every other magazine-sequence failure: the
    # pending load is gone and the status line says so.
    assert r["mark"] == "absent", r["mark"]
    assert r["text"].startswith("failed"), r["text"]
    print("test_a_scan_after_eject_runs_open_then_load OK "
          "(open reached the wire, failed closed)")


def test_a_scan_after_eject_without_a_magazine_refuses_and_keeps_the_mark():
    """The strip has not been pushed back in yet (or was never taken out)
    -- the loader sensor still reads clear. Read-only NO_DOCS, worded for
    a strip swap rather than the Released kind's reseat wording, and the
    mark is KEPT: pushing the magazine in and scanning again is the whole
    fix, exactly as it is for the Released kind's equivalent refusal."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_scan_after_eject_without_a_magazine_refuses_and_keeps_the_mark")

    r = _run(probe, "scenario", "load-after-eject-no-magazine")
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, load = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert load[0] == SANE_STATUS_NO_DOCS, load
    assert "no magazine in the slot" in load[1], load[1]
    assert "mechanical stop" in load[1], load[1]
    assert "Nothing was written" in load[1], load[1]
    assert r["mark"].startswith("present"), r["mark"]
    assert r["text"].startswith("ejected"), r["text"]
    print("test_a_scan_after_eject_without_a_magazine_refuses_and_keeps_the_mark OK "
          "(NO_DOCS, mark kept)")


def test_a_scan_after_eject_on_a_cold_scanner_refuses_and_clears_the_mark():
    """A power cycle happened between the eject and the next scan. A
    next-strip load assumes the transport is still homed and positioned
    from the same power-on -- of135i/loadflow.py's --next-strip makes
    exactly this assumption and refuses the same way (NEXT_STRIP_COLD_MSG).
    Nothing was written (this is a register read), so the mark is cleared
    as stale but the session is NOT failed: state drops to Unknown, and
    the documented way out (Load film's jog, or a fresh `of135i load`)
    remains available -- unlike the wrong-state refusal below it in
    gl126.cpp, which does fail the session."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_scan_after_eject_on_a_cold_scanner_refuses_and_clears_the_mark")

    r = _run(probe, "scenario", "load-after-eject-cold")
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, load = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert load[0] == SANE_STATUS_INVAL, load
    assert "power-cycled after the eject" in load[1], load[1]
    assert "Load film" in load[1], load[1]
    assert "same power-on" in load[1], load[1]
    assert "Nothing was written" in load[1], load[1]
    assert r["mark"] == "absent", r["mark"]
    # Unknown, not failed: nothing was written, so the session is not
    # terminal the way a real motor-sequence failure is.
    assert r["text"].startswith("unknown"), r["text"]
    print("test_a_scan_after_eject_on_a_cold_scanner_refuses_and_clears_the_mark OK "
          "(INVAL, mark cleared, session not failed)")


def test_an_ejected_mark_from_another_process_runs_open_then_load():
    """The cross-process case the Released mark was built for in the
    first place: `scanimage --eject-film=yes` in one process, a plain
    scan in the next. Two SEPARATE probe invocations sharing one
    OF135I_LOCK_FILE -- the first writes an Ejected mark and exits (no
    in-process state survives that), the second knows nothing except what
    it reads from disk.

    This also proves the mark's device key round-trips a name WITH SPACES
    for the Ejected kind specifically (the backend's own test-mode device
    is named "test device:0x07b3:0x1436"): if the space truncated the key
    on either write or read, the second run's key would not match its own
    device and the load would never be attempted at all -- it would
    return GOOD with nothing touched, not reach "open". (The Released
    kind's equivalent round trip is covered by tests/test_sane_lock.py's
    test_magazine_mark_round_trip, unaffected by this change.)"""
    probe = _build_probe()
    if probe is None:
        return _skip("test_an_ejected_mark_from_another_process_runs_open_then_load")

    with tempfile.TemporaryDirectory() as lock_dir:
        r1 = _run(probe, "scenario", "state-mark-ejected-pending", lock_dir=lock_dir)
        assert r1["mark"].startswith("present ejected"), r1["mark"]
        assert " " in r1["key"], r1["key"]   # the test device's name has a space

        r2 = _run(probe, "scenario", "load-mark-ejected-crossproc", lock_dir=lock_dir)
    assert r2["key"] == r1["key"], (r1["key"], r2["key"])
    status, msg = r2["statuses"][0]
    assert status == SANE_STATUS_IO_ERROR, r2["statuses"]
    assert "magazine open sequence" in msg, msg
    assert r2["mark"] == "absent", r2["mark"]
    print("test_an_ejected_mark_from_another_process_runs_open_then_load OK "
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


def test_load_film_from_ejected_still_runs_the_open_and_jog_programs():
    """An eject does not narrow what Load film can do -- only what a
    plain scan can skip. Pressed again after an eject, the full release
    path (cold-init-if-needed, device-open table, jog) still runs exactly
    as it does from any other non-Failed state; this is the fallback the
    cold-power-cycle refusal above points the operator to."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_load_film_from_ejected_still_runs_the_open_and_jog_programs")

    r = _run(probe, "scenario", "release-after-eject")
    assert len(r["statuses"]) == 2, r["statuses"]
    eject, release = r["statuses"]
    assert eject[0] == SANE_STATUS_GOOD, eject
    assert release[0] == SANE_STATUS_IO_ERROR, release
    assert "magazine open sequence" in release[1], release[1]
    assert r["mark"] == "absent", r["mark"]
    assert r["text"].startswith("failed"), r["text"]
    print("test_load_film_from_ejected_still_runs_the_open_and_jog_programs OK "
          "(Load film unaffected by a prior eject)")


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
    assert r["text"].startswith("unknown"), r["text"]
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
    assert r["text"].startswith("unknown"), r["text"]
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
    # status line says so instead of inviting another press.
    assert r["text"].startswith("failed"), r["text"]
    assert r["mark"] == "absent", r["mark"]
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
    assert r["mark"] == "absent", r["mark"]
    print("test_release_from_cold_stops_at_the_first_motor_completion OK "
          f"(PollTimeout at op {first}, the first of nine; failed, no mark)")


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
        assert r["mark"] == "absent", (scenario, r["mark"])
    print("test_a_usb_failure_mid_sequence_fails_the_session OK "
          "(release and eject: failed, mark dropped)")


def test_an_impossible_scan_request_never_moves_the_magazine():
    """The load half runs BEFORE calibration -- the core calls
    load_document() first -- while the scan request used to be validated
    inside offset_calibration(). With a release pending, an impossible
    request would therefore have driven the loader and only then been
    refused (Astra review 2026-09-13).

    Now the same write-free validation runs first. The refusal must leave
    the mark intact (the request is wrong, the magazine is not) and must
    NOT fail the session -- nothing was written, so nothing is unknown."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_an_impossible_scan_request_never_moves_the_magazine")

    r = _run(probe, "scenario", "load-mark-invalid-request")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_INVAL, r["statuses"]
    assert "outside 1-6" in msg, msg
    assert "Nothing was written" in msg, msg
    # A refusal, not a failure: the session stays usable and the pending
    # load survives, so scanning a valid frame completes it.
    assert not r["text"].startswith("failed"), r["text"]
    assert r["mark"].startswith("present"), r["mark"]
    print("test_an_impossible_scan_request_never_moves_the_magazine OK "
          "(INVAL before any write, mark kept, session not failed)")


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
    """The two buttons really are wired to the magazine hooks: pressed
    from a start state nobody can name, each returns the refusal only
    those hooks produce. (The text itself is asserted on the direct calls
    -- the backend's public entry points wrap every exception into a bare
    status code, so the message never escapes the library.)"""
    probe = _build_probe()
    if probe is None:
        return _skip("test_the_option_handlers_reach_the_hooks")

    for scenario in ("wiring-load", "wiring-eject"):
        r = _run(probe, "scenario", scenario)
        assert r["optstatuses"] == [SANE_STATUS_INVAL], (scenario, r["optstatuses"])
    print("test_the_option_handlers_reach_the_hooks OK (load-film and eject-film)")


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
    assert r["text"].startswith("unknown"), r["text"]

    r2 = _run(probe, "scenario", "start-no-mark")
    assert r2["progress"] == "offset_calibration", r2
    assert r2["text"].startswith("unknown"), r2["text"]
    print("test_a_scan_with_no_release_pending_never_touches_the_magazine OK "
          "(hook returns without reading, calibration entered)")


def test_a_scan_after_a_failed_magazine_sequence_refuses():
    """A failed release clears the mark, so "is a load pending?" would
    answer no and let the scan through to calibrate on top of a transport
    state nobody can name. The load half therefore checks the FAILED
    state first, before it asks about the mark at all."""
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


def test_a_pending_load_without_a_magazine_refuses_and_keeps_the_mark():
    """The operator pressed Load film, took the magazine out, and scanned
    before putting it back. Refuse read-only with NO_DOCS -- and KEEP the
    mark, because inserting the magazine and scanning again is the whole
    fix; losing it would force another release.

    Also checked through sane_start, which is where this really happens:
    the refusal must land BEFORE calibration, so nothing is written."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_pending_load_without_a_magazine_refuses_and_keeps_the_mark")

    r = _run(probe, "scenario", "load-mark-no-magazine")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_NO_DOCS, r["statuses"]
    assert "no magazine in the slot" in msg, msg
    assert "mechanical stop" in msg, msg
    assert "Nothing was written" in msg, msg
    assert r["mark"].startswith("present"), r["mark"]

    r2 = _run(probe, "scenario", "start-mark-no-magazine")
    assert r2["start"][0] == SANE_STATUS_NO_DOCS, r2["start"]
    assert r2["progress"] != "offset_calibration", r2
    assert r2["mark"].startswith("present"), r2["mark"]
    print("test_a_pending_load_without_a_magazine_refuses_and_keeps_the_mark OK "
          "(NO_DOCS before calibration, mark kept)")


def test_a_pending_load_from_the_wrong_state_refuses_and_drops_the_mark():
    """The mark says a release is pending but the scanner is not in the
    state the jog leaves behind. A load is not attempted from an
    unverified state: refuse, drop the mark so nothing retries by itself,
    and send the operator through the power cycle."""
    probe = _build_probe()
    if probe is None:
        return _skip("test_a_pending_load_from_the_wrong_state_refuses_and_drops_the_mark")

    r = _run(probe, "scenario", "load-mark-bad-state")
    status, msg = r["statuses"][0]
    assert status == SANE_STATUS_INVAL, r["statuses"]
    assert "not in the state the jog leaves it in" in msg, msg
    assert "Nothing was written" in msg, msg
    assert r["mark"] == "absent", r["mark"]
    assert r["text"].startswith("failed"), r["text"]
    print("test_a_pending_load_from_the_wrong_state_refuses_and_drops_the_mark OK")


def main() -> int:
    tests = [
        test_magazine_options_exist_only_for_gl126,
        test_magazine_text_starts_unknown,
        test_the_status_line_is_readable_and_comes_first,
        test_the_status_line_reports_a_load_pending_from_another_process,
        test_a_scan_after_eject_runs_open_then_load,
        test_a_scan_after_eject_without_a_magazine_refuses_and_keeps_the_mark,
        test_a_scan_after_eject_on_a_cold_scanner_refuses_and_clears_the_mark,
        test_an_ejected_mark_from_another_process_runs_open_then_load,
        test_an_ejected_mark_for_another_device_is_ignored_and_cleared,
        test_load_film_from_ejected_still_runs_the_open_and_jog_programs,
        test_release_refuses_an_unknown_start_state,
        test_release_from_idle_runs_the_open_and_jog_programs,
        test_release_from_cold_stops_at_the_first_motor_completion,
        test_a_failed_sequence_is_terminal,
        test_a_usb_failure_mid_sequence_fails_the_session,
        test_an_impossible_scan_request_never_moves_the_magazine,
        test_eject_refuses_from_cold_and_sends_you_to_load_film,
        test_eject_with_no_magazine_does_nothing,
        test_eject_refuses_the_base_table_state,
        test_the_option_handlers_reach_the_hooks,
        test_a_scan_with_no_release_pending_never_touches_the_magazine,
        test_a_scan_after_a_failed_magazine_sequence_refuses,
        test_a_mark_for_another_device_is_ignored_and_cleared,
        test_a_pending_load_without_a_magazine_refuses_and_keeps_the_mark,
        test_a_pending_load_from_the_wrong_state_refuses_and_drops_the_mark,
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
