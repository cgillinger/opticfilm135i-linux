# Changelog

Notable changes to the Plustek OpticFilm 135i Linux driver and its SANE
backend port. Entries name the hardware evidence (docs/test-log.md) where
one exists; "offline" means implemented and tested without the scanner.

## v0.2.1 — 2026-09-29
The state submitted to the SANE project; no functional change. See [docs/release-notes-v0.2.1.md](docs/release-notes-v0.2.1.md).

- The SANE backend was submitted to the SANE project on 2026-09-29 as
  merge request !1032
  (<https://gitlab.com/sane-project/backends/-/merge_requests/1032>),
  by the owner, from his own fork. It is open and under review, not
  merged.
- Dead code removed from `sane/gl126.cpp` (42 lines: an uncalled helper
  marked with the C++17 `[[maybe_unused]]` attribute and two unused
  constants) after the upstream project's clang CI job rejected it under
  `-Werror`; the submission package was re-exported as v5. The compiled
  code is identical (Test 95), and all seven upstream CI jobs pass.

## v0.2.0 — 2026-09-28
The SANE backend, one-button loading, next-strip loading, 110 film. See [docs/release-notes-v0.2.0.md](docs/release-notes-v0.2.0.md).

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
  README gained a matching six-line "digiKam cheat sheet". Seen live in
  Test 92.
- **One-button loading verified on hardware** (2026-09-28, Test 92): from
  digiKam and from `scanimage`, five loads on one power-on (one cold with
  jog, four without), feed and traverse on the first poll every time; the
  120 s timeout and the two refusals (Scan without a load, Load film while
  loaded) caught read-only; Check status correct after every step; the
  `ejected` / `loaded` marks carried the state across processes. The
  frontend limits of the design (instruction visible only for an instant,
  button errors invisible in digiKam, digiKam's own text for the NO_DOCS
  refusal) were all seen live.
- **One-button loading (WP-5)** (offline, 2026-09-27, following a live
  session that passed on the mechanics but whose operator still needed
  outside guidance, and a defect the same session hit for real: a second
  `Load film` press re-jogged and un-seated an already-released magazine,
  failing the following scan at the feed): `Load film` now runs the WHOLE
  flow in one press -- release (skipped when nothing needs releasing), a
  read-only wait (up to 120 s) for the loader sensor's present-clear-
  present edge, then the load. `Scan` never loads the magazine any more
  (`load_document()` is a pure checker of the in-process state and the
  on-disk mark); a new `Check status` button reads the hardware and
  reconciles the status line, without ever claiming Loaded on hardware
  evidence alone. A third mark kind, `loaded`, lets `scanimage -n
  --load-film` hand off to a separate scanning invocation. Status line
  values grew from seven to twelve (`docs/sane-wp5-load-button.md` §3.6).
  Design and implementation notes: `docs/sane-wp5-load-button.md`.
  Hardware-verified the next day (Test 92, above).
- **WP-5 review round two** (offline, 2026-09-27, later the same day --
  an independent reviewer plus the coordinator, nine findings): an
  Ejected-origin retry could lose its no-jog/lenient-regs treatment the
  moment a timeout made it look like an ordinary Released retry, and
  would then have refused Failed on real post-scan register values for a
  magazine that was never jogged loose; a magazine failure now WRITES a
  fourth mark kind (`failed`) instead of clearing the pending one, so a
  second process knows the transport's state was never established; a
  process killed while Load film waits now always leaves a mark that
  blocks the next scan; Check status no longer reports "unknown state --
  power-cycle" for a magazine that is actually loaded (reg 0x101 is not
  idle-class-shaped right after LOAD or during calibration); a
  cross-process `loaded` mark now blocks a second Load film press the
  same way the in-process state does; a cold reg 0x01 read inside one
  process now resets a stale Loaded/Failed claim instead of refusing
  forever; the sensor-clear debounce needs 2 consecutive reads, not one.
  Full list and rationale: `docs/sane-wp5-load-button.md` §9.5. 389
  offline tests pass (was 380). Hardware-verified the next day (Test
  92, above).

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
- WP-3 package re-exported 2026-09-28 (v3, `sane/wp3-package/`): rebased
  onto upstream `f8b5e16` and refreshed with everything since 2026-09-15
  (strict cold-start completions, the post-eject precondition fix, the
  dialog cleanup, English-only strings, WP-5 one-button loading); builds
  standalone with zero warnings; recreated identically from the bundle and
  from the patches. Still prepared only, nothing sent. The SANE
  project's `tstbackend -l 1` run against that build (Test 93): 22 965
  checks, 0 warnings, 0 errors, zero writes to the scanner.
- **Source comments cleaned for submission** (2026-09-28 evening, v4 of
  the package): every comment and log message in the nine GL126 files,
  the generated tables and the shared genesys hunks that cited the
  project's private documents, test numbers, work packages, review rounds,
  reviewers or dates now states the fact without the citation; one
  pointer to the public protocol documentation added to the headers.
  Compiled code identical (comment-stripped comparison of all 21 files);
  389 offline tests; `tstbackend -l 1` repeated against v4 (Test 94).

## v0.1.2 — 2026-09-12
IR channel alignment fix. See [docs/release-notes-v0.1.2.md](docs/release-notes-v0.1.2.md).

## v0.1.1 — 2026-09-06
Early driver release. See [docs/release-notes-v0.1.1.md](docs/release-notes-v0.1.1.md).

## v0.1.0 — 2026-09-06
First tag.
