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

    Called before the first write of any hook that writes. */
void check_start_state(Genesys_Device* dev)
{
    std::uint8_t val = dev->interface->read_register(REG_0x01);
    if (val != 0x22 && val != 0x00) {
        throw SaneException(SANE_STATUS_INVAL,
                            "gl126: refusing to write from an unknown start state "
                            "(reg 0x01 = 0x%02x, expected 0x22 idle-homed or 0x00 "
                            "cold). No registers were written. Power-cycle the "
                            "scanner; no recovery is attempted.", val);
    }
}

/** Write one generated register table, in capture order. */
void write_table(Genesys_Device* dev, const RegPair* regs, std::size_t count)
{
    for (std::size_t i = 0; i < count; i++) {
        dev->interface->write_register(regs[i].reg, regs[i].val);
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

void CommandSetGl126::asic_boot(Genesys_Device* dev, bool cold) const
{
    DBG_HELPER(dbg);
    check_start_state(dev);

    if (cold) {
        /* The cold table is written here, but the three loader-homing
           rounds that follow it in the vendor's sequence are motor moves
           and belong to stage 3. */
        write_table(dev, COLD_INIT, COLD_INIT_COUNT);
        not_brought_up("cold-start homing (asic_boot cold)");
    }

    write_table(dev, BASE_INIT, BASE_INIT_COUNT);
    write_table(dev, AFE_BASE, AFE_BASE_COUNT);
}

void CommandSetGl126::init(Genesys_Device* dev) const
{
    DBG_HELPER(dbg);
    check_start_state(dev);
    write_table(dev, BASE_INIT, BASE_INIT_COUNT);
    write_table(dev, AFE_BASE, AFE_BASE_COUNT);
}

ScanSession CommandSetGl126::calculate_scan_session(const Genesys_Device* /*dev*/,
                                                    const Genesys_Sensor& /*sensor*/,
                                                    const Genesys_Settings& settings) const
{
    DBG_HELPER(dbg);
    /* The geometry model is written out in docs/sane-port.md; it is not
       guessed here. Until it is implemented and checked against a real
       frame, refuse rather than compute a scan window that could position
       the transport somewhere the vendor never does. */
    (void) settings;
    not_brought_up("calculate_scan_session");
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
