# The upstream submission runbook — one mission, run at submission time

Blockers 6, 7 and 8 of the B2 preparation phase (`docs/ROADMAP.md`) are
**not three separate to-dos.** They are one mission, executed in a single
session, on the day Christian decides the code is ready to be offered
upstream. This document freezes every decision so that day is execution,
not re-deliberation.

## Why they collapse into one mission

Each of the three is version-bound, and doing any of them ahead of the
others wastes the work:

- **The rebase (6) can only be done at submission time.** Upstream
  sane-backends moves; a rebase done now is stale within weeks. It is
  meaningful only against the master that exists the day we submit.
- **The conformance run (7) must describe the submitted build.** A
  maintainer's implicit question is "did these pass on the code you are
  submitting?" A run before the rebase describes a build that is not what
  ships. So it runs *after* the rebase, against the rebased package.
- **The decision (8) follows the evidence (7).**

So there is nothing useful to do on 6, 7 or 8 before that day. Until then
the WP-3 package simply sits reviewable in `sane/wp3-package/`.

## Trigger

Christian says, in his own words, that the code is ready to be offered
upstream. **No session runs any part of this before that.** This is not
implied by "finish B2", "prepare upstream", or any earlier go.

## Frozen decisions (do not re-argue these)

1. **Conformance tools: run `tstbackend -l 1` only.** Verified by reading
   the source (`frontend/tstbackend.c`, 2026-09-15): at test level 0–1 it
   opens the device, gets and sets options, and presses no buttons (it
   allocates no value for `SANE_TYPE_BUTTON`, so `load-film`/`eject-film`
   are never actuated). Our `sane_open` writes nothing and reads one
   register; the sensor hook is a no-op; the `magazine` status line reads
   memory and a disk file, not a register. So level 1 is a **read-only
   hardware session — no motor, no scan, zero writes to the scanner** —
   the same risk class as `of135i status`. It needs the scanner powered
   and on the bus; it is **not** offline.
2. **Do not run `tstbackend -l 2+` or `scanimage -T`.** Both drive the
   motor and cancel a scan mid-pass. On this single, irreplaceable unit
   that needs a power cycle after every aborted pass, and it touches the
   exact undefined-state path the safety model is built to refuse
   (`docs/hardware-safety.md`). Document them as **"not run, because …"**
   with the operations they would request, citing the real scan evidence
   already on record (Tests 62–79, `docs/test-log.md`).
3. **Everything runs against the rebased branch**, never the pre-rebase
   development build.

## The mission, in order (one session)

**0. Preconditions.** Working tree clean; the full offline gate green
(`.venv/bin/python tools/release_check.py`); the WP-3 package as last
exported (`sane/wp3-package/`).

**1. Rebase (blocker 6).** Fetch current `origin/master` in the
sane-backends clone. Rebase the four-commit series onto it. Resolve
conflicts as a real port — read the new upstream and adapt, never paste
the old solution over changed code. Split the `ImagePipelineNodeExtract`
fix into its own commit (blocker 4's remaining item). Rebuild standalone
(`./autogen.sh && ./configure --sysconfdir=/etc`, then `lib`, `sanei`,
`backend/libsane-genesys.la`) with zero warnings. Run the offline checks
against the branch (`docs/offline-checks.md`). Re-export the package to
`sane/wp3-package/` with a fresh revision table (new base, commit and
tree ids).

**2. Conformance run (blocker 7).** With Christian's go, the scanner in a
known idle state (reg 0x01 = 0x22) and nothing else owning the device
(watch for VMware autoConnect), run `tstbackend -l 1` against the rebased
build. Record the result verbatim. It is read-only; no motor moves. Write
the "not run, because …" note for the scan-driving tools. Fold both into
`docs/sane-wp3-submission.md` (§3 and §7).

**3. Present the decision (blocker 8).** Give Christian the go/no-go:
"submit this to SANE" or "not yet, for these reasons." Stop there.

## Hard boundaries (unchanged)

- Nothing is pushed upstream. No merge request, issue, mail, or contact
  with the SANE project or anyone else — not as part of "prepare
  upstream" or "finish B2". Any such act needs a **separate, explicit
  instruction from Christian naming the action and the recipient.**
- Push only to `cgillinger/opticfilm135i-linux`; check the destination
  first.
- The mission **stops at "ready for Christian's submit decision."**
  Prepared is not submitted. B2 is not complete until Christian has
  decided to submit and that act has been performed.

## What this does not touch

The Python driver, the motor sequences, wait times, profiles and image
processing stay unchanged. C2 (slides) and every "candidate, not
scheduled" item stay out. This mission only finalises and offers the
existing, hardware-verified backend.
