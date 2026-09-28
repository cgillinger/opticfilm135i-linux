/* sane - Scanner Access Now Easy.

   Copyright (C) 2026 Christian Gillinger

   This file is part of the SANE package.

   This program is free software; you can redistribute it and/or
   modify it under the terms of the GNU General Public License as
   published by the Free Software Foundation; either version 2 of the
   License, or (at your option) any later version.

   This program is distributed in the hope that it will be useful, but
   WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
   General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

/* GL126 process lock: mutual exclusion with the companion userspace
   driver.

   The companion driver and this backend are two independent programs
   that can both open the same physical unit (07b3:1436, one reference
   device in existence). Claiming the USB interface alone is not enough
   to keep them apart: the driver's read-only sessions (e.g. a status
   check) hold no interface claim at all, they talk to the device just
   enough to read a register, so a `sane_open` racing one of them would
   not see it and could send a command while the driver is mid-read.

   The fix is a convention, not a protocol: both programs take a
   non-blocking exclusive `flock` on the same well-known file before
   touching the device, and release it when they are done. This header
   is the C++ side of that convention, kept byte-for-byte compatible with
   the driver's own implementation:

     - path: `$OF135I_LOCK_FILE`, or `/tmp/of135i-07b3-1436.lock` if unset
       (one lock path per host, exactly like the driver: there is only
       ever one unit, so this is not a per-device lock).
     - held file content on success: `pid <pid> since <ISO8601 UTC>
       (sane genesys gl126)\n`, so `cat` on the lock file names the
       holder either way.

   GL126-only: no other genesys ASIC has a conflicting driver, so no
   other command set includes this header or calls into this namespace.

   File handling: the path is deliberately predictable, in a directory
   (/tmp) any local user can write to, and the file is created mode
   0666: two independent programs, potentially run by two different
   users on a shared machine, need to take the same lock, so it cannot
   be owner-only. That combination (fixed path, shared directory, open
   permissions) is exactly the setup a symlink, hard-link, or FIFO/
   device-node attack targets, so every open() of the lock or the
   magazine mark passes:
     - O_NOFOLLOW, so a symlink planted at the path fails the open()
       itself with ELOOP rather than being followed onto whatever it
       points at;
     - O_NONBLOCK, so the open() itself can never block: a FIFO (or
       certain device nodes) dropped at the path would otherwise hang
       an O_RDONLY open with no writer attached, before any later check
       ever runs; it has no effect on a regular file's later read()/
       write(), and none on flock();
   and the resulting fd is then checked to be a regular file with
   exactly one hard link (S_ISREG && st_nlink == 1) before anything is
   locked, read or written, refused otherwise (process_lock_acquire()
   throws; magazine_mark_read() returns false, since it runs before
   every load and must never fail the session). That check catches what
   O_NOFOLLOW/O_NONBLOCK do not: a directory (an O_RDONLY open of a
   directory succeeds), a device node, or a hard link onto some other
   file this process can write to (not a symlink at all: st_nlink
   climbs to 2 or more, which a lock/mark file this code created never
   has). The mark is additionally never modified in place:
   magazine_mark_write() writes a private temp file next to it and
   rename()s that over the mark path, so the write can never land
   inside whatever the mark path used to point to. A mark that cannot
   be written or read is by design not a failure: it is a hint for a
   load that spans two processes, and the hardware is re-verified
   before every load anyway (see the comment below), so the worst case
   is falling back to asking the operator to reseat the magazine.

   What this is not: a security boundary against a privileged or
   same-user attacker who can also write to /tmp at will. It is a
   cooperative convention between two trusted programs (the driver and
   this backend) sharing one predictable, world-writable path, hardened
   against the ordinary local hazards of that spot (stale symlinks,
   FIFOs, hard links) rather than against a determined adversary who
   races the open() or has broader filesystem control.

   Reference-counted within a process: process_lock_acquire() and
   process_lock_release() must be called in matched pairs (an acquire
   while already held just adds a reference), so that one owner's
   release, e.g. a failed sane_open of a second handle, cannot drop
   another owner's still-open session. genesys.cpp's sane_open_impl/
   sane_close_impl ties one reference to the lifetime of one successful
   open via a small RAII guard, matching the driver's own lock semantics.

   Deliberately free of genesys headers (no genesys.h, no Genesys_Device)
   so it can be compiled and exercised standalone, without pulling in the
   rest of the backend, using a small probe program that links only this
   file. */

#ifndef BACKEND_GENESYS_GL126_LOCK_H
#define BACKEND_GENESYS_GL126_LOCK_H

#include <string>

namespace genesys {
namespace gl126 {

/** Default lock path: "/tmp/of135i-07b3-1436.lock". */
const char* process_lock_default_path();

/** Effective lock path: $OF135I_LOCK_FILE if set, else the default. */
std::string process_lock_path();

/** Acquire the driver's process lock, non-blocking. Reference-counted:
    each successful call to process_lock_acquire(), whether it takes
    the flock for the first time or finds it already held by this
    process, increments an internal reference count, and must be
    paired with exactly one call to process_lock_release(). This lets
    two independent owners in the same process (e.g. one open session
    and a second, still-being-opened one) hold the lock without either
    one's release dropping the other's.

    Returns true once the lock is held, either newly acquired (fresh
    flock, reference count set to 1), or already held by this process
    (idempotent: the flock is not retaken, the reference count is
    incremented). Returns false if another process holds it
    (EWOULDBLOCK/EAGAIN on flock); when `holder` is non-null, it is set
    to the holder line read back from the lock file (may be empty if the
    file could not be read).

    Throws std::runtime_error, with strerror() text, on any other
    failure (open() or flock() erroring for a reason other than the lock
    being held); nothing is considered acquired in that case. */
bool process_lock_acquire(std::string* holder);

/** Release one reference taken by process_lock_acquire(). No-op if the
    reference count is already zero. Only the release that brings the
    count to zero actually unlocks (LOCK_UN) and closes the fd. */
void process_lock_release();

/** Whether this process currently holds the lock (reference count > 0). */
bool process_lock_held();

/** Current reference count (0 if not held). Exposed for the probe and
    the test suite; not needed by ordinary callers. */
int process_lock_refs();

// ------------------------------------------------ the magazine mark

/* The magazine flow can span two steps with the operator in between: the
   "Load film" option releases the magazine (the vendor's jog), the
   person takes it out and reseats it to the stop, and the load itself
   waits for that reseat before running. Inside one frontend that holds
   the device open (digiKam) a fact like "released" can live in memory;
   `scanimage` cannot, each invocation is a new process, and a load
   without the jog before it, in the same power cycle, fails.

   So the fact is also written next to the process lock, as
   `<lock path>.magazine`, holding the device it applies to. It is never
   trusted on its own: before a load the backend re-reads the hardware
   (reg 0x01 idle-homed, the loader-sensor bit set, the device-open
   register state), and a power cycle both re-enumerates the unit under a
   new address and leaves reg 0x01 cold, so a stale mark cannot authorise
   anything. The mark is a hint that survives a process, not a state.

   The mark carries one of four kinds. "released" (the original: Load
   film's jog already ran, only the bare "load" program is needed) and
   "ejected" (an eject completed; the next scan must also replay the
   device-open table first, since nothing has written it since) are both
   read from the mark's first word, which used to be the constant
   "released" and is now whichever name applies, so an old-format mark
   still reads back byte-identically to a new one.

   A third kind, "loaded", covers the case where "Load film" runs the
   whole flow (release, wait for the reseat edge, and LOAD) in one button
   press: a load can complete in a process that then exits
   (`scanimage -n --load-film`) before the scan that uses it runs in a
   separate process, so a cross-process "the magazine is loaded" fact is
   needed the same way "a release/eject is pending" already is.
   `load_document()` never runs LOAD itself: it only checks this mark (or
   the in-process state) to decide whether to let a scan through.

   A fourth kind, "failed", covers a magazine sequence that fails and
   leaves the transport in a state nobody can name: that fact must not be
   dropped the moment the failing process exits, or a following
   `scanimage` invocation would have no way to know the previous one had
   failed, and would try a scan (or another magazine action) against a
   transport whose state was never established. `MagazineFailGuard`
   (sane/gl126.cpp) writes this mark on any failure instead of clearing
   whatever was there; every entry point that already refuses on an
   in-process Failed state (Load film, Eject film, load_document())
   refuses the same way on a matching `failed` mark from Unknown. Only a
   cold reg 0x01 read clears it, the one event that actually
   re-establishes a known transport state (a power cycle), checked at
   Load film's own start and by Check status. */

/** The four things a magazine mark can mean. */
enum class MagazineMarkKind { Released, Ejected, Loaded, Failed };

/** "released", "ejected", "loaded" or "failed", also the word the mark
    file leads with. */
const char* magazine_mark_kind_name(MagazineMarkKind kind);

/** Path of the magazine mark: the lock path plus ".magazine". */
std::string magazine_mark_path();

/** Record that `device_key` (the SANE device name, e.g.
    "libusb:001:007", it carries the USB address, which a power cycle
    changes) is waiting for a load of the given kind. Returns false if the
    file could not be written; a mark that cannot be written is not fatal
    (the in-process record still works for a frontend that stays open),
    so callers log and continue. */
bool magazine_mark_write(MagazineMarkKind kind, const std::string& device_key);

/** Back-compat convenience: writes a Released mark, byte-identical to
    what this function always produced before the Ejected kind existed.
    Existing callers of this form must keep working unmodified. */
bool magazine_mark_write(const std::string& device_key);

/** The kind and device key of a pending mark, or false when there is none
    (or it could not be read). */
bool magazine_mark_read(MagazineMarkKind* kind, std::string* device_key);

/** Back-compat convenience: true only for a Released mark, what this
    function always meant before the Ejected kind existed. An Ejected
    mark reads as "no mark" through this overload, exactly as it would
    have before that kind existed (kept for the same callers as the
    write overload above). */
bool magazine_mark_read(std::string* device_key);

/** What magazine_mark_clear() actually did, so a caller that logs the
    outcome (sane/gl126.cpp's clear_magazine_mark()) can say which. */
enum class MagazineMarkClearResult { Removed, NonePresent, Error };

/** Remove the mark. Removing nothing (there was none) is not an error. */
MagazineMarkClearResult magazine_mark_clear();

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_LOCK_H
