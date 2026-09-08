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

/* GL126 op-program runner -- SANE hook 2 (docs/sane-hook2-offset.md,
   sections 3 and 6).

   gen_sane_tables.py turns the four phases hook 2 needs (prep, afe_base,
   cal_dark_a, cal_dark_b) into an ordered `OpProgram` per profile
   (gl126_tables.h): one `Op` per captured transfer, transfer boundaries
   and interleaving kept exactly as captured. This header is the runner
   that executes such a program against an abstract `Wire`, plus the pure
   offset/residual computation (docs/sane-hook2-offset.md section 5).

   Deliberately free of genesys headers (no genesys.h, no Genesys_Device),
   like gl126_lock, so it compiles and is exercised standalone -- see
   tests/test_sane_ops.py, which builds this file with a tiny probe
   program (tests/gl126_ops_probe.cpp). gl126.cpp (the main session's
   file, not touched here) implements `Wire` over `UsbDevice` with three
   one-line forwards and calls run_program() for offset_calibration(). */

#ifndef BACKEND_GENESYS_GL126_OPS_H
#define BACKEND_GENESYS_GL126_OPS_H

#include "gl126_tables.h"

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace genesys {
namespace gl126 {

/** The wire, abstracted so the runner and its tests do not depend on
    libusb or genesys's UsbDevice. Every method throws on a transport-
    level failure (a USB error, not a protocol mismatch -- a bad ack or a
    short bulk read are OpsError, not an exception from Wire itself). */
class Wire {
public:
    virtual ~Wire() = default;

    /** Control OUT (bmRequestType 0x40). Throws on failure. */
    virtual void control_write(std::uint8_t request, std::uint16_t value,
                               std::uint16_t index, const std::uint8_t* data,
                               std::size_t len) = 0;

    /** Control IN (bmRequestType 0xc0). Fills `data[0..len)`. Throws on
        failure. */
    virtual void control_read(std::uint8_t request, std::uint16_t value,
                              std::uint16_t index, std::uint8_t* data,
                              std::size_t len) = 0;

    /** Bulk IN from EP 0x81. Returns the number of bytes actually read
        (may be less than `len`: a short read is not itself an error at
        this layer -- run_program() is what turns a short BulkIn into
        OpsFailure::ShortBulk). */
    virtual std::size_t bulk_read(std::uint8_t* data, std::size_t len) = 0;

    /** Sleep for (at least) `ms` milliseconds. */
    virtual void sleep_ms(unsigned ms) = 0;

    /** A monotonic clock, in milliseconds. The fake used in tests
        advances it inside sleep_ms() rather than wall-clock time, so a
        timeout test runs instantly. */
    virtual unsigned now_ms() = 0;
};

/** Why run_program() stopped before finishing the program. Every failure
    is fail-closed: zero further transfers after the one that failed
    (docs/sane-hook2-offset.md section 3's failure-rules table). */
enum class OpsFailure : std::uint8_t {
    BadAck,        // AckRead did not reply 0x55
    PollTimeout,   // PollDataReady never saw bit 0x01 within the budget
    ShortBulk,     // BulkIn returned fewer bytes than the op called for
};

const char* to_string(OpsFailure failure);

/** Thrown by run_program() on any of the OpsFailure conditions above.
    `op_index` is the index into the OpProgram's `ops` array of the op
    that failed. */
class OpsError : public std::runtime_error {
public:
    OpsError(OpsFailure failure_, std::size_t op_index_, const std::string& what)
        : std::runtime_error(what), failure(failure_), op_index(op_index_) {}

    OpsFailure failure;
    std::size_t op_index;
};

/** One PollDataReady site's outcome: the first and last polled reg 0x101
    high byte, how many control_read calls it took, and the elapsed time
    -- exactly what docs/sane-hook2-offset.md section 3 asks the hook to
    log ("the run logs every poll's first and last value"). */
struct PollRecord {
    std::size_t op_index;
    std::uint8_t first;
    std::uint8_t last;
    unsigned polls;
    unsigned elapsed_ms;
};

/** One logged-only read (Read or BulkDone): the reply this run got, and
    the value the capture recorded at the same site, for provenance
    comparison -- never gates success. */
struct ReadRecord {
    std::size_t op_index;
    std::uint16_t value;
    std::uint16_t index;
    std::uint8_t reply[2];
    std::uint8_t reply_len;
    std::uint8_t captured[2];
};

/** Everything one run_program() call collected. */
struct RunResult {
    std::vector<std::vector<std::uint8_t>> buffers;  // one per BulkIn, in order
    std::vector<PollRecord> polls;
    std::vector<ReadRecord> reads;
    std::size_t ops_done = 0;  // completed ops; ties a failure to "no transfer after it"
};

/** Tunable wait policy for PollDataReady (docs/sane-hook2-offset.md
    section 3: "2 s [timeout]; captured 16 ms"). The 4 ms interval keeps a
    real timeout well under a second of wall time while still being many
    multiples of the captured settle time. */
struct RunPolicy {
    unsigned poll_timeout_ms = 2000;
    unsigned poll_interval_ms = 4;
};

/** Execute one OpProgram against `wire`, appending to `out` (so a caller
    can run several programs into one RunResult if it wants a combined
    log; a fresh RunResult per program is the normal case). Throws
    OpsError, fail-closed, per docs/sane-hook2-offset.md section 3:

      Write         -> control_write(request, value, index, data, len).
      AckRead       -> control_read(0x0c, 0x008e, 0x0020, ..., 1); reply
                       != 0x55 -> OpsError{BadAck}, nothing further sent.
      Read          -> control_read with the op's own setup; recorded,
                       never fails on a mismatch.
      PollDataReady -> loop: control_read(0x04, 0x018e, 0x0122, ..., 2);
                       (reply[0] & 0x01) -> done, recorded; else timeout
                       -> OpsError{PollTimeout} (message carries first/
                       last), nothing further sent; else sleep_ms(...).
      BulkIn        -> bulk_read(len); short -> OpsError{ShortBulk}
                       (message carries got/want) -- the partial data is
                       still appended to out.buffers before the throw.
      BulkDone      -> control_read(0x0c, 0x008e, 0x0018, ..., 1);
                       recorded, never fails on a mismatch. */
void run_program(Wire& wire, const OpProgram& prog, RunResult& out,
                 const RunPolicy& policy = RunPolicy());

// ---------------------------------------------------------------- S5/S6

/** The offset computation of docs/sane-hook2-offset.md section 5, per
    channel (R, G, B), ported from of135i/calibrate.py's offset_codes().
    Dark buffers are RGB16LE (len_a/len_b in bytes, each a multiple of 6);
    `mean_a`/`mean_b` and `slope` are exposed for the DBG_info-level log
    the hook writes (docs/sane-hook2-offset.md section 5, "the hook also
    logs dark_a_mean, dark_b_mean, slopes and codes"); `fallback[ch]` is
    true where the bracket slope was below the minimum and `code[ch]`
    fell back to the hardcoded default instead of a computed value. */
struct OffsetResult {
    double mean_a[3];
    double mean_b[3];
    double slope[3];
    std::uint16_t code[3];
    bool fallback[3];
};

OffsetResult offset_codes(const std::uint8_t* dark_a, std::size_t len_a,
                          const std::uint8_t* dark_b, std::size_t len_b);

/** True when `buf` looks like residual/canned data rather than a real
    measurement -- a short repeating block of a few distinct u16 values
    (Test 32). Ported from of135i/calibrate.py's dark_is_residual():
    1 < distinct-u16-count < 32 over the whole buffer (RGB16LE, so `len`
    counts bytes; distinct values are counted over every 2-byte sample,
    not per channel). A single distinct value (a dead AFE, or an
    all-zero buffer) is deliberately NOT residual -- offset_codes()'s
    slope-fallback already owns that case. */
bool dark_is_residual(const std::uint8_t* buf, std::size_t len);

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_OPS_H
