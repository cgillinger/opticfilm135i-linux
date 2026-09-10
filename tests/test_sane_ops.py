#!/usr/bin/env python3
"""Offline tests for the GL126 SANE backend's op-program runner
(hooks 2, 3 and 4).

sane/gl126_ops.{h,cpp} executes the op programs tools/gen_sane_tables.py
generates into sane/gl126_tables.{h,cpp} for the prep/afe_base/
cal_dark_a/cal_dark_b phases (docs/sane-hook2-offset.md, sections 3 and
6, hook 2), the cal_white/cal_gain_check_a/cal_gain_check_b phases
(docs/sane-hook3-gain.md, sections 3, 4 and 6, hook 3) and the
cal_shading_measure/cal_shading_upload/cal_shading_verify/
cal_shading_verify_upload phases (docs/sane-hook4-shading.md, sections
3, 4 and 6, hook 4): the whole wire sequence offset, gain and shading
calibration need, with transfer boundaries and interleaving kept
exactly as captured. These tests check it without building the full
SANE backend or touching hardware: gl126_ops.cpp is compiled standalone
(no genesys headers) together with sane/gl126_tables.cpp and a tiny
probe program, tests/gl126_ops_probe.cpp (see its file comment for the
script format).

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
  9-14. hook 3 (docs/sane-hook3-gain.md section 6): the gain phases'
     wire equality (with the gain injection applied on both sides), the
     MissingInjection rule, a multi-chunk short bulk, gain_codes()/
     percentile_linear() against reference data and numpy, and the
     warmup retry policy.
  15-22. hook 4 (docs/sane-hook4-shading.md section 6): the shading
     phases' wire equality (four programs, byte and bulk injections
     applied on both sides, BulkOut payloads compared by digest), that
     none of the four programs' generated BulkOut ops carries the
     reference unit's captured chunk as `data` (checked structurally via
     the probe's `program_info` mode, no run_program() involved),
     PollClass wait/timeout, a short BulkOut, the two bulk-injection
     failure rules, and the shading computation (shading_table/
     shading_table2/shading_upload_len) against reference vectors and
     against of135i.calibrate byte for byte.

Run with:
    .venv/bin/python tests/test_sane_ops.py

Requires a C++ compiler (g++) on PATH; if none is found, every test that
needs the probe prints a SKIP line and passes trivially.
"""

from __future__ import annotations

import hashlib
from collections import deque
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from of135i import calibrate, tables  # noqa: E402
from of135i.device import Scanner  # noqa: E402
from of135i.usbio import UsbIo  # noqa: E402

from test_safety import FakeUsbDevice, fast_time  # noqa: E402
from test_calibrate import (  # noqa: E402
    _build_cal_buffers, _peak_for_gain_code, _parse_shading_blocks,
)

try:
    from test_calibrate import _WarmupHarness  # noqa: E402
except ImportError:   # pragma: no cover -- cross-check is best-effort
    _WarmupHarness = None

SANE_DIR = REPO / "sane"
TESTS_DIR = Path(__file__).resolve().parent
PROBE_SRC = TESTS_DIR / "gl126_ops_probe.cpp"
CAPTURE_DIR = REPO / "cal-data" / "capture"

_probe_bin: str | None = None
_build_attempted = False

PHASE_NAMES = ("prep", "afe_base", "cal_dark_a", "cal_dark_b")
GAIN_PHASE_NAMES = ("cal_white", "cal_gain_check_a", "cal_gain_check_b")


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
                       script_lines: list[str] | None = None,
                       injects: dict[str, int] | None = None,
                       bulk_injects: dict[str, bytes] | None = None):
    with tempfile.TemporaryDirectory() as td:
        script_path = str(Path(td) / "script.txt")
        Path(script_path).write_text("\n".join(script_lines or []) + "\n")
        cmd = [probe, "run", profile, phase, script_path]
        for name, val in (injects or {}).items():
            cmd += ["--inject", f"{name}=0x{val:02x}"]
        for name, data in (bulk_injects or {}).items():
            bulk_path = Path(td) / f"bulk_{name}.bin"
            bulk_path.write_bytes(data)
            cmd += ["--inject-bulk", f"{name}={bulk_path}"]
        r = subprocess.run(cmd, capture_output=True, text=True)
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
        elif kind == "BO":
            out.append(("BO", int(kv["len"]), kv["sha256"]))
        else:
            raise AssertionError(f"unrecognised probe transfer line: {line!r}")
    return out


def _python_transfers(entries: list[dict]) -> list[tuple]:
    """Map a slice of FakeUsbDevice.wire_log to the same tuple shape
    _parse_probe_transfers produces, so the two logs compare directly.
    A bulk OUT ('bo') compares by (length, sha256 digest) -- the probe's
    "BO len=<n> sha256=<hex>" line -- rather than the raw payload, which
    can be tens of KB (docs/sane-hook4-shading.md section 6, Part C)."""
    out: list[tuple] = []
    for e in entries:
        if e["t"] == "cw":
            out.append(("W", e["br"], e["wv"], e["wi"], bytes(e["data"])))
        elif e["t"] == "cr":
            out.append(("R", e["br"], e["wv"], e["wi"], e["length"]))
        elif e["t"] == "bi":
            out.append(("B", e["length"]))
        elif e["t"] == "bo":
            data = bytes(e["data"])
            out.append(("BO", len(data), hashlib.sha256(data).hexdigest()))
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


# ------------------------------------------------- 9-14. hook 3 (gain)


def test_gain_programs_match_python_replayer():
    """docs/sane-hook3-gain.md section 6, test 1: wire equality for
    cal_white/cal_gain_check_a/cal_gain_check_b, gain injections applied
    on both sides -- the Python driver's own computed codes (read back
    from the _run_phase kwargs it passes cal_gain_check_a) fed to the
    C++ runner via --inject."""
    probe = _build_probe()
    if probe is None:
        print("test_gain_programs_match_python_replayer SKIPPED (no g++)")
        return "skipped"

    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_build_cal_buffers())
    scanner = Scanner(UsbIo(fake))

    slices: dict[str, list[tuple[int, int]]] = {}
    gain_kwargs: dict[str, bytes] = {}
    orig_run_phase = scanner._run_phase

    def wrapped(phase, *a, **kw):
        start = len(fake.wire_log)
        result = orig_run_phase(phase, *a, **kw)
        end = len(fake.wire_log)
        slices.setdefault(phase.name, []).append((start, end))
        if phase.name == "cal_gain_check_a":
            gain_kwargs.update(kw)
        return result

    scanner._run_phase = wrapped  # type: ignore[method-assign]

    with fast_time():
        scanner.initialize()          # runs "prep" then "afe_base"
        scanner.scan(frame=1)         # runs the gain phases along the way

    assert set(("gain_r", "gain_g", "gain_b")) <= set(gain_kwargs), gain_kwargs
    injects = {name: gain_kwargs[name][0] for name in ("gain_r", "gain_g", "gain_b")}

    total = 0
    for phase_name in GAIN_PHASE_NAMES:
        assert phase_name in slices, (phase_name, sorted(slices))
        start, end = slices[phase_name][0]
        py_transfers = _python_transfers(fake.wire_log[start:end])

        this_injects = injects if phase_name == "cal_gain_check_a" else None
        rc, out, err = _run_probe_program(probe, "plain3600", phase_name,
                                          injects=this_injects)
        assert rc == 0, (phase_name, out, err)
        assert _lines(out)[-1].startswith("DONE"), (phase_name, out)
        cpp_transfers = _parse_probe_transfers(out)

        assert py_transfers == cpp_transfers, (
            f"{phase_name}: python and C++ transfer logs differ\n"
            f"python ({len(py_transfers)}): {py_transfers}\n"
            f"cpp    ({len(cpp_transfers)}): {cpp_transfers}")
        total += len(py_transfers)

    print(f"test_gain_programs_match_python_replayer OK "
          f"({total} transfers across {len(GAIN_PHASE_NAMES)} phases, "
          f"gain={[hex(v) for v in injects.values()]})")


def test_missing_injection_fails_before_any_transfer():
    """docs/sane-hook3-gain.md section 6, test 2: no --inject at all, and
    only two of three names, both fail MissingInjection with zero
    transfers logged (checked before any transfer, per gl126_ops.h)."""
    probe = _build_probe()
    if probe is None:
        print("test_missing_injection_fails_before_any_transfer SKIPPED (no g++)")
        return "skipped"

    rc, out, err = _run_probe_program(probe, "plain3600", "cal_gain_check_a")
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert len(lines) == 1, lines
    assert lines[0].startswith("FAIL MissingInjection "), lines
    assert lines[0].endswith("ops=0"), lines

    rc2, out2, err2 = _run_probe_program(
        probe, "plain3600", "cal_gain_check_a",
        injects={"gain_r": 0x2E, "gain_g": 0x21})
    assert rc2 == 1, (out2, err2)
    lines2 = _lines(out2)
    assert len(lines2) == 1, lines2
    assert lines2[0].startswith("FAIL MissingInjection "), lines2
    assert lines2[0].endswith("ops=0"), lines2

    print(f"test_missing_injection_fails_before_any_transfer OK "
          f"({lines[0]!r}, {lines2[0]!r})")


def test_multi_chunk_bulk_short_second_chunk():
    """docs/sane-hook3-gain.md section 6, test 3: cal_white's second
    BulkIn (of three) returns short -> ShortBulk after exactly two bulk
    transfers, nothing sent after."""
    probe = _build_probe()
    if probe is None:
        print("test_multi_chunk_bulk_short_second_chunk SKIPPED (no g++)")
        return "skipped"

    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_white", ["bulk_len_at 1 1000"])
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert lines[-1].startswith("FAIL ShortBulk op="), lines[-1]
    bulk_lines = [ln for ln in lines if ln.startswith("B len=")]
    assert bulk_lines == ["B len=16384", "B len=1000"], bulk_lines
    assert lines[-2] == "B len=1000", lines[-2]   # nothing sent after the short read
    print(f"test_multi_chunk_bulk_short_second_chunk OK ({lines[-1]})")


def test_gain_codes_reference_vectors():
    """docs/sane-hook3-gain.md section 4: the vendor's white line, a
    synthetic all-0x21 line, an all-zero line, and a one-channel-
    saturated line."""
    probe = _build_probe()
    if probe is None:
        print("test_gain_codes_reference_vectors SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        # Vector 1: the vendor's own capture.
        raw = (CAPTURE_DIR / "cal-frame00501-len31104.bin").read_bytes()
        white = np.frombuffer(raw, dtype="<u2").reshape(-1, 3)
        assert white.shape == (5184, 3), white.shape
        path1 = Path(td) / "v1.bin"
        path1.write_bytes(raw)
        r1 = subprocess.run([probe, "gain", str(path1)], capture_output=True, text=True)
        assert r1.returncode == 0, r1
        lines1 = _lines(r1.stdout)
        codes1 = [int(ln.split("code=")[1], 16) for ln in lines1[:3]]
        expected = (0x2E, 0x21, 0x29)
        for got, want, ch in zip(codes1, expected, "RGB"):
            assert abs(got - want) <= 1, (ch, got, want)
        py_codes1 = calibrate.gain_codes(white)
        assert tuple(codes1) == tuple(py_codes1), (codes1, py_codes1)
        assert lines1[3] == "saturated=0", lines1

        # Vector 2: every pixel at _peak_for_gain_code(0x21) -> (0x21,)*3.
        peak = _peak_for_gain_code(0x21)
        arr2 = np.full((5184, 3), peak, dtype=np.uint16)
        path2 = Path(td) / "v2.bin"
        path2.write_bytes(arr2.astype("<u2").tobytes())
        r2 = subprocess.run([probe, "gain", str(path2)], capture_output=True, text=True)
        assert r2.returncode == 0, r2
        lines2 = _lines(r2.stdout)
        codes2 = [int(ln.split("code=")[1], 16) for ln in lines2[:3]]
        assert codes2 == [0x21, 0x21, 0x21], codes2
        assert lines2[3] == "saturated=0", lines2

        # Vector 3: all-zero -> (63, 63, 63), not saturated.
        arr3 = np.zeros((5184, 3), dtype=np.uint16)
        path3 = Path(td) / "v3.bin"
        path3.write_bytes(arr3.astype("<u2").tobytes())
        r3 = subprocess.run([probe, "gain", str(path3)], capture_output=True, text=True)
        assert r3.returncode == 0, r3
        lines3 = _lines(r3.stdout)
        codes3 = [int(ln.split("code=")[1], 16) for ln in lines3[:3]]
        assert codes3 == [63, 63, 63], codes3
        assert lines3[3] == "saturated=0", lines3

        # Vector 4: one channel at 65535 -> saturated. The 99.9th
        # percentile needs the TOP ~0.1% of samples at full scale (a
        # single outlier sorts below it and never reaches the
        # percentile), so the last 16 of 5184 samples are set.
        arr4 = np.full((5184, 3), peak, dtype=np.uint16)
        arr4[-16:, 1] = 65535
        path4 = Path(td) / "v4.bin"
        path4.write_bytes(arr4.astype("<u2").tobytes())
        r4 = subprocess.run([probe, "gain", str(path4)], capture_output=True, text=True)
        assert r4.returncode == 0, r4
        lines4 = _lines(r4.stdout)
        assert lines4[3] == "saturated=1", lines4

    print(f"test_gain_codes_reference_vectors OK ({[hex(c) for c in codes1]})")


def test_percentile_matches_numpy():
    """docs/sane-hook3-gain.md section 4: percentile_linear() against
    numpy.percentile's default 'linear' method, seeded random arrays of
    several sizes and several q values."""
    probe = _build_probe()
    if probe is None:
        print("test_percentile_matches_numpy SKIPPED (no g++)")
        return "skipped"

    rng = np.random.default_rng(1234)
    sizes = (1, 2, 7, 5184)
    qs = (50.0, 99.9, 100.0, 0.0)

    with tempfile.TemporaryDirectory() as td:
        checked = 0
        for n in sizes:
            arr = rng.integers(0, 65536, n, dtype=np.uint16)
            path = Path(td) / f"n{n}.bin"
            path.write_bytes(arr.astype("<u2").tobytes())
            for q in qs:
                want = float(np.percentile(arr.astype(np.float64), q))
                r = subprocess.run([probe, "percentile", str(path), repr(q)],
                                   capture_output=True, text=True)
                assert r.returncode == 0, r
                got = float(r.stdout.strip().split("=")[1])
                assert abs(got - want) <= 1e-6, (n, q, got, want)
                checked += 1

    print(f"test_percentile_matches_numpy OK ({checked} (size, q) pairs)")


def test_warmup_policy():
    """docs/sane-hook3-gain.md section 6, test 5: the warmup retry
    policy against literal sequences (mirroring tests/test_calibrate.py's
    _WarmupHarness sequences, cross-checked against it directly when it
    can be imported)."""
    probe = _build_probe()
    if probe is None:
        print("test_warmup_policy SKIPPED (no g++)")
        return "skipped"

    def white_bytes(peak) -> bytes:
        arr = np.full((5184, 3), round(peak), dtype=np.uint16)
        return arr.astype("<u2").tobytes()

    def run_sequence(td, peaks) -> tuple[str, dict]:
        script_lines = []
        for i, pk in enumerate(peaks):
            p = Path(td) / f"m{i}.bin"
            p.write_bytes(white_bytes(pk))
            script_lines.append(str(p))
        script_path = Path(td) / "warmup_script.txt"
        script_path.write_text("\n".join(script_lines) + "\n")
        r = subprocess.run([probe, "warmup", str(script_path)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r
        lines = _lines(r.stdout)
        outcome_line = lines[-1]
        assert outcome_line.startswith("OUTCOME "), lines
        tokens = outcome_line.split()
        outcome = tokens[1]
        kv = dict(p.split("=", 1) for p in tokens[2:])
        return outcome, kv

    good = _peak_for_gain_code(0x21)
    jump = good * 1.05

    with tempfile.TemporaryDirectory() as td:
        # 1. [p(0x21)] -> Ready after 1.
        outcome, kv = run_sequence(td, [good])
        assert outcome == "Ready", (outcome, kv)
        assert kv["attempts"] == "1", kv
        assert kv["codes"] == "21,21,21", kv

        # 2. [0, 0, p(0x21), p(0x21)] -> Ready after 4, elapsed 15s.
        outcome2, kv2 = run_sequence(td, [0, 0, good, good])
        assert outcome2 == "Ready", (outcome2, kv2)
        assert kv2["attempts"] == "4", kv2
        assert kv2["codes"] == "21,21,21", kv2
        assert abs(float(kv2["elapsed"]) - 15.0) < 1e-9, kv2

        # 3. 15 x [0] -> Exhausted, attempts == 13 (the cap).
        outcome3, kv3 = run_sequence(td, [0] * 15)
        assert outcome3 == "Exhausted", (outcome3, kv3)
        assert kv3["attempts"] == "13", kv3

        # 4. [0, p(0x21), p(0x21)*1.05, p(0x21)*1.05] -> Ready after 4
        # (the 5% jump fails the 3% stability check once).
        outcome4, kv4 = run_sequence(td, [0, good, jump, jump])
        assert outcome4 == "Ready", (outcome4, kv4)
        assert kv4["attempts"] == "4", kv4

        # 5. a saturated line -> Saturated after 1.
        outcome5, kv5 = run_sequence(td, [65535])
        assert outcome5 == "Saturated", (outcome5, kv5)
        assert kv5["attempts"] == "1", kv5

    if _WarmupHarness is not None:
        h1 = _WarmupHarness([good])
        assert h1.run() == (0x21, 0x21, 0x21), "harness cross-check 1 failed"
        h2 = _WarmupHarness([0, 0, good, good])
        assert h2.run() == (0x21, 0x21, 0x21), "harness cross-check 2 failed"
        assert h2.runs == 4, h2.runs
        from of135i import safety
        h3 = _WarmupHarness([0] * 15)
        try:
            h3.run()
        except safety.LampWarmupError as e:
            assert e.measurements == 13, e.measurements
        else:
            raise AssertionError("harness cross-check 3: dark lamp did not fail")

    print(f"test_warmup_policy OK ({outcome}, {outcome2}, {outcome3}, {outcome4}, {outcome5})")


# ------------------------------------------------- 15-21. hook 4 (shading)

# The four C++ programs docs/sane-hook4-shading.md section 6 emits from
# the ONE captured cal_shading_verify phase (split at its own split_at)
# plus cal_shading_measure/cal_shading_upload, in the order Scanner.scan()
# runs them.
SHADING_PROGRAM_NAMES = (
    "cal_shading_measure", "cal_shading_upload",
    "cal_shading_verify", "cal_shading_verify_upload",
)
_SHADING_OFFSET_INJECTIONS = (
    "offset_r_hi", "offset_r_lo", "offset_g_hi", "offset_g_lo",
    "offset_b_hi", "offset_b_lo",
)


def test_shading_programs_match_python_replayer():
    """docs/sane-hook4-shading.md section 6, Part C test 1: wire equality
    for the four shading programs of plain3600. `_exec_ops` (not
    `_run_phase`) is wrapped: cal_shading_verify's two halves are run as
    two direct `_exec_ops` calls from `_scan_plain`, not through
    `_run_phase`, and wrapping `_exec_ops` catches every call (including
    the ones `_run_phase` itself makes) uniformly. The byte injections
    (cal_shading_measure's offset codes) and the bulk injections
    (cal_shading_upload's/cal_shading_verify_upload's shading-table
    payloads) are recovered directly from the already-patched `ops` list
    each call received -- exactly what went out on the wire -- and fed
    to the C++ side via --inject/--inject-bulk so both sides compute
    from the same values; the C++ shading computation itself is checked
    separately (test_shading_table_reference_vectors), so this test
    isolates the transfer stream."""
    probe = _build_probe()
    if probe is None:
        print("test_shading_programs_match_python_replayer SKIPPED (no g++)")
        return "skipped"

    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_build_cal_buffers())
    scanner = Scanner(UsbIo(fake))

    calls: list[tuple[str, int, int, list]] = []
    orig_exec_ops = scanner._exec_ops

    def wrapped(ops, *a, **kw):
        start = len(fake.wire_log)
        result = orig_exec_ops(ops, *a, **kw)
        end = len(fake.wire_log)
        calls.append((scanner.session.phase, start, end, ops))
        return result

    scanner._exec_ops = wrapped  # type: ignore[method-assign]

    with fast_time():
        scanner.initialize()
        scanner.scan(frame=1)

    def calls_named(name):
        return [c for c in calls if c[0] == name]

    measure_calls = calls_named("cal_shading_measure")
    upload_calls = calls_named("cal_shading_upload")
    verify_calls = calls_named("cal_shading_verify")
    assert len(measure_calls) == 1, measure_calls
    assert len(upload_calls) == 1, upload_calls
    # cal_shading_verify runs as two direct _exec_ops calls sharing the
    # same session.phase (the measurement half, then the re-upload half,
    # _scan_plain lines ~1730-1737).
    assert len(verify_calls) == 2, verify_calls

    split_at = tables.CAL_SHADING_VERIFY.split_at

    program_calls = {
        "cal_shading_measure": measure_calls[0],
        "cal_shading_upload": upload_calls[0],
        "cal_shading_verify": verify_calls[0],
        "cal_shading_verify_upload": verify_calls[1],
    }

    total = 0
    counts: dict[str, int] = {}
    for prog_name in SHADING_PROGRAM_NAMES:
        _, start, end, ops = program_calls[prog_name]
        py_transfers = _python_transfers(fake.wire_log[start:end])

        injects: dict[str, int] = {}
        bulk_injects: dict[str, bytes] = {}
        if prog_name == "cal_shading_measure":
            for name in _SHADING_OFFSET_INJECTIONS:
                _, idx, off = tables.CAL_SHADING_MEASURE.injections[name]
                injects[name] = ops[idx].data[off]
        elif prog_name == "cal_shading_upload":
            _, idxs = tables.CAL_SHADING_UPLOAD.injections["shading_table"]
            bulk_injects["shading_table"] = b"".join(ops[i].data for i in idxs)
        elif prog_name == "cal_shading_verify_upload":
            _, idxs = tables.CAL_SHADING_VERIFY.injections["shading_table2"]
            local = [i - split_at for i in idxs]
            bulk_injects["shading_table2"] = b"".join(ops[i].data for i in local)

        rc, out, err = _run_probe_program(
            probe, "plain3600", prog_name,
            injects=injects or None, bulk_injects=bulk_injects or None)
        assert rc == 0, (prog_name, out, err)
        assert _lines(out)[-1].startswith("DONE"), (prog_name, out)
        cpp_transfers = _parse_probe_transfers(out)

        assert py_transfers == cpp_transfers, (
            f"{prog_name}: python and C++ transfer logs differ\n"
            f"python ({len(py_transfers)}): {py_transfers}\n"
            f"cpp    ({len(cpp_transfers)}): {cpp_transfers}")
        counts[prog_name] = len(py_transfers)
        total += len(py_transfers)

    counts_str = ", ".join(f"{name}={counts[name]}" for name in SHADING_PROGRAM_NAMES)
    print(f"test_shading_programs_match_python_replayer OK "
          f"({total} transfers across {len(SHADING_PROGRAM_NAMES)} programs: {counts_str})")


def test_shading_bulk_out_ops_carry_no_captured_data():
    """No emitted BulkOut op of the four shading programs carries the
    reference unit's captured chunk as `data`: tools/gen_sane_tables.py
    clears it for every BulkOut a bulk injection covers (docs/sane-
    hook4-shading.md section 6, Part B/2 -- the captured chunk is the
    reference unit's own shading table, calibration data of ONE unit,
    the same principle as a register injection); only `len` survives.
    Checked structurally, without running a program at all, via the
    probe's `program_info` mode (kind/len/has_data per op)."""
    probe = _build_probe()
    if probe is None:
        print("test_shading_bulk_out_ops_carry_no_captured_data SKIPPED (no g++)")
        return "skipped"

    total_bulk_out = 0
    for prog_name in SHADING_PROGRAM_NAMES:
        r = subprocess.run([probe, "program_info", "plain3600", prog_name],
                           capture_output=True, text=True)
        assert r.returncode == 0, (prog_name, r.stdout, r.stderr)
        for line in _lines(r.stdout):
            if " kind=BulkOut " in line:
                assert line.endswith("has_data=0"), (prog_name, line)
                total_bulk_out += 1
    assert total_bulk_out > 0, (
        "expected at least one BulkOut op across the four shading programs")
    print(f"test_shading_bulk_out_ops_carry_no_captured_data OK "
          f"({total_bulk_out} BulkOut ops across {len(SHADING_PROGRAM_NAMES)} "
          f"programs, all has_data=0)")


def test_poll_class_waits_then_continues():
    """docs/sane-hook4-shading.md section 3, W2: the poll on reg 0x100
    (cal_shading_measure op 448) waits through non-matching classes then
    continues once the reply's upper nibble reaches 0xf0's class."""
    probe = _build_probe()
    if probe is None:
        print("test_poll_class_waits_then_continues SKIPPED (no g++)")
        return "skipped"

    injects = {
        "offset_r_hi": 0x01, "offset_r_lo": 0x0B, "offset_g_hi": 0x01,
        "offset_g_lo": 0x0A, "offset_b_hi": 0x01, "offset_b_lo": 0x0B,
    }
    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_shading_measure",
        ["class_poll d055,d055,f055"], injects=injects)
    assert rc == 0, (out, err)
    lines = _lines(out)
    assert lines[-1] == "DONE ops=450", lines[-1]
    class_site = [ln for ln in lines if ln.startswith("R req=04 val=018e idx=0022")]
    assert len(class_site) == 3, class_site
    print("test_poll_class_waits_then_continues OK")


def test_poll_class_timeout_fails_closed():
    """docs/sane-hook4-shading.md section 3: a class that never reaches
    0xf0 times out after class_timeout_ms, fail-closed at op 448."""
    probe = _build_probe()
    if probe is None:
        print("test_poll_class_timeout_fails_closed SKIPPED (no g++)")
        return "skipped"

    injects = {
        "offset_r_hi": 0x01, "offset_r_lo": 0x0B, "offset_g_hi": 0x01,
        "offset_g_lo": 0x0A, "offset_b_hi": 0x01, "offset_b_lo": 0x0B,
    }
    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_shading_measure",
        ["class_poll d055"], injects=injects)
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert lines[-1].startswith("FAIL PollTimeout op=448 "), lines[-1]
    # Nothing after the failure but the repeated (never-settling) poll read.
    assert lines[-2] == "R req=04 val=018e idx=0022 len=2", lines[-2]
    print(f"test_poll_class_timeout_fails_closed OK ({lines[-1]})")


def test_short_bulk_out_fails_closed():
    """docs/sane-hook4-shading.md section 6, Part C test 3:
    cal_shading_upload with its second BulkOut accepting only 1000 B ->
    FAIL ShortBulkOut after exactly two bulk OUTs."""
    probe = _build_probe()
    if probe is None:
        print("test_short_bulk_out_fails_closed SKIPPED (no g++)")
        return "skipped"

    payload = bytes(calibrate.SHADING_UPLOAD_LEN)   # content is irrelevant here
    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_shading_upload", ["bulk_out_len_at 1 1000"],
        bulk_injects={"shading_table": payload})
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert lines[-1].startswith("FAIL ShortBulkOut op="), lines[-1]
    bo_lines = [ln for ln in lines if ln.startswith("BO len=")]
    assert len(bo_lines) == 2, bo_lines
    assert bo_lines[1].startswith("BO len=1000 "), bo_lines[1]
    print(f"test_short_bulk_out_fails_closed OK ({lines[-1]})")


def test_missing_bulk_injection_fails_before_any_transfer():
    """docs/sane-hook4-shading.md section 6, Part C test 4: no
    --inject-bulk at all -> MissingInjection before any transfer."""
    probe = _build_probe()
    if probe is None:
        print("test_missing_bulk_injection_fails_before_any_transfer SKIPPED (no g++)")
        return "skipped"

    rc, out, err = _run_probe_program(probe, "plain3600", "cal_shading_upload")
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert len(lines) == 1, lines
    assert lines[0].startswith("FAIL MissingInjection "), lines
    assert lines[0].endswith("ops=0"), lines
    print(f"test_missing_bulk_injection_fails_before_any_transfer OK ({lines[0]!r})")


def test_bulk_injection_too_long_is_refused():
    """docs/sane-hook4-shading.md section 6, Part C test 4: a value
    longer than the covered BulkOut ops' combined length (46080 B for
    plain3600's cal_shading_upload: 16384+16384+12800+512) ->
    BadInjection before any transfer."""
    probe = _build_probe()
    if probe is None:
        print("test_bulk_injection_too_long_is_refused SKIPPED (no g++)")
        return "skipped"

    payload = bytes(46081)   # 1 B over the 46080 B combined chunk total
    rc, out, err = _run_probe_program(
        probe, "plain3600", "cal_shading_upload",
        bulk_injects={"shading_table": payload})
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert len(lines) == 1, lines
    assert lines[0].startswith("FAIL BadInjection "), lines
    assert lines[0].endswith("ops=0"), lines
    print(f"test_bulk_injection_too_long_is_refused OK ({lines[0]!r})")


def test_shading_table_reference_vectors():
    """docs/sane-hook4-shading.md section 4/6, Part C test 5: the pure
    shading computation (shading_table/shading_table2/shading_upload_len),
    C++ vs the reference capture and vs of135i.calibrate byte for byte."""
    probe = _build_probe()
    if probe is None:
        print("test_shading_table_reference_vectors SKIPPED (no g++)")
        return "skipped"

    meas_path = CAPTURE_DIR / "cal-frame00797-len2889216.bin"
    raw = meas_path.read_bytes()
    meas = np.frombuffer(raw, dtype="<u2").reshape(128, 3762, 3)

    with tempfile.TemporaryDirectory() as td:
        # -- shading_table: reference capture, C++ vs Python, vs the
        # vendor's own upload (the driver's own tolerance) --
        out1 = Path(td) / "out1.bin"
        r1 = subprocess.run(
            [probe, "shading_table", str(meas_path), "128", "3762", str(out1)],
            capture_output=True, text=True)
        assert r1.returncode == 0, r1
        assert _lines(r1.stdout)[-1] == "OK len=45856", r1.stdout
        got1 = out1.read_bytes()
        assert len(got1) == 45856 == calibrate.SHADING_UPLOAD_LEN

        py1 = calibrate.shading_table(meas)
        assert got1 == py1, "C++ shading_table differs from Python byte for byte"

        truth = (CAPTURE_DIR / "shading-upload-len45856.bin").read_bytes()
        got_offsets, got_gains = _parse_shading_blocks(got1)
        want_offsets, _want_gains = _parse_shading_blocks(truth)
        assert set(np.unique(got_gains).tolist()) == {0x4000}
        diff = np.abs(got_offsets.astype(int) - want_offsets.astype(int))
        within_tol = float((diff <= 8).mean())
        assert within_tol >= 0.99, f"only {within_tol:.4%} of pixels within +/-8"

        # -- shading_table2 (a): the same capture as both white and dark --
        out2a = Path(td) / "out2a.bin"
        r2a = subprocess.run(
            [probe, "shading_table2", str(meas_path), str(meas_path),
             "128", "3762", str(out2a)],
            capture_output=True, text=True)
        assert r2a.returncode == 0, r2a
        got2a = out2a.read_bytes()
        py2a = calibrate.shading_table2(meas, meas)
        assert got2a == py2a, "shading_table2(a) differs from Python byte for byte"

        # -- shading_table2 (b): seeded random white > dark --
        rng = np.random.default_rng(20260908)
        dark_b = rng.integers(0, 2000, (128, 3762, 3)).astype(np.uint16)
        white_b = np.clip(
            dark_b.astype(np.int32) + rng.integers(1000, 20000, (128, 3762, 3)),
            0, 65535).astype(np.uint16)
        white_b_path, dark_b_path = Path(td) / "white_b.bin", Path(td) / "dark_b.bin"
        white_b_path.write_bytes(white_b.astype("<u2").tobytes())
        dark_b_path.write_bytes(dark_b.astype("<u2").tobytes())
        out2b = Path(td) / "out2b.bin"
        r2b = subprocess.run(
            [probe, "shading_table2", str(white_b_path), str(dark_b_path),
             "128", "3762", str(out2b)],
            capture_output=True, text=True)
        assert r2b.returncode == 0, r2b
        got2b = out2b.read_bytes()
        py2b = calibrate.shading_table2(white_b, dark_b)
        assert got2b == py2b, "shading_table2(b) differs from Python byte for byte"

        # -- shading_table2 (c): hits both the gain clip at 65535 (most
        # pixels: white == dark, so w - f0 == 0, floored to 1.0, driving
        # gain to the clip) and the max(w - f0, 1.0) floor itself --
        dark_c = np.full((128, 3762, 3), 100, dtype=np.uint16)
        white_c = np.full((128, 3762, 3), 100, dtype=np.uint16)
        white_c[:, 0, 0] = 50000   # one pixel: a real difference, not clipped
        white_c_path, dark_c_path = Path(td) / "white_c.bin", Path(td) / "dark_c.bin"
        white_c_path.write_bytes(white_c.astype("<u2").tobytes())
        dark_c_path.write_bytes(dark_c.astype("<u2").tobytes())
        out2c = Path(td) / "out2c.bin"
        r2c = subprocess.run(
            [probe, "shading_table2", str(white_c_path), str(dark_c_path),
             "128", "3762", str(out2c)],
            capture_output=True, text=True)
        assert r2c.returncode == 0, r2c
        got2c = out2c.read_bytes()
        py2c = calibrate.shading_table2(white_c, dark_c)
        assert got2c == py2c, "shading_table2(c) differs from Python byte for byte"
        _offsets_c, gains_c = _parse_shading_blocks(got2c)
        assert 65535 in set(gains_c.tolist()), "test case did not hit the gain clip"

        # -- shading_upload_len --
        r3 = subprocess.run([probe, "upload_len", "3762"], capture_output=True, text=True)
        assert r3.returncode == 0, r3
        assert r3.stdout.strip() == "LEN=45856", r3.stdout
        r4 = subprocess.run([probe, "upload_len", "5184"], capture_output=True, text=True)
        assert r4.returncode == 0, r4
        assert r4.stdout.strip() == "LEN=63192", r4.stdout

    print(f"test_shading_table_reference_vectors OK (within +/-8: {within_tol:.4%})")


# ------------------------------------------ 23-27. hooks 5-7: the frame
#
# docs/sane-hook5-frame.md: POSITION (hook 5), SCAN's setup (hook 6a) and
# image chunks (hook 6b), and the semantic PARK program (hook 7). The
# "position"/"scan_setup" op programs are generated the same way hook
# 2-4's are (tools/gen_sane_tables.py's decode_ops()); "park" is built
# straight from tables.PARK's own captured constants (build_park_program()
# in the generator), not replayed verbatim -- see that function's
# docstring for the two deviations from the plan's own prose (no
# AckRead ops, four ReadModifyWrite sites not three) and their evidence.


def test_position_and_scan_setup_match_python_replayer():
    """Wire equality for POSITION (whole phase) and SCAN's setup (ops
    0-320, the part before the first image-data descriptor): the Python
    driver's actual transfers, from a live scanner.scan(frame=1) over
    the existing FakeUsbDevice, against the C++ "position"/"scan_setup"
    OpProgram's own transfer log, both fed frame 1's FEEDL and the
    default line count."""
    probe = _build_probe()
    if probe is None:
        print("test_position_and_scan_setup_match_python_replayer SKIPPED (no g++)")
        return "skipped"

    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_build_cal_buffers())
    scanner = Scanner(UsbIo(fake))

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
        scanner.initialize()
        scanner.scan(frame=1)   # POSITION, then SCAN (image + verbatim PARK)

    # ---- position ------------------------------------------------------
    assert "position" in slices, sorted(slices)
    pos_start, pos_end = slices["position"][0]
    py_position = _python_transfers(fake.wire_log[pos_start:pos_end])

    # The live driver commands the A+C geometry since the Test 58
    # migration; feed the C++ program the SAME values so the equality
    # under test stays what it always was -- the transfer STRUCTURE.
    # (The backend's own runtime grid is still the legacy tables until
    # SANE migrates; test_feedl_and_position_budget pins that.)
    from of135i import holder as _holder
    from of135i import image as _ofimage
    geom = _holder.overscan_geometry(
        1, res_units_per_line=7200 // 3600,
        chunk_lines=tables.IMAGE_CHUNK_LINES,
        colour_crop_lines=_ofimage.align_shift(3600))
    feedl = geom.feedl
    injects = {
        "feedl_hi": (feedl >> 16) & 0xFF,
        "feedl_mid": (feedl >> 8) & 0xFF,
        "feedl_lo": feedl & 0xFF,
    }
    rc, out, err = _run_probe_program(probe, "plain3600", "position", None, injects=injects)
    assert rc == 0, (out, err)
    assert _lines(out)[-1].startswith("DONE"), out
    cpp_position = _parse_probe_transfers(out)
    assert py_position == cpp_position, (
        f"position: python and C++ transfer logs differ\n"
        f"python ({len(py_position)}): {py_position}\n"
        f"cpp    ({len(cpp_position)}): {cpp_position}")

    # ---- scan_setup (SCAN ops 0-320) ------------------------------------
    assert "scan" in slices, sorted(slices)
    scan_start, _scan_end = slices["scan"][0]
    desc = tables.IMAGE_DESC_DATA
    first_desc = next(i for i, op in enumerate(tables.SCAN.ops)
                      if op.kind == "cw" and op.wv == 0x0082 and op.data == desc)
    assert first_desc == 321, first_desc
    py_scan_setup = _python_transfers(fake.wire_log[scan_start:scan_start + first_desc])

    lines_n = tables.scan_lines_for_chunks(geom.chunks)
    injects2 = {"lines_hi": (lines_n >> 8) & 0xFF, "lines_lo": lines_n & 0xFF}
    rc, out, err = _run_probe_program(probe, "plain3600", "scan_setup", None, injects=injects2)
    assert rc == 0, (out, err)
    assert _lines(out)[-1].startswith("DONE"), out
    cpp_scan_setup = _parse_probe_transfers(out)
    assert py_scan_setup == cpp_scan_setup, (
        f"scan_setup: python and C++ transfer logs differ\n"
        f"python ({len(py_scan_setup)}): {py_scan_setup}\n"
        f"cpp    ({len(cpp_scan_setup)}): {cpp_scan_setup}")

    print(f"test_position_and_scan_setup_match_python_replayer OK "
          f"(position {len(py_position)} transfers, scan_setup {len(py_scan_setup)} "
          f"transfers, feedl={feedl}, lines={lines_n})")


def test_image_chunks_match_python_replayer():
    """docs/sane-hook5-frame.md section 2/6, hook 6b: read_image_chunk()
    over the plain3600 profile's own frame-1 shape (223 full 519156 B
    chunks + one 180576 B tail, tables.IMAGE_CHUNK_COUNT/IMAGE_CHUNK_LEN/
    IMAGE_TRAILING_DRAIN_LEN) against a Python-side expected transfer
    list built from those same constants -- NOT a literal replay of
    tables.SCAN.ops[321:]'s ~33-fragment-per-chunk raw USB capture (see
    read_image_chunk()'s doc comment in sane/gl126_ops.h for why: that
    fragmentation is a USB-packet-level artifact of the reference
    capture, not protocol behaviour, the same "provenance, not
    behaviour" principle already applied to captured pacing)."""
    probe = _build_probe()
    if probe is None:
        print("test_image_chunks_match_python_replayer SKIPPED (no g++)")
        return "skipped"

    n_full = tables.IMAGE_CHUNK_COUNT
    full_len = tables.IMAGE_CHUNK_LEN
    last_len = tables.IMAGE_TRAILING_DRAIN_LEN
    assert (n_full, full_len, last_len) == (223, 519156, 180576), (n_full, full_len, last_len)

    def expected_transfers():
        out: list[tuple] = []
        for i in range(n_full):
            wi = 0x0008 if i == 0 else 0x0000
            desc = bytes([0x00, 0x00, 0x00, 0x10]) + full_len.to_bytes(4, "little")
            out.append(("W", 0x04, 0x0082, wi, desc))
            out.append(("R", 0x0C, 0x008E, 0x0020, 1))
            out.append(("B", full_len))
        desc = bytes([0x00, 0x00, 0x00, 0x10]) + last_len.to_bytes(4, "little")
        out.append(("W", 0x04, 0x0082, 0x0000, desc))
        out.append(("R", 0x0C, 0x008E, 0x0020, 1))
        out.append(("B", last_len))
        return out

    py_transfers = expected_transfers()
    assert py_transfers[0] == ("W", 0x04, 0x0082, 0x0008,
                               bytes.fromhex("00000010f4eb0700")), py_transfers[0]
    assert py_transfers[-3] == ("W", 0x04, 0x0082, 0x0000,
                                bytes.fromhex("0000001060c10200")), py_transfers[-3]

    r = subprocess.run([str(probe), "image_chunks", str(n_full), str(full_len), str(last_len)],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _lines(r.stdout)[-1] == "DONE", r.stdout
    cpp_transfers = _parse_probe_transfers(r.stdout)

    assert py_transfers == cpp_transfers, (
        f"image_chunks: python and C++ transfer logs differ in "
        f"{sum(1 for a, b in zip(py_transfers, cpp_transfers) if a != b)} places "
        f"(python {len(py_transfers)}, cpp {len(cpp_transfers)})")
    print(f"test_image_chunks_match_python_replayer OK "
          f"({len(py_transfers)} transfers, {n_full + 1} chunks)")


class _ParkWireDev:
    """Records EVERY control transfer (both directions) into one ordered
    `transfers` list, in the same tuple shape _parse_probe_transfers()/
    _python_transfers() use -- unlike tests/test_park.py's own _FakeDev/
    _FakeIo, which deliberately do NOT log read_reg()'s wire reads (that
    file's assertions never needed them). This is a separate, purpose-
    built fake for byte-for-byte wire equality against the C++ "park"
    OpProgram (docs/sane-hook5-frame.md section 6, Part C test 3)."""

    def __init__(self, reg15, reg32_seq, reg35, status_seq):
        self.transfers: list[tuple] = []
        self._reg15 = reg15
        self._reg32_seq = list(reg32_seq)
        self._reg32_calls = 0
        self._reg35 = reg35
        self._status_seq = list(status_seq)
        self._status_calls = 0

    def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wlength=None, timeout=None):
        if bm & 0x80:   # IN
            length = int(data_or_wlength)
            self.transfers.append(("R", br, wv, wi, length))
            if wv == 0x018E and wi == 0x0122:
                idx = min(self._status_calls, len(self._status_seq) - 1)
                self._status_calls += 1
                return bytes(self._status_seq[idx])
            if wv == 0x008E:
                reg = wi >> 8
                if reg == 0x01:
                    v = 0x22   # start-state guard (of135i.safety): idle-homed
                elif reg == 0x15:
                    v = self._reg15
                elif reg == 0x35:
                    v = self._reg35
                elif reg == 0x32:
                    idx = min(self._reg32_calls, len(self._reg32_seq) - 1)
                    self._reg32_calls += 1
                    v = self._reg32_seq[idx]
                else:
                    v = 0
                return bytes([v, 0x55])
            return bytes(length)
        data = bytes(data_or_wlength) if data_or_wlength is not None else b""
        self.transfers.append(("W", br, wv, wi, data))
        return len(data)


class _ParkWireIo:
    """Duck type for UsbIo, routing every call through _ParkWireDev so
    park_semantic()'s writes AND reads both land on the wire log."""

    def __init__(self, dev):
        self.dev = dev

    def write_regs(self, pairs):
        data = bytes(b for pair in pairs for b in pair)
        self.dev.ctrl_transfer(0x40, 0x04, 0x0083, 0, data)

    def read_reg(self, reg: int, strict: bool = False) -> int:
        resp = bytes(self.dev.ctrl_transfer(0xC0, 0x04, 0x008E, (reg << 8) | 0x22, 2))
        return resp[0]

    def read_ext_reg(self, reg: int) -> int:
        return 0

    def close(self) -> None:
        pass


def test_park_program_matches_park_semantic():
    """docs/sane-hook5-frame.md section 6, Part C test 3: the C++
    "park" OpProgram's transfers over the probe fake == park_semantic()'s
    own transfers over a Python fake scripted with the SAME register
    values (so the RMW sites' computed write payloads agree byte for
    byte on both sides), tested as tests/test_park.py drives it --
    scanner.park_semantic(ir=False), no prior load/scan needed."""
    probe = _build_probe()
    if probe is None:
        print("test_park_program_matches_park_semantic SKIPPED (no g++)")
        return "skipped"

    reg15, reg35 = 0x90, 0xFB
    reg32_seq = (0x81, 0x95)
    status_seq = [b"\xe8\x55"]   # idle immediately -- Wait B settles on the first read

    dev = _ParkWireDev(reg15=reg15, reg32_seq=reg32_seq, reg35=reg35, status_seq=status_seq)
    io = _ParkWireIo(dev)
    scanner = Scanner(io)
    with fast_time():
        scanner.park_semantic(ir=False)
    # Scanner._operation("park")'s own start-state guard reads reg 0x01
    # once before park_semantic()'s own first transfer -- session-guard
    # machinery around the operation, not part of PARK's op sequence
    # itself (the C++ "park" OpProgram has no such read either).
    py_transfers = dev.transfers
    assert py_transfers[0] == ("R", 0x04, 0x008E, 0x0122, 2), py_transfers[0]
    py_transfers = py_transfers[1:]

    script = [
        f"rmw_read_at 0 {reg15:02x}",     # reg 0x15
        f"rmw_read_at 1 {reg32_seq[0]:02x}",   # reg 0x32, pre-Wait-A
        f"rmw_read_at 2 {reg35:02x}",     # reg 0x35, post-Wait-A
        f"rmw_read_at 3 {reg32_seq[1]:02x}",   # reg 0x32, closing idle round
        f"masked_poll_at 0 {reg35:02x}55",     # Wait A: settle at once
        f"masked_poll_at 1 {status_seq[0].hex()}",   # Wait B: settle at once
    ]
    rc, out, err = _run_probe_program(probe, "plain3600", "park", script)
    assert rc == 0, (out, err)
    assert _lines(out)[-1].startswith("DONE"), out
    cpp_transfers = _parse_probe_transfers(out)

    assert py_transfers == cpp_transfers, (
        f"park: python and C++ transfer logs differ\n"
        f"python ({len(py_transfers)}): {py_transfers}\n"
        f"cpp    ({len(cpp_transfers)}): {cpp_transfers}")
    # Sanity: no AckRead-shaped extra reads snuck in on the Python side
    # either (see build_park_program()'s docstring -- write_regs() never
    # issues one), and the two 0x8b payloads landed with their captured
    # values.
    ctrl_8b = [t for t in py_transfers if t[0] == "W" and t[2] == 0x008B]
    assert len(ctrl_8b) == 2, ctrl_8b
    print(f"test_park_program_matches_park_semantic OK ({len(py_transfers)} transfers)")


def test_poll_masked_waits_then_continues():
    """docs/sane-hook5-frame.md section 3: POSITION's W3 poll (class F on
    reg 0x101) waits through non-matching classes then continues once the
    reply's upper nibble reaches 0xf0."""
    probe = _build_probe()
    if probe is None:
        print("test_poll_masked_waits_then_continues SKIPPED (no g++)")
        return "skipped"

    injects = {"feedl_hi": 0x00, "feedl_mid": 0x1A, "feedl_lo": 0x57}
    rc, out, err = _run_probe_program(
        probe, "plain3600", "position", ["masked_poll_at 0 9c55,d555,f455"], injects=injects)
    assert rc == 0, (out, err)
    lines = _lines(out)
    assert lines[-1] == "DONE ops=48", lines[-1]
    # POSITION's own op 34 is a plain (non-polling) Read at this same
    # wValue/wIndex, captured before the mode batch write -- one more
    # match here regardless of scripting; the PollMasked site (op 47)
    # itself is read 3 times per the script.
    site = [ln for ln in lines if ln.startswith("R req=04 val=018e idx=0122")]
    assert len(site) == 4, site
    print("test_poll_masked_waits_then_continues OK")


def test_poll_masked_timeout_fails_closed():
    """A class that never reaches F (POSITION's W3) and a status word
    that never reads PARK_COMPLETE (PARK's Wait B) both time out
    PollMasked, fail-closed, no transfer sent after."""
    probe = _build_probe()
    if probe is None:
        print("test_poll_masked_timeout_fails_closed SKIPPED (no g++)")
        return "skipped"

    injects = {"feedl_hi": 0x00, "feedl_mid": 0x1A, "feedl_lo": 0x57}
    rc, out, err = _run_probe_program(
        probe, "plain3600", "position", ["masked_poll_at 0 d555"], injects=injects)
    assert rc == 1, (out, err)
    lines = _lines(out)
    assert lines[-1].startswith("FAIL PollTimeout op=47 "), lines[-1]
    assert lines[-2] == "R req=04 val=018e idx=0122 len=2", lines[-2]

    # PARK's Wait B (occurrence 1): Wait A (occurrence 0) settles
    # unscripted at its own `want`, Wait B never does.
    rc2, out2, err2 = _run_probe_program(
        probe, "plain3600", "park", ["masked_poll_at 1 9c55"])
    assert rc2 == 1, (out2, err2)
    lines2 = _lines(out2)
    assert lines2[-1].startswith("FAIL PollTimeout op="), lines2[-1]
    assert lines2[-2] == "R req=04 val=018e idx=0122 len=2", lines2[-2]
    print(f"test_poll_masked_timeout_fails_closed OK "
          f"(position: {lines[-1]}; park: {lines2[-1]})")


def test_feedl_and_position_budget():
    """docs/sane-hook5-frame.md section 4/6: feedl_for_frame() against
    of135i/tables.py's own table for frames 1-6 (the six-aperture strip
    holder, of135i/holder.py), and position_timeout_ms() against
    3 * 1.6141 * position_timeout_scale()."""
    probe = _build_probe()
    if probe is None:
        print("test_feedl_and_position_budget SKIPPED (no g++)")
        return "skipped"

    from of135i.device import position_timeout_scale

    feedls = {}
    for frame in (1, 2, 3, 4, 5, 6):
        want = tables.feedl_for_frame(frame)
        r = subprocess.run([str(probe), "feedl", str(frame)], capture_output=True, text=True)
        assert r.returncode == 0, r
        kv = dict(p.split("=", 1) for p in r.stdout.split())
        got = int(kv["FEEDL"])
        assert got == want, (frame, got, want)
        assert int(kv["hi"], 16) == (want >> 16) & 0xFF
        assert int(kv["mid"], 16) == (want >> 8) & 0xFF
        assert int(kv["lo"], 16) == want & 0xFF
        feedls[frame] = want

    assert feedls[1] == 6743, feedls
    assert feedls[5] == 49783 and feedls[6] == 60543, feedls

    r1 = subprocess.run([str(probe), "position_timeout", "6743"], capture_output=True, text=True)
    assert r1.returncode == 0, r1
    ms1 = int(r1.stdout.strip().split("=")[1])
    assert abs(ms1 - 4842) <= 1, ms1

    feedl4 = feedls[4]
    r4 = subprocess.run([str(probe), "position_timeout", str(feedl4)],
                        capture_output=True, text=True)
    assert r4.returncode == 0, r4
    ms4 = int(r4.stdout.strip().split("=")[1])
    scale4 = position_timeout_scale(tables, feedl4)
    want_s4 = 3 * 1.6141 * scale4
    assert abs(ms4 / 1000.0 - want_s4) < 0.05, (ms4, want_s4, scale4)
    print(f"test_feedl_and_position_budget OK (feedl frames 1-6={feedls}, "
          f"position_timeout_ms(6743)={ms1}, ({feedl4})={ms4} ~= {want_s4:.1f}s)")


def test_position_frames_2_to_6_match_python_replayer():
    """Frame selection (docs/sane-hook5-frame.md section 10): for frames
    2-6 (the six-aperture strip holder, of135i/holder.py) the driver's
    POSITION differs from frame 1's only in the three FEEDL bytes; the
    C++ "position" program fed the same frame's feedl_for_frame() must
    produce the same transfers, and the FEEDL-scaled budget must match
    position_timeout_scale()."""
    probe = _build_probe()
    if probe is None:
        print("test_position_frames_2_to_6_match_python_replayer SKIPPED (no g++)")
        return "skipped"
    from of135i.device import position_timeout_scale  # noqa: E402

    results = []
    for frame in (2, 3, 4, 5, 6):
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_build_cal_buffers())
        scanner = Scanner(UsbIo(fake))
        slices: dict[str, list[tuple[int, int]]] = {}
        orig_run_phase = scanner._run_phase

        def wrapped(phase, *a, **kw):
            start = len(fake.wire_log)
            result = orig_run_phase(phase, *a, **kw)
            slices.setdefault(phase.name, []).append((start, len(fake.wire_log)))
            return result

        scanner._run_phase = wrapped  # type: ignore[method-assign]
        with fast_time():
            scanner.initialize()
            scanner.scan(frame=frame)
        pos_start, pos_end = slices["position"][0]
        py_position = _python_transfers(fake.wire_log[pos_start:pos_end])

        # The live driver commands the A+C geometry (Test 58 migration);
        # inject the same frame's geometry FEEDL into the C++ program so
        # the equality under test stays the transfer structure.
        from of135i import holder as _holder
        from of135i import image as _ofimage
        feedl = _holder.overscan_geometry(
            frame, res_units_per_line=7200 // 3600,
            chunk_lines=tables.IMAGE_CHUNK_LINES,
            colour_crop_lines=_ofimage.align_shift(3600)).feedl
        injects = {"feedl_hi": (feedl >> 16) & 0xFF, "feedl_mid": (feedl >> 8) & 0xFF,
                   "feedl_lo": feedl & 0xFF}
        rc, out, err = _run_probe_program(probe, "plain3600", "position", None,
                                          injects=injects)
        assert rc == 0, (out, err)
        cpp_position = _parse_probe_transfers(out)
        assert py_position == cpp_position, (
            f"frame {frame}: python and C++ POSITION transfer logs differ\n"
            f"python ({len(py_position)}): {py_position}\n"
            f"cpp    ({len(cpp_position)}): {cpp_position}")

        # The budget: C++ position_timeout_ms(feedl) vs 3 x 1.6141 s x scale.
        r = subprocess.run([probe, "position_timeout", str(feedl)],
                           capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout, r.stderr)
        ms = int(r.stdout.strip().split("=")[1])
        expected_ms = 3 * 1.6141 * position_timeout_scale(tables, feedl) * 1000
        assert abs(ms - expected_ms) <= 2, (frame, ms, expected_ms)
        results.append((frame, feedl, len(py_position), ms))
    print(f"test_position_frames_2_to_6_match_python_replayer OK "
          f"({', '.join(f'f{f}: feedl {fl}, {n} transfers, budget {ms} ms' for f, fl, n, ms in results)})")


def test_feedl_for_frame_refuses_frame_7_and_frame_0():
    """gl126_ops::feedl_for_frame(frame) (gl126_ops.h/cpp) is the C++
    mirror of of135i/holder.py::check_frame(): a frame outside
    1-kFeedlFrameMax (6, the strip holder's aperture count) is refused
    with std::invalid_argument before any FEEDL is computed. The probe's
    "feedl" command does not catch it itself -- main()'s top-level
    catch(std::exception&) does, printing "ERROR ..." and returning 2 --
    so this is also a check that nothing downstream of feedl_for_frame()
    ever sees an out-of-range value."""
    probe = _build_probe()
    if probe is None:
        print("test_feedl_for_frame_refuses_frame_7_and_frame_0 SKIPPED (no g++)")
        return "skipped"
    for frame in (7, 0):
        r = subprocess.run([str(probe), "feedl", str(frame)], capture_output=True, text=True)
        assert r.returncode == 2, (frame, r.stdout, r.stderr)
        assert "ERROR" in r.stderr and "outside 1-6" in r.stderr, (frame, r.stderr)
    # 1 and 6 (the holder's own bounds) are accepted.
    for frame in (1, 6):
        r = subprocess.run([str(probe), "feedl", str(frame)], capture_output=True, text=True)
        assert r.returncode == 0, (frame, r.stdout, r.stderr)
    print("test_feedl_for_frame_refuses_frame_7_and_frame_0 OK")


# ---------------------------------------------------- 8. scan-pass state
CHUNK = 519156
RAW_TOTAL = 3762 * 3 * 2 * 5137   # frame 1 at 3600 dpi: 115 952 364 raw bytes
N_CHUNKS = -(-RAW_TOTAL // CHUNK)  # 224


def _run_scanpass(probe: str, *events: str) -> list[str]:
    r = subprocess.run([probe, "scanpass", str(RAW_TOTAL), *events],
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return _lines(r.stdout)


def test_scan_pass_complete_then_park():
    """The verified path (Test 52 attempt 3): arm, 224 full chunks (the
    first with the wIndex-8 descriptor), Complete, PARK runs once, the
    core's second end_scan is a no-op, and the next sane_start may arm
    again from Parked."""
    probe = _build_probe()
    if probe is None:
        print("test_scan_pass_complete_then_park SKIPPED (no g++)")
        return "skipped"
    events = ["arm"] + ["chunk", str(CHUNK)] * N_CHUNKS + ["park", "park", "arm"]
    out = _run_scanpass(probe, *events)
    assert out[0] == "arm ok=1 state=Armed read=0", out[0]
    assert out[1] == f"chunk ok=1 first=1 state=Streaming read={CHUNK}", out[1]
    assert out[2].startswith("chunk ok=1 first=0 state=Streaming"), out[2]
    # Streaming right up to the chunk that crosses the raw total.
    assert out[N_CHUNKS - 1].endswith(f"state=Streaming read={CHUNK * (N_CHUNKS - 1)}"), \
        out[N_CHUNKS - 1]
    assert out[N_CHUNKS] == f"chunk ok=1 first=0 state=Complete read={CHUNK * N_CHUNKS}", \
        out[N_CHUNKS]
    assert out[N_CHUNKS + 1].startswith("park decision=Run state=Parked"), out[N_CHUNKS + 1]
    assert out[N_CHUNKS + 2].startswith("park decision=AlreadyParked state=Parked"), \
        out[N_CHUNKS + 2]
    assert out[N_CHUNKS + 3] == "arm ok=1 state=Armed read=0", out[N_CHUNKS + 3]
    print(f"test_scan_pass_complete_then_park OK ({N_CHUNKS} chunks -> Complete -> "
          f"Parked, second end_scan a no-op, re-armable)")


def test_scan_pass_aborted_never_parks():
    """The three ways a pass ends early: a failed chunk read, a frontend
    cancel after N chunks, a cancel before the first chunk. None may reach
    PARK; each closes the pass (Failed) so a later end_scan -- the core
    calls it again on close -- writes nothing either, and no new pass may
    be armed until a new sane_open (the hardware gate) resets it."""
    probe = _build_probe()
    if probe is None:
        print("test_scan_pass_aborted_never_parks SKIPPED (no g++)")
        return "skipped"
    # (a) chunk read fails mid-pass.
    out = _run_scanpass(probe, "arm", "chunk", str(CHUNK), "chunkfail", "park", "park",
                        "chunk", str(CHUNK), "arm", "close", "arm")
    assert out[2] == "chunkfail ok=1 state=Failed read=519156", out[2]
    assert out[3] == "park decision=Failed state=Failed read=519156", out[3]
    assert out[4] == "park decision=Failed state=Failed read=519156", out[4]
    assert out[5].startswith("chunk ok=0"), out[5]          # no read from Failed
    assert out[6] == "arm ok=0 state=Failed read=519156", out[6]
    assert out[8] == "arm ok=1 state=Armed read=0", out[8]  # after close (new open)
    # (b) cancel after 12 of 224 chunks.
    out = _run_scanpass(probe, "arm", *(["chunk", str(CHUNK)] * 12), "park", "park", "arm")
    assert out[13] == f"park decision=AbortedPass state=Failed read={12 * CHUNK}", out[13]
    assert out[14] == f"park decision=Failed state=Failed read={12 * CHUNK}", out[14]
    assert out[15] == f"arm ok=0 state=Failed read={12 * CHUNK}", out[15]
    # (c) cancel before any chunk (begin_scan done, nothing read).
    out = _run_scanpass(probe, "arm", "park", "park")
    assert out[1] == "park decision=AbortedPass state=Failed read=0", out[1]
    assert out[2] == "park decision=Failed state=Failed read=0", out[2]
    # (d) nothing armed at all (a failed sane_start before begin_scan).
    out = _run_scanpass(probe, "park")
    assert out[0] == "park decision=NoPass state=Idle read=0", out[0]
    print("test_scan_pass_aborted_never_parks OK (chunk failure, cancel mid-pass, "
          "cancel before the first chunk, no pass: none reaches PARK, none retries)")


def test_scan_pass_park_failure_is_terminal():
    """PARK started from Complete and did not reach its completion wait
    (Test 52 attempt 1's shape): the pass is Failed, the core's next
    end_scan writes nothing, no new pass may be armed."""
    probe = _build_probe()
    if probe is None:
        print("test_scan_pass_park_failure_is_terminal SKIPPED (no g++)")
        return "skipped"
    events = ["arm"] + ["chunk", str(CHUNK)] * N_CHUNKS + ["parkfail", "park", "arm"]
    out = _run_scanpass(probe, *events)
    assert out[N_CHUNKS + 1] == f"parkfail decision=Run state=Failed read={CHUNK * N_CHUNKS}", \
        out[N_CHUNKS + 1]
    assert out[N_CHUNKS + 2].startswith("park decision=Failed state=Failed"), out[N_CHUNKS + 2]
    assert out[N_CHUNKS + 3].startswith("arm ok=0 state=Failed"), out[N_CHUNKS + 3]
    print("test_scan_pass_park_failure_is_terminal OK")



# ------------------------------------------------ 9. hook 8: dual-light
DUAL_PROFILES = (
    ("ir3600", 3600), ("dpi600", 600), ("dpi1200", 1200), ("dpi2400", 2400), ("dpi7200", 7200),
)
DUAL_PROGRAM_NAMES = (
    "prep", "afe_base", "cal_dark_a", "cal_dark_b", "cal_white", "cal_gain_check_a",
    "cal_gain_check_b", "cal_shading_measure", "cal_shading_upload", "cal_shading_verify",
    "cal_shading_verify_upload", "position", "scan_setup",
)


def _dual_module(dpi):
    from of135i import device
    return device.dual_tables(dpi)


def _dual_cal_buffers(t):
    """Canned buffers for a dual-light scan over FakeUsbDevice, keyed by
    descriptor length: the 2-line white buffer (IR line first) crafted so
    gain_codes() on the visible line returns the trace's own codes, and
    the alternating 256-line shading measurement served to both the
    measure and the verify read (test_dpi.py's construction)."""
    import test_dpi
    gc = t.CAL_GAIN_CHECK_A
    codes = tuple(gc.ops[gc.injections[k][1]].data[gc.injections[k][2]]
                  for k in ("gain_r", "gain_g", "gain_b"))
    a_off = test_dpi._captured_shading_offsets(t, t.CAL_SHADING_UPLOAD, "shading_table_a")
    b_off = test_dpi._captured_shading_offsets(t, t.CAL_SHADING_UPLOAD, "shading_table_b")
    meas = test_dpi._synthetic_measurement(t, b_off, a_off)
    white, white_len = test_dpi._white_buffer(t, codes)
    return {white_len: deque([white]), len(meas): deque([meas, meas])}


def _run_dual_python(t, dpi, n_chunks=2):
    """The driver's scan(frame=1, ir=True, dpi=dpi, lines=<two chunks>) over
    FakeUsbDevice, every _exec_ops call sliced out of the wire log."""
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_dual_cal_buffers(t))
    scanner = Scanner(UsbIo(fake))
    calls: list[tuple[str, int, int, list]] = []
    orig_exec_ops = scanner._exec_ops

    def wrapped(ops, *a, **kw):
        start = len(fake.wire_log)
        result = orig_exec_ops(ops, *a, **kw)
        end = len(fake.wire_log)
        calls.append((scanner.session.phase, start, end, ops))
        return result

    scanner._exec_ops = wrapped  # type: ignore[method-assign]
    n_lines = n_chunks * t.LINES_PER_CHUNK
    with fast_time():
        scanner.initialize(ir=True, dpi=dpi)
        scanner.scan(frame=1, ir=True, dpi=dpi, lines=n_lines)
    return fake, calls, n_lines


def test_dual_programs_match_python_replayer():
    """Hook 8 (docs/sane-hook8-dual.md section 6): wire equality of every
    op program of the five dual-light profiles against the driver's own
    transfers for the same profile -- the calibration chain with its
    injections recovered from the patched ops the driver sent (offset
    codes, gain codes, the four shading tables), POSITION with the
    profile's own FEEDL, the scan setup with the driver's three-byte
    line count, and the semantic PARK. Two image chunks are scanned so
    7200 dpi stays cheap; the chunk transfers themselves are covered by
    test_dual_image_chunks."""
    probe = _build_probe()
    if probe is None:
        print("test_dual_programs_match_python_replayer SKIPPED (no g++)")
        return "skipped"

    summary = []
    for profile_name, dpi in DUAL_PROFILES:
        t = _dual_module(dpi)
        fake, calls, n_lines = _run_dual_python(t, dpi)

        def calls_named(name):
            return [c for c in calls if c[0] == name]

        # scan is one _exec_ops call covering setup + chunks + tail;
        # cut it at the first image descriptor like the plain test.
        scan_calls = calls_named("scan")
        assert len(scan_calls) == 1, (profile_name, len(scan_calls))
        _, s_start, s_end, s_ops = scan_calls[0]
        first_desc = next(i for i, op in enumerate(s_ops)
                          if op.kind == "cw" and op.wv == 0x0082 and op.data == t.IMAGE_DESC_DATA)
        verify_calls = calls_named("cal_shading_verify")
        assert len(verify_calls) == 2, (profile_name, len(verify_calls))
        split_at = t.CAL_SHADING_VERIFY.split_at

        program_calls = {}
        for name in DUAL_PROGRAM_NAMES:
            if name == "cal_shading_verify":
                program_calls[name] = verify_calls[0]
            elif name == "cal_shading_verify_upload":
                program_calls[name] = verify_calls[1]
            elif name == "scan_setup":
                program_calls[name] = ("scan", s_start, s_start + first_desc, s_ops)
            else:
                cs = calls_named(name)
                assert len(cs) == 1, (profile_name, name, len(cs))
                program_calls[name] = cs[0]

        total = 0
        feedl = t.feedl_for_frame(1)
        for prog_name in DUAL_PROGRAM_NAMES:
            _, start, end, ops = program_calls[prog_name]
            py_transfers = _python_transfers(fake.wire_log[start:end])
            injects: dict[str, int] = {}
            bulk_injects: dict[str, bytes] = {}
            phase = {"cal_gain_check_a": t.CAL_GAIN_CHECK_A,
                     "cal_shading_measure": t.CAL_SHADING_MEASURE,
                     "cal_shading_upload": t.CAL_SHADING_UPLOAD,
                     "cal_shading_verify_upload": t.CAL_SHADING_VERIFY}.get(prog_name)
            if prog_name in ("cal_gain_check_a", "cal_shading_measure"):
                for name, spec in phase.injections.items():
                    _, idx, off = spec
                    injects[name] = ops[idx].data[off]
            elif prog_name == "cal_shading_upload":
                for name in ("shading_table_a", "shading_table_b"):
                    _, idxs = phase.injections[name]
                    bulk_injects[name] = b"".join(ops[i].data for i in idxs)
            elif prog_name == "cal_shading_verify_upload":
                for name in ("shading_table2_a", "shading_table2_b"):
                    _, idxs = phase.injections[name]
                    bulk_injects[name] = b"".join(ops[i - split_at].data for i in idxs)
            elif prog_name == "position":
                injects = {"feedl_hi": (feedl >> 16) & 0xFF, "feedl_mid": (feedl >> 8) & 0xFF,
                           "feedl_lo": feedl & 0xFF}
            elif prog_name == "scan_setup":
                injects = {"lines_top": (n_lines >> 16) & 0xFF, "lines_hi": (n_lines >> 8) & 0xFF,
                           "lines_lo": n_lines & 0xFF}

            rc, out, err = _run_probe_program(
                probe, profile_name, prog_name,
                injects=injects or None, bulk_injects=bulk_injects or None)
            assert rc == 0, (profile_name, prog_name, out, err)
            assert _lines(out)[-1].startswith("DONE"), (profile_name, prog_name, out)
            cpp_transfers = _parse_probe_transfers(out)
            py_cmp, cpp_cmp = py_transfers, cpp_transfers
            assert py_cmp == cpp_cmp, (
                f"{profile_name}/{prog_name}: python and C++ transfer logs differ\n"
                f"python ({len(py_cmp)}): {py_cmp[:12]}\n"
                f"cpp    ({len(cpp_cmp)}): {cpp_cmp[:12]}")
            total += len(py_cmp)
        summary.append(f"{profile_name}: {total}")
    print(f"test_dual_programs_match_python_replayer OK "
          f"({len(DUAL_PROGRAM_NAMES)} programs x 5 profiles; transfers {', '.join(summary)})")


def test_dual_park_programs_match_park_semantic():
    """Each dual profile's "park" program against the driver's
    park_semantic(t=<that profile's module>, ir=True) over the scripted
    park fake, as test_park_program_matches_park_semantic does for
    plain3600: the profile's own two 0x8b payloads, the same RMW values."""
    probe = _build_probe()
    if probe is None:
        print("test_dual_park_programs_match_park_semantic SKIPPED (no g++)")
        return "skipped"
    reg15, reg35 = 0x90, 0xFB
    reg32_seq = (0x81, 0x95)
    status_seq = [b"\xe8\x55"]
    counts = []
    for profile_name, dpi in DUAL_PROFILES:
        t = _dual_module(dpi)
        dev = _ParkWireDev(reg15=reg15, reg32_seq=reg32_seq, reg35=reg35, status_seq=status_seq)
        scanner = Scanner(_ParkWireIo(dev))
        with fast_time():
            scanner.park_semantic(t=t, ir=True)
        py_transfers = dev.transfers
        assert py_transfers[0] == ("R", 0x04, 0x008E, 0x0122, 2), py_transfers[0]
        py_transfers = py_transfers[1:]
        script = [
            f"rmw_read_at 0 {reg15:02x}", f"rmw_read_at 1 {reg32_seq[0]:02x}",
            f"rmw_read_at 2 {reg35:02x}", f"rmw_read_at 3 {reg32_seq[1]:02x}",
            f"masked_poll_at 0 {reg35:02x}55", f"masked_poll_at 1 {status_seq[0].hex()}",
        ]
        rc, out, err = _run_probe_program(probe, profile_name, "park", script)
        assert rc == 0, (profile_name, out, err)
        assert _lines(out)[-1].startswith("DONE"), (profile_name, out)
        cpp_transfers = _parse_probe_transfers(out)
        assert py_transfers == cpp_transfers, (
            f"{profile_name}/park: python and C++ transfer logs differ\n"
            f"python ({len(py_transfers)}): {py_transfers}\n"
            f"cpp    ({len(cpp_transfers)}): {cpp_transfers}")
        counts.append(f"{profile_name}={len(py_transfers)}")
    print(f"test_dual_park_programs_match_park_semantic OK ({', '.join(counts)})")


def test_dual_image_chunks():
    """The dual profiles' image chunks: the driver's descriptor + bulk IN
    per chunk (two chunks scanned) against read_image_chunk() over the
    fake wire with the profile's chunk length."""
    probe = _build_probe()
    if probe is None:
        print("test_dual_image_chunks SKIPPED (no g++)")
        return "skipped"
    for profile_name, dpi in DUAL_PROFILES:
        t = _dual_module(dpi)
        fake, calls, n_lines = _run_dual_python(t, dpi)
        _, s_start, s_end, s_ops = [c for c in calls if c[0] == "scan"][0]
        py = _python_transfers(fake.wire_log[s_start:s_end])
        descs = [i for i, x in enumerate(py)
                 if x[0] == "W" and x[2] == 0x0082 and x[4] == t.IMAGE_DESC_DATA]
        assert len(descs) >= 2, (profile_name, len(descs))
        # the two image chunks: descriptor, ack read, bulk IN, each. The
        # driver reads a chunk in USB fragments; the C++ side asks for the
        # chunk in one request -- the same bulk stream (the accepted
        # equivalence of docs/sane-hook5-frame.md's status section).
        raw_chunks = py[descs[0]:descs[1] + 40]
        py_chunks = []
        for x in raw_chunks:
            if x[0] == "B" and py_chunks and py_chunks[-1][0] == "B":
                py_chunks[-1] = ("B", py_chunks[-1][1] + x[1])
            else:
                py_chunks.append(x)
        py_chunks = py_chunks[:6]
        r = subprocess.run([str(probe), "image_chunks", "1", str(t.IMAGE_CHUNK_LEN),
                            str(t.IMAGE_CHUNK_LEN)], capture_output=True, text=True)
        assert r.returncode == 0, (profile_name, r.stdout, r.stderr)
        cpp = _parse_probe_transfers(r.stdout)
        assert py_chunks == cpp, (profile_name, py_chunks[:6], cpp[:6])
    print("test_dual_image_chunks OK (2 chunks x 5 profiles, descriptor wIndex 8 then 0)")


def test_shading_table2_dual_reference_vectors():
    """gl126::shading_table2_dual() byte-identical to calibrate.
    shading_table2_dual() on synthetic buffers, both targets, two widths."""
    probe = _build_probe()
    if probe is None:
        print("test_shading_table2_dual_reference_vectors SKIPPED (no g++)")
        return "skipped"
    rng = np.random.default_rng(8)
    with tempfile.TemporaryDirectory() as td:
        for width in (876, 5184):
            lines = 128
            white = rng.integers(20000, 65000, size=(lines, width, 3), dtype=np.uint16)
            dark = rng.integers(100, 500, size=(lines, width, 3), dtype=np.uint16)
            for target in (calibrate.SHADING2_TARGET_A, calibrate.SHADING2_TARGET_B):
                want = calibrate.shading_table2_dual(white, dark, width=width, target=target)
                wp, dp, op = (Path(td) / n for n in ("w.bin", "d.bin", "o.bin"))
                wp.write_bytes(white.astype("<u2").tobytes())
                dp.write_bytes(dark.astype("<u2").tobytes())
                r = subprocess.run([str(probe), "shading_table2_dual", str(wp), str(dp),
                                    str(lines), str(width), repr(target), str(op)],
                                   capture_output=True, text=True)
                assert r.returncode == 0, (r.stdout, r.stderr)
                got = op.read_bytes()
                assert got == want, (width, target, len(got), len(want))
            # alternate_lines == arr[p::2]
            arr = rng.integers(0, 65535, size=(256, width, 3), dtype=np.uint16)
            bp = Path(td) / "b.bin"
            bp.write_bytes(arr.astype("<u2").tobytes())
            for parity in (0, 1):
                r = subprocess.run([str(probe), "alternate_lines", str(bp), "256", str(width),
                                    str(parity), str(op)], capture_output=True, text=True)
                assert r.returncode == 0, (r.stdout, r.stderr)
                assert op.read_bytes() == arr[parity::2].astype("<u2").tobytes(), (width, parity)
    print("test_shading_table2_dual_reference_vectors OK (2 widths x 2 targets, alternate_lines x 2)")


def test_frame_geometry_all_profiles():
    """frame_geometry() and the per-profile FEEDL against the Python tables:
    the plain profile reads its whole register value (5137), a dual profile
    reads chunk_count x lines_per_chunk (ir3600: 10544 of 10622), one line
    in two is the image, the colour shift is 24 lines x dpi / 3600 and the
    delivered count is the image less the shift."""
    probe = _build_probe()
    if probe is None:
        print("test_frame_geometry_all_profiles SKIPPED (no g++)")
        return "skipped"
    rows = []
    for profile_name, dpi in (("plain3600", 3600),) + DUAL_PROFILES:
        t = tables if profile_name == "plain3600" else _dual_module(dpi)
        r = subprocess.run([str(probe), "geometry", profile_name], capture_output=True, text=True)
        assert r.returncode == 0, (profile_name, r.stdout, r.stderr)
        got = dict(kv.split("=") for kv in _lines(r.stdout)[-1].split()[1:])
        got = {k: int(v) for k, v in got.items()}
        dual = profile_name != "plain3600"
        read_lines = t.IMAGE_CHUNK_COUNT * t.LINES_PER_CHUNK if dual else t.DEFAULT_LINES
        image_lines = read_lines // 2 if dual else read_lines
        shift = 24 * dpi // 3600
        assert shift == 2 * round(24 * dpi / 7200), (profile_name, shift)  # image.align_channels
        want = {
            "dual": int(dual), "width": t.IMAGE_WIDTH, "wire_lines": t.DEFAULT_LINES,
            "read_lines": read_lines, "image_lines": image_lines, "shift_lines": shift,
            "delivered_lines": image_lines - shift, "chunk_len": t.IMAGE_CHUNK_LEN,
            "chunk_count": -(-(read_lines * t.IMAGE_WIDTH * 6) // t.IMAGE_CHUNK_LEN),
            "feedl_frame1": t.FEEDL_FRAME1, "feedl_pitch": t.FEEDL_PITCH,
        }
        assert got == want, (profile_name, got, want)
        for frame in (1, 4, 5, 6):
            r = subprocess.run([str(probe), "feedl", str(frame), profile_name],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            assert int(_lines(r.stdout)[-1].split()[0].split("=")[1]) == t.feedl_for_frame(frame)
        # Frames 5/6 = FEEDL_FRAME1 + (n-1)*FEEDL_PITCH, spelled out (not
        # just cross-checked against the Python side): 49783/60543 for the
        # plain profile's 6743 base, 49786/60546 for every dual profile's
        # 6746 base (of135i/holder.py's evidence section).
        want5, want6 = ((49783, 60543) if profile_name == "plain3600" else (49786, 60546))
        assert t.feedl_for_frame(5) == want5 and t.feedl_for_frame(6) == want6, (
            profile_name, t.feedl_for_frame(5), t.feedl_for_frame(6))
        rows.append(f"{profile_name} {want['wire_lines']}->{want['read_lines']}->"
                    f"{want['delivered_lines']}")
    assert got["read_lines"] == 21248  # 7200 dpi, the last profile
    print(f"test_frame_geometry_all_profiles OK ({'; '.join(rows)})")

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
        test_gain_programs_match_python_replayer,
        test_missing_injection_fails_before_any_transfer,
        test_multi_chunk_bulk_short_second_chunk,
        test_gain_codes_reference_vectors,
        test_percentile_matches_numpy,
        test_warmup_policy,
        test_shading_programs_match_python_replayer,
        test_shading_bulk_out_ops_carry_no_captured_data,
        test_poll_class_waits_then_continues,
        test_poll_class_timeout_fails_closed,
        test_short_bulk_out_fails_closed,
        test_missing_bulk_injection_fails_before_any_transfer,
        test_bulk_injection_too_long_is_refused,
        test_shading_table_reference_vectors,
        test_position_and_scan_setup_match_python_replayer,
        test_image_chunks_match_python_replayer,
        test_park_program_matches_park_semantic,
        test_poll_masked_waits_then_continues,
        test_poll_masked_timeout_fails_closed,
        test_feedl_and_position_budget,
        test_position_frames_2_to_6_match_python_replayer,
        test_feedl_for_frame_refuses_frame_7_and_frame_0,
        test_scan_pass_complete_then_park,
        test_scan_pass_aborted_never_parks,
        test_scan_pass_park_failure_is_terminal,
        test_dual_programs_match_python_replayer,
        test_dual_park_programs_match_park_semantic,
        test_dual_image_chunks,
        test_shading_table2_dual_reference_vectors,
        test_frame_geometry_all_profiles,
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
