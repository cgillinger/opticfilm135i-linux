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
# Swedish option text (docs/sane-install.md S6): KSane looks up our option
# titles/descriptions through gettext, domain sane-backends, in whatever
# catalog is installed at <localedir>/sv/LC_MESSAGES/sane-backends.mo --
# owned, on a Fedora system, by the sane-backends package. Same backup
# discipline as the CONF block: back up the distribution's file once (or
# record that there was none), never overwrite that record, restore byte
# for byte on uninstall, refuse rather than guess if neither is present
# while a catalog sits there.
LOCALE_LANG="sv"
LOCALE_DOMAIN="sane-backends.mo"
LOCALE_BACKUP_SUFFIX=".opticfilm135i-backup"
LOCALE_ABSENT_SUFFIX=".opticfilm135i-absent"
# The sha256 of the catalog THIS script installed, recorded once at install
# time next to the backup/absent marker. Same reasoning as the library's
# ORIG_MARK, inverted: that one names the distribution's file so it is
# never removed out from under a live link; this one names OUR file, so a
# later uninstall can tell "still what I installed" from "replaced since
# (a package update, or someone else)" without trusting the CURRENT build,
# which may have moved on since this install ran.
LOCALE_INSTALLED_SUFFIX=".opticfilm135i-installed"
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

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# ---------------------------------------------------------- sv locale catalog
# Where the distribution's (and our) Swedish catalog lives. Requires the
# directory to already exist -- like backend_dir(), it does not create system
# locale infrastructure that was never there, it only replaces one file
# inside it that some other package (or a previous run of this script) put
# there.
#
# Whichever candidate already carries one of our three record files
# (backup, absence marker, installed-hash) wins, so a directory that
# appeared or disappeared between an install and a later status/uninstall
# never orphans them; only when neither candidate has any of our records
# does this fall back to plain existence.
locale_dir() {
    local d
    for d in "$ROOT/usr/share/locale" "$ROOT/usr/local/share/locale"; do
        if [ -e "$d/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN$LOCALE_BACKUP_SUFFIX" ] ||
           [ -e "$d/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN$LOCALE_ABSENT_SUFFIX" ] ||
           [ -e "$d/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN$LOCALE_INSTALLED_SUFFIX" ]; then
            echo "$d"; return
        fi
    done
    for d in "$ROOT/usr/share/locale" "$ROOT/usr/local/share/locale"; do
        [ -d "$d/$LOCALE_LANG/LC_MESSAGES" ] && { echo "$d"; return; }
    done
    die "no $LOCALE_LANG/LC_MESSAGES locale directory found under /usr/share/locale or /usr/local/share/locale"
}

locale_catalog() { echo "$(locale_dir)/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN"; }

# The catalog this repo builds, and the staleness check install refuses on:
# po/sv.gmo is a build product (tests/test_sane_install.py fakes it; the real
# one comes from `make -C po sv.gmo` in the clone) and is trusted only when it
# is at least as new as the po/sv.po it was built from.
built_locale() {
    local f="$CLONE/po/sv.gmo"
    [ -e "$f" ] || die "no built Swedish catalog: $f is missing -- run 'make -C po sv.gmo' in $CLONE first"
    [ "$f" -ot "$CLONE/po/sv.po" ] && \
        die "$f is older than po/sv.po -- run 'make -C po sv.gmo' in $CLONE first"
    echo "$f"
}

# The config directory compiled into the library (sanei_config's DEFAULT_DIRS,
# "./:<sysconfdir>/sane.d"). It MUST match where genesys.conf actually lives.
# scanimage can paper over a wrong one -- libsane.so.1 sits in the global
# symbol scope there and interposes its own sanei_config_open -- but a
# frontend that loads SANE through a dlopen'd plugin with RTLD_LOCAL (digiKam
# via its Qt scanner plugin) gets the library's own path and finds no config
# at all: "Couldn't access configuration file 'genesys.conf'". Found the hard
# way 2026-09-13.
built_config_dirs() {
    strings -a "$1" 2>/dev/null | grep -E '^\.:(/[^:]+)+/sane\.d$' | head -1
}

# Where genesys.conf lives, as the library would have to see it (no staging
# prefix: the compiled-in path is absolute and knows nothing about ROOT).
system_config_dir() {
    local c="${CONF#$ROOT}"
    dirname "$c"
}

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
        local cfg; cfg="$(built_config_dirs "$b")"
        if [ "$cfg" = ".:$(system_config_dir)" ]; then
            echo "                 config path $cfg (matches $(system_config_dir))"
        else
            echo "                 config path ${cfg:-unknown} -- MISMATCH, expected .:$(system_config_dir)"
        fi
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

    local loc_dir loc_cat loc_hash loc_built
    if loc_dir="$(locale_dir 2>/dev/null)"; then
        loc_cat="$loc_dir/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN"
        loc_hash="$loc_cat$LOCALE_INSTALLED_SUFFIX"
        if [ ! -e "$loc_cat" ]; then
            echo "sv catalog     : none at $loc_cat"
        elif [ -e "$loc_hash" ] && [ "$(sha256_of "$loc_cat")" = "$(cat "$loc_hash")" ]; then
            # The live file's hash still matches what we recorded at
            # install time: still ours, not replaced by a package update
            # since (§ package-update model below).
            if loc_built="$(built_locale 2>/dev/null)" && cmp -s "$loc_built" "$loc_cat"; then
                echo "sv catalog     : $loc_cat (ours, identical to the current build)"
            else
                echo "sv catalog     : $loc_cat (ours, DIFFERS from the current build -- reinstall)"
            fi
        else
            echo "sv catalog     : $loc_cat (present, not ours -- the distribution's; our records dropped at next uninstall)"
        fi
    else
        echo "sv catalog     : no $LOCALE_LANG/LC_MESSAGES directory under /usr/share/locale or /usr/local/share/locale"
    fi
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
    local cfgdirs want_cfg
    cfgdirs="$(built_config_dirs "$src")"
    want_cfg=".:$(system_config_dir)"
    if [ -z "$cfgdirs" ]; then
        echo "sane_install: warning: could not read the config path compiled into" >&2
        echo "              $src -- skipping that check" >&2
    elif [ "$cfgdirs" != "$want_cfg" ]; then
        die "the built library looks for its config in '$cfgdirs', but genesys.conf
    lives in '$(system_config_dir)'. scanimage would still work (libsane
    interposes its own path), but a frontend that loads SANE through a
    dlopen'd plugin -- digiKam does -- would fail with \"Couldn't access
    configuration file 'genesys.conf'\".
    Rebuild with the right sysconfdir, then install again:
        cd $CLONE && ./configure --sysconfdir=$(dirname "$(system_config_dir)") \\
            BACKENDS=genesys --disable-locking --without-gphoto2 --without-v4l
        make -C sanei clean && make -j8 -C sanei
        make -C backend clean && make -j8 -C backend libsane-genesys.la"
    fi
    [ -w "$dir" ] || die "$dir is not writable"
    [ -L "$link" ] || die "$link is not a symlink -- refusing to touch it"
    [ -f "$CONF" ] || die "$CONF does not exist -- is sane-backends installed?"
    [ -w "$CONF" ] || die "$CONF is not writable"
    [ -w "$(dirname "$CONF")" ] || die "$(dirname "$CONF") is not writable"
    # Swedish catalog: only a precondition when there is somewhere to put it.
    # A system with no sv/LC_MESSAGES at all just does not get the feature --
    # that is not this install's problem to fix -- but a system that DOES
    # have Swedish sane-backends strings must not have them silently
    # regressed by a stale or missing local build.
    local loc_dir="" built_gmo=""
    if loc_dir="$(locale_dir 2>/dev/null)"; then
        built_gmo="$(built_locale)"
        [ -w "$loc_dir/$LOCALE_LANG/LC_MESSAGES" ] || \
            die "$loc_dir/$LOCALE_LANG/LC_MESSAGES is not writable"
    fi
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

    if [ -n "$loc_dir" ]; then
        local loc_cat loc_backup loc_absent loc_hash
        loc_cat="$loc_dir/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN"
        loc_backup="$loc_cat$LOCALE_BACKUP_SUFFIX"
        loc_absent="$loc_cat$LOCALE_ABSENT_SUFFIX"
        if [ ! -e "$loc_backup" ] && [ ! -e "$loc_absent" ]; then
            # First install here: preserve exactly what was there before,
            # one way or the other, so uninstall can put it back without
            # guessing. Never taken again once one of the two exists.
            if [ -e "$loc_cat" ]; then
                cp -a "$loc_cat" "$loc_backup" || abort "cannot back up $loc_cat"
                rollback_push "rm -f '$loc_backup'"
            else
                : > "$loc_absent" || abort "cannot record that $loc_cat did not exist"
                rollback_push "rm -f '$loc_absent'"
            fi
        fi
        # This run's own undo, separate from the one-time backup above: if
        # $loc_cat already exists (the distribution's on a first install,
        # or ours from a previous run), keep a copy and push its restore;
        # if not, push a plain removal. Mirrors the library's $name.prev.
        if [ -e "$loc_cat" ]; then
            cp -a "$loc_cat" "$loc_cat.prev" || abort "cannot back up $loc_cat for this run"
            rollback_push "mv -f '$loc_cat.prev' '$loc_cat'"
        else
            rollback_push "rm -f '$loc_cat'"
        fi
        cp -a "$built_gmo" "$loc_cat.new" || abort "cannot stage the Swedish catalog"
        rollback_push "rm -f '$loc_cat.new'"
        mv -f "$loc_cat.new" "$loc_cat" || abort "cannot install the Swedish catalog"
        if [ -z "$ROOT" ] && command -v restorecon >/dev/null 2>&1; then
            restorecon "$loc_cat" || true
        fi
        # Package-update detection: record what we just installed's hash,
        # so a later uninstall/status can tell it apart from a
        # replacement without trusting the (possibly since-changed) build.
        loc_hash="$loc_cat$LOCALE_INSTALLED_SUFFIX"
        if [ -e "$loc_hash" ]; then
            cp -a "$loc_hash" "$loc_hash.prev" || abort "cannot back up $loc_hash"
            rollback_push "mv -f '$loc_hash.prev' '$loc_hash'"
        else
            rollback_push "rm -f '$loc_hash'"
        fi
        sha256_of "$loc_cat" > "$loc_hash" || abort "cannot record the installed catalog's hash"
        echo "installed the Swedish catalog at $loc_cat"
    else
        echo "sane_install: no sv/LC_MESSAGES locale directory found -- option text will" >&2
        echo "              show in English; the library and genesys.conf were still installed." >&2
    fi

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
    if [ -n "$loc_dir" ]; then
        rm -f "$loc_dir/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN.prev"
        rm -f "$loc_dir/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN$LOCALE_INSTALLED_SUFFIX.prev"
    fi
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
    uninstall_locale
    echo "the distribution's own libsane-genesys.so.1.* was never modified"
}

# Restore the sv locale catalog to its pre-install state. Two questions,
# answered in order, neither guessed at:
#
#   1. Did WE ever touch this file? Install always writes a backup or an
#      absence marker before its first overwrite here, so neither existing
#      means we never did -- the file, if any, is the distribution's, as
#      found, and is left alone (this is the state a system installed with
#      a pre-2026-09-27 script, or never installed by this script at all,
#      is in: not an error, just nothing of ours to undo).
#   2. If we DID touch it, is it still what we installed? The hash
#      recorded at install time answers that without trusting the
#      CURRENT build, which may have moved on: a mismatch means a package
#      update (or someone else) replaced our file since, and that
#      replacement is not ours to remove or overwrite -- only our own
#      backup/marker/record are cleaned up.
uninstall_locale() {
    local loc_dir loc_cat loc_backup loc_absent loc_hash live_hash
    if ! loc_dir="$(locale_dir 2>/dev/null)"; then
        echo "no $LOCALE_LANG/LC_MESSAGES locale directory found; nothing to restore for the Swedish catalog" >&2
        return 0
    fi
    loc_cat="$loc_dir/$LOCALE_LANG/LC_MESSAGES/$LOCALE_DOMAIN"
    loc_backup="$loc_cat$LOCALE_BACKUP_SUFFIX"
    loc_absent="$loc_cat$LOCALE_ABSENT_SUFFIX"
    loc_hash="$loc_cat$LOCALE_INSTALLED_SUFFIX"

    if [ ! -e "$loc_backup" ] && [ ! -e "$loc_absent" ]; then
        rm -f "$loc_hash"     # a hash record with neither is not meaningful
        echo "$loc_cat: not ours (no pre-install record here) -- left untouched"
        return 0
    fi

    if [ -e "$loc_cat" ] && [ -e "$loc_hash" ]; then
        live_hash="$(sha256_of "$loc_cat")"
        if [ "$live_hash" != "$(cat "$loc_hash")" ]; then
            rm -f "$loc_backup" "$loc_absent" "$loc_hash"
            echo "$loc_cat was replaced since our install (its hash no longer matches our"
            echo "record) -- left as found; our backup/marker/record were removed"
            return 0
        fi
    fi

    if [ -e "$loc_backup" ]; then
        cp -a "$loc_backup" "$loc_cat" || die "cannot restore $loc_cat from $loc_backup"
        rm -f "$loc_backup" "$loc_hash"
        echo "restored $loc_cat from the pre-install backup"
    elif [ -e "$loc_absent" ]; then
        rm -f "$loc_cat" "$loc_absent" "$loc_hash"
        echo "removed $loc_cat (nothing existed there before install)"
    fi
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
