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
  (Tests 55–61; `docs/holder-position-design.md`). The mounted-slide
  holder is uncharacterised; panorama is a vendor software mode (one
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
   build warnings); **hardware-verification of the SANE backend on
   this geometry is PENDING** (the CLI driver's own hardware
   verification above does not transfer to this separate
   implementation). The overscan window is still delivered whole to
   the SANE frontend (Lager 2's aperture-registered crop + coverage
   check is not ported). The default flip reopened frames 1–6
   for the positioning requirement; the one empty-holder regression
   load re-verified them (Test 59, coverage 6/6).
3. Each of the six scan windows contains its whole aperture with positive
   measured margin on both sides, on hardware, with the empty holder.
4. Load-to-load variation is measured over three separate loads and is
   smaller than that margin.
5. Frames 5 and 6 are hardware-verified: POSITION completes on class F
   inside budget, the scan delivers, PARK completes.
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
    verified, separately from what was measured offline.

**C2 — the mounted-slide holder.** An empty original holder can establish
identification, load and transport, frame count, pitch, the four aperture
positions, scan geometry, repeatability and park/eject. It cannot
establish focus at the film plane inside a mount, sharpness, positive-film
colour or tonal rendering, infrared behaviour on a real slide, or dust
removal. Done when the first list is verified on hardware and the second
is documented as separately unverified, pending a physical slide.

---

## Current status (2026-09-10)

- **M1 — protocol** ✅ and **M2 — driver drives the hardware** ✅.
- **M3 — robustness:** ✅ **complete.** Every row of the A-matrix is met;
  A10 (the residual-dark_b fix) is hardware-verified on B5 (Test 36). No
  open rows.
- **A — own driver:** ✅ **complete.** All acceptance criteria met and
  packaged: tagged **v0.1.0** (45305a4), README install/usage confirmed.
- **B1 — SANE, in progress** (since 2026-09-06): all six profiles are
  implemented and offline-verified (register tables generated from the
  driver's own tables, wire-equal op tests, geometry ledger). Hooks 1–8 are
  built; `sane_open`/calibration/positioning/scan/park run through
  `scanimage`. The calibration-cache item is fixed and hardware-confirmed
  (Test 64) — ordinary scans need no `--force-calibration`. Hardware SANE
  coverage: **plain3600** frames 1–6 (transport + coverage, Test 62),
  **dpi2400** frame 1 (transport + coverage + corrected proportion, eye-
  accepted for geometry, Tests 62/63), and — 2026-09-12 — **dpi600, dpi1200,
  dpi7200 and ir3600** frame 1 each, transport to the ledger, coverage
  verified, eye-accepted (Tests 65–67, 71). The infrared profile took four
  runs and fixed three real bugs (Tests 68–70: an unrequested last chunk, the
  core's Extract node copying a third of each row, and the IR channels'
  colour-line stagger — the last also fixed in the CLI driver, v0.1.2). The
  remaining B1 work is WP-2 (install/packaging + a SANE-frontend scan). The
  two Test-63 image findings
  (mirror, colour cast) are investigated and app-layer, not B1 blockers (see
  the dpi2400 bullet below). Per-profile detail is in the **SANE profile
  matrix**, and the remaining work in the **B1/B2 finite plan**, both below.
  **B2** not started.
- **C — full-length holder, in progress:** the strip holder's six
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
  - Slide holder: PENDING
- **A6 note** (Test 44/46): the driver's eject stalled from a state only
  the backend's first `init()` produced; that `init()` now writes nothing.
  No CLI workflow was ever affected. The stall mechanism itself is an
  open observation, not a blocker.

Milestone A is done; the A6 note records an observation from B1's
bring-up, not a CLI regression. The next action is the remaining B1 hardware
verification per the finite plan below.

## SANE profile matrix (2026-09-12)

SANE-only status. Do NOT read the Python driver's own hardware acceptance
(Tests 55–61) into this table — the backend is a separate implementation.
"Impl+offline" = tables generated + wire-equal op tests + geometry ledger.
"SANE HW" = verified on the device THROUGH the SANE backend. Image acceptance
is the owner's eye rule and is scoped to what was actually judged.

| Profile   | Impl + offline | SANE HW verified | Frames (SANE HW) | Image acceptance (scope) | Concrete remaining for B1 |
|-----------|----------------|------------------|------------------|--------------------------|---------------------------|
| plain3600 | yes | transport + coverage (Test 62) | 1–6 | images seen; no formal eye-accept recorded | one correctly-rendered plain3600 SANE image, owner-approved (the delivery path is the one Tests 65–71 accepted) |
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
all four are done. Not read into this table: the plain3600 row's formal
eye-accept, still to be recorded.

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
- Remaining offline: document the install steps and the `dll.conf`/config; dry-
  run the frontend path where possible without hardware.
- Minimal hardware test: one load→scan→deliver through the installed backend
  and through digiKam (can pigg-back on WP-1's session).
- Stop condition: an install/enumeration/safety deviation stops WP-2; logged.

**WP-3 — SANE submission package, prepared only (B2).**
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
