#!/usr/bin/env python3
"""tools/sane_install.sh against a staging root: does it install what we
claim, does it leave nothing behind when a step fails, and does uninstall
put the distribution back without guessing?

The install path is the one place in this project that writes into files a
distribution package owns (the `libsane-genesys.so.1` symlink in the SANE
backend directory and `/etc/sane.d/genesys.conf`). The promises in
docs/sane-install.md are narrow and testable:

  * the distribution's own `libsane-genesys.so.1.*` is never modified;
  * our backend lands under its own name, so nothing is overwritten;
  * only the symlink is repointed, and the distribution's target recorded;
  * a failure partway through leaves NOTHING applied;
  * uninstall looks at the CURRENT link first: a package update that has
    already taken the link back is left alone, a recorded target that has
    been replaced is followed to the new distribution library, and an
    ambiguous or missing target is refused rather than guessed -- never a
    dangling link, never a library removed while the link points at it;
  * only our marked block leaves genesys.conf: original blank lines and
    edits made after the install are kept, and the pre-install backup is
    never restored over them.

Every test runs against a synthetic root (OF135I_SANE_ROOT), never the
system: no root privileges, no distribution files touched, no scanner. The
staged library is a small stand-in, not the 27 MB build -- what is under
test is the installer's file handling. `verify` is exercised against a stub
`scanimage` (SCANIMAGE=...), so no enumeration ever happens.

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
NEWER_SO = "libsane-genesys.so.1.5.0"          # what a package update brings
OURS = "libsane-genesys-gl126.so.1.4.0"
LINK = "libsane-genesys.so.1"
ORIG_MARK = ".libsane-genesys.so.1.opticfilm135i-orig"

# Trailing blank lines on purpose: an earlier version of the uninstaller ate
# them along with its own block.
GENESYS_CONF = """\
# genesys.conf: sample configuration
usb 0x04a9 0x2213

# Plustek OpticFilm 7600i
usb 0x07b3 0x0c3b

"""


def _fake_clone(tmp: Path, config_dirs: str = ".:/etc/sane.d"):
    """A sane-backends checkout shaped like the real one: the nine gl126
    sources under backend/genesys, and a built library carrying a gl126
    symbol (the installer refuses a build without one) and the
    sanei_config DEFAULT_DIRS string the installer checks."""
    clone = tmp / "sane-backends"
    (clone / "backend" / "genesys").mkdir(parents=True)
    (clone / "backend" / ".libs").mkdir(parents=True)
    for name in ("gl126.h", "gl126.cpp", "gl126_registers.h", "gl126_tables.h",
                 "gl126_tables.cpp", "gl126_ops.h", "gl126_ops.cpp",
                 "gl126_lock.h", "gl126_lock.cpp"):
        (clone / "backend" / "genesys" / name).write_text(f"/* {name} */\n")
    lib = clone / "backend" / ".libs" / DISTRO_SO
    src = tmp / "stub.c"
    src.write_text("int gl126_stub_symbol(void) { return 126; }\n"
                   f'const char sane_config_default_dirs[] = "{config_dirs}";\n')
    r = subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(lib), str(src)],
                       capture_output=True, text=True)
    return clone, (lib if r.returncode == 0 else None)


def _fake_root(tmp: Path) -> Path:
    """A system root shaped like Fedora's: the distribution library plus its
    two symlinks, genesys.conf, dll.conf, the udev rule."""
    root = tmp / "root"
    sane = root / "usr" / "lib64" / "sane"
    sane.mkdir(parents=True)
    (root / "etc" / "sane.d").mkdir(parents=True)
    (root / "etc" / "udev" / "rules.d").mkdir(parents=True)
    (sane / DISTRO_SO).write_bytes(b"DISTRIBUTION BACKEND\n")
    os.symlink(DISTRO_SO, sane / LINK)
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


class _Staged:
    """A fresh fake root + fake clone per test."""

    def __init__(self, config_dirs: str = ".:/etc/sane.d"):
        self.config_dirs = config_dirs

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="of135i-install-"))
        self.clone, lib = _fake_clone(self.tmp, self.config_dirs)
        self.have_cc = lib is not None
        self.root = _fake_root(self.tmp)
        self.sane = self.root / "usr" / "lib64" / "sane"
        self.conf = self.root / "etc" / "sane.d" / "genesys.conf"
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run(self, action, env=None):
        e = dict(os.environ, OF135I_SANE_ROOT=str(self.root),
                 SANE_BACKENDS_DIR=str(self.clone))
        e.pop("LD_LIBRARY_PATH", None)
        e.pop("SANE_CONFIG_DIR", None)
        e.update(env or {})
        return subprocess.run(["bash", str(SCRIPT), action], capture_output=True,
                              text=True, env=e)

    def link_target(self) -> str:
        return os.readlink(self.sane / LINK)

    def skip(self) -> bool:
        if not self.have_cc:
            print("SKIP: no C compiler to build the stand-in library")
        return not self.have_cc


def _stub_scanimage(tmp: Path, *, dlopen_path: str | None, device: bool,
                    exit_code: int = 0) -> Path:
    """A stand-in for scanimage -L: prints the dll debug line and optionally a
    device line, then exits. Never touches hardware."""
    lines = []
    if dlopen_path:
        lines.append(f"[dll] load: dlopen()ing `{dlopen_path}'")
    if device:
        lines.append("device `genesys:libusb:001:007' is a PLUSTEK OpticFilm 135i "
                     "film scanner")
    # A quoted heredoc: the output contains backticks and apostrophes, which
    # no amount of echo-quoting survives cleanly.
    body = ""
    if lines:
        body = "cat <<'STUB_EOF'\n" + "\n".join(lines) + "\nSTUB_EOF\n"
    path = tmp / "scanimage-stub"
    path.write_text(f"#!/usr/bin/env bash\n{body}exit {exit_code}\n")
    path.chmod(0o755)
    return path


# --------------------------------------------------------------- install

def test_install_adds_without_overwriting_the_distribution():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        before = (s.sane / DISTRO_SO).read_bytes()
        r = s.run("install")
        assert r.returncode == 0, f"install failed: {r.stdout}{r.stderr}"
        assert (s.sane / OURS).exists(), "our library was not installed"
        assert s.link_target() == OURS, "the .so.1 symlink was not repointed"
        assert (s.sane / DISTRO_SO).read_bytes() == before, \
            "the distribution's library was modified"
        assert os.readlink(s.sane / "libsane-genesys.so") == DISTRO_SO, \
            "the distribution's development symlink was repointed"
        assert (s.sane / ORIG_MARK).read_text().strip() == DISTRO_SO, \
            "the distribution's link target was not recorded"
        print("test_install_adds_without_overwriting_the_distribution OK")


def test_install_adds_the_usb_id_between_markers():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        text = s.conf.read_text()
        assert "usb 0x07b3 0x1436" in text, "the 135i USB id was not added"
        assert text.startswith(GENESYS_CONF), "existing genesys.conf content changed"
        assert text.count("BEGIN opticfilm135i-linux") == 1
        assert text.count("END opticfilm135i-linux") == 1
        assert (s.conf.parent / "genesys.conf.opticfilm135i-backup").exists(), \
            "no backup of genesys.conf was taken"
        print("test_install_adds_the_usb_id_between_markers OK")


def test_install_is_idempotent():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        assert s.run("install").returncode == 0
        assert s.conf.read_text().count("usb 0x07b3 0x1436") == 1, "USB id added twice"
        assert (s.sane / ORIG_MARK).read_text().strip() == DISTRO_SO, \
            "the second install recorded our own library as the distribution target"
        print("test_install_is_idempotent OK")


def test_install_refuses_an_incomplete_clone():
    """All nine gl126 sources must be symlinked into the clone. The older
    five-file list in the docs built a library that does not link; the
    installer refuses rather than installing it."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        (s.clone / "backend" / "genesys" / "gl126_ops.cpp").unlink()
        r = s.run("install")
        assert r.returncode != 0, "install accepted a clone missing gl126_ops.cpp"
        assert "gl126_ops.cpp" in r.stderr, f"unhelpful error: {r.stderr}"
        assert s.link_target() == DISTRO_SO, "a refused install still repointed the link"
        print("test_install_refuses_an_incomplete_clone OK")


def test_install_refuses_a_library_built_for_the_wrong_config_dir():
    """The defect that reached hardware on 2026-09-13: the clone was
    configured without --sysconfdir=/etc, so the library looked for
    genesys.conf in /usr/local/etc/sane.d. scanimage still worked (libsane
    is in the global symbol scope and interposes its own sanei_config), but
    digiKam -- which loads SANE through a dlopen'd Qt plugin with
    RTLD_LOCAL -- got the library's own path and found no config at all.
    The installer must catch this before anything is changed."""
    with _Staged(config_dirs=".:/usr/local/etc/sane.d") as s:
        if s.skip():
            return "skipped"
        before = _fingerprint(s.root)
        r = s.run("install")
        assert r.returncode != 0, "install accepted a library with the wrong config path"
        assert "/usr/local/etc/sane.d" in r.stderr and "sysconfdir" in r.stderr, \
            f"unhelpful error: {r.stderr}"
        assert _fingerprint(s.root) == before, "a refused install changed the tree"
        print("test_install_refuses_a_library_built_for_the_wrong_config_dir OK")


def test_status_flags_a_wrong_config_dir():
    with _Staged(config_dirs=".:/usr/local/etc/sane.d") as s:
        if s.skip():
            return "skipped"
        r = s.run("status")
        assert r.returncode == 0
        assert "MISMATCH" in r.stdout, r.stdout
        print("test_status_flags_a_wrong_config_dir OK")


def test_install_rolls_back_a_failure_after_the_symlink_changed():
    """The config step runs after the library and the symlink have changed.
    If it fails, nothing may be left applied (the review found a half-done
    install here)."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        before = _fingerprint(s.root)
        r = s.run("install", env={"OF135I_INSTALL_FAIL_AT": "conf"})
        assert r.returncode != 0, "the simulated failure did not fail the install"
        after = _fingerprint(s.root)
        assert after == before, (
            "install left changes behind after a failed step:\n"
            f"  only before: {sorted(set(before) - set(after))}\n"
            f"  only after : {sorted(set(after) - set(before))}")
        print("test_install_rolls_back_a_failure_after_the_symlink_changed OK")


# ------------------------------------------------------------- uninstall

def test_uninstall_restores_the_tree_byte_for_byte():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        before = _fingerprint(s.root)
        assert s.run("install").returncode == 0
        assert _fingerprint(s.root) != before, "install changed nothing"
        r = s.run("uninstall")
        assert r.returncode == 0, f"uninstall failed: {r.stdout}{r.stderr}"
        after = _fingerprint(s.root)
        assert after == before, (
            "uninstall did not restore the tree:\n"
            f"  only before: {sorted(set(before) - set(after))}\n"
            f"  only after : {sorted(set(after) - set(before))}")
        print("test_uninstall_restores_the_tree_byte_for_byte OK")


def test_uninstall_follows_a_package_update_to_the_new_library():
    """A distribution update replaced libsane-genesys.so.1.4.0 with .1.5.0
    while our link was in place. Restoring the recorded target would produce
    a DANGLING link and report success (the review reproduced exactly that)."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        (s.sane / DISTRO_SO).unlink()                       # the update
        (s.sane / NEWER_SO).write_bytes(b"NEWER DISTRIBUTION BACKEND\n")
        r = s.run("uninstall")
        assert r.returncode == 0, f"uninstall failed: {r.stdout}{r.stderr}"
        assert s.link_target() == NEWER_SO, \
            f"link points at {s.link_target()}, not the updated library"
        assert (s.sane / LINK).resolve().exists(), "uninstall left a dangling link"
        assert not (s.sane / OURS).exists(), "our library was not removed"
        print("test_uninstall_follows_a_package_update_to_the_new_library OK")


def test_uninstall_leaves_a_reinstated_distribution_link_alone():
    """The package update also restored its own symlink. Then there is
    nothing to restore -- only our library to remove."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        os.remove(s.sane / LINK)
        os.symlink(DISTRO_SO, s.sane / LINK)                # package took it back
        r = s.run("uninstall")
        assert r.returncode == 0, f"uninstall failed: {r.stdout}{r.stderr}"
        assert s.link_target() == DISTRO_SO, "the reinstated link was changed"
        assert not (s.sane / OURS).exists(), "our library was not removed"
        assert "left untouched" in r.stdout, f"unclear report: {r.stdout}"
        print("test_uninstall_leaves_a_reinstated_distribution_link_alone OK")


def test_uninstall_refuses_when_the_restore_target_is_ambiguous():
    """Recorded target gone and two distribution candidates present: guessing
    could point SANE at the wrong library, so refuse and change nothing --
    in particular, do not remove the library the link still points at."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        (s.sane / DISTRO_SO).unlink()
        (s.sane / NEWER_SO).write_bytes(b"A\n")
        (s.sane / "libsane-genesys.so.1.6.0").write_bytes(b"B\n")
        before = _fingerprint(s.root)
        r = s.run("uninstall")
        assert r.returncode != 0, "uninstall guessed instead of refusing"
        assert _fingerprint(s.root) == before, "a refused uninstall changed the tree"
        assert (s.sane / OURS).exists(), \
            "our library was removed while the link still points at it"
        assert s.link_target() == OURS
        assert "candidates" in r.stderr, f"unhelpful error: {r.stderr}"
        print("test_uninstall_refuses_when_the_restore_target_is_ambiguous OK")


def test_uninstall_refuses_when_no_distribution_library_remains():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        (s.sane / DISTRO_SO).unlink()
        before = _fingerprint(s.root)
        r = s.run("uninstall")
        assert r.returncode != 0, "uninstall produced a dangling link instead of refusing"
        assert _fingerprint(s.root) == before, "a refused uninstall changed the tree"
        assert (s.sane / OURS).exists(), "our library was removed with no replacement"
        assert "dnf reinstall" in r.stderr, f"no recovery hint: {r.stderr}"
        print("test_uninstall_refuses_when_no_distribution_library_remains OK")


def test_uninstall_keeps_blank_lines_and_later_edits_in_genesys_conf():
    """Only our marked block leaves the file. Original trailing blank lines
    stay (an earlier version stripped them), an edit made after the install
    stays, and the pre-install backup is kept for comparison, not restored
    over the live file."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        text = s.conf.read_text()
        # someone adds another scanner after our install
        s.conf.write_text(text.replace("# Plustek OpticFilm 7600i",
                                       "# Someone's other scanner\nusb 0x1234 0x5678\n\n"
                                       "# Plustek OpticFilm 7600i"))
        r = s.run("uninstall")
        assert r.returncode == 0, f"uninstall failed: {r.stdout}{r.stderr}"
        out = s.conf.read_text()
        assert "usb 0x07b3 0x1436" not in out, "our USB id survived"
        assert "BEGIN opticfilm135i-linux" not in out, "our marker survived"
        assert "usb 0x1234 0x5678" in out, "a later edit was reverted"
        assert out.endswith("usb 0x07b3 0x0c3b\n\n"), \
            f"original trailing blank line was eaten: {out[-40:]!r}"
        assert (s.conf.parent / "genesys.conf.opticfilm135i-backup").exists(), \
            "the backup was discarded even though the file had diverged"
        print("test_uninstall_keeps_blank_lines_and_later_edits_in_genesys_conf OK")


def test_uninstall_leaves_a_foreign_usb_id_in_place():
    """If the USB id is in the file without our markers, someone else put it
    there. Removing it would break their setup."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        s.conf.write_text(GENESYS_CONF + "usb 0x07b3 0x1436\n")
        assert s.run("install").returncode == 0        # sees the id, adds nothing
        r = s.run("uninstall")
        assert r.returncode == 0, f"uninstall failed: {r.stdout}{r.stderr}"
        assert "usb 0x07b3 0x1436" in s.conf.read_text(), \
            "uninstall removed a USB id it did not add"
        print("test_uninstall_leaves_a_foreign_usb_id_in_place OK")


# ---------------------------------------------------------------- verify

def test_verify_passes_when_the_right_library_and_device_appear():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        stub = _stub_scanimage(s.tmp, dlopen_path=str(s.sane / LINK), device=True)
        r = s.run("verify", env={"SCANIMAGE": str(stub)})
        assert r.returncode == 0, f"verify failed on a good run: {r.stdout}{r.stderr}"
        assert "LOAD   OK" in r.stdout and "DEVICE OK" in r.stdout, r.stdout
        print("test_verify_passes_when_the_right_library_and_device_appear OK")


def test_verify_fails_on_the_wrong_library():
    """The point of the check. A different genesys backend serving the scan
    must not be reported as success (the first version hid this behind
    `|| true`)."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        stub = _stub_scanimage(s.tmp, dlopen_path="/usr/lib64/sane/libsane-genesys.so.1",
                               device=True)
        r = s.run("verify", env={"SCANIMAGE": str(stub)})
        assert r.returncode == 2, f"expected exit 2, got {r.returncode}: {r.stdout}{r.stderr}"
        assert "different genesys backend" in r.stderr, r.stderr
        print("test_verify_fails_on_the_wrong_library OK")


def test_verify_separates_library_load_from_device_detection():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        stub = _stub_scanimage(s.tmp, dlopen_path=str(s.sane / LINK), device=False)
        r = s.run("verify", env={"SCANIMAGE": str(stub)})
        assert r.returncode == 1, f"expected exit 1, got {r.returncode}: {r.stdout}{r.stderr}"
        assert "LOAD   OK" in r.stdout, "the library load should still be reported OK"
        assert "DEVICE MISSING" in r.stderr, r.stderr
        print("test_verify_separates_library_load_from_device_detection OK")


def test_verify_fails_when_scanimage_fails():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        stub = _stub_scanimage(s.tmp, dlopen_path=None, device=False, exit_code=3)
        r = s.run("verify", env={"SCANIMAGE": str(stub)})
        assert r.returncode == 2, f"expected exit 2, got {r.returncode}: {r.stdout}{r.stderr}"
        assert "exited 3" in r.stderr, r.stderr
        print("test_verify_fails_when_scanimage_fails OK")


def test_verify_refuses_a_development_environment():
    """A leftover LD_LIBRARY_PATH would make the check prove nothing."""
    with _Staged() as s:
        if s.skip():
            return "skipped"
        stub = _stub_scanimage(s.tmp, dlopen_path=str(s.sane / LINK), device=True)
        r = s.run("verify", env={"SCANIMAGE": str(stub), "LD_LIBRARY_PATH": "/tmp/x"})
        assert r.returncode == 2, f"expected exit 2, got {r.returncode}"
        assert "LD_LIBRARY_PATH is set" in r.stderr, r.stderr
        print("test_verify_refuses_a_development_environment OK")


# ---------------------------------------------------------------- status

def test_status_reports_a_clean_system_without_root():
    with _Staged() as s:
        r = s.run("status")
        assert r.returncode == 0, f"status failed: {r.stdout}{r.stderr}"
        assert "installed lib  : none" in r.stdout
        assert "135i USB id MISSING" in r.stdout
        assert "dll.conf       : genesys enabled" in r.stdout
        assert "our backend is NOT in use" in r.stdout
        print("test_status_reports_a_clean_system_without_root OK")


def test_status_flags_a_recorded_target_that_disappeared():
    with _Staged() as s:
        if s.skip():
            return "skipped"
        assert s.run("install").returncode == 0
        (s.sane / DISTRO_SO).unlink()
        (s.sane / NEWER_SO).write_bytes(b"NEWER\n")
        r = s.run("status")
        assert r.returncode == 0
        assert "GONE -- the package was updated since" in r.stdout, r.stdout
        print("test_status_flags_a_recorded_target_that_disappeared OK")


def main() -> int:
    tests = [
        test_install_adds_without_overwriting_the_distribution,
        test_install_adds_the_usb_id_between_markers,
        test_install_is_idempotent,
        test_install_refuses_an_incomplete_clone,
        test_install_refuses_a_library_built_for_the_wrong_config_dir,
        test_status_flags_a_wrong_config_dir,
        test_install_rolls_back_a_failure_after_the_symlink_changed,
        test_uninstall_restores_the_tree_byte_for_byte,
        test_uninstall_follows_a_package_update_to_the_new_library,
        test_uninstall_leaves_a_reinstated_distribution_link_alone,
        test_uninstall_refuses_when_the_restore_target_is_ambiguous,
        test_uninstall_refuses_when_no_distribution_library_remains,
        test_uninstall_keeps_blank_lines_and_later_edits_in_genesys_conf,
        test_uninstall_leaves_a_foreign_usb_id_in_place,
        test_verify_passes_when_the_right_library_and_device_appear,
        test_verify_fails_on_the_wrong_library,
        test_verify_separates_library_load_from_device_detection,
        test_verify_fails_when_scanimage_fails,
        test_verify_refuses_a_development_environment,
        test_status_reports_a_clean_system_without_root,
        test_status_flags_a_recorded_target_that_disappeared,
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
