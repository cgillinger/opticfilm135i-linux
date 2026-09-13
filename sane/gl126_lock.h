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

/* GL126 process lock -- mutual exclusion with the of135i driver.

   The Python driver (of135i/safety.py, ProcessLock) and this backend are
   two independent programs that can both open the same physical unit
   (07b3:1436, one reference device in existence). Claiming the USB
   interface alone is not enough to keep them apart: the driver's
   read-only sessions (`of135i status`, `doctor`) hold no interface claim
   at all -- they talk to the device just enough to read a register --
   so a `sane_open` racing one of them would not see it and could send a
   command while the driver is mid-read.

   The fix is a convention, not a protocol: both programs take a
   non-blocking exclusive `flock` on the same well-known file before
   touching the device, and release it when they are done. This header
   is the C++ side of that convention, kept byte-for-byte compatible with
   the Python implementation:

     - path: `$OF135I_LOCK_FILE`, or `/tmp/of135i-07b3-1436.lock` if unset
       (one lock path per host, exactly like the driver -- there is only
       ever one unit, so this is not a per-device lock).
     - held file content on success: `pid <pid> since <ISO8601 UTC>
       (sane genesys gl126)\n`, so `cat` on the lock file names the
       holder either way.

   GL126-only: no other genesys ASIC has a conflicting driver, so no
   other command set includes this header or calls into this namespace.

   Reference-counted within a process: process_lock_acquire() and
   process_lock_release() must be called in matched pairs (an acquire
   while already held just adds a reference), so that one owner's
   release -- e.g. a failed sane_open of a second handle -- cannot drop
   another owner's still-open session. genesys.cpp's sane_open_impl/
   sane_close_impl ties one reference to the lifetime of one successful
   open via a small RAII guard; see the "Mutual exclusion with the
   driver" section of docs/sane-port.md for why.

   Deliberately free of genesys headers (no genesys.h, no Genesys_Device)
   so it can be compiled and exercised standalone, without pulling in the
   rest of the backend -- see tests/test_sane_lock.py in the driver repo,
   which builds this file with a tiny probe program. */

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
    each successful call to process_lock_acquire() -- whether it takes
    the flock for the first time or finds it already held by this
    process -- increments an internal reference count, and must be
    paired with exactly one call to process_lock_release(). This lets
    two independent owners in the same process (e.g. one open session
    and a second, still-being-opened one) hold the lock without either
    one's release dropping the other's.

    Returns true once the lock is held -- either newly acquired (fresh
    flock, reference count set to 1), or already held by this process
    (idempotent: the flock is not retaken, the reference count is
    incremented). Returns false if another process holds it
    (EWOULDBLOCK/EAGAIN on flock); when `holder` is non-null, it is set
    to the holder line read back from the lock file (may be empty if the
    file could not be read).

    Throws std::runtime_error, with strerror() text, on any other
    failure (open() or flock() erroring for a reason other than the lock
    being held) -- nothing is considered acquired in that case. */
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

// ------------------------------------------------ the magazine mark (WP-4)

/* docs/sane-wp4-magazine.md section 2.1. The magazine flow is two steps
   with the OPERATOR in between: the "Load film" option releases the
   magazine (the vendor's jog), the person takes it out and reseats it to
   the stop, and the NEXT scan runs the load. Inside one frontend that
   holds the device open (digiKam) the "released" fact can live in memory;
   `scanimage` cannot -- each invocation is a new process -- and a load
   without the jog before it, in the same power cycle, is precisely the
   failure the project spent Tests 11b-15 on.

   So the fact is also written next to the process lock, as
   `<lock path>.magazine`, holding the device it applies to. It is never
   trusted on its own: before a load the backend re-reads the hardware
   (reg 0x01 idle-homed, the loader-sensor bit set, the device-open
   register state), and a power cycle both re-enumerates the unit under a
   new address and leaves reg 0x01 cold, so a stale mark cannot authorise
   anything. The mark is a hint that survives a process, not a state. */

/** Path of the magazine mark: the lock path plus ".magazine". */
std::string magazine_mark_path();

/** Record that `device_key` (the SANE device name, e.g.
    "libusb:001:007" -- it carries the USB address, which a power cycle
    changes) has had its magazine released and is waiting for the load.
    Returns false if the file could not be written; a mark that cannot
    be written is not fatal (the in-process record still works for a
    frontend that stays open), so callers log and continue. */
bool magazine_mark_write(const std::string& device_key);

/** The device key of a pending mark, or false when there is none (or it
    could not be read). */
bool magazine_mark_read(std::string* device_key);

/** Remove the mark. No-op when there is none. */
void magazine_mark_clear();

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_LOCK_H
