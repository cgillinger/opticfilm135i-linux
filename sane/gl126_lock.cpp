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
#include <sys/stat.h>
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

/* Refuse to lock or write through anything but a plain, ordinary file at
   the well-known path, with exactly one hard link, at the well-known
   path: both the lock and the magazine mark live at a predictable spot
   in a world-writable directory (/tmp), so a symlink planted there
   (pointing at, say, a config file this process can overwrite), a
   directory dropped in its place, or a hard link to some other file
   this process can write must never be locked or written through.
   O_NOFOLLOW makes the open() itself fail (ELOOP) on the symlink case;
   this check catches what O_NOFOLLOW does not: a directory (an
   O_RDONLY open of a directory succeeds), a device node created at the
   path, or -- since a hard link is not a symlink at all, just another
   directory entry for the same regular-file inode -- a hard link onto
   a victim file (st_nlink >= 2; an ordinary, never-linked lock/mark
   file always has st_nlink == 1). Throws with `path` in the message;
   the caller has not touched the fd's content yet. */
void require_plain_file(int fd, const std::string& path)
{
    struct stat st{};
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
        close(fd);
        throw std::runtime_error(std::string("gl126: ") + path +
                                 " is not a regular file, refusing to use it "
                                 "as the lock/mark file");
    }
    if (st.st_nlink != 1) {
        close(fd);
        throw std::runtime_error(std::string("gl126: ") + path +
                                 " has more than one hard link (possible "
                                 "hard-link attack), refusing to use it as "
                                 "the lock/mark file");
    }
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

    // O_NOFOLLOW: the lock lives at a predictable path in a shared,
    // world-writable directory (/tmp) -- never follow a symlink planted
    // there onto some other file this process happens to be able to
    // write to. A symlink at the path fails open() with ELOOP.
    // O_NONBLOCK: the same predictable path could instead hold a FIFO or
    // a device node; without O_NONBLOCK, opening a FIFO for reading (or
    // some character devices) blocks the open() itself until a writer
    // shows up, which would hang this call before the regular-file check
    // below ever runs. On a regular file O_NONBLOCK is a no-op -- it does
    // not affect the flock() or the later read()/write() calls.
    int fd = open(path.c_str(), O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0666);
    if (fd < 0 && (errno == EACCES || errno == EPERM)) {
        // Read-only fallback: flock() works on a read-only fd, it is the
        // write of the holder line afterwards that is best-effort.
        fd = open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    }
    if (fd < 0 && errno == ELOOP) {
        throw std::runtime_error(std::string("gl126: ") + path +
                                 " is a symbolic link, refusing to lock through it");
    }
    if (fd < 0) {
        throw std::runtime_error(std::string("gl126: could not open lock file ") +
                                 path + ": " + std::strerror(errno));
    }

    require_plain_file(fd, path); // throws; e.g. a directory or hard link at the path

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

// ------------------------------------------------ the magazine mark (WP-4)

std::string magazine_mark_path()
{
    return process_lock_path() + ".magazine";
}

const char* magazine_mark_kind_name(MagazineMarkKind kind)
{
    switch (kind) {
    case MagazineMarkKind::Released: return "released";
    case MagazineMarkKind::Ejected:  return "ejected";
    }
    return "released";
}

bool magazine_mark_write(MagazineMarkKind kind, const std::string& device_key)
{
    // Written atomically via a temp file + rename(), never by truncating
    // whatever already sits at the mark path in place: a truncate-in-
    // place would follow a symlink planted at the mark path straight
    // into its target, and would leave a half-written file visible to a
    // concurrent reader. rename() replaces the mark path's directory
    // entry itself -- including a symlink entry -- without ever opening
    // or following what that entry used to point to.
    std::string mark_path = magazine_mark_path();
    std::string tmp_path = mark_path + ".tmp." + std::to_string(static_cast<long>(getpid()));

    // O_EXCL already refuses a pre-existing FIFO/device at the temp path
    // (the path is fresh, PID-suffixed); O_NONBLOCK is added for the same
    // uniformity as every other open() in this file -- it costs nothing
    // here since O_EXCL guarantees this process created the file.
    int fd = ::open(tmp_path.c_str(),
                    O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0666);
    if (fd < 0) {
        return false;
    }

    // The device key goes on its OWN line: it is whatever string the SANE
    // frontend uses to name the device, and it can contain spaces (the
    // backend's own test mode produces "test device:0x07b3:0x1436"), so a
    // space-delimited field would truncate it and silently look like a
    // mark for some other device. First line stays human-readable, since
    // `cat` on this file should say what it is -- and its FIRST WORD is
    // the kind ("released" or "ejected"), not a fixed constant any more.
    std::string line = std::string(magazine_mark_kind_name(kind)) + " " +
                       now_iso8601_utc() + " (sane genesys gl126)\n" + device_key + "\n";
    ssize_t written = ::write(fd, line.data(), line.size());
    ::close(fd);
    if (written != static_cast<ssize_t>(line.size())) {
        ::unlink(tmp_path.c_str());
        return false;
    }
    if (::rename(tmp_path.c_str(), mark_path.c_str()) != 0) {
        ::unlink(tmp_path.c_str());
        return false;
    }
    return true;
}

bool magazine_mark_write(const std::string& device_key)
{
    return magazine_mark_write(MagazineMarkKind::Released, device_key);
}

bool magazine_mark_read(MagazineMarkKind* kind, std::string* device_key)
{
    // O_NONBLOCK: this path is read unconditionally before every load, so
    // the open() itself must never block -- a FIFO planted here (no
    // writer attached) would otherwise hang the open() before the
    // regular-file check below is ever reached. On the regular file this
    // function actually expects, O_NONBLOCK has no effect on the read().
    int fd = ::open(magazine_mark_path().c_str(),
                    O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        return false;
    }
    {
        struct stat st{};
        if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_nlink != 1) {
            // Not a mark we wrote (symlink, directory, FIFO, hard link to
            // a victim file, ...): treat exactly like "no mark" -- this
            // function must never throw.
            ::close(fd);
            return false;
        }
    }
    char buf[256] = {0};
    ssize_t n = ::read(fd, buf, sizeof(buf) - 1);
    ::close(fd);
    if (n <= 0) {
        return false;
    }
    // Line 1: "<kind> <timestamp> (sane genesys gl126)", kind being the
    // first word, "released" or "ejected". Line 2: the device key, whole,
    // spaces and all.
    std::string text(buf, static_cast<std::size_t>(n));
    std::size_t eol = text.find('\n');
    if (eol == std::string::npos) {
        return false;
    }
    std::string first_line = text.substr(0, eol);
    std::size_t space = first_line.find(' ');
    std::string word = space == std::string::npos ? first_line : first_line.substr(0, space);
    MagazineMarkKind found_kind;
    if (word == "released") {
        found_kind = MagazineMarkKind::Released;
    } else if (word == "ejected") {
        found_kind = MagazineMarkKind::Ejected;
    } else {
        // Neither known kind (garbage, or a future/older format): treat
        // exactly like "no mark", same as the old fixed "released" check.
        return false;
    }
    std::size_t key_end = text.find('\n', eol + 1);
    std::string key = text.substr(eol + 1,
                                  key_end == std::string::npos
                                      ? std::string::npos : key_end - eol - 1);
    if (key.empty()) {
        return false;
    }
    if (kind != nullptr) {
        *kind = found_kind;
    }
    if (device_key != nullptr) {
        *device_key = key;
    }
    return true;
}

bool magazine_mark_read(std::string* device_key)
{
    MagazineMarkKind kind = MagazineMarkKind::Released;
    if (!magazine_mark_read(&kind, device_key)) {
        return false;
    }
    // Back-compat: this overload only ever meant a Released mark, so an
    // Ejected one reads as "no mark" through it -- exactly as it would
    // have before that kind existed.
    return kind == MagazineMarkKind::Released;
}

void magazine_mark_clear()
{
    ::unlink(magazine_mark_path().c_str());
}

} // namespace gl126
} // namespace genesys
