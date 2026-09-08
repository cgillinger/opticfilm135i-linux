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
    case OpsFailure::BadInjection:     return "BadInjection";
    case OpsFailure::ShortBulkOut:     return "ShortBulkOut";
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

// The combined length (bytes) of the BulkOut ops a bulk injection
// covers -- the total a payload is zero-padded to before slicing
// (docs/sane-hook4-shading.md section 6, Part B/2).
std::size_t bulk_injection_total(const OpProgram& prog, const OpBulkInjection& inj)
{
    std::size_t total = 0;
    for (std::size_t i = inj.first_op; i <= inj.last_op; ++i) {
        total += prog.ops[i].len;
    }
    return total;
}

// Checked once, before any transfer -- and before check_injections()'s
// byte-injection check, so a program with both kinds missing reports the
// bulk one first, deterministically (docs/sane-hook4-shading.md section
// 6, Part B/2): every bulk injection this program needs must be present
// in `bulk_values` (a null `bulk_values` counts as none present), and
// not longer than the BulkOut ops it covers. Names in `bulk_values` the
// program has no bulk injection for are ignored -- not checked here.
void check_bulk_injections(const OpProgram& prog,
                           const std::map<std::string, std::vector<std::uint8_t>>* bulk_values)
{
    for (std::size_t k = 0; k < prog.bulk_injection_count; ++k) {
        const OpBulkInjection& inj = prog.bulk_injections[k];
        if (bulk_values == nullptr || bulk_values->find(inj.name) == bulk_values->end()) {
            std::ostringstream oss;
            oss << "gl126_ops: missing bulk injection value for \"" << inj.name
                << "\" (ops " << inj.first_op << "-" << inj.last_op
                << ") -- nothing sent";
            throw OpsError(OpsFailure::MissingInjection, inj.first_op, oss.str());
        }
        std::size_t total = bulk_injection_total(prog, inj);
        const std::vector<std::uint8_t>& val = bulk_values->at(inj.name);
        if (val.size() > total) {
            std::ostringstream oss;
            oss << "gl126_ops: bulk injection \"" << inj.name << "\" (ops "
                << inj.first_op << "-" << inj.last_op << ") is " << val.size()
                << " B, longer than the covered ops' combined " << total
                << " B -- nothing sent";
            throw OpsError(OpsFailure::BadInjection, inj.first_op, oss.str());
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

// The bulk injection (if any) covering op `idx`, or nullptr -- at most
// one can, since a program's OpBulkInjection ranges never overlap (each
// names a distinct contiguous run of BulkOut ops).
const OpBulkInjection* bulk_injection_covering(const OpProgram& prog, std::size_t idx)
{
    for (std::size_t k = 0; k < prog.bulk_injection_count; ++k) {
        const OpBulkInjection& inj = prog.bulk_injections[k];
        if (idx >= inj.first_op && idx <= inj.last_op) {
            return &inj;
        }
    }
    return nullptr;
}

// Checked once, before any transfer, and before check_bulk_injections()'s
// value-presence check (docs/sane-hook4-shading.md section 6): every
// BulkOut op whose captured payload was stripped at generation time
// (`data == nullptr` -- tools/gen_sane_tables.py never keeps a captured
// chunk that is a bulk injection's own reference-unit calibration data)
// must be covered by an OpBulkInjection. This is a structural check on
// the generated table itself, not on the caller's `bulk_values` map --
// a BulkOut with no data and no covering injection is a mis-generated
// table (see the module doc comment on `Op` in gl126_tables.h) and must
// fail closed rather than hand a null pointer to bulk_write().
void check_bulk_out_coverage(const OpProgram& prog)
{
    for (std::size_t i = 0; i < prog.count; ++i) {
        const Op& op = prog.ops[i];
        if (op.kind != OpKind::BulkOut || op.data != nullptr) {
            continue;
        }
        if (bulk_injection_covering(prog, i) == nullptr) {
            std::ostringstream oss;
            oss << "gl126_ops: BulkOut at op " << i << " has no captured "
                << "payload and is not covered by any bulk injection -- "
                << "nothing sent";
            throw OpsError(OpsFailure::MissingInjection, i, oss.str());
        }
    }
}

void do_bulk_out(Wire& wire, const Op& op, const OpProgram& prog, std::size_t idx,
                 const std::map<std::string, std::vector<std::uint8_t>>* bulk_values)
{
    const std::uint8_t* data = op.data;
    std::size_t len = op.len;
    std::vector<std::uint8_t> patched;   // must outlive the wire.bulk_write() call

    const OpBulkInjection* inj = bulk_injection_covering(prog, idx);
    if (inj != nullptr) {
        // check_bulk_injections() already guaranteed `bulk_values` has
        // this name and it is not longer than the covered ops' combined
        // length. Zero-pad to that combined length, then slice out this
        // op's share -- of135i/tables.py's Phase.patched() "bo" rule.
        const std::vector<std::uint8_t>& val = bulk_values->at(inj->name);
        std::size_t offset = 0;
        for (std::size_t i = inj->first_op; i < idx; ++i) {
            offset += prog.ops[i].len;
        }
        patched.resize(len);
        for (std::size_t b = 0; b < len; ++b) {
            std::size_t pos = offset + b;
            patched[b] = (pos < val.size()) ? val[pos] : 0;
        }
        data = patched.data();
    }

    std::size_t written = wire.bulk_write(data, len);
    if (written != len) {
        std::ostringstream oss;
        oss << "gl126_ops: BulkOut at op " << idx << " wrote " << written
            << " B, want " << len << " B -- nothing further sent";
        throw OpsError(OpsFailure::ShortBulkOut, idx, oss.str());
    }
}

void do_poll_class(Wire& wire, const Op& op, std::size_t idx, RunResult& out,
                   const RunPolicy& policy)
{
    unsigned start = wire.now_ms();
    std::uint8_t first = 0;
    std::uint8_t last = 0;
    unsigned polls = 0;
    std::uint8_t want_class =
        (op.data != nullptr && op.len > 0) ? static_cast<std::uint8_t>(op.data[0] & 0xF0) : 0xF0;

    for (;;) {
        std::uint8_t reply[2] = {0, 0};
        wire.control_read(op.request, op.value, op.index, reply, 2);
        ++polls;
        if (polls == 1) {
            first = reply[0];
        }
        last = reply[0];

        if ((reply[0] & 0xF0) == want_class) {
            PollRecord rec{idx, first, last, polls, wire.now_ms() - start};
            out.polls.push_back(rec);
            return;
        }

        unsigned elapsed = wire.now_ms() - start;
        if (elapsed > policy.class_timeout_ms) {
            std::ostringstream oss;
            oss << "gl126_ops: PollClass at op " << idx << " (reg 0x100) "
                << "timed out after " << elapsed << "ms: first 0x" << std::hex
                << static_cast<int>(first) << ", last 0x" << static_cast<int>(last)
                << std::dec << " -- class 0x" << std::hex << static_cast<int>(want_class)
                << std::dec << " never reached, nothing further sent";
            throw OpsError(OpsFailure::PollTimeout, idx, oss.str());
        }
        wire.sleep_ms(policy.poll_interval_ms);
    }
}

void do_poll_masked(Wire& wire, const Op& op, std::size_t idx, RunResult& out,
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

        if ((reply[0] & op.mask) == op.want) {
            PollRecord rec{idx, first, last, polls, wire.now_ms() - start};
            out.polls.push_back(rec);
            return;
        }

        unsigned elapsed = wire.now_ms() - start;
        if (elapsed > policy.masked_timeout_ms) {
            std::ostringstream oss;
            oss << "gl126_ops: PollMasked at op " << idx << " (mask 0x" << std::hex
                << static_cast<int>(op.mask) << " want 0x" << static_cast<int>(op.want)
                << ") timed out after " << std::dec << elapsed << "ms: first 0x" << std::hex
                << static_cast<int>(first) << ", last 0x" << static_cast<int>(last)
                << std::dec << " -- nothing further sent";
            throw OpsError(OpsFailure::PollTimeout, idx, oss.str());
        }
        wire.sleep_ms(policy.poll_interval_ms);
    }
}

// docs/sane-hook5-frame.md section 4: no ack read -- park_semantic()'s
// own read-modify-write sites go through Scanner.io.write_regs(), which
// performs one control write and nothing else (see gl126_tables.h's Op
// doc comment and build_park_program()'s docstring in
// tools/gen_sane_tables.py for the evidence).
//
// DEVIATION found by tests/test_sane_ops.py's wire-equality test (its
// own docstring calls that test "the arbiter"): PARK's reg-0x35 RMW
// (clearing bit 0x40 after Wait A) does NOT re-read the register on the
// real driver -- of135i/device.py's _park_semantic_steps() reuses
// Wait A's own poll loop's last read (`v35`) directly:
//     v35 = self.io.read_reg(0x35)
//     while not (v35 & 0x40):
//         ...
//         v35 = self.io.read_reg(0x35)
//     self.io.write_regs([(0x35, v35 & ~0x40 & 0xFF)])   # no fresh read
// So here: a ReadModifyWrite op immediately preceded (in the program) by
// a PollMasked op with the SAME (request, value, index) -- exactly
// Wait A followed by the reg-0x35 clear -- reuses that PollMasked op's
// last polled value instead of issuing its own control_read(); every
// other RMW site in PARK (0x15, 0x32 x2) has no such immediately
// preceding same-register poll and reads fresh, as originally designed.
void do_read_modify_write(Wire& wire, const OpProgram& prog, const Op& op,
                          std::size_t idx, RunResult& out)
{
    std::uint8_t reg = static_cast<std::uint8_t>((op.index >> 8) & 0xFF);
    std::uint8_t v = 0;
    bool reused = false;
    if (idx > 0 && !out.polls.empty()) {
        const Op& prev = prog.ops[idx - 1];
        const PollRecord& last_poll = out.polls.back();
        if (prev.kind == OpKind::PollMasked && last_poll.op_index == idx - 1 &&
            prev.request == op.request && prev.value == op.value && prev.index == op.index) {
            v = last_poll.last;
            reused = true;
        }
    }
    if (!reused) {
        std::uint8_t reply[2] = {0, 0};
        wire.control_read(op.request, op.value, op.index, reply, 2);
        record_read(out, op, idx, reply, 2);
        v = reply[0];
    }
    std::uint8_t patched = static_cast<std::uint8_t>((v & op.mask) | op.want);
    std::uint8_t payload[2] = {reg, patched};
    wire.control_write(0x04, 0x0083, 0x0000, payload, 2);
}

} // namespace

void run_program(Wire& wire, const OpProgram& prog, RunResult& out,
                 const RunPolicy& policy,
                 const std::map<std::string, std::uint8_t>* values,
                 const std::map<std::string, std::vector<std::uint8_t>>* bulk_values)
{
    check_bulk_out_coverage(prog);
    if (prog.bulk_injection_count > 0) {
        check_bulk_injections(prog, bulk_values);
    }
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
        case OpKind::PollClass:
            do_poll_class(wire, op, i, out, policy);
            break;
        case OpKind::BulkIn:
            do_bulk_in(wire, op, i, out);
            break;
        case OpKind::BulkOut:
            do_bulk_out(wire, op, prog, i, bulk_values);
            break;
        case OpKind::BulkDone:
            do_bulk_done(wire, op, i, out);
            break;
        case OpKind::PollMasked:
            do_poll_masked(wire, op, i, out, policy);
            break;
        case OpKind::ReadModifyWrite:
            do_read_modify_write(wire, prog, op, i, out);
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

// ------------------------------------------------------- hook 4: shading

// docs/sane-hook4-shading.md section 4 / of135i/calibrate.py's shading
// section.
const double kShading2Targets[3] = {81752.0, 83490.0, 87083.0};   // R, G, B

namespace {

constexpr std::uint16_t kShadingGain = 0x4000;
constexpr std::size_t kPayloadPairsPerFullBlock = 126;
constexpr std::size_t kTrailerPairsPerFullBlock = 2;

// Walks the 512 B block structure once, calling `emit_payload(i, n)` for
// each block's payload run (starting pair index `i`, count `n`) and
// `emit_trailer(n)` for its zero trailer pairs (n == 0 or
// kTrailerPairsPerFullBlock) -- the single place that owns the block
// layout, shared by shading_upload_len() and pack_shading() so they
// cannot drift apart.
template <typename EmitPayload, typename EmitTrailer>
void walk_shading_blocks(std::size_t n_pairs, EmitPayload emit_payload, EmitTrailer emit_trailer)
{
    std::size_t i = 0;
    while (i < n_pairs) {
        std::size_t remaining = n_pairs - i;
        std::size_t n_payload, n_trailer;
        if (remaining >= kPayloadPairsPerFullBlock) {
            n_payload = kPayloadPairsPerFullBlock;
            n_trailer = kTrailerPairsPerFullBlock;
        } else {
            n_payload = remaining;
            n_trailer = 0;
        }
        emit_payload(i, n_payload);
        emit_trailer(n_trailer);
        i += n_payload;
    }
}

} // namespace

std::size_t shading_upload_len(unsigned width)
{
    std::size_t n_pairs = static_cast<std::size_t>(width) * 3;
    std::size_t total = 0;
    walk_shading_blocks(
        n_pairs,
        [&](std::size_t, std::size_t n_payload) { total += n_payload * 4; },
        [&](std::size_t n_trailer) { total += n_trailer * 4; });
    return total;
}

std::vector<std::uint8_t> pack_shading(const std::vector<std::uint16_t>& offsets,
                                       const std::vector<std::uint16_t>& gains,
                                       unsigned width)
{
    std::size_t n_pairs = static_cast<std::size_t>(width) * 3;
    if (offsets.size() != n_pairs || gains.size() != n_pairs) {
        throw std::invalid_argument(
            "gl126_ops::pack_shading: offsets/gains size does not match width*3");
    }
    std::vector<std::uint8_t> out;
    out.reserve(shading_upload_len(width));
    walk_shading_blocks(
        n_pairs,
        [&](std::size_t i, std::size_t n_payload) {
            for (std::size_t k = 0; k < n_payload; ++k) {
                std::uint16_t off = offsets[i + k];
                std::uint16_t g = gains[i + k];
                out.push_back(static_cast<std::uint8_t>(off & 0xFF));
                out.push_back(static_cast<std::uint8_t>((off >> 8) & 0xFF));
                out.push_back(static_cast<std::uint8_t>(g & 0xFF));
                out.push_back(static_cast<std::uint8_t>((g >> 8) & 0xFF));
            }
        },
        [&](std::size_t n_trailer) {
            for (std::size_t k = 0; k < n_trailer; ++k) {
                out.push_back(0);
                out.push_back(0);
                out.push_back(0);
                out.push_back(0);
            }
        });
    return out;
}

namespace {

// Per-pixel/channel mean over `lines` of a `lines * width * 6` byte
// RGB16LE buffer, pixel-interleaved (docs/sane-hook4-shading.md section
// 4). `p` indexes the flattened (pixel, channel) pair, 0..width*3-1,
// same order as a scanned line's own bytes.
double mean_over_lines(const std::uint8_t* buf, unsigned lines, unsigned width, std::size_t p)
{
    std::size_t row_stride = static_cast<std::size_t>(width) * 6;
    double sum = 0.0;
    for (unsigned line = 0; line < lines; ++line) {
        sum += read_u16le(buf + line * row_stride + p * 2);
    }
    return lines ? sum / static_cast<double>(lines) : 0.0;
}

void check_shading_buffer(const char* fn, const std::uint8_t* buf, std::size_t len,
                          unsigned lines, unsigned width)
{
    (void)buf;
    std::size_t expected = static_cast<std::size_t>(lines) * width * 6;
    if (len != expected) {
        std::ostringstream oss;
        oss << "gl126_ops::" << fn << ": buffer is " << len << " B, expected "
            << "lines*width*6 = " << expected << " B (lines=" << lines
            << ", width=" << width << ")";
        throw std::invalid_argument(oss.str());
    }
}

} // namespace

std::vector<std::uint8_t> shading_table(const std::uint8_t* meas, std::size_t len,
                                        unsigned lines, unsigned width)
{
    check_shading_buffer("shading_table", meas, len, lines, width);
    std::size_t n_pairs = static_cast<std::size_t>(width) * 3;
    std::vector<std::uint16_t> offsets(n_pairs);
    std::vector<std::uint16_t> gains(n_pairs, kShadingGain);
    for (std::size_t p = 0; p < n_pairs; ++p) {
        double mean = mean_over_lines(meas, lines, width, p);
        offsets[p] = static_cast<std::uint16_t>(round_half_even(mean));
    }
    return pack_shading(offsets, gains, width);
}

std::vector<std::uint8_t> shading_table2(const std::uint8_t* white, std::size_t white_len,
                                         const std::uint8_t* dark, std::size_t dark_len,
                                         unsigned lines, unsigned width,
                                         const double targets[3])
{
    check_shading_buffer("shading_table2", white, white_len, lines, width);
    check_shading_buffer("shading_table2", dark, dark_len, lines, width);
    std::size_t n_pairs = static_cast<std::size_t>(width) * 3;
    std::vector<std::uint16_t> f0(n_pairs);
    std::vector<std::uint16_t> gains(n_pairs);
    for (std::size_t p = 0; p < n_pairs; ++p) {
        double w = mean_over_lines(white, lines, width, p);
        double f0_val = round_half_even(mean_over_lines(dark, lines, width, p));
        f0[p] = static_cast<std::uint16_t>(f0_val);
        double denom = std::max(w - f0_val, 1.0);
        int ch = static_cast<int>(p % 3);
        double g = round_half_even(targets[ch] * 0x4000 / denom);
        if (g < 1.0) {
            g = 1.0;
        } else if (g > 65535.0) {
            g = 65535.0;
        }
        gains[p] = static_cast<std::uint16_t>(g);
    }
    return pack_shading(f0, gains, width);
}

std::vector<std::uint8_t> shading_table2(const std::uint8_t* white, std::size_t white_len,
                                         const std::uint8_t* dark, std::size_t dark_len,
                                         unsigned lines, unsigned width)
{
    return shading_table2(white, white_len, dark, dark_len, lines, width, kShading2Targets);
}

// ------------------------------------------- hook 8: dual-light profiles

std::vector<std::uint8_t> alternate_lines(const std::uint8_t* buf, std::size_t len,
                                          unsigned lines, unsigned width, unsigned parity)
{
    check_shading_buffer("alternate_lines", buf, len, lines, width);
    if (lines % 2 != 0 || parity > 1) {
        throw std::invalid_argument("alternate_lines: even line count and parity 0/1 required");
    }
    const std::size_t row = static_cast<std::size_t>(width) * 6;
    std::vector<std::uint8_t> out;
    out.reserve(row * (lines / 2));
    for (unsigned l = parity; l < lines; l += 2) {
        out.insert(out.end(), buf + std::size_t(l) * row, buf + std::size_t(l + 1) * row);
    }
    return out;
}

std::vector<std::uint8_t> shading_table2_dual(const std::uint8_t* white, std::size_t white_len,
                                              const std::uint8_t* dark, std::size_t dark_len,
                                              unsigned lines, unsigned width, double target)
{
    check_shading_buffer("shading_table2_dual", white, white_len, lines, width);
    check_shading_buffer("shading_table2_dual", dark, dark_len, lines, width);
    std::size_t n_pairs = static_cast<std::size_t>(width) * 3;
    std::vector<std::uint16_t> f0(n_pairs);
    std::vector<std::uint16_t> gains(n_pairs);
    for (std::size_t p = 0; p < n_pairs; ++p) {
        double w = std::max(mean_over_lines(white, lines, width, p), 1.0);
        f0[p] = static_cast<std::uint16_t>(round_half_even(mean_over_lines(dark, lines, width, p)));
        double g = round_half_even(target * 0x4000 / w);
        if (g < 1.0) {
            g = 1.0;
        } else if (g > 65535.0) {
            g = 65535.0;
        }
        gains[p] = static_cast<std::uint16_t>(g);
    }
    return pack_shading(f0, gains, width);
}

FrameGeometry frame_geometry(const Profile& profile)
{
    FrameGeometry g;
    g.dual = profile.lines_per_chunk != 0;   // the plain profile carries no chunk plan
    g.width = profile.image_width;
    g.wire_lines = profile.captured_lines;
    g.chunk_len = profile.chunk_len;
    if (g.dual) {
        g.read_lines = profile.chunk_count * profile.lines_per_chunk;
        g.image_lines = g.read_lines / 2;
    } else {
        g.read_lines = profile.captured_lines;
        g.image_lines = g.read_lines;
    }
    g.shift_lines = 24 * profile.dpi / 3600;
    g.delivered_lines = g.image_lines - g.shift_lines;
    std::size_t raw = std::size_t(g.read_lines) * g.width * 6;
    g.chunk_count = static_cast<unsigned>((raw + g.chunk_len - 1) / g.chunk_len);
    return g;
}

// ------------------------------------------------- hooks 5-7: the frame

unsigned feedl_for_frame(unsigned frame)
{
    return kFeedlFrame1 + (frame - 1) * kFeedlPitch;
}

unsigned feedl_for_frame(unsigned frame, const Profile& profile)
{
    return profile.feedl_frame1 + (frame - 1) * profile.feedl_pitch;
}

FeedlBytes feedl_bytes(unsigned feedl)
{
    FeedlBytes b;
    b.hi = static_cast<std::uint8_t>((feedl >> 16) & 0xFF);
    b.mid = static_cast<std::uint8_t>((feedl >> 8) & 0xFF);
    b.lo = static_cast<std::uint8_t>(feedl & 0xFF);
    return b;
}

unsigned position_timeout_ms(unsigned feedl)
{
    // docs/sane-hook5-frame.md section 3/6: "3 * 1.6141 * position_
    // timeout_scale" -- POSITION's captured completion duration for
    // frame 1 (1.6141 s, tables.POSITION's own op 47/W3) times 3, scaled
    // linearly by FEEDL relative to frame 1's (never below 1x, matching
    // of135i/device.py's position_timeout_scale()).
    constexpr double kCapturedMs = 1614.1;
    double scale = std::max(1.0, static_cast<double>(feedl) / static_cast<double>(kFeedlFrame1));
    double ms = 3.0 * kCapturedMs * scale;
    return static_cast<unsigned>(ms + 0.5);
}

void read_image_chunk(Wire& wire, std::uint8_t* data, std::size_t len, bool first)
{
    // docs/sane-hook5-frame.md section 2/4, hook 6b: descriptor address
    // fixed at 0x10000000 (IMAGE_READ_ADDR, of135i/tables.py), length is
    // this chunk's own `len`, LE32; wIndex 8 arms the FIRST chunk of a
    // scan, 0 every chunk after (a flag begin_scan sets, per the plan).
    std::uint8_t desc[8] = {
        0x00, 0x00, 0x00, 0x10,
        static_cast<std::uint8_t>(len & 0xFF),
        static_cast<std::uint8_t>((len >> 8) & 0xFF),
        static_cast<std::uint8_t>((len >> 16) & 0xFF),
        static_cast<std::uint8_t>((len >> 24) & 0xFF),
    };
    wire.control_write(0x04, 0x0082, first ? 0x0008 : 0x0000, desc, sizeof(desc));

    std::uint8_t ack = 0;
    wire.control_read(0x0C, 0x008E, 0x0020, &ack, 1);
    if (ack != 0x55) {
        std::ostringstream oss;
        oss << "gl126_ops: read_image_chunk descriptor ack got 0x" << std::hex
            << static_cast<int>(ack) << ", want 0x55 -- nothing further sent";
        throw OpsError(OpsFailure::BadAck, 0, oss.str());
    }

    // ONE logical bulk IN of the whole chunk -- see read_image_chunk()'s
    // doc comment (gl126_ops.h) for why the captured ~33-fragment/chunk
    // USB-packet breakdown is not reproduced here, and why there is no
    // trailing bulk-done read.
    std::size_t got = wire.bulk_read(data, len);
    if (got != len) {
        std::ostringstream oss;
        oss << "gl126_ops: read_image_chunk bulk IN got " << got << " B, want "
            << len << " B -- nothing further sent";
        throw OpsError(OpsFailure::ShortBulk, 1, oss.str());
    }
}

// ------------------------------------------------------------ scan pass

const char* scan_pass_state_name(ScanPassState s)
{
    switch (s) {
        case ScanPassState::Idle: return "Idle";
        case ScanPassState::Armed: return "Armed";
        case ScanPassState::Streaming: return "Streaming";
        case ScanPassState::Complete: return "Complete";
        case ScanPassState::Parked: return "Parked";
        case ScanPassState::Failed: return "Failed";
    }
    return "?";
}

const char* park_decision_name(ParkDecision d)
{
    switch (d) {
        case ParkDecision::Run: return "Run";
        case ParkDecision::NoPass: return "NoPass";
        case ParkDecision::AlreadyParked: return "AlreadyParked";
        case ParkDecision::AbortedPass: return "AbortedPass";
        case ParkDecision::Failed: return "Failed";
    }
    return "?";
}

bool ScanPass::arm(std::size_t bytes_expected)
{
    if (state_ != ScanPassState::Idle && state_ != ScanPassState::Parked) {
        return false;
    }
    state_ = ScanPassState::Armed;
    expected_ = bytes_expected;
    read_ = 0;
    return true;
}

bool ScanPass::chunk_begin(bool* first)
{
    if (state_ == ScanPassState::Armed) {
        state_ = ScanPassState::Streaming;
        *first = true;
        return true;
    }
    if (state_ == ScanPassState::Streaming) {
        *first = false;
        return true;
    }
    return false;
}

void ScanPass::chunk_done(std::size_t bytes)
{
    if (state_ != ScanPassState::Streaming) {
        return;
    }
    read_ += bytes;
    if (read_ >= expected_) {
        state_ = ScanPassState::Complete;
    }
}

void ScanPass::fail()
{
    state_ = ScanPassState::Failed;
}

ParkDecision ScanPass::park_decision()
{
    switch (state_) {
        case ScanPassState::Complete: return ParkDecision::Run;
        case ScanPassState::Idle: return ParkDecision::NoPass;
        case ScanPassState::Parked: return ParkDecision::AlreadyParked;
        case ScanPassState::Failed: return ParkDecision::Failed;
        case ScanPassState::Armed:
        case ScanPassState::Streaming:
            state_ = ScanPassState::Failed;
            return ParkDecision::AbortedPass;
    }
    return ParkDecision::Failed;
}

void ScanPass::parked()
{
    if (state_ == ScanPassState::Complete) {
        state_ = ScanPassState::Parked;
    }
}

} // namespace gl126
} // namespace genesys
