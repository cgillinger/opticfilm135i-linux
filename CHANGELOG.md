# Changelog

Notable changes to the Plustek OpticFilm 135i Linux driver and its SANE
backend port. Entries name the hardware evidence (docs/test-log.md) where
one exists; "offline" means implemented and tested without the scanner.

## Unreleased (master since v0.1.2)

### Magazine handling
- **`of135i load --next-strip`** (2026-09-25): load the next strip after an
  eject in the same power-on with no jog and no remove-and-reseat step —
  swap the strip, push the magazine in to the stop, run the command. Vendor
  capture (Test 88) showed the vendor app doing exactly this; the driver
  path was hardware-verified the same day (Test 89: feed and traverse
  complete on the first poll, next frame positions identically). Saves
  three motor moves and one reseat per strip. Refused on a cold scanner.
- SANE backend: the same next-strip load happens automatically on the next
  scan after `eject-film` (swap the strip, push it in, scan); the magazine
  mark now records released vs ejected so it works across scanimage calls.
  Hardware-verified from scanimage 2026-09-27 (Test 90): open + load,
  no jog, feed and traverse on the first poll, the frame positioned like
  the one after a full load. The first run found and fixed a backend
  bug: the post-eject precondition required regs 0x3b/0x3c = 0x00/0x00
  (what the jog leaves), but an eject leaves the last scan profile's
  values, so every next-strip load after a scan would have been refused;
  now only the base-table 0xff/0xff is refused.
- Cold start shortened: the 15 s initial wait that could never succeed is
  gone (Test 78); the nine motor completions of the cold start fail closed
  in both the Python driver and the backend.
- SANE backend drives the magazine itself (WP-4): `load-film` /
  `eject-film` buttons and a `magazine` status line; a whole
  load → scan → eject cycle from scanimage and digiKam, including a
  power-cycled, latched magazine (Tests 75–77).

### SANE frontend surface
- **digiKam/KSane dialog cleanup** (offline, 2026-09-27, following the
  owner's report that the dialog was too cluttered to start a scan from):
  every genesys option that never had an effect on GL126 (exposure time,
  brightness/contrast, lamp timing, the whole calibration-cache family,
  colour filter) is now hidden; the magazine/frame controls sit in their
  own `Film` group between `Enhancement` and `Extras`, in the order
  `magazine`, `Load film`, `Eject film`, `Frame`; the backend's default
  mode is `Color` (colour filter defaults to `None`) instead of the
  generic Gray/Green combination this scanner refuses.
- **Two libksane display bugs found and worked around, both backend-side**
  (Test 91, 2026-09-27: a live session with a Swedish translation catalog
  installed failed at the feed): the seven `magazine` status values are
  now deliberately untranslated (plain English constants, `sane/gl126.cpp`)
  because KSaneWidgets' `LabeledCombo` matches a value-list option's
  internal value against a *translated* one, so a translated status never
  follows a backend-side change; and GL126's resolution word list is now
  ascending (600 first) in `genesys.cpp`'s `set_resolution_option_values`,
  because the same widget's constructor matches the bare default number
  against each item's unit-bearing text and always falls back to index 0.
  Following this, the backend's own strings are English-only by design —
  the Swedish catalog feature (`tools/sane_install.sh`, `po/sv.po`) that
  was added earlier the same evening was removed; other dialog strings
  still come from the distribution's own sane-backends catalog, so the
  dialog is mixed-language by design. See `docs/sane-install.md` §6 for
  the walkthrough and the mechanism, and `docs/ROADMAP.md` for what is
  still unverified in a live digiKam session.
- **Status line enabled, reworded to name "Scan"** (offline, 2026-09-27,
  following a second live digiKam session that passed on the mechanics
  but whose operator still needed outside guidance -- Test 91's second
  paragraph, owner's verdict: "no one can do this process without a
  written manual"): `magazine` gets `SANE_CAP_SOFT_SELECT` added
  (`SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT`) so KSaneWidgets renders
  its label and value enabled/black instead of the disabled grey it draws
  for a `SANE_CAP_SOFT_DETECT`-only option; its SET handler is a
  documented no-op (returns `SANE_INFO_RELOAD_OPTIONS`, changes nothing,
  a listed value is accepted and an unlisted one is rejected by SANE core
  before the handler runs). The option is retitled `Magazine -- next
  step`, its seven values now each name the frontend's own button
  ("... push in, Scan" instead of "... push in and scan"), and its
  tooltip (`desc`) spells out the whole load/scan/eject procedure. The
  README gained a matching six-line "digiKam cheat sheet". Not yet seen
  live.

### Film and holders
- **110 (Pocket Instamatic) film in the 35 mm strip holder**: `--film 110`
  with a perforation-anchored frame detector, the two-placement protocol
  (`--placement A|B`, numbering by position on the strip), an independent
  edge check (`tools/film110_check.py`). Two strips verified (Tests 86–87).
- Mounted-slide holder characterised (empty-holder capture, then a real
  slide at 600 and 3600 dpi, Tests 84–85); slide support itself is still
  unscheduled.

### Scanning defaults
- Semantic PARK is the default and a per-session calibration cache skips
  repeated calibration on the plain 3600 profile.

### Colour
- The colour cast question closed by measurement: it varies between strips
  and shows in the vendor path too; the driver keeps delivering raw
  negatives, no colour change (docs/colour-rendering-analysis.md).

### Project
- Multi-licensed: driver GPL or MIT, docs CC BY 4.0; CONTRIBUTING and a
  pull-request template; hardware-free CI (offline checks).
- `tools/release_check.py` reports PASS / PARTIAL / SKIP / FAIL truthfully.
- SANE lock and magazine-mark files hardened against hard links and FIFOs.
- WP-3 submission package prepared and rebased onto current upstream —
  prepared only, nothing sent.

## v0.1.2 — 2026-09-12
IR channel alignment fix. See [docs/release-notes-v0.1.2.md](docs/release-notes-v0.1.2.md).

## v0.1.1 — 2026-09-06
Early driver release. See [docs/release-notes-v0.1.1.md](docs/release-notes-v0.1.1.md).

## v0.1.0 — 2026-09-06
First tag.
