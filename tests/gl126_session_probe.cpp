/* Offline probe for the SANE open/parameter path -- used only by
   tests/test_sane_open_params.py. NOT part of the SANE backend build.

   Unlike gl126_ops_probe.cpp (which compiles sane/gl126_ops.cpp standalone
   with no genesys headers), this probe links the BUILT libsane-genesys.so
   and calls the real CommandSetGl126::calculate_scan_session() on a real
   gl126 Genesys_Device + sensor. It reproduces the option/parameter flow
   that runs inside sane_open (genesys init_options -> calc_parameters ->
   calculate_scan_session), which is where the 2026-09-11 regression lived:
   genesys sets its default mode GRAY (colour filter GREEN) and computes
   parameters once before the frontend applies --mode Color, and the colour-
   shift invariant threw on that default, so sane_open failed before any
   scan. The standalone op-probe can never catch that -- the throw needs
   compute_session(), the sensor tables and the model, i.e. the whole
   backend.

   Usage:
     session <dpi> <color|gray> <none|red|green|blue> <visible|ir> [frame]

   Prints one line:
     OK channels=<n> lines=<n> pixels=<n> max_shift=<n> output_lines=<n>
   or, when calculate_scan_session throws (as the buggy build did on the
   GRAY default):
     THROW <message>
   Exit code is 0 on either (a throw is a valid outcome to assert on); 2 on
   a usage/setup error. */

#include "low.h"
#include "gl126.h"
#include "error.h"

#include <cstdio>
#include <cstring>
#include <string>

using namespace genesys;

int main(int argc, char** argv)
{
    if (argc < 5) {
        std::fprintf(stderr,
            "usage: %s <dpi> <color|gray> <none|red|green|blue> <visible|ir> [frame]\n",
            argv[0]);
        return 2;
    }
    unsigned dpi = static_cast<unsigned>(std::stoul(argv[1]));
    std::string mode = argv[2], filter = argv[3], method = argv[4];
    unsigned frame = (argc > 5) ? static_cast<unsigned>(std::stoul(argv[5])) : 1;

    // The tables sane_init() builds (genesys.cpp), so find_sensor and the
    // model list are populated.
    genesys_init_sensor_tables();
    genesys_init_frontend_tables();
    genesys_init_gpo_tables();
    genesys_init_memory_layout_tables();
    genesys_init_motor_tables();
    genesys_init_usb_device_tables();

    const Genesys_Model* model = nullptr;
    for (const auto& entry : *s_usb_devices) {
        if (entry.model().model_id == ModelId::PLUSTEK_OPTICFILM_135I) {
            model = &entry.model();
            break;
        }
    }
    if (model == nullptr) {
        std::fprintf(stderr, "gl126 model not found in s_usb_devices\n");
        return 2;
    }

    Genesys_Device dev;
    dev.model = model;
    sanei_genesys_init_structs(&dev);

    Genesys_Settings s;
    s.scan_method = (method == "ir") ? ScanMethod::TRANSPARENCY_INFRARED
                                     : ScanMethod::TRANSPARENCY;
    s.scan_mode = (mode == "color") ? ScanColorMode::COLOR_SINGLE_PASS
                                    : ScanColorMode::GRAY;
    if (filter == "red")        s.color_filter = ColorFilter::RED;
    else if (filter == "green") s.color_filter = ColorFilter::GREEN;
    else if (filter == "blue")  s.color_filter = ColorFilter::BLUE;
    else                        s.color_filter = ColorFilter::NONE;
    s.xres = dpi;
    s.yres = dpi;
    s.depth = 16;
    s.frame = frame;
    // A plausible raw window, as genesys' own calc_parameters passes before
    // the profile is pinned (the real values are overwritten for a capture,
    // and left as-is for the tolerant preliminary path).
    s.pixels = 876;
    s.requested_pixels = 876;
    s.lines = 927;
    s.tl_x = 0.0;
    s.tl_y = 0.0;

    unsigned channels = (s.scan_mode == ScanColorMode::COLOR_SINGLE_PASS) ? 3 : 1;

    try {
        const Genesys_Sensor& sensor =
            sanei_genesys_find_sensor(&dev, dpi, channels, s.scan_method);
        gl126::CommandSetGl126 cmd;
        ScanSession sess = cmd.calculate_scan_session(&dev, sensor, s);
        /* The delivered width the frontend sees is the pipeline's output
           width -- what calculate_scan_parameters() reports as
           pixels_per_line. build_image_pipeline only constructs nodes here
           (no device I/O until a row is pulled), so it is safe offline and
           is the authoritative check that the host ScaleRows resamples the
           anisotropic dpi2400 raw width (pixels) to the delivered width
           (requested_pixels). */
        auto pipeline = build_image_pipeline(dev, sess, 0, false);
        unsigned delivered = static_cast<unsigned>(pipeline.get_output_width());
        std::printf("OK channels=%u lines=%u pixels=%u max_shift=%u output_lines=%u "
                    "requested=%u delivered=%u raw_line_bytes=%u\n",
                    sess.params.channels, sess.params.lines, sess.params.pixels,
                    sess.max_color_shift_lines, sess.output_line_count,
                    sess.params.get_requested_pixels(), delivered,
                    static_cast<unsigned>(sess.output_line_bytes_raw));
    } catch (const SaneException& e) {
        std::printf("THROW %s\n", e.what());
    }
    return 0;
}
