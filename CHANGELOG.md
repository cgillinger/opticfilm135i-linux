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
- SANE backend: the same next-strip load after `eject-film` — in progress,
  offline (Test 90 pending).
- Cold start shortened: the 15 s initial wait that could never succeed is
  gone (Test 78); the nine motor completions of the cold start fail closed
  in both the Python driver and the backend.
- SANE backend drives the magazine itself (WP-4): `load-film` /
  `eject-film` buttons and a `magazine` status line; a whole
  load → scan → eject cycle from scanimage and digiKam, including a
  power-cycled, latched magazine (Tests 75–77).

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
