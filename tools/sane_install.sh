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
#   * Only the packaged symlink libsane-genesys.so.1 is repointed at it,
#     because sane's dll backend dlopen()s that exact absolute path. The
#     distribution's link target is recorded next to it.
#   * /etc/sane.d/genesys.conf gets the 135i USB id between markers.
#   * dll.conf is NOT touched: `genesys` is already enabled there.
#
# Safety rules this script follows:
#   * every precondition is checked BEFORE the first change;
#   * a failure partway through undoes the changes already made;
#   * uninstall reasons about the CURRENT link before restoring anything: a
#     later distribution update is preserved, and a missing or ambiguous
#     restore target is refused rather than guessed;
#   * a library is never removed while the live link still points at it;
#   * only our own marked block is removed from genesys.conf -- later edits
#     by anyone else are kept, and the pre-install backup is never restored
#     over them.
#
# `verify` talks to the SANE stack and therefore to a connected scanner.
# Run it only inside an approved hardware session.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLONE="${SANE_BACKENDS_DIR:-$(cd "$REPO/.." && pwd)/sane-backends}"
# OF135I_SANE_ROOT prefixes every system path. Empty = the real system.
# A staging directory here exercises install/uninstall verbatim, without root
# and without touching the distribution (tests/test_sane_install.py).
ROOT="${OF135I_SANE_ROOT:-}"
CONF="$ROOT/etc/sane.d/genesys.conf"
SCANIMAGE="${SCANIMAGE:-scanimage}"
USB_ID="usb 0x07b3 0x1436"
MARK_BEGIN="# BEGIN opticfilm135i-linux (GL126 port) -- tools/sane_install.sh"
MARK_END="# END opticfilm135i-linux"
LINK_NAME="libsane-genesys.so.1"
OURS_GLOB="libsane-genesys-gl126.so.*"
ORIG_MARK=".libsane-genesys.so.1.opticfilm135i-orig"
CONF_BACKUP_SUFFIX=".opticfilm135i-backup"
GL126_SOURCES=(gl126.h gl126.cpp gl126_registers.h gl126_tables.h gl126_tables.cpp
               gl126_ops.h gl126_ops.cpp gl126_lock.h gl126_lock.cpp)

die() { echo "sane_install: $*" >&2; exit 1; }
need_root() {
    [ -n "$ROOT" ] && return 0          # staging root: no privileges needed
    [ "$(id -u)" -eq 0 ] || die "$1 needs root (sudo tools/sane_install.sh $1)"
}

# --------------------------------------------------------------- rollback
# Commands to undo, most recent first, if a later step fails.
ROLLBACK=()
rollback_push() { ROLLBACK+=("$1"); }
rollback_run() {
    local i
    echo "sane_install: rolling back" >&2
    for (( i=${#ROLLBACK[@]}-1; i>=0; i-- )); do
        eval "${ROLLBACK[$i]}" >&2 || echo "sane_install: rollback step failed: ${ROLLBACK[$i]}" >&2
    done
    ROLLBACK=()
}
abort() {                                # fail a partially applied change
    echo "sane_install: $*" >&2
    rollback_run
    exit 1
}

# ------------------------------------------------------------- discovery
backend_dir() {
    local d
    for d in "$ROOT/usr/lib64/sane" "$ROOT/usr/lib/x86_64-linux-gnu/sane" "$ROOT/usr/lib/sane"; do
        [ -e "$d/$LINK_NAME" ] && { echo "$d"; return; }
    done
    die "no distribution SANE backend directory with $LINK_NAME found"
}

built_lib() {
    local f
    f="$(ls -1 "$CLONE"/backend/.libs/libsane-genesys.so.1.*.* 2>/dev/null | head -1)" || true
    [ -n "${f:-}" ] || die "no built backend in $CLONE/backend/.libs -- see docs/sane-install.md step 3"
    echo "$f"
}

installed_name() {
    echo "libsane-genesys-gl126.so.$(basename "$(built_lib)" | sed 's/^libsane-genesys\.so\.//')"
}

is_ours() { case "$1" in libsane-genesys-gl126.so.*) return 0 ;; *) return 1 ;; esac; }

# Distribution libraries present in the backend dir (ours excluded).
distro_libs() {
    local dir="$1" f base
    for f in "$dir"/libsane-genesys.so.1.*; do
        [ -f "$f" ] || continue                       # skip symlinks and gaps
        base="$(basename "$f")"
        is_ours "$base" && continue
        echo "$base"
    done
}

# ---------------------------------------------------------------- status
cmd_status() {
    local dir b inst current
    dir="$(backend_dir)"
    echo "repo           : $REPO"
    echo "sane-backends  : $CLONE"
    local missing=() f
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
        echo "                 sha256 $(sha256sum "$b" | cut -c1-16)...  $(nm -C "$b" 2>/dev/null | grep -c gl126 || true) gl126 symbols"
    else
        echo "built library  : NOT BUILT"
        b=""
    fi
    echo "backend dir    : $dir"
    current="$(readlink "$dir/$LINK_NAME" 2>/dev/null || echo '?')"
    echo "genesys.so.1   -> $current"
    if is_ours "$current"; then
        echo "                 (ours)"
    else
        echo "                 (the distribution's -- our backend is NOT in use)"
    fi
    inst=""
    [ -n "$b" ] && inst="$dir/$(installed_name)"
    if [ -n "$inst" ] && [ -e "$inst" ]; then
        echo "installed lib  : $inst"
        if cmp -s "$b" "$inst"; then echo "                 identical to the current build"
        else echo "                 DIFFERS from the current build -- reinstall"; fi
    else
        echo "installed lib  : none"
    fi
    if [ -e "$dir/$ORIG_MARK" ]; then
        local recorded; recorded="$(cat "$dir/$ORIG_MARK")"
        if [ -e "$dir/$recorded" ]; then
            echo "recorded target: $recorded"
        else
            echo "recorded target: $recorded (GONE -- the package was updated since)"
        fi
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

# --------------------------------------------------------------- install
cmd_install() {
    need_root install
    local dir src name link current f
    dir="$(backend_dir)"; src="$(built_lib)"; name="$(installed_name)"
    link="$dir/$LINK_NAME"

    # ---- preconditions, every one of them before the first change --------
    for f in "${GL126_SOURCES[@]}"; do
        [ -e "$CLONE/backend/genesys/$f" ] || \
            die "clone is missing backend/genesys/$f -- symlink all ${#GL126_SOURCES[@]} gl126 sources first"
    done
    # (grep -q would SIGPIPE nm, and pipefail would turn that into a failure)
    [ "$(nm -C "$src" | grep -c gl126 || true)" -gt 0 ] \
        || die "the built library carries no gl126 symbols"
    [ -w "$dir" ] || die "$dir is not writable"
    [ -L "$link" ] || die "$link is not a symlink -- refusing to touch it"
    [ -f "$CONF" ] || die "$CONF does not exist -- is sane-backends installed?"
    [ -w "$CONF" ] || die "$CONF is not writable"
    [ -w "$(dirname "$CONF")" ] || die "$(dirname "$CONF") is not writable"
    current="$(readlink "$link")"

    # ---- changes, each undoable -----------------------------------------
    # Safety net for anything that fails without an explicit `|| abort`.
    trap 'rollback_run; echo "sane_install: install failed; nothing was left applied" >&2; exit 1' ERR
    install -m 0755 "$src" "$dir/$name.new" || die "cannot stage the library in $dir"
    rollback_push "rm -f '$dir/$name.new'"
    if [ -e "$dir/$name" ]; then                      # reinstall: keep a copy
        cp -a "$dir/$name" "$dir/$name.prev" || abort "cannot back up the existing $name"
        rollback_push "mv -f '$dir/$name.prev' '$dir/$name'"
    else
        rollback_push "rm -f '$dir/$name'"
    fi
    mv -f "$dir/$name.new" "$dir/$name" || abort "cannot install the library"
    # SELinux: the policy already maps this name to lib_t (matchpathcon
    # agrees), but relabel rather than trust the inherited context.
    if [ -z "$ROOT" ] && command -v restorecon >/dev/null 2>&1; then
        restorecon "$dir/$name" || true
    fi

    # Record the distribution's link target. Refresh it whenever the live
    # link is the distribution's (a package update moved it since last time);
    # never overwrite it with our own name.
    if ! is_ours "$current"; then
        if [ -e "$dir/$ORIG_MARK" ]; then
            rollback_push "cp -a '$dir/$ORIG_MARK.bak' '$dir/$ORIG_MARK'; rm -f '$dir/$ORIG_MARK.bak'"
            cp -a "$dir/$ORIG_MARK" "$dir/$ORIG_MARK.bak"
        else
            rollback_push "rm -f '$dir/$ORIG_MARK'"
        fi
        echo "$current" > "$dir/$ORIG_MARK" || abort "cannot record the distribution target"
        echo "recorded distribution target: $current"
        rm -f "$dir/$ORIG_MARK.bak"
    fi

    rollback_push "ln -sfn '$current' '$link'"
    ln -sfn "$name" "$link" || abort "cannot repoint $LINK_NAME"
    echo "installed $dir/$name and repointed $LINK_NAME at it"

    # Test hook: force a failure at this exact point (after the library and
    # the symlink have changed) so tests/test_sane_install.py can prove the
    # rollback. Staging roots only -- it is inert on the real system.
    if [ -n "$ROOT" ] && [ "${OF135I_INSTALL_FAIL_AT:-}" = "conf" ]; then
        abort "simulated failure at the config step (OF135I_INSTALL_FAIL_AT)"
    fi

    if grep -qF "$USB_ID" "$CONF"; then
        echo "$CONF already carries the 135i USB id"
    else
        cp -a "$CONF" "$CONF$CONF_BACKUP_SUFFIX" || abort "cannot back up $CONF"
        rollback_push "cp -a '$CONF$CONF_BACKUP_SUFFIX' '$CONF'; rm -f '$CONF$CONF_BACKUP_SUFFIX'"
        # The block is self-contained and starts at the marker: uninstall
        # deletes exactly these lines and nothing around them (no blank line
        # of ours to guess about later).
        if [ -n "$(tail -c1 "$CONF")" ]; then                   # ensure a final newline
            printf '\n' >> "$CONF"
        fi
        printf '%s\n# Plustek OpticFilm 135i\n%s\n%s\n' \
            "$MARK_BEGIN" "$USB_ID" "$MARK_END" >> "$CONF" \
            || abort "cannot add the USB id to $CONF"
        echo "added the 135i USB id to $CONF (backup: $CONF$CONF_BACKUP_SUFFIX)"
    fi

    trap - ERR
    ROLLBACK=()                                       # committed
    rm -f "$dir/$name.prev"
    echo
    echo "Next: tools/sane_install.sh status, then (hardware session) tools/sane_install.sh verify"
}

# ------------------------------------------------------------- uninstall
# Decide what libsane-genesys.so.1 should point at once we step aside.
# Prints the target, or fails with an explanation and changes nothing.
restore_target() {
    local dir="$1" recorded candidates n
    recorded=""
    [ -e "$dir/$ORIG_MARK" ] && recorded="$(cat "$dir/$ORIG_MARK")"
    if [ -n "$recorded" ] && [ -e "$dir/$recorded" ]; then
        echo "$recorded"; return 0
    fi
    # The recorded target is gone (a package update replaced it) or was never
    # recorded. Fall back to an unambiguous distribution library, or refuse.
    mapfile -t candidates < <(distro_libs "$dir")
    n=${#candidates[@]}
    if [ "$n" -eq 1 ]; then
        echo "${candidates[0]}"; return 0
    fi
    if [ -n "$recorded" ]; then
        echo "sane_install: the recorded target '$recorded' no longer exists" >&2
    else
        echo "sane_install: no distribution target was recorded" >&2
    fi
    if [ "$n" -eq 0 ]; then
        echo "sane_install: and no distribution libsane-genesys.so.1.* is present" >&2
    else
        echo "sane_install: and $n candidates are: ${candidates[*]}" >&2
    fi
    return 1
}

cmd_uninstall() {
    need_root uninstall
    local dir link current target f base
    dir="$(backend_dir)"; link="$dir/$LINK_NAME"
    [ -L "$link" ] || die "$link is not a symlink -- refusing to touch it"
    current="$(readlink "$link")"

    if is_ours "$current"; then
        if ! target="$(restore_target "$dir")"; then
            die "refusing to uninstall: nothing was changed. $LINK_NAME still points at
    our backend, and no safe restore target could be determined. Fix the
    distribution's files first (on Fedora: dnf reinstall
    sane-backends-drivers-scanners), then run uninstall again."
        fi
        [ -e "$dir/$target" ] || \
            die "internal error: restore target '$target' does not exist; nothing changed"
        ln -sfn "$target" "$link"
        echo "restored $LINK_NAME -> $target"
    else
        echo "$LINK_NAME already points at $current (not ours) -- left untouched;"
        echo "a package update most likely restored it."
    fi

    # Never remove a library the live link still resolves to.
    current="$(readlink "$link")"
    for f in "$dir"/$OURS_GLOB; do
        [ -e "$f" ] || continue
        base="$(basename "$f")"
        if [ "$base" = "$current" ]; then
            echo "keeping $base: $LINK_NAME still points at it" >&2
            continue
        fi
        rm -f "$f"; echo "removed $f"
    done
    rm -f "$dir/$ORIG_MARK"

    uninstall_conf
    echo "the distribution's own libsane-genesys.so.1.* was never modified"
}

# Remove only our marked block; keep everything else, including edits made
# after the install. The pre-install backup is used for comparison, never
# restored over the live file.
uninstall_conf() {
    local backup="$CONF$CONF_BACKUP_SUFFIX" tmp
    if [ ! -f "$CONF" ]; then
        echo "$CONF is gone; nothing to clean up" >&2
        return 0
    fi
    if ! grep -qF "$MARK_BEGIN" "$CONF"; then
        if grep -qF "$USB_ID" "$CONF"; then
            echo "note: $CONF carries $USB_ID outside our markers -- left in place" >&2
        else
            echo "no marker block in $CONF; leaving it alone"
        fi
        return 0
    fi
    tmp="$(mktemp "$CONF.of135i.XXXXXX")"
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        $0 == b { drop = 1; next }
        $0 == e { drop = 0; next }
        !drop   { print }
    ' "$CONF" > "$tmp"
    cat "$tmp" > "$CONF"                   # keep the original inode/mode/labels
    rm -f "$tmp"
    echo "removed the 135i USB id block from $CONF"
    if [ -f "$backup" ]; then
        if cmp -s "$backup" "$CONF"; then
            rm -f "$backup"
            echo "$CONF is byte-identical to its pre-install state; backup removed"
        else
            echo "$CONF differs from the pre-install backup -- edits made since the"
            echo "install are KEPT. The backup is left at $backup for comparison."
        fi
    fi
}

# ----------------------------------------------------------------- verify
# Exit 0: our library loaded AND the scanner enumerated.
# Exit 1: our library loaded, no 135i listed (scanner off, asleep, or busy).
# Exit 2: the wrong library was loaded, or scanimage itself failed.
cmd_verify() {
    local dir link_path out rc dlopen_line
    dir="$(backend_dir)"; link_path="$dir/$LINK_NAME"
    echo "# Runs the SANE stack and will enumerate a connected scanner."
    echo "# Expecting the dlopen()ed backend to be $link_path"
    if [ -n "${LD_LIBRARY_PATH:-}" ]; then
        echo "FAIL: LD_LIBRARY_PATH is set ($LD_LIBRARY_PATH) -- it would defeat the" >&2
        echo "      point of this check. Unset it and run again." >&2
        return 2
    fi
    if [ -n "${SANE_CONFIG_DIR:-}" ]; then
        echo "FAIL: SANE_CONFIG_DIR is set ($SANE_CONFIG_DIR) -- unset it and run again." >&2
        return 2
    fi
    set +e
    out="$(SANE_DEBUG_DLL=4 "$SCANIMAGE" -L 2>&1)"; rc=$?
    set -e
    if [ $rc -ne 0 ]; then
        echo "FAIL: $SCANIMAGE -L exited $rc" >&2
        printf '%s\n' "$out" | tail -20 >&2
        return 2
    fi
    dlopen_line="$(printf '%s\n' "$out" | grep -F "dlopen()ing" | grep -F "libsane-genesys" | head -1 || true)"
    if [ -z "$dlopen_line" ]; then
        echo "FAIL: the genesys backend was never dlopen()ed. Is it enabled in dll.conf?" >&2
        printf '%s\n' "$out" | grep -Ei "genesys|unable to find|couldn't find" | tail -20 >&2
        return 2
    fi
    case "$dlopen_line" in
        *"$link_path"*) echo "LOAD   OK: $dlopen_line" ;;
        *) echo "FAIL: a different genesys backend was loaded:" >&2
           echo "      $dlopen_line" >&2
           return 2 ;;
    esac
    echo "LOAD   OK: $link_path -> $(readlink "$link_path")"
    if printf '%s\n' "$out" | grep -qE "^device \`genesys:"; then
        echo "DEVICE OK: $(printf '%s\n' "$out" | grep -E "^device \`genesys:" | head -1)"
        return 0
    fi
    echo "DEVICE MISSING: the library loaded, but no genesys device was listed." >&2
    echo "      Scanner powered off, asleep, claimed by a VM, or held by of135i?" >&2
    printf '%s\n' "$out" | grep -vF "[dll]" | tail -10 >&2
    return 1
}

case "${1:-status}" in
    status) cmd_status ;;
    install) cmd_install ;;
    uninstall) cmd_uninstall ;;
    verify) cmd_verify ;;
    *) die "usage: $0 {status|install|uninstall|verify}" ;;
esac
