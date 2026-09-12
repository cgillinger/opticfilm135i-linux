"""Regression test for the SANE open/parameter path (the 2026-09-11 bug).

sane_open failed before any scan could start: genesys' init_options runs
calc_parameters once with its hardcoded default (mode GRAY, colour filter
GREEN) before the frontend applies --mode Color, and gl126's
calculate_scan_session pinned the colour profile for that default and threw
the colour-shift invariant (GRAY gives max_color_shift_lines 0, the profile
expects the ld_shift). The whole sane_open returned SANE_STATUS_INVAL.

The rest of the offline suite exercises gl126_ops standalone and the
geometry functions, never the integrated calculate_scan_session (it needs
compute_session, the sensor tables and the model -- the whole backend), so
the bug reached hardware. This test closes that gap: it builds a probe
(tests/gl126_session_probe.cpp) that links the BUILT libsane-genesys.so and
calls the real calculate_scan_session on a real gl126 device + sensor, for
the option-init default, the Color transition, host-side gray, and an
actual single-channel gray request. Case 1 (the default) printed THROW on
the buggy build and must print OK now; that is the regression guard.

Needs g++, the sane-backends checkout (SANE_BACKENDS_DIR, else a sibling
`sane-backends/`), and a built backend/.libs/libsane-genesys.so. Skips
cleanly (not fails) when any is absent, since a fresh checkout or CI has no
built backend -- exactly like test_sane_ops skips without g++.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE_SRC = REPO / "tests" / "gl126_session_probe.cpp"

_probe_bin = None
_build_attempted = False
_skip_reason = None


def _sane_backends_dir() -> Path | None:
    env = os.environ.get("SANE_BACKENDS_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(REPO.parent / "sane-backends")
    for c in candidates:
        if (c / "backend" / "genesys" / "low.h").exists():
            return c
    return None


def _built_so(sb: Path) -> Path | None:
    for name in ("libsane-genesys.so", "libsane-genesys.so.1.4.0"):
        p = sb / "backend" / ".libs" / name
        if p.exists():
            return p
    return None


def _build_probe():
    """Compile the session probe against the built .so once; return the
    binary path, or None (setting _skip_reason) when the toolchain, the
    sane-backends checkout, or the built .so is missing."""
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
    binary = str(Path(tempfile.mkdtemp(prefix="gl126-session-")) / "sessprobe")
    cmd = [gxx, "-std=gnu++11", "-Wall", "-Wextra",
           "-DHAVE_CONFIG_H", "-DBACKEND_NAME=genesys",
           "-I", str(sb), "-I", str(g), "-I", str(sb / "backend"),
           "-I", str(sb / "include"), "-I", str(sb / "include" / "sane"),
           str(PROBE_SRC),
           "-L", str(libs), "-lsane-genesys", f"-Wl,-rpath,{libs}",
           "-o", binary]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("failed to build the gl126 session probe:\n"
                             f"{' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    _probe_bin = binary
    return _probe_bin


def _run(probe, dpi, mode, filt, method, frame=None):
    cmd = [probe, str(dpi), mode, filt, method]
    if frame is not None:
        cmd.append(str(frame))
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"probe exit {r.returncode}: {r.stdout}\n{r.stderr}"
    line = r.stdout.strip()
    if line.startswith("THROW"):
        return {"throw": True, "msg": line[6:]}
    assert line.startswith("OK"), f"unexpected probe output: {line!r}"
    kv = dict(p.split("=", 1) for p in line.split()[1:])
    return {"throw": False, **{k: int(v) for k, v in kv.items()}}


def _skip():
    print(f"SKIP: {_skip_reason}")
    return "skipped"


def test_option_init_gray_default_does_not_throw():
    """The exact configuration genesys computes inside sane_open before the
    frontend applies --mode Color: mode GRAY, colour filter GREEN. This threw
    on the buggy build (sane_open failed); it must be tolerant now, reporting
    the raw window and NOT pinning gray geometry as the capture."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, 600, "gray", "green", "visible")
    assert not r["throw"], f"the option-init GRAY default threw again: {r.get('msg')}"
    assert r["channels"] == 1, r          # not expanded, not a capture
    assert r["max_shift"] == 0, r         # no colour shift pinned
    return True


def test_color_pins_plain3600_geometry():
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, 3600, "color", "none", "visible", 1)
    assert not r["throw"], r.get("msg")
    assert r["channels"] == 3, r
    assert r["pixels"] == 3762, r         # pinned to the full sensor width
    assert r["lines"] == 5335, r          # plain3600 frame 1 delivered lines
    assert r["max_shift"] == 24, r        # the model's ld_shift
    assert r["output_lines"] == r["lines"] + r["max_shift"], r
    return True


def test_host_side_gray_pins_like_color():
    """HOST_SIDE_GRAY: a Gray scan with colour filter NONE is scanned RGB and
    reduced on the host, so compute_session expands channels 1->3 and the
    session is the same capture as Color -- Gray is supported, not refused."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    color = _run(probe, 3600, "color", "none", "visible", 1)
    gray = _run(probe, 3600, "gray", "none", "visible", 1)
    assert not gray["throw"], gray.get("msg")
    assert gray["channels"] == 3, gray
    for k in ("channels", "lines", "pixels", "max_shift", "output_lines"):
        assert gray[k] == color[k], (k, gray, color)
    return True


def test_unsupported_single_channel_gray_is_tolerant_not_pinned():
    """An actual single-channel gray request (a colour filter other than
    NONE) is not a capture this backend performs. calculate_scan_session must
    stay tolerant (no throw, so option-init parameter queries never fail) and
    must NOT pin the colour geometry -- the raw window is reported, never used
    as USB geometry. offset_calibration refuses it before the first write."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, 3600, "gray", "green", "visible", 1)
    assert not r["throw"], r.get("msg")
    assert r["channels"] == 1, r          # not expanded (colour filter != NONE)
    assert r["pixels"] != 3762, r         # NOT pinned to the capture width
    assert r["max_shift"] == 0, r
    return True


def test_ir_pins_dual_geometry():
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, 3600, "gray", "none", "ir", 1)
    assert not r["throw"], r.get("msg")
    assert r["pixels"] == 5184, r         # IR dual width
    assert r["max_shift"] == 0, r         # IR is cropped, not shifted
    return True


def test_dpi2400_delivers_square_width_raw_unchanged():
    """The dual2400 anisotropy fix (docs/sane-port.md): the 2400 dpi
    profile reads the sensor at 3600 dpi across (raw width 5256) but the
    transport advances at 2400 dpi, so square-pixel viewers stretched the
    delivered image 1.5x. The backend now delivers the sensor axis scaled
    to 5256*2400/3600 = 3504 px via the core's ImagePipelineNodeScaleRows,
    while the RAW read path is untouched:
      - params.pixels (the raw window) stays 5256, and output_line_bytes_
        raw stays 5256*3*2 -- the USB byte/chunk bookkeeping is unchanged;
      - requested_pixels and the pipeline's output width are 3504 -- what
        the frontend receives and sane_get_parameters reports.
    Only dpi2400 changes; every other profile delivers its raw width."""
    probe = _build_probe()
    if probe is None:
        return _skip()
    r = _run(probe, 2400, "color", "none", "visible", 1)
    assert not r["throw"], r.get("msg")
    assert r["pixels"] == 5256, r                 # RAW window unchanged
    assert r["raw_line_bytes"] == 5256 * 3 * 2, r  # RAW byte accounting unchanged
    assert r["requested"] == 3504, r              # scaled sensor axis
    assert r["delivered"] == 3504, r              # pipeline (frontend) width
    # Every isotropic profile delivers its raw width -- no scaling node, no
    # change from before the fix.
    for dpi, method, filt, mode, raw_w in (
            (3600, "visible", "none", "color", 3762),
            (600,  "visible", "none", "color", 876),
            (1200, "visible", "none", "color", 1752),
            (7200, "visible", "none", "color", 10512),
            (3600, "ir",      "none", "gray",  5184)):
        r = _run(probe, dpi, mode, filt, method, 1)
        assert not r["throw"], (dpi, r.get("msg"))
        assert r["pixels"] == raw_w, (dpi, r)
        assert r["requested"] == raw_w, (dpi, r)
        assert r["delivered"] == raw_w, (dpi, r)
    return True


def main():
    tests = [
        test_option_init_gray_default_does_not_throw,
        test_color_pins_plain3600_geometry,
        test_host_side_gray_pins_like_color,
        test_unsupported_single_channel_gray_is_tolerant_not_pinned,
        test_ir_pins_dual_geometry,
        test_dpi2400_delivers_square_width_raw_unchanged,
    ]
    passed = skipped = 0
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
