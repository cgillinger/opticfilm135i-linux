#!/usr/bin/env python3
"""Offline tests for the GL126 SANE backend's op-program runner (hook 2).

sane/gl126_ops.{h,cpp} executes the op programs tools/gen_sane_tables.py
generates into sane/gl126_tables.{h,cpp} for the prep/afe_base/
cal_dark_a/cal_dark_b phases (docs/sane-hook2-offset.md, sections 3 and
6): the whole wire sequence hook 2 (offset calibration) needs, with
transfer boundaries and interleaving kept exactly as captured. These
tests check it without building the full SANE backend or touching
hardware: gl126_ops.cpp is compiled standalone (no genesys headers)
together with sane/gl126_tables.cpp and a tiny probe program,
tests/gl126_ops_probe.cpp (see its file comment for the script format).

  1. test_programs_match_python_replayer -- the plan's wire-equality
     test: the Python driver (of135i.device.Scanner) runs the same four
     phases over the existing FakeUsbDevice (tests/test_safety.py), and
     its recorded host->device transfers are compared, element by
     element, against the C++ runner's own transfer log for the same
     profile/phase, driven by a Wire fake that defaults every reply to
     the OpProgram's own captured value.
  2-6. the wait policy and the three failure rules (docs/sane-hook2-
     offset.md section 3), each checked for its outcome and for "no
     transfer after the failure".
  7-8. the S5/S6 computation (offset_codes/dark_is_residual), against
     the same reference vectors as tests/test_calibrate.py and cross-
     checked against of135i.calibrate at runtime.

Run with:
    .venv/bin/python tests/test_sane_ops.py

Requires a C++ compiler (g++) on PATH; if none is found, every test that
needs the probe prints a SKIP line and passes trivially.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from of135i import calibrate  # noqa: E402
from of135i.device import Scanner  # noqa: E402
from of135i.usbio import UsbIo  # noqa: E402

from test_safety import FakeUsbDevice, fast_time  # noqa: E402
from test_calibrate import _build_cal_buffers  # noqa: E402

SANE_DIR = REPO / "sane"
TESTS_DIR = Path(__file__).resolve().parent
PROBE_SRC = TESTS_DIR / "gl126_ops_probe.cpp"

_probe_bin: str | None = None
_build_attempted = False

PHASE_NAMES = ("prep", "afe_base", "cal_dark_a", "cal_dark_b")


def _build_probe() -> str | None:
    """Compile gl126_ops.cpp + gl126_tables.cpp + the probe once; return
    the probe binary path, or None (having printed a SKIP line, once) if
    no g++ is on PATH."""
    global _probe_bin, _build_attempted
    if _build_attempted:
        return _probe_bin
    _build_attempted = True

    gxx = shutil.which("g++")
    if gxx is None:
        print("SKIP: g++ not found on PATH -- test_sane_ops.py needs a C++ "
              "compiler to build sane/gl126_ops.cpp standalone")
        return None

    tmpdir = tempfile.mkdtemp(prefix="gl126-ops-test-")
    binary = str(Path(tmpdir) / "probe")
    cmd = [gxx, "-std=c++11", "-Wall", "-Wextra", "-Werror",
           str(SANE_DIR / "gl126_ops.cpp"), str(SANE_DIR / "gl126_tables.cpp"),
           str(PROBE_SRC), "-I", str(SANE_DIR), "-o", binary]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"failed to build the gl126_ops probe:\n"
            f"{' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    _probe_bin = binary
    return _probe_bin


def _run_probe_program(probe: str, profile: str, phase: str,
                       script_lines: list[str] | None = None):
    with tempfile.TemporaryDirectory() as td:
        script_path = str(Path(td) / "script.txt")
        Path(script_path).write_text("\n".join(script_lines or []) + "\n")
        r = subprocess.run([probe, "run", profile, phase, script_path],
                            capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def _parse_probe_transfers(text: str) -> list[tuple]:
    """Parse a probe `run` transfer log (everything except the trailing
    DONE/FAIL line) into (kind, ...) tuples comparable against the
    Python side -- see _python_transfers below."""
    out: list[tuple] = []
    for line in _lines(text):
        if line.startswith("DONE") or line.startswith("FAIL"):
            continue
        parts = line.split()
        kind = parts[0]
        kv = dict(p.split("=", 1) for p in parts[1:])
        if kind == "W":
            out.append(("W", int(kv["req"], 16), int(kv["val"], 16),
                       int(kv["idx"], 16), bytes.fromhex(kv["data"])))
        elif kind == "R":
            out.append(("R", int(kv["req"], 16), int(kv["val"], 16),
                       int(kv["idx"], 16), int(kv["len"])))
        elif kind == "B":
            out.append(("B", int(kv["len"])))
        else:
            raise AssertionError(f"unrecognised probe transfer line: {line!r}")
    return out


def _python_transfers(entries: list[dict]) -> list[tuple]:
    """Map a slice of FakeUsbDevice.wire_log to the same tuple shape
    _parse_probe_transfers produces, so the two logs compare directly."""
    out: list[tuple] = []
    for e in entries:
        if e["t"] == "cw":
            out.append(("W", e["br"], e["wv"], e["wi"], bytes(e["data"])))
        elif e["t"] == "cr":
            out.append(("R", e["br"], e["wv"], e["wi"], e["length"]))
        elif e["t"] == "bi":
            out.append(("B", e["length"]))
        elif e["t"] == "bo":
            raise AssertionError(
                "unexpected bulk OUT recorded in an op-program phase")
        else:
            raise AssertionError(f"unknown wire_log entry kind: {e!r}")
    return out


# ---------------------------------------------------- 1. wire equality


def test_programs_match_python_replayer():
    probe = _build_probe()
    if probe is None:
        print("test_programs_match_python_replayer SKIPPED (no g++)")
        return "skipped"

    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_build_cal_buffers())
    scanner = Scanner(UsbIo(fake))

    # Wrap _run_phase (not _exec_ops -- this goes through the same
    # guarded public methods every other test uses, per test_calibrate.py's
    # own `h.scanner._run_phase = ...` pattern) to record the wire_log
    # slice each phase run covers, without altering its behaviour at all.
    slices: dict[str, list[tuple[int, int]]] = {}
    orig_run_phase = scanner._run_phase

    def wrapped(phase, *a, **kw):
        start = len(fake.wire_log)
        result = orig_run_phase(phase, *a, **kw)
        end = len(fake.wire_log)
        slices.setdefault(phase.name, []).append((start, end))
        return result

    scanner._run_phase = wrapped  # type: ignore[method-assign]

    with fast_time():
        scanner.initialize()          # runs "prep" then "afe_base"
        scanner.scan(frame=1)         # runs "cal_dark_a"/"cal_dark_b" first

    total = 0
    for phase_name in PHASE_NAMES:
        assert phase_name in slices, (phase_name, sorted(slices))
        start, end = slices[phase_name][0]
        py_transfers = _python_transfers(fake.wire_log[start:end])

        rc, out, err = _run_probe_program(probe, "plain3600", phase_name)
        assert rc == 0, (phase_name, out, err)
        assert _lines(out)[-1].startswith("DONE"), (phase_name, out)
        cpp_transfers = _parse_probe_transfers(out)

        assert py_transfers == cpp_transfers, (
            f"{phase_name}: python and C++ transfer logs differ\n"
            f"python ({len(py_transfers)}): {py_transfers}\n"
            f"cpp    ({len(cpp_transfers)}): {cpp_transfers}")
        total += len(py_transfers)

    print(f"test_programs_match_python_replayer OK "
          f"({total} transfers across {len(PHASE_NAMES)} phases)")


# --------------------------------------------------- 2-6. wait/failures


def test_poll_waits_then_continues():
    probe = _build_probe()
    if probe is None:
        print("test_poll_waits_then_continues SKIPPED (no g++)")
        return "skipped"

    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_dark_a", ["poll 9c55,9c55,bd55"])
    assert rc == 0, (out, err)
    lines = _lines(out)
    assert lines[-1] == "DONE ops=29", lines[-1]
    # The W1 site (wv=018e, wi=0122) is read 3 times while polling, then
    # once more later in the phase as a plain (non-polling) status read.
    poll_site = [ln for ln in lines if ln.startswith("R req=04 val=018e idx=0122")]
    assert len(poll_site) == 4, poll_site
    print("test_poll_waits_then_continues OK")


def test_poll_timeout_fails_closed():
    probe = _build_probe()
    if probe is None:
        print("test_poll_timeout_fails_closed SKIPPED (no g++)")
        return "skipped"

    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_dark_a", ["poll 9c55"])
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert lines[-1].startswith("FAIL PollTimeout op=16 "), lines[-1]
    # Nothing after the failure but the repeated (never-settling) poll read.
    assert lines[-2] == "R req=04 val=018e idx=0122 len=2", lines[-2]
    print(f"test_poll_timeout_fails_closed OK ({lines[-1]})")


def test_bad_ack_fails_closed():
    probe = _build_probe()
    if probe is None:
        print("test_bad_ack_fails_closed SKIPPED (no g++)")
        return "skipped"

    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_dark_a", ["ack_at 0 00"])
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert lines[-1].startswith("FAIL BadAck op=1 "), lines[-1]
    assert len(lines) == 3, lines   # one write, one (bad) ack read, FAIL
    assert lines[0].startswith("W "), lines
    assert lines[1] == "R req=0c val=008e idx=0020 len=1", lines
    print("test_bad_ack_fails_closed OK")


def test_short_bulk_fails_closed():
    probe = _build_probe()
    if probe is None:
        print("test_short_bulk_fails_closed SKIPPED (no g++)")
        return "skipped"

    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_dark_a", ["bulk_len 1024"])
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert lines[-1].startswith("FAIL ShortBulk op="), lines[-1]
    assert lines[-2] == "B len=1024", lines[-2]   # nothing sent after the short read
    print(f"test_short_bulk_fails_closed OK ({lines[-1]})")


def test_bulk_done_mismatch_is_logged_only():
    probe = _build_probe()
    if probe is None:
        print("test_bulk_done_mismatch_is_logged_only SKIPPED (no g++)")
        return "skipped"

    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_dark_a", ["bulkdone 00"])
    assert rc == 0, (out, err)
    lines = _lines(out)
    assert lines[-1] == "DONE ops=29", lines[-1]
    print("test_bulk_done_mismatch_is_logged_only OK")


# ------------------------------------------------------- 7-8. S5/S6


def _rgb16le_bytes(means, n: int = 512) -> tuple[np.ndarray, bytes]:
    arr = np.tile(np.array(means, dtype=np.uint16), (n, 1))
    return arr, arr.astype("<u2").tobytes()


def _parse_offset_output(text: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for line in _lines(text):
        kv = dict(p.split("=", 1) for p in line.split())
        ch = int(kv["ch"])
        out[ch] = {
            "mean_a": float(kv["mean_a"]),
            "mean_b": float(kv["mean_b"]),
            "slope": float(kv["slope"]),
            "code": int(kv["code"], 16),
            "fallback": int(kv["fallback"]),
        }
    return out


def test_offset_codes_reference_vectors():
    probe = _build_probe()
    if probe is None:
        print("test_offset_codes_reference_vectors SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        # Vector 1: the reference unit's own bracket (tests/test_calibrate.py
        # test_offset_codes_from_reference_bracket).
        means_a = [21411, 27770, 24897]
        means_b = [23644, 30052, 27174]
        arr_a, bytes_a = _rgb16le_bytes(means_a)
        arr_b, bytes_b = _rgb16le_bytes(means_b)
        a_path, b_path = Path(td) / "a1.bin", Path(td) / "b1.bin"
        a_path.write_bytes(bytes_a)
        b_path.write_bytes(bytes_b)

        r = subprocess.run([probe, "offset", str(a_path), str(b_path)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r
        parsed = _parse_offset_output(r.stdout)
        py_codes = calibrate.offset_codes(arr_a, arr_b)
        expected = (0x010B, 0x010A, 0x010B)
        for ch in range(3):
            assert parsed[ch]["code"] == py_codes[ch] == expected[ch], (
                ch, parsed[ch], py_codes)
            assert parsed[ch]["fallback"] == 0, parsed[ch]

        # Vector 2: all-zero -> slope-fallback path, same defaults.
        arr_a0 = np.zeros((512, 3), dtype=np.uint16)
        arr_b0 = np.zeros((512, 3), dtype=np.uint16)
        a0_path, b0_path = Path(td) / "a2.bin", Path(td) / "b2.bin"
        a0_path.write_bytes(arr_a0.astype("<u2").tobytes())
        b0_path.write_bytes(arr_b0.astype("<u2").tobytes())

        r0 = subprocess.run([probe, "offset", str(a0_path), str(b0_path)],
                            capture_output=True, text=True)
        assert r0.returncode == 0, r0
        parsed0 = _parse_offset_output(r0.stdout)
        py_codes0 = calibrate.offset_codes(arr_a0, arr_b0)
        for ch in range(3):
            assert parsed0[ch]["code"] == py_codes0[ch] == expected[ch], (
                ch, parsed0[ch], py_codes0)
            assert parsed0[ch]["fallback"] == 1, parsed0[ch]

        # Vector 3: synthetic doubled slope (tests/test_calibrate.py
        # test_offset_codes_adapts_to_different_slope) -- cross-checked
        # against of135i.calibrate at runtime, not just a fixed constant.
        doubled_b = [a + 2 * (b - a) for a, b in zip(means_a, means_b)]
        arr_a2, bytes_a2 = _rgb16le_bytes(means_a)
        arr_b2, bytes_b2 = _rgb16le_bytes(doubled_b)
        a2_path, b2_path = Path(td) / "a3.bin", Path(td) / "b3.bin"
        a2_path.write_bytes(bytes_a2)
        b2_path.write_bytes(bytes_b2)

        r2 = subprocess.run([probe, "offset", str(a2_path), str(b2_path)],
                            capture_output=True, text=True)
        assert r2.returncode == 0, r2
        parsed2 = _parse_offset_output(r2.stdout)
        py_codes2 = calibrate.offset_codes(arr_a2, arr_b2)
        for ch in range(3):
            assert parsed2[ch]["code"] == py_codes2[ch] == 261, (ch, parsed2[ch], py_codes2)
            assert parsed2[ch]["fallback"] == 0, parsed2[ch]

    print(f"test_offset_codes_reference_vectors OK "
          f"(ref={[hex(parsed[c]['code']) for c in range(3)]}, "
          f"fallback={[hex(parsed0[c]['code']) for c in range(3)]}, "
          f"doubled={[parsed2[c]['code'] for c in range(3)]})")


def test_dark_is_residual():
    probe = _build_probe()
    if probe is None:
        print("test_dark_is_residual SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        # 24 distinct values, repeated -- the Test 32 residual shape.
        block = np.arange(1000, 1000 + 24, dtype=np.uint16)
        residual = np.tile(block, 200)
        assert 1 < len(np.unique(residual)) < 32
        residual_path = Path(td) / "residual.bin"
        residual_path.write_bytes(residual.astype("<u2").tobytes())

        # Healthy noise: > 32 distinct values.
        rng = np.random.default_rng(0)
        healthy = rng.integers(20000, 26000, 4096, dtype=np.uint16)
        assert len(np.unique(healthy)) > 32
        healthy_path = Path(td) / "healthy.bin"
        healthy_path.write_bytes(healthy.astype("<u2").tobytes())

        # A single distinct value: owned by offset_codes()'s slope
        # fallback, not dark_is_residual (unique == 1).
        constant = np.full(1024, 7, dtype=np.uint16)
        constant_path = Path(td) / "constant.bin"
        constant_path.write_bytes(constant.astype("<u2").tobytes())

        cases = [
            (residual_path, residual, "RESIDUAL", True),
            (healthy_path, healthy, "NOT_RESIDUAL", False),
            (constant_path, constant, "NOT_RESIDUAL", False),
        ]
        for path, arr, want_line, want_py in cases:
            r = subprocess.run([probe, "residual", str(path)],
                               capture_output=True, text=True)
            assert r.returncode == 0, r
            assert r.stdout.strip() == want_line, (path, r.stdout)
            assert bool(calibrate.dark_is_residual(arr)) is want_py, (path, arr)

    print("test_dark_is_residual OK")


def main() -> int:
    tests = [
        test_programs_match_python_replayer,
        test_poll_waits_then_continues,
        test_poll_timeout_fails_closed,
        test_bad_ack_fails_closed,
        test_short_bulk_fails_closed,
        test_bulk_done_mismatch_is_logged_only,
        test_offset_codes_reference_vectors,
        test_dark_is_residual,
    ]
    passed = 0
    skipped = 0
    for t in tests:
        result = t()
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
