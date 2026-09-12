/* Standalone probe for sane/gl126_ops.{h,cpp} -- used only by
   tests/test_sane_ops.py. Not part of the SANE backend build.

   Usage:
     probe run <profile> <phase> <script>
           [--inject name=0xNN ...] [--inject-bulk name=<file> ...]
         Runs the named profile/phase's OpProgram (sane/gl126_tables.h)
         against a scripted Wire fake and prints one line per transfer,
         in the order run_program() performs it:

             W req=<hex2> val=<hex4> idx=<hex4> data=<hex...>
             R req=<hex2> val=<hex4> idx=<hex4> len=<n>
             B len=<n>
             BO len=<n> sha256=<hex64>

         then either "DONE ops=<n>" or "FAIL <failure> op=<i> ops=<n>"
         (failure is BadAck, PollTimeout, ShortBulk, ShortBulkOut,
         MissingInjection or BadInjection -- see OpsFailure). `n` after
         "ops=" is RunResult::ops_done: completed ops, so a test can
         confirm nothing ran after a failure just by checking no further
         lines follow the FAIL line.

         `--inject name=0xNN` (repeatable) builds the name -> byte map
         run_program() takes for the program's OpInjection entries (docs/
         sane-hook3-gain.md section 6, Part B/1 -- cal_gain_check_a).
         `--inject-bulk name=<file>` (repeatable) builds the name -> byte
         vector map for the program's OpBulkInjection entries (docs/
         sane-hook4-shading.md section 6, Part B/2 -- cal_shading_upload/
         cal_shading_verify_upload), the file's raw bytes as the value
         (BEFORE zero-padding -- run_program() does that itself, exactly
         as of135i/tables.py's Phase.patched() does). Omitting either
         flag passes a null map, so a program with that kind of
         injection fails MissingInjection before any transfer -- the
         point of test_missing_injection_* and
         test_missing_bulk_injection_*.

     probe program_info <profile> <phase>
         Prints one "OP <i> kind=<OpKind> len=<n> has_data=<0|1>" line per
         op in the named OpProgram, in order -- no Wire, no run_program()
         involved. Used only to check the generator's own output (docs/
         sane-hook4-shading.md section 6, Part B/2): a BulkOut a bulk
         injection covers is emitted with has_data=0 (the captured chunk
         is the reference unit's own calibration data and must never be
         kept); every other op keeps has_data=1 (or 0 for BulkIn, which
         never carries data).

     probe offset <dark_a.bin> <dark_b.bin>
         Runs gl126::offset_codes() on two raw RGB16LE buffers and prints
         one "ch=<0|1|2> mean_a=<f> mean_b=<f> slope=<f> code=<hex4>
         fallback=<0|1>" line per channel.

     probe residual <buf.bin>
         Runs gl126::dark_is_residual() on a raw buffer and prints
         RESIDUAL or NOT_RESIDUAL.

     probe gain <white.bin>
         Runs gl126::gain_codes() on a raw RGB16LE white-line buffer and
         prints one "ch=<0|1|2> peak=<f> code=<hex2>" line per channel,
         then "saturated=<0|1>".

     probe percentile <u16.bin> <q>
         Runs gl126::percentile_linear() on a raw little-endian uint16
         array and prints "VALUE=<f>".

     probe warmup <script>
         Runs gl126::gain_with_warmup() with `measure` returning the next
         file `script` names (one raw RGB16LE white-line buffer path per
         line, one per measurement attempt) and a fake clock advanced
         only inside `sleep` (default WarmupPolicy). Prints one line per
         attempt, in order --

             ATTEMPT <n> peaks=<f>,<f>,<f> codes=<hex2>,<hex2>,<hex2>

         then "OUTCOME Ready|Saturated|Exhausted attempts=<n>
         elapsed=<f> codes=<hex2>,<hex2>,<hex2>" (codes are 00,00,00
         when the outcome is not Ready).

     probe shading_table <meas.bin> <lines> <width> <out.bin>
         Runs gl126::shading_table() on a raw RGB16LE measurement buffer
         and writes the payload to <out.bin>; prints "OK len=<n>".

     probe shading_table2 <white.bin> <dark.bin> <lines> <width> <out.bin>
         Runs gl126::shading_table2() (default kShading2Targets) on two
         raw RGB16LE buffers and writes the payload to <out.bin>; prints
         "OK len=<n>".

     probe upload_len <width>
         Runs gl126::shading_upload_len() and prints "LEN=<n>".

     probe image_chunks <n_full> <full_len> <last_len>
         Calls gl126::read_image_chunk() over a no-fault fake Wire:
         `n_full` chunks of `full_len` bytes (first=true only for the
         very first one), then one final chunk of `last_len` bytes
         (docs/sane-hook5-frame.md section 2/4, hook 6b). Transfer log in
         the same W/R/B format `run` uses; "DONE" or "FAIL <failure>
         op=<i>" (op_index is 0 for a BadAck, 1 for a ShortBulk -- this
         mode is not table-driven).

     probe feedl <frame>
         Runs gl126::feedl_for_frame()/feedl_bytes() and prints
         "FEEDL=<n> hi=<hex2> mid=<hex2> lo=<hex2>".

     probe position_timeout <feedl>
         Runs gl126::position_timeout_ms() and prints "MS=<n>".

   Script format (tests/gl126_ops_probe.cpp's `run` mode; a missing or
   empty file means "use every op program's own captured values", which
   is what makes the wire-equality test's script trivial): one directive
   per non-blank, non-'#' line, each an override of what the *default*
   (the OpProgram's own captured `data`) would serve --

     ack_at <occurrence> <hex-byte>   -- override the Nth AckRead's reply
                                         (0-indexed occurrence in this run)
     poll <hex4>[,<hex4>...]          -- override the (single)
                                         PollDataReady site's reply
                                         sequence entirely; the last value
                                         repeats once the list is
                                         exhausted (so one value that
                                         never sets bit 0x01 models a
                                         timeout, and a settling value
                                         needs only be listed once)
     class_poll <hex4>[,<hex4>...]    -- the same, for the (single)
                                         PollClass site (docs/sane-hook4-
                                         shading.md section 3, W2)
     bulk_len <n>                     -- override every BulkIn's returned
                                         length (a short read: n < the
                                         op's own len)
     bulk_len_at <occurrence> <n>     -- override only the Nth BulkIn's
                                         returned length (0-indexed
                                         occurrence in this run) --
                                         cal_white has three; overrides
                                         here take priority over a plain
                                         `bulk_len` for that occurrence
     bulk_out_len_at <occurrence> <n> -- simulate a short BulkOut: the
                                         Nth BulkOut (0-indexed occurrence
                                         in this run) reports only `n`
                                         bytes accepted, n < the op's own
                                         len
     bulkdone <hex-byte>              -- override the (single) BulkDone's
                                         reply
     read_at <occurrence> <hex-bytes> -- override the Nth plain Read's
                                         reply (0-indexed occurrence)
     masked_poll_at <occ> <hex4>[,<hex4>...] -- override the Nth
                                         PollMasked site's reply sequence
                                         (0-indexed occurrence among
                                         PollMasked ops in this run --
                                         "park" has two: Wait A, Wait B).
                                         Unscripted: settles on the first
                                         read, at the op's own `want`.
     rmw_read_at <occurrence> <hex-byte> -- the register value the Nth
                                         ReadModifyWrite op's read
                                         returns (0-indexed occurrence).
                                         Unscripted: 0x00.

   Every op program in scope here has at most one PollDataReady/PollClass/
   BulkDone, so `poll`/`class_poll`/`bulkdone` need no occurrence index;
   cal_white alone has three BulkIn ops (`bulk_len_at` addresses those
   individually); "park" (docs/sane-hook5-frame.md) has two PollMasked
   sites and four ReadModifyWrite sites, addressed by `masked_poll_at`/
   `rmw_read_at`'s own occurrence index. */

#include "../sane/gl126_ops.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace genesys::gl126;

namespace {

// ------------------------------------------------------------------ sha256
//
// A small, self-contained SHA-256 (FIPS 180-4), test-only code -- used
// solely so the wire-equality test can compare a BulkOut payload's
// digest against a Python-computed one (hashlib.sha256) without
// shipping the whole payload through the probe's text output (docs/
// sane-hook4-shading.md section 6, Part C). Not part of the SANE
// backend.

class Sha256 {
public:
    Sha256() { reset(); }

    void update(const std::uint8_t* data, std::size_t len)
    {
        total_len_ += len;
        while (len > 0) {
            std::size_t take = 64 - buf_len_;
            if (take > len) take = len;
            std::memcpy(buf_ + buf_len_, data, take);
            buf_len_ += take;
            data += take;
            len -= take;
            if (buf_len_ == 64) {
                process(buf_);
                buf_len_ = 0;
            }
        }
    }

    std::string hex_digest()
    {
        std::uint64_t bit_len = total_len_ * 8;
        std::uint8_t pad = 0x80;
        update(&pad, 1);
        std::uint8_t zero = 0x00;
        while (buf_len_ != 56) {
            update(&zero, 1);
        }
        std::uint8_t len_be[8];
        for (int i = 0; i < 8; ++i) {
            len_be[i] = static_cast<std::uint8_t>(bit_len >> (56 - 8 * i));
        }
        // Bypass update()'s total_len_ accounting for the length field itself.
        std::memcpy(buf_ + 56, len_be, 8);
        process(buf_);

        static const char* hexd = "0123456789abcdef";
        std::string out;
        out.reserve(64);
        for (int i = 0; i < 8; ++i) {
            for (int b = 3; b >= 0; --b) {
                std::uint8_t byte = static_cast<std::uint8_t>(h_[i] >> (8 * b));
                out.push_back(hexd[byte >> 4]);
                out.push_back(hexd[byte & 0xf]);
            }
        }
        return out;
    }

private:
    void reset()
    {
        h_[0] = 0x6a09e667u; h_[1] = 0xbb67ae85u; h_[2] = 0x3c6ef372u; h_[3] = 0xa54ff53au;
        h_[4] = 0x510e527fu; h_[5] = 0x9b05688cu; h_[6] = 0x1f83d9abu; h_[7] = 0x5be0cd19u;
        buf_len_ = 0;
        total_len_ = 0;
    }

    static std::uint32_t rotr(std::uint32_t x, unsigned n) { return (x >> n) | (x << (32 - n)); }

    void process(const std::uint8_t block[64])
    {
        static const std::uint32_t k[64] = {
            0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
            0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
            0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
            0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
            0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
            0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
            0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
            0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u,
        };
        std::uint32_t w[64];
        for (int i = 0; i < 16; ++i) {
            w[i] = (static_cast<std::uint32_t>(block[i * 4]) << 24) |
                   (static_cast<std::uint32_t>(block[i * 4 + 1]) << 16) |
                   (static_cast<std::uint32_t>(block[i * 4 + 2]) << 8) |
                   static_cast<std::uint32_t>(block[i * 4 + 3]);
        }
        for (int i = 16; i < 64; ++i) {
            std::uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
            std::uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        std::uint32_t a = h_[0], b = h_[1], c = h_[2], d = h_[3];
        std::uint32_t e = h_[4], f = h_[5], g = h_[6], hh = h_[7];
        for (int i = 0; i < 64; ++i) {
            std::uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            std::uint32_t ch = (e & f) ^ (~e & g);
            std::uint32_t t1 = hh + s1 + ch + k[i] + w[i];
            std::uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            std::uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            std::uint32_t t2 = s0 + maj;
            hh = g; g = f; f = e; e = d + t1;
            d = c; c = b; b = a; a = t1 + t2;
        }
        h_[0] += a; h_[1] += b; h_[2] += c; h_[3] += d;
        h_[4] += e; h_[5] += f; h_[6] += g; h_[7] += hh;
    }

    std::uint32_t h_[8];
    std::uint8_t buf_[64];
    std::size_t buf_len_;
    std::uint64_t total_len_;
};

std::string sha256_hex(const std::uint8_t* data, std::size_t len)
{
    Sha256 h;
    h.update(data, len);
    return h.hex_digest();
}

// ---------------------------------------------------------------- hex utils

std::string hex2(unsigned v)
{
    char buf[3];
    std::snprintf(buf, sizeof(buf), "%02x", v & 0xffu);
    return std::string(buf);
}

std::string hex4(unsigned v)
{
    char buf[5];
    std::snprintf(buf, sizeof(buf), "%04x", v & 0xffffu);
    return std::string(buf);
}

std::string hex_bytes(const std::uint8_t* d, std::size_t n)
{
    std::string out;
    out.reserve(n * 2);
    char buf[3];
    for (std::size_t i = 0; i < n; ++i) {
        std::snprintf(buf, sizeof(buf), "%02x", d[i]);
        out += buf;
    }
    return out;
}

std::vector<std::uint8_t> parse_hex(const std::string& s)
{
    std::vector<std::uint8_t> out;
    for (std::size_t i = 0; i + 2 <= s.size(); i += 2) {
        out.push_back(static_cast<std::uint8_t>(std::stoul(s.substr(i, 2), nullptr, 16)));
    }
    return out;
}

std::vector<std::uint8_t> read_file(const std::string& path)
{
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("could not open " + path);
    }
    return std::vector<std::uint8_t>((std::istreambuf_iterator<char>(f)),
                                     std::istreambuf_iterator<char>());
}

// -------------------------------------------------------------------- script

struct Script {
    std::map<std::size_t, std::uint8_t> ack_overrides;
    std::vector<std::array<std::uint8_t, 2>> poll_list;   // empty: use default
    std::vector<std::array<std::uint8_t, 2>> class_poll_list;   // empty: use default
    bool has_bulk_len = false;
    std::size_t bulk_len = 0;
    std::map<std::size_t, std::size_t> bulk_len_at;   // occurrence -> length
    std::map<std::size_t, std::size_t> bulk_out_len_at;   // occurrence -> accepted length
    bool has_bulkdone = false;
    std::uint8_t bulkdone = 0;
    std::map<std::size_t, std::vector<std::uint8_t>> read_overrides;
    // hooks 5-7 (docs/sane-hook5-frame.md): PARK carries TWO distinct
    // PollMasked sites (Wait A, Wait B) and FOUR ReadModifyWrite sites,
    // so -- unlike poll/class_poll/bulkdone above, which address the
    // single occurrence a hook2-4 program ever has -- these are indexed
    // by occurrence (0-based, in the order the program executes them).
    std::map<std::size_t, std::vector<std::array<std::uint8_t, 2>>> masked_poll_at;
    std::map<std::size_t, std::uint8_t> rmw_read_at;
};

// Shared by "poll" and "class_poll": parse a comma-separated list of
// hex u16 replies into a poll-reply list.
std::vector<std::array<std::uint8_t, 2>> parse_poll_list(const std::string& rest)
{
    std::vector<std::array<std::uint8_t, 2>> out;
    std::size_t pos = 0;
    while (pos < rest.size()) {
        std::size_t comma = rest.find(',', pos);
        std::string tok = rest.substr(
            pos, comma == std::string::npos ? std::string::npos : comma - pos);
        auto bytes = parse_hex(tok);
        std::array<std::uint8_t, 2> v{{0, 0}};
        if (bytes.size() > 0) v[0] = bytes[0];
        if (bytes.size() > 1) v[1] = bytes[1];
        out.push_back(v);
        if (comma == std::string::npos) break;
        pos = comma + 1;
    }
    return out;
}

Script parse_script(const std::string& path)
{
    Script s;
    std::ifstream f(path);
    if (!f) {
        return s;   // no script file: every site uses its captured default
    }
    std::string line;
    while (std::getline(f, line)) {
        while (!line.empty() && (line.back() == '\r' || line.back() == '\n')) {
            line.pop_back();
        }
        if (line.empty() || line[0] == '#') {
            continue;
        }
        std::istringstream iss(line);
        std::string cmd;
        iss >> cmd;
        if (cmd == "ack_at") {
            std::size_t n = 0;
            std::string hex;
            iss >> n >> hex;
            auto bytes = parse_hex(hex);
            if (!bytes.empty()) {
                s.ack_overrides[n] = bytes[0];
            }
        } else if (cmd == "poll") {
            std::string rest;
            iss >> rest;
            s.poll_list = parse_poll_list(rest);
        } else if (cmd == "class_poll") {
            std::string rest;
            iss >> rest;
            s.class_poll_list = parse_poll_list(rest);
        } else if (cmd == "bulk_len") {
            std::size_t n = 0;
            iss >> n;
            s.bulk_len = n;
            s.has_bulk_len = true;
        } else if (cmd == "bulk_len_at") {
            std::size_t occurrence = 0, n = 0;
            iss >> occurrence >> n;
            s.bulk_len_at[occurrence] = n;
        } else if (cmd == "bulk_out_len_at") {
            std::size_t occurrence = 0, n = 0;
            iss >> occurrence >> n;
            s.bulk_out_len_at[occurrence] = n;
        } else if (cmd == "bulkdone") {
            std::string hex;
            iss >> hex;
            auto bytes = parse_hex(hex);
            if (!bytes.empty()) {
                s.bulkdone = bytes[0];
                s.has_bulkdone = true;
            }
        } else if (cmd == "read_at") {
            std::size_t n = 0;
            std::string hex;
            iss >> n >> hex;
            s.read_overrides[n] = parse_hex(hex);
        } else if (cmd == "masked_poll_at") {
            std::size_t occurrence = 0;
            std::string rest;
            iss >> occurrence >> rest;
            s.masked_poll_at[occurrence] = parse_poll_list(rest);
        } else if (cmd == "rmw_read_at") {
            std::size_t occurrence = 0;
            std::string hex;
            iss >> occurrence >> hex;
            auto bytes = parse_hex(hex);
            if (!bytes.empty()) {
                s.rmw_read_at[occurrence] = bytes[0];
            }
        } else {
            throw std::runtime_error("gl126_ops_probe: unknown script directive: " + cmd);
        }
    }
    return s;
}

// ---------------------------------------------------------------- ScriptedWire

/* Drives run_program() with a single cursor into the OpProgram: each
   call is matched against prog.ops[cursor_] by the op kind the caller
   (run_program) can only be servicing next, so no ambiguity between,
   say, the PollDataReady site and a later plain Read that happens to
   reuse the same (value, index) -- see the probe's file comment and
   docs/sane-hook2-offset.md section 6. */
class ScriptedWire : public Wire {
public:
    ScriptedWire(const OpProgram& prog, const Script& script)
        : prog_(prog), script_(script) {}

    void control_write(std::uint8_t request, std::uint16_t value, std::uint16_t index,
                       const std::uint8_t* data, std::size_t len) override
    {
        // A ReadModifyWrite op is serviced by TWO wire calls on the SAME
        // op (control_read for the read half, then control_write for the
        // write half) -- the cursor only advances once the write lands.
        if (cursor_ < prog_.count && current().kind == OpKind::ReadModifyWrite) {
            std::cout << "W req=" << hex2(request) << " val=" << hex4(value)
                      << " idx=" << hex4(index) << " data=" << hex_bytes(data, len) << "\n";
            ++rmw_occurrence_;
            advance();
            return;
        }
        expect(OpKind::Write);
        std::cout << "W req=" << hex2(request) << " val=" << hex4(value)
                  << " idx=" << hex4(index) << " data=" << hex_bytes(data, len) << "\n";
        advance();
    }

    void control_read(std::uint8_t request, std::uint16_t value, std::uint16_t index,
                      std::uint8_t* data, std::size_t len) override
    {
        const Op& op = current();
        switch (op.kind) {
        case OpKind::AckRead: {
            auto it = script_.ack_overrides.find(ack_calls_);
            std::uint8_t v = (it != script_.ack_overrides.end())
                ? it->second : (op.data != nullptr ? op.data[0] : 0x55);
            if (len > 0) data[0] = v;
            log_read(request, value, index, len);
            ++ack_calls_;
            advance();
            break;
        }
        case OpKind::Read: {
            auto it = script_.read_overrides.find(read_calls_);
            for (std::size_t i = 0; i < len; ++i) {
                if (it != script_.read_overrides.end() && i < it->second.size()) {
                    data[i] = it->second[i];
                } else {
                    data[i] = (op.data != nullptr && i < op.len) ? op.data[i] : 0;
                }
            }
            log_read(request, value, index, len);
            ++read_calls_;
            advance();
            break;
        }
        case OpKind::PollDataReady: {
            if (poll_list_.empty()) {
                if (!script_.poll_list.empty()) {
                    poll_list_ = script_.poll_list;
                } else {
                    std::array<std::uint8_t, 2> v{{0, 0}};
                    if (op.data != nullptr) {
                        v[0] = op.data[0];
                        if (op.len > 1) v[1] = op.data[1];
                    }
                    poll_list_.push_back(v);
                }
            }
            std::size_t idx = poll_idx_ < poll_list_.size() ? poll_idx_ : poll_list_.size() - 1;
            if (poll_idx_ + 1 < poll_list_.size()) ++poll_idx_;
            const std::array<std::uint8_t, 2>& v = poll_list_[idx];
            if (len > 0) data[0] = v[0];
            if (len > 1) data[1] = v[1];
            log_read(request, value, index, len);
            if (v[0] & 0x01) {
                advance();   // this was the last call at this site
            }
            break;
        }
        case OpKind::PollClass: {
            if (class_poll_list_.empty()) {
                if (!script_.class_poll_list.empty()) {
                    class_poll_list_ = script_.class_poll_list;
                } else {
                    std::array<std::uint8_t, 2> v{{0, 0}};
                    if (op.data != nullptr) {
                        v[0] = op.data[0];
                        if (op.len > 1) v[1] = op.data[1];
                    }
                    class_poll_list_.push_back(v);
                }
            }
            std::size_t idx = class_poll_idx_ < class_poll_list_.size()
                ? class_poll_idx_ : class_poll_list_.size() - 1;
            if (class_poll_idx_ + 1 < class_poll_list_.size()) ++class_poll_idx_;
            const std::array<std::uint8_t, 2>& v = class_poll_list_[idx];
            if (len > 0) data[0] = v[0];
            if (len > 1) data[1] = v[1];
            log_read(request, value, index, len);
            std::uint8_t want_class = (op.data != nullptr && op.len > 0)
                ? static_cast<std::uint8_t>(op.data[0] & 0xF0) : 0xF0;
            if ((v[0] & 0xF0) == want_class) {
                advance();   // this was the last call at this site
            }
            break;
        }
        case OpKind::BulkDone: {
            std::uint8_t v = script_.has_bulkdone
                ? script_.bulkdone : (op.data != nullptr ? op.data[0] : 0x02);
            if (len > 0) data[0] = v;
            log_read(request, value, index, len);
            advance();
            break;
        }
        case OpKind::PollMasked: {
            std::size_t occ = masked_poll_occurrence_;
            PollState& st = masked_poll_state_[occ];
            if (st.list.empty()) {
                auto it = script_.masked_poll_at.find(occ);
                if (it != script_.masked_poll_at.end() && !it->second.empty()) {
                    st.list = it->second;
                } else {
                    // Default: settle on the very first read, at the op's
                    // own `want` value (matching mask trivially).
                    st.list.push_back({{op.want, 0x55}});
                }
            }
            std::size_t idx = st.idx < st.list.size() ? st.idx : st.list.size() - 1;
            if (st.idx + 1 < st.list.size()) ++st.idx;
            const std::array<std::uint8_t, 2>& v = st.list[idx];
            if (len > 0) data[0] = v[0];
            if (len > 1) data[1] = v[1];
            log_read(request, value, index, len);
            if ((v[0] & op.mask) == op.want) {
                ++masked_poll_occurrence_;
                advance();   // this was the last call at this site
            }
            break;
        }
        case OpKind::ReadModifyWrite: {
            std::size_t occ = rmw_occurrence_;
            std::uint8_t v = 0x00;
            auto it = script_.rmw_read_at.find(occ);
            if (it != script_.rmw_read_at.end()) {
                v = it->second;
            }
            if (len > 0) data[0] = v;
            if (len > 1) data[1] = 0x55;
            log_read(request, value, index, len);
            // No advance(): the write half of this same op (control_write,
            // above) is what moves the cursor past a ReadModifyWrite op.
            break;
        }
        default:
            throw std::runtime_error("gl126_ops_probe: control_read while the program "
                                     "cursor is at a non-read op");
        }
    }

    std::size_t bulk_read(std::uint8_t* data, std::size_t len) override
    {
        expect(OpKind::BulkIn);
        const Op& op = current();
        std::size_t n;
        auto it = script_.bulk_len_at.find(bulk_calls_);
        if (it != script_.bulk_len_at.end()) {
            n = it->second;
        } else if (script_.has_bulk_len) {
            n = script_.bulk_len;
        } else {
            n = static_cast<std::size_t>(op.len);
        }
        if (n > len) n = len;
        for (std::size_t i = 0; i < n; ++i) data[i] = 0;
        std::cout << "B len=" << n << "\n";
        ++bulk_calls_;
        advance();
        return n;
    }

    std::size_t bulk_write(const std::uint8_t* data, std::size_t len) override
    {
        expect(OpKind::BulkOut);
        std::size_t n = len;
        auto it = script_.bulk_out_len_at.find(bulk_out_calls_);
        if (it != script_.bulk_out_len_at.end()) {
            n = it->second;
            if (n > len) n = len;
        }
        // `len=` is the ACCEPTED length (what bulk_write() reports back
        // to the runner, `n`), matching "B len=" (bulk_read)'s own
        // convention -- so a short-write script shows up here the same
        // way test_short_bulk_fails_closed reads "B len=<short>". The
        // digest covers the same `n` bytes ("the bytes written").
        std::cout << "BO len=" << n << " sha256=" << sha256_hex(data, n) << "\n";
        ++bulk_out_calls_;
        advance();
        return n;
    }

    void sleep_ms(unsigned ms) override { clock_ms_ += ms; }
    unsigned now_ms() override { return clock_ms_; }

private:
    const Op& current() const { return prog_.ops[cursor_]; }

    void expect(OpKind kind) const
    {
        if (cursor_ >= prog_.count || current().kind != kind) {
            throw std::runtime_error("gl126_ops_probe: program/wire call sequence mismatch");
        }
    }

    void advance() { ++cursor_; }

    void log_read(std::uint8_t request, std::uint16_t value, std::uint16_t index, std::size_t len)
    {
        std::cout << "R req=" << hex2(request) << " val=" << hex4(value)
                  << " idx=" << hex4(index) << " len=" << len << "\n";
    }

    const OpProgram& prog_;
    const Script& script_;
    std::size_t cursor_ = 0;
    std::size_t ack_calls_ = 0;
    std::size_t read_calls_ = 0;
    std::vector<std::array<std::uint8_t, 2>> poll_list_;
    std::size_t poll_idx_ = 0;
    std::vector<std::array<std::uint8_t, 2>> class_poll_list_;
    std::size_t class_poll_idx_ = 0;
    std::size_t bulk_calls_ = 0;
    std::size_t bulk_out_calls_ = 0;
    unsigned clock_ms_ = 0;

    struct PollState {
        std::vector<std::array<std::uint8_t, 2>> list;
        std::size_t idx = 0;
    };
    std::map<std::size_t, PollState> masked_poll_state_;
    std::size_t masked_poll_occurrence_ = 0;
    std::size_t rmw_occurrence_ = 0;
};

const char* op_kind_name(OpKind k)
{
    switch (k) {
    case OpKind::Write:          return "Write";
    case OpKind::AckRead:        return "AckRead";
    case OpKind::Read:           return "Read";
    case OpKind::PollDataReady:  return "PollDataReady";
    case OpKind::PollClass:      return "PollClass";
    case OpKind::BulkIn:         return "BulkIn";
    case OpKind::BulkOut:        return "BulkOut";
    case OpKind::BulkDone:       return "BulkDone";
    case OpKind::PollMasked:     return "PollMasked";
    case OpKind::ReadModifyWrite:return "ReadModifyWrite";
    }
    return "Unknown";
}

const OpProgram* find_program(const std::string& profile, const std::string& phase)
{
    for (std::size_t i = 0; i < PROFILE_COUNT; ++i) {
        if (profile != PROFILES[i].name) continue;
        const Profile& p = PROFILES[i];
        for (std::size_t j = 0; j < p.program_count; ++j) {
            if (phase == p.programs[j].name) {
                return &p.programs[j];
            }
        }
        return nullptr;
    }
    return nullptr;
}

int cmd_run(int argc, char** argv)
{
    static const char* usage =
        "usage: probe run <profile> <phase> <script> "
        "[--inject name=0xNN ...] [--inject-bulk name=<file> ...]\n";
    if (argc < 5) {
        std::cerr << usage;
        return 2;
    }
    const std::string profile = argv[2];
    const std::string phase = argv[3];
    const std::string script_path = argv[4];

    std::map<std::string, std::uint8_t> injects;
    bool has_injects = false;
    std::map<std::string, std::vector<std::uint8_t>> bulk_injects;
    bool has_bulk_injects = false;
    for (int i = 5; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--inject" && i + 1 < argc) {
            std::string kv = argv[++i];
            std::size_t eq = kv.find('=');
            if (eq == std::string::npos) {
                throw std::runtime_error("gl126_ops_probe: bad --inject argument "
                                         "(want name=0xNN): " + kv);
            }
            std::string name = kv.substr(0, eq);
            unsigned long v = std::stoul(kv.substr(eq + 1), nullptr, 0);
            injects[name] = static_cast<std::uint8_t>(v);
            has_injects = true;
        } else if (arg == "--inject-bulk" && i + 1 < argc) {
            std::string kv = argv[++i];
            std::size_t eq = kv.find('=');
            if (eq == std::string::npos) {
                throw std::runtime_error("gl126_ops_probe: bad --inject-bulk argument "
                                         "(want name=<file>): " + kv);
            }
            std::string name = kv.substr(0, eq);
            bulk_injects[name] = read_file(kv.substr(eq + 1));
            has_bulk_injects = true;
        } else {
            std::cerr << usage;
            return 2;
        }
    }

    const OpProgram* prog = find_program(profile, phase);
    if (prog == nullptr) {
        std::cerr << "unknown profile/phase: " << profile << "/" << phase << "\n";
        return 2;
    }

    Script script = parse_script(script_path);
    ScriptedWire wire(*prog, script);
    RunResult result;

    try {
        run_program(wire, *prog, result, RunPolicy(),
                   has_injects ? &injects : nullptr,
                   has_bulk_injects ? &bulk_injects : nullptr);
    } catch (const OpsError& e) {
        std::cout << "FAIL " << to_string(e.failure) << " op=" << e.op_index
                  << " ops=" << result.ops_done << "\n";
        return 1;
    }
    std::cout << "DONE ops=" << result.ops_done << "\n";
    return 0;
}

int cmd_program_info(int argc, char** argv)
{
    if (argc != 4) {
        std::cerr << "usage: probe program_info <profile> <phase>\n";
        return 2;
    }
    const std::string profile = argv[2];
    const std::string phase = argv[3];
    const OpProgram* prog = find_program(profile, phase);
    if (prog == nullptr) {
        std::cerr << "unknown profile/phase: " << profile << "/" << phase << "\n";
        return 2;
    }
    for (std::size_t i = 0; i < prog->count; ++i) {
        const Op& op = prog->ops[i];
        std::cout << "OP " << i << " kind=" << op_kind_name(op.kind)
                  << " len=" << op.len
                  << " has_data=" << (op.data != nullptr ? 1 : 0) << "\n";
    }
    return 0;
}

int cmd_offset(int argc, char** argv)
{
    if (argc != 4) {
        std::cerr << "usage: probe offset <dark_a.bin> <dark_b.bin>\n";
        return 2;
    }
    std::vector<std::uint8_t> a = read_file(argv[2]);
    std::vector<std::uint8_t> b = read_file(argv[3]);
    OffsetResult r = offset_codes(a.data(), a.size(), b.data(), b.size());
    for (int ch = 0; ch < 3; ++ch) {
        std::cout << "ch=" << ch
                  << " mean_a=" << r.mean_a[ch]
                  << " mean_b=" << r.mean_b[ch]
                  << " slope=" << r.slope[ch]
                  << " code=" << hex4(r.code[ch])
                  << " fallback=" << (r.fallback[ch] ? 1 : 0) << "\n";
    }
    return 0;
}

int cmd_residual(int argc, char** argv)
{
    if (argc != 3) {
        std::cerr << "usage: probe residual <buf.bin>\n";
        return 2;
    }
    std::vector<std::uint8_t> buf = read_file(argv[2]);
    std::cout << (dark_is_residual(buf.data(), buf.size()) ? "RESIDUAL" : "NOT_RESIDUAL") << "\n";
    return 0;
}

int cmd_gain(int argc, char** argv)
{
    if (argc != 3) {
        std::cerr << "usage: probe gain <white.bin>\n";
        return 2;
    }
    std::vector<std::uint8_t> white = read_file(argv[2]);
    GainResult r = gain_codes(white.data(), white.size());
    std::cout << std::setprecision(17);
    for (int ch = 0; ch < 3; ++ch) {
        std::cout << "ch=" << ch << " peak=" << r.peak[ch]
                  << " code=" << hex2(r.code[ch]) << "\n";
    }
    std::cout << "saturated=" << (r.saturated ? 1 : 0) << "\n";
    return 0;
}

int cmd_percentile(int argc, char** argv)
{
    if (argc != 4) {
        std::cerr << "usage: probe percentile <u16.bin> <q>\n";
        return 2;
    }
    std::vector<std::uint8_t> buf = read_file(argv[2]);
    if (buf.size() % 2 != 0) {
        throw std::runtime_error("gl126_ops_probe: percentile input length "
                                 "is not a multiple of 2");
    }
    std::size_t n = buf.size() / 2;
    std::vector<std::uint16_t> values(n);
    for (std::size_t i = 0; i < n; ++i) {
        values[i] = static_cast<std::uint16_t>(buf[i * 2]) |
                   static_cast<std::uint16_t>(static_cast<std::uint16_t>(buf[i * 2 + 1]) << 8);
    }
    double q = std::stod(argv[3]);
    double v = percentile_linear(values.data(), n, q);
    std::cout << std::setprecision(17) << "VALUE=" << v << "\n";
    return 0;
}

int cmd_warmup(int argc, char** argv)
{
    if (argc != 3) {
        std::cerr << "usage: probe warmup <script>\n";
        return 2;
    }
    std::ifstream f(argv[2]);
    if (!f) {
        throw std::runtime_error("could not open " + std::string(argv[2]));
    }
    std::vector<std::string> paths;
    std::string line;
    while (std::getline(f, line)) {
        while (!line.empty() && (line.back() == '\r' || line.back() == '\n')) {
            line.pop_back();
        }
        if (line.empty() || line[0] == '#') {
            continue;
        }
        paths.push_back(line);
    }

    std::size_t next = 0;
    double clock_s = 0.0;
    auto measure = [&]() -> std::vector<std::uint8_t> {
        if (next >= paths.size()) {
            throw std::runtime_error(
                "gl126_ops_probe: warmup script ran out of measurements");
        }
        return read_file(paths[next++]);
    };
    auto sleep_fn = [&](double s) { clock_s += s; };
    auto now_fn = [&]() -> double { return clock_s; };

    WarmupPolicy policy;
    WarmupRecord rec;
    std::uint8_t codes[3] = {0, 0, 0};
    WarmupOutcome outcome = gain_with_warmup(measure, sleep_fn, now_fn, policy, rec, codes);

    std::cout << std::setprecision(17);
    for (unsigned i = 0; i < rec.attempts; ++i) {
        const auto& peaks = rec.peak_history[i];
        const auto& codes_i = rec.gain_history[i];
        std::cout << "ATTEMPT " << (i + 1) << " peaks=" << peaks[0] << "," << peaks[1]
                  << "," << peaks[2] << " codes=" << hex2(codes_i[0]) << ","
                  << hex2(codes_i[1]) << "," << hex2(codes_i[2]) << "\n";
    }

    const char* outcome_str = "Unknown";
    switch (outcome) {
    case WarmupOutcome::Ready:     outcome_str = "Ready"; break;
    case WarmupOutcome::Saturated: outcome_str = "Saturated"; break;
    case WarmupOutcome::Exhausted: outcome_str = "Exhausted"; break;
    }
    std::cout << "OUTCOME " << outcome_str << " attempts=" << rec.attempts
              << " elapsed=" << rec.elapsed_s << " codes=" << hex2(codes[0]) << ","
              << hex2(codes[1]) << "," << hex2(codes[2]) << "\n";
    return 0;
}

void write_file(const std::string& path, const std::vector<std::uint8_t>& data)
{
    std::ofstream f(path, std::ios::binary);
    if (!f) {
        throw std::runtime_error("could not open " + path + " for writing");
    }
    if (!data.empty()) {
        f.write(reinterpret_cast<const char*>(data.data()),
               static_cast<std::streamsize>(data.size()));
    }
}

int cmd_shading_table(int argc, char** argv)
{
    if (argc != 6) {
        std::cerr << "usage: probe shading_table <meas.bin> <lines> <width> <out.bin>\n";
        return 2;
    }
    std::vector<std::uint8_t> meas = read_file(argv[2]);
    unsigned lines = static_cast<unsigned>(std::stoul(argv[3]));
    unsigned width = static_cast<unsigned>(std::stoul(argv[4]));
    std::vector<std::uint8_t> out = shading_table(meas.data(), meas.size(), lines, width);
    write_file(argv[5], out);
    std::cout << "OK len=" << out.size() << "\n";
    return 0;
}

int cmd_shading_table2(int argc, char** argv)
{
    if (argc != 7) {
        std::cerr << "usage: probe shading_table2 <white.bin> <dark.bin> "
                     "<lines> <width> <out.bin>\n";
        return 2;
    }
    std::vector<std::uint8_t> white = read_file(argv[2]);
    std::vector<std::uint8_t> dark = read_file(argv[3]);
    unsigned lines = static_cast<unsigned>(std::stoul(argv[4]));
    unsigned width = static_cast<unsigned>(std::stoul(argv[5]));
    std::vector<std::uint8_t> out = shading_table2(
        white.data(), white.size(), dark.data(), dark.size(), lines, width);
    write_file(argv[6], out);
    std::cout << "OK len=" << out.size() << "\n";
    return 0;
}

/* shading_table2_dual <white.bin> <dark.bin> <lines> <width> <target> <out.bin> */
int cmd_shading_table2_dual(int argc, char** argv)
{
    if (argc != 8) {
        std::cerr << "usage: probe shading_table2_dual <white.bin> <dark.bin> <lines> <width> "
                     "<target> <out.bin>\n";
        return 2;
    }
    std::vector<std::uint8_t> white = read_file(argv[2]);
    std::vector<std::uint8_t> dark = read_file(argv[3]);
    unsigned lines = static_cast<unsigned>(std::stoul(argv[4]));
    unsigned width = static_cast<unsigned>(std::stoul(argv[5]));
    double target = std::stod(argv[6]);
    std::vector<std::uint8_t> out = shading_table2_dual(
        white.data(), white.size(), dark.data(), dark.size(), lines, width, target);
    write_file(argv[7], out);
    std::cout << "OK len=" << out.size() << "\n";
    return 0;
}

/* alternate_lines <buf.bin> <lines> <width> <parity> <out.bin> */
int cmd_alternate_lines(int argc, char** argv)
{
    if (argc != 7) {
        std::cerr << "usage: probe alternate_lines <buf.bin> <lines> <width> <parity> <out.bin>\n";
        return 2;
    }
    std::vector<std::uint8_t> buf = read_file(argv[2]);
    unsigned lines = static_cast<unsigned>(std::stoul(argv[3]));
    unsigned width = static_cast<unsigned>(std::stoul(argv[4]));
    unsigned parity = static_cast<unsigned>(std::stoul(argv[5]));
    std::vector<std::uint8_t> out = alternate_lines(buf.data(), buf.size(), lines, width, parity);
    write_file(argv[6], out);
    std::cout << "OK len=" << out.size() << "\n";
    return 0;
}

/* geometry <profile> <frame> -- frame_geometry() of a named profile's
   frame (1-6, the A+C ledger since Test 58/61). */
int cmd_geometry(int argc, char** argv)
{
    if (argc != 4) {
        std::cerr << "usage: probe geometry <profile> <frame>\n";
        return 2;
    }
    const Profile* p = nullptr;
    for (std::size_t i = 0; i < PROFILE_COUNT; i++) {
        if (std::string(PROFILES[i].name) == argv[2]) p = &PROFILES[i];
    }
    if (p == nullptr) {
        std::cerr << "no profile " << argv[2] << "\n";
        return 2;
    }
    unsigned frame = static_cast<unsigned>(std::stoul(argv[3]));
    FrameGeometry g = frame_geometry(*p, frame);
    std::cout << "GEOMETRY dual=" << (g.dual ? 1 : 0) << " width=" << g.width
              << " wire_lines=" << g.wire_lines << " read_lines=" << g.read_lines
              << " image_lines=" << g.image_lines << " shift_lines=" << g.shift_lines
              << " delivered_lines=" << g.delivered_lines << " chunk_len=" << g.chunk_len
              << " chunk_count=" << g.chunk_count << " feedl_frame1=" << p->feedl_frame1
              << " feedl_pitch=" << p->feedl_pitch << "\n";
    return 0;
}

int cmd_upload_len(int argc, char** argv)
{
    if (argc != 3) {
        std::cerr << "usage: probe upload_len <width>\n";
        return 2;
    }
    unsigned width = static_cast<unsigned>(std::stoul(argv[2]));
    std::cout << "LEN=" << shading_upload_len(width) << "\n";
    return 0;
}

// ------------------------------------------------- hooks 5-7: the frame

/* Not table-driven (read_image_chunk() takes no OpProgram), so this
   mode's fake always succeeds: descriptor write, ack 0x55, then a bulk
   IN of exactly the requested length -- logged in the same W/R/B/BO
   text format cmd_run() uses, so _parse_probe_transfers() in
   tests/test_sane_ops.py handles both alike. */
class ImageChunkWire : public Wire {
public:
    void control_write(std::uint8_t request, std::uint16_t value, std::uint16_t index,
                       const std::uint8_t* data, std::size_t len) override
    {
        std::cout << "W req=" << hex2(request) << " val=" << hex4(value)
                  << " idx=" << hex4(index) << " data=" << hex_bytes(data, len) << "\n";
    }

    void control_read(std::uint8_t request, std::uint16_t value, std::uint16_t index,
                      std::uint8_t* data, std::size_t len) override
    {
        if (len > 0) data[0] = 0x55;
        if (len > 1) data[1] = 0x55;
        std::cout << "R req=" << hex2(request) << " val=" << hex4(value)
                  << " idx=" << hex4(index) << " len=" << len << "\n";
    }

    std::size_t bulk_read(std::uint8_t* data, std::size_t len) override
    {
        for (std::size_t i = 0; i < len; ++i) data[i] = 0;
        std::cout << "B len=" << len << "\n";
        return len;
    }

    std::size_t bulk_write(const std::uint8_t*, std::size_t len) override { return len; }
    void sleep_ms(unsigned ms) override { clock_ms_ += ms; }
    unsigned now_ms() override { return clock_ms_; }

private:
    unsigned clock_ms_ = 0;
};

int cmd_image_chunks(int argc, char** argv)
{
    if (argc != 5) {
        std::cerr << "usage: probe image_chunks <n_full> <full_len> <last_len>\n";
        return 2;
    }
    unsigned n_full = static_cast<unsigned>(std::stoul(argv[2]));
    std::size_t full_len = static_cast<std::size_t>(std::stoul(argv[3]));
    std::size_t last_len = static_cast<std::size_t>(std::stoul(argv[4]));

    ImageChunkWire wire;
    std::vector<std::uint8_t> buf(std::max(full_len, last_len));
    try {
        for (unsigned i = 0; i < n_full; ++i) {
            read_image_chunk(wire, buf.data(), full_len, i == 0);
        }
        read_image_chunk(wire, buf.data(), last_len, n_full == 0);
    } catch (const OpsError& e) {
        std::cout << "FAIL " << to_string(e.failure) << " op=" << e.op_index << "\n";
        return 1;
    }
    std::cout << "DONE\n";
    return 0;
}

int cmd_feedl(int argc, char** argv)
{
    if (argc != 3 && argc != 4) {
        std::cerr << "usage: probe feedl <frame> [profile]\n";
        return 2;
    }
    unsigned frame = static_cast<unsigned>(std::stoul(argv[2]));
    unsigned feedl = feedl_for_frame(frame);
    if (argc == 4) {
        for (std::size_t i = 0; i < PROFILE_COUNT; i++) {
            if (std::string(PROFILES[i].name) == argv[3]) feedl = feedl_for_frame(frame, PROFILES[i]);
        }
    }
    FeedlBytes b = feedl_bytes(feedl);
    std::cout << "FEEDL=" << feedl << " hi=" << hex2(b.hi) << " mid=" << hex2(b.mid)
              << " lo=" << hex2(b.lo) << "\n";
    return 0;
}

/* scanpass <expected_bytes> <event>... -- drive a ScanPass through a
   sequence of events and print the state after each one. Events:
   arm | chunk <bytes> | chunkfail | park | parkfail | close. `park`
   prints the decision; on Run it marks the pass parked (the verified
   PARK completed). `parkfail` = PARK started and failed. `close` = a new
   sane_open (the hardware gate passed) resets the bookkeeping. */
int cmd_scanpass(int argc, char** argv)
{
    if (argc < 4) {
        std::cerr << "usage: probe scanpass <expected_bytes> <event>...\n";
        return 2;
    }
    std::size_t expected = static_cast<std::size_t>(std::stoull(argv[2]));
    ScanPass pass;
    for (int i = 3; i < argc; ++i) {
        std::string ev = argv[i];
        if (ev == "arm") {
            bool ok = pass.arm(expected);
            std::cout << "arm ok=" << (ok ? 1 : 0);
        } else if (ev == "armtail") {
            // arm with a tail the pipeline is known to leave (Test 68)
            if (i + 1 >= argc) { std::cerr << "armtail needs <bytes>\n"; return 2; }
            std::size_t t = static_cast<std::size_t>(std::stoull(argv[++i]));
            bool ok = pass.arm(expected, t);
            std::cout << "armtail ok=" << (ok ? 1 : 0) << " tail=" << pass.tail_bytes();
        } else if (ev == "drain") {
            // end_scan's first step: read the remaining chunks only when the
            // shortfall is exactly the armed tail; otherwise nothing is read
            if (i + 1 >= argc) { std::cerr << "drain needs <chunk_len>\n"; return 2; }
            std::size_t clen = static_cast<std::size_t>(std::stoull(argv[++i]));
            bool pending = pass.drain_pending();
            std::size_t chunks = 0;
            if (pending) {
                while (pass.state() == ScanPassState::Streaming) {
                    std::size_t rem = pass.bytes_expected() - pass.bytes_read();
                    bool first = false;
                    if (!pass.chunk_begin(&first)) break;
                    pass.chunk_done(std::min(clen, rem));
                    ++chunks;
                }
            }
            std::cout << "drain pending=" << (pending ? 1 : 0) << " chunks=" << chunks;
        } else if (ev == "chunk") {
            if (i + 1 >= argc) { std::cerr << "chunk needs <bytes>\n"; return 2; }
            std::size_t n = static_cast<std::size_t>(std::stoull(argv[++i]));
            bool first = false;
            bool ok = pass.chunk_begin(&first);
            if (ok) pass.chunk_done(n);
            std::cout << "chunk ok=" << (ok ? 1 : 0) << " first=" << (first ? 1 : 0);
        } else if (ev == "chunkfail") {
            bool first = false;
            bool ok = pass.chunk_begin(&first);
            if (ok) pass.fail();
            std::cout << "chunkfail ok=" << (ok ? 1 : 0);
        } else if (ev == "park") {
            ParkDecision d = pass.park_decision();
            if (d == ParkDecision::Run) pass.parked();
            std::cout << "park decision=" << park_decision_name(d);
        } else if (ev == "parkfail") {
            ParkDecision d = pass.park_decision();
            if (d == ParkDecision::Run) pass.fail();
            std::cout << "parkfail decision=" << park_decision_name(d);
        } else if (ev == "close") {
            pass = ScanPass();
            std::cout << "close";
        } else {
            std::cerr << "unknown event " << ev << "\n";
            return 2;
        }
        std::cout << " state=" << scan_pass_state_name(pass.state())
                  << " read=" << pass.bytes_read() << "\n";
    }
    return 0;
}

/* tail <read_lines> <crop_lines> <raw_line_bytes> <chunk_len> -- the raw
   bytes the core's pipeline leaves unrequested (unconsumed_tail_bytes). */
int cmd_tail(int argc, char** argv)
{
    if (argc != 6) {
        std::cerr << "usage: probe tail <read_lines> <crop_lines> <raw_line_bytes> <chunk_len>\n";
        return 2;
    }
    try {
        std::size_t t = unconsumed_tail_bytes(static_cast<unsigned>(std::stoul(argv[2])),
                                              static_cast<unsigned>(std::stoul(argv[3])),
                                              static_cast<std::size_t>(std::stoull(argv[4])),
                                              static_cast<std::size_t>(std::stoull(argv[5])));
        std::cout << "tail=" << t << "\n";
    } catch (const std::invalid_argument& e) {
        std::cout << "ERROR " << e.what() << "\n";
    }
    return 0;
}

int cmd_position_timeout(int argc, char** argv)
{
    if (argc != 3) {
        std::cerr << "usage: probe position_timeout <feedl>\n";
        return 2;
    }
    unsigned feedl = static_cast<unsigned>(std::stoul(argv[2]));
    std::cout << "MS=" << position_timeout_ms(feedl) << "\n";
    return 0;
}

} // namespace

int main(int argc, char** argv)
{
    static const char* usage_line =
        "run|program_info|offset|residual|gain|percentile|warmup|"
        "shading_table|shading_table2|upload_len|"
        "image_chunks|feedl|position_timeout|scanpass|tail ...\n";
    if (argc < 2) {
        std::cerr << "usage: " << argv[0] << " " << usage_line;
        return 2;
    }
    std::string mode = argv[1];
    try {
        if (mode == "run") return cmd_run(argc, argv);
        if (mode == "tail") return cmd_tail(argc, argv);
        if (mode == "program_info") return cmd_program_info(argc, argv);
        if (mode == "offset") return cmd_offset(argc, argv);
        if (mode == "residual") return cmd_residual(argc, argv);
        if (mode == "gain") return cmd_gain(argc, argv);
        if (mode == "percentile") return cmd_percentile(argc, argv);
        if (mode == "warmup") return cmd_warmup(argc, argv);
        if (mode == "shading_table") return cmd_shading_table(argc, argv);
        if (mode == "shading_table2") return cmd_shading_table2(argc, argv);
        if (mode == "upload_len") return cmd_upload_len(argc, argv);
        if (mode == "shading_table2_dual") return cmd_shading_table2_dual(argc, argv);
        if (mode == "alternate_lines") return cmd_alternate_lines(argc, argv);
        if (mode == "geometry") return cmd_geometry(argc, argv);
        if (mode == "image_chunks") return cmd_image_chunks(argc, argv);
        if (mode == "feedl") return cmd_feedl(argc, argv);
        if (mode == "position_timeout") return cmd_position_timeout(argc, argv);
        if (mode == "scanpass") return cmd_scanpass(argc, argv);
    } catch (const std::exception& e) {
        std::cerr << "ERROR " << e.what() << "\n";
        return 2;
    }
    std::cerr << "usage: " << argv[0] << " " << usage_line;
    return 2;
}
