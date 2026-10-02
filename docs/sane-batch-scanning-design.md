# Whole-strip batch scanning in the SANE backend (design, 2026-09-28)

Status: **Design only, written 2026-09-28, not implemented, no owner's go
yet.** It was written before Test 92 (the WP-5 hardware run) and names
that test as a prerequisite because it touches sane_start, the same code
WP-5 changed; Test 92 passed later the same day, the text below is left
as written. Nothing here has run anywhere.
Statements about frontends and the SANE standard that were recalled rather
than read in this repository are marked *(unverified)*; statements derived
from reading `sane/gl126.cpp` rather than from a test are marked
*(inferred)*.

## 1. The problem, precisely

The vendor's QuickScan scans all six frames of the strip holder in one
operation ("Processing 4/6"). The backend scans one frame per `sane_start`,
the one the `frame` option names (1–6, `OPT_FRAME`, an INT range). A whole
strip today is six Scan presses with the Frame slider moved in between, or
six `scanimage --frame N` runs. docs/ROADMAP.md ("Whole-strip batch
scanning") lists what is missing: a mode meaning "several frames", a frame
counter the backend advances between successive `sane_start` calls, a
defined end (`SANE_STATUS_NO_DOCS`) so the frontend's loop stops, and a
decision on ejecting at the end.

What is *not* missing:

- **The SANE mechanism.** A frontend calls `sane_start` again after
  `sane_read` has returned EOF for the current image; the backend starts the
  next image or returns `SANE_STATUS_NO_DOCS`. `scanimage --batch` drives
  exactly this loop. `last_frame = true` stays: a SANE "frame" is a band of
  one image (RGB is delivered in one), not a photograph on the strip.
- **The hardware sequence.** Test 76 scanned frame 2 after frame 1 on the
  same load and the same `sane_open`, with `load_document` doing nothing
  and POSITION going to frame 2's ledger FEEDL (17315). The Python driver's
  `scan --frames 1-6` has done six on one load many times (Tests 58–60, 84).
  A batch is that sequence, repeated; no motor program is new.

## 2. Design summary

One new option, `last-frame`. When it is greater than `frame`, the handle
holds a **batch cursor**: each `sane_start` scans the cursor's frame, a
completed PARK advances it, and the `sane_start` after the last frame
returns `SANE_STATUS_NO_DOCS` with no device I/O at all. Every frame is a
complete single-frame scan as today (calibration, POSITION, scan pass,
PARK). The magazine is never touched by the batch. When `last-frame` is at
or below `frame`, nothing about the backend's behaviour changes.

## 3. The design

### 3.1 Option surface

Candidates, judged against the two libksane display bugs
(docs/sane-install.md §6: a value-list combo matches the live value against
item text/translated text; a value-list combo's construction-time default
is matched against item text):

| candidate | KSane widget | verdict |
|---|---|---|
| `frame` gains a value 0 = "all" | slider showing "0" | rejected: changes the meaning of an existing option, "0" is unreadable, and "all" cannot express a partial strip |
| string list `frames` ("1", …, "6", "1-6", "3-6", …) | combo (value list) | rejected: exactly the widget both bugs live in; 21 ranges is also too many items |
| BOOL `whole-strip` ("from Frame to 6") | checkbox | workable, no bug exposure; but a strip that ends mid-holder (Test 83) or an empty trailing aperture still costs full frames |
| **INT range `last-frame`, 1–6, default 1** | slider, same widget as `frame` | **recommended** |

`last-frame` ("Last frame", Film group, directly after Frame): *"Scan every
frame from Frame to this one, one image each. At or below Frame: only Frame
is scanned."* Default 1, so with every existing setting the backend is in
single-frame mode. It uses the widget `frame` already uses, which has
worked live since Test 76 and is not a value-list combo (no text matching
at all). No coupling between the two options: no dynamic constraint, no
clamping, no `SANE_INFO_RELOAD_OPTIONS` from either. English only, like
every string this backend adds. `scanimage` spells it `--last-frame 6`.

Empty apertures are harmless to scan (Test 59, the empty-holder 1–6 run);
`last-frame` exists to save their time, not for safety.

### 3.2 The batch cursor: state and lifetime

Per device, in `gl126.cpp`, beside `scan_pass()` (same keying, same reset
in `init()` at `sane_open`), a genesys-free `BatchCursor` in `gl126_ops.h`
so it is testable like `ScanPass`:

    Off --first sane_start with last-frame > frame--> Running(next=F, end=L, key)
    Running(next=n) --PARK completed for frame n--> Running(next=n+1)
    Running(next=L+1) == Done  --every sane_start--> NO_DOCS, no I/O (sticky)
    any --reset event--> Off

`key` = (profile, scan method, colour mode, `frame`, `last-frame`),
captured at the first `sane_start`.

| event | effect |
|---|---|
| PARK completes (`end_scan`, `ParkDecision::Run` returns normally) | `next++`; logged "batch: frame n done, next n+1 of F-L" |
| a SET that **changes** the value of `frame`, `last-frame`, resolution, source or mode | reset to Off. A SET that repeats the current value does not reset (KSane may re-apply values; *unverified*) |
| `Load film` completes a LOAD; `Eject film` runs | reset to Off (a different strip, or none) |
| `Check status` | no effect (read-only) |
| any failure inside a `sane_start` or `sane_read` of the batch | reset to Off, and the session taint of §7 applies |
| `sane_cancel` after a complete, parked image | **no effect** (see 3.3) |
| `sane_cancel` mid-image | the existing `AbortedPass` path (IO_ERROR, power cycle); cursor reset |
| `sane_close` / new `sane_open` | cursor dropped; never persisted, no new mark kind |

The cursor is in-process only. A new `scanimage` process starts at `frame`
again; that is intended (use `--batch`, §6).

### 3.3 The sane_start loop

**Which frame.** `calculate_scan_settings()` (genesys core, in the patch)
sets `settings.frame = gl126::batch_frame(dev, s->frame, s->last_frame)`:
the cursor's `next` while Running, else `s->frame`. It is a pure read and
never advances anything, because the core calls it from option sets and
`sane_get_parameters` too. This matters beyond FEEDL: the A+C ledger makes
the delivered line count frame-dependent (`frame_geometry(profile, frame)`,
chunk counts 233/233/232/231/231/232 in Test 59), so `sane_get_parameters`
after each `sane_start` must report that frame's height, and before it the
height of the frame the next `sane_start` will scan.

**First `sane_start` of a batch** (cursor Off, `last-frame > frame`):
validate (both in 1–6 — already guaranteed by the ranges, re-checked
before any I/O as `begin_scan` does for `frame`), capture `key`, set
`next = frame`, log "batch frames F-L starts". Then the unchanged path:
`load_document()` (pure checker), offset → gain → shading, POSITION to
frame F, scan pass, PARK at EOF.

**Later `sane_start`s** (cursor Running): the first action, before
`magazine_check_scan_allowed()`'s register read, is the batch check:

- `next > end` (Done) → `SANE_STATUS_NO_DOCS`, zero reads, zero writes,
  cursor stays Done.
- `key` differs from the captured one → treat as a first `sane_start` (a
  path the SET-resets should already have covered; belt and braces).
- `scan_pass()` is not `Parked`, or the session is tainted (§7) → refuse
  `SANE_STATUS_INVAL` before any I/O, reset.
- otherwise identical to a single-frame `sane_start` for frame `next`. On
  the wire the only difference from frame 1 is FEEDL and the line-count
  injection, both from the ledger, as in Test 76.

**`sane_cancel` between images.** After a complete image PARK has already
run inside the last `sane_read` (EOF in `genesys_read_ordered_data` →
`end_scan`), and `dev->parking` is set so the core's cancel path calls
neither `end_scan` nor `move_back_home` (docs/sane-hook5-frame.md §4 table)
— wire-silent *(inferred from the patch and hook-5 notes, not observed in a
log)*. The cursor **survives** it. Rationale: KSane calls `sane_cancel`
after every non-ADF image *(unverified)*; if cancel ended the batch, digiKam
could never advance, and KSane's own timer batch mode would rescan frame F
forever. A document feeder behaves the same way physically: cancel does not
un-feed the pages already taken. `scanimage --batch` works either way.

**The end.** `NO_DOCS` is sticky until a reset event, so a frontend that
ignores it and keeps calling `sane_start` gets `NO_DOCS` again, with no
I/O, instead of a second pass over the strip. To rescan the strip with the
same settings: change Frame (or Last frame) and back, or Eject/Load film.

### 3.4 Where it lives

- `gl126_ops.h/.cpp`: `BatchCursor` (pure state machine, like `ScanPass`).
- `gl126.cpp`: the per-device map; `batch_frame()`; `batch_check_start()`
  called as the first line of `load_document()` (zero I/O, so
  `load_document()` stays a pure checker); the advance in `end_scan()`
  after `it->second.parked()`; resets from `magazine_load_film_impl()` and
  `magazine_eject_impl()`; the status text (3.5).
- `gl126-integration.patch`: `OPT_LAST_FRAME` (enum, `init_options`, get/
  set, inactive on non-GL126), the one-line `calculate_scan_settings()`
  change, and change-detecting resets in `set_option_value()` for the five
  options of 3.2. The WP-3 package must be re-exported afterwards.

### 3.5 Status line

New `magazine` values, all ≤ 40 characters, distinct, untranslated, each an
overlay on Loaded (like WP-5's Check-status override, invalidated by every
real `set_magazine_state()` transition):

- `batch -- next Scan: Frame 2` … `Frame 6` (five values, 27 chars)
- `batch done -- Eject film, or set Frame` (38 chars)

Loaded outside a batch keeps `loaded -- set Frame, press Scan`. Whether
KSane re-reads the status option after a scan (rather than only after an
option SET that returns `RELOAD_OPTIONS`) is **unverified**; if it does not,
the line is stale during a digiKam batch and `Check status` refreshes it.
Test 93 answers this. The debug log always names the frame.

## 4. Interaction with WP-5 and the magazine state machine

- `load_document()` stays what WP-5 §3.4 made it: a checker that never
  loads, never ejects, and reads only reg 0x01. The batch check adds no
  I/O. Every frame of a batch passes through the same check, so a cold
  read between frames (a power cut while the digiKam dialog idles) clears
  the marks and refuses exactly as for a single scan.
- The batch never changes `MagazineState` and writes no mark. A batch
  starts from Loaded (or from Unknown-with-no-mark, the CLI division of
  docs/sane-install.md §7, with the existing warning on every frame).
- `Eject film` between images of a digiKam batch ends the batch (3.2); the
  next Scan then gets WP-5's "press Load film first" `NO_DOCS`. The two
  `NO_DOCS` meanings look identical in libksane ("Document feeder out of
  documents"); the status line and the log tell them apart.
- **Auto-eject at the end: out.** (1) The end of a batch is a `NO_DOCS`
  return, which this design keeps zero-I/O; an eject there would put a
  motor move inside a status return, and an eject folded into the last
  PARK would put it inside the final `sane_read`, where a failure could
  make the frontend drop frame 6's complete image. (2) Rescanning one frame
  (another resolution, IR) is common, and after an eject it costs a full
  `Load film` with its edge wait and the only operator-induced failure
  (`0xfc` at the feed, Tests 48/49/91) this scanner has. (3) "The end" is
  ambiguous when the frontend stops early (`--batch-count`, a KSane
  cancel). (4) WP-4 §7 already excluded ejecting after each frame, and
  `Eject film` is one press. Revisit only as a separate option with its
  own go.

## 5. Calibration and PARK per frame

Kept, per frame, unchanged. The safety model is "calibrate every scan,
PARK after every complete pass": `begin_scan` refuses without this
`sane_start`'s own `CalStage::ShadingDone`, the vendor calibrates every
frame, and GL126 never restores a cache because a restored cache made
`begin_scan` refuse (B1). Calibration happens at the post-PARK position
over the open area ahead of the film, so it is film-independent (Test 60).
Every frame therefore starts from the verified post-PARK state (reg 0x01 =
0x22, 0x101 = 0xf8), which is what hook 2's S0 check requires.

Cost, from logged timings:

| item | figure | source |
|---|---|---|
| calibration (offset+gain+shading) | ≈ 4–5 s | ROADMAP; Test 52 timeline |
| POSITION, frame 1 (SANE) | 1.43 s | Tests 65, 66, 91 |
| POSITION, frames 1–6 (driver) | 1.8 / 3.8 / 6.0 / 8.1 / ≈10.2 / 12.4 s | Test 21 (1–4), Tests 58–60 (6); frame 5 interpolated |
| semantic PARK | 3.7–3.8 s, flat over frames 1–6 | Tests 65–71, 90 (3837 ms); Test 84 (driver, semantic, six frames) |
| whole 600 dpi frame 1 (`scanimage` wall) | 16.6 s | Test 65 |
| 3600 plain scan pass (SANE) | ≈ 46 s | Test 52 |

Estimate *(inferred)*: six frames at 600 dpi ≈ 6 × 16.6 s + ≈ 31 s of
extra POSITION travel for frames 2–6 ≈ **2.2 min**, of which calibration
is ≈ 25–30 s (~20 %). At 3600 plain ≈ 6 × (4.5 + 46 + 3.8) s + 42 s of
POSITION ≈ **6.1 min**, calibration ≈ 7 %. Calibrating once per batch would
save at most ≈ 20–25 s per strip — not a reason to reopen the mechanism B1
switched off on purpose. Dual profiles differ: the driver's verbatim 2400
PARK ran ≈ 50 s (the DPI→DPI check before Test 32) and 65 s (Test 61); the
SANE semantic 2400 PARK time is not in the
logs read for this design.

## 6. Frontend behaviour

| frontend | what happens | status |
|---|---|---|
| `scanimage --batch=f%d.tif --batch-start=F --frame F --last-frame L` | loops `sane_start`/`sane_read`, one file per frame, stops at `NO_DOCS` and exits 0 | loop: standard `scanimage` behaviour; exit 0 on `NO_DOCS` after ≥ 1 image and no `sane_cancel` between images: *(unverified, recalled from scanimage.c)* |
| `scanimage --batch` in single-frame mode | rescans the same frame until an error or `--batch-count` — today's behaviour, unchanged | *(inferred)* |
| digiKam / KSane, batch mode off | one image per Scan press; the frame advances by itself; the press after frame L shows libksane's `NO_DOCS` text | KSane loops only for an ADF-named source or its timer batch mode, and calls `sane_cancel` after each image: *(unverified, recalled from libksane)* |
| KSane timer batch mode ("off unless switched on", sane-install §6) | `sane_start` repeats after the delay; frames F..L, then `NO_DOCS` | whether it stops or shows an error at `NO_DOCS`: *(unverified)* |
| xsane, others | any frontend that follows the ADF convention gets F..L then `NO_DOCS` | *(unverified)* |

What the digiKam user sees *(inferred)*: set Frame 1, Last frame 6, press
Scan six times; each frame arrives as a separate image under digiKam's own
numbering (Test 76: `image2.tif`, `image3`), not the frame number, so order
is the only link; heights differ slightly per frame (ledger); each press
spends the silent calibration and POSITION time before data starts. The
backend's source stays `Transparency Adapter` — renaming it to trigger
KSane's ADF loop is rejected (it would change a standard name and rely on a
frontend heuristic).

SANE standard, §4.3.9 (`sane_start`) and the code-flow section *(recalled,
not checked against a copy this session)*: `sane_start` may return
`SANE_STATUS_NO_DOCS` ("document feeder out of documents"); after EOF of the
last frame of an image the frontend may call `sane_start` for the next
image; `sane_cancel` is required before the handle is used for anything
else once acquisition ends. The design is valid whether or not the
frontend calls `sane_cancel` between images.

## 7. Failure modes

| case | behaviour |
|---|---|
| POSITION misses its class-F completion on frame k (FEEDL-scaled budget) | as today: `sane_start` fails, the transport is somewhere on the path, no PARK (`NoPass`), frames F..k−1 are complete files; batch reset; recovery power cycle + Load film, then a new batch `frame = k` |
| **gap found while writing this *(inferred from `begin_scan`)*** | a POSITION or calibration failure does not mark `ScanPass` Failed, and `validate_scan_request()` does not consult it; a later `sane_start` on the same handle would run calibration writes (if reg 0x01 still reads 0x22) and POSITION again from an unknown position. Today only a human re-press reaches this; a frontend loop could. Fix in this package: a per-device **taint** set by any throw after the first write of a `sane_start`, and a refusal before any I/O while tainted or while the pass is Failed; cleared only by a new `sane_open` (whose hardware gate already exists) |
| residual dark_b on frame k (ROADMAP A10, Test 32) | today: `SANE_STATUS_IO_ERROR` after writes, i.e. power cycle mid-batch. The residual appears on later frames of one session (f2/f4, f2/f3/f4 on B5; frame 1 always healthy; none in Tests 59–60), so a batch is exactly where it bites. Recommended: port A10 (HW-confirmed in the driver, Test 36) — remember the last healthy dark_b per device, same profile, same `sane_open`; substitute and log; fail closed when none. It changes a calibration *input* only, as in the driver; owner's decision (§11) |
| mid-image cancel / short read | unchanged `AbortedPass` / Failed path; batch reset |
| PARK failure after frame k | unchanged: pass Failed, the final `sane_read` reports it; batch reset |
| lock conflict | none mid-batch: the process `flock` is held from `sane_open` to `sane_close`, so no `of135i` or second SANE process can touch the device between frames |
| mark conflict | cannot change mid-batch except through this handle's buttons (which reset the batch); a stale `loaded` mark at batch start is WP-5 §3.4's case, handled identically |
| cold read between frames | `load_document()`'s reg 0x01 read clears marks, refuses; batch reset |

## 8. What this design does NOT change

- Motor programs: cold_init, open, jog, load, eject, POSITION, scan setup,
  PARK — byte-identical, same poll conditions and timeouts. A batch of
  frames F..L is on the wire exactly the single-frame scans of F..L in
  order on one handle (to be proven offline, §9).
- No write from an unknown state: every frame starts from post-PARK 0x22
  under hook 2's S0 check; the `NO_DOCS` path does no I/O; the taint adds a
  refusal, never a write.
- `load_document()`: still pure (no load, no eject, one reg 0x01 read); the
  batch never loads or ejects anything.
- Calibration per frame and the `CalStage` interlock; `ScanPass` (PARK only
  from Complete); no cache reuse.
- Single-frame mode (`last-frame` ≤ `frame`, the default): identical
  behaviour, including what `scanimage --batch` does today.
- No new mark kind, no cursor persistence, no auto-eject, no translations,
  no custom frontend, the Python driver untouched.

## 9. Offline tests to add

`tests/test_sane_ops.py` (`BatchCursor`, probe command `batch`; scripted
`Wire`):
`test_batch_cursor_walks_first_to_last_then_no_docs`,
`test_batch_cursor_advances_only_on_a_completed_park`,
`test_batch_cursor_done_is_sticky_until_reset`,
`test_batch_cursor_last_frame_at_or_below_frame_is_single_mode`,
`test_batch_wire_equals_repeated_single_frames`.

`tests/test_sane_magazine.py` (real backend, write/read counters):
`test_batch_end_returns_no_docs_without_any_io`,
`test_batch_resets_on_eject_and_on_load_film`,
`test_batch_resets_on_a_value_change_not_on_a_repeated_set`,
`test_batch_survives_cancel_after_a_parked_image`,
`test_batch_status_values_are_pinned`,
`test_a_failed_pass_refuses_the_next_start_before_any_io`,
`test_a_position_failure_taints_the_session`,
`test_single_frame_mode_is_unchanged`.

`tests/test_sane_geometry.py`:
`test_public_last_frame_option_constraint_is_1_to_6`; the existing
`test_load_document_is_pure_and_never_moves_the_magazine` must still pass.

`tests/test_sane_open_params.py`:
`test_parameters_follow_the_batch_cursor_per_frame`.

If A10 is ported: `test_residual_dark_b_substitutes_the_same_profiles_healthy_one`,
`test_residual_dark_b_without_a_healthy_one_still_fails_closed`,
`test_healthy_dark_b_is_forgotten_on_profile_change_and_close`.

Same harness limit as WP-5 §9.3: the silent mock cannot complete a real
calibration, so the full loop is proven op-level (scripted `Wire`) and the
state machine/option plumbing through the built backend.

## 10. Hardware plan (Test 93, placeholder, owner's go required)

Prerequisites: Test 92 PASS; this design implemented and reviewed offline;
one power-on; 600 dpi throughout (≈ 2 min per strip); `SANE_DEBUG_GENESYS=8`.

- **A. scanimage.** `scanimage -n --load-film` (reseat), then
  `--frame 1 --last-frame 6 --resolution 600 --batch=f%d.tif
  --batch-start=1`. Expect six files; log: six calibrations, POSITION to
  the 600 ledger FEEDLs 6519 / 17272 / 28031 / 38809 / 49541 / 60256 in
  order, six PARKs ending in class E/F, then one `sane_start` answered
  `NO_DOCS` with no register read or write after the sixth PARK; exit code
  recorded; heights per frame equal the ledger.
- **B. Partial and single.** Same load: `--frame 3 --last-frame 4` → two
  files; then plain `--frame 5` → one frame (single mode unchanged).
- **C. digiKam.** Frame 1, Last frame 6, Scan × 7: six images, then the
  `NO_DOCS` text; record whether the status line followed each image.
  Change Frame to 2 → Scan → frame 2. Eject film.
- **D. (optional)** KSane timer batch mode on, one strip: record whether it
  stops cleanly at `NO_DOCS`.
- A residual dark_b is not provoked; if one occurs, record it (and the
  substitution line if A10 was ported).

**Stop condition:** any deviation — a refusal, a frame or FEEDL out of
sequence or repeated, POSITION outside its budget, a PARK not ending in
class E/F, any I/O after a `NO_DOCS`, an `IO_ERROR`. Stop pressing Scan;
recovery is the standing one (power cycle, Load film). Acceptance: A and C
complete as written, no write after `NO_DOCS`, no `0xfc`.

## 11. Relation to other packages, and open questions

After WP-5 (Test 92); modifies `load_document()`'s body (one pure call),
`end_scan()` (one advance) and the patch (option + settings + resets), so
docs/sane-wp3-submission.md §8 and its "No batch scanning" limitation, the
README digiKam cheat sheet, docs/sane-install.md §6 ("leave batch mode
off") and ROADMAP's entry change with it. The Python driver keeps its own
`--frames`.

Open questions for the owner:

1. Cursor survives `sane_cancel` after a complete image (recommended,
   needed for digiKam) vs ends at cancel (strict reading, digiKam cannot
   batch).
2. Port A10's dark_b substitution in this package (recommended) or keep
   fail-closed for v1.
3. The session taint of §7: part of this package (recommended — it also
   closes the gap for single frames) or a separate fix first.
4. `last-frame` INT (recommended) vs a `whole-strip` checkbox.
