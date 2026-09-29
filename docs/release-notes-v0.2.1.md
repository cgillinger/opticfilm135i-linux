# v0.2.1 — the state submitted to the SANE project

Release notes for the `v0.2.1` tag (2026-09-29) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.0](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.1/docs/release-notes-v0.2.0.md).

## What this release is

A maintenance release with no functional change. It marks the state of the
repository that matches what was submitted to the SANE project, and it
corrects the one statement in the v0.2.0 notes that is no longer true:
v0.2.0 said the submission was prepared and not sent.

**The SANE backend is submitted.** On 2026-09-29 the owner opened merge
request !1032 against the SANE project's `backends` repository
(<https://gitlab.com/sane-project/backends/-/merge_requests/1032>), from
his own fork. It is open and under review. It is **not merged**, and
nothing here should be read as acceptance by the SANE project. Until it is
merged, the backend is installed from this repository as before
([docs/sane-install.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.1/docs/sane-install.md)).

**Dead code removed from the backend.** The SANE project's CI pipeline
builds with clang and `-Werror` in one of its jobs. That job rejected the
v4 series: a helper nothing called carried the `[[maybe_unused]]`
attribute, which is a C++17 extension in a C++11 build, and two constants
were never read. GCC accepts all three silently, which is why every
earlier build had been clean. The 42 lines are removed from
`sane/gl126.cpp`. The disassembly of the compiled object is identical
before and after, and the exported symbols are the same.

**Submission package v5.** The exact five-commit series that was
submitted is in
[sane/wp3-package/](https://github.com/cgillinger/opticfilm135i-linux/tree/v0.2.1/sane/wp3-package)
as a bundle and as patches; both routes recreate the same tree.

## What did not change

- The Python / pyusb command-line driver: no code change. Only its version
  string moved.
- Motor sequences, wait conditions, timing, scan profiles, calibration and
  image handling, in both the driver and the backend.
- The supported hardware and its limits: one unit, the strip holder, as
  described in the v0.2.0 notes.

## Verification

- The SANE project's CI pipeline on the submitted branch: all seven jobs
  pass (the distribution build with the style check, five compile jobs
  including clang, and the distribution check). Passing CI is not review.
- The offline suite: 389 passed, 0 skipped. The three backend suites pass
  against the v5 build. The standalone GCC build has 0 warnings.
- No hardware run was made for this release. Because the compiled code is
  identical to the v4 build, the hardware evidence and the `tstbackend -l 1`
  result recorded for v0.2.0 stand for it; they were not repeated. The
  record is Test 95 in
  [docs/test-log.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.1/docs/test-log.md).

## Changes since v0.2.0

The full list is the `v0.2.1` section of
[CHANGELOG.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.1/CHANGELOG.md).
