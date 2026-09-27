# WP-4 — magazine handling inside the SANE backend (design, offline)

Status: **DONE. Designed, implemented and hardware-verified 2026-09-13**
— runs A, B and C (Tests 75, 76, 77), including the power-cycled
latched-magazine case. Reviewed and corrected twice before the runs
(§9), and again after them (§9a) when driving the real dialogue exposed
five interaction defects. The two hooks (`load_document()`, `eject_document()`)
and the three new options exist in `sane/gl126.cpp` and the integration
patch; every program is wire-equality-tested against the Python driver;
nothing has moved the motor from C++ yet. The hardware plan is
`docs/sane-wp4-hardware-plan.md`. Until that run, the documented
workflow stays `of135i load` → SANE scan → `of135i eject`
(`docs/sane-install.md`).

Why this exists: B2 (upstream submission) requires that a full
load → scan → eject cycle runs from a SANE frontend alone
(`docs/ROADMAP.md`, WP-4). The transfers all exist — the Python
loader's captured tables (`of135i/tables_load.py`), its hand-written
cold-start and eject sequences (`of135i/device.py`) and the op-program
machinery of hooks 2–7 — so the work is (a) the interaction model,
because SANE has no way for a backend to ask the operator to do
something in the middle of `sane_start`, and (b) porting the project's
most delicate motor sequence to C++ without changing a byte of it.

## 1. What the vendor does, and what SANE cannot

The vendor's insert flow (`docs/protocol-notes.md`, app open with a
loose and with a latched magazine; `docs/test-log.md` Tests 14–23):

1. **App open:** the OPEN register table, then the *jog* — feed 6690,
   feed 6690, eject 3090 with the loader motor profile. The jog IS the
   eject: it releases a latched magazine, and it is run with a loose
   one at every app start.
2. **The operator takes the magazine fully out and reinserts it to
   the mechanical stop.** The app polls the interrupt endpoint
   continuously while it waits.
3. **LOAD** — sensor ack, the engaging feed (completion 0xf455: done
   class, sensor bit CLEAR — the cassette was pulled past the
   sensor), the prescan traverse (0xdc55), one idle round.
4. Scan, later, as a separate command.

Two facts shape the design:

- **The reinsert has no machine verification** (Test 24 note, Test
  51): reg 0x32 reads the same before and after it and the interrupt
  endpoint carries no event for it. What *is* verified is the LOAD
  feed's completion: a magazine that was not reseated does not engage
  and the feed completes 0xfc55 instead of 0xf455 (Tests 48/49, 2/2,
  benign, but a failed session by the safety model). The converse is
  NOT established -- a feed that does not complete has other possible
  causes -- which is why the backend names the step that failed and
  leaves the cause to the log (§9, finding 5).
- **A SANE backend only runs while the frontend is calling it**, and a
  `sane_start` must return either an image stream or an error. There
  is no callback to the operator, no "wait for the user" status, and
  no background thread in this backend by design.

So the backend cannot copy step 2. Something in the frontend's normal
vocabulary has to stand in for "the operator did the reinsert".

## 2. The interaction model (decided: two-step)

Christian chose the two-step protocol 2026-09-13 among three
candidates (a pollable sensor option; two steps; refusing with a
renderable status). The alternatives are recorded in §8.

Three new options in the backend's GL126 group, all standard SANE
types every frontend already renders:

| option | type | what it does |
|---|---|---|
| `load-film` | button | **Stage A — release.** Brings a cold unit up (the vendor cold-start sequence), writes the OPEN table and runs the jog. The magazine pops loose. Returns. The backend now remembers "released". |
| *(the next `sane_start`)* | — | **Stage B — load.** If the unit is remembered as released, the LOAD program runs first (feed + traverse, fail-closed), then the usual calibration and scan. If nothing is pending, `sane_start` is exactly what it is today. |
| `eject-film` | button | The eject program from a loaded or parked magazine. |
| `magazine` | string, read-only | One line for the frontend to show: what the backend believes the magazine state is and what to do next (`cold`, `released — take the magazine out, reinsert it to the stop, then scan`, `loaded`, `ejected — press Load film to load again`, `unknown`). |

Operator's view, from digiKam ("Specifika alternativ för bildläsare"
tab): press **Load film** — the magazine pops out — take it out, push
it back to the stop — press **Läs in**. Frames 2–6: just Läs in with
another frame number. At the end: **Eject film**.

From the command line the same two steps are two invocations:

    scanimage -n --load-film              # -n: set options, do not scan
    # take the magazine out, reinsert it to the stop
    scanimage --frame 1 --mode Color --resolution 3600 -o f1.tiff
    scanimage -n --eject-film

This mirrors QuickScan except for who triggers step 3: the vendor's
app reacts to the reinsert by itself (it has a thread and a sensor
loop); here the operator's next scan is the trigger. The transfers,
their order and their completion checks are the vendor's.

### 2.1 "Released" has to survive a process

digiKam keeps the device open, so "released" can live in the backend's
memory. `scanimage` cannot: each invocation is a new process, and a
LOAD without the jog *in the same power cycle* is exactly the failure
the project spent Tests 11b–15 on (feed done, sensor still set,
magazine loose). So the mark is kept in two places and both must
agree before Stage B runs:

1. **In-process:** a per-device record set by Stage A.
2. **On disk:** `<lock path>.magazine` next to the process lock
   (`/tmp/of135i-07b3-1436.lock.magazine`, `$OF135I_LOCK_FILE` respected
   like the lock), written by Stage A with the USB bus:address of the
   unit it jogged, read by a later process.

A disk mark alone is never enough: before LOAD the backend re-reads
the hardware and requires reg 0x01 = 0x22 (idle-homed), the status
word in the done class with the loader-sensor bit SET (0xf8 pattern —
a magazine is physically in the slot) and regs 0x3b/0x3c = 0x00/0x00
(the OPEN table's values, not the scan base table's 0xff/0xff). A
power cycle re-enumerates the unit (new address) *and* leaves reg 0x01
at 0x00, so a stale mark can never authorise a LOAD after one. The
mark is consumed — deleted — by Stage B whether LOAD succeeds or
fails, by Stage A (a new release supersedes it) and by eject.

### 2.2 The state machine

Per device, in `gl126.cpp`:

    Unknown ──load-film──► Released ──sane_start──► Loaded
       ▲                      │  ▲                     │
       │            load-film │  │                     │ eject-film
       │            (jog again│  │                     ▼
       │             = double │  └── eject-film ──► Ejected ──┐
       │             jog)     │            ▲                  │ sane_start
       │                      │            │ load-film         │ (open + load,
       │                      │            └── (open + jog,    │  no jog --
       │                      │                same as from    │  next strip,
       │                      │                any other state)│  §10)
       │                      └────────────────────────────────┘
       └───────── any failure (Failed: power cycle) ◄───────────────┘

  (Ejected is now a pending load like Released -- two KINDS of pending,
  tracked by the same in-process state and the same cross-process mark,
  §10.)

- **Unknown** is what `sane_open` starts in (it writes nothing; the
  hardware may be cold, idle, loaded — the backend does not know).
- **load-film from Unknown:** reg 0x01 must read 0x22 or 0x00. 0x00 →
  the cold-start program first (§3.1), then reg 0x01 must read 0x22;
  then OPEN, then JOG. → Released.
- **load-film from any non-cold state:** OPEN + JOG. There is no
  per-state branch — the device-open table is written and the jog run
  every time, which is the vendor's own app open, latched magazine or
  not (capture `20260907-vendor-open-with-latched-magazine`, Test 46
  timeline). Pressed a second time from Released it is Test 51's second
  jog, from the loose position, and that is the supported way out of
  "power-cycled + latched magazine" (§2.3) — hardware-verified in Test
  77. → Released.

  *(Corrected 2026-09-13 after Test 77: this section previously claimed
  "load-film from Released: JOG only". No such branch exists in the
  code and none ever did. Writing the open table again is harmless —
  it is registers, no motor — so the document was wrong, not the
  implementation.)*
- **sane_start from Released:** Stage B (§3.3), the Released kind of
  pending load -- the bare `load` program. Success → Loaded; failure →
  Failed, `sane_start` returns the error, nothing further written.
- **sane_start from Ejected (2026-09-25, §10):** Stage B again, the
  Ejected kind -- `open` then `load`, no jog. Same preconditions, same
  failure handling; the one addition is a cold check (reg 0x01 = 0x00
  after the eject → refused, mark cleared, state → Unknown, not
  Failed -- nothing was written).
- **sane_start from any other state:** no magazine action; today's
  behaviour.
- **eject-film from Loaded / Unknown (idle 0x22):** the eject program
  (§3.4) with the Python driver's guards. → Ejected, and (§10) the
  cross-process mark is now written as "ejected", not cleared. From
  Released: refused (the magazine is already loose; there is nothing to
  eject — the jog was the eject). From cold: refused with "press Load
  film" (the jog releases it; ejecting from an unhomed transport is not
  a verified sequence).
- **load-film from Ejected:** unchanged -- OPEN + JOG, same as from any
  other non-Failed, non-cold state (§10). This is the fallback after a
  power cycle, since the next-strip load above is not valid across one.
- **Failed** is terminal for the process: every magazine action
  refuses until the operator power-cycles; a new `sane_open` starts in
  Unknown again and the hardware check is the gate, exactly like the
  scan-pass state machine (`docs/sane-hook5-frame.md` §9).

### 2.3 The standing requirement: power-cycled + latched magazine

Today's recipe (Test 51, n = 2): power cycle → `of135i load
--double-jog` → the first jog releases the latched cassette, the
operator reinserts, a second jog from the loose position, reinsert,
LOAD. In this model that is: **Load film, reinsert, Load film again,
reinsert, scan.** The second press from Released is by construction
the second jog. The backend does not try to detect "latched" (the
cold-start deviations that hint at it — 0x4855 timeout, 0x32 = 0x1d —
are n = 2 observations, not a rule); the operator knows whether the
magazine was left in. The `magazine` option says "released" after
each press so the operator can see the state. A plain single press
from cold with a loose magazine is the 7/7-verified flow.

## 3. The programs

All five run through the existing op-program runner
(`sane/gl126_ops.cpp`, `run_program()`), with the fail-closed rules of
hooks 2–7: any acknowledgement, poll or bulk failure stops the program
with nothing further written and the `SaneException` carries the
power-cycle instruction. They are emitted by `tools/gen_sane_tables.py`
into `MAGAZINE_PROGRAMS[]` (`sane/gl126_tables.h`), separate from the
per-profile scan programs because they have no dpi.

Two op kinds were added for them, because the Python driver — the
arbiter, hardware-verified — does two things the calibration/scan
programs never needed:

- **`Sleep`** — the replayer's pacing: `Scanner._exec_ops` sleeps
  `min(dt, 2.0)` before every op whose captured gap exceeds 50 ms.
  The load flow was verified *with* that pacing (Tests 17–23, 7/7),
  including the 1.6 s pause before each motor completion poll and the
  2.0 s pause in LOAD's idle round. Hook 5 dropped the pre-sleep for
  POSITION deliberately (decision 2 there); for the magazine flow the
  verified form is kept byte for byte and second for second. Only the
  magazine programs carry `Sleep` ops; the scan programs are
  unchanged.
- **`PollBestEffort`** — a poll that logs and continues on timeout,
  with its own per-op budget. The Python cold start's ready and settle
  reads (`poll_status_word`'s default form) and the eject
  (`_eject_body`'s completion loop) are *non-raising* by design: the
  cold start's opening ready poll times out on every cold start (Test
  78 — the engine is not yet in the class it waits for), a latched
  magazine (Tests 45/51/77) adds nothing to that, and a strict poll
  there would refuse exactly the case the standing requirement is
  about. `PollMasked` (fail-closed) is used for every motor completion:
  the cold start's nine (since 2026-09-15, see the offline entry of
  that date in the test log — a completion is the only wait between one
  motor start and the next), JOG's four and LOAD's two.

### 3.1 `cold_init` (hand-built, like `park`)

`Scanner._cold_init_body()` step for step: chip handshake, status word,
the ready poll (mask 0xf0 / 0xf0, 15 s, best-effort), the cold register
table + end-of-access + AFE bring-up (125 pairs in 4 batches, the
EEPROM reads as logged reads of 3 and 19 bytes), the reg 0x31
read-modify-write pair, then three homing rounds (feed 6690 / feed
6690 / eject 3090 with the loader slope table to both RAM addresses,
each completion polled for 0xf8 FAIL-CLOSED with the driver's 30 s
budget — `PollMasked`, since 2026-09-15) with
the table + AFE rewritten between rounds, and the settle poll on regs
0x35/0x32. 9 motor moves. Deviations from the Python, all in the
non-gating direction and listed in `build_cold_init_program()`'s
docstring: the motor-completion poll compares the status byte only
(Python compared the 16-bit word incl. the constant 0x55 ack); the
settle loop polls 0x35 then 0x32 instead of re-reading both together.
After the program the hook reads reg 0x01 and requires 0x22, as
`cold_init()` does.

### 3.2 `open`, `jog`, `load` (generated from `tables_load`)

Verbatim, one op per captured transfer, like `position`/`scan_setup`:

- register writes as `Write` with the captured payload (the JOG's
  reg 0x31/0x32 read-then-write pairs are replayed as captured, not as
  read-modify-writes — that is how the Python driver runs them);
- the four JOG completions (0xf855 ×4), LOAD's feed (0xf455) and
  traverse (0xdc55) as `PollMasked` under the driver's
  `LOAD_STATUS_MASK` 0xfb (state class AND loader-sensor bit; bit 0x04
  masked as session-variable), 5 s budget (the driver's 3× captured is
  at most 4.8 s);
- the two non-completion polls (OPEN's class-D status read, LOAD's
  final reg 0x32 read) as `PollBestEffort` with the driver's 1 s
  budget;
- the EEPROM reads (3 and 64 bytes) as logged reads — the runner now
  reads the captured length instead of capping at 2 bytes;
- pacing `Sleep`s where the driver sleeps (§3): 1599/509/1618/875 ms
  before the JOG completions, 1613/1097 ms before LOAD's, 116/2000/104
  ms in LOAD's idle round.

### 3.3 Stage B in `load_document()`

Called by the core's `genesys_start_scan` before calibration (the same
call site sheet-fed models use, gated to GL126 by the patch). Order:

1. No mark (memory or disk) → return; nothing read, nothing written.
2. Disk mark for another bus:address → ignored (deleted), return.
3. **The scan request is validated first**, on pure computation: the
   profile, the frame bound, the travel ceiling, the ledger invariant
   and the colour mode (`validate_scan_request()`). This hook runs
   BEFORE calibration, which is where that validation used to live, so
   without it an impossible request would move the magazine and only
   then be refused (§9). A refusal here keeps the mark: the request is
   wrong, the magazine is not.
4. Hardware check, reads only: reg 0x01 == 0x22; status word class F
   with bit 0x08 set; regs 0x3b/0x3c == 0x00. A clear sensor bit →
   `SANE_STATUS_NO_DOCS` ("no magazine in the slot") with the mark
   kept, so the operator can insert it and scan again. Any other
   mismatch → `SANE_STATUS_INVAL`, mark deleted, state Failed.
5. The `load` program. A failure NAMES the step that did not complete —
   the engaging feed's completion, the traverse's, or another op —
   and says nothing about the cause. A timeout at the feed has the same
   shape as the documented `fc55` outcome of Tests 48/49 (the magazine
   was not reseated, nothing is stuck), but the hook does not check for
   that signature, so it does not assert it; the exception carries the
   op index and the values actually polled, and the signature is read
   from the log. No message in this flow invites another attempt (§9,
   findings 4 and 5).
6. A final status-word read must match the traverse target under the
   mask (`load_completion_target()`'s rule) → Loaded, mark consumed.

`sane_start` then continues into offset calibration, whose own S0
check (reg 0x01 = 0x22) still runs.

### 3.4 `eject` (hand-built from `_eject_body`)

Guards first, reads only, in the driver's order: reg 0x01 must be
0x22; the loader sensor (reg 0x101 bit 0x08) clear → "no magazine
detected, nothing to do" (return, like the driver); regs 0x3b/0x3c =
0xff/0xff → refused (`UnejectableStateError`'s reason: the eject
stalled twice from the base-table-only state, Test 44). A scan pass
that is Armed or Streaming → refused. Then the program: 0x33 = 0x8e,
reg 0x32 read-modify-write (|= 0x02), motor enable, the 19-register
move batch (mode 0x18, FEEDL 3090, loader speed profile), the loader
slope table to both addresses, GO, the completion poll ((status &
0x21) == 0x20, 10 s, best-effort as in the driver), motor disable.
→ Ejected. Since 2026-09-25 (§10) this WRITES the cross-process mark as
"ejected" rather than clearing it, so a load pending from an eject
survives a process boundary the same way a release's does.

## 4. Where the code lives

- `tools/gen_sane_tables.py` — `decode_ops(..., magazine=True)` (the
  poll classification and `Sleep` emission above),
  `build_cold_init_program()`, `build_eject_program()`,
  `validate_op_program()` cases for the five, `MAGAZINE_PROGRAMS`
  emission. Python is authoritative; `--check` keeps the generated
  files honest.
- `sane/gl126_tables.h` — `OpKind::Sleep`, `OpKind::PollBestEffort`,
  `MAGAZINE_PROGRAMS[5]`.
- `sane/gl126_ops.{h,cpp}` — the two kinds in `run_program()`;
  `do_read` reads the op's own length; `magazine_program()` lookup;
  `LoadStatusMask` constants mirrored from `device.py`.
- `sane/gl126_lock.{h,cpp}` — the disk mark (`magazine_mark_path/
  _write/_read/_clear`), next to the process lock whose path it borrows.
  It lives there, not in `gl126.cpp`, so it has no genesys dependency and
  is exercised standalone by `tests/test_sane_lock.py`. The device key
  goes on its own line: a SANE device name can contain spaces (the
  backend's own test mode produces `test device:0x07b3:0x1436`), and a
  space-delimited field silently truncated it into a different device.
- `sane/gl126.cpp` — `load_document()`, `eject_document()`, the
  state record and its transitions, and three free functions the patch
  calls from the option handlers: `gl126::magazine_release(dev)`,
  `gl126::magazine_eject(dev)`, `gl126::magazine_state_text(dev)`.
  Two pieces carry the safety model (§9): `MagazineFailGuard`, which owns
  the outcome of a whole operation rather than leaving it to the
  innermost handler, and `validate_scan_request()`, the write-free
  refusal set shared with `offset_calibration()`. Three
  `test_checkpoint()` calls mark the points where an offline test can
  inject a failure; they are no-ops on the USB interface, and gl124,
  gl841 and gl646 use the same mechanism.
  `UsbWire::sleep_ms()` now waits through the scanner interface
  (`sleep_us`) instead of `std::this_thread`: in this backend a wait is
  the interface's business — `ScannerInterfaceUsb` skips it in replay
  mode, the test interface always does — and these are the only waits
  GL126 has.
- `sane/gl126-integration.patch` — `OPT_LOAD_FILM`, `OPT_EJECT_FILM`,
  `OPT_MAGAZINE` (inactive for every other ASIC), the
  `genesys_start_scan` call site.
- `tests/test_sane_ops.py` — wire equality of all five programs
  against the Python driver over the same fake (§5), the two new
  kinds' wait/timeout behaviour, structural checks.
- `tests/test_sane_magazine.py` + `tests/gl126_magazine_probe.cpp` —
  the option plumbing and the state machine through the real built
  backend in genesys's test mode (§5).

## 5. Offline verification (done before any hardware)

1. **Wire equality, five programs.** The Python driver runs
   `initialize(prep=False)` / `jog_magazine()` / `load_magazine()`
   over `FakeUsbDevice` with `vendor_like_load_status(jog=True)`, and
   `cold_init()` / `eject()` over the same fake; the C++ programs run
   over the probe's scripted `Wire` fake; the host→device transfer
   logs (control writes with payload, reads with setup and length,
   bulk OUT by length + digest) must be identical element for
   element. Every read-modify-write site is scripted with the same
   register value on both sides so the computed write payloads agree.
2. **The new op kinds:** `Sleep` advances the fake clock and sends
   nothing; `PollBestEffort` settles, or times out and continues with
   the next op (recorded), never throws. The cold start's nine motor
   completions are `PollMasked`: fed a busy value that never clears,
   the program stops at that op with no further transfer and no next
   execute pulse (pinned at the first move and at round 2's first).
3. **No captured chunk is emitted twice / none missing** — the
   structural `program_info` check on the five programs (slope-table
   BulkOuts carry their data; there are no injections).
4. **Option plumbing through the built backend** (test mode,
   `TestScannerInterface`, no USB): the three options exist for the
   GL126 model and are inactive for a GL124 one; `load-film` from an
   unknown reg 0x01 refuses with zero writes; `load-film` from a
   scripted 0x22 reaches the OPEN program and stops at its first
   unacknowledged write (the test interface answers no 0x55) with
   `ops_done` 0 and the state Failed; `sane_start` with no mark never
   touches the magazine (progress reaches `offset_calibration` as in
   `test_sane_calibration_cache.py`); `eject-film` from the
   base-table-only state refuses read-only; `magazine` renders the
   expected text in each state.
5. **The disk mark:** written/consumed/ignored as §2.1 says, under a
   temporary `$OF135I_LOCK_FILE`; a mark for a different bus:address
   is ignored; a mark with reg 0x01 ≠ 0x22 is ignored.
6. `release_check` green, `gen_sane_tables.py --check` clean, the
   backend builds with zero warnings, the patch regenerated from the
   clone's diff.

## 6. What the hardware run has to show (summary; the plan is separate)

One cycle from a frontend alone, on the reference unit, with the
operator listening: power cycle (unit at 0x00) → **Load film** from
scanimage (`-n`) → cold start + OPEN + JOG on the wire, completion
polls at their captured values, magazine released → reinsert →
`scanimage --frame 1` → LOAD (feed 0xf4, traverse 0xdc), then the
verified calibration/POSITION/scan/PARK → **Eject film** → magazine
loose. Then the same from digiKam on a second load. Then the
double-jog recipe once (power cycle with the magazine latched). Stop
at any deviation; the exit is the power cycle route with the Python
tools, as always. `docs/sane-wp4-hardware-plan.md`.

## 7. What this does NOT do

- No detection of the reinsert (none exists, §1) and no waiting inside
  `sane_start`.
- **No interrupt-endpoint drain, and that has a known consequence.** The
  Python tool reads EP 0x83 after the jog, the reinsert and the load
  because a load that never reads it leaves the endpoint in a permanent
  `EOVERFLOW` state that only a power cycle clears (Test 20 established
  both halves: draining prevents it, a power cycle clears it). The
  genesys USB abstraction (`IUsbDevice`) has control and bulk transfers
  only — no interrupt read — so draining it from here would mean
  extending that interface, which is an upstream change well outside
  WP-4. Nothing in the SANE flow reads the endpoint, so nothing here is
  harmed; but after a backend-driven load, `of135i status` / `doctor`
  may report the interrupt overflow until the next power cycle. The
  hardware plan records whether it actually happens.
- No automatic eject at `sane_cancel` (the core's sheet-fed path is
  not used; a batch of six frames must not eject after each).
- No new motor sequence of any kind: cold start, OPEN, JOG, LOAD and
  eject are the driver's, transfer for transfer.
- The `magazine` text is what the backend *believes*; it is not a
  sensor. The loader sensor reports presence, not latching — as
  everywhere in this project.

## 8. Decisions taken (offline, 2026-09-13) — each open to Christian's veto

1. **Two-step protocol** (Christian's choice): `load-film` releases,
   the next `sane_start` loads. Not taken: a sensor option the
   frontend polls (no signal exists for the reinsert, §1); refusing
   with a status (no frontend renders a "please reinsert" from a
   status code; NO_DOCS is used only for "no magazine present").
   Also not taken: blocking inside `sane_start` waiting for the loader
   sensor to go clear and set again — it would be the vendor's UX but
   depends on unverified sensor behaviour after the OPEN table and
   freezes the frontend for the duration.
2. **The released mark persists on disk, keyed by bus:address and
   re-verified against the hardware before use** (§2.1), so
   `scanimage` can load in two invocations. Removing this makes the
   CLI unable to load; it does not affect digiKam.
3. **A second `load-film` press from Released is the double jog** and
   the operator's recipe for the latched-after-power-cycle case; the
   backend does not guess whether the magazine was latched.
4. **The load flow keeps the replayer's pacing (`Sleep` ops)**; hook
   5's "poll from t = 0" was a choice for a rewritten PARK, not a
   rule. The scan programs are untouched.
5. **Cold start ready/settle polls and the eject poll are best-effort,
   as in the driver** (`PollBestEffort`); every motor completion —
   the cold start's nine (revised 2026-09-15, both implementations),
   JOG's four, LOAD's two — is fail-closed. Cold start's completion is
   additionally verified by reg 0x01 = 0x22 afterwards, as in the
   driver.
6. **Eject is never run from cold** (Python's `eject()` does
   `cold_init` first; the SANE flow sends the operator to Load film
   instead, whose jog is the vendor's own release). One less motor
   path to bring up.
7. **Stage B refuses read-only when the sensor bit is clear**
   (`SANE_STATUS_NO_DOCS`, mark kept) rather than running the feed to
   find out.
8. **`sane_cancel` does not eject**; the model stays non-sheet-fed.

## 9a. Interaction fixes, 2026-09-13 after the hardware runs

Christian's verdict after driving the whole flow from digiKam was that
the dialogue and the interaction had to become more transparent. Tests
76 and 77 turned that into five specific defects, all fixed offline, none
touching a motor sequence.

1. **The status value was a full sentence.** KSane draws an
   unconstrained string option as an editable combo scrolled to the END
   of its content, so the operator saw the tail of the advice and never
   the state word. Every value is now at most 33 characters and leads
   with the state: `unknown -- press Load film`, `released -- reseat,
   then scan`, `loaded -- scan, then Eject film`, `ejected -- press Load
   film`, `failed -- power-cycle the scanner`.
2. **It was drawn with Add and Remove buttons beside it** — noise for a
   line nobody can set. The option now carries a
   `SANE_CONSTRAINT_STRING_LIST` of exactly those values, which makes
   KSane render a plain combo showing the current one.
3. **It sat below the two buttons it describes**, so the state was read
   after acting. `OPT_MAGAZINE` now precedes `OPT_LOAD_FILM` and
   `OPT_EJECT_FILM` in the option enum, and option order is display
   order.
4. **It reported `unknown` while a load was genuinely pending.** The
   text read only the in-process record, but a release survives on disk
   — which is the whole point of the mark, and Test 77 hit the case for
   real when digiKam was restarted between the release and the scan. It
   now consults the mark and says `reseat the magazine, then scan`.
5. **A scan with nothing loaded was not refused.** The vendor answers
   that case with "Please insert the film holder"; we drove the
   transport with no film in front of the sensor and handed the frontend
   an image of nothing, silently. `load_document()` now refuses with
   `SANE_STATUS_NO_DOCS` when the magazine is in the **Ejected** state.
   Unknown is deliberately not refused: `of135i load` remains a
   documented way to load, and a fresh process cannot tell that apart
   from nothing being loaded — refusing there would break the CLI
   workflow to catch a mistake we cannot actually detect.

Also, the `load-film` option's description now says that a cold start
takes about twenty-five seconds on the reference unit, with no progress
shown — the figure was 40 s when that text was written and Tests 78 and
79 then cut the cold start to ~23 s; the description says which unit it
was measured on rather than promising a time. SANE has no progress
channel while an option is being set — the frontend blocks until the
call returns — so the description is the only lever. **The better answer was to remove
most of the wait**, which is what happened: Test 77 measured the opening
poll as dead time, Christian approved the change, and Tests 78 and 79
A/B'd it on hardware — 40.1 s → 22.9 s, 43 % of the wait gone, with every
genuine wait untouched.

## 9. Review round, 2026-09-13 evening (Astra) — four corrections

Found against HEAD f54ad13, all offline, all fixed before any hardware
run. Recorded here because each one is a property of the safety model,
not a tidy-up.

1. **Only `OpsError` failed the session.** `run_magazine_program()`
   marked the state Failed and dropped the pending load in its
   `OpsError` handler — but a real USB failure arrives as a plain
   `SaneException` from the device layer ("invalid read, scanner
   unplugged?"), and so do the register reads that follow a motor
   sequence. Those paths left the state Released and the mark on disk
   after an actual failure, so the next scan would have driven the
   loader again. Now a `MagazineFailGuard` owns the outcome of the whole
   operation: armed at the point writes may begin, and on ANY exit other
   than success it fails the session and clears the mark. Tested by
   injecting a non-`OpsError` exception mid-sequence through genesys's
   own test checkpoint (a no-op on the USB interface).
2. **The load ran before the scan request was checked.** The core calls
   `load_document()` before calibration, and the write-free validation
   lived inside `offset_calibration()` — so an impossible request with a
   release pending would move the magazine first. The validation is now
   `validate_scan_request()`, called by both, and the load half runs it
   before touching the device.
3. **The poll cap was honoured on real hardware.**
   `$OF135I_SANE_POLL_CAP_MS` was read unconditionally. A shorter wait is
   not automatically a safer one — a best-effort poll that gives up early
   continues to the next op, which on the unit could mean continuing
   before a move has finished. It is now read only when the scanner
   interface is a mock **and** the library is in test mode. A plan saying
   "leave it unset" is not a code guarantee.
4. **"Nothing is stuck" was said about every load timeout.** That
   reassurance belongs to one documented signature — the engaging feed
   failing to grip, seen 2/2 in Tests 48/49 — not to a traverse timeout
   or a bulk failure. The hook was changed to key the message on which
   completion failed.

Also corrected in `docs/sane-wp4-hardware-plan.md`: the mark's path
(`<lock path>.magazine`, i.e. `/tmp/of135i-07b3-1436.lock.magazine`),
and the stop rules — a failure ends the approved attempt and the log is
read before any further motor command, rather than "power-cycle and
start over".

### Second round, the same evening — two findings that were still open

5. **The load message still asserted a cause it had not checked.**
   Keying on the feed's completion (finding 4) narrowed WHERE the
   message applied, but it still told the operator the magazine had not
   been reseated, that the scanner was fine and nothing was stuck, and
   invited a power cycle and another attempt. The code never tests for
   the `fc55` signature: a timeout at that op has other possible causes,
   including ones where something IS stuck. The message is now neutral —
   it names the step that failed, states that the transport state is
   unknown and the session blocked, and says to read the log. The
   evidence is not lost: the underlying exception already carries the op
   index and the first and last values polled, so the signature can be
   read rather than assumed. The generic magazine failure message lost
   its "power-cycle the scanner, then press Load film" for the same
   reason — after writes, the next motor command is a decision.

   The line this draws: a refusal that wrote NOTHING (an unknown start
   state, a cold unit, no magazine in the slot, the base-table state)
   leaves the unit exactly as it was and may tell the operator what to
   do next. A failure AFTER writes may not — nobody has named the state
   it left behind.

6. **The hardware plan still contained automatic recovery.** Its stop
   section offered "bring the scanner to a safe state" — a power cycle
   plus `of135i load`/`eject`, and QuickScan in the VM if the magazine
   was stuck — as the one thing done without further discussion, and
   listed evidence preservation AFTER it. Run C ended by sending a
   failed load back to run A. Every one of those is a motor operation,
   and after a deviation nobody knows what state the transport is in, so
   none of them is pre-approved. §6 is now a stop rule with no recovery
   step: stop, preserve (read-only `status`/`doctor` allowed, nothing
   else), and no further motor operation of any kind — rerun, next run,
   Python load/eject, double jog, VM route — until the log has been
   reviewed and Christian has said what happens next. Cutting power on a
   bad noise stays unconditional, and is explicitly a way to stop rather
   than permission to start again.

## 10. Next strip without the jog (2026-09-25)

New evidence, not a redesign: a fresh vendor USB capture
(`20260925-vendor-next-strip.pcap`, private analysis area) shows
QuickScan running its app-start jog exactly ONCE per session, not once
per strip (`docs/protocol-notes.md` Pass 14 addendum 4, `docs/test-
log.md` Test 88). The between-strip load it runs after an eject button
press — operator swaps the strip, pushes the new one in to the stop — is
just the LOAD table again: no jog, no OPEN table replay, no reinsert
prompt. `of135i load --next-strip` (`of135i/loadflow.py`) implements
this offline the same day and Test 89 hardware-verifies it: feed
completion `0xf455` and traverse completion `0xdc55` on the first poll,
straight after a driver eject, and the following scan positions exactly
like the strip before it.

This section brings the same shortcut into the backend: **Ejected is now
a second KIND of pending load**, alongside Released, so `load_document()`
completes it automatically at the next `sane_start` — no `load-film`
press, no reinsert prompt, just the strip swapped and pushed to the
stop.

### 10.1 The two kinds

`gl126_lock.h`'s `MagazineMarkKind` names them, and the mark's format
barely changed to carry it: the mark file's first word, which used to be
the fixed string `"released"`, is now whichever of `"released"` or
`"ejected"` applies — so a mark written before this section still reads
back byte-identically, and `magazine_mark_write(device_key)` /
`magazine_mark_read(device_key*)` (the old one-argument forms) keep
meaning exactly what they always meant: a Released mark, full stop. They
are kept, unchanged, only because `tests/gl126_lock_probe.cpp` and
`tests/test_sane_lock.py` — outside this section's edit scope — call
them and must keep working unmodified. `sane/gl126.cpp` uses the new
two-argument, kind-aware overloads throughout.

| kind | what already happened | what `load_document()` runs |
|---|---|---|
| Released | `load-film` ran OPEN + JOG; operator reseated the magazine | `load` only (§3.3, unchanged) |
| Ejected | `eject-film` ran EJECT; operator swapped the strip and pushed it to the stop | `open` then `load` — no jog (new) |

In-process `MagazineState` still takes precedence over the on-disk mark
(unchanged rule); a `Released` state or an `Ejected` state each set the
matching kind directly. When neither is known in this process, the mark
is read with its kind, exactly as before but now returning which of the
two it is.

### 10.2 Why `open` has to run again

The vendor's own between-strip load skips OPEN too — but it does so by
scanning with whatever speed registers and slope table the PRECEDING
scan pass happened to leave in scanner RAM, not the loader profile OPEN
would set up. `of135i/loadflow.py`'s `--next-strip` deliberately does NOT
replay that: it runs `Scanner.initialize(prep=False)` — the driver's own
OPEN table, hardware-verified from the post-jog position in Tests 17-23
and again as the next-strip load itself in Test 89 — rather than trust
the vendor's undocumented "whatever is already there" fallback, because
that is exactly the kind of leftover state that stalled the driver's
other eject variant twice in the past (`docs/protocol-notes.md` Pass 14
addendum 4). The backend's Ejected-kind load makes the same choice for
the same reason: `run_magazine_program(dev, "open", ...)` before `run_
magazine_program(dev, "load", ...)`, both under the same
`MagazineFailGuard` the Released path already used only for `load`.

### 10.3 Preconditions and refusals

The same three hardware reads as the Released path (reg 0x01 == 0x22;
loader-sensor bit set with the idle status class; regs 0x3b/0x3c),
plus one new check specific to the Ejected kind, in this order — with
one difference in what regs 0x3b/0x3c must read, learned on hardware
(Test 90, 2026-09-27): the jog leaves the OPEN table's 0x00/0x00, but an
eject rewrites neither register, so after an eject they hold the LAST
SCAN PROFILE's values (0x02/0x00 after 600 dpi, 0x00/0x01 after every
other profile). The first hardware run refused on exactly that, with
the 0x00/0x00 requirement copied from the Released kind. The Ejected
kind therefore requires only that neither register reads 0xff — the
base-table-only state of Test 44, the one state `eject` itself refuses
from — and `open` rewrites both to 0x00/0x00 before `load` anyway
(§10.2). The Released kind keeps 0x00/0x00.

1. **Cold (reg 0x01 == 0x00):** a power cycle happened between the eject
   and this scan. A next-strip load assumes the transport is still homed
   and positioned from earlier in the SAME power-on — the same
   assumption `--next-strip` makes and refuses the same way. Refused
   `SANE_STATUS_INVAL`, pointing at Load film (the jog + reseat path).
   Nothing was written, so the mark is cleared as stale but the state
   drops to **Unknown, not Failed** — this is a read-only refusal, not a
   motor-sequence failure, and the documented ways out (Load film, or a
   fresh `of135i load`) stay open exactly as a fresh Unknown session
   already allows.
2. **Loader sensor clear:** the strip has not been pushed to the stop
   yet (mid-swap, or forgotten). Refused `SANE_STATUS_NO_DOCS`, worded
   for a strip swap rather than the Released kind's "reseat" wording.
   Mark KEPT — pushing the magazine in and scanning again is the whole
   fix, same rule as the Released kind's equivalent refusal.
3. **Anything else wrong** (idle class, regs 0x3b/0x3c as above): the
   generic wrong-state refusal, unchanged in shape — `SANE_STATUS_INVAL`,
   state → Failed, mark cleared — with the message naming the kind that
   applied ("the eject leaves it in" / "the jog leaves it in") and the
   0x3b/0x3c expectation that applied.

**The trade-off this accepts, spelled out:** a scan started after an
eject WITHOUT pushing the magazine to the stop first can still reach the
sensor-present branch if the magazine is only resting against the
mechanism rather than seated — the sensor cannot tell the difference.
The load then runs on a magazine that is not actually engaged, the feed
fails to grip (the documented benign `0xfc` completion signature, Tests
48/49), the sequence fails closed (state → Failed, mark cleared,
`MagazineFailGuard`), and a power cycle plus `Load film` is required to
recover. This is not a new risk: it is the same trade-off the vendor
app's own between-strip load and `of135i/loadflow.py`'s `--next-strip`
already accept, in exchange for skipping the jog and the reinsert
prompt. No code here tries to distinguish "seated" from "merely
present" — the sensor genuinely cannot.

### 10.4 The status line

`kMagazineEjected` changed from "ejected -- press Load film" to
"ejected -- push in, then scan" (still under the 40-character widget
limit, Test 76), because an eject is no longer a dead end for the next
scan. A new value, `kMagazineEjectedPending`, is `magazine_state_text`'s
cross-process analogue of `kMagazinePending` for the Ejected kind:
"ejected earlier -- push in and scan", shown when this process's state
is Unknown but the on-disk mark names this device with kind Ejected —
mirroring exactly how `kMagazinePending` already worked for a Released
mark (Test 77).

**Reworded again, 2026-09-27 (§10.8): all seven values now name "Scan".**
The texts above were the wording live through Test 90/91; after the
second digiKam session (§10.8) they were rewritten so every value ends
by naming the frontend's actual button instead of a generic "scan" the
operator had to interpret for themselves:

| kind | before (Test 76/90/91) | after (this task) |
|---|---|---|
| `kMagazineUnknown` | `unknown -- press Load film` | `not loaded -- press Load film` |
| `kMagazineReleased` | `released -- reseat, then scan` | `released -- take out, push in, Scan` |
| `kMagazinePending` | `reseat the magazine, then scan` | `released earlier -- reseat, then Scan` |
| `kMagazineLoaded` | `loaded -- scan, then Eject film` | `loaded -- press Scan, or Eject film` |
| `kMagazineEjected` | `ejected -- push in, then scan` | `ejected -- swap strip, push in, Scan` |
| `kMagazineEjectedPending` | `ejected earlier -- push in and scan` | `ejected earlier -- push in, then Scan` |
| `kMagazineFailed` | `failed -- power-cycle the scanner` | `failed -- power-cycle, then Load film` |

"Scan" is capitalised on purpose in each: it names digiKam's Basic-tab
button by the word that button actually shows (KSaneCore labels it
"Scan" in English, "Läs in" in Swedish — the button's own text, not
this backend's, so this cannot make the two agree on every desktop, but
it at least makes the STATUS LINE consistent with itself and with the
new tooltip, §10.8). All seven stay under 40 characters and stay
distinct (`tests/test_sane_magazine.py`,
`test_the_status_line_is_readable_and_comes_first`), and `kMagazineUnknown`'s
first word changed from "unknown" to "not loaded" — the internal
`MagazineState::Unknown` name is unchanged, only the text an operator
reads.

### 10.5 Hardware verification (Test 90, 2026-09-27)

Verified on the device from `scanimage`, one power-on, every step its
own process (so the on-disk mark carried the kind across processes):
Load film → reseat → scan frame 1 (600 dpi) → Eject film → strip swapped
and pushed to the stop → scan frame 1 with NO Load film press. The
second scan ran `open` then `load` with no jog, feed and traverse
completing on the first polls (0xf4 / 0xdc), and positioned the frame
identically to the first (FEEDL 6519, POSITION 1428 vs 1433 ms, gain
within ±1 code). **PASS, n = 1** — after one real bug: the first
attempt refused read-only on regs 0x3b/0x3c = 0x02/0x00 (§10.3), fixed
offline in the same session and re-run on the untouched hardware.
`docs/test-log.md` Test 90 has the numbers. What the offline suite
proves is unchanged (state machine, refusals, `open` before `load` on
the wire); what it could not model — the register state a real eject
leaves — is now covered by two scenarios seeded with the measured
values. The in-process case (digiKam: Eject film, swap, scan in one
dialog session) has not been run; it goes through the same code with
the in-process state instead of the mark.

### 10.6 Every mark write and clear is now logged (2026-09-27)

On 2026-09-27 an "ejected" mark went missing between an eject and the
next dialog open, with no log evidence of when or why: before this, a
successful `magazine_mark_write` logged nothing at all (only a failed
write did), and every `magazine_mark_clear()` call site was silent about
its reason. Both write call sites (the Released write in
`magazine_release_impl()`, the Ejected write in `write_ejected_mark()`)
now go through one shared helper that logs `DBG_info` on success too
("magazine mark written: `<kind>` for `<device key>` at `<path>`"), and
every clear site now says why, through a second shared helper
(`clear_magazine_mark(reason)`): `"failed sequence"` (the `MagazineFailGuard`
destructor, and the two preflight checks that find the session already
Failed or the hardware in the wrong state), `"foreign device"` (a mark
naming a different device key), `"cold refusal"` (a next-strip load
refused because the scanner was power-cycled since the eject), and
`"consumed by load"` (a load that completed and the mark it was
satisfying no longer applies). This is traceability only — no state
machine transition, precondition or refusal changed; `tests/test_sane_magazine.py`
(all 28 tests) and `tests/test_sane_lock.py` still pass unchanged.

### 10.7 The status line can go stale in the frontend, independent of any of this (2026-09-27)

Everything in §10.1–10.6 is about what this backend computes and writes
for `magazine_state_text()` — the state machine, the mark, the logging.
Test 91 (2026-09-27, `docs/test-log.md`) found a failure mode that has
nothing to do with any of it: the backend can recompute and return the
correct text on every `sane_control_option` GET, and the *frontend* can
still keep showing the previous one. KSaneWidgets' `LabeledCombo` (the
widget libksane draws for this value-list option) updates via
`setValue`, which matches the combo's item data — this option's INTERNAL
values, `magazine_state_values()` — against the value
`KSaneCore::Option::valueChanged` carries, which is TRANSLATED whenever
the current msgid has a catalog entry. With a translation installed,
internal and translated text never match, so the combo silently stops
following backend-side changes. In Test 91 the backend correctly moved
to Released after the first "Ladda film" and correctly returned
"released -- reseat, then scan" on the next GET; the dialog kept showing
its earlier text regardless, the operator pressed Load film a second
time on an already-released magazine, and the following scan fed at the
un-seated-magazine signature (`0xfc`).

This is a **frontend limitation, not a state-machine or mark bug** — it
is the reason `docs/sane-install.md` §6 documents it under "Two libksane
display bugs" rather than here as a WP-4 defect. The workaround is also
frontend-facing rather than a WP-4 change: `magazine_state_values()`'s
seven strings (§10.4 above) are no longer wrapped in `SANE_I18N` (plain
English, a comment at the definition explains why), so there is no
catalog entry for `valueChanged` to translate and the mismatch this
widget checks for cannot occur. The state machine, the mark, and the
five kinds of text `magazine_state_text()` can return are all unchanged
by this — only whether a frontend has any chance of translating the
result.

### 10.8 The status line rendered disabled, and the owner's verdict (2026-09-27)

A second live digiKam session ran the same evening as Test 91, after the
English-only fix from §10.7 was installed. It passed on the mechanics —
Load film, reseat, Scan, Eject film, swap strip, Scan again with no Load
film (the next-strip load, this time in-process rather than across two
`scanimage` invocations) all worked, and both the cross-process and
in-process next-strip loads are now hardware-verified (`docs/test-log.md`
Test 91, second paragraph). But the status line was still rendered
DISABLED — KSaneWidgets' `LabeledCombo` greys out both the label and the
value for any option that is `SANE_CAP_SOFT_DETECT` without
`SANE_CAP_SOFT_SELECT`, which is what `magazine` had been since Test 76
("read-only, so the widget can't be misused"). The operator needed to be
walked through the sequence from outside the dialog to complete it. His
verdict, verbatim: **"no one can do this process without a written
manual."**

That is a limitation of the KSane dialog surface, not a bug in it: SANE
has no mechanism for a backend to pop up a prompt, so a status line and a
written cheat sheet are the only channel a backend has at all — and a
status line rendered in grey text nobody expects to be able to read is
barely a channel. Three changes, all offline, all in this same change:

1. **The option is now settable.** `cap` gains `SANE_CAP_SOFT_SELECT`
   (`genesys.cpp`, `init_options`): `SANE_CAP_SOFT_SELECT |
   SANE_CAP_SOFT_DETECT` instead of `SANE_CAP_SOFT_DETECT` alone. This
   renders the label and value ENABLED (black) in KSaneWidgets. Nothing
   about the state machine or the mark becomes settable, though: the new
   `case OPT_MAGAZINE` in `set_option_value()` (`genesys.cpp`) is a
   documented no-op — it sets only `*myinfo |= SANE_INFO_RELOAD_OPTIONS`,
   which makes the frontend immediately re-read the option and re-display
   the TRUE value, so a SET "succeeds" (any of the seven listed values is
   accepted, `SANE_STATUS_GOOD`) while changing nothing observable. A
   value outside the seven is rejected before this handler ever runs, by
   SANE core's own `sanei_constrain_value` (the string-list constraint
   check in `sane_control_option_impl`) — unmodified, and exercised
   through the real path by
   `tests/test_sane_magazine.py`'s `test_setting_the_status_line_is_a_no_op`.
2. **All seven values reworded to name "Scan"** — §10.4 above has the
   before/after table. A value like "released -- reseat, then scan" left
   the operator to work out for themselves which button "scan" meant;
   "released -- take out, push in, Scan" names it.
3. **The option's tooltip (`desc`) now spells out the whole procedure**:
   "1. Load film. 2. Take the magazine fully out, push it back in to the
   stop. 3. Set Frame, press Scan (Basic tab). 4. Eject film. Next strip:
   swap, push in to the stop, Scan -- no Load film." — the same steps as
   the README's new "digiKam cheat sheet", so the two channels a SANE
   frontend actually has (a status line and a written sheet) say the same
   thing. The option is also retitled from "Film magazine" to "Magazine --
   next step".

**None of this has been tried live.** The next digiKam session is what
would show whether an enabled, better-worded status line and a spelled-
out tooltip actually reduce how much external guidance the process
needs, or only make the status line legible without closing the gap the
owner's verdict named. `docs/ROADMAP.md` "digiKam dialog usability"
tracks that as still open.

## 11. Superseded by WP-5 for the frontend path (2026-09-27, offline)

The owner's verdict after §10.8's changes were tried live (Test 91,
second session, `docs/test-log.md`) was that even an enabled, better-
worded status line and a spelled-out tooltip were not enough: "no one can
do this process without a written manual." `docs/sane-wp5-load-button.md`
replaces the two-call protocol this document designed (§2's state
machine, §3's five programs' orchestration into "release now, load at
the next `sane_start`") with ONE button: "Load film" runs cold-init-if-
needed, OPEN, JOG (skipped when nothing needs releasing), a read-only
wait for the operator's reseat, and LOAD, all inside a single call.

**What stays true, unchanged, and is NOT superseded:**

- The five programs themselves -- `cold_init`, `open`, `jog`, `load`,
  `eject` -- byte-identical to the Python driver, wire-equality-tested
  exactly as this document describes (§3, §5).
- The cross-process mark mechanism (§2.1, §10.1): `gl126_lock.h`'s
  `MagazineMarkKind`, now three kinds (`Released`, `Ejected`, and a new
  `Loaded`, WP-5 section 3.3) instead of two, still living next to the
  process lock and still re-verified against the hardware before any use.
- `eject-film` and its own state machine and preconditions (§3.4, §10.2,
  §10.3): unchanged by WP-5, since the button that used to trigger a
  next-strip load automatically at the next `sane_start` (§10) no longer
  does -- see below.
- The safety model: `MagazineFailGuard`, `validate_scan_request()`'s
  existence (still called from `offset_calibration()`, no longer from the
  load half, which cannot move the magazine any more -- see below), the
  register preconditions Test 90 established (§10.3), all still true, now
  checked from inside "Load film" itself rather than from
  `load_document()`.

**What §3.3 and §10 above describe as HISTORICAL, no longer how the code
behaves:**

- §3.3's "Stage B in `load_document()`": `load_document()` does not run
  LOAD at all any more. It is a pure checker (WP-5 section 3.4): Loaded
  (in-process or a `loaded` mark) lets a scan through; Released or Ejected
  refuses `SANE_STATUS_NO_DOCS` "press Load film first"; Unknown with no
  mark proceeds with the old warning; Failed refuses. It performs zero
  device I/O -- not even a register read -- which retires this document's
  own §9's Astra-review concern about validating the scan request before
  moving the magazine: nothing in `load_document()` can move the magazine
  any more, so there is nothing left to protect it from.
- §10's "Ejected is a pending load, completed automatically at the next
  `sane_start`, no `load-film` press, no reinsert prompt": this no longer
  happens on Scan. Since WP-5, "Load film" pressed from an Ejected state
  (in-process or an "ejected" mark) is what completes the next-strip
  load -- it waits for the edge (present, since the strip is being
  swapped and the sensor may already read that way, WP-4 section 10.3's
  own trade-off), then runs `open` (§10.2's reasoning, unchanged) then
  `load`, with no jog. The operator presses one button either way; WP-5
  removed the one path that let a bare Scan load anything.
- The two-step protocol's own state-machine diagram (§2.2) and its
  worked example (§2.3, "Load film, reinsert, Load film again, reinsert,
  scan"): still an accurate description of what the FIVE PROGRAMS do and
  in what order, but the button sequence a Christian-shaped operator
  actually presses is WP-5's, not this.

Nothing above changed a byte of any op program, a poll condition, a
timeout, calibration, POSITION, PARK, the image path, or the Python
driver -- `docs/sane-wp5-load-button.md` section 4 says so explicitly,
and it is the same claim this document makes throughout. The WP-3
submission package (§ "Relation to the other packages" in
`docs/sane-wp5-load-button.md`) must be re-exported after WP-5, since its
§8 gets the new hunks (`OPT_CHECK_STATUS`, the option handlers, the test-
mode register defaults).
