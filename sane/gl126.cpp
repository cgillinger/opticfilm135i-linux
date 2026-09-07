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

#define DEBUG_DECLARE_ONLY

#include "gl126.h"
#include "gl126_registers.h"
#include "gl126_tables.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace genesys {
namespace gl126 {

/* ---------------------------------------------------------------- helpers */

namespace {

/** Refuse to touch a scanner whose mechanical state we cannot name.

    This is the C++ side of the driver's safety model (of135i/safety.py,
    docs/hardware-safety.md) and must stay equivalent to it: reg 0x01 is
    read first, and unless it reads 0x22 (idle, homed) or 0x00 (cold,
    never homed) the call fails having written NOTHING. There is exactly
    one unit in existence and no automatic recovery is attempted -- a
    refusal means the operator power-cycles, not that the backend retries
    or homes to "fix" the state.

    Called before the first write of any hook that writes. Returns the
    value read so the caller can tell the cold state (0x00) from the
    idle-homed one (0x22). */
std::uint8_t check_start_state(Genesys_Device* dev)
{
    std::uint8_t val = dev->interface->read_register(REG_0x01);
    if (val != 0x22 && val != 0x00) {
        throw SaneException(SANE_STATUS_INVAL,
                            "gl126: refusing to write from an unknown start state "
                            "(reg 0x01 = 0x%02x, expected 0x22 idle-homed or 0x00 "
                            "cold). No registers were written. Power-cycle the "
                            "scanner; no recovery is attempted.", val);
    }
    return val;
}

/** Write (reg, val) pairs the way the vendor and the Python driver do:
    as 0x40/0x04 wValue 0x83 register batches of up to 32 pairs (64 B)
    per control transfer, in order, duplicates kept (of135i/usbio.py
    write_regs). Genesys_Register_Set cannot carry this -- it de-duplicates
    by address, and the AFE sequence below repeats 0x51/0x5d/0x5e -- and
    one control transfer per register is a wire pattern the unit has
    never been driven with, so the batch goes to the USB device
    directly. */
void write_pairs(Genesys_Device* dev, const RegPair* regs, std::size_t count)
{
    constexpr std::size_t PAIRS_PER_TRANSFER = 32;
    std::uint8_t buf[PAIRS_PER_TRANSFER * 2];

    for (std::size_t i = 0; i < count; i += PAIRS_PER_TRANSFER) {
        std::size_t n = std::min(PAIRS_PER_TRANSFER, count - i);
        for (std::size_t j = 0; j < n; j++) {
            buf[2 * j] = regs[i + j].reg;
            buf[2 * j + 1] = regs[i + j].val;
        }
        dev->interface->get_usb_device().control_msg(REQUEST_TYPE_OUT, REQUEST_BUFFER,
                                                     VALUE_SET_REGISTER, INDEX,
                                                     static_cast<int>(n * 2), buf);
    }
}

/** Write one generated register table, in capture order. */
[[maybe_unused]] void write_table(Genesys_Device* dev, const RegPair* regs, std::size_t count)
{
    write_pairs(dev, regs, count);
}

/** Program the AFE base values. AFE_BASE holds (afe register, value)
    pairs, NOT chip registers: each one reaches the front end through the
    chip's indirection regs -- 0x51 = AFE address, 0x5d = high byte (0),
    0x5e = low byte -- one three-pair batch per value, exactly as
    of135i/device.py initialize() writes them. Writing the table with
    write_table() would instead overwrite chip regs 0x00-0x07 (0x01 among
    them) with AFE values. */
[[maybe_unused]] void write_afe_base(Genesys_Device* dev)
{
    for (std::size_t i = 0; i < AFE_BASE_COUNT; i++) {
        const RegPair triple[3] = {
            { 0x51, AFE_BASE[i].reg },
            { 0x5d, 0x00 },
            { 0x5e, AFE_BASE[i].val },
        };
        write_pairs(dev, triple, 3);
    }
}

/** Write a phase's registers, with computed values patched in.

    `values` supplies one byte per injection name; every injection the
    phase declares must be present. A missing one is a programming error,
    not a fallback to the captured byte: the captured byte is the
    reference unit's calibration and writing it on another unit would
    produce a plausible-looking, wrong scan. */
[[maybe_unused]] void write_phase(Genesys_Device* dev, const Phase& phase,
                 const std::map<std::string, std::uint8_t>& values)
{
    std::vector<RegPair> regs(phase.regs, phase.regs + phase.reg_count);

    for (std::size_t i = 0; i < phase.injection_count; i++) {
        const RegInjection& inj = phase.injections[i];
        auto it = values.find(inj.name);
        if (it == values.end()) {
            throw SaneException(SANE_STATUS_INVAL,
                                "gl126: phase '%s' needs a computed value for '%s'; "
                                "the captured byte belongs to the reference unit and "
                                "must not be written", phase.name, inj.name);
        }
        regs[inj.index].val = it->second;
    }

    write_table(dev, regs.data(), regs.size());
}

/** The profile for a scan: resolution plus whether the IR channel is
    captured. Non-3600 resolutions exist only as dual-light captures. */
[[maybe_unused]] const Profile* find_profile(unsigned dpi, bool ir)
{
    const char* want = nullptr;
    if (dpi == 3600) {
        want = ir ? "ir3600" : "plain3600";
    } else if (dpi == 600) {
        want = "dpi600";
    } else if (dpi == 1200) {
        want = "dpi1200";
    } else if (dpi == 2400) {
        want = "dpi2400";
    } else if (dpi == 7200) {
        want = "dpi7200";
    }
    if (want == nullptr) {
        return nullptr;
    }
    for (std::size_t i = 0; i < PROFILE_COUNT; i++) {
        if (std::string(PROFILES[i].name) == want) {
            return &PROFILES[i];
        }
    }
    return nullptr;
}

/** A hook that has not been brought up against the hardware yet.

    Stage 3 enables these one at a time, in the documented order
    (boot/status -> offset -> gain -> shading -> position -> scan -> park
    -> eject). Until then the backend says so rather than issuing a motor
    command derived from a table it has never executed. */
[[noreturn]] void not_brought_up(const char* hook)
{
    throw SaneException(SANE_STATUS_UNSUPPORTED,
                        "gl126: %s is not brought up against the hardware yet "
                        "(docs/sane-port.md stage 3). The backend refuses rather "
                        "than moving the motor from an unverified sequence.", hook);
}

} // namespace

/* ------------------------------------------------------------------ hooks */

/* The vendor does not home between frames; the scan pass IS the move
   (docs/protocol-notes.md pass 14). Homing here would add a mechanical
   move the vendor never makes. */
bool CommandSetGl126::needs_home_before_init_regs_for_scan(Genesys_Device* /*dev*/) const
{
    return false;
}

/* sane_open writes NOTHING -- like the driver's Scanner.open() and like the
   vendor's app open. The reference (capture 20260907-vendor-open-with-
   latched-magazine: the vendor writes its OPEN table and jogs, never the
   base table) puts the base table at scan start, per frame; the driver
   writes it in scan(), not at open. Writing it at open, as this backend
   did on 2026-09-07 (Test 43), left the unit in the base-table-only state
   -- transient in every verified flow (the AFE_BASE phase overwrites it
   0.1 s later in scan()) -- and the driver's eject stalled twice from it
   (Test 44). So: at open, read the start state and stop. The base table
   is written by the scan-session hooks when they are brought up. */
void CommandSetGl126::asic_boot(Genesys_Device* dev, bool cold) const
{
    DBG_HELPER(dbg);
    std::uint8_t state = check_start_state(dev);
    DBG(DBG_info, "asic_boot: reg 0x01 = 0x%02x (%s), nothing written\n", state,
        cold ? "core asked for cold" : "warm");
}

void CommandSetGl126::init(Genesys_Device* dev) const
{
    DBG_HELPER(dbg);
    std::uint8_t state = check_start_state(dev);
    DBG(DBG_info, "init: reg 0x01 = 0x%02x (%s), nothing written\n", state,
        state == 0x00 ? "cold, never homed" : "idle-homed");
}

/* Pure computation: the ScanSession the core sizes its image pipeline
   from. Nothing here reaches the wire, and nothing here positions the
   transport -- frame positioning is FEEDL from the captured tables
   (docs/sane-port.md, geometry model), not the core's scanner_move. The
   start offsets are kept in the same units the gl124 template uses so
   the core's bookkeeping stays consistent; the scan hook, when it is
   brought up, must pin `pixels`/`lines` to the profile's captured
   geometry rather than trust these values. */
ScanSession CommandSetGl126::calculate_scan_session(const Genesys_Device* dev,
                                                    const Genesys_Sensor& sensor,
                                                    const Genesys_Settings& settings) const
{
    DBG_HELPER(dbg);
    debug_dump(DBG_info, settings);

    float move = dev->model->y_offset + settings.tl_y;
    move = static_cast<float>((move * settings.yres) / MM_PER_INCH);

    float start = dev->model->x_offset + settings.tl_x;
    start = static_cast<float>((start * settings.xres) / MM_PER_INCH);

    ScanSession session;
    session.params.xres = settings.xres;
    session.params.yres = settings.yres;
    session.params.startx = static_cast<unsigned>(start);
    session.params.starty = static_cast<unsigned>(move);
    session.params.pixels = settings.pixels;
    session.params.requested_pixels = settings.requested_pixels;
    session.params.lines = settings.lines;
    session.params.depth = settings.depth;
    session.params.channels = settings.get_channels();
    session.params.scan_method = settings.scan_method;
    session.params.scan_mode = settings.scan_mode;
    session.params.color_filter = settings.color_filter;
    /* As gl124: these come from the device's current settings, which the
       core keeps valid from sane_open on, whereas the incoming settings
       object carries the frontend's exposure field unset at option-init
       time. */
    session.params.contrast_adjustment = dev->settings.contrast;
    session.params.brightness_adjustment = dev->settings.brightness;
    session.params.exposure_lperiod = dev->settings.exposure_lperiod;
    session.params.flags = ScanFlag::NONE;

    compute_session(dev, session, sensor);

    return session;
}

void CommandSetGl126::init_regs_for_scan_session(Genesys_Device* /*dev*/,
                                                 const Genesys_Sensor& /*sensor*/,
                                                 Genesys_Register_Set* /*reg*/,
                                                 const ScanSession& /*session*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("init_regs_for_scan_session");
}

void CommandSetGl126::init_regs_for_warmup(Genesys_Device* /*dev*/,
                                           const Genesys_Sensor& /*sensor*/,
                                           Genesys_Register_Set* /*regs*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("init_regs_for_warmup");
}

void CommandSetGl126::init_regs_for_shading(Genesys_Device* /*dev*/,
                                            const Genesys_Sensor& /*sensor*/,
                                            Genesys_Register_Set& /*regs*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("init_regs_for_shading");
}

void CommandSetGl126::offset_calibration(Genesys_Device* /*dev*/,
                                         const Genesys_Sensor& /*sensor*/,
                                         Genesys_Register_Set& /*regs*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("offset_calibration");
}

void CommandSetGl126::coarse_gain_calibration(Genesys_Device* /*dev*/,
                                              const Genesys_Sensor& /*sensor*/,
                                              Genesys_Register_Set& /*regs*/,
                                              int /*dpi*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("coarse_gain_calibration");
}

/* The lamp is warmed by the gain phase's own retry loop, not by a
   separate LED calibration pass; the vendor has no equivalent step. */
SensorExposure CommandSetGl126::led_calibration(Genesys_Device* /*dev*/,
                                                const Genesys_Sensor& /*sensor*/,
                                                Genesys_Register_Set& /*regs*/) const
{
    DBG_HELPER(dbg);
    return SensorExposure{};
}

void CommandSetGl126::begin_scan(Genesys_Device* /*dev*/, const Genesys_Sensor& /*sensor*/,
                                 Genesys_Register_Set* /*regs*/, bool /*start_motor*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("begin_scan");
}

void CommandSetGl126::end_scan(Genesys_Device* /*dev*/, Genesys_Register_Set* /*regs*/,
                               bool /*check_stop*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("end_scan (PARK)");
}

void CommandSetGl126::move_back_home(Genesys_Device* /*dev*/, bool /*wait_until_home*/) const
{
    DBG_HELPER(dbg);
    /* home() is the scan pass on this chip (pass 14) and is only
       meaningful right after a cold init. Calling it from the scan flow
       would run a scan-length move at an unexpected moment. */
    not_brought_up("move_back_home");
}

void CommandSetGl126::wait_for_motor_stop(Genesys_Device* /*dev*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("wait_for_motor_stop");
}

void CommandSetGl126::load_document(Genesys_Device* /*dev*/) const
{
    DBG_HELPER(dbg);
    /* The magazine load is an interactive, three-move sequence the
       operator drives (tools/load_magazine.py). It is not started from a
       frontend callback. */
    not_brought_up("load_document (magazine load)");
}

void CommandSetGl126::eject_document(Genesys_Device* /*dev*/) const
{
    DBG_HELPER(dbg);
    not_brought_up("eject_document");
}

void CommandSetGl126::detect_document_end(Genesys_Device* /*dev*/) const
{
    DBG_HELPER(dbg);
    /* Frames are addressed by number, not by an end-of-document sensor. */
}

void CommandSetGl126::update_hardware_sensors(struct Genesys_Scanner* /*s*/) const
{
    DBG_HELPER(dbg);
    /* The loader sensor bit is only reliable BEFORE the base table is
       written, so it is read in the load flow rather than polled here. */
}

void CommandSetGl126::update_home_sensor_gpio(Genesys_Device& /*dev*/) const
{
    DBG_HELPER(dbg);
}

void CommandSetGl126::send_shading_data(Genesys_Device* /*dev*/,
                                        const Genesys_Sensor& /*sensor*/,
                                        std::uint8_t* /*data*/, int /*size*/) const
{
    DBG_HELPER(dbg);
    /* has_send_shading_data() is false: the shading upload is part of the
       calibration phases, in the vendor's own format. */
}

void CommandSetGl126::send_gamma_table(Genesys_Device* /*dev*/,
                                       const Genesys_Sensor& /*sensor*/) const
{
    DBG_HELPER(dbg);
    /* The driver delivers linear data; gamma belongs to the frontend. */
}

void CommandSetGl126::set_fe(Genesys_Device* /*dev*/, const Genesys_Sensor& /*sensor*/,
                             std::uint8_t /*set*/) const
{
    DBG_HELPER(dbg);
    /* The AFE is programmed from the phase tables (regs 0x5d/0x5e), not
       through a separate front-end entry point. */
}

void CommandSetGl126::set_powersaving(Genesys_Device* /*dev*/, int /*delay*/) const
{
    DBG_HELPER(dbg);
}

void CommandSetGl126::save_power(Genesys_Device* /*dev*/, bool /*enable*/) const
{
    DBG_HELPER(dbg);
    /* The unit leaves the USB bus a few minutes after a session releases
       it and only a power cycle brings it back, so there is nothing safe
       to do here. */
}

} // namespace gl126
} // namespace genesys
