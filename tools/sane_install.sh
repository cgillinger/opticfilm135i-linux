#!/usr/bin/env bash
# Install / uninstall the GL126 (OpticFilm 135i) genesys backend built from
# this repository's sane/ sources, so that ordinary SANE frontends
# (scanimage, digiKam via KSaneCore) load it without LD_LIBRARY_PATH.
#
#   tools/sane_install.sh status      # what is installed right now (no root)
#   sudo tools/sane_install.sh install
#   sudo tools/sane_install.sh uninstall
#   tools/sane_install.sh verify      # which file a frontend actually loads
#
# Design (docs/sane-install.md):
#   * The built library is installed under its OWN name,
#     libsane-genesys-gl126.so.<ver>, in the distribution's SANE backend
#     directory. No distribution file is overwritten.
#   * Only the RPM/DEB-owned symlink libsane-genesys.so.1 is repointed at it,
#     because sane's dll backend dlopen()s that exact absolute path. The
#     original link target is recorded next to it and restored on uninstall.
#   * /etc/sane.d/genesys.conf gets the 135i USB id between markers.
#   * dll.conf is NOT touched: `genesys` is already enabled there.
#
# `verify` talks to the SANE stack and therefore to a connected scanner.
# Run it only inside an approved hardware session.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLONE="${SANE_BACKENDS_DIR:-$(cd "$REPO/.." && pwd)/sane-backends}"
# OF135I_SANE_ROOT prefixes every system path. Empty = the real system.
# A staging directory here exercises install/uninstall verbatim, without root
# and without touching the distribution (docs/sane-install.md, staging check).
ROOT="${OF135I_SANE_ROOT:-}"
CONF="$ROOT/etc/sane.d/genesys.conf"
USB_ID="usb 0x07b3 0x1436"
MARK_BEGIN="# BEGIN opticfilm135i-linux (GL126 port) -- tools/sane_install.sh"
MARK_END="# END opticfilm135i-linux"
GL126_SOURCES=(gl126.h gl126.cpp gl126_registers.h gl126_tables.h gl126_tables.cpp
               gl126_ops.h gl126_ops.cpp gl126_lock.h gl126_lock.cpp)

die() { echo "sane_install: $*" >&2; exit 1; }
need_root() {
    [ -n "$ROOT" ] && return 0          # staging root: no privileges needed
    [ "$(id -u)" -eq 0 ] || die "$1 needs root (sudo tools/sane_install.sh $1)"
}

backend_dir() {
    local d
    for d in "$ROOT/usr/lib64/sane" "$ROOT/usr/lib/x86_64-linux-gnu/sane" "$ROOT/usr/lib/sane"; do
        [ -e "$d/libsane-genesys.so.1" ] && { echo "$d"; return; }
    done
    die "no distribution SANE backend directory with libsane-genesys.so.1 found"
}

built_lib() {
    local f
    f="$(ls -1 "$CLONE"/backend/.libs/libsane-genesys.so.1.*.* 2>/dev/null | head -1)" || true
    [ -n "${f:-}" ] || die "no built backend in $CLONE/backend/.libs -- see docs/sane-install.md step 2"
    echo "$f"
}

installed_name() { echo "libsane-genesys-gl126.so.$(basename "$(built_lib)" | sed 's/^libsane-genesys\.so\.//')"; }

cmd_status() {
    local dir; dir="$(backend_dir)"
    echo "repo           : $REPO"
    echo "sane-backends  : $CLONE"
    local missing=()
    local f
    for f in "${GL126_SOURCES[@]}"; do
        [ -e "$CLONE/backend/genesys/$f" ] || missing+=("$f")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo "gl126 sources  : MISSING in the clone: ${missing[*]}"
    else
        echo "gl126 sources  : all ${#GL126_SOURCES[@]} present in $CLONE/backend/genesys"
    fi
    if b="$(built_lib 2>/dev/null)"; then
        echo "built library  : $b"
        echo "                 sha256 $(sha256sum "$b" | cut -c1-16)...  $(nm -C "$b" 2>/dev/null | grep -c gl126) gl126 symbols"
    else
        echo "built library  : NOT BUILT"
    fi
    echo "backend dir    : $dir"
    echo "genesys.so.1   -> $(readlink "$dir/libsane-genesys.so.1" || echo '?')"
    if [ -e "$dir/$(installed_name 2>/dev/null || echo __none__)" ]; then
        local inst="$dir/$(installed_name)"
        echo "installed lib  : $inst"
        if b="$(built_lib 2>/dev/null)"; then
            if cmp -s "$b" "$inst"; then echo "                 identical to the current build"
            else echo "                 DIFFERS from the current build -- reinstall"; fi
        fi
    else
        echo "installed lib  : none"
    fi
    if grep -qF "$USB_ID" "$CONF" 2>/dev/null; then
        echo "$CONF : 135i USB id present"
    else
        echo "$CONF : 135i USB id MISSING"
    fi
    if [ -e "$ROOT/etc/udev/rules.d/60-of135i.rules" ]; then
        echo "udev rule      : installed"
    else
        echo "udev rule      : MISSING (sudo cp udev/60-of135i.rules /etc/udev/rules.d/)"
    fi
    grep -qE '^\s*genesys\s*$' "$ROOT/etc/sane.d/dll.conf" 2>/dev/null \
        && echo "dll.conf       : genesys enabled" \
        || echo "dll.conf       : genesys NOT enabled"
}

cmd_install() {
    need_root install
    local dir src name link orig
    dir="$(backend_dir)"; src="$(built_lib)"; name="$(installed_name)"
    link="$dir/libsane-genesys.so.1"
    for f in "${GL126_SOURCES[@]}"; do
        [ -e "$CLONE/backend/genesys/$f" ] || die "clone is missing backend/genesys/$f -- symlink all ${#GL126_SOURCES[@]} gl126 sources first"
    done
    # (grep -q would SIGPIPE nm, and pipefail would turn that into a failure)
    [ "$(nm -C "$src" | grep -c gl126 || true)" -gt 0 ] \
        || die "the built library carries no gl126 symbols"

    # Record the distribution's own link target once, before repointing it.
    orig="$dir/.libsane-genesys.so.1.opticfilm135i-orig"
    if [ ! -e "$orig" ]; then
        readlink "$link" > "$orig" || die "cannot read the current $link"
        echo "recorded distribution target: $(cat "$orig")"
    fi

    install -m 0755 "$src" "$dir/$name.new"
    mv -f "$dir/$name.new" "$dir/$name"
    # SELinux: the policy already maps this name to lib_t (matchpathcon
    # agrees), but relabel rather than trust the inherited context.
    if [ -z "$ROOT" ] && command -v restorecon >/dev/null 2>&1; then
        restorecon "$dir/$name" || true
    fi
    ln -sfn "$name" "$link"
    echo "installed $dir/$name and repointed libsane-genesys.so.1 at it"

    if grep -qF "$USB_ID" "$CONF"; then
        echo "$CONF already carries the 135i USB id"
    else
        cp -a "$CONF" "$CONF.opticfilm135i-backup"
        printf '\n%s\n# Plustek OpticFilm 135i\n%s\n%s\n' \
            "$MARK_BEGIN" "$USB_ID" "$MARK_END" >> "$CONF"
        echo "added the 135i USB id to $CONF (backup: $CONF.opticfilm135i-backup)"
    fi
    echo
    echo "Next: tools/sane_install.sh status, then (hardware session) tools/sane_install.sh verify"
}

cmd_uninstall() {
    need_root uninstall
    local dir link orig name
    dir="$(backend_dir)"; link="$dir/libsane-genesys.so.1"
    orig="$dir/.libsane-genesys.so.1.opticfilm135i-orig"
    if [ -e "$orig" ]; then
        ln -sfn "$(cat "$orig")" "$link"
        echo "restored $link -> $(cat "$orig")"
        rm -f "$orig"
    else
        echo "no recorded distribution target; leaving $link alone" >&2
    fi
    for name in "$dir"/libsane-genesys-gl126.so.*; do
        [ -e "$name" ] || continue
        rm -f "$name"; echo "removed $name"
    done
    if grep -qF "$MARK_BEGIN" "$CONF" 2>/dev/null; then
        sed -i "/^$(printf '%s' "$MARK_BEGIN" | sed 's/[][\.*^$/]/\\&/g')$/,/^$(printf '%s' "$MARK_END" | sed 's/[][\.*^$/]/\\&/g')$/d" "$CONF"
        sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$CONF"
        echo "removed the 135i USB id block from $CONF"
    else
        echo "no marker block in $CONF; leaving it alone"
    fi
    echo "the distribution's own libsane-genesys.so.1.* was never modified"
}

cmd_verify() {
    local dir; dir="$(backend_dir)"
    echo "# This runs the SANE stack and will enumerate a connected scanner."
    echo "# Expected: the dlopen line names $dir/libsane-genesys.so.1"
    SANE_DEBUG_DLL=4 scanimage -L 2>&1 | grep -E "dlopen|unable to find|found .* devices|genesys:" || true
    echo "---"
    echo "libsane-genesys.so.1 -> $(readlink "$dir/libsane-genesys.so.1")"
}

case "${1:-status}" in
    status) cmd_status ;;
    install) cmd_install ;;
    uninstall) cmd_uninstall ;;
    verify) cmd_verify ;;
    *) die "usage: $0 {status|install|uninstall|verify}" ;;
esac
