#!/usr/bin/env python3
"""tools/sane_install.sh against a staging root: does it install what we
claim, and does uninstall put the distribution back exactly as it was?

The install path is the one place in this project that writes into files a
distribution package owns (the `libsane-genesys.so.1` symlink in the SANE
backend directory and `/etc/sane.d/genesys.conf`). The promise in
docs/sane-install.md is narrow and testable:

  * the distribution's own `libsane-genesys.so.1.*` is never modified;
  * the backend we install lands under its own name, so nothing is
    overwritten;
  * only the symlink is repointed, and its original target is recorded;
  * the USB id goes into genesys.conf between markers;
  * `uninstall` restores the tree byte for byte.

Every test runs against a synthetic root (OF135I_SANE_ROOT), never the
system: no root privileges, no distribution files touched, no scanner.
The staged library is a small stand-in file, not the 27 MB build -- what
is under test is the installer's file handling, not the backend.

Run with:
    .venv/bin/python tests/test_sane_install.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "sane_install.sh"
DISTRO_SO = "libsane-genesys.so.1.4.0"
OURS = "libsane-genesys-gl126.so.1.4.0"

GENESYS_CONF = """\
# genesys.conf: sample configuration
usb 0x04a9 0x2213

# Plustek OpticFilm 7600i
usb 0x07b3 0x0c3b
"""


def _fake_clone(tmp: Path) -> Path:
    """A sane-backends checkout shaped like the real one: the nine gl126
    sources present under backend/genesys, and a built library carrying a
    gl126 symbol (the installer refuses a build without one)."""
    clone = tmp / "sane-backends"
    (clone / "backend" / "genesys").mkdir(parents=True)
    (clone / "backend" / ".libs").mkdir(parents=True)
    for name in ("gl126.h", "gl126.cpp", "gl126_registers.h", "gl126_tables.h",
                 "gl126_tables.cpp", "gl126_ops.h", "gl126_ops.cpp",
                 "gl126_lock.h", "gl126_lock.cpp"):
        (clone / "backend" / "genesys" / name).write_text(f"/* {name} */\n")
    lib = clone / "backend" / ".libs" / DISTRO_SO
    # `nm -C` on a non-object prints nothing, so give the installer a real
    # (tiny) object file whose symbol table mentions gl126.
    src = tmp / "stub.c"
    src.write_text("int gl126_stub_symbol(void) { return 126; }\n")
    r = subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(lib), str(src)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return clone, None
    return clone, lib


def _fake_root(tmp: Path) -> Path:
    """A system root shaped like Fedora's: the distribution library plus its
    two symlinks, genesys.conf, dll.conf, the udev rule."""
    root = tmp / "root"
    sane = root / "usr" / "lib64" / "sane"
    sane.mkdir(parents=True)
    (root / "etc" / "sane.d").mkdir(parents=True)
    (root / "etc" / "udev" / "rules.d").mkdir(parents=True)
    (sane / DISTRO_SO).write_bytes(b"DISTRIBUTION BACKEND\n")
    os.symlink(DISTRO_SO, sane / "libsane-genesys.so.1")
    os.symlink(DISTRO_SO, sane / "libsane-genesys.so")
    (root / "etc" / "sane.d" / "genesys.conf").write_text(GENESYS_CONF)
    (root / "etc" / "sane.d" / "dll.conf").write_text("net\ngenesys\n")
    (root / "etc" / "udev" / "rules.d" / "60-of135i.rules").write_text("# rule\n")
    return root


def _fingerprint(root: Path) -> list[str]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_symlink():
            out.append(f"{rel} -> {os.readlink(p)}")
        elif p.is_dir():
            out.append(f"{rel}/")
        else:
            out.append(f"{rel} {hashlib.sha256(p.read_bytes()).hexdigest()}")
    return out


def _run(action: str, root: Path, clone: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, OF135I_SANE_ROOT=str(root), SANE_BACKENDS_DIR=str(clone))
    return subprocess.run(["bash", str(SCRIPT), action], capture_output=True,
                          text=True, env=env)


class _Staged:
    """A fresh fake root + fake clone per test."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="of135i-install-"))
        self.clone, lib = _fake_clone(self.tmp)
        self.have_cc = lib is not None
        self.root = _fake_root(self.tmp)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_install_adds_without_overwriting_the_distribution():
    """install lands our library under its own name, repoints only the
    .so.1 symlink, and leaves the distribution's file and its .so
    development link untouched."""
    with _Staged() as s:
        if not s.have_cc:
            print("SKIP: no C compiler to build the stand-in library")
            return "skipped"
        sane = s.root / "usr" / "lib64" / "sane"
        before = (sane / DISTRO_SO).read_bytes()
        r = _run("install", s.root, s.clone)
        assert r.returncode == 0, f"install failed: {r.stdout}{r.stderr}"
        assert (sane / OURS).exists(), "our library was not installed"
        assert os.readlink(sane / "libsane-genesys.so.1") == OURS, \
            "the .so.1 symlink was not repointed at our build"
        assert (sane / DISTRO_SO).read_bytes() == before, \
            "the distribution's library was modified"
        assert os.readlink(sane / "libsane-genesys.so") == DISTRO_SO, \
            "the distribution's development symlink was repointed"
        assert (sane / ".libsane-genesys.so.1.opticfilm135i-orig").read_text().strip() \
            == DISTRO_SO, "the original link target was not recorded"
        print("test_install_adds_without_overwriting_the_distribution OK")


def test_install_adds_the_usb_id_between_markers():
    with _Staged() as s:
        if not s.have_cc:
            print("SKIP: no C compiler to build the stand-in library")
            return "skipped"
        conf = s.root / "etc" / "sane.d" / "genesys.conf"
        assert _run("install", s.root, s.clone).returncode == 0
        text = conf.read_text()
        assert "usb 0x07b3 0x1436" in text, "the 135i USB id was not added"
        assert text.startswith(GENESYS_CONF), "existing genesys.conf content changed"
        assert text.count("BEGIN opticfilm135i-linux") == 1
        assert text.count("END opticfilm135i-linux") == 1
        assert conf.with_suffix(".conf.opticfilm135i-backup").exists(), \
            "no backup of genesys.conf was taken"
        print("test_install_adds_the_usb_id_between_markers OK")


def test_install_is_idempotent():
    """Running install twice must not stack USB-id blocks or lose the
    recorded distribution target (the second run's 'current' link is
    already ours)."""
    with _Staged() as s:
        if not s.have_cc:
            print("SKIP: no C compiler to build the stand-in library")
            return "skipped"
        assert _run("install", s.root, s.clone).returncode == 0
        assert _run("install", s.root, s.clone).returncode == 0
        sane = s.root / "usr" / "lib64" / "sane"
        conf = (s.root / "etc" / "sane.d" / "genesys.conf").read_text()
        assert conf.count("usb 0x07b3 0x1436") == 1, "USB id added twice"
        assert (sane / ".libsane-genesys.so.1.opticfilm135i-orig").read_text().strip() \
            == DISTRO_SO, "the second install overwrote the recorded target with our own"
        print("test_install_is_idempotent OK")


def test_uninstall_restores_the_tree_byte_for_byte():
    with _Staged() as s:
        if not s.have_cc:
            print("SKIP: no C compiler to build the stand-in library")
            return "skipped"
        before = _fingerprint(s.root)
        assert _run("install", s.root, s.clone).returncode == 0
        assert _fingerprint(s.root) != before, "install changed nothing"
        r = _run("uninstall", s.root, s.clone)
        assert r.returncode == 0, f"uninstall failed: {r.stdout}{r.stderr}"
        # the config backup is an install artefact, not distribution state
        (s.root / "etc" / "sane.d" / "genesys.conf.opticfilm135i-backup").unlink()
        after = _fingerprint(s.root)
        assert after == before, (
            "uninstall did not restore the tree:\n"
            f"  only before: {sorted(set(before) - set(after))}\n"
            f"  only after : {sorted(set(after) - set(before))}")
        print("test_uninstall_restores_the_tree_byte_for_byte OK")


def test_install_refuses_an_incomplete_clone():
    """All nine gl126 sources must be symlinked into the clone. The older
    five-file list in the docs built a backend without gl126_ops/gl126_lock;
    the installer refuses that rather than installing a crippled library."""
    with _Staged() as s:
        if not s.have_cc:
            print("SKIP: no C compiler to build the stand-in library")
            return "skipped"
        (s.clone / "backend" / "genesys" / "gl126_ops.cpp").unlink()
        r = _run("install", s.root, s.clone)
        assert r.returncode != 0, "install accepted a clone missing gl126_ops.cpp"
        assert "gl126_ops.cpp" in r.stderr, f"unhelpful error: {r.stderr}"
        sane = s.root / "usr" / "lib64" / "sane"
        assert os.readlink(sane / "libsane-genesys.so.1") == DISTRO_SO, \
            "a refused install still repointed the symlink"
        print("test_install_refuses_an_incomplete_clone OK")


def test_status_reports_a_clean_system_without_root():
    with _Staged() as s:
        r = _run("status", s.root, s.clone)
        assert r.returncode == 0, f"status failed: {r.stdout}{r.stderr}"
        assert "installed lib  : none" in r.stdout
        assert "135i USB id MISSING" in r.stdout
        assert "dll.conf       : genesys enabled" in r.stdout
        print("test_status_reports_a_clean_system_without_root OK")


def main() -> int:
    tests = [
        test_install_adds_without_overwriting_the_distribution,
        test_install_adds_the_usb_id_between_markers,
        test_install_is_idempotent,
        test_uninstall_restores_the_tree_byte_for_byte,
        test_install_refuses_an_incomplete_clone,
        test_status_reports_a_clean_system_without_root,
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
