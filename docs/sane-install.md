# Installing the GL126 backend, and the digiKam path (WP-2)

Written offline 2026-09-13 against HEAD `aed052a`. This is the install and
frontend half of ROADMAP's **WP-2**. §9 states exactly what has been
exercised and what has not; the system install itself and the two hardware
scans are in `docs/sane-wp2-hardware-plan.md` and wait for Christian's go.

Reference host: Fedora 44 KDE, `sane-backends` 1.4.0-6.fc44 (RPM),
digiKam 9.1.0 (RPM) on KSaneCore/KSaneWidgets 26.08.0 (RPM). Not Flatpak,
not AppImage — that matters: a Flatpak digiKam would carry its own SANE
inside the sandbox and would not see a backend installed on the host at all.
Check before trusting any of this: `rpm -q digikam sane-backends` and
`flatpak list | grep -i digikam` (empty here).

## 1. Why the install has the shape it has

SANE's `dll` meta-backend does not search a library path at run time in any
way we can steer per-application. `backend/dll.c` builds one directory list —
the directories in `LD_LIBRARY_PATH`, then the compiled-in `LIBDIR` — and
`dlopen()`s the **absolute path** `<dir>/libsane-genesys.so.1`. Consequences:

* `ld.so.conf.d` does nothing: the loader is never asked to resolve a soname.
* A second copy under `/usr/local` does nothing: the distribution's
  `libsane.so.1` has `LIBDIR=/usr/lib64/sane` compiled in.
* Renaming the library to `libsane-gl126.so.1` and adding `gl126` to
  `dll.conf` does not work either: `dll.c` resolves `sane_<backend-name>_init`
  and friends, and our library exports `sane_genesys_*` (it *is* the genesys
  backend). A different name would need a second `BACKEND_NAME` build target.
* `LD_LIBRARY_PATH` does work — it is how every bring-up run since Test 41
  was done — but it is a development crutch: it has to be exported into
  digiKam's environment too, and it changes dynamic linking for the whole
  process.

So a normal installation means: the file the distribution's `dll` backend
already `dlopen()`s must be our build. The smallest way to get there without
overwriting a packaged file:

| what | how |
|---|---|
| our library | installed **under its own name**, `/usr/lib64/sane/libsane-genesys-gl126.so.1.4.0` — no packaged file is overwritten |
| `libsane-genesys.so.1` | the one packaged symlink that is repointed at our file; its original target is recorded beside it |
| `libsane-genesys.so` | left pointing at the distribution's library (it is only the development link) |
| `/etc/sane.d/genesys.conf` | the 135i USB id appended between markers (a `%config` file; editing it is supported and reversible) |
| `/etc/sane.d/dll.conf` | **untouched** — `genesys` is already enabled |
| other backends | untouched: airscan, hpaio and the rest keep their own `.so` files and configs |

Our build is upstream master (1d47d7c, i.e. 1.4.0 plus later upstream work)
with GL126 added — it carries upstream's whole genesys model table, so other
genesys scanners are expected to keep working while it is installed. Expected,
not tested: this project has one scanner and one model to test with.

`tools/sane_install.sh` does exactly the four rows above, and `uninstall`
reverses them.

## 2. Prerequisites

Build dependencies actually used by this build on Fedora 44 (all present on
the reference host):

```
sudo dnf install gcc-c++ make autoconf automake libtool gettext-devel \
                 libusb1-devel libtiff-devel libjpeg-turbo-devel libxml2-devel
```

USB permissions: `udev/60-of135i.rules` (`TAG+="uaccess"` for 07b3:1436) —
already installed on the reference host. It is what lets both `of135i` and the
SANE backend reach the device without root:

```
sudo cp udev/60-of135i.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

Nothing else is needed: the backend needs no group membership, no
`sane-backends`-side udev rule, and no `saned`.

SELinux (enforcing on the reference host) needs no policy work: the file name
we install maps to `lib_t` exactly as the distribution's does
(`matchpathcon /usr/lib64/sane/libsane-genesys-gl126.so.1.4.0`), and the
installer runs `restorecon` on it anyway.

## 3. Build

The port's sources live in this repository's `sane/`; a sane-backends
checkout picks them up by symlink. **All nine files** — the older five-file
list in `docs/sane-port.md` predates `gl126_ops` and `gl126_lock` and builds a
library that does not link:

```
cd /path/to/sane-backends/backend/genesys
for f in gl126.h gl126.cpp gl126_registers.h \
         gl126_tables.h gl126_tables.cpp \
         gl126_ops.h gl126_ops.cpp \
         gl126_lock.h gl126_lock.cpp; do
    ln -sf /path/to/opticfilm135i-linux/sane/$f $f
done
```

Then the integration patch and the build. The reference clone
(`~/Dokument/Github/sane-backends`, branch `gl126-opticfilm135i`) is
sane-backends master **1d47d7c** with `sane/gl126-integration.patch` applied
but uncommitted; `git diff` there is byte-identical to the checked-in patch
(verified 2026-09-13).

```
cd /path/to/sane-backends
git checkout -b gl126-opticfilm135i 1d47d7c
patch -p1 < /path/to/opticfilm135i-linux/sane/gl126-integration.patch
./autogen.sh
./configure --sysconfdir=/etc BACKENDS=genesys --disable-locking \
            --without-gphoto2 --without-v4l
make -j8 -C backend libsane-genesys.la
```

**`--sysconfdir=/etc` is not optional, and getting it wrong is nearly
invisible.** It sets the sanei_config search path compiled into the library
(`.:/etc/sane.d`). Without it the default prefix applies and the library
looks in `/usr/local/etc/sane.d`, where nothing is installed —
yet `scanimage` still works, because there `libsane.so.1` is in the global
symbol scope and interposes its own `sanei_config_open`. A frontend that
loads SANE through a `dlopen`ed plugin with `RTLD_LOCAL` gets no
interposition and fails with *Couldn't access configuration file
'genesys.conf'*. That is exactly what happened to digiKam on 2026-09-13
(Test 74). `tools/sane_install.sh` now refuses to install a library whose
compiled path does not match, and `status` prints it. If you change
configure flags, note that automake does **not** rebuild on that alone:

```
make -C sanei clean && make -j8 -C sanei
make -C backend clean && make -j8 -C backend libsane-genesys.la
```

`BACKENDS=genesys` builds only what we install. `--disable-locking` concerns
SANE's own `sanei_access` device locking, which genesys does not use — our
mutual exclusion with the Python driver is `gl126_lock` (below), unaffected.
The result is `backend/.libs/libsane-genesys.so.1.4.0` (~27 MB unstripped;
it is installed as built, debug symbols included).

## 4. Install

```
tools/sane_install.sh status        # no root; shows what is where
sudo tools/sane_install.sh install  # Howdy will ask for your face
tools/sane_install.sh status
```

`status` before an install on the reference host prints, and every line
should be checked:

```
gl126 sources  : all 9 present in .../sane-backends/backend/genesys
built library  : .../backend/.libs/libsane-genesys.so.1.4.0
                 sha256 1062ed01560f7e52...  612 gl126 symbols
backend dir    : /usr/lib64/sane
genesys.so.1   -> libsane-genesys.so.1.4.0        <- the distribution's
installed lib  : none
/etc/sane.d/genesys.conf : 135i USB id MISSING
udev rule      : installed
dll.conf       : genesys enabled
```

After `install`, `genesys.so.1 -> libsane-genesys-gl126.so.1.4.0`,
`installed lib` reports *identical to the current build*, and the USB id is
present. `install` refuses if any of the nine sources is missing from the
clone or if the built library carries no gl126 symbols, and it is idempotent.

The manual equivalent, if you would rather not run the script:

```
sudo install -m 0755 backend/.libs/libsane-genesys.so.1.4.0 \
        /usr/lib64/sane/libsane-genesys-gl126.so.1.4.0
readlink /usr/lib64/sane/libsane-genesys.so.1   # note this down
sudo ln -sfn libsane-genesys-gl126.so.1.4.0 /usr/lib64/sane/libsane-genesys.so.1
printf '\n# Plustek OpticFilm 135i\nusb 0x07b3 0x1436\n' | sudo tee -a /etc/sane.d/genesys.conf
```

## 5. Which library is actually loaded

A green `scanimage -L` is not proof by itself — the point is *which file*
served it.

* `tools/sane_install.sh verify` runs `SANE_DEBUG_DLL=4 scanimage -L`, checks
  the `dlopen()`ing line against `/usr/lib64/sane/libsane-genesys.so.1`, and
  **exits non-zero if it does not match** — a different genesys backend, a
  failing `scanimage`, or a leftover `LD_LIBRARY_PATH`/`SANE_CONFIG_DIR` all
  fail the check rather than passing quietly. It separates the two questions:
  `LOAD OK` (the right library was loaded) and `DEVICE OK` (the 135i was
  enumerated). Exit 0 = both, 1 = library right but no device, 2 = wrong
  library or a broken run. (This enumerates a connected scanner — hardware
  session only.)
* `tools/sane_install.sh status` compares the installed file with the current
  build byte for byte, so "the right path" also means "the right build".
* For digiKam, the same `dll` backend does the loading. `ldd` on
  `/usr/lib64/qt6/plugins/digikam/generic/Generic_DigitalScanner_Plugin.so`
  shows the chain: `libKSaneWidgets6.so.6` → `libKSaneCore6.so.1` →
  `/lib64/libsane.so.1` — the `dll` meta-backend, whose compiled-in `LIBDIR`
  is `/usr/lib64/sane` (its own debug output says so). digiKam therefore
  loads the same file `scanimage` does; there is no second SANE to configure.
  **But `ldd` is not proof.** It shows what is linked at start-up; the
  backend itself arrives later, through `dlopen`, and nothing in the link
  map says which file that will be. Two ways to see the real thing, both
  after the scanner dialog has been opened at least once (the library is
  loaded lazily, on the first `sane_init`):

  ```
  grep libsane-genesys /proc/$(pidof digikam)/maps      # the mapped file
  ls -l /proc/$(pidof digikam)/map_files/ | grep libsane-genesys
  ```

  The `libsane-genesys*` path there is the file that is really serving the
  scan — resolve it (`ls -l`) and compare with
  `tools/sane_install.sh status`. Belt and braces: start digiKam with
  `SANE_DEBUG_DLL=4` and read its `dlopen()`ing line in the log, which is
  the same evidence `verify` checks for `scanimage`.

## 6. The digiKam workflow, and where its limits are

Verified by reading KSaneCore/libksane 26.08 sources and the installed
binaries, not by running digiKam.

**Path:** digiKam → *Import* → *Import from Scanner* → pick the device.
The device dialog is a radio-button list built from `sane_get_devices()`,
labelled `PLUSTEK : OpticFilm 135i` with the `genesys:libusb:BBB:DDD` string
underneath. There is no free-text field, so the device string cannot be typed
in; pick the row.

**Settings for a plain 3600 colour scan of frame 1:**

| control | where | value |
|---|---|---|
| Source | Basic Options | `Transparency Adapter` (IR is `Transparency Adapter Infrared`) |
| Mode | Basic Options | **`Color`**. genesys' own default is Gray, and for this model (`is_cis = false`) its default colour filter is **Green** — a single-channel gray the vendor never captures, refused before any device I/O. Gray *is* available, but only with *Color filter = None* (host-side gray) on the Scanner Specific Options tab |
| Bit depth | Basic Options | 16 (the only value the model offers) |
| Resolution | Basic Options | 3600 (the list is 600/1200/2400/3600/7200) |
| Frame | **Scanner Specific Options** | 1 (range 1–6) |
| Output format | save dialog | PNG or TIFF — both lossless. KSaneCore hands over 16 bits per channel (`QImage::Format_RGBX64`) and digiKam's `DImg` can keep them, but choosing a lossless format is **not evidence** that 16 bits reached the file: check the file itself with `tools/image_probe.py` (below) |

`Frame` appears automatically: KSaneWidget puts every option it does not
handle itself onto a *Scanner Specific Options* tab, using the SANE
descriptor's title, description and range — which the patch provides.

The magazine controls (`Load film`, `Eject film`, and the read-only
`magazine` status) land on that same tab. **They render awkwardly:** KSane
draws the status as a wide field and truncates its text, and the layout of
the two buttons looks odd. This is cosmetic — the controls work — and is a
known issue (see the roadmap's "Known issues"); a fix needs a digiKam session
to verify.

**Things that can ask for something the backend will not do:**

* **Preview is a real scan.** KSaneCore's preview sets `tl-*`/`br-*` to the
  extremes, drops `depth` to 8 and the resolution towards 50 dpi, then runs a
  normal `sane_start`. Our constraints round those back (depth → 16, 50 dpi →
  600 dpi), so it does not fail — it performs a full 600 dpi transport pass of
  the selected frame, lamp, calibration, PARK and all. There is no cheap
  preview on this unit. **Do not press Preview** in the WP-2 session; scan
  directly.
* **The scan-area rectangle does nothing.** The backend scans the captured
  frame whatever window the frontend asks for (`calculate_scan_session` pins
  the geometry), so dragging a selection changes neither the image nor the
  time it takes.
* **Nothing scans by itself.** `openDevice()` is `sane_open()` plus an option
  read; no scan is started by opening the device or by changing an option.
  Batch mode and "wait for external button" exist in KSaneCore but are off
  unless switched on — leave them off.
* **Cancel does not park — by design — and once a scan has been started it
  costs a power cycle.** Two situations, and they are not the same:

  1. *Closing the dialog without having started a scan.* This is the only
     write-free case: `sane_open` writes nothing on GL126 (Test 46/47) and
     `sane_close` is gated to write nothing either, so the device is left as
     it was found.
  2. *Anything after pressing Scan.* By then the backend has already written
     to the scanner: `sane_start` runs offset, gain and shading calibration
     (hooks 2–4) before the frame is positioned and the pass begins. A
     cancel, an error, or a frontend crash anywhere in there leaves the
     hardware in a state the driver does not try to reason about. PARK is
     deliberately NOT run (`ParkDecision::AbortedPass` raises an error naming
     bytes read of bytes expected; `NoPass` means only that the scan pass was
     never armed — it does **not** mean the session wrote nothing). Recovery
     is the standing rule in `docs/hardware-safety.md`: power off, power on,
     read-only status check, `of135i load`, start over. No blind retry.
* **Bit depth has to be checked on the saved file.** Pillow — which
  `sane_coverage.py` and the preview-positive command use — returns 8-bit
  RGB for a 16-bit RGB PNG or TIFF, and a header saying `bit_depth 16`
  cannot distinguish real 16-bit data from 8-bit data widened to 16. Use
  `tools/image_probe.py`, which parses PNG itself and reads TIFF through
  `tifffile`, so 16-bit RGB survives:

  ```
  .venv/bin/python tools/image_probe.py FILE \
      --expect 3762x5335 --expect-channels 3 --expect-bits 16 \
      --min-low-byte-nonzero 0.5
  ```

  `low_byte_nonzero` is the fraction of samples whose low byte is not zero.
  A real scan sits near 1.0 (this repo's own 16-bit TIFF measures 0.9987);
  8-bit data widened to 16 gives exactly 0.0. The Pillow path stays where it
  is for coverage and preview images — it is adequate there, because the
  aperture edge is found from a relative step in a row-mean profile — and
  the limitation is now asserted by a test rather than assumed
  (`tests/test_image_probe.py`).
* A harmless log line at `sane_close`: *Cannot open calibration for writing*.
  GL126 never restores a calibration cache (the patch gates the restore off),
  so nothing depends on it. `mkdir -p ~/.sane` silences it.

## 7. Who does what: load, scan, eject

**The backend handles the magazine itself.** `load_document()` /
`eject_document()` are implemented behind a `load-film` / `eject-film` /
`magazine` option set (WP-4, `docs/sane-wp4-magazine.md`), and the whole
load → scan → eject cycle has run on hardware from `scanimage` and from
digiKam with no command-line step, including freeing a latched magazine
after a power cycle (Tests 75–77, 2026-09-13). The two-step protocol:
press **Load film**, take the magazine out when told and re-seat it to
the stop, then start the scan — the load runs before calibration.
**Eject film** ejects from the post-PARK state.

The command-line division below still works and was the B1 workflow
(Christian's decision of 2026-09-13; it does not satisfy B2, which is why
WP-4 exists). Use it when you prefer the driver's interactive load tool:

| step | who | command |
|---|---|---|
| power on, load the magazine | the Python driver, in a real terminal | `of135i status && of135i load` |
| position, calibrate, scan one frame, PARK | SANE — `scanimage` or digiKam | see §6 |
| eject | the Python driver | `of135i eject` (from the post-PARK state) |

The two sides exclude each other through one `flock` on
`/tmp/of135i-07b3-1436.lock`: `sane_open` takes it, `sane_close` releases it
(reference-counted, with scope guards). Practical consequence: **digiKam holds
the device open for as long as its scanner dialog is open**, so `of135i eject`
will report the scanner busy until that dialog is closed. Close the dialog
first, then eject.

## 8. Uninstall / restore

```
sudo tools/sane_install.sh uninstall
```

It does not blindly put back what it once recorded — it looks at the link's
current state first:

| what it finds | what it does |
|---|---|
| `libsane-genesys.so.1` points at our library, recorded target still present | restores that target, removes our library |
| points at ours, but the recorded target is **gone** (a package update replaced it) | follows the one distribution library that is there, so the link never dangles |
| points at ours, recorded target gone and **several or no** distribution libraries present | **refuses, changes nothing**, and says to run `dnf reinstall sane-backends-drivers-scanners` first |
| already points at a distribution library (the update took the link back) | leaves the link alone and only removes our library |

A library is never removed while the live link still resolves to it. From
`genesys.conf` only our marked block is deleted: original blank lines stay,
and edits made after the install are kept — the pre-install backup is used
for comparison, never restored over the live file (it is deleted only when
the result is byte-identical to it).

**Caveat worth knowing:** an RPM update of `sane-backends-drivers-scanners`
recreates its own `libsane-genesys.so.1` symlink, silently pointing SANE back
at the distribution's genesys — at which point the 135i stops being found.
`tools/sane_install.sh status` says so in as many words (`(the
distribution's -- our backend is NOT in use)`, and it flags a recorded target
that has disappeared), and re-running `install` fixes it. This is the price
of not overwriting packaged files, and it is the right trade.

## 9. Verified offline, and what is left

First written 2026-09-13; revised the same day after external review (Astra)
found three real defects in the first installer — see the log below.

Verified:

* the load mechanism read out of `backend/dll.c` (absolute path, `LIBDIR`),
  which is what rules out the alternatives in §1;
* Fedora's `libsane.so.1` `dlopen()`s our 27 MB build and resolves every
  `sane_genesys_*` op — run through a staging directory with **every** `usb`
  line disabled, so no device was attached and the scanner was never
  addressed;
* the installer against a synthetic root mirroring Fedora's layout
  (`tests/test_sane_install.py`, 19 tests): install adds without overwriting,
  is idempotent, refuses an incomplete clone, and **rolls back completely if
  a step after the symlink change fails**; uninstall restores byte for byte,
  follows a package update to the new library instead of leaving a dangling
  link, leaves a reinstated distribution link alone, refuses an ambiguous or
  missing restore target without changing anything, keeps original blank
  lines and later edits in `genesys.conf`, and leaves a USB id it did not add;
* `verify`'s failure paths, against a stub `scanimage` (no enumeration):
  wrong library → exit 2, library right but no device → exit 1, `scanimage`
  failing → exit 2, a leftover `LD_LIBRARY_PATH` → exit 2;
* 16-bit preservation can be checked on a real file: `tools/image_probe.py`
  round-trips 16-bit RGB through all five PNG row filters and through TIFF,
  and catches 8-bit data widened to 16 bits, which the header cannot
  (`tests/test_image_probe.py`, 6 tests);
* the clone's `git diff` is byte-identical to `sane/gl126-integration.patch`,
  and the built library carries 612 gl126 symbols (sha256 `1062ed01…`, the
  build Tests 71–73 ran);
* 286 offline tests green (`tools/release_check.py`), including the four C++
  probes that link this build (`test_sane_ops`, `test_sane_geometry`,
  `test_sane_open_params`, `test_sane_calibration_cache`) — none skipped;
* the digiKam path and its limits in §6, from the KSaneCore/libksane sources.

**Review log, 2026-09-13.** The first version of `tools/sane_install.sh` had
three defects, all reproduced in staging and all fixed above, with a test
each: `uninstall` restored a recorded link target that a package update had
removed, producing a dangling link and reporting success; a failure at the
config step left the library installed and the symlink repointed; and
`uninstall` stripped `genesys.conf`'s original trailing blank lines along
with its own block. `verify` also hid every failure behind `|| true`, and
the PNG test in `tests/test_aperture_crop.py` was described as if it proved
bit-depth preservation, which it does not (it is 8-bit by construction; its
subject is the coverage verdict).

**Done on hardware 2026-09-13 (Test 74).** Installed normally; `verify` exit
0 naming `/usr/lib64/sane/libsane-genesys.so.1`; one plain3600 frame-1 scan
through the installed `scanimage` (FEEDL 6562, 233 chunks, full transfer,
PARK, coverage 0.455/0.956 mm) and one from digiKam on the same load
(identical geometry, coverage 0.486/0.935 mm), with the serving library
proved from `/proc/<pid>/maps`; digiKam's saved PNG probed at 3762 × 5335, 3
channels, 16 bits per channel, `low_byte_nonzero` 0.996; eject from the CLI;
and Christian's eye acceptance for geometry and integrity (colour explicitly
not judged). The one defect this found — the missing `--sysconfdir=/etc` —
is fixed and guarded above.
