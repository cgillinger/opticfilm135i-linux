# WP-2 — install & frontend: minimal hardware plan

Written offline 2026-09-13 at HEAD `aed052a` (+ the WP-2 offline commit).
**Do not run until Christian approves it.** It closes three things and
nothing else:

1. the backend, **installed normally** (no `LD_LIBRARY_PATH`, no private
   `SANE_CONFIG_DIR`), is the library `scanimage` and digiKam actually load;
2. one plain3600 frame-1 scan through the installed `scanimage`;
3. one plain3600 frame-1 scan **from inside digiKam**, whose image also
   supplies plain3600's missing formal eye acceptance (ROADMAP profile
   matrix).

Install mechanics, the digiKam controls, and the limits of that path are in
`docs/sane-install.md`. Not in scope: other profiles (WP-1 is done), IR,
frames 2–6, `load_document`/`eject_document`, lateral overscan, colour
rendering, WP-3.

Estimated hands-on time: ~20 minutes, one magazine load, two scans.

## 0. Environment lock before the first motor write (mandatory, logged)

1. `git rev-parse HEAD` == the approved WP-2 commit; `git status` clean.
2. `.venv/bin/python tools/gen_sane_tables.py --check` → "up to date".
3. `.venv/bin/python tools/release_check.py` → 267 tests, all OK.
4. `make -j8 -C ~/Dokument/Github/sane-backends/backend libsane-genesys.la`
   → links, no warnings from our files.
5. `tools/sane_install.sh status` → nine sources present, built library
   `sha256 1062ed01…`, 612 gl126 symbols.
6. **Unset any development environment**: `LD_LIBRARY_PATH` and
   `SANE_CONFIG_DIR` must be empty in every terminal used from here on
   (`env | grep -E 'LD_LIBRARY_PATH|SANE_CONFIG'` → no output). A leftover
   from an earlier session would silently prove nothing.

## 1. Install (root; Howdy will ask for your face)

```
sudo tools/sane_install.sh install
tools/sane_install.sh status
```

Expected after install: `genesys.so.1 -> libsane-genesys-gl126.so.1.4.0`,
`installed lib … identical to the current build`, `135i USB id present`.
Stop if any line differs. Nothing has touched the scanner yet.

## 2. Preconditions (hardware protocol, unchanged)

- Scanner healthy, any VM detached (`lsusb | grep 07b3`,
  `.venv/bin/python -m of135i status` → reg 0x01 = 0x22).
- **Seat the strip straight** against the stop — a skewed strip fails the
  coverage check on the outer frames (2026-09-11). Frame 1 is the least
  sensitive, but seat it properly anyway.
- Power-cycle, then in a **real terminal window** (the load prompt needs a
  tty): `.venv/bin/python -m of135i status && .venv/bin/python -m of135i load`
- Low USB debug — `SANE_DEBUG_GENESYS=4`. **Never 255**: it hexdumps the
  image, writes hundreds of MB and falsifies every timing.
- Listen. Any scraping → cut power immediately.
- Both scans below run on the **same load**: same profile, same frame, no
  power-cycle and no eject between them. That sequence is covered by
  existing evidence — Test 64 ran two consecutive `scanimage` sessions on one
  load without a power-cycle, and Test 62 ran plain3600 frames 1–6 on one
  load. A DPI change would require a new load; we do not change DPI.

## 3. Which library is serving (before any scan)

```
tools/sane_install.sh verify
```

Accept only if the `dlopen()`ing line names
`/usr/lib64/sane/libsane-genesys.so.1` and the device is listed as
`genesys:libusb:BBB:DDD  PLUSTEK OpticFilm 135i`. Note the **fresh** device
string — it changes on every re-enumeration; never reuse an old one.

## 4. Scan A — plain3600 frame 1 through the installed `scanimage`

```
OUT=~/Dokument/plustek-135i-analys/wp2-20260913; mkdir -p $OUT
SANE_DEBUG_GENESYS=4 scanimage -d genesys:libusb:<fresh> \
    --mode Color --resolution 3600 --frame 1 --format pnm \
    -o $OUT/scanimage-plain3600-f1.pnm 2> $OUT/scanimage.log
```

No `--force-calibration`: GL126 always calibrates since the Test 64 fix, and
an ordinary scan is what a user runs. `--mode Color` is required — the
genesys default is Gray, and the coverage tool needs RGB.

Expected, from the A+C ledger (`sane/gl126_tables.cpp`, plain3600 frame 1):

| FEEDL | line register | raw lines | delivered | chunks | end_hwdpi | POSITION observed | hard timeout |
|---|---|---|---|---|---|---|---|
| 6562 | 5367 | 5359 | 5335 | 233 | 11897 | ~1.8 s | 4.8 s |

Delivered image 3762 × 5335, RGB16. Full transfer 233/233, semantic PARK
normal, **no eject**.

Host check:
`.venv/bin/python tools/sane_coverage.py $OUT/scanimage-plain3600-f1.pnm --dpi 3600`
→ `verified=True`, both margins ≥ 0.15 mm (expect ~0.5–1.0 mm).

## 5. Scan B — the same scan from inside digiKam

Close any running digiKam first (a second invocation would just raise the
existing window and log nothing), then start it from a terminal so its SANE
output is captured:

```
pgrep -a digikam          # expect no output
SANE_DEBUG_GENESYS=4 digikam 2> $OUT/digikam.log
```

If this is digiKam's first run it opens a first-time configuration wizard;
click through it before going to the scanner — do that while the scanner is
still idle, not mid-session.

In digiKam: **Import → Import from Scanner** → pick the row
`PLUSTEK : OpticFilm 135i`. Then, per `docs/sane-install.md` §6:

- Basic Options: Source `Transparency Adapter`, Mode **`Color`**,
  Bit depth 16, Resolution **3600**.
- Scanner Specific Options tab: **Frame = 1**.
- Batch mode off. **Do not press Preview** — on this unit a preview is a
  full 600 dpi transport pass, not a cheap one.
- **Do not press Cancel once the scan is running.** An aborted pass is never
  parked (by design): the backend raises an error, writes nothing, and the
  session ends in a state that needs a power cycle plus a fresh
  `of135i load`. Let the scan finish.
- Press **Scan**, and save as **PNG** or **TIFF** (both lossless) wherever
  the dialog offers — digiKam saves into an album, so the exact path is its
  choice, not ours. Afterwards copy the file **unmodified** to
  `$OUT/digikam-plain3600-f1.png` and keep digiKam's own copy too.

Expected: the same geometry as scan A in `digikam.log` (FEEDL 6562, 233
chunks, 3762 × 5335 delivered), PARK normal, and a 16-bit image in digiKam.

Then **close the scanner dialog** before ejecting: digiKam holds the device
open, and the shared process lock will otherwise refuse the eject.

## 6. Eject, preserve, review

```
.venv/bin/python -m of135i eject          # from post-PARK, as always
```

Preserve, unmodified, in `$OUT`: both originals
(`scanimage-plain3600-f1.pnm`, `digikam-plain3600-f1.png`), both logs,
`tools/sane_install.sh status` output, and the `verify` output from §3.

Render preview positives for the eye check — the digiKam image, which is the
one being judged, and the `scanimage` PNM beside it as the 16-bit reference
(Pillow hands back 8-bit RGB for a 16-bit RGB TIFF, verified here, and is
expected to do the same for a 48-bit PNG; the PNM path keeps all 16 bits).
Memory discipline: one bounded process per image — `to_positive` makes
float64 whole-frame copies, which is what killed two sessions in September:

```
mkdir -p ~/Bilder/opticfilm-granskning/wp2-20260913
systemd-run --user --scope -p MemoryMax=3G .venv/bin/python -c '
import sys, importlib.util, numpy as np
from PIL import Image
from of135i.image import to_positive
spec = importlib.util.spec_from_file_location("sc", "tools/sane_coverage.py")
sc = importlib.util.module_from_spec(spec); spec.loader.exec_module(sc)
img = sc.read_image(sys.argv[1])
Image.fromarray((to_positive(img) >> 8).astype("uint8")).save(sys.argv[2])
' $OUT/digikam-plain3600-f1.png \
  ~/Bilder/opticfilm-granskning/wp2-20260913/digikam-plain3600-f1-positive.png
```

Then the same command once more with
`$OUT/scanimage-plain3600-f1.pnm` → `…/scanimage-plain3600-f1-positive.png`.
The two should look the same; a visible difference between them is a finding
about the digiKam path and is logged, not smoothed over.

**Christian's eye acceptance** on that positive, against the standing
checklist (ROADMAP, "How done is measured"): colour planes line up, the whole
frame including both edges is present, no banding, compared against the
driver's own plain3600 image of the same strip. This is the production image
for plain3600 — a pass fills the last cell of the profile matrix; a fail
stops WP-2 for plain3600 and is logged as-is.

## 7. Acceptance

WP-2 is done when **all** of these hold:

- `verify` named `/usr/lib64/sane/libsane-genesys.so.1`, with no
  `LD_LIBRARY_PATH`/`SANE_CONFIG_DIR` set, and `status` said the installed
  file is identical to the build;
- scan A: ledger geometry exactly, full transfer, normal PARK, coverage
  verified;
- scan B: delivered from digiKam with the same geometry, image saved
  losslessly;
- normal eject from post-PARK;
- Christian's eye acceptance of the digiKam positive.

Anything less is written down as what it is. **B1 is not marked complete on
this plan alone** — see the open question in §9.

## 8. Stop rules and recovery (existing rules, nothing new)

The governing text is `docs/hardware-safety.md` — "Recovery after an
interrupted scan" (power off, power on, start from a known state; restarting
the program is not recovery) and "Cross-process exclusion" (the shared
`flock`, which is why digiKam's dialog must be closed before `of135i eject`).

No deliberate interruptions, no cancel experiments, no recovery attempts are
part of this plan. On any anomaly — wrong FEEDL/chunk count, POSITION past
the hard timeout, short transfer, unexpected eject, PARK error, unusual
sound, or a frontend error mid-pass:

1. **Stop.** No blind retry, no second attempt at the same step.
2. No PARK after an incomplete transfer (the ScanPass machine already
   enforces this; do not work around it).
3. Power-cycle, then a **read-only** state check (`of135i status`,
   `lsusb | grep 07b3`). No motor command from an undefined state.
4. Preserve every log and partial file; log the anomaly in
   `docs/test-log.md` before anything else is run.
5. `load --double-jog` → eject only under its already-documented
   precondition (power-cycled, latched magazine in the well) — not as a
   general recovery.

If the install itself misbehaves at any point:
`sudo tools/sane_install.sh uninstall` restores the distribution's backend.

## 9. Open question for Christian — does this close B1?

B1's definition says the backend "performs the agreed workflow via
`scanimage` and a SANE frontend (digiKam): **load**, scan a frame, deliver
the image". This plan delivers scan and deliver from both frontends. It does
**not** deliver *load* from a frontend, and cannot: `load_document()` and
`eject_document()` are declared but throw `SANE_STATUS_UNSUPPORTED` — never
brought up on the C++ side. The magazine is loaded and ejected with
`of135i load` / `of135i eject` (`docs/sane-install.md` §7).

Two readings, Christian's call — the definition is not being edited here:

- **(a) The division of labour is the workflow.** "Load" names a step of the
  workflow, not a frontend feature; the CLI owns the magazine, SANE owns the
  scan. Then this plan closes B1, and the documentation states plainly that
  magazine handling is a CLI step.
- **(b) The frontend must do it all.** Then B1 has a remaining item:
  bring up `load_document`/`eject_document` in the backend — new hardware
  bring-up of the load flow from C++, with its own plan, and related to the
  parked "power-cycled + latched magazine" requirement.

Recommendation: **(a)**, documented explicitly, with (b) recorded as a
separate wish. The load flow needs an operator at the machine anyway (the
strip is taken out and re-seated by hand mid-sequence), so a frontend button
could not make it unattended — and the shared process lock means the CLI and
the frontend already cooperate cleanly.
