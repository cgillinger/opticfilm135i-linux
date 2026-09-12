"""Regression test for the SANE calibration-cache decision (the B1 item):
ordinary scanning without --force-calibration must still calibrate.

The genesys core skips calibration when genesys_restore_calibration() finds a
compatible cache. GL126's begin_scan requires THIS sane_start's own
offset->gain->shading hooks (CalStage::ShadingDone); a restored cache skipped
them, so begin_scan refused the scan -- which is why --force-calibration was
needed. The fix (of135i/gl126-integration.patch, genesys_start_scan) gates the
restore off for GL126 so calibration always runs; other ASICs are untouched.

This drives the REAL public flow sane_open -> sane_start in the backend's test
mode (enable_testing_mode + TestScannerInterface, no USB), via
tests/gl126_calibration_cache_probe.cpp, and observes whether calibration was
ENTERED. genesys_flatbed_calibration records the progress message
"offset_calibration" just before the offset hook runs; in test mode that hook
throws at once (reg 0x01 != idle-homed), so:
    PROGRESS == "offset_calibration"  <=> calibration was entered (correct)
    PROGRESS == "" (or other)          <=> calibration was skipped (the bug)
This was confirmed to discriminate: reverting the one-line fix and rebuilding
makes the compatible-cache case report an empty progress message (calibration
skipped, begin_scan then refuses).

Needs g++, the sane-backends checkout (SANE_BACKENDS_DIR, else a sibling
`sane-backends/`), and a built backend/.libs/libsane-genesys.so. Skips cleanly
(not fails) when any is absent -- like tests/test_sane_open_params.py.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE_SRC = REPO / "tests" / "gl126_calibration_cache_probe.cpp"
PATCH = REPO / "sane" / "gl126-integration.patch"

_probe_bin = None
_build_attempted = False
_skip_reason = None

GL124_DEVICE = "04a9:1909"   # a GL124 model in the backend's USB tables


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
    binary = str(Path(tempfile.mkdtemp(prefix="gl126-cache-")) / "cacheprobe")
    cmd = [gxx, "-std=gnu++11", "-Wall", "-Wextra",
           "-DHAVE_CONFIG_H", "-DBACKEND_NAME=genesys",
           "-I", str(sb), "-I", str(g), "-I", str(sb / "backend"),
           "-I", str(sb / "include"), "-I", str(sb / "include" / "sane"),
           str(PROBE_SRC),
           "-L", str(libs), "-lsane-genesys", f"-Wl,-rpath,{libs}",
           "-o", binary]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("failed to build the gl126 calibration-cache probe:\n"
                             f"{' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    _probe_bin = binary
    return _probe_bin


def _run(probe, case, device=None):
    """Run the probe for `case` with an isolated empty HOME (so no real
    on-disk calibration file is loaded). Returns a dict of parsed fields."""
    cmd = [probe, case]
    if device is not None:
        cmd.append(device)
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, HOME=home)
        env.pop("SANE_DEBUG_GENESYS", None)
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, f"probe exit {r.returncode}: {r.stdout}\n{r.stderr}"
    out = {}
    for line in r.stdout.splitlines():
        m = re.match(r"STATUS(\d*) (-?\d+) PROGRESS\1 (.*)", line)
        if m:
            suffix = m.group(1)
            out[f"status{suffix}"] = int(m.group(2))
            out[f"progress{suffix}"] = m.group(3).strip()
    assert "status" in out, f"no STATUS line: {r.stdout!r}"
    return out


def _skip():
    print(f"SKIP: {_skip_reason}")
    return "skipped"


def test_no_cache_calibrates():
    """No cache, no --force-calibration: calibration is entered."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, "nocache")
    assert r["progress"] == "offset_calibration", r
    return True


def test_compatible_cache_still_calibrates():
    """THE regression: a previously compatible cache must NOT let GL126 skip
    calibration. Calibration is still entered (progress 'offset_calibration');
    on the pre-fix build this reported an empty progress -- calibration skipped,
    begin_scan then refused the scan."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, "cache")
    assert r["progress"] == "offset_calibration", (
        "a compatible cache bypassed calibration (the B1 bug)", r)
    return True


def test_force_calibration_still_calibrates():
    """Explicit --force-calibration keeps working (calibration entered)."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, "force")
    assert r["progress"] == "offset_calibration", r
    return True


def test_two_consecutive_scans_each_calibrate():
    """A previous scan's calibration is not enough for the next: two
    consecutive sane_start calls (a compatible cache present for each) both
    enter calibration."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, "twice")
    assert r["progress"] == "offset_calibration", r
    assert r.get("progress2") == "offset_calibration", (
        "the second scan reused the first scan's calibration", r)
    return True


def test_other_model_calibration_flow_intact():
    """A non-GL126 model (a GL124 device) is untouched: without a cache the
    shared genesys_start_scan flow calibrates normally and the scan starts
    (STATUS 0), reaching a real calibration step. The fix only adds a
    GL126-gated conjunct, so this path is byte-for-byte the original for other
    ASICs (see test_fix_is_gl126_scoped_in_source)."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, "nocache", device=GL124_DEVICE)
    assert r["status"] == 0, ("GL124 no-cache scan did not start cleanly", r)
    assert r["progress"] and r["progress"] != "offset_calibration", (
        "GL124 calibration flow did not run as before", r)
    return True


def test_fix_is_gl126_scoped_in_source():
    """The recorded fix (sane/gl126-integration.patch) gates the cache restore
    on GL126 only -- a conjunct that leaves every other ASIC evaluating the
    original genesys_restore_calibration() call unchanged. No compiler needed."""
    src = PATCH.read_text()
    # The recorded fix replaces the unconditional restore call with a
    # GL126-gated bool: the old line is removed (a '-' hunk line) and the gated
    # decision is added (a '+' hunk line). Verifying both proves the change is
    # exactly the GL126-scoped conjunct -- other ASICs still evaluate the
    # original genesys_restore_calibration() call.
    assert "-  if (!genesys_restore_calibration (dev, sensor))" in src, \
        "the original unconditional restore call is not shown as removed in the patch"
    assert "+  bool restored = dev->model->asic_type != AsicType::GL126 &&" in src, \
        "the GL126-scoped restore gate is not added in the patch"
    assert "+                  genesys_restore_calibration(dev, sensor);" in src, \
        "the gated restore call is not added in the patch"
    assert "+  if (!restored)" in src, "the gated branch guard is not added in the patch"
    print("test_fix_is_gl126_scoped_in_source OK")
    return True


def main():
    tests = [
        test_no_cache_calibrates,
        test_compatible_cache_still_calibrates,
        test_force_calibration_still_calibrates,
        test_two_consecutive_scans_each_calibrate,
        test_other_model_calibration_flow_intact,
        test_fix_is_gl126_scoped_in_source,
    ]
    passed = skipped = 0
    for t in tests:
        if t() == "skipped":
            skipped += 1
        else:
            passed += 1
            name = t.__name__
            # the source test prints its own OK; give the others a line too
            if name != "test_fix_is_gl126_scoped_in_source":
                print(f"{name} OK")
    if skipped:
        print(f"\n{passed} tests passed, {skipped} skipped.")
    else:
        print(f"\n{passed} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
