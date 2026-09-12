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
    /** Budget for PollMasked (docs/sane-hook5-frame.md section 3/4):
        POSITION's W3 (class F, FEEDL-scaled -- see position_timeout_ms())
        and PARK's Wait A/Wait B share this one policy value per
        run_program() call; a caller running "park" should pass a value
        generous enough for both waits (park_semantic()'s own budgets are
        15 s and 30 s respectively -- there is no per-op override here,
        so the hook picks one value for the whole program). */
    unsigned masked_timeout_ms = 5000;
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
      PollMasked    -> loop: control_read(op's own request/value/index,
                       ..., 2); (reply[0] & op.mask) == op.want -> done,
                       recorded (a PollRecord); else timeout
                       (policy.masked_timeout_ms) -> OpsError{PollTimeout}
                       (message carries first/last), nothing further
                       sent; else sleep_ms(...). docs/sane-hook5-frame.md
                       section 3/4: POSITION's W3 (class F on reg 0x101)
                       and PARK's Wait A (reg 0x35 bit 0x40)/Wait B (the
                       PARK_COMPLETE status word).
      ReadModifyWrite -> read register (op.index >> 8) via
                       control_read(op.request, op.value, op.index, ...,
                       2), UNLESS the immediately preceding op is a
                       PollMasked with the identical (request, value,
                       index) -- then the value is that poll's own last
                       recorded reply instead of a fresh read (docs/sane-
                       hook5-frame.md section 4: park_semantic()'s reg
                       0x35 clear reuses Wait A's own last read, verified
                       by tests/test_sane_ops.py's park wire-equality
                       test); either way, control_write(0x04, 0x0083,
                       0x0000, [reg, (v & op.mask) | op.want], 2) --
                       no ack read (see the Op doc comment above).

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

// ------------------------------------------- hook 8: dual-light profiles

/** SHADING2_TARGET_A / _B (of135i/calibrate.py): the vendor's per-address
    white targets of the dual-light profiles' second shading upload.
    Address A (0x10014000) is applied by the scanner to the ODD (visible)
    lines, address B (0x10034000) to the EVEN (IR) lines -- the empirically
    corrected assignment (docs/protocol-notes.md pass 18, device.py's
    _scan_dual). */
constexpr double kShading2TargetA = 61440.0;
constexpr double kShading2TargetB = 90112.0;

/** One line subset of an alternating-line buffer: every second line of a
    `lines * width * 6` byte RGB16LE buffer starting at line `parity`
    (0 = even = the IR pass, 1 = odd = the visible pass), as a contiguous
    `(lines/2) * width * 6` byte buffer -- numpy's `arr[parity::2]`.
    Throws std::invalid_argument on a malformed buffer or an odd line
    count. */
std::vector<std::uint8_t> alternate_lines(const std::uint8_t* buf, std::size_t len,
                                          unsigned lines, unsigned width, unsigned parity);

/** The dual-light profiles' second (white-uniformity) shading upload for
    ONE table -- ported from of135i/calibrate.py's shading_table2_dual().
    `white`/`dark`: one line subset each (alternate_lines() of the verify
    and the shading measurement), `lines` lines of `width` px. Per
    pixel/channel: offset = round-half-even of the mean of `dark`; gain =
    clip(round-half-even(target * 0x4000 / max(mean(white), 1)), 1, 65535)
    -- NOT the plain profile's (white - f0) denominator. Returns
    shading_upload_len(width) bytes. */
std::vector<std::uint8_t> shading_table2_dual(const std::uint8_t* white, std::size_t white_len,
                                              const std::uint8_t* dark, std::size_t dark_len,
                                              unsigned lines, unsigned width, double target);

/** The image geometry of one profile's frame, since the Test 58/61 A+C
    migration sourced from profile.frames[frame-1] (the frozen ledger
    gen_sane_tables.frame_geom_entries() emits from of135i/holder.py's
    overscan_geometry()/dual_overscan_geometry()), NOT from the legacy
    profile.captured_lines/chunk_count fields (docs/holder-position-
    design.md; docs/sane-hook8-dual.md section 3 for the field meanings,
    now frame-dependent):
      wire_lines     the line-count register value written -- frames[].line_register
                     (plain: incl the 8-line drain; dual: the interleaved
                     wire count) -- begin_scan injects this;
      read_lines     raw lines the backend reads off the wire --
                     frames[].read_lines; the byte budget and chunk driver
                     size from THIS, not wire_lines;
      image_lines    lines of one pass: read_lines for plain, read_lines/2
                     for dual (even = IR, odd = visible);
      shift_lines    the colour-line shift consumed on the host, R to B, at
                     this dpi: 24 * dpi / 3600 (the model's ld_shift at the
                     motor's base dpi, scaled as the core scales it);
      delivered_lines frames[].delivered_lines (the frontend receives this,
                     NOT image_lines - shift_lines recomputed -- the ledger
                     is authoritative; a consistency assertion checks the
                     two agree and throws std::invalid_argument, fail-
                     closed, if the wiring or the table generation is wrong);
      chunk_count    frames[].chunks -- bulk reads of chunk_len, the last
                     one shorter when read_lines is not a multiple of
                     lines_per_chunk. */
struct FrameGeometry {
    bool dual = false;
    unsigned width = 0;             // raw px read off the wire (profile.image_width)
    unsigned delivered_width = 0;   // px delivered to the frontend after the host
                                    // ScaleRows: == width for every profile but the
                                    // anisotropic dpi2400 (3600 across / 2400 along),
                                    // where it is 3504 so the delivered pixels are square
    unsigned wire_lines = 0;
    unsigned read_lines = 0;
    unsigned image_lines = 0;
    unsigned shift_lines = 0;
    unsigned delivered_lines = 0;
    unsigned chunk_len = 0;
    unsigned chunk_count = 0;
};
/** Throws std::invalid_argument (before any use) if `frame` is outside
    1-kFeedlFrameMax, or if the ledger's own invariant (delivered_lines +
    shift_lines == image_lines) fails -- the latter means frames[] and
    this function's wiring have drifted apart, or the table generator's
    output changed shape; either way, nothing has been written. */
FrameGeometry frame_geometry(const Profile& profile, unsigned frame);

// ------------------------------------------------- hooks 5-7: the frame

/** FEEDL_FRAME1 (of135i/tables.py) -- LEGACY: the vendor capture grid's
    absolute POSITION target for frame 1, 1/7200 inch (HWDPI) units from
    home, NOT the runtime positioning authority since Test 58 (that is
    profile.frames[frame-1].feedl, via the two-arg feedl_for_frame()
    below). Used only by the single-arg feedl_for_frame() overload below,
    kept for capture-evidence/test callers. The dual-light captures carry
    6746 (Profile::feedl_frame1, also legacy). */
constexpr unsigned kFeedlFrame1 = 6743;
/** FEEDL_PITCH (of135i/tables.py) -- LEGACY: the vendor capture grid's
    step between frames (38.0 mm film pitch), same status as kFeedlFrame1
    above. */
constexpr unsigned kFeedlPitch = 10760;

/** The film holder's aperture count -- the largest frame number
    feedl_for_frame() will turn into a FEEDL target. Six, not four: the
    vendor's whole-holder 600 dpi pass images the empty holder end to
    end and shows six apertures on a constant pitch (measured,
    docs/holder-geometry.md), and the vendor's own WIA batch capture
    positioned to frames 5 and 6 directly (FEEDL 49796 and 60174).
    Mirrors gl126.h's kFrameMax (genesys::gl126::kFrameMax) under a
    different name, not the same symbol: this header is deliberately
    free of genesys headers (see the file comment above) and cannot
    include gl126.h to share it. Keep the two numbers equal by hand --
    the way both already mirror of135i/holder.py::STRIP.frames, the
    driver's one Python-side authority on the same holder. */
constexpr unsigned kFeedlFrameMax = 6;
/** The largest FEEDL target this runner will ever hand to POSITION,
    independent of which frame number produced it -- a second guard
    behind kFeedlFrameMax, so a bad FEEDL is caught however it was
    computed, including one built from a valid frame number against a
    wrong table. The load flow's traverse, the longest move this unit
    is known to make, run on every load
    (of135i/holder.py::FEEDL_CEILING). */
constexpr unsigned kFeedlCeiling = 71490;

/** LEGACY: absolute FEEDL target for `frame` (1-based) on the vendor
    capture grid (kFeedlFrame1 + (frame-1)*kFeedlPitch), ported from
    of135i/tables.py's feedl_for_frame() (docs/sane-hook5-frame.md
    section 4, "Injections") -- NOT the runtime positioning authority
    since Test 58; see the two-arg overload below. Throws
    std::invalid_argument, before any transfer, for a frame outside
    1-kFeedlFrameMax or a resulting FEEDL above kFeedlCeiling. */
unsigned feedl_for_frame(unsigned frame);
/** The A+C production authority (docs/holder-position-design.md): the
    absolute FEEDL target for `frame` (1-based) from `profile.frames[frame-1]`
    (the frozen ledger, NOT profile's legacy feedl_frame1/feedl_pitch).
    Throws std::invalid_argument, before any transfer, for a frame outside
    1-kFeedlFrameMax, or if that frame's end_hwdpi (the pass's furthest
    motor reach) or feedl exceeds kFeedlCeiling -- the same travel ceiling
    overscan_geometry() already checked in Python, inherited verbatim. */
unsigned feedl_for_frame(unsigned frame, const Profile& profile);

/** feedl split into its three POSITION injection bytes (hi/mid/lo --
    of135i/tables.py's feedl_hi/feedl_mid/feedl_lo, POSITION's op-array
    byte offsets 7/9/11 of the injected Write). */
struct FeedlBytes {
    std::uint8_t hi;
    std::uint8_t mid;
    std::uint8_t lo;
};
FeedlBytes feedl_bytes(unsigned feedl);

/** POSITION's W3 completion budget for a move of `feedl` steps: 3x the
    captured frame-1 move's duration (1.6141 s), scaled linearly with
    FEEDL and never below the base budget -- ported from of135i/
    device.py's position_timeout_scale() (docs/sane-hook5-frame.md
    section 3/6: "3 * 1.6141 * position_timeout_scale"). Pass the result
    as RunPolicy::masked_timeout_ms when running the "position" program
    for a frame other than 1. */
unsigned position_timeout_ms(unsigned feedl);

/** Read one image-data chunk (docs/sane-hook5-frame.md section 2/4,
    hook 6b -- the GL126 branch of bulk_read_data()). Failures are
    reported via the same OpsError/OpsFailure used by run_program()
    (BadAck, ShortBulk), fail-closed, nothing further sent for this
    chunk; `op_index` is 0 for a BadAck, 1 for a ShortBulk -- this
    function is not table-driven, so there is no real op index.

    A control write of
    the 8-byte buffer descriptor `[00 00 00 10][len LE32]` (address fixed
    at 0x10000000, wIndex 8 for the very first chunk of a scan and 0 for
    every one after, `first` selects which), the ack read (0x0c/0x008e/
    0x0020, must be 0x55 -- OpsError{BadAck} otherwise), then ONE bulk IN
    of `len` bytes into `data` (OpsError{ShortBulk} on a short read).

    DEVIATION, evidence-backed (see this task's report): the captured
    wire (of135i/tables.py's SCAN ops 321-356) shows each chunk's `len`
    bytes arriving as ~33 raw USB-level bulk reads (mostly 16384 B, two
    odd-sized ones at the end) with NO trailing "bulk-done" read
    (0x0c/0x008e/0x0018) anywhere in the image-chunk sequence, unlike
    every other buffer transfer in this codebase. That fragmentation is
    treated here as a USB-packet-level artifact of the reference
    capture, not protocol behaviour to reproduce (the same "provenance,
    not behaviour" principle docs/sane-port.md decision 3 already
    applies to captured pacing) -- read_image_chunk() issues ONE
    Wire::bulk_read() call per chunk, and emits NO bulk-done read,
    matching the capture's own absence of one. A stricter, byte-for-byte
    replay of the 33/12-fragment breakdown was considered and rejected
    as overfitting to reference-unit USB artifacts; see the report for
    the full reasoning. */
void read_image_chunk(Wire& wire, std::uint8_t* data, std::size_t len, bool first);

/** Scan-pass bookkeeping for hooks 6-7 (docs/sane-hook5-frame.md), kept
    genesys-free so the transitions are unit-tested offline. The one thing
    it decides is whether the verified PARK may run: PARK is defined from
    the END of a complete scan pass and from nowhere else (Test 52 attempt
    1 ran it from post-shading and its Wait B never completed; a pass
    aborted half-way -- a failed chunk read, a frontend cancel -- is a
    state PARK has never been run from). Anything but Complete refuses,
    writes nothing, and closes the session (Failed) so that no later call
    -- a second cancel, the close path -- tries again; the operator
    power-cycles, as with the driver.

        Idle --arm()--> Armed --chunk_begin()--> Streaming --chunk_done()
        (bytes >= expected)--> Complete --parked()--> Parked --arm()--> Armed
        any state --fail()--> Failed (terminal; a new sane_open resets it,
        gated by the hardware check that reg 0x01 reads idle) */
enum class ScanPassState : std::uint8_t { Idle, Armed, Streaming, Complete, Parked, Failed };
const char* scan_pass_state_name(ScanPassState s);

enum class ParkDecision : std::uint8_t {
    Run,            // Complete: every expected byte was read; PARK may run
    NoPass,         // Idle: nothing was started, nothing to write
    AlreadyParked,  // Parked: the core's second end_scan call, nothing to do
    AbortedPass,    // Armed/Streaming: cancelled mid-pass; refused, now Failed
    Failed          // an earlier failure already closed the pass; nothing written
};
const char* park_decision_name(ParkDecision d);

class ScanPass {
public:
    ScanPassState state() const { return state_; }
    std::size_t bytes_expected() const { return expected_; }
    std::size_t bytes_read() const { return read_; }

    /** begin_scan succeeded: Idle/Parked -> Armed with the raw byte total the
        pass must deliver. Returns false (state unchanged) from any other
        state -- the caller refuses the scan. */
    bool arm(std::size_t bytes_expected);
    /** A chunk read is about to start. Armed -> Streaming and *first = true
        (the wIndex-8 descriptor); Streaming stays with *first = false.
        Returns false from any other state -- the caller refuses the read. */
    bool chunk_begin(bool* first);
    /** A chunk of `bytes` arrived in full. Streaming -> Complete once the
        expected total is reached. */
    void chunk_done(std::size_t bytes);
    /** Anything failed (a chunk read, PARK itself): -> Failed, terminal. */
    void fail();
    /** end_scan asks whether PARK may run. AbortedPass has the side effect
        of closing the pass (-> Failed) so no later call retries. */
    ParkDecision park_decision();
    /** PARK ran to its completion wait: Complete -> Parked. */
    void parked();

private:
    ScanPassState state_ = ScanPassState::Idle;
    std::size_t expected_ = 0;
    std::size_t read_ = 0;
};

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_OPS_H
