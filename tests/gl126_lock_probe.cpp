/* Tiny standalone probe for sane/gl126_lock.{h,cpp} -- used only by
   tests/test_sane_lock.py. Not part of the SANE backend build.

   Usage:
     probe try    -- attempt to acquire the lock once.
                     ACQUIRED (exit 0), or BUSY <holder> (exit 3).
     probe hold   -- acquire the lock, print HELD and flush, then block
                     reading stdin until EOF, release, print RELEASED,
                     exit 0. If the lock cannot be acquired: BUSY
                     <holder>, exit 3.
     probe nested -- exercise the reference count within one process,
                     modelling two GL126 sessions (A = a long-lived open,
                     B = a second open attempt that fails after taking
                     its own reference and cleans up after itself):
                       acquire (A)               -> "A_HELD refs=1"
                       acquire again (B)          -> "B_ACQUIRED refs=2"
                       release once (B's cleanup) -> "B_RELEASED refs=1 held=1"
                     then blocks reading stdin until EOF (so a caller can
                     verify the lock is still refused while A's
                     reference stands), then:
                       release (A's cleanup)      -> "A_RELEASED refs=0 held=0"
                     exit 0. If the first acquire fails: BUSY <holder>,
                     exit 3.
     probe release-unheld -- call release() with no prior acquire (must
                     be a no-op), then acquire() (must still succeed)
                     -> "OK", exit 0.

   Lock path comes from $OF135I_LOCK_FILE, same as the driver and the
   backend.
*/

#include "../sane/gl126_lock.h"

#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>

using genesys::gl126::process_lock_acquire;
using genesys::gl126::process_lock_held;
using genesys::gl126::process_lock_refs;
using genesys::gl126::process_lock_release;

namespace {

void drain_stdin_until_eof()
{
    std::string line;
    while (std::getline(std::cin, line)) {
        // discard
    }
}

int do_try()
{
    std::string holder;
    if (!process_lock_acquire(&holder)) {
        std::cout << "BUSY " << holder << std::endl;
        return 3;
    }
    std::cout << "ACQUIRED" << std::endl;
    return 0;
}

int do_hold()
{
    std::string holder;
    if (!process_lock_acquire(&holder)) {
        std::cout << "BUSY " << holder << std::endl;
        return 3;
    }
    std::cout << "HELD" << std::endl;
    std::cout.flush();

    drain_stdin_until_eof();

    process_lock_release();
    std::cout << "RELEASED" << std::endl;
    return 0;
}

int do_nested()
{
    std::string holder;
    if (!process_lock_acquire(&holder)) {
        std::cout << "BUSY " << holder << std::endl;
        return 3;
    }
    std::cout << "A_HELD refs=" << process_lock_refs() << std::endl;
    std::cout.flush();

    // B: a second, independent acquire in the same process (models a
    // second open attempt while A's session is still live).
    std::string holder_b;
    if (!process_lock_acquire(&holder_b)) {
        // Should not happen -- acquiring while already held is
        // idempotent -- but report it plainly if it ever does.
        std::cout << "B_BUSY " << holder_b << std::endl;
        return 3;
    }
    std::cout << "B_ACQUIRED refs=" << process_lock_refs() << std::endl;
    std::cout.flush();

    // B's open failed downstream; B cleans up its own reference only.
    process_lock_release();
    std::cout << "B_RELEASED refs=" << process_lock_refs()
               << " held=" << (process_lock_held() ? 1 : 0) << std::endl;
    std::cout.flush();

    // Hold here so the test can confirm A's reference still excludes
    // other processes.
    drain_stdin_until_eof();

    // A's session closes normally.
    process_lock_release();
    std::cout << "A_RELEASED refs=" << process_lock_refs()
               << " held=" << (process_lock_held() ? 1 : 0) << std::endl;
    return 0;
}

int do_release_unheld()
{
    process_lock_release(); // no-op: nothing was acquired
    std::string holder;
    if (!process_lock_acquire(&holder)) {
        std::cout << "BUSY " << holder << std::endl;
        return 3;
    }
    std::cout << "OK" << std::endl;
    process_lock_release();
    return 0;
}

} // namespace

int main(int argc, char** argv)
{
    if (argc != 2) {
        std::cerr << "usage: " << argv[0]
                   << " try|hold|nested|release-unheld" << std::endl;
        return 2;
    }

    try {
        if (std::strcmp(argv[1], "try") == 0) {
            return do_try();
        }
        if (std::strcmp(argv[1], "hold") == 0) {
            return do_hold();
        }
        if (std::strcmp(argv[1], "nested") == 0) {
            return do_nested();
        }
        if (std::strcmp(argv[1], "release-unheld") == 0) {
            return do_release_unheld();
        }
    } catch (const std::exception& e) {
        std::cerr << "ERROR " << e.what() << std::endl;
        return 2;
    }

    std::cerr << "usage: " << argv[0]
               << " try|hold|nested|release-unheld" << std::endl;
    return 2;
}
