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

#include <cmath>
#include <ios>
#include <sstream>

namespace genesys {
namespace gl126 {

const char* to_string(OpsFailure failure)
{
    switch (failure) {
    case OpsFailure::BadAck:      return "BadAck";
    case OpsFailure::PollTimeout: return "PollTimeout";
    case OpsFailure::ShortBulk:   return "ShortBulk";
    }
    return "Unknown";
}

namespace {

// ------------------------------------------------------------- run_program

void do_write(Wire& wire, const Op& op)
{
    wire.control_write(op.request, op.value, op.index, op.data, op.len);
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
                 const RunPolicy& policy)
{
    for (std::size_t i = 0; i < prog.count; ++i) {
        const Op& op = prog.ops[i];
        switch (op.kind) {
        case OpKind::Write:
            do_write(wire, op);
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

} // namespace gl126
} // namespace genesys
