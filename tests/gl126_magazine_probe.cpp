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
     magprobe layout [vid:pid] [mode]
         One line per option/group descriptor, in display order (index 1
         upward, exactly as a frontend enumerates them):
             ITEM <index> name=<name> type=<n> group=<0|1> inactive=<0|1>
                 TITLE <title>
         then the two option values off the scanner struct (DEFAULT_MODE,
         DEFAULT_COLOR_FILTER) -- sane_open's own defaults if [mode] is
         omitted, or the state after a real SET_VALUE of "mode" to it
         (2026-09-27: the ENABLE(OPT_COLOR_FILTER) / ENABLE(OPT_CONTRAST)/
         ENABLE(OPT_BRIGHTNESS) fix, so a plain switch to Gray does not
         reopen options GL126 hides).
         2026-09-27 digiKam review (docs/sane-install.md S6): the "Film"
         group and its four options' position relative to the other
         groups, and GL126's Color/None defaults.
         Also prints, after DEFAULT_COLOR_FILTER, the two libksane
         workarounds' option-order evidence:
             RESVALUE <n>                 (one per "resolution" word-list
                                            entry, in option/display order)
             MAGVALUE <text>               (one per "magazine" string-list
                                            entry, in option/display order)
     magprobe scenario <name>
         Runs one scripted scenario and prints, in order:
             KEY <device name>            (the mark's device key)
             STATUS <n> <message>         (one per hook call)
             OPTSTATUS <n>                (one per option press or SET)
             OPTINFO <n>                  (the *info word after a magazine
                                            SET; SANE_INFO_RELOAD_OPTIONS
                                            is bit 1, value 2)
             STARTSTATUS <n> <message>    (scenarios that call sane_start)
             PROGRESS <msg>               (likewise)
             TEXT <the "magazine" option's value>
             MARK <present <kind> <key>|absent>   (kind: released|ejected)
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

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <string>
#include <vector>

using namespace genesys;

namespace {

const char* const kMagazineOptions[] = {
    "load-film", "eject-film", "check-status", "magazine",
};

/* Which test checkpoint, if any, should throw -- genesys's own injection
   mechanism (a no-op on the USB interface, a callback here). It throws
   the shape a real USB failure has: a plain SaneException out of the
   device layer, NOT an OpsError. Before Astra's 2026-09-13 review that
   was the hole -- such an exception left the magazine state Released and
   the pending-load mark on disk after an actual failure. */
std::string g_throw_at;

/* WP-5 (docs/sane-wp5-load-button.md section 6): the magazine edge
   wait's own script. On every "gl126_magazine_edge_poll" checkpoint (one
   per poll, fired AFTER that poll's read_register but before the next
   one) this writes g_edge_script[g_edge_step] to reg 0x101, so the NEXT
   read sees it, and advances the step (holding the last value once the
   script is exhausted). An empty script leaves the register exactly as
   seed() left it, forever -- the two timeout shapes (present-only,
   clear-only) need nothing scripted at all, only the right seed before
   the press.

   Also the vehicle for the wait's own "no write happened" assertion
   (implementation note 1 / review finding F): the first poll snapshots
   TestScannerInterface::write_count() (raw USB OUT traffic PLUS every
   write any of its own methods perform -- not just out_transfer_count(),
   which alone would miss a stray write_register()/write_registers()/etc.
   call that never reaches the wire), and every later poll compares
   against it -- any mismatch sets g_edge_write_violation, printed at the
   end as EDGEWRITE so a Python test can assert on it without the
   callback itself aborting the run. The script itself writes through
   seed_register() (uncounted), never write_register() (counted) -- the
   scripting is test setup, not the thing being measured. */
std::vector<int> g_edge_script;
std::size_t g_edge_step = 0;
bool g_edge_baseline_set = false;
unsigned g_edge_baseline = 0;
bool g_edge_write_violation = false;

void set_edge_script(std::initializer_list<int> values)
{
    g_edge_script.assign(values);
    g_edge_step = 0;
}

/* Ejected-kind and Released-retry scenarios below all start the wait with
   the sensor reading present (0xF8, the "resting after eject" case WP-4
   section 10.3 describes) and want it to resolve: clear, clear, present
   x5. Shared so every "the edge really does resolve" scenario scripts
   the identical sequence. */
void set_edge_script_resolve_from_present()
{
    set_edge_script({ 0xF0, 0xF0, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8 });
}

/* Review finding A: the same sequence, but the checkpoint fired on the
   Seen-triggering (5th present) read writes ONE MORE value, `trailing`,
   which is then what the POST-EDGE presence+class re-read sees (nothing
   else changes the register between the wait returning and that
   re-read). Used to script "the magazine was pulled back out during the
   600 ms settle" (trailing = clear, 0xF0) and "present but not the idle
   class" (trailing = 0xD8) without touching production code's own
   settle delay (the mock's sleep_ms is a no-op in test mode, so the
   settle costs no wall time either way). */
void set_edge_script_resolve_from_present_then(std::uint8_t trailing)
{
    set_edge_script({ 0xF0, 0xF0, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, trailing });
}

/* The retry case starts the SECOND wait already clear (the first wait's
   own timeout left it there in every scenario below) and needs one more
   clear read to reach the 2-consecutive threshold before the same five
   presents. */
void set_edge_script_resolve_from_clear()
{
    set_edge_script({ 0xF0, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8 });
}

/* Review finding I: a single-poll clear "glitch" -- present, one clear
   read, present again -- must NOT be mistaken for the magazine coming
   loose (clear_consec resets before reaching kEdgeClearReadsNeeded, so
   saw_clear stays false through it). This script absorbs exactly one
   such glitch and THEN genuinely clears for 2 consecutive reads before
   resolving present x5 -- proving the glitch does not block a real
   resolve either. Seed the register present (0xF8) before the press;
   read order this produces: present(seed), clear(glitch), present
   (glitch interrupted, consec reset), clear, clear (the real
   transition), present x5 (Seen). */
void set_edge_script_debounce_glitch_then_resolve()
{
    set_edge_script({ 0xF0, 0xF8, 0xF0, 0xF0, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8 });
}

/* The other half: the SAME single glitch, but nothing ever clears for
   real afterwards -- must time out with saw_clear FALSE (the glitch
   alone must not count). Seed present (0xF8) before the press; produces
   present(seed), clear(glitch), present, present, present... forever. */
void set_edge_script_debounce_single_glitch_then_present()
{
    set_edge_script({ 0xF0, 0xF8, 0xF8 });
}

void checkpoint_callback(const Genesys_Device&, TestScannerInterface& iface,
                         const std::string& name)
{
    if (!g_throw_at.empty() && name == g_throw_at) {
        throw SaneException(SANE_STATUS_IO_ERROR,
                            "injected: invalid read, scanner unplugged?");
    }
    if (name == "gl126_magazine_edge_poll") {
        if (!g_edge_baseline_set) {
            g_edge_baseline = iface.write_count();
            g_edge_baseline_set = true;
        } else if (iface.write_count() != g_edge_baseline) {
            g_edge_write_violation = true;
        }
        if (!g_edge_script.empty()) {
            std::size_t idx = std::min(g_edge_step, g_edge_script.size() - 1);
            iface.seed_register(0x101, static_cast<std::uint8_t>(g_edge_script[idx]));
            if (g_edge_step + 1 < g_edge_script.size()) {
                ++g_edge_step;
            }
        }
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
            // Kept as "release" (not renamed to "load-film") purely so
            // every EXISTING scenario string in this file and in
            // tests/test_sane_magazine.py that calls it needs no
            // mechanical rename -- the C++ function it reaches is
            // gl126::magazine_load_film() (WP-5), the whole one-button
            // flow, not just a release any more.
            gl126::magazine_load_film(dev);
        } else if (which == "eject") {
            gl126::magazine_eject(dev);
        } else if (which == "check-status") {
            gl126::magazine_check_status(dev);
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

/* SET the "magazine" option through the REAL option path (task 1, the
   2026-09-27 enabled-status-line change): reports the status code AND
   the info word a frontend would see, so a test can assert
   SANE_INFO_RELOAD_OPTIONS came back without the caller needing to know
   its numeric value. */
void set_magazine(SANE_Handle h, const char* value)
{
    int opt = find_opt(h, "magazine");
    if (opt < 0) {
        std::printf("OPTSTATUS -1\n");
        return;
    }
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%s", value);
    SANE_Int info = 0;
    SANE_Status st = sane_control_option(h, opt, SANE_ACTION_SET_VALUE, buf, &info);
    std::printf("OPTSTATUS %d\n", static_cast<int>(st));
    std::printf("OPTINFO %d\n", static_cast<int>(info));
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
    gl126::MagazineMarkKind kind;
    std::string key;
    if (gl126::magazine_mark_read(&kind, &key)) {
        std::printf("MARK present %s %s\n", gl126::magazine_mark_kind_name(kind), key.c_str());
    } else {
        std::printf("MARK absent\n");
    }
}

/* Seed the test interface's register cache: gl126's hooks read reg 0x01,
   the loader sensor (reg 0x101) and regs 0x3b/0x3c before they decide
   anything, and in test mode those reads come from this cache. Through
   seed_register() (uncounted), not write_register() (review finding F):
   this is test setup, not a production write, and must never itself
   trip the "no write during the wait" assertion. */
void seed(Genesys_Device* dev, std::uint16_t addr, std::uint8_t value)
{
    auto* iface = dynamic_cast<TestScannerInterface*>(dev->interface.get());
    if (iface != nullptr) {
        iface->seed_register(addr, value);
    } else {
        dev->interface->write_register(addr, value);
    }
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

/* Every option descriptor in display order (2026-09-27, the digiKam
   review): what tests/test_sane_magazine.py's "options" command cannot
   show, because it only looks up the three magazine options by name.
   This walks index 1 upward exactly as a frontend does, so a test can
   assert group placement and the whole backend's active/inactive split
   without hardcoding an enum index anywhere -- the enum is not public
   API and its numbering is free to move. Also reports the two option
   values an operator sees before touching anything: sane_open's own
   defaults for "mode" and "color-filter", read directly off the
   Genesys_Scanner struct (GET_VALUE on color-filter, hidden for GL126,
   would return SANE_STATUS_INVAL -- see the comment at the read site),
   no SET ever called. An optional third argument sets "mode" through the
   real sane_control_option path first (SANE_ACTION_SET_VALUE), so a test
   can also observe what a frontend switching to Gray does to the option
   set. */
int cmd_layout(int argc, char** argv)
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
    // Optional: exercise set_option_value's OPT_MODE handler through the
    // real path before dumping the option set, so a test can see what
    // switching modes does to color-filter/brightness/contrast (the
    // ENABLE gates fixed 2026-09-27) -- not just init_options' one-time
    // defaults, which the plain "layout" call above already covers.
    if (argc > 3) {
        if (!set_str(h, "mode", argv[3])) {
            std::fprintf(stderr, "failed to set mode=%s\n", argv[3]);
            sane_close(h);
            sane_exit();
            return 2;
        }
    }
    for (int i = 1; ; ++i) {
        const SANE_Option_Descriptor* d = sane_get_option_descriptor(h, i);
        if (d == nullptr) {
            break;
        }
        std::printf("ITEM %d name=%s type=%d group=%d inactive=%d TITLE %s\n",
                    i, d->name != nullptr ? d->name : "(null)",
                    static_cast<int>(d->type),
                    d->type == SANE_TYPE_GROUP ? 1 : 0,
                    (d->cap & SANE_CAP_INACTIVE) ? 1 : 0,
                    d->title != nullptr ? d->title : "(null)");
    }
    // Read init_options' own defaults directly off the scanner struct,
    // not through sane_control_option: GET_VALUE on an inactive option is
    // refused everywhere in genesys (sane_control_option_impl,
    // SANE_OPTION_IS_ACTIVE), by design and unrelated to this change, and
    // color-filter is now one of the options hidden for GL126. Reading
    // the struct field is exactly what sane_control_option's own
    // get_option_value() does internally for an active option; here it
    // is done directly, the same way genesys.cpp itself casts the handle.
    auto* scanner = reinterpret_cast<Genesys_Scanner*>(h);
    std::printf("DEFAULT_MODE %s\n", scanner->mode.c_str());
    std::printf("DEFAULT_COLOR_FILTER %s\n", scanner->color_filter.c_str());
    // Two more display-order facts a test needs (2026-09-27, the two
    // libksane bugs, docs/sane-install.md S6): the "resolution" option's
    // word-list constraint IN OPTION ORDER (what a frontend's combo shows
    // at each index, unlike get_resolutions()'s min/nearest which do not
    // care about order), and the "magazine" option's string-list
    // constraint IN OPTION ORDER (to pin that the values are the plain
    // English constants, not a gettext-translated form).
    {
        int opt = find_opt(h, "resolution");
        if (opt >= 0) {
            const SANE_Option_Descriptor* d = sane_get_option_descriptor(h, opt);
            if (d->constraint_type == SANE_CONSTRAINT_WORD_LIST &&
                d->constraint.word_list != nullptr)
            {
                SANE_Int count = d->constraint.word_list[0];
                for (SANE_Int i = 1; i <= count; ++i) {
                    std::printf("RESVALUE %d\n", static_cast<int>(d->constraint.word_list[i]));
                }
            }
        }
    }
    {
        int opt = find_opt(h, "magazine");
        if (opt >= 0) {
            const SANE_Option_Descriptor* d = sane_get_option_descriptor(h, opt);
            if (d->constraint_type == SANE_CONSTRAINT_STRING_LIST &&
                d->constraint.string_list != nullptr)
            {
                for (const SANE_String_Const* v = d->constraint.string_list; *v != nullptr; ++v) {
                    std::printf("MAGVALUE %s\n", *v);
                }
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
    } else if (scenario == "load-film-killed-mid-wait") {
        // Review finding B: a process killed BETWEEN the jog completing
        // and the wait finishing (Ctrl-C on `scanimage -n --load-film`,
        // or "Terminate" on a frozen digiKam) must leave a mark that
        // makes the NEXT scan refuse. Modelled here as a thrown exception
        // at the wait's own first poll -- reached only after the jog has
        // already run and the pre-wait mark write has already happened
        // (magazine_load_film_impl() writes it before calling
        // wait_for_magazine_edge()) -- so the guard's own failure path
        // never gets a chance to run either; the mark this scenario
        // checks is the ONE the pre-wait write left, not one the guard
        // wrote afterwards.
        g_throw_at = "gl126_magazine_edge_poll";
    }

    // Always registered now (not just when g_throw_at is set): the edge
    // wait's own script/no-write-assertion logic lives in the same
    // callback and every scenario needs it available, even the ones that
    // never populate g_edge_script (the callback is then a no-op for
    // "gl126_magazine_edge_poll" beyond the baseline snapshot).
    enable_testing_mode(0x07b3, 0x1436, 0x0000, TestCheckpointCallback(checkpoint_callback));
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
        // WP-5 (docs/sane-wp5-load-button.md section 3.4): load_document()
        // is a pure checker now -- it never reads the hardware at all, so
        // the sensor/register seeds below are vestigial (kept so the
        // scenario still demonstrates NOTHING is read: a Released mark
        // alone, from ANY hardware state, refuses read-only).
        gl126::magazine_mark_write(dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "load");
    } else if (scenario == "load-mark-bad-state") {
        // Same point, the other seed combination: WP-5's load_document()
        // does not distinguish "sensor clear" from "wrong register state"
        // any more -- both are just "not loaded" now (the distinction only
        // matters inside magazine_load_film_impl()'s own edge-wait/regs
        // check, exercised separately below).
        gl126::magazine_mark_write(dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0xFF);
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
    } else if (scenario == "load-mark-frame-does-not-matter") {
        // WP-5: load_document() no longer validates the scan request at
        // all (it never moves the magazine any more, so Astra's 2026-09-13
        // "an impossible request must not move it first" concern is moot
        // here -- offset_calibration() still validates, unchanged, before
        // ANY scan). A pending Released mark refuses NO_DOCS regardless of
        // the frame number; frame 9 is past the holder's six apertures,
        // set directly (bypassing the option's own constraint) to show
        // load_document() never even looks at it.
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
    } else if (scenario == "load-after-eject-no-magazine") {
        // Ejected ("nothing to do": sensor already clear), then a scan
        // attempted before the new strip is pushed in. Section 10
        // (2026-09-25): this no longer means "the magazine was ejected,
        // press Load film" -- an eject is now a PENDING next-strip load,
        // and the sensor-clear refusal is the same read-only NO_DOCS the
        // Released kind already gives, worded for a strip swap.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);        // loader sensor clear -> "nothing to do"
        call_hook(dev, "eject");       // -> Ejected (mark: ejected), no motor command
        call_hook(dev, "load");        // sensor still clear -> NO_DOCS, mark kept
    } else if (scenario == "load-after-eject") {
        // WP-5: load_document() (the "load" hook here) is a pure checker
        // now -- Ejected (in-process) refuses NO_DOCS "press Load film
        // first" no matter what the hardware reads. Reaching "open" the
        // way this scenario used to prove is now the JOB of "Load film"
        // itself (see load-film-edge-seen-ejected below); this scenario
        // stays to prove the Scan-side gate keeps refusing after an
        // eject, seeds and all, unaffected by what they say.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);        // eject with nothing to do -> Ejected
        call_hook(dev, "eject");
        seed(dev, 0x101, 0xF8);        // strip pushed in: sensor present again
        seed(dev, 0x3B, 0x00);
        seed(dev, 0x3C, 0x00);
        call_hook(dev, "load");        // NO_DOCS -- press Load film first
    } else if (scenario == "load-film-edge-seen-ejected") {
        // The WP-5 replacement for the old "load-after-eject-scan-regs":
        // the Ejected KIND is now reached through "Load film" itself
        // (magazine_load_film_impl()), which waits for the edge BEFORE
        // running "open" -- no jog. Test 90's finding (0x3b/0x3c = 0x02/
        // 0x00, what a 600 dpi scan leaves, NOT the jog's 0x00/0x00) is
        // reproduced here as the regs the post-wait check must accept.
        // Scripted so the wait actually resolves (present -> clear ->
        // clear -> present x5, tests/test_sane_magazine.py's poll_cap_ms
        // override gives it enough polls); "open" then reaches the wire
        // and fails closed on the mock, same evidence every other "open
        // ran" assertion in this suite uses -- and the checkpoint records
        // that NOT ONE of those polls wrote anything (EDGEWRITE 0).
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF8);        // resting after the eject (WP-4 10.3)
        seed(dev, 0x3B, 0x02);         // what the 600 dpi profile leaves
        seed(dev, 0x3C, 0x00);
        set_edge_script_resolve_from_present();
        call_hook(dev, "release");     // wait resolves -> "open" (fails closed)
    } else if (scenario == "load-film-edge-seen-bad-regs") {
        // The WP-5 replacement for the old "load-after-eject-base-table":
        // the one 0x3b/0x3c state the Ejected kind still refuses -- Test
        // 44's base-table-only 0xff/0xff -- but now checked by "Load
        // film" AFTER the edge resolves, not by the Scan-side gate. Read-
        // only INVAL (no "open" attempted), state Failed, mark cleared.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0xFF);
        seed(dev, 0x3C, 0xFF);
        set_edge_script_resolve_from_present();
        call_hook(dev, "release");     // wait resolves, then refuses INVAL
    } else if (scenario == "load-film-edge-present-only") {
        // WP-5 section 3.2/3.6, section 6's "present only": the operator
        // pressed Load film and did nothing else -- the sensor never
        // clears. Times out with NO clear ever seen: Released, "did not
        // come loose" wording, no motor write of any kind (not even
        // "open" -- the regs check after a Seen outcome never runs).
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF8);        // present, and stays present: no script
        call_hook(dev, "release");     // times out, no clear ever seen
    } else if (scenario == "load-film-edge-debounce-glitch-then-resolve") {
        // Review finding I: a single-poll clear glitch must not be
        // mistaken for the reseat, but must also not PREVENT the real
        // one from resolving right afterwards.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0x02);
        seed(dev, 0x3C, 0x00);
        set_edge_script_debounce_glitch_then_resolve();
        call_hook(dev, "release");     // glitch absorbed, then resolves -> "open"
    } else if (scenario == "load-film-edge-debounce-single-glitch-times-out") {
        // The other half: the same single glitch, nothing genuine after
        // it -- must time out with "did not come loose" (saw_clear
        // false), not the default wording.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF8);
        set_edge_script_debounce_single_glitch_then_present();
        call_hook(dev, "release");     // glitch absorbed, then times out, no clear
    } else if (scenario == "load-film-retry-no-clear-rejogs") {
        // Review finding G corrected what this proves: since the FIRST
        // press here is Ejected-origin, a SECOND press with no clear ever
        // seen does NOT re-jog (Ejected never jogs, whatever a retry's
        // saw-clear flag says) -- it waits again, and once that resolves,
        // runs "open" (the lenient regs rule), never "jog". (Test 51's
        // literal double jog is a RELEASED-origin retry, which is not
        // reachable through this probe at all: reaching a timed-out
        // Released state in the first place needs OPEN to have already
        // succeeded, and OPEN's first acknowledgement always fails on
        // this always-zero-answering mock -- documented as a coverage gap,
        // docs/sane-wp5-load-button.md section 9.3.)
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF8);        // present, never clears
        call_hook(dev, "release");     // 1st press: times out, no clear seen
        seed(dev, 0x101, 0xF8);        // present again for the 2nd wait
        seed(dev, 0x3B, 0x02);         // what a 600 dpi scan leaves
        seed(dev, 0x3C, 0x00);
        set_edge_script_resolve_from_present();
        call_hook(dev, "release");     // 2nd press: waits (no jog), then "open"
    } else if (scenario == "load-film-retry-with-clear-then-edge") {
        // The other retry row, same Ejected origin: the first wait DID
        // see a clear (it just never came back present in time). The
        // SECOND press still does not jog (it never would have, being
        // Ejected-origin) and, once the wait resolves, still runs "open"
        // before "load" -- the lenient regs rule, not the strict one a
        // genuine Released-origin retry would use.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF0);        // clear from the start
        call_hook(dev, "release");     // 1st press: times out, clear was seen
        seed(dev, 0x101, 0xF0);        // the second wait starts clear again
        seed(dev, 0x3B, 0x02);
        seed(dev, 0x3C, 0x00);
        set_edge_script_resolve_from_clear();
        call_hook(dev, "release");     // 2nd press: waits, then "open" then "load"
    } else if (scenario == "load-film-cold-after-eject-forces-fresh-path") {
        // The Ejected/Released shortcuts assume the transport is still
        // homed and positioned from the SAME power-on (WP-4 section
        // 10.3's own reasoning) -- so a cold reg 0x01 at "Load film" time
        // must force the full cold_init + open + jog path even from an
        // in-process Ejected state, never the no-jog shortcut. On the
        // mock the cold-start program's own fail-closed motor completion
        // is reached first (identical shape to "release-cold"), which is
        // the proof the shortcut was NOT taken (an Ejected-kind press
        // would have gone straight to the edge wait instead, with no
        // motor completion to fail on at all).
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x01, 0x00);         // power-cycled since the eject
        call_hook(dev, "release");     // must run cold_init, not the shortcut
    } else if (scenario == "release-after-eject") {
        // Load film pressed again after an eject now WAITS first (no jog)
        // instead of releasing again -- section 3.2's Ejected row. The
        // sensor is left clear from the eject step and nothing scripts a
        // change, so the wait times out having SEEN a clear (immediately):
        // Released, the default retry wording, no motor write at all.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        call_hook(dev, "release");     // waits, times out (clear seen), Released
    } else if (scenario == "load-film-edge-seen-then-not-idle") {
        // Review finding A: the edge wait only watches bit 0x08, so a
        // present-but-BUSY class (0xd8: present set, but 0xd8 & 0xf0 =
        // 0xd0 != 0xf0) slipped in during the 600 ms settle must still be
        // caught by the post-edge re-read, not treated as good enough
        // because the wait itself already saw five present reads.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0x02);
        seed(dev, 0x3C, 0x00);
        set_edge_script_resolve_from_present_then(0xD8);   // present, not idle
        call_hook(dev, "release");     // must refuse, no "open" attempted
    } else if (scenario == "load-film-edge-seen-then-clear") {
        // The other half of finding A: the magazine pulled back out
        // during the settle (clear again right after the wait's own
        // Seen decision).
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0x02);
        seed(dev, 0x3C, 0x00);
        set_edge_script_resolve_from_present_then(0xF0);   // clear after settle
        call_hook(dev, "release");
    } else if (scenario == "load-film-blocked-by-loaded-mark") {
        // Review finding D: a "loaded" mark from an EARLIER PROCESS
        // blocks a second "Load film" press exactly like the in-process
        // Loaded state already does -- read-only refusal, mark untouched
        // (this refusal is reached before check_start_state(), let alone
        // the guard, so nothing here could have changed it anyway).
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Loaded, dev->file_name);
        seed(dev, 0x01, 0x22);
        call_hook(dev, "release");
    } else if (scenario == "load-film-killed-mid-wait") {
        // Review finding B: g_throw_at (set above, before
        // enable_testing_mode) fires at the wait's OWN FIRST POLL -- the
        // shape of a process that dies while waiting (Ctrl-C on
        // `scanimage -n --load-film`, "Terminate" on a frozen digiKam).
        // By this point the pre-wait mark write has already run (state
        // Released, mark "released") -- but the exception unwinds
        // through the ARMED MagazineFailGuard too, which is not yet
        // `done_`, so it OVERWRITES that mark with "failed" on the way
        // out. Either mark refuses the next scan; this is which one
        // actually lands (and proves the pre-wait write in isolation
        // would not be enough on its own -- the guard has the last
        // word).
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");       // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF8);        // present, resting after the eject
        call_hook(dev, "release");     // killed at the wait's first poll
    } else if (scenario == "load-film-failed-mark-blocks-scan") {
        // Review finding E: a magazine sequence that fails must leave a
        // mark a SECOND process can see -- the failure clears the
        // in-process state, and used to clear the mark too, so nothing
        // survived a process boundary at all.
        seed(dev, 0x01, 0x22);
        call_hook(dev, "release");     // fails on the mock wire -> Failed, mark failed
        call_hook(dev, "load");        // this "process"'s own scan gate: refuses too
    } else if (scenario == "state-mark-failed-pending") {
        // The cross-process half of the same finding: a "failed" mark
        // written by an EARLIER PROCESS, used with load-mark-failed-
        // crossproc under a shared OF135I_LOCK_FILE.
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Failed, dev->file_name);
    } else if (scenario == "load-mark-failed-crossproc") {
        // Writes nothing: the "failed" mark must already be on disk from
        // a prior invocation of state-mark-failed-pending sharing the
        // same lock path. A fresh process (Unknown in-process) must
        // still refuse -- on the mark alone -- for Scan, Load film and
        // Eject film alike.
        seed(dev, 0x01, 0x22);
        call_hook(dev, "load");
        call_hook(dev, "release");
        call_hook(dev, "eject");
    } else if (scenario == "load-film-ejected-origin-retry-keeps-open") {
        // Review finding G: the bug it found. An Ejected-origin press
        // that times out with a clear seen, retried, must STILL run
        // "open" (no jog either time) and use the LENIENT regs rule --
        // before the fix, the second press forgot the origin the moment
        // state became Released, used the strict 0x00/0x00 rule, and
        // refused Failed on exactly these regs (0x02/0x00, what a 600
        // dpi scan leaves).
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");           // -> Ejected (mark: ejected)
        seed(dev, 0x101, 0xF0);            // clear from the start
        call_hook(dev, "release");         // 1st press: times out, clear seen
        seed(dev, 0x101, 0xF0);            // the second wait starts clear again
        seed(dev, 0x3B, 0x02);             // what a 600 dpi scan leaves
        seed(dev, 0x3C, 0x00);
        set_edge_script_resolve_from_clear();
        call_hook(dev, "release");         // 2nd press: must still run "open"
    } else if (scenario == "load-film-loaded-then-cold") {
        // Review finding H: a power cycle since a load completed. A real
        // LOAD never completes on this always-zero-answering mock (every
        // "reaches the wire" proof in this suite fails closed at the
        // first unacknowledged write), so the in-process Loaded state is
        // not directly reachable here -- its cross-process form, a
        // "loaded" mark, exercises the identical code path (the precheck
        // does not distinguish the two: "had_mark || had_state"). Must
        // fall into the fresh path, not "already loaded".
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Loaded, dev->file_name);
        seed(dev, 0x01, 0x00);         // power-cycled since the load
        call_hook(dev, "release");     // must run cold_init, not "already loaded"
    } else if (scenario == "state-mark-ejected-pending") {
        // An Ejected mark written by an EARLIER PROCESS -- used with
        // load-mark-ejected-crossproc under a shared OF135I_LOCK_FILE to
        // prove the mark (not in-process state) carries the kind across
        // a process boundary, device key with spaces included (the test
        // device's own name, printed as KEY above).
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Ejected, dev->file_name);
    } else if (scenario == "load-mark-ejected-crossproc") {
        // Writes nothing: the mark must already be on disk from a prior
        // invocation of state-mark-ejected-pending sharing the same lock
        // path. WP-5: load_document() refuses NO_DOCS regardless -- the
        // seeds below are vestigial, kept to show they are never read.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF8);
        seed(dev, 0x3B, 0x00);
        seed(dev, 0x3C, 0x00);
        call_hook(dev, "load");
    } else if (scenario == "start-mark-ejected-other-device") {
        // An Ejected mark for a foreign device (or this one under a
        // pre-power-cycle address): ignored and cleared, same as the
        // Released mark's equivalent, and the scan proceeds normally.
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Ejected, "libusb:999:999");
        seed(dev, 0x01, 0x22);
        do_start = true;
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
        // WP-5: refuses NO_DOCS before calibration regardless of the
        // hardware -- the seeds are vestigial (kept to show they are
        // never read; the name is historical, from when the sensor
        // state was the reason).
        gl126::magazine_mark_write(dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        do_start = true;
    } else if (scenario == "start-mark-loaded") {
        // section 3.4: a "loaded" mark from an EARLIER PROCESS (Load film
        // ran there, then that process exited -- `scanimage -n
        // --load-film`) lets a scan proceed in THIS one, exactly like the
        // in-process Loaded state does.
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Loaded, dev->file_name);
        seed(dev, 0x01, 0x22);
        do_start = true;
    } else if (scenario == "load-mark-loaded") {
        // The hook directly: a "loaded" mark lets load_document() return
        // without refusing, and the mark is NOT consumed by a mere check
        // -- only an eject, a failure, or a cold read clears it. Seeded
        // idle-homed (review finding J: load_document() now reads reg
        // 0x01 too, and the test interface's own default is cold).
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Loaded, dev->file_name);
        seed(dev, 0x01, 0x22);
        call_hook(dev, "load");
    } else if (scenario == "wiring-check-status") {
        // The third button really is wired to the hook: unlike Load film
        // and Eject film, Check status never refuses (it only reads), so
        // the wiring proof here is simply that it returns GOOD through
        // the real option path from a state the OTHER two buttons would
        // have refused from.
        seed(dev, 0x01, 0x17);
        press(h, "check-status");
    } else if (scenario == "check-status-cold") {
        // section 3.5's one real transition: reg 0x01 = 0x00 resets the
        // state to Unknown and clears any mark, whatever was pending.
        gl126::magazine_mark_write(dev->file_name);   // as if a release were pending
        seed(dev, 0x01, 0x00);
        call_hook(dev, "check-status");
    } else if (scenario == "check-status-no-magazine") {
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);           // loader sensor CLEAR
        call_hook(dev, "check-status");
    } else if (scenario == "check-status-present-not-loaded") {
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF8);           // idle class, sensor present
        call_hook(dev, "check-status");
    } else if (scenario == "check-status-loaded-mark") {
        // Loaded is reported from a cross-process mark too -- but it is
        // NEVER promoted to Loaded on hardware evidence alone (the sensor
        // cannot tell "loaded" from "loose in the slot", section 3.5's
        // honest limit): this only reports it, it does not set the
        // in-process state to Loaded.
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Loaded, dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF8);
        call_hook(dev, "check-status");
    } else if (scenario == "check-status-loaded-non-idle") {
        // Review finding C: right after LOAD completes (or during
        // calibration) reg 0x101 reads 0xdc/0xd8-shaped -- NEITHER is the
        // idle class (0xf0-shaped) -- and a genuinely loaded magazine
        // must be reported as "loaded", not "unknown state -- power-
        // cycle", just because of that.
        gl126::magazine_mark_write(gl126::MagazineMarkKind::Loaded, dev->file_name);
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xDC);           // present, NOT the idle class
        call_hook(dev, "check-status");
    } else if (scenario == "check-status-unknown-hw") {
        // "unknown state -- power-cycle" is reserved for reg 0x01 OUTSIDE
        // {0x22, 0x00} (review finding C) -- seeded here as 0x17, since
        // 0x22 with any sensor reading now resolves to one of the other
        // three rows.
        seed(dev, 0x01, 0x17);
        seed(dev, 0x101, 0xF8);
        call_hook(dev, "check-status");
    } else if (scenario == "magazine-set-accepts-a-listed-value-as-a-no-op") {
        // Task 1 (2026-09-27): OPT_MAGAZINE is settable now (so KSane
        // renders it enabled/black), but SET must never change the
        // state or the on-disk mark -- GET afterwards must still return
        // the TRUE text. Get to a real, non-default state first: the
        // "nothing to do" eject (loader sensor already clear) is the one
        // state transition in this file that succeeds GOOD without
        // touching the wire at all (every OTHER hook call in this suite
        // fails closed on the silent mock, by design -- see
        // "eject-no-magazine"), so it is the only state reachable here
        // to set the option AGAINST.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);            // loader sensor CLEAR
        call_hook(dev, "eject");           // -> Ejected, mark written, GOOD
        // Set it to a DIFFERENT listed value than the current one,
        // without hardcoding either string: the state values array's
        // first entry (kMagazineUnknown) is never what "eject" left
        // behind.
        set_magazine(h, gl126::magazine_state_values()[0]);
    } else if (scenario == "magazine-set-rejects-an-unlisted-value") {
        // The constraint check happens in SANE core
        // (sanei_constrain_value, called from sane_control_option_impl
        // before set_option_value() ever runs) -- this exercises that
        // real path, not a hand-rolled check in the handler.
        seed(dev, 0x01, 0x22);
        seed(dev, 0x101, 0xF0);
        call_hook(dev, "eject");
        set_magazine(h, "not one of the constrained values");
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
    std::printf("EDGEWRITE %d\n", g_edge_write_violation ? 1 : 0);

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
        if (std::strcmp(argv[1], "layout") == 0) {
            return cmd_layout(argc, argv);
        }
        if (std::strcmp(argv[1], "scenario") == 0) {
            return cmd_scenario(argc, argv);
        }
    } catch (const std::exception& e) {
        std::fprintf(stderr, "ERROR %s\n", e.what());
        return 2;
    }
    std::fprintf(stderr, "usage: magprobe options|layout|scenario ...\n");
    return 2;
}
