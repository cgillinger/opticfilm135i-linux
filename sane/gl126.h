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

/* GL126 command set -- Plustek OpticFilm 135i (07b3:1436).

   STATUS: stage 1 skeleton, UNTESTED against hardware. Every hook that
   would move the motor refuses with a clear message instead of guessing a
   sequence; those are brought up one at a time in stage 3, on the one
   reference unit that exists, with the operator listening for a
   mechanical fault (docs/hardware-safety.md). Do not "fill in" a motor
   hook from the register tables alone.

   The register values come from gl126_tables.h, generated from the
   Python driver's captured tables (docs/sane-port.md stage 2). The chip
   is close enough to GL124 for the genesys framework to fit, but its
   scan flow is the vendor's, not GL124's -- hence a separate command set
   rather than a GL124 model variant.
*/

#ifndef BACKEND_GENESYS_GL126_H
#define BACKEND_GENESYS_GL126_H

#include "genesys.h"
#include "command_set_common.h"

namespace genesys {
namespace gl126 {

class CommandSetGl126 : public CommandSetCommon
{
public:
    ~CommandSetGl126() override = default;

    bool needs_home_before_init_regs_for_scan(Genesys_Device* dev) const override;

    void init(Genesys_Device* dev) const override;

    void init_regs_for_warmup(Genesys_Device* dev, const Genesys_Sensor& sensor,
                              Genesys_Register_Set* regs) const override;

    void init_regs_for_shading(Genesys_Device* dev, const Genesys_Sensor& sensor,
                               Genesys_Register_Set& regs) const override;

    void init_regs_for_scan_session(Genesys_Device* dev, const Genesys_Sensor& sensor,
                                    Genesys_Register_Set* reg,
                                    const ScanSession& session) const override;

    void set_fe(Genesys_Device* dev, const Genesys_Sensor& sensor,
                std::uint8_t set) const override;
    void set_powersaving(Genesys_Device* dev, int delay) const override;
    void save_power(Genesys_Device* dev, bool enable) const override;

    void begin_scan(Genesys_Device* dev, const Genesys_Sensor& sensor,
                    Genesys_Register_Set* regs, bool start_motor) const override;

    void end_scan(Genesys_Device* dev, Genesys_Register_Set* regs,
                  bool check_stop) const override;

    void send_gamma_table(Genesys_Device* dev, const Genesys_Sensor& sensor) const override;

    void offset_calibration(Genesys_Device* dev, const Genesys_Sensor& sensor,
                            Genesys_Register_Set& regs) const override;

    void coarse_gain_calibration(Genesys_Device* dev, const Genesys_Sensor& sensor,
                                 Genesys_Register_Set& regs, int dpi) const override;

    SensorExposure led_calibration(Genesys_Device* dev, const Genesys_Sensor& sensor,
                                   Genesys_Register_Set& regs) const override;

    void wait_for_motor_stop(Genesys_Device* dev) const override;

    void move_back_home(Genesys_Device* dev, bool wait_until_home) const override;

    void update_hardware_sensors(struct Genesys_Scanner* s) const override;

    void update_home_sensor_gpio(Genesys_Device& dev) const override;

    void load_document(Genesys_Device* dev) const override;

    void detect_document_end(Genesys_Device* dev) const override;

    void eject_document(Genesys_Device* dev) const override;

    void send_shading_data(Genesys_Device* dev, const Genesys_Sensor& sensor,
                           std::uint8_t* data, int size) const override;

    /* The vendor uploads shading coefficients to scanner RAM itself, in
       its own format, and verifies them with a second measurement pass.
       The core's host-side shading targets a different data model, so it
       stays out of the way here (docs/sane-port.md, decision 2). */
    bool has_send_shading_data() const override { return false; }

    ScanSession calculate_scan_session(const Genesys_Device* dev,
                                       const Genesys_Sensor& sensor,
                                       const Genesys_Settings& settings) const override;

    void asic_boot(Genesys_Device* dev, bool cold) const override;
};

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_H
