/* Tiny standalone probe for sane/gl126_lock.{h,cpp} -- used only by
   tests/test_sane_lock.py. Not part of the SANE backend build.

   Usage:
     probe try    -- attempt to acquire the lock once.
                     ACQUIRED (exit 0), or BUSY <holder> (exit 3).
     probe hold   -- acquire the lock, print HELD and flush, then block
                     reading stdin until EOF, release, print RELEASED,
                     exit 0. If the lock cannot be acquired: BUSY
                     <holder>, exit 3.

   Lock path comes from $OF135I_LOCK_FILE, same as the driver and the
   backend.
*/

#include "../sane/gl126_lock.h"

#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>

using genesys::gl126::process_lock_acquire;
using genesys::gl126::process_lock_release;

namespace {

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

    // Block until the test closes stdin.
    std::string line;
    while (std::getline(std::cin, line)) {
        // discard
    }

    process_lock_release();
    std::cout << "RELEASED" << std::endl;
    return 0;
}

} // namespace

int main(int argc, char** argv)
{
    if (argc != 2) {
        std::cerr << "usage: " << argv[0] << " try|hold" << std::endl;
        return 2;
    }

    try {
        if (std::strcmp(argv[1], "try") == 0) {
            return do_try();
        }
        if (std::strcmp(argv[1], "hold") == 0) {
            return do_hold();
        }
    } catch (const std::exception& e) {
        std::cerr << "ERROR " << e.what() << std::endl;
        return 2;
    }

    std::cerr << "usage: " << argv[0] << " try|hold" << std::endl;
    return 2;
}
