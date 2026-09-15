#!/usr/bin/env python3
"""Offline tests for the GL126 SANE backend's process lock.

sane/gl126_lock.{h,cpp} implements the C++ side of the mutual-exclusion
convention shared with the driver's ProcessLock (of135i/safety.py):
same lock path, same non-blocking flock, same holder-line format. These
tests check both directions -- the driver holding the lock excludes the
backend, and the backend holding it excludes the driver -- without
building the full SANE backend or touching hardware. gl126_lock.cpp is
compiled standalone (no genesys headers) together with a tiny probe
program, tests/gl126_lock_probe.cpp.

Run with:
    .venv/bin/python tests/test_sane_lock.py

Requires a C++ compiler (g++) on PATH; if none is found, every test
prints a SKIP line and passes trivially.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from of135i.safety import ProcessLock, SafetyError, ScannerBusyError  # noqa: E402

SANE_DIR = REPO / "sane"
PROBE_SRC = Path(__file__).resolve().parent / "gl126_lock_probe.cpp"

_probe_bin: str | None = None
_build_attempted = False


def _build_probe() -> str | None:
    """Compile gl126_lock.cpp + the probe once; return the probe binary
    path, or None (having printed a SKIP line, once) if no g++ is on
    PATH."""
    global _probe_bin, _build_attempted
    if _build_attempted:
        return _probe_bin
    _build_attempted = True

    gxx = shutil.which("g++")
    if gxx is None:
        print("SKIP: g++ not found on PATH -- test_sane_lock.py needs a C++ "
              "compiler to build sane/gl126_lock.cpp standalone")
        return None

    tmpdir = tempfile.mkdtemp(prefix="gl126-lock-test-")
    binary = str(Path(tmpdir) / "probe")
    cmd = [gxx, "-std=c++11", "-Wall", "-Wextra", "-Werror",
           str(SANE_DIR / "gl126_lock.cpp"), str(PROBE_SRC),
           "-I", str(SANE_DIR), "-o", binary]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"failed to build the gl126_lock probe:\n"
            f"{' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    _probe_bin = binary
    return _probe_bin


def _probe_env(lock_path: str) -> dict:
    env = dict(os.environ)
    env["OF135I_LOCK_FILE"] = lock_path
    return env


def test_driver_holding_lock_refuses_sane_open():
    probe = _build_probe()
    if probe is None:
        print("test_driver_holding_lock_refuses_sane_open SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "of135i.lock")

        driver_lock = ProcessLock(path)
        driver_lock.acquire()
        try:
            r = subprocess.run([probe, "try"], capture_output=True, text=True,
                                env=_probe_env(path))
            assert r.returncode == 3, r
            assert r.stdout.startswith("BUSY"), r.stdout
            assert "pid " in r.stdout, r.stdout
        finally:
            driver_lock.release()

        r2 = subprocess.run([probe, "try"], capture_output=True, text=True,
                             env=_probe_env(path))
        assert r2.returncode == 0, r2
        assert r2.stdout.strip() == "ACQUIRED", r2.stdout
    print("test_driver_holding_lock_refuses_sane_open OK")


def test_sane_holding_lock_refuses_driver():
    probe = _build_probe()
    if probe is None:
        print("test_sane_holding_lock_refuses_driver SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "of135i.lock")

        holder = subprocess.Popen([probe, "hold"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True,
                                   env=_probe_env(path))
        try:
            line = holder.stdout.readline()
            assert line.strip() == "HELD", line

            error = None
            try:
                ProcessLock(path).acquire()
            except ScannerBusyError as exc:
                error = exc
            assert error is not None, "expected ScannerBusyError while the backend holds the lock"
            assert "sane genesys gl126" in str(error), str(error)
        finally:
            holder.stdin.close()
            rest = holder.stdout.read()
            holder.wait(timeout=5)
            assert rest.strip() == "RELEASED", rest

        # Backend released it: the driver can now take it.
        lock = ProcessLock(path)
        lock.acquire()
        assert lock.held
        lock.release()
    print("test_sane_holding_lock_refuses_driver OK")


def test_lock_file_format_matches_driver():
    probe = _build_probe()
    if probe is None:
        print("test_lock_file_format_matches_driver SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "of135i.lock")
        r = subprocess.run([probe, "try"], capture_output=True, text=True,
                            env=_probe_env(path))
        assert r.returncode == 0, r

        content = Path(path).read_text().strip()
        pattern = (r"^pid \d+ since \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 "
                   r"\(sane genesys gl126\)$")
        assert re.match(pattern, content), repr(content)
    print("test_lock_file_format_matches_driver OK")


def test_read_only_lock_file_still_locks():
    if os.geteuid() == 0:
        print("test_read_only_lock_file_still_locks SKIPPED (running as root, "
              "chmod 0444 does not restrict root)")
        return "skipped"

    probe = _build_probe()
    if probe is None:
        print("test_read_only_lock_file_still_locks SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "of135i.lock")
        Path(path).touch()
        os.chmod(path, 0o444)

        holder = subprocess.Popen([probe, "hold"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True,
                                   env=_probe_env(path))
        try:
            line = holder.stdout.readline()
            assert line.strip() == "HELD", line  # read-only fallback still locks

            error = None
            try:
                ProcessLock(path).acquire()
            except ScannerBusyError as exc:
                error = exc
            assert error is not None, "a read-only lock file did not exclude the driver"
        finally:
            holder.stdin.close()
            rest = holder.stdout.read()
            holder.wait(timeout=5)
            assert rest.strip() == "RELEASED", rest
    print("test_read_only_lock_file_still_locks OK")


def test_failed_second_open_keeps_first_sessions_lock():
    """A failed second GL126 open must not release the first session's
    lock (external review, 2026-09-08): probe nested models a process
    that already holds the lock (A) taking a second reference (B, a
    second open attempt) and releasing only B's reference when B fails.
    The lock must still refuse a third party (the driver) until A itself
    releases."""
    probe = _build_probe()
    if probe is None:
        print("test_failed_second_open_keeps_first_sessions_lock SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "of135i.lock")

        holder = subprocess.Popen([probe, "nested"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True,
                                   env=_probe_env(path))
        try:
            line_a = holder.stdout.readline()
            assert line_a.strip() == "A_HELD refs=1", line_a

            line_b_acquired = holder.stdout.readline()
            assert line_b_acquired.strip() == "B_ACQUIRED refs=2", line_b_acquired

            line_b_released = holder.stdout.readline()
            assert line_b_released.strip() == "B_RELEASED refs=1 held=1", line_b_released

            # B's failure released only its own reference -- A's session
            # must still exclude the driver.
            error = None
            try:
                ProcessLock(path).acquire()
            except ScannerBusyError as exc:
                error = exc
            assert error is not None, (
                "driver acquired the lock while probe session A still held a reference")
        finally:
            holder.stdin.close()
            rest = holder.stdout.read()
            holder.wait(timeout=5)
            assert rest.strip() == "A_RELEASED refs=0 held=0", rest

        # A released for good: the driver can now take it.
        lock = ProcessLock(path)
        lock.acquire()
        assert lock.held
        lock.release()
    print("test_failed_second_open_keeps_first_sessions_lock OK")


def test_release_without_acquire_is_noop():
    probe = _build_probe()
    if probe is None:
        print("test_release_without_acquire_is_noop SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "of135i.lock")
        r = subprocess.run([probe, "release-unheld"], capture_output=True, text=True,
                            env=_probe_env(path))
        assert r.returncode == 0, r
        assert r.stdout.strip() == "OK", r.stdout
    print("test_release_without_acquire_is_noop OK")



def test_magazine_mark_round_trip():
    """docs/sane-wp4-magazine.md section 2.1: the "a release is pending"
    fact, written next to the process lock so a SECOND process can see
    it -- `scanimage` loads in two invocations (press Load film, reseat,
    scan) and each one is a new process.

    It is only ever a hint: the backend re-reads the hardware before the
    load, and a power cycle both re-enumerates the unit under a new
    address and leaves reg 0x01 cold. What this test pins is the file
    itself -- where it lives, that it round-trips the device key, that a
    foreign key comes back as written (so the caller can compare and
    ignore it), and that clearing really removes it."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_round_trip SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        lock_path = str(Path(td) / "of135i.lock")
        env = _probe_env(lock_path)

        def run(*args):
            return subprocess.run([probe, *args], capture_output=True,
                                  text=True, env=env)

        # The mark sits beside the lock, so one OF135I_LOCK_FILE setting
        # moves both -- a test, or a second host user, cannot collide
        # with the real one.
        r = run("mark-path")
        assert r.returncode == 0, r
        mark_path = r.stdout.strip()
        assert mark_path == lock_path + ".magazine", mark_path
        assert not Path(mark_path).exists(), "no mark before anything is written"

        # No mark: "NONE", non-zero exit -- this is the ordinary case on
        # every scan that is not completing a load.
        r = run("mark-read")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r

        r = run("mark-write", "libusb:001:007")
        assert r.returncode == 0 and r.stdout.strip() == "WROTE", r
        r = run("mark-read")
        assert r.returncode == 0 and r.stdout.strip() == "KEY libusb:001:007", r

        # Readable by a person: `cat` on it says what it is, like the
        # lock file does, with the device key on its own line -- a SANE
        # device name can contain spaces, so it is never a field in a
        # space-delimited line.
        lines = Path(mark_path).read_text().splitlines()
        assert lines[0].startswith("released "), lines
        assert lines[0].endswith("(sane genesys gl126)"), lines
        assert lines[1] == "libusb:001:007", lines

        # A key WITH spaces round-trips whole (the backend's own test mode
        # names its device "test device:0x07b3:0x1436").
        r = run("mark-write", "test device:0x07b3:0x1436")
        assert r.returncode == 0, r
        r = run("mark-read")
        assert r.stdout.strip() == "KEY test device:0x07b3:0x1436", r
        r = run("mark-write", "libusb:001:007")
        assert r.returncode == 0, r

        # Writing again replaces rather than appends: one unit, at most
        # one magazine waiting.
        r = run("mark-write", "libusb:001:009")
        assert r.returncode == 0, r
        r = run("mark-read")
        assert r.stdout.strip() == "KEY libusb:001:009", r
        assert Path(mark_path).read_text().count("released") == 1
        assert Path(mark_path).read_text().splitlines()[1] == "libusb:001:009"

        r = run("mark-clear")
        assert r.returncode == 0, r
        assert not Path(mark_path).exists()
        r = run("mark-read")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r

        # A truncated or foreign file is "no mark", not a crash.
        Path(mark_path).write_text("garbage\n")
        r = run("mark-read")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r

    print("test_magazine_mark_round_trip OK "
          "(path, round trip, replace, clear, malformed)")


def test_lock_path_symlink_is_refused():
    """A symlink planted at the well-known lock path must never be
    followed for locking or writing -- it could point anywhere this
    process can write. Both the C++ probe and the Python ProcessLock
    must refuse it, and the symlink's target must come out untouched."""
    probe = _build_probe()
    if probe is None:
        print("test_lock_path_symlink_is_refused SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        victim = Path(td) / "victim"
        victim.write_text("victim content\n")
        lock_path = Path(td) / "of135i.lock"
        lock_path.symlink_to(victim)

        r = subprocess.run([probe, "try"], capture_output=True, text=True,
                            env=_probe_env(str(lock_path)))
        assert r.returncode != 0, r
        assert str(lock_path) in r.stderr, r.stderr
        assert victim.read_text() == "victim content\n"

        error = None
        try:
            ProcessLock(str(lock_path)).acquire()
        except Exception as exc:  # not necessarily ScannerBusyError
            error = exc
        assert error is not None, "ProcessLock followed a symlink at the lock path"
        assert not isinstance(error, ScannerBusyError), (
            "a symlinked lock path is a file-handling refusal, not a busy lock")
        assert str(lock_path) in str(error), str(error)
        assert victim.read_text() == "victim content\n"
    print("test_lock_path_symlink_is_refused OK")


def test_lock_path_directory_is_refused():
    """A directory at the lock path (however it got there) must be
    refused cleanly, not crash either implementation."""
    probe = _build_probe()
    if probe is None:
        print("test_lock_path_directory_is_refused SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        lock_path = Path(td) / "of135i.lock"
        lock_path.mkdir()

        r = subprocess.run([probe, "try"], capture_output=True, text=True,
                            env=_probe_env(str(lock_path)))
        assert r.returncode != 0, r

        error = None
        try:
            ProcessLock(str(lock_path)).acquire()
        except Exception as exc:
            error = exc
        assert error is not None, "ProcessLock opened a directory as the lock file"
    print("test_lock_path_directory_is_refused OK")


def test_lock_path_hard_link_is_refused():
    """A hard link planted at the lock path -- not a symlink, so
    O_NOFOLLOW does not touch it, but the same directory entry as some
    other file this process can also see -- must never be locked or
    written through. Both the C++ probe and the Python ProcessLock must
    refuse it (st_nlink > 1), and the victim's content must come out
    byte-identical (no ftruncate(0)+write clobbering it)."""
    probe = _build_probe()
    if probe is None:
        print("test_lock_path_hard_link_is_refused SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        victim = Path(td) / "victim"
        victim.write_text("victim content\n")
        lock_path = Path(td) / "of135i.lock"
        os.link(str(victim), str(lock_path))

        r = subprocess.run([probe, "try"], capture_output=True, text=True,
                            env=_probe_env(str(lock_path)), timeout=10)
        assert r.returncode != 0, r
        assert str(lock_path) in r.stderr, r.stderr
        assert victim.read_text() == "victim content\n"

        error = None
        try:
            ProcessLock(str(lock_path)).acquire()
        except Exception as exc:  # not necessarily ScannerBusyError
            error = exc
        assert error is not None, "ProcessLock locked/wrote through a hard link at the lock path"
        assert not isinstance(error, ScannerBusyError), (
            "a hard-linked lock path is a file-handling refusal, not a busy lock")
        assert isinstance(error, SafetyError), type(error)
        assert str(lock_path) in str(error), str(error)
        assert victim.read_text() == "victim content\n"
    print("test_lock_path_hard_link_is_refused OK")


def test_magazine_mark_path_fifo_does_not_block_the_read():
    """A FIFO planted at the mark path must not hang magazine_mark_read()
    -- it is called before every load and must always return promptly.
    Before O_NONBLOCK this open() would block forever on a FIFO with no
    writer attached; a regression here must fail the test outright
    rather than hang the whole suite, hence the explicit subprocess
    timeout."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_path_fifo_does_not_block_the_read SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        lock_path = str(Path(td) / "of135i.lock")
        mark_path = Path(lock_path + ".magazine")
        os.mkfifo(str(mark_path))

        try:
            r = subprocess.run([probe, "mark-read"], capture_output=True, text=True,
                                env=_probe_env(lock_path), timeout=10)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "magazine_mark_read() blocked on a FIFO at the mark path -- "
                "the O_NONBLOCK open regressed")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r
    print("test_magazine_mark_path_fifo_does_not_block_the_read OK")


def test_magazine_mark_path_hard_link_is_refused():
    """A hard link planted at the mark path must read as "no mark" (like
    a symlink or FIFO there) and must never be written through -- the
    victim's content comes out unchanged."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_path_hard_link_is_refused SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        lock_path = str(Path(td) / "of135i.lock")
        victim = Path(td) / "victim"
        victim.write_text("victim content\n")
        mark_path = Path(lock_path + ".magazine")
        os.link(str(victim), str(mark_path))

        r = subprocess.run([probe, "mark-read"], capture_output=True, text=True,
                            env=_probe_env(lock_path), timeout=10)
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r
        assert victim.read_text() == "victim content\n"
    print("test_magazine_mark_path_hard_link_is_refused OK")


def test_magazine_mark_symlink_is_replaced_not_written_through():
    """A symlink planted at the mark path must never be written through
    in place: mark-write must either fail, or replace the symlink
    itself (via rename()) -- never touch what the symlink pointed at,
    and never leave a `.tmp.<pid>` file behind either way."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_symlink_is_replaced_not_written_through SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        lock_path = str(Path(td) / "of135i.lock")
        victim = Path(td) / "victim"
        victim.write_text("victim content\n")
        mark_path = Path(lock_path + ".magazine")
        mark_path.symlink_to(victim)

        r = subprocess.run([probe, "mark-write", "libusb:001:007"],
                            capture_output=True, text=True, env=_probe_env(lock_path))
        assert r.stdout.strip() in ("WROTE", "FAILED"), r.stdout

        # Whichever outcome: the symlink's old target is untouched, and no
        # temp file survives.
        assert victim.read_text() == "victim content\n"
        leftovers = [p.name for p in Path(td).iterdir() if ".tmp." in p.name]
        assert leftovers == [], leftovers

        if r.stdout.strip() == "WROTE":
            # Succeeded by replacing the symlink itself with a regular file.
            assert not mark_path.is_symlink(), "mark-write wrote through the symlink"
            r2 = subprocess.run([probe, "mark-read"], capture_output=True, text=True,
                                 env=_probe_env(lock_path))
            assert r2.stdout.strip() == "KEY libusb:001:007", r2.stdout
    print("test_magazine_mark_symlink_is_replaced_not_written_through OK")


def test_magazine_mark_empty_file_reads_as_no_mark():
    """An empty mark file (e.g. a crash mid-write, before the atomic
    rename existed) must read as "no mark", not a crash -- same rule as
    the truncated/garbage case already covered by
    test_magazine_mark_round_trip. Also checks that a normal write
    leaves no temp file behind."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_empty_file_reads_as_no_mark SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        lock_path = str(Path(td) / "of135i.lock")
        env = _probe_env(lock_path)

        r = subprocess.run([probe, "mark-write", "libusb:001:007"],
                            capture_output=True, text=True, env=env)
        assert r.stdout.strip() == "WROTE", r
        mark_path = Path(lock_path + ".magazine")
        assert mark_path.exists() and not mark_path.is_symlink()
        leftovers = [p.name for p in Path(td).iterdir() if ".tmp." in p.name]
        assert leftovers == [], leftovers

        mark_path.write_text("")
        r = subprocess.run([probe, "mark-read"], capture_output=True, text=True, env=env)
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r
    print("test_magazine_mark_empty_file_reads_as_no_mark OK")


def main() -> int:
    tests = [
        test_driver_holding_lock_refuses_sane_open,
        test_sane_holding_lock_refuses_driver,
        test_lock_file_format_matches_driver,
        test_read_only_lock_file_still_locks,
        test_failed_second_open_keeps_first_sessions_lock,
        test_release_without_acquire_is_noop,
        test_magazine_mark_round_trip,
        test_lock_path_symlink_is_refused,
        test_lock_path_directory_is_refused,
        test_lock_path_hard_link_is_refused,
        test_magazine_mark_path_fifo_does_not_block_the_read,
        test_magazine_mark_path_hard_link_is_refused,
        test_magazine_mark_symlink_is_replaced_not_written_through,
        test_magazine_mark_empty_file_reads_as_no_mark,
    ]
    passed = 0
    skipped = 0
    for t in tests:
        result = t()
        # Each test prints its own "SKIPPED (...)" line and returns the
        # string "skipped" in that case; a test that ran to completion
        # prints an "OK" line and returns None (the implicit return of a
        # plain `print(...)` as the last statement).
        if result == "skipped":
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
