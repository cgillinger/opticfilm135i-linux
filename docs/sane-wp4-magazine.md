# WP-4 — magazine handling inside the SANE backend (design, offline)

Status: **design decided 2026-09-13, implemented OFFLINE the same day,
reviewed and corrected the same evening (§9), NOT hardware-run.** The two hooks (`load_document()`, `eject_document()`)
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
  benign, but a failed session by the safety model).
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
       │             = double │  └── eject-film ──► Ejected
       │             jog)     │                         │
       └───────── any failure (Failed: power cycle) ◄───┘

- **Unknown** is what `sane_open` starts in (it writes nothing; the
  hardware may be cold, idle, loaded — the backend does not know).
- **load-film from Unknown:** reg 0x01 must read 0x22 or 0x00. 0x00 →
  the cold-start program first (§3.1), then reg 0x01 must read 0x22;
  then OPEN, then JOG. → Released.
- **load-film from Released:** JOG only, from the loose position —
  this is Test 51's second jog, the recipe for "power-cycled + latched
  magazine" (§2.3). → Released.
- **load-film from Loaded / Ejected:** OPEN + JOG, which is the
  vendor's app open with a latched magazine (it releases it; capture
  `20260907-vendor-open-with-latched-magazine`, Test 46 timeline).
  → Released.
- **sane_start from Released:** Stage B (§3.3). Success → Loaded;
  failure → Failed, `sane_start` returns the error, nothing further
  written.
- **sane_start from any other state:** no magazine action; today's
  behaviour.
- **eject-film from Loaded / Unknown (idle 0x22):** the eject program
  (§3.4) with the Python driver's guards. → Ejected. From Released:
  refused (the magazine is already loose; there is nothing to eject —
  the jog was the eject). From cold: refused with "press Load film"
  (the jog releases it; ejecting from an unhomed transport is not a
  verified sequence).
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
  with its own per-op budget. The Python cold start (`poll_status_word`)
  and eject (`_eject_body`'s completion loop) are *non-raising* by
  design: with a latched magazine the cold start shows a status-word
  timeout every time (Tests 45/51) and still completes, and a strict
  poll there would refuse exactly the case the standing requirement is
  about. `PollMasked` (fail-closed) stays for the motor completions the
  driver runs strictly.

### 3.1 `cold_init` (hand-built, like `park`)

`Scanner._cold_init_body()` step for step: chip handshake, status word,
the ready poll (mask 0xf0 / 0xf0, 15 s, best-effort), the cold register
table + end-of-access + AFE bring-up (125 pairs in 4 batches, the
EEPROM reads as logged reads of 3 and 19 bytes), the reg 0x31
read-modify-write pair, then three homing rounds (feed 6690 / feed
6690 / eject 3090 with the loader slope table to both RAM addresses,
each completion polled for 0xf8 best-effort with a 30 s budget) with
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
5. The `load` program. **Its FIRST completion** — the engaging feed —
   not reaching 0xf4 is the "magazine was not reseated" case, and only
   that one gets the driver's `FEED_NOT_ENGAGED_MSG` wording ("the
   scanner is fine and nothing is stuck"). A failure at the traverse's
   completion, or anywhere else, keeps the neutral message: that
   reassurance is a claim about one documented benign signature, not
   about every timeout in the sequence (§9).
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
Marks cleared → Ejected.

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
   the next op (recorded), never throws.
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
5. **Cold start and eject polls are best-effort, as in the driver**
   (`PollBestEffort`); JOG/LOAD motor completions are fail-closed
   under the driver's mask. Cold start's completion is verified by
   reg 0x01 = 0x22 afterwards, as in the driver.
6. **Eject is never run from cold** (Python's `eject()` does
   `cold_init` first; the SANE flow sends the operator to Load film
   instead, whose jog is the vendor's own release). One less motor
   path to bring up.
7. **Stage B refuses read-only when the sensor bit is clear**
   (`SANE_STATUS_NO_DOCS`, mark kept) rather than running the feed to
   find out.
8. **`sane_cancel` does not eject**; the model stays non-sheet-fed.

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
   or a bulk failure. The hook now keys the message on which completion
   failed, and the test suite pins the two completions' order and their
   loader-sensor bit so the distinction cannot drift.

Also corrected in `docs/sane-wp4-hardware-plan.md`: the mark's path
(`<lock path>.magazine`, i.e. `/tmp/of135i-07b3-1436.lock.magazine`),
and the stop rules — a failure ends the approved attempt and the log is
read before any further motor command, rather than "power-cycle and
start over".
