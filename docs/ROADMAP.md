# Roadmap

An independent Linux driver for the Plustek OpticFilm 135i film scanner
(USB 07b3:1436, GL126 controller), reverse-engineered from USB captures.
The end goal is a SANE backend so the scanner works with standard Linux
scanning tools.

## How "done" is measured (and how we avoid endless testing)

Progress is measured by **criteria, not test count**. Every item on the
lists below gets exactly one outcome:

- **Done** — works, verified on hardware against a threshold set in advance.
- **Documented limitation** — accepted, written down, moved past.
- **Parked** — deliberately deferred, with the condition to revisit it.

A phase is finished when every item has an outcome. Two rules keep testing
from feeding on itself:

1. **Acceptance thresholds are functional and set before the test** — "at
   what deviation does it stop working", not "how close to zero can we
   get". A measurement inside the threshold closes the item; it does not
   spawn more tests. (Example: frame-positioning tolerance is on the order
   of millimetres — the frame must fit the scan window with margin — so
   chasing tenth-of-a-millimetre transport variation is out of scope.)
2. **A new test is in scope only if it maps to an open item and has a
   pre-defined functional threshold.** Otherwise it is not run.

## Milestones

### M1 — Protocol understood ✅
USB captures decoded: init, calibration (AFE gain/offset, shading), the
per-frame position/scan/park sequence, IR channel. See `docs/protocol-notes.md`.

### M2 — Driver drives the hardware ✅
Hardware-verified: magazine load, single-frame and 1–4 batch scans at all
supported DPI, IR capture + dust removal, eject, cold-start init,
read-only `doctor`/`status`, and the safety guards that refuse unsafe
states. A positive-preview mode is available; colour interpretation is
left to the application (the driver delivers correct raw data).

### M3 — Robustness and honest limits (in progress)
Closing the remaining risks, each to a functional threshold:

- **Done:** DPI-change position stability; frame-to-frame and load-to-load
  position variation (well within the frame-fits threshold); calibration
  reproducibility.
- **Documented limitation:** only one physical unit exists, so cross-unit
  behaviour is compensated by run-time calibration and honestly labelled,
  not claimed as verified.
- **Open:** an even-frame calibration anomaly seen on one host — under
  investigation with a dedicated diagnostic; it will resolve to either a
  fix or a documented, application-correctable limitation. A separate
  completion-mask safety question on the longest positioning move.

### M4 — SANE backend
A `genesys`-family backend (using the gl124 backend as the template),
brought to the point where it builds, scans a frame via `scanimage`, and
works in SANE frontends such as digiKam. Started only once M3's list is
complete.

Two distinct steps — the first does not depend on the second:

- **Local backend (self-contained):** build the backend against
  sane-backends, install the `.so`, register it in `dll.conf`. SANE and
  its frontends then see the scanner. This needs no approval from anyone —
  it is entirely under our control.
- **Upstream contribution (optional, later):** getting the backend merged
  into the SANE project so it ships with distributions. Defined by SANE's
  own contribution requirements. Wider reach and shared maintenance, but
  not required to use the scanner.

Meanwhile the CLI already scans batches to raw 16-bit TIFF — a sound
workflow for bulk-digitising film to the best possible starting point,
with colour interpretation done later in the application.

## Where we are now

M1–M2 complete; M3 in progress with a short, frozen list of open items
(above). The test log (`docs/test-log.md`) records each hardware and
offline pass. M4 has a skeleton but is not active work yet.
