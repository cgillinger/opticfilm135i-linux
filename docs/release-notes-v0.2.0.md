# v0.2.0 — the SANE backend, one-button loading, next-strip loading, 110 film

Release notes for the `v0.2.0` tag (2026-09-28) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.1.2](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.0/docs/release-notes-v0.1.2.md).

## What this release is

v0.1.x covered the Python / pyusb command-line driver only. v0.2.0 is the
first release whose scope also includes the **SANE genesys backend port**
(`sane/`): it installs as an ordinary genesys build, and the whole load,
scan and eject cycle runs from `scanimage` and from digiKam with no
command-line step. Both parts are hardware-verified on the one unit; the
verification status of each is stated separately below.

## SANE backend

**Every profile scans from a SANE frontend.** 600, 1200, 2400, 3600 and
7200 dpi and the infrared pass, from the installed `scanimage` and from
inside digiKam. Install and the digiKam walkthrough:
[docs/sane-install.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.0/docs/sane-install.md).

**The backend handles the magazine itself, with one button.** `Load film`
releases the magazine, waits (read-only, up to 120 s) for the loader
sensor to see it taken out and pushed back in to the stop, and loads it.
`Scan` never loads: with nothing loaded it refuses after one register read.
`Eject film` ejects, `Check status` re-reads the hardware, and the
`Magazine -- next step` status line tells the operator what to do. The one
rule an operator needs: press Load film first, then take the magazine out
and push it in. Next strip: Eject film, then Load film again, swap, push
in; no jog, no extra reseat. Verified on hardware from digiKam and from
`scanimage` (five loads on one power-on, the timeout and both refusals
exercised, state carried across processes;
[docs/sane-wp5-load-button.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.0/docs/sane-wp5-load-button.md),
test log Test 92). A power-cycled unit with a latched magazine is a
supported start (Tests 75–77).

**The digiKam dialog shows only what the scanner does.** Genesys options
that never had an effect on this chip are hidden; the film controls sit in
their own group; the default mode is Color. Two libksane display bugs are
worked around backend-side (English-only status values, ascending
resolution list). What a SANE frontend cannot do is documented rather than
papered over: there are no dialogs, so the status line and the button
tooltip are the whole interface, and the dialog is unresponsive while
Load film waits.

**Submission to the SANE project: prepared, not sent.** The exact
five-commit series against current upstream is in
[sane/wp3-package/](https://github.com/cgillinger/opticfilm135i-linux/tree/v0.2.0/sane/wp3-package)
(bundle and patches, recreated identically both ways), builds standalone
with zero warnings, and passes the SANE project's own `tstbackend -l 1`
(22 965 checks, 0 warnings, 0 errors, no writes to the device; Test 93).
Whether and when it is submitted is the author's decision; nothing has been
sent.

## Command-line driver

- **`of135i load --next-strip`**: after an eject in the same power-on, swap
  the strip, push the magazine in to the stop and run the command. No jog,
  no remove-and-reseat. Matches the vendor application's own behaviour
  (captured), hardware-verified (Test 89). Refused on a cold scanner.
- **110 (Pocket Instamatic) film in the 35 mm strip holder**: `scan --film
  110 --placement A|B`, a perforation-anchored frame detector, numbering by
  position on the strip, and an independent edge check
  (`tools/film110_check.py`). Two strips verified (Tests 86–87);
  [docs/film-110.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.0/docs/film-110.md).
- **Cold start shortened**: the 15 s initial wait that could never succeed
  is gone; the nine motor completions of the cold start fail closed in both
  the driver and the backend.
- **Semantic PARK is the default**, and a per-session calibration cache
  skips repeated calibration on the plain 3600 dpi profile.
- Mounted-slide holder characterised (empty holder, then a real slide at
  600 and 3600 dpi); slide support itself is not in this release.
- The colour cast question is closed by measurement: it varies between
  strips and shows in the vendor path too. The driver keeps delivering raw
  negatives; no colour processing was added
  ([docs/colour-rendering-analysis.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.0/docs/colour-rendering-analysis.md)).

## Project

- Multi-licensed: driver GPL-2.0-or-later or MIT, documentation CC BY 4.0.
  CONTRIBUTING and a pull-request template.
- Hardware-free CI: the offline checks (`tools/release_check.py`, 389
  tests at this tag, 0 skipped) run without the scanner.
- SANE lock and magazine-mark files hardened against hard links and FIFOs.

## Verification and limits

- One unit, one developer. Nothing is verified across units; the
  calibration is measured at run time on every scan to compensate.
- Every hardware claim above has a numbered entry in
  [docs/test-log.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.0/docs/test-log.md)
  (Tests 62–93 for the backend and the magazine flow).
- Not run, deliberately: `scanimage -T` and `tstbackend -l 2` and up. They
  cancel a scan mid-pass, which on this unit needs a power cycle afterwards.
- Interrupting a scan mid-pass leaves the transport unparked; recovery is
  a power cycle and Load film. There is no automatic recovery, by design.

## Changes since v0.1.2

The full list is the `v0.2.0` section of
[CHANGELOG.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.0/CHANGELOG.md).
