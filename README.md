# opticfilm135i-linux — Linux driver for the Plustek OpticFilm 135i film scanner

![License: GPL-2.0-or-later](https://img.shields.io/badge/License-GPL--2.0--or--later-blue.svg)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Status: working prototype](https://img.shields.io/badge/Status-working%20prototype-green.svg)

**Unofficial, community-built Linux driver for the Plustek OpticFilm 135i**
(USB `07b3:1436`, Genesys Logic GL126) — a 35 mm film scanner with motorized
batch feeding that officially supports only Windows and macOS. This project
makes it scan **natively on Linux**: no VM, no vendor driver, no SANE backend
required (yet).

Keywords: *Plustek OpticFilm 135i Linux driver, film scanner Linux, 35mm
negative scanner Linux, SANE OpticFilm 135i, GL126, pyusb scanner driver*.

## Project status

The driver scans on Linux today via its CLI (details below). The end goal
is a **SANE backend** so the scanner also works in standard SANE frontends
(`scanimage`, digiKam, …). How "done" is measured — by criteria with
functional thresholds, not test count — and the full plan: **[docs/ROADMAP.md](docs/ROADMAP.md)**.

- **M1 — Protocol reverse-engineered** ✅
- **M2 — Driver drives the hardware** ✅ (load, 1–4 batch, all DPI, IR + dust removal, eject)
- **M3 — Robustness and honest limits** ✅ (every acceptance-matrix row met;
  the residual-dark_b fix is hardware-verified — A10/Test 36 — positioning
  verified, cross-unit a documented limitation)
- **M4 — SANE backend** — not started yet (skeleton only)

See **[docs/ROADMAP.md](docs/ROADMAP.md)** for the acceptance matrix,
frozen scope, and exactly what remains before the driver is "complete".

Today you scan from the command line to raw 16-bit TIFF — including a
resumable **bulk-digitisation** workflow (`of135i digitize`) for working
through boxes of film strip by strip — and import the files into any tool
(including digiKam). Scanning *from inside* a SANE frontend needs M4. The
SANE backend can be **built and installed locally** — it does not depend on
the SANE project accepting it upstream; upstreaming is a separate, later
step for wider distribution.

## What works today

- Full scan flow over raw USB (pyusb): initialization, magazine handling,
  homing, per-frame positioning, **self-computed calibration** (AFE gain from
  live white measurements, AFE offset from a two-point dark bracket,
  two-stage per-pixel shading correction) and 3600 dpi 48-bit RGB
  scanning. The dark-bracket offset reproduces the vendor's codes on the
  reference unit, hardware-verified. On later frames of a batch the device
  can return a *residual* dark_b (a stale buffer, not a fresh measurement);
  the driver detects this and substitutes the session's healthy dark_b, or
  fails closed if none is available (docs/test-log.md Test 32–33).
- Color-line (staggered CCD) channel alignment — no RGB fringing.
- Output as 16-bit TIFF or PNM: the raw linear negative (the driver's
  actual product: calibrated, channel-aligned, unclipped), or a preview
  positive (`--positive`, sRGB-tagged) by per-frame density inversion
  with automatic black/white points per channel. The preview is a
  convenience; colour interpretation (inversion, mask, white balance,
  tone) belongs in the application — darktable's negadoctor, VueScan, a
  SANE frontend — working from the raw negative.
- IR scanning (`--ir`): dual-light alternating capture — visible image plus an
  IR channel where only dust and scratches are visible, with per-light
  two-stage shading calibration.
- Verified against hardware: calibration values reproduce the vendor
  driver's within ±1 gain code / 0.03 % shading gain on the same unit;
  rendered output judged on par with the vendor app.
- IR-based automatic dust/scratch removal (multi-scale inpainting), on by
  default with `--ir`.
- A udev rule for running without root (`udev/60-of135i.rules`).
- A fail-closed hardware-safety layer (`of135i/safety.py`) that refuses
  every writing operation unless the scanner is in a known start state
  — verified before the USB open sequence, so a refusal sends nothing
  at all, not even `SET_CONFIGURATION` — treats a short OUT transfer as
  an unknown hardware state, prevents two processes from driving the
  scanner at once (a Linux process lock), and keeps `doctor`/`status`
  strictly read-only. The guard has held on hardware through five
  failed magazine loads (it stopped each one before the next motor
  move) and every motor completion is verified against the vendor
  capture's state class. See
  [`docs/hardware-safety.md`](docs/hardware-safety.md).

## Development status

**Phase: working prototype. Magazine loading, single-frame and
whole-strip batch scanning, and eject are the verified paths.** Scan,
calibration, IR and dust removal are stable and hardware-verified, per
frame and across a 4-frame strip in one invocation, frame for frame
against the vendor application's output of the same strip (2026-09-05).
The rough edges you should know about:

- **Eject depends on how the magazine was loaded.** Load with
  `tools/load_magazine.py` (the vendor's insert flow) and `eject` /
  `--eject` works, before or after scanning. Ejecting from other
  transport states (e.g. after the older `--full` load flow) stalled
  the mechanism with the magazine stuck part-way; recovery was a power
  cycle plus an initialization with the vendor software.
- **Speed: not tuned yet.** The driver replays the vendor's complete
  captured command stream, including every status read and the
  captured pacing between commands, because slimmed-down variants
  produced wrong calibration (see docs/protocol-notes.md). A 3600 dpi
  frame therefore takes noticeably longer than in the vendor software.
  Slow and correct first. The stream is now classified op by op in
  [`docs/replay-analysis.md`](docs/replay-analysis.md) and the first
  phase (PARK) has a semantic implementation behind `--park semantic`
  (real read-modify-write and condition waits instead of captured
  pacing); it is off by default until it has been A/B-tested on
  hardware.
- **After a power-on, load first.** The driver detects a freshly
  power-cycled scanner (reg 0x01 = 0x00) and runs the vendor's cold-start
  homing sequence automatically inside the magazine load flow — no VM or
  vendor software needed. What it does not do is scan straight after
  that: the load's traverse is what puts the transport at the scan
  reference position, and a scan attempted right after the cold homing
  reads a dark white line (51 measurements over five minutes on
  2026-09-05, not a warming lamp — no light at the sensor). Earlier
  "flat images after a cold start" had the same cause. `scan` therefore
  refuses in a cold-started session until `tools/load_magazine.py` has
  run, exactly as the vendor application always loads after a power-on.
  The white-line check that caught this stays as a guard: a scan whose
  lamp never reaches a stable usable level fails instead of producing a
  flat image (`--warmup-budget`, 60 s by default).
- **After an interrupted scan, power-cycle before anything else.** A
  session that is aborted inside a phase (Ctrl-C, a crash, a USB
  timeout) can leave the scan engine running (reg 0x01 reads 0x23
  rather than the idle 0x22). Starting a new session on top of that
  state produced a motor event and a firmware hang on 2026-09-04; the
  scanner stayed on the bus but answered nothing until power was cut.
  Every command — not just `hwblock.py` — now refuses to write to a
  scanner that is not in a known start state (idle-homed `0x22`, or
  `0x00` for the cold-init path only): the state is read before the
  device is even configured, so a refusal means zero USB writes and
  zero state-changing requests, and no automatic recovery is ever
  attempted. A USB transfer that completes short is treated the same
  way as a failed one. The only fix is a physical power cycle;
  restarting the program is not sufficient. See
  [`docs/hardware-safety.md`](docs/hardware-safety.md).
- **The magazine must be loaded through the driver**
  (`tools/load_magazine.py`; the tool verifies each motor completion
  against the vendor capture's state class and loader-sensor bit and
  fails, requiring a power cycle, if the scanner answers anything else.
  The flow replays the vendor's own session from device open — its
  register table, app-start jog, a fresh insert to the stop by the
  operator, then the feed and traverse — and latched the magazine on
  hardware three times out of three on 2026-09-05, see the test log.
  The tool is interactive: run it in a terminal, it asks you to take
  the magazine out and reinsert it half-way through) — the
  autoloader is driver-managed and the
  hardware buttons are dead without a driver process. The loader sensor
  reports magazine *presence*, not that it is mechanically locked — see
  [`docs/hardware-safety.md`](docs/hardware-safety.md).
- `of135i status` reports the loader sensor (magazine present/absent)
  and button state.
- **Multiple resolutions:** `--dpi 600|1200|2400|3600|7200` — all five
  hardware-verified. All non-3600 resolutions are dual-light (IR +
  visible) passes, so `--ir` decides whether the IR channel is written
  out and used for dust removal. 7200 dpi produces a 10512 px wide,
  ~1.3 GB raw frame — expect a long scan and a lot of RAM.

## Requirements

- Linux, Python 3.10+
- `pyusb`, `numpy`, `pillow`
- The scanner connected via USB

## Install

```bash
git clone https://github.com/cgillinger/opticfilm135i-linux.git
cd opticfilm135i-linux
python3 -m venv .venv
.venv/bin/pip install pyusb numpy pillow
# USB access without root: install the udev rule, then re-plug or power-cycle the scanner
sudo cp udev/60-of135i.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger
```

Nothing below needs `sudo` once the udev rule is in place. Check the
install without touching the scanner's state:

```bash
.venv/bin/python -m of135i version          # driver version + git revision, no USB
.venv/bin/python -m of135i doctor           # read-only hardware report (scanner on)
```

## Usage — the normal workflow

The scanner is driver-managed: after every power-on the magazine must
be loaded through the driver before anything can be scanned, exactly as
the vendor application does it.

```bash
# 1. Load. Interactive: run it in a terminal window. It runs the
#    vendor's app-start jog, then asks you to take the magazine fully
#    OUT and reinsert it to the stop, then feeds and positions it.
.venv/bin/python -m of135i load

# 2. Check by hand: the magazine is latched (does not pull out) and the
#    button LED is blue. The driver cannot sense the latch.

# 3. Scan. Raw 16-bit linear negative (the driver's product) ...
.venv/bin/python -m of135i scan --frame 1 --ir -o frame1.tiff
#    ... or a preview positive, correctly oriented
.venv/bin/python -m of135i scan --frame 1 --ir --positive --rotate 90 -o frame1-positive.tiff
#    ... or the whole strip in one go (rulle-f1.tiff ... rulle-f4.tiff), ejecting at the end
.venv/bin/python -m of135i scan --frames 1-4 --ir --positive --rotate 90 --eject -o rulle.tiff
#    other resolutions
.venv/bin/python -m of135i scan --frame 1 --dpi 2400 --positive -o frame1-2400.tiff

# 4. Eject (if not done by --eject). The magazine then sits loose in the slot.
.venv/bin/python -m of135i eject
```

`--park semantic` on `scan` selects an experimental park phase (see
docs/replay-analysis.md); it is off by default and not hardware-verified.
`--warmup-budget SECONDS` bounds how long a scan waits for the lamp.

### Bulk digitisation (a box of film, strip by strip)

`of135i digitize` runs one strip end to end — load, scan frames 1–4,
eject — into a resumable staging tree, and records each strip in an
append-only manifest. Run it once per strip; the roll number advances
from the manifest **and** the roll directories already on disk (per
`--prefix`), so you can stop and pick up where you left off.

```bash
# Insert a strip, then:
.venv/bin/python -m of135i digitize --out ~/scans/boxA --prefix boxA-
# -> loads, scans, ejects, writes ~/scans/boxA/boxA-roll-001/f{1..4}.tiff
#    (+ -ir.tiff + .diag.json) and appends ~/scans/boxA/manifest.jsonl.
# Insert the next strip and run the same command again (roll-002, ...).
```

The main `fN.tiff` is always the **raw negative** (calibrated,
channel-aligned, and — with IR, unless `--no-clean` — dust-cleaned; not an
untouched sensor dump). Do colour (inversion, white balance, tone) later
in your editor. `--positive` writes a **separate** `fN-preview.tiff` and
leaves the negative untouched; `--roll N` targets a specific roll,
`--no-ir` drops the IR output (non-3600 profiles still capture dual-light).
Resume is **between strips**: the roll number advances past the highest
recorded or on-disk roll, an existing roll is not overwritten without
`--force` (which first clears that roll's previous `f*.tiff`/`.diag.json`
so the dir is not a mix of two runs), and an interrupted strip is recorded
failed and re-run whole
(there is no mid-strip resume). The manifest records per-frame calibration
and whether a residual dark_b was auto-corrected (docs/test-log.md
Test 32–33).

### What the driver knows about the magazine

| term | meaning | how it is known |
|---|---|---|
| **present** | something is in the slot | loader sensor (reg 0x101 bit 0x08), trusted before the first write of a session only |
| **load completed** | the feed pulled the cassette past the sensor and the traverse finished | the load flow's verified completions (f455, dc55) |
| **latched** | mechanically locked, blue LED | **only you can tell** — check by hand before scanning |
| **start state** | what the transport was doing when the session opened | reg 0x01: `0x22` idle-homed (normal), `0x00` cold (a load must follow), anything else refused |

### Other commands

```bash
.venv/bin/python -m of135i status                  # registers, loader sensor, button (read-only)
.venv/bin/python -m of135i doctor --json report.json   # full read-only health report
.venv/bin/python -m of135i watch                   # ejects on a hardware button press
```

`doctor` is strictly read-only on the wire (USB control reads and
descriptor queries only — no register writes, no motor commands, no
`initialize()`/`cold_init()`): it prints USB descriptor info, the
GL chip id, the derived engine state, a dump of every register the
vendor driver itself is observed reading, magazine/button status, and
host info. Every `scan` additionally writes a `<output>.diag.json`
sidecar per frame with the computed calibration values, phase timings,
lamp-warmup measurements and poll/mismatch counters — disable with
`--no-diag`.

### When something goes wrong

Every error message ends with the next safe action. The rules behind them:

- A refusal before the first write ("No commands were sent") means the
  scanner was not in a known start state, another process holds it, or
  an operation was asked for out of order (a scan before the load in a
  cold session). Nothing happened; fix the cause and run again.
- A failure after writes ("Power the scanner OFF …") means an operation
  stopped part-way: a motor completion that did not arrive, a USB error,
  Ctrl-C. The driver never sends recovery commands. Power-cycle, then
  start again with `load`.
- The scanner leaves the USB bus about five minutes after a session
  ends and does not come back by itself; a power cycle wakes it.

Exit codes: `0` success, `1` refused or failed (details on stderr),
`2` usage error, `130` interrupted (Ctrl-C).

### Release check

```bash
.venv/bin/python tools/release_check.py     # runs every offline test file, requires a clean checkout
```

### Autonomous hardware test block

```bash
# magazine loaded through `of135i load` and checked latched, scanner idle/homed
.venv/bin/python tools/hwblock.py warm --repeat 10 --skip-dpi-change --eject --out results/warm-01
```

(The former `cold` block is retired: a cold-started session loads
before it scans, so cold-start verification is `of135i load` followed
by the warm block.)

`tools/hwblock.py` runs a long, unattended block of already-verified
`scan`/`eject` operations (reproducibility, batch, DPI-change, and
cold-start checks) behind a single human confirmation, recording every
image, `.diag.json` sidecar, and a `summary.json`/`report.md` report —
and stops immediately on the first anomaly.

Troubleshooting: if the device is not found, check that nothing else
holds it (a virtual machine's USB passthrough, another scanning
application) and that it has not entered standby — the scanner drops
off the USB bus after ~5 minutes of inactivity and needs a physical
power cycle to reappear. Run the offline test suite
(no hardware needed):

```bash
.venv/bin/python tests/test_offline.py
.venv/bin/python tests/test_calibrate.py
```

## How it works

The GL126 speaks the Genesys vendor protocol over USB control transfers.
The protocol was reverse-engineered from USB captures of the Windows
driver and is documented in [`docs/protocol-notes.md`](docs/protocol-notes.md)
— transport layer, register semantics, motor/positioning model (relative
FEEDL stepping from the carriage's current position, 38.0 mm frame pitch), calibration data flow, image format
(pixel-interleaved RGB16, 3762 px/line @ 3600 dpi) and the two-stage
shading correction. [`docs/driver-design.md`](docs/driver-design.md)
describes the architecture; [`docs/cal-analysis.md`](docs/cal-analysis.md)
the calibration formula reverse-engineering; [`docs/replay-analysis.md`](docs/replay-analysis.md)
classifies the replayed command stream and sets the order for turning
it into semantic register sets and explicit waits.

The driver executes the captured command stream as data tables
(`of135i/tables.py`, regenerable via `tools/gen_tables.py`) with live
calibration values injected at well-defined points. This repository
contains **no vendor code or vendor configuration files** — only derived
interoperability constants and our own code.

## Roadmap

- [x] IR channel capture (`--ir`): separate visible + IR output, dual-light calibration
- [x] IR-based automatic dust/scratch removal (multi-scale inpainting, on by default with `--ir`)
- [x] Resolution profiles: all five DPIs (600/1200/2400/3600/7200) hardware-verified
- [x] Whole-strip batch scanning (`--frames 1-4`)
- [x] Eject from a loaded magazine, before or after scanning
- [x] Magazine loading through the driver (the vendor's insert flow replayed whole; latched 3/3, 2026-09-05)
- [x] Per-frame positioning wait scaled with the move length (frame 4 of a batch was scanned mid-move before; fixed and verified against the vendor's output, 2026-09-05)
- [x] Residual dark_b handled: on later batch frames the device can return a stale buffer instead of measuring; detected and the session's healthy dark_b substituted, else fail-closed (Test 32–33)
- [x] Bulk-digitisation workflow (`of135i digitize`): one strip per run into a resumable staging tree with an append-only manifest
- [ ] Speed tuning (trim the replayed command stream)
- [x] Cold-start initialization from a bare power-on
- [x] udev rule for rootless operation
- [x] Loader sensor and button event reading
- [x] ICC-tagged output (`--positive` TIFFs carry an sRGB profile; raw negatives are untagged linear data)
- [ ] SANE genesys backend support for GL126 (upstream goal) — plan and hook mapping in [`docs/sane-port.md`](docs/sane-port.md); skeleton in progress

## Status & disclaimer

Working prototype, developed and tested against a single OpticFilm 135i
unit. This project is not affiliated with, endorsed by, or supported by
Plustek Inc. Use at your own risk. Plustek and OpticFilm are trademarks
of their respective owner; they are used here only to identify the
hardware this software interoperates with.

## License

GPL-2.0-or-later — see [LICENSE](LICENSE).
