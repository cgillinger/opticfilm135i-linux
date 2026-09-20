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
   follows with the post-release digitize fixes (Test 40). **v0.1.2**
   (2026-09-12) is a bug-fix release: the IR channel is colour-line
   aligned before averaging (the three CCD rows are staggered under IR
   light too; every dust speck was a triplet before) —
   docs/release-notes-v0.1.2.md, Test 72.

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
  apertures have since been measured (`docs/holder-geometry.md`) and
  frames 1–6 are fully supported: the corrected A+C positioning is the
  production default, hardware-verified across all six frames and every
  profile, and the six-frame production workflow is accepted
  (Tests 55–61; `docs/holder-position-design.md`). **The four-slide
  holder that ships with the scanner is not supported yet** — its
  mechanics are characterised (S1+S2 empty, 2026-09-17; Test 84 with a
  mounted slide, 2026-09-18, `docs/slide-holder-analysis.md`: load flow
  identical to the strip holder, a measured 62.6 mm grid of four openings
  inside the verified FEEDL range, the slide's image whole and unclipped
  through the strip-holder windows at 600 dpi), but no slide-specific
  driver path (positioning, crop, positive handling, dpi-scaled dust
  removal) exists yet (C2, a planned separate milestone); panorama is a vendor software mode (one
  continuous scan, the holder encodes as the strip holder) that needs
  its own capture. (The 1–6 work was delivered under milestone C.)

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

**Magazine handling.** When B1 was declared done (2026-09-13, Christian's
decision) "load" was a step of the workflow, not a frontend feature: the
magazine was loaded and ejected with `of135i load` / `of135i eject`, and
SANE owned the scan. That division satisfied B1 but not B2, so WP-4 was
added and completed the same day: `load_document()`/`eject_document()`
are implemented and the backend runs the whole load → scan → eject cycle
by itself (Tests 75–77). The CLI division remains a valid workflow
(`docs/sane-install.md` §7); it is no longer the only one.

**B1 is DONE, 2026-09-13** (Test 74): installed as a normal genesys build,
`scanimage` and digiKam both scan through it with the loaded library proved
at the `dlopen` level, the safety model intact (calibration every scan, PARK
only after a complete pass), and every claimed profile owner-approved by eye
for geometry and integrity. Colour rendition was explicitly not part of that
acceptance; it is the application's job.

## B2 — SANE contribution delivered (definition of done)

Code + documentation + verification evidence **submitted** to the SANE
project per its current contribution process (its CONTRIBUTING / merge-
request flow at submission time). Delivered = submitted, review-ready.
- We control *prepared → submitted*. *Accepted → published* is the SANE
  maintainers' decision and timeline, not a gate we can close.
- Post-submission: address review feedback on the submitted work as a
  bounded follow-up. Not an open-ended maintenance pledge (SANE's own
  "unmaintained" status exists for backends whose author steps back).

**Prerequisite added 2026-09-13 (Christian's decision): the backend must
stand on its own.** A SANE backend that needs an external CLI — our Python
driver — to load and eject the magazine is not a SANE backend from a user's
point of view; a person who installs sane-backends and opens any frontend
must be able to operate the unit. So before anything is submitted:
`load_document()` and `eject_document()` are implemented in `gl126.cpp` and
hardware-verified, and the whole load → scan → eject cycle runs from a SANE
frontend alone, with no `of135i` command. The load flow is the project's
most delicate motor sequence (it caused the one motor stall), it has to run
from C++ for the first time, and the operator still has to take the magazine
out and re-seat it by hand mid-sequence — SANE has no way for a backend to
prompt for that, so the interaction model needs designing, not just the
transfers. This is its own work package (WP-4) with its own hardware plan.
Related and to be taken together: the standing requirement that "power-cycled
+ latched magazine" become a supported driver operation.

---

## C — Full-length holder and slide holder (definition of done)

A does not cover this: A's scope was frozen at frames 1–4, and it stays
frozen. C extends the holder support without reopening it.

**C1 — the strip holder, frames 1–6.** Done when:
1. The six apertures' positions and dimensions are measured, not assumed.
   ✅ done offline (`docs/holder-geometry.md`): six apertures, 35.80–36.12
   mm long, crossbars 1.90–2.00 mm, constant pitch, measured from the
   vendor's whole-holder pass with positions 5 and 6 empty.
2. The pitch is settled by evidence, not by either older nominal
   reading. **Resolved (Test 56/N2):** neither the vendor's commanded
   grid (10752) nor the driver's constant (10760) describes the
   measured end-to-end mapping (~10733, from three empty-holder loads
   plus N1); the decision taken is **A+C — a corrected mean mapping
   plus overscan with a host-side aperture-registered crop**
   (`docs/holder-position-design.md`), hardware-demonstrated on the
   plain 3600 dpi profile (Test 57) and production-accepted on a real
   six-frame colour negative (Test 58/N3). The corrected model is now
   the plain-3600 **production default** — the single runtime
   geometry; the vendor grid remains as capture evidence only. The
   SANE backend's C++ (`sane/gl126_ops.cpp`'s `feedl_for_frame()` /
   `frame_geometry()`) was wired to the SAME frozen A+C ledger
   (`Profile::frames[]`, `sane/gl126_tables.h`) 2026-09-10 ("Lager 1")
   — offline-verified (235 tests green, generator `--check` clean, 0
   build warnings) and **hardware-verified through the backend** on
   plain 3600 dpi, frames 1–6 (Test 62, 2026-09-11; the CLI driver's own
   verification does not transfer to this separate implementation, so
   it was run on its own). The overscan window is still delivered whole to
   the SANE frontend (Lager 2's aperture-registered crop + coverage
   check is not ported). The default flip reopened frames 1–6
   for the positioning requirement; the one empty-holder regression
   load re-verified them (Test 59, coverage 6/6).
3. Each of the six scan windows contains its whole aperture with positive
   measured margin on both sides, on hardware, with the empty holder.
   ✅ done (Test 59: the empty-holder regression load under the A+C
   default, coverage verified 6/6, lead 0.73–0.91 / trail 0.59–0.94 mm).
4. Load-to-load variation is measured over three separate loads and is
   smaller than that margin. ✅ done (Test 56: three separate empty-holder
   loads, ±0.24 mm observed against the 0.75 mm commanded margin).
5. Frames 5 and 6 are hardware-verified: POSITION completes on class F
   inside budget, the scan delivers, PARK completes. ✅ done (Tests 55
   and 59: POSITION 12.4–12.5 s on frame 6 against the 43.5 s budget,
   scan and PARK normal; repeated through `digitize` in Test 83).
6. A full-length six-frame **colour** negative scans 1–6 with the right
   image in each position. ✅ done on hardware and accepted by eye
   (Test 58/N3: six frames delivered via overscan + crop, coverage
   verified 6/6 with 0.60–0.98 mm margins, human-eye acceptance PASS;
   the run is archived as the A+C reference).
7. A full-length six-frame **black-and-white** negative does the same, as
   an independent physical control. (Silver black-and-white film is
   opaque to infrared and is not an infrared or dust-removal reference.)
   ✅ done (Test 60: Kodak 5052 TMX, coverage 6/6, calibration and
   timing identical to the colour strip's band).
8. The CLI and the SANE backend both accept 1–6 and both refuse frame 0
   and frame 7+ before any write. ✅ done offline.
9. Frames 1–4 show no regression. ✅ done (Test 59: the one-load 1–6
   empty-holder regression under the A+C default — coverage 6/6,
   calibration and timing identical to N3's band).
10. README, this roadmap and the test log describe what was actually
    verified, separately from what was measured offline. ✅ converged
    2026-09-15 (this revision).

**C1 status, per implementation (2026-09-15).** Python driver: every
criterion above is met with evidence; **C1 is done for the driver.** SANE
backend: positioning to the same ledger is hardware-verified for frames
1–6 on plain 3600 dpi (Test 62) and frame 1 on every other profile (Tests
63–71); frames 0 and 7+ are refused before any write; the backend delivers
the whole overscan window — the host-side coverage check and crop are not
ported, by design (submission limitation 2). No C1 criterion is open for
the backend beyond that documented limitation, and none is a B2 blocker.

**C2 — the mounted-slide holder.** The empty-holder mechanics were
characterised 2026-09-17 (S1 + S2, `docs/slide-holder-analysis.md`), and
Test 84 (2026-09-18) ran a mounted slide through the strip holder's
frames 1–6 at 600 dpi on existing code: load, six POSITION moves, PARK
and eject normal; the slide's image whole and unclipped; IR usable; the
holder's four openings measured as a 62.6 mm grid inside the verified
FEEDL range (the vendor sweeps instead of positioning, but the grid is
there). Test 85 (2026-09-19) then answered the imaging questions at
3600 dpi dual, twice over: the film plane is in focus and 3600 dpi is the
right sampling (a lower resolution loses 3.8 % of the detail at 1800 dpi,
7.1 % at 600; the spectrum reaches the noise floor at ~1200–1500 lp/in),
a dual profile is required laterally (plain 3600's 26.5 mm clips a
mounted slide), and `remove_dust` is correct at 3600 — its dpi-blindness
is a low-resolution problem. The aperture measured the same across three
loads (35.06 × 22.75 mm, moving 0.13 mm). What remains for C2: a slide
profile in the driver (POSITION to the opening on the measured grid, crop
keyed on IR, positive handling without inversion, dust removal scaled by
dpi for the low resolutions) and grid stability beyond two loads. C2 is
done when that list is verified on a mounted slide. **Brought forward
2026-09-18 (owner's decision, slides are in scope for the driver): in
progress.**

---

## Current status (2026-09-15)

- **M1 — protocol** ✅ and **M2 — driver drives the hardware** ✅.
- **M3 — robustness:** ✅ **complete.** Every row of the A-matrix is met;
  A10 (the residual-dark_b fix) is hardware-verified on B5 (Test 36). No
  open rows.
- **A — own driver:** ✅ **complete.** All acceptance criteria met and
  packaged: tagged **v0.1.0** (45305a4), README install/usage confirmed.
- **B1 — SANE, local backend:** ✅ **complete (2026-09-13, Test 74).** All
  six profiles implemented, offline-verified and hardware-run through the
  backend (plain3600 frames 1–6; the other five frame 1 each, eye-accepted
  for geometry — Tests 62–71); ordinary scans need no `--force-calibration`
  (Test 64); installed as a normal genesys build and used from `scanimage`
  and digiKam (Test 74). Per-profile detail in the **SANE profile matrix**
  below.
- **B2 — SANE contribution:** **PREPARATION PHASE — prepared, not
  submitted.** The B2 prerequisite (the backend works the magazine itself)
  is met on hardware (WP-4, Tests 75–77, including a power-cycled unit with
  a latched magazine). A five-commit submission package exists and builds
  on its own, rebased onto current upstream (WP-3,
  `docs/sane-wp3-submission.md`). Nothing has been sent.
  What stands between "prepared" and a submission decision is the blocker
  list in **B2 — preparation phase** below.
- **C — full-length holder:** C1 is done for the driver and verified
  through the backend as far as it promises (per-criterion status above);
  C2 (slides) is pending, scheduled after B2. History: the strip holder's six
  apertures are measured and the driver reaches and scans all six
  positions on hardware — transport, scan and PARK verified across
  three separate empty-holder loads plus one with film (Test 55–57).
  That testing found neither nominal pitch candidate (10752 or 10760)
  matches the measured end-to-end mapping (~10733) and that the
  transport varies somewhat load to load; the decision taken is a
  corrected mean mapping plus overscan with a host-side
  aperture-registered crop (A+C, `docs/holder-position-design.md`),
  hardware-demonstrated on the plain 3600 dpi profile (Test 57) and
  production-accepted on a real six-frame colour negative (Test 58/N3:
  coverage verified 6/6, human-eye acceptance PASS). A+C is now the
  plain-3600 production default and the single runtime geometry,
  re-verified across frames 1–6 by the empty-holder regression load
  (Test 59) and on a second film stock (Test 60); the dual profiles
  carry the same contract, hardware-verified one run per profile
  (Test 61).

  Status, conservatively:
  - Strip holder geometry 1–6: HARDWARE VERIFIED
  - Frames 5–6 transport / scan / PARK: HARDWARE VERIFIED
  - Plain 3600 corrected positioning + overscan: HARDWARE VERIFIED (Tests 57–58)
  - Plain 3600 aperture coverage: HARDWARE VERIFIED (Test 58: 6/6 on real film)
  - Full 1–6 production-image workflow with real full-length film: ACCEPTED (Test 58/N3)
  - A+C as the plain-3600 default: HARDWARE VERIFIED (Test 59: the 1–6 empty-holder regression load, coverage 6/6, no regression)
  - B&W six-frame control strip: PASSED (Test 60)
  - Dual robust A+C: HARDWARE VERIFIED (Test 61: one run per profile
    plus the 2400 confirmation on the corrected anchoring — coverage
    5/5 on the sequences the driver actually commands, IR-to-visible
    registration 0.4/0.06 lines; the dual engine start-anchors FEEDL —
    design doc section 11)
  - dpi2400 delivered-image proportion (anisotropic 3600 across / 2400
    along): the SANE backend delivers the sensor axis scaled 5256 → 3504 px
    (square pixels) via the core's host row scaling, and the Python TIFF
    export states the true per-axis dpi (both, 2026-09-12).
    HARDWARE-CONFIRMED for SANE dual2400 frame 1 (Test 63: delivered PNM
    3504×3560, raw 5256/225,545,472 B unchanged, FEEDL 6543, coverage
    verified, proportion near-square 37.1×36.05 mm, channel shift 0/0,
    normal PARK) and EYE-ACCEPTED for the geometry goal 2026-09-12
    (Christian + Astra; proportions natural, generous overscan). NOT
    generalised to the other dual profiles. The two Test-63 findings are now
    investigated offline (2026-09-12), both APP-LAYER, neither a B1 blocker:
    * **Mirror — explained, no code bug.** The new preview differs from the
      2026-09-11 previews by exactly the horizontal-mirror step: the 09-11
      review images were rendered with `rot90(3)` only, the Test-63 preview
      with `rot90(3)[:, ::-1]` — the driver's canonical `--positive` (vendor
      HorizontalMirror=1, added in Test 37/46, vendor-validated in N3/Test 58).
      SANE delivers ONE consistent raw negative; orientation is the app's job,
      so this is a throwaway-preview-script inconsistency, not a SANE or shared-
      code defect. The vendor-matching orientation is the flipped one (Test 63).
      Residual: Christian confirms the real left–right from the original/memory
      (no new scan) — a one-off user check, not backend work.
    * **Yellow-green cast — preview only, raw healthy.** The raw negative is a
      proper C-41 orange mask (R base p99.8 ~24500 vs G/B ~11000, unclipped);
      `to_positive`'s per-channel linear stretch leaves the positive blue-
      deficient → green. A principled per-channel median white-balance
      (factors ~0.88/0.85/1.46) recovers fully neutral colour. So the cast is
      the deliberately raw-faithful `to_positive` convenience, NOT a backend
      channel/calibration fault; raw delivery is sound. No vendor scan of this
      strip exists, so absolute colour is not compared (a stated limitation).
      Any tone/colour work in `to_positive` remains Christian's separate call.
  - Calibration cache (ordinary scan without --force-calibration): FIXED
    offline 2026-09-12. A compatible Genesys calibration cache made
    genesys_start_scan skip calibration, and GL126's begin_scan (which needs
    this sane_start's own ShadingDone) then refused the scan — the reason
    --force-calibration was required. The fix gates the cache restore off for
    GL126 only (every scan calibrates); begin_scan's guard and other ASICs are
    unchanged. Offline-verified by driving the real sane_open→sane_start flow
    in test mode (tests/test_sane_calibration_cache.py), and HARDWARE-CONFIRMED
    2026-09-12 (Test 64: two consecutive no-flag scans on one load, the second
    with the compatible .cal cache present, both calibrated and completed).
    --force-calibration is no longer required for an ordinary scan.
  - Slide holder: empty-holder mechanics characterised 2026-09-17 (S1+S2,
    docs/slide-holder-analysis.md); imaging + crop pending a physical slide
- **A6 note** (Test 44/46): the driver's eject stalled from a state only
  the backend's first `init()` produced; that `init()` now writes nothing.
  No CLI workflow was ever affected. The stall mechanism itself is an
  open observation, not a blocker.

Milestone A is done; the A6 note records an observation from B1's
bring-up, not a CLI regression. B1 is done. The next action is the B2
preparation-phase blocker list below.

## SANE profile matrix (2026-09-12)

SANE-only status. Do NOT read the Python driver's own hardware acceptance
(Tests 55–61) into this table — the backend is a separate implementation.
"Impl+offline" = tables generated + wire-equal op tests + geometry ledger.
"SANE HW" = verified on the device THROUGH the SANE backend. Image acceptance
is the owner's eye rule and is scoped to what was actually judged.

| Profile   | Impl + offline | SANE HW verified | Frames (SANE HW) | Image acceptance (scope) | Concrete remaining for B1 |
|-----------|----------------|------------------|------------------|--------------------------|---------------------------|
| plain3600 | yes | transport + coverage (Tests 62, 74) | 1–6 | EYE-ACCEPTED for geometry/integrity (Test 74, via digiKam); colour explicitly not judged, and it is the app's job | none |
| dpi2400   | yes | transport + coverage + proportion (Tests 62/63) | 1 | EYE-ACCEPTED, geometry only (Test 63); colour is the app's job | none for geometry; frames 2–6 not required for B1 (one frame proves the profile) |
| ir3600    | yes | transport + full-width, colour-aligned IR (Tests 68–71) | 1 | EYE-ACCEPTED (Test 71: cleaner, no streaks, well positioned; alignment by measurement) | none |
| dpi600    | yes | transport + coverage (Test 65) | 1 | EYE-ACCEPTED, geometry (Test 65) | none |
| dpi1200   | yes | transport + coverage (Test 66) | 1 | EYE-ACCEPTED, geometry (Test 66) | none |
| dpi7200   | yes | transport + coverage (Test 67) | 1 | EYE-ACCEPTED, geometry (Test 67) | none |

Notes: the calibration-cache fix (Test 64) and the safety model apply to all
profiles. The mirror and colour-cast findings are app-layer (raw delivery is
sound), so they do not gate any row. B1's image-acceptance rule is about
geometry/integrity (whole frame, right proportions, no banding), not absolute
colour or the positive's orientation, which are the frontend's/user's job.

The four profiles' hardware plan, ledger and per-run status (Tests 65–71,
2026-09-12) are in **[docs/sane-remaining-profiles-plan.md](sane-remaining-profiles-plan.md)**;
all four are done.

## B1 / B2 finite plan (2026-09-12)

Only the work packages that remain for B1 (local backend) and B2 (submission).
Lateral overscan and Layer 2 are NOT introduced here; no finding makes B1's
promised scope require them. VueScan stays out of public docs.

**WP-1 — Remaining-profile hardware verification (B1).**
- Goal / acceptance: each claimed profile (ir3600, dpi600, dpi1200, dpi7200)
  produces one SANE scan on the device that completes (calibration, transport,
  PARK), delivers a whole frame with correct proportions, and is owner-
  approved by eye; plus one owner-approved plain3600 SANE image for the record.
- Evidence already enough: dpi2400 (Tests 62/63) needs nothing more for B1;
  plain3600 transport/coverage (Test 62) stands — only the eye-approval image
  remains.
- Remaining offline: none (all six implemented and wire-equal).
- Minimal hardware test: ONE session, ONE scan per remaining profile
  (600/1200/7200 + IR3600 + one plain3600), no `--force-calibration`, low USB
  debug, straight-seated strip, per docs/sane-lager1-hardware-plan.md order
  and stop/recovery rules. NOT all six frames at every resolution — one frame
  per profile proves the profile; frame coverage is already shown (plain 1–6,
  the driver's own 1–6).
- Stop condition: any profile that fails transport/geometry, or an image the
  owner does not approve, stops WP-1 for that profile and is logged; no blind
  retry.

**WP-2 — Install & frontend (B1).**
- Goal / acceptance: the backend installs as a normal genesys build (the `.so`
  + `dll.conf`), enumerates, and performs load→scan→deliver via `scanimage`
  AND one SANE frontend (digiKam, per the B1 definition), preserving the
  safety model.
- Evidence already enough: uninstalled runs via `LD_LIBRARY_PATH` +
  `SANE_CONFIG_DIR` work (Tests 47–64).
- Remaining offline: **done 2026-09-13.** The install path, the digiKam
  workflow and its limits are **[docs/sane-install.md](sane-install.md)**;
  `tools/sane_install.sh` installs/uninstalls it and was exercised against a
  staging root (`tests/test_sane_install.py`, 19 tests: byte-for-byte restore,
  rollback of a failed install, package-update and ambiguous-target handling,
  and `verify`'s failure paths against a stub `scanimage`). Bit-depth
  preservation in a frontend's saved file has its own probe,
  `tools/image_probe.py` (`tests/test_image_probe.py`, 6 tests), because
  Pillow truncates 16-bit RGB and a file header cannot tell real 16-bit data
  from 8-bit widened to 16;
  Fedora's own `libsane.so.1` was shown to load our build with every device
  line disabled, so nothing was addressed on the wire. Two facts the older
  notes had wrong: the symlink list in `sane-port.md` was missing `gl126_ops`
  and `gl126_lock` (fixed), and a preview in digiKam is a **full 600 dpi scan**,
  not a cheap one.
- Minimal hardware test: **DONE 2026-09-13 (Test 74)** per
  **[docs/sane-wp2-hardware-plan.md](sane-wp2-hardware-plan.md)**. Installed,
  the loaded library proved at the `dlopen` level for both frontends, one
  plain3600 frame-1 scan through the installed `scanimage` and one from
  digiKam on the same load (identical geometry, full transfer, normal PARK),
  eject from the CLI, and the owner's eye acceptance for geometry/integrity —
  which fills the plain3600 row above. One real install defect was found on
  the device and fixed the same session: the library had been built without
  `--sysconfdir=/etc`, which `scanimage` hid through symbol interposition but
  digiKam (a `dlopen`ed plugin, `RTLD_LOCAL`) did not.
- **Decided 2026-09-13 (Christian):** the CLI/SANE division satisfies **B1** —
  `of135i load` → SANE scan → `of135i eject`, documented as the workflow. It
  does **not** satisfy **B2**: "en SANE-drivrutin som förlitar sig på CLI och
  en pythondrivrutin är ingen SANE-drivrutin". Frontend-driven magazine
  handling is therefore a prerequisite for submission, tracked as WP-4 below,
  not a B1 gap.
- **WP-2 status: DONE (Test 74).** With it, **B1 is complete.**
- Stop condition: an install/enumeration/safety deviation stops WP-2; logged.

**WP-4 — Magazine handling inside the backend (B2 prerequisite).**
- Why: a backend that needs `of135i` to load and eject is not usable by
  someone who only installed sane-backends. Christian's condition for
  submission, 2026-09-13.
- Goal / acceptance: `load_document()` and `eject_document()` implemented in
  `sane/gl126.cpp` (they threw `SANE_STATUS_UNSUPPORTED` when WP-4 opened), and one full
  load → scan → eject cycle driven from a SANE frontend alone, no `of135i`
  command anywhere in it, with the safety model unchanged.
- **Interaction model decided 2026-09-13 (Christian): the two-step
  protocol.** The vendor's load flow requires taking the magazine out and
  re-seating it to the stop mid-sequence, and SANE has no mechanism for a
  backend to ask for that during `sane_start`. So a `load-film` button
  option runs the release half (a cold unit's bring-up, the vendor
  device-open table, the jog that frees the cassette), the operator
  reseats, and the next `sane_start` runs the load before it calibrates.
  An `eject-film` button and a read-only `magazine` status line complete
  the set. Design and the alternatives not taken:
  `docs/sane-wp4-magazine.md`.
- **Offline half DONE 2026-09-13.** Five op programs (`cold_init`, `open`,
  `jog`, `load`, `eject`), each proven to put exactly the Python driver's
  transfers on the wire in the driver's order
  (`tests/test_sane_ops.py`: 218 / 199 / 12 transfers); the hooks, the
  state machine and the on-disk "a release is pending" mark that lets
  `scanimage` load in two invocations (`tests/test_sane_magazine.py`,
  `tests/test_sane_lock.py`). Nothing has driven the motor from C++.
- **Reviewed and corrected the same evening (Astra), before any hardware.**
  Four real defects in the new safety model, all fixed and tested:
  only `OpsError` failed the session (a plain USB exception left a pending
  load armed); the load ran before the scan request was validated, so an
  impossible request could move the magazine first; the offline poll cap
  was honoured on real hardware; and "nothing is stuck" was said about
  every load timeout instead of the one documented benign signature.
  `docs/sane-wp4-magazine.md` §9.
- **RUN A DONE ON HARDWARE 2026-09-13 (Test 75).** A full load → scan →
  eject cycle driven from `scanimage` alone, no `of135i` command in it:
  release from cold (nine motor moves, the jog's four completions 0xf8 on
  the first poll), the operator's reseat, then the LOAD — engaging feed
  0xf4 first poll, traverse 0xdc — followed by the already-verified pass
  (FEEDL 6562, 120 963 348 raw bytes, PARK) and the eject. A real frame 1,
  3762 × 5335, 16 bit/channel, nothing clipped, no banding; sound normal.
  The cold start's opening ready poll timed out at 15 s and the sequence
  continued, which is the first hardware confirmation that the
  best-effort poll policy was right. Acceptance criteria 1, 2, 3 and 6
  met.
- **RUN B DONE ON HARDWARE 2026-09-13 (Test 76).** The same cycle driven
  entirely from the digiKam dialog, and a second frame on the same load
  proving `load_document` does nothing when no release is pending
  (FEEDL 17315 for frame 2, a genuinely different image). Transitions
  `unknown -> released -> loaded -> ejected`, once each, zero refusals.
  Criterion 4 met.
- **RUN C DONE ON HARDWARE 2026-09-13 (Test 77) — WP-4 IS COMPLETE.**
  Power-cycled with the magazine LATCHED (a vendor session had left it
  that way), freed by two presses of Load film — the double jog of Test
  51, now from C++ — then loaded (feed 0xf4 first poll, traverse 0xdc),
  scanned and ejected, all from the digiKam dialog with no `of135i`
  command. All six acceptance criteria met. **Christian's standing
  requirement that "power-cycled + latched magazine" be a supported
  driver operation is satisfied by the backend itself.**
- **WP-4 status: DONE.** B2's magazine prerequisite is met; what remains
  for B2 is WP-3 (submission package, prepared only).
- Offline follow-ups the three runs exposed, none touching a motor
  sequence: the `magazine` status value is too long for KSane's widget
  and shows only its tail, it sits below the buttons it describes, it
  reads only the in-process record so a fresh frontend says `unknown`
  while a load is pending, and unlike the vendor we do not refuse a scan
  when nothing is loaded. Plus the cold start's opening 15 s poll, which
  Test 77 measured as dead time on this unit.
- Minimal hardware test plan: `docs/sane-wp4-hardware-plan.md`. This is the load flow — the project's most
  delicate motor sequence, the one that caused the motor stall — driven
  from C++ for the first time. Not a piggy-back on another session.
- Take together with: the standing requirement that "power-cycled + latched
  magazine" become a supported driver operation.
- Stop condition: any deviation stops WP-4; no blind retry, no recovery
  experiments.

**WP-3 — SANE submission package, prepared only (B2). PREPARED,
rebased onto current upstream 2026-09-15 — see
`docs/sane-wp3-submission.md`.** A five-commit series on branch
`wp3-gl126-submission-v2`, based on sane-backends `7fb102b`, with the
GL126 files as REAL files rather than the development symlinks: it builds
clean from the branch alone (zero warnings), exports the same 107 gl126
symbols as the development build, and passes the three backend-dependent
offline suites (33 tests) run against it. The SANE checklist items that
apply to a new ASIC in an existing backend are done, including a licence
header the two generated files were missing. **Nothing has been sent, and
B2 is not complete**; what remains before anything could be is §7 of that
document.
- Goal / acceptance: a review-ready branch and evidence bundle exist LOCALLY /
  in Christian's repo — nothing is sent. Contents, mapped to SANE's process
  (`doc/backend-writing.txt`; the project's GitLab merge-request flow):
  `doc/descriptions/genesys.desc` 135i entries `:untested → :good`;
  `backend/genesys.conf.in` USB id; man-page chip list; `scanimage -T` and
  `tstbackend` output; `nm` export check; a commit series against a current
  sane-backends master; the integration expressed as real backend changes
  (the current work lives as `sane/gl126-integration.patch` + symlinks — WP-3
  converts it to committed files on a branch of a sane-backends clone).
- Evidence already enough: the patch, the generated tables, the op/geometry
  test suites, and Tests 62–64.
- Remaining offline: assemble the branch and the evidence bundle; run
  `scanimage -T`/`tstbackend`/`nm` locally; write the submission text.
- Minimal hardware test: none beyond WP-1/WP-2 (submission needs their
  results, not new runs).
- Stop condition: **WP-3 stops at "prepared".** Actual submission — pushing to
  any non-`cgillinger` repo, opening a merge request, or contacting the SANE
  maintainers / sane-devel — is a SEPARATE step requiring Christian's explicit
  instruction naming the action and recipient. It is NOT performed as part of
  "prepare upstream" or "finish B2".

Hardware proposals above are for review and Christian's explicit go; they are
not executed here.

## B2 — preparation phase: blockers and completion criteria (2026-09-15)

Fixed after the status review of `docs/updated-course.md` (the steering
document adopted 2026-09-15). A blocker is added only on concrete
evidence; everything else is a documented design choice or future work.
Items 1–5 are done offline. Items 6, 7 and 8 are each version-bound —
a rebase is only meaningful at submission time, the conformance run
must describe the rebased build, and the decision follows it — so they
are **one mission run in a single session at submission time**, frozen
in **[docs/sane-submission-runbook.md](sane-submission-runbook.md)**.
Nothing in that mission runs before Christian says the code is ready.

| # | Blocker | Closes when |
|---|---|---|
| 1 | Status drift between README, this roadmap and the submission document | The six questions in `updated-course.md` §3 get one answer everywhere; the submission text's motor-wait and testing claims match the code and the evidence. **Done in this revision.** |
| 2 | The exact WP-3 series is not reproducible from this repository | **Done 2026-09-15:** bundle + five patches in `sane/wp3-package/`, recreated identically (tip tree `65a7b8bd…`) by both the `git am` and bundle routes in a clean clone. |
| 3 | Lock and magazine-mark file handling (`/tmp`, mode 0666, no `O_NOFOLLOW`, truncating write) | **Done 2026-09-15:** both sides open `O_NOFOLLOW` + regular-file check, the mark is written via temp+`rename`, path/format unchanged; new probes in `test_sane_lock`/`test_safety` (see `gl126_lock.h`). |
| 4 | Shared genesys code changed without a per-hunk rationale | **Done 2026-09-15:** every shared hunk classified in submission §8; each best-effort poll site carries its reason in the generated table; the `ImagePipelineNodeExtract` fix is now the series' own first commit. |
| 5 | The offline checks are not fixed as a list | **Done 2026-09-15:** `docs/offline-checks.md` documents them with commands and expected results; `release_check.py` now reports PASS/FAIL/SKIP and refuses to call a run full when a mandatory suite skipped. Substantiated by a local run: **FULL VERIFICATION, 325 tests**. A GitHub Actions workflow file is provided but not activated (publishing it needs a `workflow`-scoped push); no CI run is claimed. |
| 6 | The series is based on `1d47d7c`; upstream has moved | **Done for this revision 2026-09-15:** rebased onto `7fb102b` (upstream had touched none of the affected paths — clean), built standalone (0 warnings, 107 symbols), backend suites pass, re-exported to `sane/wp3-package/` (tip tree `65a7b8bd…`). At submission time, re-rebase only if upstream moved on the affected paths (runbook). |
| 7 | `scanimage -T` and `tstbackend` neither run nor analysed | **Analysed 2026-09-15** (source read): run `tstbackend -l 1` only (read-only, no motor); document the scan-driving tools as "not run because". **Submission-time mission step 2**, against the rebased build. |
| 8 | Decision | "Send this to SANE" or "not yet, for these reasons". Christian's. **Submission-time mission step 3.** |

Not blockers — documented design choices or future work: the size of the
generated table (its answer is provenance and byte-exact verification,
not a rewrite); whole-strip scanning; the physical button and the
interrupt endpoint; the slide holder (C2, after B2); any colour rendering
change; further timing work; repeats of accepted profiles.

## Known issues (cosmetic, not blocking)

- **The magazine controls render awkwardly in digiKam / KSane.** The
  read-only `magazine` status is a wide text field that truncates its value
  (KSane shows only the tail), and it sits with the `Load film` / `Eject
  film` buttons in a layout KSane lays out oddly — one control looks
  over-long. Scanning and the magazine operations themselves work; this is
  purely how KSaneWidgets draws the options. The code already constrains the
  status to a value list (so KSane does not draw an editable combo) and orders
  the status before the buttons, but the width/truncation remains. A proper
  fix — a shorter status string and option sizing that KSane renders cleanly —
  needs a digiKam session to see the result, so it is tracked here rather than
  changed blind (which would also churn the WP-3 package). Observed on digiKam
  9.1.0 / KSane 26.08 (Test 76).

## C3 — 110 (Pocket Instamatic) film in the strip holder (added 2026-09-19)

Image-side only: no motor, wait, calibration or geometry change
(`docs/film-110-proposal.md` §2). Definition of done:

1. Film model + perforation-anchored detector + `scan --film 110`,
   offline-tested on synthetic fixtures and on the real strip's saved
   scans — **DONE 2026-09-19, corrected 2026-09-20** (Test 86: 22 of 22
   apertures give the eye-read verdict; the review found the crop cut
   0.1–0.4 mm of picture — edge refinement assumed a clear surround —
   now every production edge is within 0.06 mm of its independently
   measured foot, and images are numbered by strip position with
   `--placement A|B` so the same photograph has the same number in both
   placements; `docs/film-110.md` §5, §9.1).
2. **Test 87:** a *second* 110 strip through the two-placement protocol
   with no manual cropping: every photograph delivered as a whole
   product, 17.2 ± 0.4 mm along the transport (lateral = the camera's
   own gate, 13.1 / 13.3–13.7 mm so far), `film110_check.py --edge-check`
   reports no lost picture on any production edge, no image missing an
   edge by eye, the same number for the same photograph in A and B, the
   survey's placement advice right, the free-end warning raised exactly
   where a strip end is. — **DONE 2026-09-20** (Test 87: a second strip
   from another camera; protocol, numbering and sag rule held exactly;
   the lateral edge model failed on this film's *lighter* side border
   and was fixed the same evening, every production edge of both strips
   then at or outside the independently measured foot; owner's eye:
   whole picture present on all four, a little overscan).
3. README says "supported", with n = 2 stated — **DONE 2026-09-20**.

Not in scope: a 110 FEEDL grid, 126 Instamatic, holder-ID detection,
`digitize --film 110` (follow-up once 2 passes).

## Candidates, not scheduled (2026-09-13)

Recorded so they are not lost. None is committed work; each needs a
decision before it starts.

**Whole-strip batch scanning.** The vendor's QuickScan scans all six
frames in one operation ("Processing 4/6"); our backend scans one frame
per `sane_start`, chosen by the `frame` option. The gap is smaller than
it looks:

- **SANE has a protocol for it, but NOT the one stated here earlier.**
  An earlier revision claimed `last_frame = false` was the mechanism.
  That is wrong: in SANE a *frame* is a band of ONE image
  (`SANE_Frame` is GRAY / RGB / RED / GREEN / BLUE — the RED/GREEN/BLUE
  values exist for three-pass scanners), and `last_frame` says whether
  this is the last band of the current image. It has nothing to do with
  the next photograph on the strip. Our backend delivers RGB in a single
  frame, so `last_frame = true` is correct and must stay.
  The multi-image mechanism is the one document feeders use: the
  frontend calls `sane_start` AGAIN after `sane_read` reports the end of
  the current image, and the backend either begins the next image or
  returns a defined end status (`SANE_STATUS_NO_DOCS`). `scanimage
  --batch` drives exactly that loop.
- **The hardware prerequisite is proven.** Test 76 scanned a second
  frame on the same load with `load_document` correctly doing nothing
  and positioning to frame 2's FEEDL. Six frames is that, five more
  times.
- **It already works from a shell loop** — `scanimage --frame 1` … 6 on
  one load — so the capability exists; it is not exposed as one
  operation.
- **Missing:** a mode meaning "all frames"; a frame counter the backend
  advances between successive `sane_start` calls rather than taking from
  the `frame` option each time; a defined end — returning
  `SANE_STATUS_NO_DOCS` once frame 6 has been delivered — so the
  frontend's loop terminates; and a decision about whether the batch
  ejects when it finishes.
- **Cost to weigh:** GL126 recalibrates on every `sane_start` (offset,
  gain, shading) because it never reuses a cache — a deliberate B1
  decision, since a restored cache made `begin_scan` refuse. Six frames
  means six calibrations, roughly 4 s each. Calibrating once per batch
  is possible to investigate but touches exactly the mechanism that was
  switched off on purpose.

**~~Shorten the cold start's opening wait.~~ DONE and PROVEN ON HARDWARE
2026-09-13** (Test 78: eighteen of nineteen polls byte-for-byte
identical to run A, only the opening one changed, 40.1 s → 26.6 s). Test
77's measurement: across two cold starts the OPENING wait never settles —
status word static at 0x48 for the full 15 s, ~1900 polls, first == last
— because at power-on the engine is not in the done class and does not
enter it until the first homing move has run. The PER-ROUND wait, same
mask and target, then settles on its FIRST read in 4 ms, every round of
every run. So 15 s was waiting for something that cannot happen yet.
Now 1.5 s, ~375x the observed settle, in BOTH implementations from one
constant (`of135i.device.COLD_READY_TIMEOUT`, which the generator reads
rather than copies, with a test tying them together). Removes 13.5 s of
the ~40 s at Load film. The six motor completions are a genuine wait
(1.0–1.9 s observed) and were deliberately left at 30 s; so were the reg
0x32 settle polls, which also time out but are only 1.5 s each and match
the driver's own loop — one variable at a time. **Those three were then shortened too
(Test 79), on the same evidence and with the same method**: 1.5 s → 0.25
s, another 3.7 s. Total across both changes: **40.1 s → 22.9 s, 43 % of
the wait gone**, with every genuine wait untouched. Test 79 also found
why reg 0x32 never reaches its target — the round's own last write to
that register clears the bit the condition requires, so it is likely a
transcription slip from the capture. The condition itself was left
alone; only the waiting was shortened.

**~~Mask compensation in the rendering path.~~ WITHDRAWN 2026-09-13.**
It rested on a claim that has since been retracted — that the cast in our
preview positives is caused by the orange mask never being removed. It is
not: `to_positive()` already measures the film base per channel and then
normalises per channel, so the mask is removed by construction and a
constant error in measuring it would cancel anyway. See
`docs/colour-rendering-analysis.md` §5.

The cast itself is real and measured, but it has **no established
cause**, and no colour change should be implemented until it does.

**The vendor rendering of this strip was obtained on 2026-09-13** (Test
80) and it removes our code from suspicion rather than implicating it:
the vendor's own software renders the same strip at B − G ≈ −160 and a
median blue of 22, against our −66.9 and 98. Our renderer understates the
blue deficit; it does not exaggerate it. Bit depth and container were
each excluded by holding one constant while changing the other. That
separation was then made the same evening — a second stock renders
neutrally through unchanged vendor settings (Test 80) — and Test 82
(2026-09-15) cleared the scanner itself: a vendor scan of one strip from
before the project and one taken that day agree within ±1 code per band.
**The colour question is parked as outside the driver's scope
(Christian, 2026-09-15).** What the evidence supports: the cast is real,
it varies between strips, it appears in the vendor's path too, and it has
not been shown to be a defect in our code; the film's properties and the
chosen treatment remain possible explanations. No colour change is
implemented or scheduled.

**The physical Eject button works while the vendor's software is
running** (observed 2026-09-13). That is a data point about where the
button lives, not just a convenience: QuickScan polls the interrupt
endpoint continuously — the same endpoint whose 0x48 event triggers its
automatic load — so the button is almost certainly reported there and
acted on by the application, not by the firmware. Consequences:

- **Our SANE backend does not do this today**: the genesys USB
  abstraction has no interrupt transfer, which is a limit of the current
  implementation (limitation 5 of the submission package), not proof that
  a button feature is impossible within SANE. The SANE-
  shaped answer is the standard one — expose button and sensor as
  read-only sensor options and let `scanbd` poll them — which needs the
  endpoint read to exist somewhere first.
- **The Python driver already reads that endpoint** (it drains EP 0x83
  during the load flow, Test 20), so it is the natural home for a
  button watcher, matching the optional user-service design line that
  has been sketched but not decided.
- **Reinserting the magazine auto-loads it** under the vendor's software
  (observed 2026-09-13): the operator pulls the magazine out and pushes
  it back to the stop, and the load runs with no button pressed. This is
  the operator-side confirmation of the mechanism `docs/sane-wp4-
  magazine.md` describes from the captures — the 0x48 insert event on the
  same endpoint triggers the vendor's LOAD directly, which is precisely
  why our two-step protocol exists: SANE has no background thread, so we
  run LOAD at the next call instead. Same endpoint, same reason.
- **That measurement has now been made** (Test 81, 2026-09-13). A press
  delivers **one byte, 0x48, on EP 0x83**, and the vendor's application
  reacts 0.6 s later with control writes that appear nowhere else in the
  capture; the magazine then drives out and the button's light goes from
  blue to orange. The interrupt precedes every command, so the press
  itself is what is reported. **Seven events across three captures, all
  0x48**, whether caused by a press or by an insert: the notification
  carries no event type, and the vendor's application discriminates by
  reading registers afterwards. A watcher must do the same. It must also
  re-arm quickly — the application re-submits its interrupt read 0.7–1.8 s
  after each completion; whether the device stores a notification that
  arrives inside that window is not established, so a watcher that
  re-arms slowly may lose presses. A button watcher and auto-load remain
  future, undecided features.

**A "no film in this aperture" check (added 2026-09-15, Test 83).**
Aperture coverage verifies positioning from the holder's plastic edges,
so an unexposed frame, a strip that ends mid-holder and an empty aperture
all pass it, are dust-cleaned and are counted as frames. An empty
aperture saturates all three channels across the full 24 mm, which is
easy to detect on the overscan frame. Cheap, host-side, no motor
sequence; not scheduled.
