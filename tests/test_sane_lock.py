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

from of135i.safety import ProcessLock, ScannerBusyError  # noqa: E402

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


def main() -> int:
    tests = [
        test_driver_holding_lock_refuses_sane_open,
        test_sane_holding_lock_refuses_driver,
        test_lock_file_format_matches_driver,
        test_read_only_lock_file_still_locks,
        test_failed_second_open_keeps_first_sessions_lock,
        test_release_without_acquire_is_noop,
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
