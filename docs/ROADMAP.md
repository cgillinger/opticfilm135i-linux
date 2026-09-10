# Roadmap

An independent Linux driver for the Plustek OpticFilm 135i film scanner
(USB 07b3:1436, GL126 controller), reverse-engineered from USB captures.
The goal is a SANE backend so the scanner works with standard Linux
scanning tools (`scanimage`, digiKam, …).

This document is the **single source of truth for status and done-ness**.
The test log (`docs/test-log.md`) keeps the history and evidence.

## How "done" is measured

By **functional acceptance criteria, not test count**. Each requirement in
the matrices below has one status. A milestone is delivered when its
mandatory criteria are met — the next action is then the *delivery*, not
more general testing.

**Images are accepted by human eyes — at milestones, on production
images.** Working images (geometry runs, calibration diagnostics,
anything whose answer is a measurement) are inspected by whoever runs
the analysis and are not separately approved. At every milestone, and
after any change that touches the image path (geometry, calibration,
colour pipeline, a new resolution profile, infrared), a
**production-ready image** is scanned and approved by the project's
owner against a fixed checklist: the colour planes align (no stagger
fringes), the whole frame is present including both ends of the window
(no skew, no clipped edge), no banding or stripes, and the whole
compares to a vendor scan of the same strip. Statistics, dimensions and
byte counts support the judgement, they never replace it (a pass that
delivers the right number of bytes of the wrong picture has happened;
see the test log on the shading-table swap). Each approved image is
archived as the reference the next working images are compared against.

**Stop / reopen rules.**
- A finished item reopens **only** on a concrete regression, new relevant
  failure evidence, or a change that affects its earlier verification. A
  wish for more repetitions or a larger safety margin is **not** a reason.
  New capability belongs to a later milestone.
- Every additional test must map to an **open** requirement or a documented
  reopen reason. After a code change, re-test only the affected
  requirements plus necessary regression checks.
- A limitation may be **accepted** only if it is outside the release's
  promised function **or** has a verified, safe handling. A safety fault,
  or a fault that makes the promised **raw image** untrustworthy, **blocks**
  the affected function — it cannot be parked as a "documented limitation"
  while it is also a mandatory requirement of the same release.

**Verification levels** (kept distinct): *observed* (a symptom seen) →
*root cause proven* → *fix implemented* → *offline-verified* (tests, no
hardware) → *hardware-verified*. A fix is not hardware-verified just
because the original anomaly was seen on hardware. Documentation and build
requirements never need hardware.

---

## Milestones and their definitions of done

| Milestone | Done when |
|---|---|
| **A — Own driver complete** | A versioned release with a frozen support scope, all mandatory acceptance criteria met, install + user instructions, and accepted limitations listed. |
| **B1 — Local SANE backend complete** | An installable backend that performs the agreed scanning workflow and preserves the driver's safety model. |
| **B2 — SANE contribution delivered** | Code, documentation and verification evidence **submitted** per the SANE project's current contribution process. |
| **C — Full-length holder and slide holder** | Frames 1–6 of the strip holder are geometrically measured and hardware-verified in the driver and the backend; the mounted-slide holder's mechanics are characterised as far as an empty original holder allows. |

B1 and B2 are explicit project goals, not optional future ideas. B2 is
scoped to **delivery**: we control *prepared → submitted*, not the
recipients' *accepted → published* decisions or timeline. Post-submission
feedback is handled as a bounded follow-up (address review comments on the
submitted work); it is not an open-ended maintenance commitment. This plan
does not authorize contacting recipients or sending material.

---

## A — Own driver: frozen support scope

**In scope (what the release promises):**
- **Host:** one unit (07b3:1436, GL126) on Linux over xHCI (pyusb).
- **DPI:** 600 / 1200 / 2400 / 3600 / 7200.
- **Frames:** 1–4 (one magazine); single-frame and 1–4 batch in one session.
- **IR:** dual-light IR pass + IR-based dust/scratch removal.
- **Transport:** driver-managed load (vendor insert flow), per-frame
  positioning, eject from a loaded magazine, cold-start init.
- **Output:** raw 16-bit linear negative (the product); optional preview
  positive; resumable bulk-digitisation (`of135i digitize`).
- **Safety:** fail-closed start-state guard, process lock, read-only
  `doctor`/`status`.

**Deferred to a later version (explicitly out of scope for A):**
- Speed tuning of the replayed command stream.
- Cross-unit support (only one physical unit exists — a documented
  limitation, compensated by run-time calibration).
- Hardware-button daemon / auto-load on insert.
- Any GUI or SANE frontend integration (that is B1+).

Scope is not reduced silently to mark A done. Any change here is a
recorded scope decision.

## A — Acceptance matrix

Evidence refers to `docs/test-log.md` entries; "rev" is the driver commit
the evidence was produced against where it matters.

| ID | Requirement (function / safety) | Pass criterion | Evidence | Remaining check | Verified |
|---|---|---|---|---|---|
| A1 | Magazine load via the driver flow | Latched (drag test holds, blue LED) from a power-on | Test 17–23 (7/7) | — | hardware |
| A2 | Single-frame 3600 dpi calibrated scan | Calibration reproduces the vendor within ±1 gain code / 0.03 % shading gain on this unit; render on par with vendor | Test log 2026-09-05; README | — | hardware |
| A3 | 1–4 batch in one session | Four frames, each correctly positioned | Test 24, 18/19 | — | hardware |
| A4 | All five DPI | Each scans and assembles | Test log (dpi profiles) | — | hardware |
| A5 | IR pass + dust removal | IR channel written; visible cleaned without color ghosts | Test log; test_ir | — | hardware |
| A6 | Eject from a loaded magazine | Magazine released to loose-in-slot, reg 0x01=0x22 | Test log (ejects) | **Test 44 (2026-09-07):** eject from the base-table-only state (produced only by the SANE backend's first `init()`, never by the CLI or the vendor) stalled twice. The backend no longer produces that state (Test 46: `sane_open` writes nothing, as the vendor and the driver do). Eject is defined from the magazine-flow and post-PARK states only; the stall's mechanism is recorded as a hypothesis, not pursued. | hardware (from the magazine flow and post-scan states) |
| A7 | Cold-start init | reg 0x01=0x00 → cold homing inside the load flow | Test 22; test log | — | hardware |
| A8 | Positioning never starts a scan on a moving transport | Long-move completion is class F; frame lands on the normal batch position structure (fits the scan window with margin — **scope decision: tolerance is on the order of mm, not rows**) | Test 28 (d555 budget), Test 34 (f555 benign) | — | hardware |
| A9 | Safety model | Refuses writes unless start state known (0x22/0x00), read before configure (zero writes on refusal), process lock, short transfer = unknown state, no auto-recovery | Test 12–16 (guard held); hardware-safety.md | — | hardware |
| A10 | Residual dark_b handled | A residual dark_b (device returns a stale buffer on a later batch frame) is detected and the session's healthy dark_b substituted, else fail-closed; the delivered raw image's calibration is trustworthy | Cause: Test 32. Fix: Test 33 (offline). **HW-confirmed Test 36** (frame 3 residual → substituted → all four frames get the reference offset) | — | hardware |
| A11 | Raw output integrity | Linear, unclipped, channel-aligned negative | Test 20 (no clipping, black floor stable) | — | hardware |
| A12 | Bulk-digitisation workflow | `of135i digitize`: resumable staging + append-only manifest, one strip per run; raw negative preserved, preview a separate file, resume protects saved images | Test 35; fixed Test 37; **audited + hardened Test 38** (preview vendor orientation, prefix-independent roll sequences, guard on any non-empty dir, success-path manifest tolerance) | — | offline (build/logic; the scan it calls is A2/A3) |

**What already counts as sufficient:** A1–A7, A9, A11 are hardware-verified
and closed. A12 is offline-complete (its scanning is A2/A3). No new test
series is required for these merely because this plan was written.

**What was required before A could be declared complete — both done:**
1. ~~A10 hardware confirmation~~ — **done (Test 36):** frame 3's residual
   dark_b was detected and substituted, all four frames got the reference
   offset, raw image sound. The fix is hardware-verified on the host that
   produces the fault.
2. ~~Release packaging~~ — **done:** tagged **v0.1.0** on 45305a4; README
   install + usage confirmed complete and current. **v0.1.1** (d24cc81)
   follows with the post-release digitize fixes (Test 40).

Every acceptance-matrix row is met and A is packaged. **Milestone A is
complete.**

## A — Accepted limitations

Each is outside the promised function or has a verified safe handling:
- **Cross-unit:** only one unit exists; behaviour is compensated by
  run-time calibration and honestly labelled. (Out of scope.)
- **Colour interpretation:** the driver delivers correct raw data; colour
  is the application's job. (Out of scope by design.)
- **Speed:** correct but not tuned. (Out of scope for A; a functional scan
  is not blocked.)
- **Holders and frame positions:** A promises positions 1–4, the only
  ones verified frame for frame when A was frozen. The holder's six
  apertures have since been measured (`docs/holder-geometry.md`) and the
  driver accepts 1–6, but 5 and 6 are not hardware-verified; the
  mounted-slide holder is uncharacterised; panorama is a vendor software
  mode (one continuous scan, the holder encodes as the strip holder)
  that needs its own capture. (Out of scope for A; milestone C.)

---

## B1 — Local SANE backend (definition of done)

A `genesys`-family backend (gl124 template) that:
- builds against sane-backends and installs (the `.so` + `dll.conf`);
- performs the agreed workflow via `scanimage` and a SANE frontend
  (digiKam): load, scan a frame, deliver the image;
- **preserves the driver's safety model** — no writes from an unknown
  start state, no automatic recovery after a fault, and the park
  sequence only after a complete scan pass (never from an aborted or
  cancelled one);
- delivers images that the owner has looked at and approved, per
  resolution and mode claimed (the human-eyes rule above).

Scope for B1 mirrors A's in-scope list (single unit, the DPI set, 1–4
frames, IR). Frontend niceties beyond "scan a frame correctly" are
deferred. B1 needs no upstream approval — it is entirely under our control.

## B2 — SANE contribution delivered (definition of done)

Code + documentation + verification evidence **submitted** to the SANE
project per its current contribution process (its CONTRIBUTING / merge-
request flow at submission time). Delivered = submitted, review-ready.
- We control *prepared → submitted*. *Accepted → published* is the SANE
  maintainers' decision and timeline, not a gate we can close.
- Post-submission: address review feedback on the submitted work as a
  bounded follow-up. Not an open-ended maintenance pledge (SANE's own
  "unmaintained" status exists for backends whose author steps back).

---

## C — Full-length holder and slide holder (definition of done)

A does not cover this: A's scope was frozen at frames 1–4, and it stays
frozen. C extends the holder support without reopening it.

**C1 — the strip holder, frames 1–6.** Done when:
1. The six apertures' positions and dimensions are measured, not assumed.
   ✅ done offline (`docs/holder-geometry.md`): six apertures, 35.80–36.12
   mm long, crossbars 1.90–2.00 mm, constant pitch, measured from the
   vendor's whole-holder pass with positions 5 and 6 empty.
2. The pitch is settled by evidence rather than by the older nominal
   reading. Seven observed vendor grid steps say 10752; the driver still
   carries 10760, a 0.14 mm difference at frame 6. **Open decision.**
3. Each of the six scan windows contains its whole aperture with positive
   measured margin on both sides, on hardware, with the empty holder.
4. Load-to-load variation is measured over three separate loads and is
   smaller than that margin.
5. Frames 5 and 6 are hardware-verified: POSITION completes on class F
   inside budget, the scan delivers, PARK completes.
6. A full-length six-frame **colour** negative scans 1–6 with the right
   image in each position.
7. A full-length six-frame **black-and-white** negative does the same, as
   an independent physical control. (Silver black-and-white film is
   opaque to infrared and is not an infrared or dust-removal reference.)
8. The CLI and the SANE backend both accept 1–6 and both refuse frame 0
   and frame 7+ before any write. ✅ done offline.
9. Frames 1–4 show no regression.
10. README, this roadmap and the test log describe what was actually
    verified, separately from what was measured offline.

**C2 — the mounted-slide holder.** An empty original holder can establish
identification, load and transport, frame count, pitch, the four aperture
positions, scan geometry, repeatability and park/eject. It cannot
establish focus at the film plane inside a mount, sharpness, positive-film
colour or tonal rendering, infrared behaviour on a real slide, or dust
removal. Done when the first list is verified on hardware and the second
is documented as separately unverified, pending a physical slide.

---

## Current status (2026-09-07)

- **M1 — protocol** ✅ and **M2 — driver drives the hardware** ✅.
- **M3 — robustness:** ✅ **complete.** Every row of the A-matrix is met;
  A10 (the residual-dark_b fix) is hardware-verified on B5 (Test 36). No
  open rows.
- **A — own driver:** ✅ **complete.** All acceptance criteria met and
  packaged: tagged **v0.1.0** (45305a4), README install/usage confirmed.
- **B1 — SANE, in progress** (since 2026-09-06): register tables
  generated, backend builds against sane-backends, model enumerates,
  `sane_open` initialises the unit exactly as the driver does (Test 43).
  Calibration, positioning, scan pass and park verified on hardware
  (Tests 48–52): `scanimage` delivers frame 1 at 3600 dpi in colour,
  equal to the driver's output within its run-to-run band. Frame
  selection (`--frame 1..4`) positions correctly on hardware (Test 53).
  The backend's image carried the sensor's colour-line offset
  uncorrected (Test 53); corrected through the core's own channel-shift
  node, host side, wire unchanged, verified offline and then on hardware
  (Test 54: residual 0 rows, 3762 × 5113 delivered, waits and wire as
  Test 52). **Image acceptance:** Christian's eye check of the Test 54
  image is the open step — no backend image is accepted until he has
  said so.
  The other resolutions and infrared (hook 8, docs/sane-hook8-dual.md)
  are implemented offline and wire-equal to the driver; their hardware
  runs and eye checks are next. Still to do for B1 after that: install/
  packaging (docs/sane-port.md). **B2** not started.
- **A6 note** (Test 44/46): the driver's eject stalled from a state only
  the backend's first `init()` produced; that `init()` now writes nothing.
  No CLI workflow was ever affected. The stall mechanism itself is an
  open observation, not a blocker.

Milestone A is done; the A6 note records an observation from B1's
bring-up, not a CLI regression. The next action is B1's next hook.
