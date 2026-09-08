/* Standalone probe for sane/gl126_ops.{h,cpp} -- used only by
   tests/test_sane_ops.py. Not part of the SANE backend build.

   Usage:
     probe run <profile> <phase> <script>
         Runs the named profile/phase's OpProgram (sane/gl126_tables.h)
         against a scripted Wire fake and prints one line per transfer,
         in the order run_program() performs it:

             W req=<hex2> val=<hex4> idx=<hex4> data=<hex...>
             R req=<hex2> val=<hex4> idx=<hex4> len=<n>
             B len=<n>

         then either "DONE ops=<n>" or "FAIL <failure> op=<i> ops=<n>"
         (failure is BadAck, PollTimeout or ShortBulk -- see OpsFailure).
         `n` after "ops=" is RunResult::ops_done: completed ops, so a
         test can confirm nothing ran after a failure just by checking no
         further lines follow the FAIL line.

     probe offset <dark_a.bin> <dark_b.bin>
         Runs gl126::offset_codes() on two raw RGB16LE buffers and prints
         one "ch=<0|1|2> mean_a=<f> mean_b=<f> slope=<f> code=<hex4>
         fallback=<0|1>" line per channel.

     probe residual <buf.bin>
         Runs gl126::dark_is_residual() on a raw buffer and prints
         RESIDUAL or NOT_RESIDUAL.

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
     bulk_len <n>                     -- override the (single) BulkIn's
                                         returned length (a short read:
                                         n < the op's own len)
     bulkdone <hex-byte>              -- override the (single) BulkDone's
                                         reply
     read_at <occurrence> <hex-bytes> -- override the Nth plain Read's
                                         reply (0-indexed occurrence)

   Every op program in scope here (prep, afe_base, cal_dark_a,
   cal_dark_b) has at most one PollDataReady/BulkIn/BulkDone, so `poll`/
   `bulk_len`/`bulkdone` need no occurrence index. */

#include "../sane/gl126_ops.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

using namespace genesys::gl126;

namespace {

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
    bool has_bulk_len = false;
    std::size_t bulk_len = 0;
    bool has_bulkdone = false;
    std::uint8_t bulkdone = 0;
    std::map<std::size_t, std::vector<std::uint8_t>> read_overrides;
};

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
            std::size_t pos = 0;
            while (pos < rest.size()) {
                std::size_t comma = rest.find(',', pos);
                std::string tok = rest.substr(
                    pos, comma == std::string::npos ? std::string::npos : comma - pos);
                auto bytes = parse_hex(tok);
                std::array<std::uint8_t, 2> v{{0, 0}};
                if (bytes.size() > 0) v[0] = bytes[0];
                if (bytes.size() > 1) v[1] = bytes[1];
                s.poll_list.push_back(v);
                if (comma == std::string::npos) break;
                pos = comma + 1;
            }
        } else if (cmd == "bulk_len") {
            std::size_t n = 0;
            iss >> n;
            s.bulk_len = n;
            s.has_bulk_len = true;
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
        case OpKind::BulkDone: {
            std::uint8_t v = script_.has_bulkdone
                ? script_.bulkdone : (op.data != nullptr ? op.data[0] : 0x02);
            if (len > 0) data[0] = v;
            log_read(request, value, index, len);
            advance();
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
        std::size_t n = script_.has_bulk_len ? script_.bulk_len : static_cast<std::size_t>(op.len);
        if (n > len) n = len;
        for (std::size_t i = 0; i < n; ++i) data[i] = 0;
        std::cout << "B len=" << n << "\n";
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
    unsigned clock_ms_ = 0;
};

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
    if (argc != 5) {
        std::cerr << "usage: probe run <profile> <phase> <script>\n";
        return 2;
    }
    const std::string profile = argv[2];
    const std::string phase = argv[3];
    const std::string script_path = argv[4];

    const OpProgram* prog = find_program(profile, phase);
    if (prog == nullptr) {
        std::cerr << "unknown profile/phase: " << profile << "/" << phase << "\n";
        return 2;
    }

    Script script = parse_script(script_path);
    ScriptedWire wire(*prog, script);
    RunResult result;

    try {
        run_program(wire, *prog, result);
    } catch (const OpsError& e) {
        std::cout << "FAIL " << to_string(e.failure) << " op=" << e.op_index
                  << " ops=" << result.ops_done << "\n";
        return 1;
    }
    std::cout << "DONE ops=" << result.ops_done << "\n";
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

} // namespace

int main(int argc, char** argv)
{
    if (argc < 2) {
        std::cerr << "usage: " << argv[0] << " run|offset|residual ...\n";
        return 2;
    }
    std::string mode = argv[1];
    try {
        if (mode == "run") return cmd_run(argc, argv);
        if (mode == "offset") return cmd_offset(argc, argv);
        if (mode == "residual") return cmd_residual(argc, argv);
    } catch (const std::exception& e) {
        std::cerr << "ERROR " << e.what() << "\n";
        return 2;
    }
    std::cerr << "usage: " << argv[0] << " run|offset|residual ...\n";
    return 2;
}
