/* Offline probe for the GL126 magazine options (WP-4) -- used only by
   tests/test_sane_magazine.py. NOT part of the SANE backend build.

   Like gl126_calibration_cache_probe.cpp it links the BUILT
   libsane-genesys.so and drives the REAL public flow sane_open ->
   sane_control_option -> sane_start in the genesys backend's test mode
   (enable_testing_mode + TestScannerInterface, no real USB). What it
   covers is the part the standalone op-probe cannot: that the three
   options exist and are wired to the magazine hooks, and that the state
   machine of docs/sane-wp4-magazine.md section 2.2 refuses what it says
   it refuses -- before any program runs.

   The test scanner interface answers every control IN with zeroes and
   discards every control OUT, so any program that reaches the wire stops
   at its first unacknowledged register write ("register write not
   acknowledged") -- or, for the cold-start program, which has no ack
   reads, at its first fail-closed motor completion (a status word that
   never reads 0xf8). That is itself the observation for the paths that
   are SUPPOSED to reach the wire: a refusal never gets that far, and its
   message says so.

   Usage:
     magprobe options [vid:pid]
         One line per magazine option, in option order:
             OPT <name> type=<n> size=<n> inactive=<0|1> cap=<hex>
         With a vid:pid of another (non-GL126) model, all three must be
         inactive.
     magprobe scenario <name>
         Runs one scripted scenario and prints, in order:
             KEY <device name>            (the mark's device key)
             STATUS <n> <message>         (one per hook call)
             OPTSTATUS <n>                (one per option press)
             STARTSTATUS <n> <message>    (scenarios that call sane_start)
             PROGRESS <msg>               (likewise)
             TEXT <the "magazine" option's value>
             MARK <present|absent>[ <key>]
         Exit 0 whenever the scenario ran (a refusal is a valid outcome
         to assert on); 2 on a setup error.
*/

#include "sane/sane.h"
#include "low.h"
#include "genesys.h"
#include "gl126_lock.h"
#include "gl126.h"
#include "test_settings.h"
#include "test_scanner_interface.h"
#include "error.h"

#include <cstdio>
#include <cstring>
#include <string>

using namespace genesys;

namespace {

const char* const kMagazineOptions[] = { "load-film", "eject-film", "magazine" };

/* Which test checkpoint, if any, should throw -- genesys's own injection
   mechanism (a no-op on the USB interface, a callback here). It throws
   the shape a real USB failure has: a plain SaneException out of the
   device layer, NOT an OpsError. Before Astra's 2026-09-13 review that
   was the hole -- such an exception left the magazine state Released and
   the pending-load mark on disk after an actual failure. */
std::string g_throw_at;

void checkpoint_callback(const Genesys_Device&, TestScannerInterface&,
                         const std::string& name)
{
    if (!g_throw_at.empty() && name == g_throw_at) {
        throw SaneException(SANE_STATUS_IO_ERROR,
                            "injected: invalid read, scanner unplugged?");
    }
}

int find_opt(SANE_Handle h, const char* name)
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

bool set_str(SANE_Handle h, const char* name, const char* value)
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

bool set_int(SANE_Handle h, const char* name, SANE_Word value)
{
    int opt = find_opt(h, name);
    if (opt < 0) {
        return false;
    }
    SANE_Int info = 0;
    SANE_Word v = value;
    return sane_control_option(h, opt, SANE_ACTION_SET_VALUE, &v, &info) == SANE_STATUS_GOOD;
}

/* Call one magazine hook directly and report both the status AND the
   message the operator would be shown.

   Directly, not through sane_control_option: the backend's public entry
   points wrap every exception into a bare status code
   (wrap_exceptions_to_status_code), so the text -- which is most of what
   these hooks are FOR, since a refusal has to say what to do instead --
   never escapes the library. The option handlers are three lines that
   forward to exactly these functions, and the "wiring-*" scenarios below
   prove that they do. */
void call_hook(Genesys_Device* dev, const std::string& which)
{
    SANE_Status st = SANE_STATUS_GOOD;
    std::string msg;
    try {
        if (which == "release") {
            gl126::magazine_release(dev);
        } else if (which == "eject") {
            gl126::magazine_eject(dev);
        } else {
            dev->cmd_set->load_document(dev);
        }
    } catch (const SaneException& e) {
        st = e.status();
        msg = e.what();
    }
    std::printf("STATUS %d %s\n", static_cast<int>(st), msg.c_str());
}

/* Set one of the two buttons through the REAL option path, reporting only
   the status code the frontend would see. */
void press(SANE_Handle h, const char* name)
{
    int opt = find_opt(h, name);
    if (opt < 0) {
        std::printf("OPTSTATUS -1\n");
        return;
    }
    SANE_Int info = 0;
    SANE_Status st = sane_control_option(h, opt, SANE_ACTION_SET_VALUE, nullptr, &info);
    std::printf("OPTSTATUS %d\n", static_cast<int>(st));
}

void print_text(SANE_Handle h)
{
    int opt = find_opt(h, "magazine");
    if (opt < 0) {
        std::printf("TEXT <no such option>\n");
        return;
    }
    char buf[256] = {0};
    SANE_Status st = sane_control_option(h, opt, SANE_ACTION_GET_VALUE, buf, nullptr);
    std::printf("TEXT %s\n", st == SANE_STATUS_GOOD ? buf : "<get failed>");
}

void print_mark()
{
    std::string key;
    if (gl126::magazine_mark_read(&key)) {
        std::printf("MARK present %s\n", key.c_str());
    } else {
        std::printf("MARK absent\n");
    }
}

/* Seed the test interface's register cache: gl126's hooks read reg 0x01,
   the loader sensor (reg 0x101) and regs 0x3b/0x3c before they decide
   anything, and in test mode those reads come from this cache. */
void seed(Genesys_Device* dev, std::uint16_t addr, std::uint8_t value)
{
    dev->interface->write_register(addr, value);
}

std::string progress_of(Genesys_Device* dev)
{
    auto* iface = dynamic_cast<TestScannerInterface*>(dev->interface.get());
    return iface != nullptr ? iface->last_progress_message() : std::string("<no-test-iface>");
}

int cmd_options(int argc, char** argv)
{
    std::uint16_t vid = 0x07b3, pid = 0x1436;
    if (argc > 2) {
        unsigned v = 0, p = 0;
        if (std::sscanf(argv[2], "%x:%x", &v, &p) == 2) {
            vid = static_cast<std::uint16_t>(v);
            pid = static_cast<std::uint16_t>(p);
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
    if (sane_open(devname.c_str(), &h) != SANE_STATUS_GOOD) {
        std::fprintf(stderr, "sane_open(%s) failed\n", devname.c_str());
        sane_exit();
        return 2;
    }
    for (const char* name : kMagazineOptions) {
        int opt = find_opt(h, name);
        if (opt < 0) {
            std::printf("OPT %s MISSING\n", name);
            continue;
        }
        const SANE_Option_Descriptor* d = sane_get_option_descriptor(h, opt);
        std::printf("OPT %s index=%d type=%d size=%d inactive=%d cap=%x constraint=%d\n",
                    name, opt,
                    static_cast<int>(d->type), static_cast<int>(d->size),
                    (d->cap & SANE_CAP_INACTIVE) ? 1 : 0,
                    static_cast<unsigned>(d->cap),
                    static_cast<int>(d->constraint_type));
        if (d->constraint_type == SANE_CONSTRAINT_STRING_LIST &&
            d->constraint.string_list != nullptr)
        {
            for (const SANE_String_Const* v = d->constraint.string_list; *v != nullptr; ++v) {
                std::printf("VALUE %s\n", *v);
            }
        }
    }
    sane_close(h);
    sane_exit();
    return 0;
}

int cmd_scenario(int argc, char** argv)
{
    if (argc < 3) {
        std::fprintf(stderr, "usage: magprobe scenario <name>\n");
        return 2;
    }
    const std::string scenario = argv[2];

    if (scenario == "release-usb-failure") {
        // At the moment the fail guard is armed, before the first
        // program. (Until 2026-09-15 this sat after the cold-start
        // program, which then ran to completion against the test
        // interface because its motor completions were best-effort; now
        // they are fail-closed and the zero-answering interface never
        // gets past the first one -- see "release-cold".)
        g_throw_at = "gl126_magazine_armed";
    } else if (scenario == "eject-usb-failure") {
        g_throw_at = "gl126_magazine_after_eject";
    }

    enable_testing_mode(0x07b3, 0x1436, 0x0000,
                        g_throw_at.empty() ? TestCheckpointCallback()
                                           : TestCheckpointCallback(checkpoint_callback));
    SANE_Int version = 0;
    if (sane_init(&version, nullptr) != SANE_STATUS_GOOD) {
        std::fprintf(stderr, "sane_init failed\n");
        return 2;
    }
    std::string devname = get_testing_device_name();
    SANE_Handle h = nullptr;
    if (sane_open(devname.c_str(), &h) != SANE_STATUS_GOOD) {
        std::fprintf(stderr, "sane_open(%s) failed\n", devname.c_str());
        sane_exit();
        return 2;
    }
    auto* scanner = reinterpret_cast<Genesys_Scanner*>(h);
    Genesys_Device* dev = scanner->dev;
    std::printf("KEY %s\n", dev->file_name.c_str());

    // A real Color capture, so a scenario that calls sane_start describes
    // an actual scan (the same setup the calibration-cache probe uses).
    set_str(h, "mode", "Color");
    set_int(h, "resolution", 2400);
    set_int(h, "frame", 1);

    bool do_start = false;

    if (scenario == "state-initial") {
        // nothing
    } else if (scenario == "release-unknown-state") {
        seed(dev, 0x01, 0x17);            // neither idle-homed nor cold
        call_hook(dev, "release");
    } else if (scenario == "release-idle") {
        seed(dev, 0x01, 0x22);
        call_hook(dev, "release");
    } else if (scenario == "release-cold") {
        seed(dev, 0x01, 0x00);
        call_hook(dev, "release");
    } else if (scenario == "release-twice-after-failure") {
        seed(dev, 0x01, 0x22);
        call_hook(dev, "release");        // fails on the mock wire
        call_hook(dev, "release");        // must refuse: the state is Failed
    } else if (scenario == "eject-cold") {
        seed(dev, 0x01, 0x00);
        call_hook(dev, "eject");
    } else if (scenario == "eject-unknown-state") {
        seed(dev, 0x01, 0x17);
        call_hook(dev, "eject");
    } else if (scenario == "eject-no-magazine") {
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);           // idle class, loader sensor CLEAR
        call_hook(dev, "eject");
    } else if (scenario == "eject-base-table-state") {
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF8);           // magazine present
        seed(dev, 0x3B, 0xFF);
        seed(dev, 0x3C, 0xFF);
        call_hook(dev, "eject");
    } else if (scenario == "wiring-load") {
        // The option really does reach the hook: from an unnameable start
        // state only magazine_release() produces this refusal.
        seed(dev, 0x01, 0x17);
        press(h, "load-film");
    } else if (scenario == "wiring-eject") {
        seed(dev, 0x01, 0x17);
        press(h, "eject-film");
    } else if (scenario == "load-no-mark") {
        gl126::magazine_mark_clear();
        seed(dev, 0x01, 0x17);            // would refuse IF it looked
        call_hook(dev, "load");
    } else if (scenario == "load-mark-no-magazine") {
        gl126::magazine_mark_write(dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);           // loader sensor CLEAR
        call_hook(dev, "load");
    } else if (scenario == "load-mark-bad-state") {
        gl126::magazine_mark_write(dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0xFF);            // not the state the jog leaves
        seed(dev, 0x3C, 0xFF);
        call_hook(dev, "load");
    } else if (scenario == "release-usb-failure") {
        // The cold-start program completes, then the wire fails. Not an
        // OpsError: the guard, not run_magazine_program(), has to catch it.
        seed(dev, 0x01, 0x00);
        gl126::magazine_mark_write(dev->file_name);   // as if one were pending
        call_hook(dev, "release");
    } else if (scenario == "eject-usb-failure") {
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0x00);
        seed(dev, 0x3C, 0x00);
        call_hook(dev, "eject");
    } else if (scenario == "load-mark-invalid-request") {
        // A pending load, a scanner in exactly the right state -- and a
        // scan request that cannot be served. The magazine must NOT move
        // before that is noticed. Frame 9 is past the holder's six
        // apertures; the option's own constraint would refuse it, so it
        // is set behind the option to model a frontend that does not.
        gl126::magazine_mark_write(dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0x00);
        seed(dev, 0x3C, 0x00);
        dev->settings.frame = 9;
        call_hook(dev, "load");
    } else if (scenario == "state-mark-pending") {
        // A release written by an earlier PROCESS. The status line must
        // say a load is pending, not "unknown" (Test 77).
        gl126::magazine_mark_write(dev->file_name);
    } else if (scenario == "scan-after-eject") {
        // Ejected, then a scan attempted. The backend knows nothing is
        // loaded and must refuse rather than scan an empty transport.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);        // loader sensor clear -> "nothing to do"
        call_hook(dev, "eject");       // -> Ejected, no motor command
        call_hook(dev, "load");        // must refuse
    } else if (scenario == "load-after-failure") {
        // A release that failed leaves the transport in a state nobody
        // can name -- and it cleared the mark, so the load half must
        // refuse on the STATE, not on the mark's absence.
        seed(dev, 0x01, 0x22);
        call_hook(dev, "release");        // fails on the mock wire
        call_hook(dev, "load");
    } else if (scenario == "start-no-mark") {
        gl126::magazine_mark_clear();
        seed(dev, 0x01, 0x22);
        do_start = true;
    } else if (scenario == "start-mark-other-device") {
        gl126::magazine_mark_write("libusb:999:999");
        seed(dev, 0x01, 0x22);
        do_start = true;
    } else if (scenario == "start-mark-no-magazine") {
        gl126::magazine_mark_write(dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);           // loader sensor CLEAR
        do_start = true;
    } else {
        std::fprintf(stderr, "unknown scenario: %s\n", scenario.c_str());
        sane_close(h);
        sane_exit();
        return 2;
    }

    if (do_start) {
        SANE_Status st = SANE_STATUS_GOOD;
        std::string msg;
        try {
            st = sane_start(h);
        } catch (const SaneException& e) {
            st = e.status();
            msg = e.what();
        }
        std::printf("STARTSTATUS %d %s\n", static_cast<int>(st), msg.c_str());
        std::printf("PROGRESS %s\n", progress_of(dev).c_str());
        sane_cancel(h);
    }

    print_text(h);
    print_mark();

    sane_close(h);
    sane_exit();
    return 0;
}

} // namespace

int main(int argc, char** argv)
{
    if (argc < 2) {
        std::fprintf(stderr, "usage: magprobe options|scenario ...\n");
        return 2;
    }
    try {
        if (std::strcmp(argv[1], "options") == 0) {
            return cmd_options(argc, argv);
        }
        if (std::strcmp(argv[1], "scenario") == 0) {
            return cmd_scenario(argc, argv);
        }
    } catch (const std::exception& e) {
        std::fprintf(stderr, "ERROR %s\n", e.what());
        return 2;
    }
    std::fprintf(stderr, "usage: magprobe options|scenario ...\n");
    return 2;
}
