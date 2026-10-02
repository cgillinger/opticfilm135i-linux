# The upstream submission runbook — one mission, run at submission time

Blockers 6, 7 and 8 of the B2 preparation phase (`docs/ROADMAP.md`) belong
together. The rebase and the package (6, and the shared-fix split from 4)
are prepared now, so a concrete package exists to assess; a final upstream
re-check, the `tstbackend -l 1` conformance run and Christian's go/no-go
run in a single session on the day he decides the code is ready. This
document freezes every decision so that day is execution, not
re-deliberation.

## Where the three stand

**Executed 2026-09-29; it ended in the submission.** Step 1 (the final
upstream re-check): upstream `master` had moved to `ccabaad` (two
commits, touching only `frontend/jpegtopdf.c`), so no rebase was done and
the five patches apply clean on top of it. Step 2 (the conformance run):
`tstbackend -l 1` had been run against v3 and v4 on 2026-09-28 (Tests 93,
94) and was not repeated for v5, whose machine code is identical to v4's.
Step 3 (the decision): Christian decided to send it, and on 2026-09-29
he submitted the series himself as merge request !1032
(<https://gitlab.com/sane-project/backends/-/merge_requests/1032>), from
his own fork, branch `wp3-gl126-submission-v5`. The rest of this document
is kept as the record of how it was prepared. (Afterwards, on 2026-10-02,
v5 was rebased onto upstream `7103e09b0` after upstream merged a GL128
series that touched the same lines: revision v6, four commits, prepared
locally and not yet pushed to the merge request; see
`docs/sane-wp3-submission.md` §1. The steps below describe the
pre-submission mission and are not updated.) The merge request is open
and under review, not merged. The package became v5 that day: the
SANE project's CI pipeline, run on the v4 branch in the fork, failed on
clang-only findings (Test 95).

The rebase and the package preparation (blockers 6 and 4) were **done for
the revision current before the submission** (v4, 2026-09-28; the
submitted package is v5, v4 minus 42 lines of dead code): the series is refreshed from the
repository's `sane/` as of Test 92, its comments cleaned of every internal
citation, rebased onto `f8b5e16`, split so the
shared `ImagePipelineNodeExtract` fix is its own first commit, built
standalone (0 warnings, 111 gl126 symbols), verified by the three backend
suites, and exported to `sane/wp3-package/` with its ids and a
verification table. A concrete package exists for Christian to assess.

What is still version-bound, and so is left for the submission session:

- **A final upstream re-check.** Upstream sane-backends keeps moving. At
  submission time, re-fetch and compare: if it has touched the paths this
  series changes (between `7fb102b` and `f8b5e16` it had, by one line in
  a helper the series does not use — assessed and folded in), re-rebase
  and re-export; if not, the current package stands. Not a wholesale
  redo — only the then-relevant difference is assessed.
- **The conformance run (7) must describe the submitted build.** A
  maintainer's implicit question is "did these pass on the code you are
  submitting?" So `tstbackend -l 1` runs against the final build, after
  any final re-rebase. **Done for v3 and again for v4, 2026-09-28
  (Tests 93, 94): 22 965 checks, 0 warnings, 0 errors, zero writes, both
  times.** Repeat only if the package is re-exported again.
- **The decision (8) follows the evidence (7).**
- **The upstream CI pipeline (added 2026-09-29, for any future
  revision).** Run the SANE project's CI pipeline on the branch before
  submitting it: in the fork, and/or the `make-dist` and
  `fedora-39-clang` jobs locally in the project's CI images
  (`registry.gitlab.com/sane-project/ci-envs`, tags `debian-bullseye-mini`
  and `fedora-39-clang`). A GCC-only build had missed three clang
  `-Werror` findings in v4 (Test 95).

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

**1. Final upstream re-check (blockers 6 and 4 — already prepared).** The
five-commit series is current against `f8b5e16` and exported. Fetch
`origin/master` again; if it has touched `backend/genesys/`, the
`Makefile.am`, `genesys.conf.in`, the `.desc`, the man page or `AUTHORS`
since `f8b5e16`, re-rebase as a real port (read the new upstream, do not
paste), rebuild standalone with zero warnings, re-run the offline checks
against the branch, and re-export to `sane/wp3-package/` with a fresh
revision table. If it has not, the current package stands unchanged.

**2. Conformance run (blocker 7).** With Christian's go, the scanner in a
known idle state (reg 0x01 = 0x22) and nothing else owning the device
(watch for VMware autoConnect), run `tstbackend -l 1` against the final
build. Record the result verbatim. It is read-only; no motor moves.
How it was done for v3 and v4 (Tests 93, 94): build the tool from the package tree
with `make -C frontend tstbackend tstbackend_LDADD="../lib/liblib.la
../backend/libsane-genesys.la -lstdc++" tstbackend_DEPENDENCIES="../lib/
liblib.la ../backend/libsane-genesys.la"` (linked straight to the
package's genesys library — the dll backend would dlopen the installed
one instead), then `./tstbackend -l 1 -r 1` with `SANE_DEBUG_GENESYS=8`
to a file; the default recursion depth 5 does not finish in ten
minutes. Write
the "not run, because …" note for the scan-driving tools. Fold both into
`docs/sane-wp3-submission.md` (§3 and §7).

**2b. Upstream CI pipeline (added 2026-09-29, for any future
revision).** Before the decision, run the project's CI pipeline on the
branch in the owner's fork, and/or the `make-dist` and `fedora-39-clang`
jobs locally in the images named above. All jobs must pass. GCC alone is
not enough: clang 17 with `-Werror` rejected v4 (Test 95).

**3. Present the decision (blocker 8).** Give Christian the go/no-go:
"submit this to SANE" or "not yet, for these reasons." Stop there.

## Hard boundaries (unchanged)

These remain the rule for automated sessions. The submission itself was
made by the owner, not by a session (2026-09-29).

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
