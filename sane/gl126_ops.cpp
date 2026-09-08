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

#include "gl126_ops.h"

#include <algorithm>
#include <cmath>
#include <ios>
#include <sstream>

namespace genesys {
namespace gl126 {

const char* to_string(OpsFailure failure)
{
    switch (failure) {
    case OpsFailure::BadAck:           return "BadAck";
    case OpsFailure::PollTimeout:      return "PollTimeout";
    case OpsFailure::ShortBulk:        return "ShortBulk";
    case OpsFailure::MissingInjection: return "MissingInjection";
    }
    return "Unknown";
}

namespace {

// ------------------------------------------------------------- run_program

// Checked once, before any transfer (docs/sane-hook3-gain.md section 6,
// Part B/1): every injection this program needs must be present in
// `values` (a null `values` counts as none present). Names in `values`
// the program has no injection for are ignored -- not checked here.
void check_injections(const OpProgram& prog,
                      const std::map<std::string, std::uint8_t>* values)
{
    for (std::size_t k = 0; k < prog.injection_count; ++k) {
        const OpInjection& inj = prog.injections[k];
        if (values == nullptr || values->find(inj.name) == values->end()) {
            std::ostringstream oss;
            oss << "gl126_ops: missing injection value for \"" << inj.name
                << "\" (op " << inj.op_index << ", byte " << inj.byte_offset
                << ") -- nothing sent";
            throw OpsError(OpsFailure::MissingInjection, inj.op_index, oss.str());
        }
    }
}

void do_write(Wire& wire, const Op& op, const OpProgram& prog, std::size_t idx,
             const std::map<std::string, std::uint8_t>* values)
{
    if (prog.injection_count == 0) {
        wire.control_write(op.request, op.value, op.index, op.data, op.len);
        return;
    }
    bool has_injection = false;
    for (std::size_t k = 0; k < prog.injection_count; ++k) {
        if (prog.injections[k].op_index == idx) {
            has_injection = true;
            break;
        }
    }
    if (!has_injection) {
        wire.control_write(op.request, op.value, op.index, op.data, op.len);
        return;
    }
    // check_injections() already guaranteed every name is in `values`.
    std::vector<std::uint8_t> patched(op.data, op.data + op.len);
    for (std::size_t k = 0; k < prog.injection_count; ++k) {
        const OpInjection& inj = prog.injections[k];
        if (inj.op_index != idx) {
            continue;
        }
        patched[inj.byte_offset] = values->at(inj.name);
    }
    wire.control_write(op.request, op.value, op.index, patched.data(), patched.size());
}

void do_ack_read(Wire& wire, const Op& op, std::size_t idx)
{
    std::uint8_t reply = 0;
    wire.control_read(op.request, op.value, op.index, &reply, 1);
    if (reply != 0x55) {
        std::ostringstream oss;
        oss << "gl126_ops: ack read at op " << idx << " (wValue=0x" << std::hex
            << op.value << " wIndex=0x" << op.index << std::dec
            << ") got 0x" << std::hex << static_cast<int>(reply)
            << ", want 0x55 -- nothing further sent";
        throw OpsError(OpsFailure::BadAck, idx, oss.str());
    }
}

// Fills a ReadRecord for a logged-only reply (Read or BulkDone): up to
// 2 B are copied into the fixed-size fields, matching what these op
// kinds ever carry (AckRead's 1 B is handled separately, never logged).
void record_read(RunResult& out, const Op& op, std::size_t idx,
                 const std::uint8_t* reply, std::size_t reply_len)
{
    ReadRecord rec{};
    rec.op_index = idx;
    rec.value = op.value;
    rec.index = op.index;
    rec.reply_len = static_cast<std::uint8_t>(reply_len);
    rec.reply[0] = reply_len > 0 ? reply[0] : 0;
    rec.reply[1] = reply_len > 1 ? reply[1] : 0;
    rec.captured[0] = (op.data != nullptr && op.len > 0) ? op.data[0] : 0;
    rec.captured[1] = (op.data != nullptr && op.len > 1) ? op.data[1] : 0;
    out.reads.push_back(rec);
}

void do_read(Wire& wire, const Op& op, std::size_t idx, RunResult& out)
{
    std::uint8_t reply[2] = {0, 0};
    std::size_t len = op.len < 2 ? op.len : 2;
    wire.control_read(op.request, op.value, op.index, reply, len);
    record_read(out, op, idx, reply, len);
}

void do_bulk_done(Wire& wire, const Op& op, std::size_t idx, RunResult& out)
{
    std::uint8_t reply = 0;
    wire.control_read(op.request, op.value, op.index, &reply, 1);
    record_read(out, op, idx, &reply, 1);
}

void do_poll(Wire& wire, const Op& op, std::size_t idx, RunResult& out,
            const RunPolicy& policy)
{
    unsigned start = wire.now_ms();
    std::uint8_t first = 0;
    std::uint8_t last = 0;
    unsigned polls = 0;

    for (;;) {
        std::uint8_t reply[2] = {0, 0};
        wire.control_read(op.request, op.value, op.index, reply, 2);
        ++polls;
        if (polls == 1) {
            first = reply[0];
        }
        last = reply[0];

        if (reply[0] & 0x01) {
            PollRecord rec{idx, first, last, polls, wire.now_ms() - start};
            out.polls.push_back(rec);
            return;
        }

        unsigned elapsed = wire.now_ms() - start;
        if (elapsed > policy.poll_timeout_ms) {
            std::ostringstream oss;
            oss << "gl126_ops: PollDataReady at op " << idx << " (DATAENB, reg 0x101) "
                << "timed out after " << elapsed << "ms: first 0x" << std::hex
                << static_cast<int>(first) << ", last 0x" << static_cast<int>(last)
                << std::dec << " -- bit 0x01 never set, nothing further sent";
            throw OpsError(OpsFailure::PollTimeout, idx, oss.str());
        }
        wire.sleep_ms(policy.poll_interval_ms);
    }
}

void do_bulk_in(Wire& wire, const Op& op, std::size_t idx, RunResult& out)
{
    std::vector<std::uint8_t> buf(op.len);
    std::size_t got = wire.bulk_read(buf.data(), buf.size());
    buf.resize(got);
    // Kept even on a short read: "the partial data is kept in
    // out.buffers before throwing" (docs/sane-hook2-offset.md section 6).
    out.buffers.push_back(buf);
    if (got != static_cast<std::size_t>(op.len)) {
        std::ostringstream oss;
        oss << "gl126_ops: BulkIn at op " << idx << " got " << got
            << " B, want " << op.len << " B -- nothing further sent";
        throw OpsError(OpsFailure::ShortBulk, idx, oss.str());
    }
}

} // namespace

void run_program(Wire& wire, const OpProgram& prog, RunResult& out,
                 const RunPolicy& policy,
                 const std::map<std::string, std::uint8_t>* values)
{
    if (prog.injection_count > 0) {
        check_injections(prog, values);
    }
    for (std::size_t i = 0; i < prog.count; ++i) {
        const Op& op = prog.ops[i];
        switch (op.kind) {
        case OpKind::Write:
            do_write(wire, op, prog, i, values);
            break;
        case OpKind::AckRead:
            do_ack_read(wire, op, i);
            break;
        case OpKind::Read:
            do_read(wire, op, i, out);
            break;
        case OpKind::PollDataReady:
            do_poll(wire, op, i, out, policy);
            break;
        case OpKind::BulkIn:
            do_bulk_in(wire, op, i, out);
            break;
        case OpKind::BulkDone:
            do_bulk_done(wire, op, i, out);
            break;
        }
        // Reached only if op i did not throw: it is done.
        out.ops_done = i + 1;
    }
}

// -------------------------------------------------------------------- S5/S6

namespace {

// docs/sane-hook2-offset.md section 5 / of135i/calibrate.py.
constexpr double kMargin[3] = {211.0, 198.0, 215.0};              // R, G, B
constexpr std::uint16_t kDefault[3] = {0x010B, 0x010A, 0x010B};   // R, G, B
constexpr double kBracketLo = 0x80;   // offset code for dark_a
constexpr double kBracketHi = 0xFF;   // offset code for dark_b
constexpr double kMinSlope = 1.0;     // counts per code step
constexpr int kDarkMinUnique = 32;    // dark_is_residual: 1 < unique < this

// Python's round() is round-half-to-even; std::lround/round() round
// half away from zero, which differs at an exact .5 -- match Python
// exactly so the two implementations agree on any input (docs/sane-
// hook2-offset.md section 5). Callers here only ever pass non-negative
// values (margin/slope with slope > 0 in the branch that computes it),
// but this handles the general case correctly too.
double round_half_even(double x)
{
    double floor_x = std::floor(x);
    double diff = x - floor_x;
    if (diff < 0.5) {
        return floor_x;
    }
    if (diff > 0.5) {
        return floor_x + 1.0;
    }
    // Exactly .5: round to the even neighbour.
    bool floor_is_even = (std::fmod(floor_x, 2.0) == 0.0);
    return floor_is_even ? floor_x : floor_x + 1.0;
}

std::uint16_t read_u16le(const std::uint8_t* p)
{
    return static_cast<std::uint16_t>(p[0]) |
           static_cast<std::uint16_t>(static_cast<std::uint16_t>(p[1]) << 8);
}

} // namespace

OffsetResult offset_codes(const std::uint8_t* dark_a, std::size_t len_a,
                          const std::uint8_t* dark_b, std::size_t len_b)
{
    if (len_a % 6 != 0) {
        throw std::invalid_argument(
            "gl126_ops::offset_codes: dark_a length is not a multiple of 6 "
            "(RGB16LE triples)");
    }
    if (len_b % 6 != 0) {
        throw std::invalid_argument(
            "gl126_ops::offset_codes: dark_b length is not a multiple of 6 "
            "(RGB16LE triples)");
    }

    const std::size_t n_a = len_a / 6;
    const std::size_t n_b = len_b / 6;

    OffsetResult r{};
    for (int ch = 0; ch < 3; ++ch) {
        double sum_a = 0.0;
        for (std::size_t i = 0; i < n_a; ++i) {
            sum_a += read_u16le(dark_a + i * 6 + ch * 2);
        }
        double sum_b = 0.0;
        for (std::size_t i = 0; i < n_b; ++i) {
            sum_b += read_u16le(dark_b + i * 6 + ch * 2);
        }
        double mean_a = n_a ? sum_a / static_cast<double>(n_a) : 0.0;
        double mean_b = n_b ? sum_b / static_cast<double>(n_b) : 0.0;
        double slope = (mean_b - mean_a) / (kBracketHi - kBracketLo);

        r.mean_a[ch] = mean_a;
        r.mean_b[ch] = mean_b;
        r.slope[ch] = slope;

        if (slope < kMinSlope) {
            r.code[ch] = kDefault[ch];
            r.fallback[ch] = true;
            continue;
        }

        double margin_codes = round_half_even(kMargin[ch] / slope);
        double code = kBracketHi + margin_codes;
        if (code < 0.0) {
            code = 0.0;
        } else if (code > 0xFFFF) {
            code = 0xFFFF;
        }
        r.code[ch] = static_cast<std::uint16_t>(code);
        r.fallback[ch] = false;
    }
    return r;
}

bool dark_is_residual(const std::uint8_t* buf, std::size_t len)
{
    if (len < 2) {
        return false;
    }
    const std::size_t n = len / 2;   // a trailing odd byte contributes no sample
    std::vector<bool> seen(1u << 16, false);
    std::size_t distinct = 0;
    for (std::size_t i = 0; i < n; ++i) {
        std::uint16_t v = read_u16le(buf + i * 2);
        if (!seen[v]) {
            seen[v] = true;
            ++distinct;
            if (distinct >= static_cast<std::size_t>(kDarkMinUnique)) {
                return false;   // already >= the ceiling; cannot be residual
            }
        }
    }
    return distinct > 1;
}

// --------------------------------------------------------- hook 3: gain

namespace {

// docs/sane-hook3-gain.md section 4 / of135i/calibrate.py's gain_codes().
constexpr double kGainTarget = 31673.0;
constexpr double kGainDivisor = 32.0;
constexpr std::uint8_t kGainMaxCode = 63;

} // namespace

double percentile_linear(const std::uint16_t* values, std::size_t n, double q)
{
    if (n == 0) {
        throw std::invalid_argument(
            "gl126_ops::percentile_linear: n == 0 (numpy.percentile requires "
            "at least one value)");
    }
    std::vector<double> sorted(n);
    for (std::size_t i = 0; i < n; ++i) {
        sorted[i] = static_cast<double>(values[i]);
    }
    std::sort(sorted.begin(), sorted.end());
    if (n == 1) {
        return sorted[0];
    }
    // numpy's default 'linear' method: pos = q/100 * (n - 1); the value
    // interpolates linearly between the sorted neighbours at floor(pos)
    // and floor(pos) + 1.
    double pos = (q / 100.0) * static_cast<double>(n - 1);
    double floor_pos = std::floor(pos);
    std::size_t i = static_cast<std::size_t>(floor_pos);
    double frac = pos - floor_pos;
    if (i + 1 >= n) {
        return sorted[n - 1];
    }
    return sorted[i] + frac * (sorted[i + 1] - sorted[i]);
}

GainResult gain_codes(const std::uint8_t* white, std::size_t len)
{
    if (len == 0 || len % 6 != 0) {
        throw std::invalid_argument(
            "gl126_ops::gain_codes: white buffer length is not a positive "
            "multiple of 6 (RGB16LE pixels)");
    }
    const std::size_t n = len / 6;

    GainResult r{};
    r.saturated = false;
    std::vector<std::uint16_t> channel(n);
    for (int ch = 0; ch < 3; ++ch) {
        for (std::size_t i = 0; i < n; ++i) {
            channel[i] = read_u16le(white + i * 6 + ch * 2);
        }
        double peak = percentile_linear(channel.data(), n, 99.9);
        r.peak[ch] = peak;
        if (peak >= 65535.0) {
            r.saturated = true;
        }
        if (peak <= 0.0) {
            r.code[ch] = kGainMaxCode;   // clamp_nonpositive: the warmup path
            continue;
        }
        double raw = round_half_even(kGainDivisor * kGainTarget / peak);
        if (raw < 0.0) {
            raw = 0.0;
        } else if (raw > static_cast<double>(kGainMaxCode)) {
            raw = static_cast<double>(kGainMaxCode);
        }
        r.code[ch] = static_cast<std::uint8_t>(raw);
    }
    return r;
}

WarmupOutcome gain_with_warmup(const std::function<std::vector<std::uint8_t>()>& measure,
                               const std::function<void(double)>& sleep,
                               const std::function<double()>& now_s,
                               const WarmupPolicy& policy, WarmupRecord& rec,
                               std::uint8_t codes_out[3])
{
    struct Measurement {
        std::array<double, 3> peaks;
        std::array<std::uint8_t, 3> codes;
        bool maxed;
        bool saturated;
    };

    const double t0 = now_s();
    const unsigned max_measurements =
        1 + static_cast<unsigned>(std::floor(policy.budget_s / policy.interval_s));

    auto measure_once = [&]() -> Measurement {
        std::vector<std::uint8_t> white = measure();   // may throw; propagated
        GainResult g = gain_codes(white.data(), white.size());
        Measurement m{};
        for (int ch = 0; ch < 3; ++ch) {
            m.peaks[ch] = g.peak[ch];
            m.codes[ch] = g.code[ch];
        }
        m.saturated = g.saturated;
        m.maxed = (g.code[0] == kGainMaxCode && g.code[1] == kGainMaxCode &&
                  g.code[2] == kGainMaxCode);
        ++rec.attempts;
        rec.elapsed_s = now_s() - t0;
        rec.peak_history.push_back(m.peaks);
        rec.gain_history.push_back(m.codes);
        return m;
    };

    Measurement cur = measure_once();
    if (cur.saturated) {
        return WarmupOutcome::Saturated;
    }
    if (!cur.maxed) {
        codes_out[0] = cur.codes[0];
        codes_out[1] = cur.codes[1];
        codes_out[2] = cur.codes[2];
        return WarmupOutcome::Ready;   // the verified single-measurement path
    }

    Measurement prev = cur;
    for (;;) {
        double elapsed = now_s() - t0;
        if (rec.attempts >= max_measurements || elapsed + policy.interval_s > policy.budget_s) {
            rec.exhausted = true;
            return WarmupOutcome::Exhausted;
        }
        sleep(policy.interval_s);
        cur = measure_once();
        if (cur.saturated) {
            return WarmupOutcome::Saturated;
        }
        if (!cur.maxed && !prev.maxed) {
            bool stable = true;
            for (int ch = 0; ch < 3; ++ch) {
                double allowed = (policy.stable_pct / 100.0) *
                                 std::max(prev.peaks[ch], 1.0);
                if (std::fabs(cur.peaks[ch] - prev.peaks[ch]) > allowed) {
                    stable = false;
                    break;
                }
            }
            if (stable) {
                codes_out[0] = cur.codes[0];
                codes_out[1] = cur.codes[1];
                codes_out[2] = cur.codes[2];
                return WarmupOutcome::Ready;
            }
        }
        prev = cur;
    }
}

} // namespace gl126
} // namespace genesys
