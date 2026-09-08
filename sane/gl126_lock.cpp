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

#include "gl126_lock.h"

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <stdexcept>

namespace genesys {
namespace gl126 {

namespace {

/* One fd for the whole process -- one lock path per host, exactly like
   the driver (of135i/safety.py: a single ProcessLock instance per
   session, no per-device path). -1 means not held. flock() is a
   property of the open file description, so a second fd in the same
   process would conflict with the first rather than share it -- hence
   one fd, reference-counted rather than reopened. */
int g_lock_fd = -1;

/* How many acquire() calls are currently outstanding. 0 means not held
   (g_lock_fd == -1 iff g_lock_refs == 0). */
int g_lock_refs = 0;

std::string now_iso8601_utc()
{
    std::time_t t = std::time(nullptr);
    std::tm tm_buf{};
    gmtime_r(&t, &tm_buf);
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S+00:00", &tm_buf);
    return std::string(buf);
}

std::string read_holder(int fd)
{
    if (lseek(fd, 0, SEEK_SET) < 0) {
        return std::string();
    }
    char buf[128];
    ssize_t n = read(fd, buf, sizeof(buf));
    if (n <= 0) {
        return std::string();
    }
    std::string s(buf, static_cast<std::size_t>(n));
    // trim trailing whitespace (the file ends in "\n")
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' ||
                          s.back() == ' ' || s.back() == '\t')) {
        s.pop_back();
    }
    return s;
}

} // namespace

const char* process_lock_default_path()
{
    return "/tmp/of135i-07b3-1436.lock";
}

std::string process_lock_path()
{
    const char* env = std::getenv("OF135I_LOCK_FILE");
    if (env != nullptr && env[0] != '\0') {
        return std::string(env);
    }
    return std::string(process_lock_default_path());
}

bool process_lock_acquire(std::string* holder)
{
    if (g_lock_fd != -1) {
        // Already held by this process: hand out another reference
        // rather than retaking the flock (which would be a harmless
        // no-op anyway, but refs must track how many releases are owed).
        ++g_lock_refs;
        return true;
    }

    std::string path = process_lock_path();

    int fd = open(path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0666);
    if (fd < 0 && (errno == EACCES || errno == EPERM)) {
        // Read-only fallback: flock() works on a read-only fd, it is the
        // write of the holder line afterwards that is best-effort.
        fd = open(path.c_str(), O_RDONLY | O_CLOEXEC);
    }
    if (fd < 0) {
        throw std::runtime_error(std::string("gl126: could not open lock file ") +
                                 path + ": " + std::strerror(errno));
    }

    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
        if (errno == EWOULDBLOCK || errno == EAGAIN) {
            if (holder != nullptr) {
                *holder = read_holder(fd);
            }
            close(fd);
            return false;
        }
        int err = errno;
        close(fd);
        throw std::runtime_error(std::string("gl126: could not lock ") +
                                 path + ": " + std::strerror(err));
    }

    g_lock_fd = fd;
    g_lock_refs = 1;

    // Best-effort holder line, exactly the driver's format (safety.py
    // ProcessLock.acquire); a read-only fd cannot be written to, and
    // that is fine -- the lock itself is what matters.
    if (ftruncate(g_lock_fd, 0) == 0) {
        std::string line = "pid " + std::to_string(static_cast<long>(getpid())) +
                           " since " + now_iso8601_utc() + " (sane genesys gl126)\n";
        ssize_t written = write(g_lock_fd, line.data(), line.size());
        (void)written; // best-effort, mirrors the driver's "except OSError: pass"
    }

    return true;
}

void process_lock_release()
{
    if (g_lock_refs == 0) {
        // No-op: nothing to release. Covers both "never acquired" and
        // "already released" -- callers are not required to track
        // whether their own acquire succeeded before calling this.
        return;
    }
    --g_lock_refs;
    if (g_lock_refs > 0) {
        // Another owner in this process still holds a reference.
        return;
    }
    flock(g_lock_fd, LOCK_UN);
    close(g_lock_fd);
    g_lock_fd = -1;
}

bool process_lock_held()
{
    return g_lock_refs > 0;
}

int process_lock_refs()
{
    return g_lock_refs;
}

} // namespace gl126
} // namespace genesys
