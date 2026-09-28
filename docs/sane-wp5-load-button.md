# WP-5 — One-button loading: the backend waits for the reseat (design, 2026-09-27)

Status: **design only, nothing implemented, nothing run.** Written after
the two live digiKam sessions of 2026-09-27 (docs/test-log.md Test 91):
the owner's verdict on the two-step protocol of docs/sane-wp4-magazine.md
was that nobody can operate it in a SANE frontend without a written
manual, and — worse — that a plausible wrong sequence (Load film pressed
twice, then Scan) leaves the scanner needing a power cycle. This package
removes that trap and reduces the operator's rule to one sentence.

## 1. The problem, precisely

WP-4's protocol is two calls because SANE gives a backend no way to ask
the operator for anything during `sane_start`: `Load film` jogs and
returns; the operator takes the magazine out and pushes it back in to
the stop; the *next* `sane_start` runs LOAD. Three consequences showed
up live:

1. Nothing in the dialog says "now take it out and push it in, then
   press Scan on the other tab" in a way an operator sees (the status
   line was grey; KSane shows the description only as a tooltip).
2. LOAD runs on the next Scan **whether or not the reseat happened**.
   The loader sensor (reg 0x101 bit 0x08) reads *present* for a loose
   magazine too, so the backend cannot tell "reseated" from "still
   loose". LOAD on a loose magazine fails at the feed (`0xfc`, Tests
   48/49/91), the session goes Failed, and a power cycle is required.
3. A second `Load film` press re-jogs and un-seats a magazine the
   operator had just reseated (Test 91: exactly this).

## 2. The observation that makes one button possible

The loader sensor cannot tell *seated* from *loose*, but it can tell
*out* from *in*: bit 0x08 clears when the magazine leaves the slot and
sets again when it is pushed back (Test 90/91 logs: 0xf0 clear, 0xf8
present; the probe seeds the same values). "Take it out, push it in" is
therefore an observable **edge**: present → clear → present. The vendor's
application uses the same event (the 0x48 insert notification on EP 0x83,
Test 81, docs/sane-wp4-magazine.md §7) to run LOAD by itself; genesys has
no interrupt transfers, but polling reg 0x101 carries the same
information.

And a SANE option handler may take time: genesys' own `Calibrate` button
runs a full calibration inside the button press, and several backends
implement `wait-for-button` by blocking inside `sane_start` until the
operator presses the scanner's physical button. Waiting for the reseat
edge inside `Load film` is the same class of behaviour.

## 3. The design

### 3.1 One rule for the operator

> **Press Load film first, then handle the magazine.** The motor runs
> when you are done. Then set Frame and press Scan.

Same button after `Eject film`: press Load film, swap the strip, push
the magazine in to the stop; it loads without the jog.

### 3.2 `Load film`, by starting state

| state at the press | what the button does | ends in |
|---|---|---|
| Unknown, reg 0x01 cold (0x00) | cold start → OPEN → JOG → **wait for the edge** → LOAD | Loaded |
| Unknown, idle (0x22) | OPEN → JOG → wait for the edge → LOAD | Loaded |
| Ejected (in-process or "ejected" mark) | wait for the edge → OPEN → LOAD (no jog; WP-4 §10) | Loaded |
| Released (a previous press timed out) | if that wait saw NO clear at all: JOG again (the latched-magazine case, Test 51/77), then wait; otherwise wait only → LOAD | Loaded |
| Loaded | refused read-only: "already loaded -- press Scan or Eject film" | unchanged |
| Failed | refused read-only, as today | unchanged |

**The edge wait**: poll reg 0x101 every 100 ms; require *clear* on 2
consecutive reads (200 ms), then *present* on 5 consecutive reads
(500 ms), then a 600 ms settle (the vendor's own insert-to-LOAD delay,
Test 81) before LOAD. Reads only, no write on the wire during the wait.
Timeout **120 s**. On timeout: no LOAD, state → Released, the on-disk mark
says `released` and **the next Scan refuses** (§3.4); the status line
says "press Load film, then take out and push in" (or, when the sensor
never cleared, "did not come loose? press Load film again").

The JOG itself is unchanged, and so are OPEN and LOAD (byte-identical
programs, hardware-verified Tests 75–77, 90, 91). The only new thing on
the wire is a read-only poll loop between them.

### 3.3 `Eject film`

Unchanged sequence. Status afterwards: "ejected -- swap strip, then Load
film". Mark `ejected` (so a later process knows the jog can be skipped).

### 3.4 Scan never runs LOAD any more

`load_document()` stops being a loader. It only checks:

- Loaded in-process, or a `loaded` mark for this device → proceed.
- `released` or `ejected` mark, or in-process Released/Ejected → refuse
  `SANE_STATUS_NO_DOCS`, read-only: "press Load film first". This is the
  refusal that replaces the trap.
- Unknown with no mark → **as today**: proceed with the "no magazine state
  known" warning. This keeps the documented CLI division working (`of135i
  load` then a SANE scan, docs/sane-install.md §7); it never runs LOAD, so
  it cannot produce the `0xfc` failure.
- Failed → refuse, as today.

The `loaded` mark is new: written when LOAD completes, cleared by eject,
by any failure, and — like every mark — ignored when the device key does
not match (a power cycle re-enumerates) or when reg 0x01 reads cold.

### 3.5 `Check status` button (owner's request)

A third button that is allowed to read the hardware (the status line's
GET deliberately never does): reg 0x01, reg 0x101, regs 0x3b/0x3c. It
updates the status line with what the scanner says and reconciles the
in-process state:

| hardware says | status line |
|---|---|
| reg 0x01 = 0x00 | "cold -- press Load film" (state → Unknown, marks cleared as stale) |
| sensor clear | "no magazine in the slot" |
| idle, sensor present, state Loaded / `loaded` mark | "loaded -- set Frame, press Scan" |
| idle, sensor present, anything else | "magazine present, not loaded? Load film" |
| anything else | "unknown state -- power-cycle, Load film" |

Honest limit, stated on the line itself: the hardware cannot distinguish
*loaded* from *loose in the slot*; the button never claims "loaded" on
hardware evidence alone.

### 3.6 Status values (all ≤ 40 characters, all distinct)

"not loaded -- press Load film", "press Load film, then take out, push in",
"did not come loose? press Load film again", "loaded -- set Frame, press
Scan", "ejected -- swap strip, then Load film", "failed -- power-cycle,
then Load film", "cold -- press Load film", "no magazine in the slot",
"magazine present, not loaded? Load film", "unknown state -- power-cycle,
Load film". English only (docs/sane-install.md §6, the libksane
translation trap). Rendered enabled, not grey (the status option is
settable-as-no-op, same evening's change).

### 3.7 scanimage

`scanimage -n --load-film` now blocks until the operator has done the
reseat (or 120 s), prints nothing meanwhile, and exits 0 with the
magazine loaded; the next invocation scans. `--eject-film` unchanged.
`--check-status` prints nothing either; read the value with `-A`.
Documented in docs/sane-install.md.

## 4. What does NOT change

Motor programs (cold_init, open, jog, load, eject), every poll condition
and timeout inside them, calibration, POSITION, PARK, the image path, the
Python driver. The trade-off of WP-4 §10.3 stays: a magazine resting
against the mechanism instead of pushed to the stop reads *present*, and
LOAD on it fails at the feed. That failure is now reachable only through
that physical mistake, never through a button sequence.

## 5. Frontend behaviour to document, not fix

KSane runs option sets on its GUI thread: the dialog is unresponsive
while `Load film` waits (up to 120 s). The operator is handling the
magazine during that time; the status line updates when the button
returns (RELOAD_OPTIONS). Closing the dialog mid-wait is queued until the
wait ends. Scan is still on the Basic tab. A frontend with real prompts
is not a backend concern (owner's decision: no custom frontends).

## 6. Offline verification before any hardware

- Probe scenarios (tests/gl126_magazine_probe.cpp) drive the edge wait
  through a per-poll `test_checkpoint("gl126_magazine_edge_poll")` whose
  callback re-seeds reg 0x101: present→clear→present (load runs),
  present only (timeout, no LOAD, Released, Scan refuses), clear only
  (timeout with "did not come loose"), Ejected + edge (open then load, no
  jog), Loaded + press (refused read-only), Released-no-edge + press (jog
  again), cross-process `loaded` / `released` / `ejected` marks at Scan.
- Wire-likeness: the programs are unchanged, so the existing byte-identity
  tests keep proving them; a new test asserts the wait sends **no write**.
- `OF135I_SANE_POLL_CAP_MS` (test mode only) caps the 120 s wait.
- Status values pinned, ≤ 40 chars, distinct.

## 7. Hardware plan (Test 92, owner's go required)

Same power-on, digiKam started with `SANE_DEBUG_GENESYS=8`, listening:

- **A. The rule.** Cold scanner, magazine loose in the slot. Press Load
  film, THEN take out / push in → loads by itself (log: jog, edge wait
  with clear then present, LOAD f4/dc first poll); status "loaded -- set
  Frame, press Scan"; Scan 600 → normal frame; Eject film; press Load
  film, swap, push in → loads with no jog; Scan; Eject film.
- **B. The traps, read-only.** Press Load film and do nothing → returns
  after 120 s, status "press Load film, then take out, push in", no LOAD
  in the log; press Scan → refused, no motor; press Load film again and
  reseat → loads. Press Load film while loaded → refused.
- **C. Check status** after each step; after a power cycle it must say
  "cold -- press Load film".
- **D. Cross-process:** `scanimage -n --load-film` (blocks; reseat) then
  a plain `scanimage` scan.

Stop on any deviation; recovery is the standing one (power cycle, Load
film). Acceptance: A and B complete as written, no `0xfc` anywhere, and
the operator needed nothing but the one rule.

**Result (Test 92, 2026-09-28): PASS**, parts A–D as written, on the
build of c5fc847 installed that day. Five loads on one power-on (one
cold with jog, four from Ejected/Released without a jog), feed 0xf4 and
traverse 0xdc on the first poll every time; both traps caught read-only
(120 s timeout -> "did not come loose? Load film again", Scan refused
with `SANE_STATUS_NO_DOCS` after one register read, Load film while
loaded refused with `SANE_STATUS_INVAL`); Check status correct after
every step including "cold" after the power cycle; the cross-process
case (`scanimage -n --load-film` honouring digiKam's `ejected` mark, then
a plain scan on the `loaded` mark, then eject) complete. Full table in
`docs/test-log.md` Test 92. The §5 frontend limits were all seen live
(instruction visible only for an instant, button errors invisible in
digiKam, the NO_DOCS dialog text is digiKam's own).

## 8. Relation to the other packages

Supersedes the two-call protocol of WP-4 §3 and §10 for the frontend
path (WP-4's programs and preconditions remain the building blocks). The
WP-3 submission package was re-exported after this (2026-09-28, v3, once
Test 92 had passed; its §8 carries the new hunks). The Python driver's `of135i load` keeps its Enter prompt; it
could adopt the same sensor edge later (candidate, not part of WP-5).

## 9. Implementation notes (2026-09-27, offline)

Implemented exactly as designed above, entirely offline: **no scanner
contact, no install, no `sudo`.** Build: 0 warnings, patch regenerated
and verified byte-identical to the clone's live `git diff`. `.venv/bin/python
tools/release_check.py`: 380 tests passed (was 346 before this task),
0 skipped; the run's only complaint is the expected "uncommitted changes
in the checkout" (the patch and the WP-5 sources are new/modified,
correctly flagged).

### 9.1 Files

- `sane/gl126.cpp` -- the bulk of the work: `wait_for_magazine_edge()`
  (the plain read-only poll loop, §3.2, with its own gated timeout budget
  `edge_wait_timeout_ms()` mirroring `magazine_policy()`'s test-mode-only
  cap); `magazine_load_film_impl()` (replaces `magazine_release_impl()`
  -- renamed because it no longer just releases, it runs the whole flow);
  `magazine_check_scan_allowed()` (replaces `magazine_load_if_pending()`
  -- renamed because it no longer loads anything, only checks); the new
  `magazine_check_status_impl()` (§3.5); a per-device `saw_clear` flag
  (`magazine_saw_clear_map()`) and a per-device status-line override
  (`magazine_check_override_map()`, consulted first by
  `magazine_state_text()`, invalidated by every real `set_magazine_state()`
  transition); the twelve `kMagazine*` status constants moved earlier in
  the file (an unnamed namespace's members are ordinary, internally-
  linked members of the enclosing `gl126::` namespace, so they only need
  to be declared before their first use, which is now inside
  `magazine_check_status_impl()`, well before the old location).
- `sane/gl126.h` -- `magazine_release()` renamed `magazine_load_film()`;
  new `magazine_check_status()`; doc comment rewritten around the one-
  button flow.
- `sane/gl126_lock.{h,cpp}` -- `MagazineMarkKind::Loaded` added (third
  kind); `magazine_mark_kind_name()` and the two-argument
  `magazine_mark_read()`'s parser extended for `"loaded"`. The one-
  argument `magazine_mark_write()`/`magazine_mark_read()` overloads are
  UNCHANGED (still Released-only), per the task's binding instruction --
  `tests/gl126_lock_probe.cpp` and `tests/test_sane_lock.py` call them and
  needed no edits.
- Genesys core (`backend/genesys/genesys.h`/`genesys.cpp` in the clone,
  captured in `sane/gl126-integration.patch`): `OPT_CHECK_STATUS` added to
  the option enum (between `OPT_EJECT_FILM` and `OPT_FRAME`, so display
  order matches the spec) and to `init_options()`/`set_option_value()`'s
  inactive-on-non-GL126 block; `OPT_LOAD_FILM`'s handler now calls
  `gl126::magazine_load_film()`; `OPT_MAGAZINE`'s and `OPT_LOAD_FILM`'s
  `desc` text rewritten for the one-button rule.
- `backend/genesys/test_usb_device.{h,cpp}` (clone, in the patch) -- a
  small `out_transfer_count()` counter, incremented on every OUT control
  transfer and every `bulk_write`, reset never (one probe process, one
  scenario). Added so a test can assert "the edge wait itself put nothing
  on the wire": genesys had no existing mechanism for that.
- `backend/genesys/test_scanner_interface.{h,cpp}` (clone, in the patch)
  -- (a) exposes that counter (`out_transfer_count()` forwarding to
  `usb_dev_`); (b) seeds regs `0x3b`/`0x3c` to `0x00` for GL126 in the
  test-mode constructor, alongside the existing `0x01`/`0x101` seeds --
  needed because `magazine_check_status_impl()` reads all four registers
  unconditionally (per this task's own instruction 5) and
  `RegisterCache::get()` throws for an address that was never written,
  unlike real hardware, which always answers a register read with
  something. A test-harness-only fix; production code is unaffected (a
  real device never has an "unset" register).
- `tests/gl126_magazine_probe.cpp` -- `call_hook("release")` now calls
  `gl126::magazine_load_film()` (the STRING `"release"` is kept, as
  internal probe vocabulary only, to avoid a purely mechanical rename
  across every existing scenario and every caller in
  `tests/test_sane_magazine.py`); a `"check-status"` hook added; the edge
  wait's own checkpoint (`"gl126_magazine_edge_poll"`) drives a scripted
  register sequence (`set_edge_script()`/`set_edge_script_resolve_from_
  present()`/`..._from_clear()`) and snapshots/checks the OUT-transfer
  counter, printing `EDGEWRITE 0|1`; several WP-4 scenarios were
  repurposed in place (same C++ block, new meaning) since `load_document()`
  no longer reads hardware -- see §9.3.
- `docs/sane-wp4-magazine.md` §11 (new), `docs/sane-wp3-submission.md`
  §8 (this task's item (f) -- see below), `docs/ROADMAP.md`,
  `CHANGELOG.md`, `docs/sane-install.md`, `README.md`: updated as listed
  in §9.4/§9.5 below.

### 9.2 Decisions taken where the spec left a detail open

1. **The two timeout messages' mapping** (§3.2's parenthetical): "did not
   come loose? press Load film again" is used when the sensor NEVER read
   clear during the wait (`EdgeWaitOutcome::TimeoutNoClear`); the default
   "press Load film, then take out, push in" is used when it DID clear at
   least once but never came back present in time
   (`TimeoutSawClear`). This reading follows §3.2's own explicit
   conditional ("when the sensor never cleared") over §6's looser
   scenario-naming shorthand ("clear only"), which this report flags as
   the one place the two sections could be read either way.
2. **A timeout returns `SANE_STATUS_GOOD`, not a non-GOOD status** (§3.2
   asks for a report either way): implemented as specified. The status
   line is genuinely the only channel (§5), and a timeout is the operator
   doing nothing (or not finishing) with the magazine, not a backend
   error -- `SANE_STATUS_GOOD` matches how the *rest* of this backend
   already treats "nothing happened, here is the state" (`eject-film`'s
   "nothing to do" branch is the same shape).
3. **`Ejected` (in-process or mark) at "Load film" is now REACHED
   directly by the edge wait, no jog** -- exactly the spec's table --
   but the WP-4 §10.3 register preconditions (reg 0x01 idle-homed, regs
   0x3b/0x3c) that used to gate the OLD `load_document()`'s Ejected-kind
   load are not mentioned again in the WP-5 spec's §3.2 table. They are
   NOT dropped: they now run inside `magazine_load_film_impl()`
   immediately after the edge is `Seen`, for every kind (Ejected AND
   Released). **Corrected 2026-09-27 (review round two, finding A):**
   the wait's own "Seen" outcome subsumes only the PRESENCE bit the old
   one-read sensor precondition checked (bit 0x08), not the whole status
   CLASS -- a magazine pulled back out during the 600 ms settle, or a
   present-but-busy class, would have passed the wait's own five-reads
   check without ever being re-verified. The post-edge check now re-reads
   reg 0x101 too and requires `present() && idle_class()` alongside reg
   0x01/0x3b/0x3c -- says nothing, either way, about the register state
   OPEN/JOG or a prior eject left behind (Test 90's finding), which is
   what the regs check is for. Kept as a regression guard
   (`test_the_regs_check_after_the_edge_still_refuses_the_base_table_
   state`, `test_the_post_edge_check_catches_a_late_class_or_presence_
   change`).
4. **A cold reg 0x01 read at "Load film" time always forces the full
   path** (cold_init, then OPEN, then JOG), even when the in-process state
   or a mark says Ejected/Released -- not spelled out in §3.2's table,
   which does not consider a power cycle happening between the eject/an
   earlier timeout and this press. Justified by WP-4 §10.3's own
   reasoning for refusing a cold next-strip load: the no-jog shortcuts
   assume the transport is still homed and positioned from the SAME
   power-on. Regression-tested
   (`test_a_cold_read_forces_the_full_release_path_even_from_ejected`).
5. **A cross-process "released" mark is NOT given the retry treatment**
   (jog-again-if-no-clear-was-seen): that decision needs the in-process
   `saw_clear` flag, which cannot survive a process boundary. A "released"
   mark found in a fresh process is treated like any other non-cold
   Unknown press (full OPEN + JOG) -- always safe (WP-4: "writing the open
   table again is harmless"). A cross-process "ejected" mark, by
   contrast, IS honoured for the no-jog shortcut, because §3.2's table
   says so explicitly ("Ejected (in-process or 'ejected' mark)").
6. **`Check status` reads all four registers unconditionally** (reg 0x01,
   0x101, 0x3b, 0x3c) as instructed, even though §3.5's table only
   branches on the first two; 0x3b/0x3c are logged for the diagnostic
   record. This is also why the test-mode default-register fix (§9.1)
   was needed.
7. **The status-line override mechanism**: §3.5 says Check status
   "updates the status line" but only the cold row is a real
   `MagazineState` transition. The other rows (`no magazine in the slot`,
   `magazine present, not loaded?`, `unknown state`) are one-shot
   snapshots with no corresponding state, so they are kept in a small
   per-device override map that `magazine_state_text()` consults first
   and that every real transition (`set_magazine_state()`) invalidates --
   so a stale hardware snapshot can never survive whatever happens next.
8. **`OPT_CHECK_STATUS` placement**: between `OPT_EJECT_FILM` and
   `OPT_FRAME`, per the task's explicit instruction, making it the last
   item in the Film group before Frame.

### 9.3 Tests

`tests/gl126_magazine_probe.cpp` + `tests/test_sane_magazine.py`: 34
Python tests (was 30; several renamed/rewritten, several added, none
silently dropped -- see the diff for the full mapping). New coverage,
by name: `test_the_edge_resolving_lets_the_ejected_kind_load`,
`test_the_regs_check_after_the_edge_still_refuses_the_base_table_state`,
`test_a_cold_read_forces_the_full_release_path_even_from_ejected`,
`test_load_film_from_ejected_waits_instead_of_jogging`,
`test_a_loaded_mark_lets_a_scan_through`,
`test_the_edge_wait_times_out_without_writing_anything`,
`test_a_released_retry_rejogs_only_if_no_clear_was_ever_seen`,
`test_check_status_reads_the_hardware_and_updates_the_line`,
`test_magazine_state_values_are_pinned`. Rewritten in place (same test
name, new meaning under WP-5, since `load_document()` no longer touches
hardware): `test_a_scan_after_eject_refuses_no_matter_what_the_hardware_
says` (was "...runs_open_then_load"), `test_an_ejected_mark_from_
another_process_refuses_the_scan` (was "...runs_open_then_load"),
`test_a_pending_load_refuses_and_keeps_the_mark_no_matter_what_the_
hardware_says` (merges the old "...without_a_magazine..." and
"...from_the_wrong_state..." tests, since both hardware seed
combinations now produce the identical outcome),
`test_load_document_never_looks_at_the_frame_number` (was "...an_
impossible_scan_request_never_moves_the_magazine"; the concern it
guarded is retired, see `docs/sane-wp4-magazine.md` §11).
`tests/test_sane_geometry.py`'s `test_magazine_load_validates_the_
request_before_moving_anything` (source-level, textual) is similarly
replaced by `test_load_document_is_pure_and_never_moves_the_magazine`,
asserting the function contains no device-I/O primitive at all.

The "no write during the wait" assertion (implementation note 1) is
`EDGEWRITE` in the probe's output: the checkpoint callback snapshots the
mock's OUT-transfer counter at the first `"gl126_magazine_edge_poll"` and
compares on every later one; a mismatch is reported, not raised, so a
test can assert on it explicitly.

**Coverage gap, stated plainly**: the edge wait cannot be observed
resolving to `Seen` from a FRESH (Unknown, non-Ejected) "Load film"
press on this test harness, because `OPEN`'s very first acknowledgement
read always fails on the always-zero-answering mock (true since WP-4,
demonstrated by every existing "reaches the wire, fails closed" test in
this suite) -- the wait sits AFTER the jog in that path, so it is never
reached. For the SAME reason, a genuinely Released-origin retry (Test
51's literal double jog: the FIRST press already completed an ordinary
release, then times out, then is retried) is equally unreachable here --
every "timed-out Released state" this probe can produce is Ejected-
origin, because reaching Ejected costs no wire traffic at all (see
below), while reaching a "fresh, non-Ejected" wait costs an OPEN that
always fails first.

The wait's full resolution is instead exercised through the two paths
where it runs BEFORE anything that can fail on the mock: the Ejected
kind (wait first, then `open`) and an Ejected-origin retry (wait only,
then `open`, then `load` -- review finding G's fix made this the ONLY
retry shape this probe can produce, and it is now correctly covered
either way, with or without a clear seen on the timed-out press).
Reaching Ejected itself is cheap and reliable: every scenario above
seeds the loader sensor CLEAR and calls "eject" first, which takes the
driver's own "nothing to do" branch (`docs/sane-wp4-magazine.md`,
`magazine_eject_impl()`'s `!sensor.present()` case) -- a real state
transition (Unknown/whatever -> Ejected, mark written) that is
guaranteed reachable from reg 0x01 == 0x22 alone, no motor sequence
involved, so it never fails on the silent mock the way OPEN does. Both
covered paths include the "no write during the wait" and the post-edge
register-precondition checks. This is the same "wire equality is proven
by a separate scripted-`Wire` fake" split this project has used since
hook 2 (`tests/gl126_ops_probe.cpp`/`test_sane_ops.py`) --
`gl126_magazine_probe.cpp` proves the STATE MACHINE and the option
plumbing through the real built backend, not byte-for-byte transfers.

(Item L, considered and left undone: a `TestUsbDevice` IN-response
callback that answers each control IN with the CURRENT op program's own
expected ack/poll byte -- the way `tests/gl126_ops_probe.cpp`'s scripted
`Wire` fake already does for the op-level tests -- was assessed as not a
reasonable amount of additional work for this task: it would need the
generic mock to carry per-op expected values that only the profile-
specific generated tables know, effectively re-implementing the op-
program interpreter genesys's own test mode was deliberately kept free
of. The gap stays exactly as described above, at the level this probe
operates.)

### 9.4 Not done / left as noted

- `sane/gl126-integration.patch` was regenerated from the clone's live
  `git diff` and is byte-identical to it (verified in a fresh diff, not
  just "should be").
- **NOT hardware-run.** No scanner contact of any kind was made or
  attempted. Test 92 (`docs/sane-wp5-load-button.md` §7) remains entirely
  the owner's call, in the owner's own future session.
- `docs/sane-wp3-submission.md` §8 was given the new hunks (`OPT_CHECK_
  STATUS`, the option handlers, the two test-mode-only genesys fixes) --
  the WP-3 branch/bundle itself was NOT re-exported by that task (a
  separate, explicit action the submission runbook gates); it was done
  2026-09-28 after Test 92 (v3).
- The Python driver's `of135i load` was not touched, per the task's hard
  rule; WP-5 §8 already notes it as a later candidate, unchanged here.

### 9.5 Review round two (2026-09-27, later the same day)

An independent reviewer plus the coordinator found nine further issues in
the 9.1-9.4 implementation, all fixed offline, none touching a motor
program:

1. **(A) Post-edge presence + class check, HIGH.** The edge wait only
   ever watched bit 0x08; a magazine pulled back out during the 600 ms
   settle, or a present-but-busy status class, was not re-checked before
   LOAD. Fixed: right after `Seen`, `magazine_load_film_impl()` re-reads
   reg 0x101 and requires `present() && idle_class()` alongside the
   existing reg 0x01/0x3b/0x3c check, same refusal shape. This also
   corrected §9.2 decision 3's own overstatement (the wait "subsumes" the
   sensor precondition -- it subsumes the PRESENCE bit, not the class).
2. **(B) A mark BEFORE the wait, HIGH.** A process killed while waiting
   (Ctrl-C on `scanimage -n --load-film`, "Terminate" on a frozen
   digiKam) used to leave whatever mark existed before the press --
   which, for a fresh release, was nothing at all. Fixed: the non-
   Ejected-origin path now writes a `released` mark right after the
   jog (or immediately, for a wait-only retry) and BEFORE the wait
   starts; the Ejected path already had its own `ejected` mark from
   Eject film. Either way, a kill leaves a mark that refuses the next
   scan.
3. **(C) Check status false "power-cycle", HIGH.** Right after LOAD
   completes, or during calibration, reg 0x101 is `0xdc`/`0xd8`-shaped --
   NEITHER is the idle class -- and the old logic reported "unknown
   state -- power-cycle" for a magazine that was, in fact, loaded. Fixed:
   Loaded (in-process or a matching mark) is now reported regardless of
   class, with reg 0x01 == 0x22 and the sensor present; "unknown state"
   is reserved for reg 0x01 outside `{0x22, 0x00}`.
4. **(D) A cross-process `loaded` mark must block Load film, MEDIUM.**
   The Unknown-branch mark lookup only checked for an `ejected` mark;
   a `loaded` mark from an earlier process let a second Load film press
   run again. Fixed: the same lookup now also refuses read-only
   "already loaded" on a matching `loaded` mark.
5. **(E) Failed must persist across processes, MEDIUM.** A magazine
   failure used to CLEAR the mark -- so a second process had no way to
   know the transport's state was never established. Fixed: a fourth
   mark kind, `MagazineMarkKind::Failed`; `MagazineFailGuard`'s failure
   path now WRITES it instead of clearing; `load_document()`, Load film
   and Eject film all refuse on a matching `failed` mark from Unknown;
   only a cold reg 0x01 read (Load film's own start-of-call check, or
   Check status) clears it.
6. **(F) "No write during the wait" made real, MEDIUM.** The OUT
   counter added in 9.1 only saw raw USB traffic (`TestUsbDevice`); the
   edge wait itself never reaches that far (it only reads registers), so
   the assertion was tautological -- true by construction, not by
   measurement. Fixed: every `TestScannerInterface::write_*` method now
   counts too (`write_count()` = the USB counter plus these), and a new
   `seed_register()` bypasses the count entirely for test setup (the
   probe's `seed()` helper and the edge-poll checkpoint's own scripting
   both use it, so scripting the sequence can never itself trip the
   assertion it is trying to prove).
7. **(G) Ejected-origin retry lost OPEN, HIGH -- the reviewer's own
   finding, and the most consequential one.** A timeout always wrote a
   `released` mark and reset state to Released, whatever the press had
   started from -- so a SECOND press on an Ejected-origin timeout forgot
   the origin, skipped `open`, and checked the strict `0x00/0x00` regs
   rule instead of the lenient one. Against real post-scan register
   values (`0x02/0x00`) that retry would have refused Failed for a
   magazine that was never jogged loose in the first place. Fixed: the
   origin is carried across the timeout two ways -- an in-process flag
   (`magazine_wait_needs_open()`) for a same-process retry, and the
   MARK'S OWN KIND for a cross-process one (a timeout of an Ejected-
   origin wait now writes an `ejected` mark, not `released`, so a fresh
   process's existing Ejected-mark detection reapplies the no-jog,
   lenient-regs treatment with no new state of its own).
8. **(H) A power cycle inside one process, LOW.** A cold reg 0x01 read
   is now checked at the very start of `magazine_load_film_impl()`,
   before the Loaded/Failed refusals: it resets state to Unknown and
   clears any mark as stale (in-process claim OR a matching mark, either
   one) before anything else is decided, so a scanner that was power-
   cycled earlier in the same process's lifetime is treated as a fresh
   press, not "already loaded" forever.
9. **(I) `saw_clear` debounced, LOW.** It used to become true on a
   SINGLE clear read; now it only becomes true once `clear_consec`
   reaches the same 2-consecutive threshold the phase transition itself
   requires, so a one-poll glitch cannot be mistaken for the magazine
   coming loose (and, since 9.5.7 above, cannot needlessly demand a
   jog on a subsequent retry either).

Also addressed, not code changes: **(J)** `magazine_check_scan_allowed()`
(`load_document()`) now performs exactly one device read, reg 0x01 --
chosen over leaving the comment/§3.4 wording as "zero device I/O", since
a cold read that would otherwise leave a stale Loaded/Failed claim
un-refuted was judged the more serious gap. The status-line GET
(`magazine_state_text()`, the "magazine" option's own read) still does
NOT do this -- it stays free of hardware reads by design, exactly as
before; only `load_document()` and Check status ever read reg 0x01
outside of a button press. **(K)** documentation fixes: the "third
button" vs "fourth button" count (`docs/sane-install.md` §6/§7 --
Load film, Eject film, Check status is three, not four),
`scanimage -n --load-film`'s blocking/Ctrl-C behaviour and
`--check-status`'s silent result (needs `-A`) documented in both the
README cheat sheet and `docs/sane-install.md` §7, KSane's dialog being
unresponsive during the wait (do not force-quit), and §7's new paragraph
on a mark going stale when `of135i` and SANE are mixed across a load/
eject pair (Check status, or delete the mark file by hand). **(L)**
considered and left undone, with the reasoning recorded above (end of
9.3): a `TestUsbDevice` IN-response callback general enough to answer
per-op expected ack/poll bytes would re-implement the op-program
interpreter the project deliberately keeps out of genesys's generic test
mode; the fresh-press coverage gap stays exactly as 9.3 describes it.

Tests: `tests/test_sane_magazine.py` grew from 34 to 42 (new:
`test_saw_clear_is_debounced`,
`test_the_post_edge_check_catches_a_late_class_or_presence_change`,
`test_a_killed_process_leaves_a_refusing_mark`,
`test_a_cross_process_loaded_mark_blocks_load_film`,
`test_a_failed_mark_persists_and_blocks_everything`,
`test_an_ejected_origin_retry_keeps_running_open`,
`test_a_power_cycle_inside_one_process_resets_loaded`,
`test_check_status_reports_loaded_despite_a_busy_class`; renamed to
match corrected behaviour: `test_a_released_retry_rejogs_only_if_no_
clear_was_ever_seen` -> `test_an_ejected_origin_retry_never_jogs_either_
way`, since the ORIGINAL name asserted the very bug finding G fixed).
`tests/test_sane_lock.py` gained `test_magazine_mark_loaded_and_failed_
kinds_round_trip` and `gl126_lock_probe.cpp` gained `mark-write-kind`/
`mark-read-kind` commands (the one-argument `mark-write`/`mark-read`
commands are unchanged, Released-only, per section 10.1's back-compat
rule). Full count: 389 tests pass (was 380), 0 skipped, patch
regenerated and re-verified byte-identical to the clone's `git diff`,
build 0 warnings. Still **NOT hardware-run**.
