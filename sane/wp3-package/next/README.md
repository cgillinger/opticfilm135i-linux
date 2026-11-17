# Next revision (v10): two patches held for after the SANE maintainers react to v9

Prepared offline, no hardware, nothing sent to the SANE merge request. Base: the v9 tip `18bea9b32` (tree `60c3bff8...`). These are the two patches you said you would take into the next revision (the logging patch with me as author, and the load-guidance text). The 20 ms poll-cadence experiment is not included; I am not proposing it. They are here only so you can see exactly what is ready; this directory is not part of the v9 series and nothing needs to merge now.

| file | what |
|---|---|
| `0008-genesys-GL126-log-what-the-magazine-load-saw-when-a-.patch` | Logging only: the `DBG` lines and the `RunResult` `timed_out` record, in `gl126.cpp`, `gl126_ops.cpp` and `gl126_ops.h` (3 files). No offline-mock hooks and no tests: the mock additions and the probe are held back until something in the tree uses them. |
| `0009-genesys-GL126-tell-the-operator-when-to-take-the-mag.patch` | Text only: one extra `DBG` line when the edge wait starts, a "Next step" sentence in the failure message, and a `FILM MAGAZINE (GL126)` section in `doc/sane-genesys.man`. The `genesys.desc` comment change from my branch is left out because you did not ask for it. |

## What the changed lines are

0008: `gl126.cpp`: `#include <cstdio>`, the "edge seen" `DBG` line also shows the reg 0x101 value already held in `status`, the post-settle line prints values the code already reads, and `log_failed_magazine_run()` / `log_load_reg32()` only format records the run had already stored (log text). `gl126_ops.cpp` (three sites) and `gl126_ops.h`: `out.timed_out = PollRecord{...}` is assigned just before the existing timeout throw, plus the `timed_out` member (in-memory bookkeeping). The change is log text and in-memory bookkeeping only; it is not exercised by a test in this series.

0009: log text, message text and the man page section, as above.

No `sanei_usb` call, register read or write, sleep or timing constant is added, removed or reordered in either patch (checked by grepping the added and removed production lines for `sanei_usb`, write, `read_`, sleep, `_ms`, bulk and control).

## Apply

    git checkout 18bea9b32     # the v9 tip
    git am 0008-*.patch 0009-*.patch

## Verified on the v9 tip with both patches (offline)

Built in a fresh clone: v9 tip `18bea9b32`, then `git am` of the new 0008 and 0009 (the 0009 patch is unchanged apart from its `From` hash line).

- `./autogen.sh && ./configure --sysconfdir=/etc BACKENDS=genesys --disable-locking --without-gphoto2 --without-v4l && make -j8 -C lib && make -j8 -C sanei && make -j8 -C backend libsane-genesys.la`: exit 0, 0 compiler warnings.
- `make -C testsuite/backend/genesys check`: PASS 1/1.
- Repository tests against that build (`SANE_BACKENDS_DIR` pointing at the clone), repository tests at `e41f2e9` (the last commit before the holder option, which the v9 series does not have): `tests/test_sane_magazine.py` 42 passed, `tests/test_sane_open_params.py` 7 passed.
- Repository tests at `4efb5bc` (current master): `tests/test_sane_calibration_cache.py` 6 passed, `tests/test_sane_install.py` 21 passed. `test_sane_magazine.py` 41 passed, 2 failed and `test_sane_open_params.py` 7 passed, 1 failed, because master's tests exercise the holder option (`slide_holder`, repo commit `e14cf9e`), which is not in the v9 series or in these patches; I did not run those two files on the v9 tip without 0008 and 0009.
- How those were run: from a checkout of the repository at the named commit, `SANE_BACKENDS_DIR=<path to the sane-backends clone with the series applied and libsane-genesys built> python3 -m pytest -q tests/test_sane_magazine.py tests/test_sane_open_params.py tests/test_sane_calibration_cache.py tests/test_sane_install.py`.
- No test in this series covers 0008: it is log text and in-memory bookkeeping only.

## One wording decision

The repository's guard in `tests/test_sane_magazine.py` (a failure after writes must not say "press load film", "try again", "retry" or "start over") is kept, because after a failed load writes were sent and the scanner state is unknown. So the next step in the failure message reads: "Next step: power-cycle the scanner, put the magazine in loose in the slot, and run the load again; if it still fails, keep the log and report it." The man page says the same.

## Not verified

Not run on hardware. You asked for one scanimage run to see the log lines; I will do that on my unit when you tell me which build to use:

    export LD_LIBRARY_PATH=<build>/lib SANE_CONFIG_DIR=<build>/conf SANE_DEBUG_GENESYS=4
    SANE_DEBUG_DLL=4 scanimage -L 2>&1 | grep -iE 'dlopen|genesys:libusb'   # must name your build; note D=genesys:libusb:BUS:DEV
    scanimage -d $D -n --load-film 2> v10-load.log                          # at the prompt line: magazine out, push in to the stop, leave it
    grep -E 'take the magazine out|edge seen|after the settle|read reg 0x32' v10-load.log
    scanimage -d $D -n --eject-film 2> v10-eject.log

Expected: the "take the magazine out of the slot now..." line when the edge wait starts, then the "edge seen", "after the settle" and reg 0x32 lines.
