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

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <stdexcept>
#include <string>
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

    /** Bulk OUT to EP 0x02 (docs/sane-hook4-shading.md section 6).
        Returns the number of bytes actually written (may be less than
        `len`: a short write is not itself an error at this layer --
        run_program() is what turns a short BulkOut into
        OpsFailure::ShortBulkOut). */
    virtual std::size_t bulk_write(const std::uint8_t* data, std::size_t len) = 0;

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
    BadAck,           // AckRead did not reply 0x55
    PollTimeout,      // PollDataReady/PollClass never settled within the budget
    ShortBulk,        // BulkIn returned fewer bytes than the op called for
    MissingInjection, // a program injection (byte or bulk) has no value in
                       // the map(s) passed to run_program() -- checked
                       // before any transfer (docs/sane-hook3-gain.md
                       // section 6, Part B/1; docs/sane-hook4-shading.md
                       // section 6); also thrown, before any of that, if
                       // the generated table itself has a BulkOut with no
                       // captured payload (data == nullptr) that no
                       // OpBulkInjection covers -- a mis-generated table,
                       // never a live-data condition
    BadInjection,     // a bulk injection value is longer than its BulkOut
                       // ops' combined length -- checked before any
                       // transfer (docs/sane-hook4-shading.md section 6,
                       // Part B/2)
    ShortBulkOut,      // BulkOut wrote fewer bytes than the op called for
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

/** Tunable wait policy for PollDataReady/PollClass (docs/sane-hook2-
    offset.md section 3: "2 s [timeout]; captured 16 ms"; docs/sane-
    hook4-shading.md section 3: PollClass gets its own, longer budget --
    "5 s"). The 4 ms interval keeps a real timeout well under a second
    of wall time while still being many multiples of the captured
    settle time. */
struct RunPolicy {
    unsigned poll_timeout_ms = 2000;
    unsigned poll_interval_ms = 4;
    unsigned class_timeout_ms = 5000;
};

/** Execute one OpProgram against `wire`, appending to `out` (so a caller
    can run several programs into one RunResult if it wants a combined
    log; a fresh RunResult per program is the normal case). Throws
    OpsError, fail-closed, per docs/sane-hook2-offset.md section 3 and
    docs/sane-hook4-shading.md section 3:

      Write         -> control_write(request, value, index, data, len) --
                       or, for a Write the program injects into (see
                       below), the same call with `data` replaced by a
                       copy of the payload patched at the injection's
                       byte offset(s).
      AckRead       -> control_read(0x0c, 0x008e, 0x0020, ..., 1); reply
                       != 0x55 -> OpsError{BadAck}, nothing further sent.
      Read          -> control_read with the op's own setup; recorded,
                       never fails on a mismatch.
      PollDataReady -> loop: control_read(0x04, 0x018e, 0x0122, ..., 2);
                       (reply[0] & 0x01) -> done, recorded; else timeout
                       (policy.poll_timeout_ms) -> OpsError{PollTimeout}
                       (message carries first/last), nothing further
                       sent; else sleep_ms(...).
      PollClass     -> loop: control_read(op's own request/value/index,
                       ..., 2); (reply[0] & 0xf0) == (op.data[0] & 0xf0)
                       -> done, recorded (a PollRecord, same as
                       PollDataReady); else timeout
                       (policy.class_timeout_ms) -> OpsError{PollTimeout}
                       (message carries first/last), nothing further
                       sent; else sleep_ms(...).
      BulkIn        -> bulk_read(len); short -> OpsError{ShortBulk}
                       (message carries got/want) -- the partial data is
                       still appended to out.buffers before the throw.
      BulkOut       -> bulk_write(data, len) -- `data` is the op's own
                       captured chunk, or, for a BulkOut a bulk
                       injection covers (see below), the matching slice
                       of the injected payload; short write -> OpsError
                       {ShortBulkOut} (message carries got/want), nothing
                       further sent.
      BulkDone      -> control_read(0x0c, 0x008e, 0x0018, ..., 1);
                       recorded, never fails on a mismatch.

    BulkOut coverage (structural, checked first of all, before either
    injection check below): every BulkOut op the table generator emitted
    with `data == nullptr` -- tools/gen_sane_tables.py never keeps a
    captured chunk that a bulk injection replaces, since it is the
    reference unit's own calibration data -- must be covered by an
    OpBulkInjection. A BulkOut with no data and no covering injection is
    a mis-generated table and throws OpsError{MissingInjection}
    (message names the op) with zero transfers done; this cannot happen
    for any table gen_sane_tables.py currently emits (op_bulk_injections_
    for()'s own contiguity check would already have failed at generation
    time) and exists as defensive fail-closed behaviour.

    Injections (docs/sane-hook3-gain.md section 6, Part B/1): if
    `prog.injection_count` is nonzero, every one of its OpInjection
    names must be present in `values` (a null `values` counts as none
    present) -- checked BEFORE any transfer, so a missing name throws
    OpsError{MissingInjection} (the message names it) with zero
    transfers done. Names in `values` that no injection uses are
    ignored.

    Bulk injections (docs/sane-hook4-shading.md section 6, Part B/2): if
    `prog.bulk_injection_count` is nonzero, every one of its
    OpBulkInjection names must be present in `bulk_values` (a null
    `bulk_values` counts as none present) -- checked BEFORE any transfer
    (before the byte-injection check above even runs a single transfer),
    so a missing name throws OpsError{MissingInjection}; a present value
    longer than its BulkOut ops' combined length throws OpsError
    {BadInjection} -- both with zero transfers done. A present, short-
    enough value is zero-padded to that combined length and sliced
    across the covered BulkOut ops in order (of135i/tables.py's
    Phase.patched() "bo" rule, exactly). Names in `bulk_values` that no
    bulk injection uses are ignored. */
void run_program(Wire& wire, const OpProgram& prog, RunResult& out,
                 const RunPolicy& policy = RunPolicy(),
                 const std::map<std::string, std::uint8_t>* values = nullptr,
                 const std::map<std::string, std::vector<std::uint8_t>>* bulk_values = nullptr);

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

// ------------------------------------------------- hook 3: gain (S4 above)

/** numpy.percentile(values[0..n), q) with the default 'linear'
    interpolation method, computed on a sorted copy (docs/sane-hook3-
    gain.md section 4). `n` must be >= 1 (throws std::invalid_argument
    otherwise); `q` is expected in [0, 100] (not itself validated -- the
    caller here, gain_codes(), always passes 99.9). */
double percentile_linear(const std::uint16_t* values, std::size_t n, double q);

/** gain_codes()'s result, per channel (R, G, B): the measured 99.9th-
    percentile peak, the AFE gain code it maps to, and whether ANY
    channel's peak reached full scale (65535) -- the hook's "implausible
    AFE state" trigger, not a per-channel property. */
struct GainResult {
    double peak[3];
    std::uint8_t code[3];
    bool saturated;
};

/** AFE gain codes (regs 2/3/4, R/G/B) from a white-line measurement,
    ported from of135i/calibrate.py's gain_codes() (docs/sane-hook3-gain.md
    section 4). `white` is RGB16LE, pixel-interleaved; `len` must be a
    positive multiple of 6 (a whole number of RGB16 pixels), else throws
    std::invalid_argument -- the hook (gl126.cpp, not here) maps that to
    the driver's "malformed white-line measurement" failure. Per channel:
    peak = percentile_linear(channel, 99.9); code = 63 when peak <= 0
    (clamp_nonpositive, the cold-lamp/warmup case), else
    clamp(round_half_even(32 * 31673 / peak), 0, 63). Validated against
    cal-data/capture/cal-frame00501-len31104.bin: expect
    (0x2e, 0x21, 0x29), +/-1 per channel. */
GainResult gain_codes(const std::uint8_t* white, std::size_t len);

/** Tunable warmup retry policy, ported from of135i/device.py's
    _WARMUP_BUDGET_S/_WARMUP_INTERVAL_S/_WARMUP_STABLE_PCT (docs/sane-
    hook3-gain.md section 4). Defaults match the Python driver's. */
struct WarmupPolicy {
    double budget_s = 60.0;
    double interval_s = 5.0;
    double stable_pct = 3.0;
};

/** Everything one gain_with_warmup() call collected -- mirrors
    of135i/device.py's Scanner._diag_warmup dict (attempts, gain_history,
    peak_history, elapsed_s, exhausted) for the hook's diagnostics log. */
struct WarmupRecord {
    unsigned attempts = 0;
    std::vector<std::array<double, 3>> peak_history;
    std::vector<std::array<std::uint8_t, 3>> gain_history;
    double elapsed_s = 0;
    bool exhausted = false;
};

/** Why gain_with_warmup() stopped: Ready (codes_out is valid), Saturated
    (a white line hit full scale -- the driver's "implausible AFE state"
    failure, at any attempt), or Exhausted (the retry budget/measurement
    cap ran out without two stable consecutive non-maxed measurements --
    the driver's LampWarmupError). */
enum class WarmupOutcome : std::uint8_t {
    Ready,
    Saturated,
    Exhausted,
};

/** Port of of135i/device.py's Scanner._gain_with_warmup() (docs/sane-
    hook3-gain.md section 4): `measure()` runs one CAL_WHITE and returns
    the raw white-line buffer (RGB16LE; may throw -- propagated
    untouched, as the Python side's USB errors and Ctrl-C do); `sleep`/
    `now_s` are injected so a test drives the retry loop without wall-
    clock time passing (a fake clock advanced only inside `sleep`).

    Policy: the first measurement's gain_codes() is computed; if it is
    saturated (any channel's peak >= 65535), returns Saturated at once,
    at any attempt -- this check always runs before the "maxed" check
    below. If not maxed (not all three codes == 63), returns Ready at
    once with that measurement's codes -- the verified single-
    measurement path. Otherwise the lamp is warming: `max_measurements
    = 1 + floor(policy.budget_s / policy.interval_s)`; before each
    retry, if `rec.attempts >= max_measurements` or `elapsed +
    policy.interval_s > policy.budget_s`, returns Exhausted (with
    `rec.exhausted = true`) before sleeping again; otherwise sleeps
    `policy.interval_s`, measures again, and returns Ready as soon as
    this measurement AND the previous one are both non-maxed and every
    channel's peak agrees with the previous one within
    `policy.stable_pct` percent (of the previous peak, floor 1.0).
    `rec` records every measurement's peaks/codes and the elapsed time,
    like the Python diag dict, regardless of outcome. */
WarmupOutcome gain_with_warmup(const std::function<std::vector<std::uint8_t>()>& measure,
                               const std::function<void(double)>& sleep,
                               const std::function<double()>& now_s,
                               const WarmupPolicy& policy, WarmupRecord& rec,
                               std::uint8_t codes_out[3]);

// ------------------------------------------------------- hook 4: shading

/** The plain (visible-only) 3600 dpi profile's own shading-measurement
    shape (docs/sane-hook4-shading.md section 1/4): 128 lines, 3762 px
    wide. Every pure function below takes `lines`/`width` explicitly
    rather than defaulting to these -- they are for callers (and tests)
    that want the plain-profile shape by name. */
constexpr unsigned kShadingWidth = 3762;
constexpr unsigned kShadingLines = 128;

/** Per-channel (R, G, B) white-uniformity targets for shading_table2()'s
    default overload, ported from of135i/calibrate.py's SHADING2_TARGETS
    (docs/sane-hook4-shading.md section 4). */
extern const double kShading2Targets[3];

/** _pack_shading()'s output length for `width`, ported from of135i/
    calibrate.py's _shading_upload_len() (docs/sane-hook4-shading.md
    section 4): 126 payload + 2 zero trailer (offset, gain) u16 LE pairs
    per full 512 B block, the final block a shorter unpadded tail.
    Validated: shading_upload_len(3762) == 45856,
    shading_upload_len(5184) == 63192. */
std::size_t shading_upload_len(unsigned width);

/** Packs (offset, gain) u16 LE pairs into the wire block format --
    ported from of135i/calibrate.py's _pack_shading(). `offsets`/`gains`
    must each hold exactly `width * 3` values (pixel-interleaved
    R,G,B,R,G,B...), else throws std::invalid_argument. Returns
    shading_upload_len(width) bytes. */
std::vector<std::uint8_t> pack_shading(const std::vector<std::uint16_t>& offsets,
                                       const std::vector<std::uint16_t>& gains,
                                       unsigned width);

/** The first shading-correction upload payload (H2, docs/sane-hook4-
    shading.md section 1/4), ported from of135i/calibrate.py's
    shading_table(): `meas` is a `lines * width * 6` byte RGB16LE buffer
    (pixel-interleaved, one row per measured line -- the dark-current-
    free 128-line measurement), else throws std::invalid_argument. Per
    pixel/channel: offset = round-half-even of the mean over `lines`;
    gain is the constant 0x4000 for every pixel. Returns
    shading_upload_len(width) bytes (pack_shading()'s packing).
    Reference: cal-data/capture/cal-frame00797-len2889216.bin (lines=128,
    width=3762) against cal-data/capture/shading-upload-len45856.bin,
    byte-identical to of135i.calibrate.shading_table() on the same
    input. */
std::vector<std::uint8_t> shading_table(const std::uint8_t* meas, std::size_t len,
                                        unsigned lines, unsigned width);

/** The second (white-uniformity) shading upload payload (H5, docs/sane-
    hook4-shading.md section 1/4), ported from of135i/calibrate.py's
    shading_table2(): `white`/`dark` are each a `lines * width * 6` byte
    RGB16LE buffer (the re-measured white map and the H1 dark map), else
    throws std::invalid_argument. Per pixel/channel: f0 = round-half-
    even of the mean of `dark` over `lines` (the SAME offsets
    shading_table() would compute from that buffer); gain =
    clip(round-half-even(targets[c] * 0x4000 / max(mean(white) - f0,
    1.0)), 1, 65535). Returns shading_upload_len(width) bytes. This
    overload's `targets` lets a caller pass its own (docs/sane-hook4-
    shading.md scopes the dual-light profiles' own targets/formula --
    shading_table2_dual() in of135i/calibrate.py -- as a later step, not
    ported here); the other overload below uses kShading2Targets. */
std::vector<std::uint8_t> shading_table2(const std::uint8_t* white, std::size_t white_len,
                                         const std::uint8_t* dark, std::size_t dark_len,
                                         unsigned lines, unsigned width,
                                         const double targets[3]);

/** shading_table2() with of135i/calibrate.py's default SHADING2_TARGETS
    (kShading2Targets: 81752, 83490, 87083 for R, G, B) -- the plain
    3600 dpi profile's own formula. */
std::vector<std::uint8_t> shading_table2(const std::uint8_t* white, std::size_t white_len,
                                         const std::uint8_t* dark, std::size_t dark_len,
                                         unsigned lines, unsigned width);

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_OPS_H
