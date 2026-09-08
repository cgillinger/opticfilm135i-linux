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

/** Acquire the driver's process lock, non-blocking.

    Returns true once the lock is held -- either newly acquired, or
    already held by this process (idempotent: a second call is a no-op
    that still returns true). Returns false if another process holds it
    (EWOULDBLOCK/EAGAIN on flock); when `holder` is non-null, it is set
    to the holder line read back from the lock file (may be empty if the
    file could not be read).

    Throws std::runtime_error, with strerror() text, on any other
    failure (open() or flock() erroring for a reason other than the lock
    being held) -- nothing is considered acquired in that case. */
bool process_lock_acquire(std::string* holder);

/** Release the lock. No-op if not currently held by this process. */
void process_lock_release();

/** Whether this process currently holds the lock. */
bool process_lock_held();

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_LOCK_H
