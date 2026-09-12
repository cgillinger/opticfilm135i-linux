/* Offline probe for the SANE calibration-cache decision -- used only by
   tests/test_sane_calibration_cache.py. NOT part of the SANE backend build.

   It links the BUILT libsane-genesys.so and drives the REAL public flow
   sane_open -> (option set / cache inject) -> sane_start in the genesys
   backend's test mode (enable_testing_mode + TestScannerInterface, no real
   USB). This exercises the actual decision in genesys_start_scan:

       if (!genesys_restore_calibration(dev, sensor)) { genesys_scanner_
       calibration(dev, sensor); ... }

   The B1 bug was that a compatible cache made genesys_restore_calibration
   return true, so genesys_scanner_calibration -- which runs GL126's
   offset->gain->shading hooks and sets CalStage::ShadingDone -- was skipped,
   and begin_scan then refused the scan. The fix gates the restore off for
   GL126 so calibration always runs.

   Observable, without a real device: genesys_flatbed_calibration records the
   progress message "offset_calibration" (TestScannerInterface stores it) just
   before GL126's offset_calibration runs; in test mode that hook throws at
   once (it reads reg 0x01 != 0x22, the idle-homed check), so the flow stops
   fast with the progress message left at "offset_calibration". If calibration
   is SKIPPED instead, that message is never recorded and the flow reaches
   begin_scan, which throws its own CalStage error. So:
       PROGRESS=offset_calibration  <=> calibration was entered (correct)
       PROGRESS=<anything else>      <=> calibration was skipped (the bug)

   Usage:  cacheprobe <nocache|cache|force|twice>
   Prints, for the (last) sane_start:
       STATUS <n> PROGRESS <msg> [STATUS2 <n> PROGRESS2 <msg>]
   Exit 0 on a clean run (a non-GOOD sane_start status is a valid outcome to
   assert on), 2 on a setup error. */

#include "sane/sane.h"

#include "low.h"
#include "genesys.h"
#include "test_settings.h"
#include "test_scanner_interface.h"
#include "error.h"

#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>

using namespace genesys;

static int find_opt(SANE_Handle h, const char* name)
{
    for (int i = 1; ; ++i) {
        const SANE_Option_Descriptor* d = sane_get_option_descriptor(h, i);
        if (d == nullptr) {
            return -1;
        }
        if (d->name != nullptr && std::strcmp(d->name, name) == 0) {
            return i;
        }
    }
}

static bool set_str(SANE_Handle h, const char* name, const char* value)
{
    int opt = find_opt(h, name);
    if (opt < 0) {
        return false;
    }
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%s", value);
    SANE_Int info = 0;
    return sane_control_option(h, opt, SANE_ACTION_SET_VALUE, buf, &info) == SANE_STATUS_GOOD;
}

static bool set_int(SANE_Handle h, const char* name, SANE_Word value)
{
    int opt = find_opt(h, name);
    if (opt < 0) {
        return false;
    }
    SANE_Int info = 0;
    return sane_control_option(h, opt, SANE_ACTION_SET_VALUE, &value, &info) == SANE_STATUS_GOOD;
}

static bool set_bool(SANE_Handle h, const char* name, SANE_Bool value)
{
    int opt = find_opt(h, name);
    if (opt < 0) {
        return false;
    }
    SANE_Int info = 0;
    return sane_control_option(h, opt, SANE_ACTION_SET_VALUE, &value, &info) == SANE_STATUS_GOOD;
}

// Build a cache entry compatible with what calculate_scan_session produces for
// the current settings, so genesys_restore_calibration would match it (this is
// exactly a "previously saved compatible cache").
static void inject_compatible_cache(Genesys_Device* dev)
{
    auto& sensor = sanei_genesys_find_sensor_for_write(dev, dev->settings.xres,
                                                       dev->settings.get_channels(),
                                                       dev->settings.scan_method);
    auto session = dev->cmd_set->calculate_scan_session(dev, sensor, dev->settings);
    Genesys_Calibration_Cache cache;
    cache.params = session.params;
    cache.session = session;
    cache.frontend = dev->frontend;
    cache.sensor = sensor;
    cache.average_size = 0;
    cache.last_calibration = std::time(nullptr);   // fresh: not expired
    dev->calibration_cache.push_back(cache);
}

static std::string run_start(SANE_Handle h, Genesys_Device* dev, SANE_Status* st_out)
{
    SANE_Status st = sane_start(h);
    *st_out = st;
    auto* iface = dynamic_cast<TestScannerInterface*>(dev->interface.get());
    return iface != nullptr ? iface->last_progress_message() : std::string("<no-test-iface>");
}

int main(int argc, char** argv)
{
    std::string mode = (argc > 1) ? argv[1] : "nocache";

    // Optional argv[2] = "vendor:product" (hex) to drive another model, for the
    // "other models keep the cache behaviour" check. Default: GL126.
    std::uint16_t vid = 0x07b3, pid = 0x1436;
    bool gl126 = true;
    if (argc > 2) {
        unsigned v = 0, p = 0;
        if (std::sscanf(argv[2], "%x:%x", &v, &p) == 2) {
            vid = static_cast<std::uint16_t>(v);
            pid = static_cast<std::uint16_t>(p);
            gl126 = (vid == 0x07b3 && pid == 0x1436);
        }
    }

    enable_testing_mode(vid, pid, 0x0000, nullptr);

    SANE_Int version = 0;
    if (sane_init(&version, nullptr) != SANE_STATUS_GOOD) {
        std::fprintf(stderr, "sane_init failed\n");
        return 2;
    }

    std::string devname = get_testing_device_name();
    SANE_Handle h = nullptr;
    SANE_Status st = sane_open(devname.c_str(), &h);
    if (st != SANE_STATUS_GOOD) {
        std::fprintf(stderr, "sane_open(%s) failed: %d\n", devname.c_str(), st);
        return 2;
    }

    // A real Color capture, so the pinned session and the injected cache
    // describe an actual scan. For GL126 use the 2400 dpi film profile and
    // frame 1; for another model keep its defaults (its resolution list and
    // options differ) and only force Color.
    set_str(h, "mode", "Color");
    if (gl126) {
        set_int(h, "resolution", 2400);
        set_int(h, "frame", 1);
    }

    auto* scanner = reinterpret_cast<Genesys_Scanner*>(h);
    Genesys_Device* dev = scanner->dev;

    if (mode == "cache" || mode == "twice") {
        inject_compatible_cache(dev);
    } else if (mode == "force") {
        inject_compatible_cache(dev);
        // Emulate --force-calibration: the option handler sets force_calibration
        // and clears the cache.
        set_bool(h, "force-calibration", SANE_TRUE);
    }

    SANE_Status st1 = SANE_STATUS_GOOD;
    std::string prog1 = run_start(h, dev, &st1);
    std::printf("STATUS %d PROGRESS %s\n", static_cast<int>(st1), prog1.c_str());

    if (mode == "twice") {
        sane_cancel(h);
        // A saved compatible cache is present again for the second scan; it must
        // still not bypass calibration.
        inject_compatible_cache(dev);
        SANE_Status st2 = SANE_STATUS_GOOD;
        std::string prog2 = run_start(h, dev, &st2);
        std::printf("STATUS2 %d PROGRESS2 %s\n", static_cast<int>(st2), prog2.c_str());
    }

    sane_cancel(h);
    sane_close(h);
    sane_exit();
    return 0;
}
