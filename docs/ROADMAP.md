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
brought to the point where it builds, scans a frame via `scanimage`,
passes the SANE backend test suite, and is ready for upstream review.
Started only once M3's list is complete. "Done" here is defined by SANE's
own contribution requirements, not by an internal test count.

## Where we are now

M1–M2 complete; M3 in progress with a short, frozen list of open items
(above). The test log (`docs/test-log.md`) records each hardware and
offline pass. M4 has a skeleton but is not active work yet.
