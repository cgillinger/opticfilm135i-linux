# Hardware safety model

This driver controls a single, irreplaceable scanner. A hardware
incident on 2026-09-04 (docs/test-log.md, Test 11d) showed how a
software mistake can brick it: an aborted session left the scan engine
running (register 0x01 = 0x23), a new session was started on top of
that state, its register writes and execute pulse hit the running
engine, and the firmware hung — the scanner stayed enumerated but
answered nothing until the power was cut.

The safety pass (2026-09-05) generalised the ad-hoc check that
`tools/hwblock.py` had grown into one authoritative mechanism enforced
for every operation that can write to the scanner. A follow-up review
closed two gaps in it (verification before the USB open sequence, and
short OUT transfers; see below). This document states what the model
guarantees, where those guarantees are enforced, and what is verified.

## Accepted start states

Before a session's first USB write, the driver reads register 0x01
exactly once and classifies it:

| reg 0x01 | meaning | what is permitted |
|---|---|---|
| `0x22` | idle, homed (the vendor's ready state) | normal operations |
| `0x00` | cold, never homed (fresh power-on) | the cold-init path only |
| anything else (`0x02`, `0x23`, `0x20`, …) | undefined / engine running | nothing — refused |
| unreadable (USB error, timeout, short or malformed reply) | unknown | nothing — refused |

`0x00` permits **only** the vendor cold-start sequence
(`Scanner.cold_init()`, run automatically by `initialize()` and
`eject()`). When that sequence finishes, register 0x01 is read again
and must be `0x22`; if it is anything else, the session is refused
there with no further write — that is a new, unexplained observation
and is reported as such.

Every other value fails closed. There is deliberately no `--force` or
any other flag that bypasses this.

## What "fail closed" means

When the start state is not accepted, or a read fails, or a write fails
mid-operation, or the operation is interrupted (Ctrl-C):

- **zero USB writes after the failed check** — no `initialize()`, no
  execute pulse — **and zero state-changing standard requests**: no
  `SET_CONFIGURATION`, no kernel-driver detach, no reset, no
  `clear_halt`, no alternate-setting change. "Nothing was sent" means
  nothing at all, not merely no vendor command;
- **no recovery sequence of any kind** — no PARK, no home, no eject, no
  re-initialization, nothing in a `finally`;
- a dedicated exception (`of135i.safety.SafetyError` and its
  subclasses) carrying the observed register value where available;
- an instruction to power-cycle: turn the scanner off, wait until its
  lights go out and it leaves the USB bus, then turn it on. Restarting
  the program is explicitly **not** sufficient.

Restarting the process does not recover the hardware. The engine state
lives in the scanner, not the driver, so a fresh process reads the same
unsafe register and refuses again — which is the point.

## Where it is enforced

The guarantees live in the shared driver layer (`of135i/safety.py`),
not in any one frontend:

- **`GuardedDevice`** wraps the pyusb device inside every `UsbIo`.
  Direction is decided from the wire (the bmRequestType direction bit,
  the bulk endpoint), so every control-OUT and bulk-OUT transfer — from
  `UsbIo`'s own helpers, from `device.py`'s verbatim op executor, or
  from any tool — passes through one gate that asks permission before
  the transfer and counts it (execute pulses included) after. pyusb's
  own state-changing standard requests (`clear_halt`, `reset`,
  `set_configuration`, altsetting/kernel-driver changes) are blocked on
  the proxy, so no "recovery" can slip past it. The proxy also holds
  the **only** reference to the raw pyusb handle (`UsbIo` stores none),
  so driver and tool code has no attribute through which to reach the
  bus around it. The one exception is the USB open sequence itself,
  issued by `UsbIo.open()` on its local handle *after* the start state
  has been verified (next section). A short OUT transfer — pyusb
  reporting fewer bytes than were requested — is not a completed write:
  it fails the session on the spot (see "Short OUT transfers").
- **`HardwareSession`** is the per-process state machine
  (`unverified → armed | cold | refused`, plus the final `failed` and
  the read-only `readonly`). It records the start verdict, the write
  and execute-pulse counts, the current phase, and the first failure.
- **`Scanner._operation()`** runs every public writing method
  (`initialize`, `cold_init`, `scan`, `eject`, `home`, `park_semantic`,
  `load_magazine`) as an operation. The first operation of a session
  runs the start-state check before its first write; any exception
  escaping any operation — `KeyboardInterrupt` included — marks the
  session failed and nothing is sent afterwards. The low-level
  executor `_exec_ops()` refuses to run outside an operation, so a
  caller cannot reach the wire around the guard by invoking it
  directly.

Because the check is per session and not per write, a legitimate batch
scan is one session: the start state is verified once, and the
transient engine states inside the session (0x02/0x03/0x23 between
phases) are expected, not re-checked. A new process, or an
independently invoked operation after a failure, is a new session and
is validated again.

## Opening a session: verification before configuration

A writing session (`UsbIo.open()`, hence `Scanner.open()` and every
CLI command and tool) proceeds strictly in this order:

1. take the process lock;
2. find the device (`usb.core.find` — a descriptor lookup, no request
   to the device);
3. create the single `HardwareSession` that owns the whole session,
   with the `GuardedDevice` proxy around the handle;
4. read register 0x01 once, strictly, through that proxy — a
   device-recipient vendor control-IN on endpoint 0, the same read
   `doctor`/`status` use. Nothing state-changing precedes it: no
   `SET_CONFIGURATION`, no kernel-driver detach, no reset, no
   `clear_halt`, no altsetting change;
5. classify with the one central rule. Anything but `0x22`/`0x00`, a
   USB error, a timeout, a short or malformed reply, or an interrupt
   during the read: the session is **refused**, the handle and the
   lock are released, and the error says that nothing was sent and
   that a power cycle is required. Zero vendor OUT transfers and zero
   state-changing pyusb calls have happened;
6. only then the verified open sequence on the raw handle: detach a
   bound kernel driver if there is one, then `SET_CONFIGURATION`. If
   that fails, the **same** session is marked failed (the device may
   be half configured), everything is released, and the error again
   requires a power cycle.

The session and its verdict survive step 6 unchanged; the operation
layer's own start check just returns the recorded verdict without a
second read. If a kernel driver were bound and the read failed
because of it, the driver refuses at step 5 — it never detaches first
merely to make the check possible. A kernel-driver detach is treated
as state-changing even though it need not produce an on-wire packet.

`open(readonly=True)` (`doctor`, `status`) stops after step 3: no
verification (a read-only session is never armed) and no standard
request of any kind.

## Short OUT transfers

pyusb reports the number of bytes a control-OUT or bulk-OUT transfer
moved. The proxy compares that with the length of the payload it was
given (zero for the verified zero-length control requests, which pyusb
reports as 0). Any other value means part of a register batch or
buffer may have reached the scanner and part may not — a hardware
state the driver cannot know. Therefore a short transfer:

- marks the session **failed** at once and raises
  `of135i.safety.ShortTransferError`, whose message and the session's
  failure record carry the requested and reported lengths, the
  operation and phase, and whether the payload contained an execute
  pulse;
- is counted as **attempted** but never as **completed**
  (`writes_attempted` includes it, `writes` does not; likewise
  `execute_pulses_attempted` vs `execute_pulses`);
- is followed by nothing: no retry, no further command, no PARK, home,
  eject, initialize, `clear_halt` or reset;
- requires a power cycle, and the operator text says explicitly that
  some or all of the bytes may have reached the scanner.

## Protected entry points

Every public path that can cause a USB write or physical movement:

| entry point | protection |
|---|---|
| `of135i scan` / `Scanner.scan()` | operation; requires `initialize()` first, before every frame |
| `of135i eject` / `Scanner.eject()` | operation; cold-init first only from `0x00` |
| `Scanner.initialize()` | operation; cold-init first only from `0x00` |
| `Scanner.cold_init()` | operation; only from `0x00`, only once per session, post-verified |
| `Scanner.load_magazine()` / `tools/load_magazine.py` | operation; requires `initialize()` first |
| `of135i watch` (button-triggered eject) | writing session; start state checked before the poll loop; a failed eject ends the watch |
| `Scanner.home()` | operation (kept for completeness; used by no tool or flow) |
| `Scanner.park_semantic()` | operation |
| `tools/hwblock.py` (warm/cold) | asks the driver for the start verdict (read-only), then drives the guarded `Scanner` |
| `tools/replay_trace.py` | opens the guarded `UsbIo`, refuses unless `0x22`, aborts on the first USB error |
| `of135i status`, `of135i doctor` | read-only session: can never write or be armed |

`Scanner.__exit__` closes the USB handle and the process lock only. It
never parks, homes, ejects or initializes — leaving a block by an
exception means the hardware state is unknown, and the only valid
recovery is a power cycle.

### Raw pyusb access audit

Every `ctrl_transfer`/`write` in `of135i/` and `tools/` goes through
`io.dev`, the `GuardedDevice` proxy (`usbio.py` helpers, `device.py`'s
op executor and its inline chip-id/EEPROM reads, `diag.py`'s read-only
collectors, `tools/replay_trace.py`'s replayer, which is handed
`io.dev`). `usb.core.find` occurs once, in `UsbIo.open()`, after the
lock. `set_configuration`/`detach_kernel_driver` occur once, in
`usbio._configure_for_writing()`, called only from `UsbIo.open()` after
verification; `clear_halt`/`reset`/altsetting/attach occur nowhere in
production code and are blocked on the proxy. No `_raw_dev` attribute
exists; the raw handle lives only as a name-mangled attribute of the
proxy, used for the transfers themselves and for releasing the handle
on close. Tests use fake devices deliberately, isolated in `tests/`.

What the proxy cannot prevent is deliberate circumvention: Python
allows reaching the mangled attribute, or the pyusb context object
that the proxy passes through by name. Nothing in the driver or tools
does either; a future contributor who does is bypassing the safety
model on purpose, and review must catch it.

## `doctor` and `status` are strictly read-only

Both open a read-only session (`UsbIo.open(readonly=True)`) that can
never be armed and never issues a write; `set_configuration` and the
kernel-driver detach are not even sent (the kernel configured the
device at enumeration). Offline
tests prove that a `doctor`/`status` run performs zero OUT transfers,
triggers no pyusb state-changing call, runs no `initialize()`/
`cold_init()`, and attempts no recovery — even against an interrupted
scanner reading `0x23`, where it reports the state and the power-cycle
advice but does nothing about it. `doctor` may diagnose an interrupted
scanner; it must never try to fix it.

## Cross-process exclusion

`UsbIo.open()` takes an exclusive `flock` on `/tmp/of135i-07b3-1436.lock`
(override with `OF135I_LOCK_FILE`) before any USB access, and holds it
for the whole session. A second of135i process — writing **or**
read-only `doctor` — is refused with `ScannerBusyError` before it
touches the device; two processes configuring or driving the scanner
at once is itself a hazard, so `doctor` respects the lock rather than
racing a running scan. The kernel drops the lock when the holder
exits, so a crash never leaves a stale lock. Releasing the lock says
nothing about the scanner's physical state: a failed session releases
it too, and the hardware may still need a power cycle.

## Magazine state is a separate, unresolved issue

The loader sensor (`is_magazine_present()`, register 0x101 bit 0x08) is
a **presence** sensor and nothing more — the API, `doctor` and this
document say "present", never "loaded". The 2026-09-04 tests showed:

- the sensor reports a magazine that is merely inserted, loose and
  unlatched, with the orange LED, read on a cold, unwritten register
  (Test 12a) — and one that is loose after a load with a blue LED
  (Test 11b);
- the sensor bit becomes unreliable once a session has written the base
  register table (Test 11d), so it is only trustworthy **before the
  first `initialize()`** of the scanner's power cycle;
- a blue LED does not prove the magazine is latched (the operator has
  seen it blue when Plustek's own indication should be orange).

So distinguish, and do not conflate:

| state | how it is known |
|---|---|
| **present** | loader sensor bit set (before first `initialize()` only) — inserted, nothing more |
| **physically inserted** | a person pushed the cassette in |
| **fed / load completed** | `load_magazine()` ran AND both motor completions and the final status read passed the masked completion test (state class AND loader-sensor bit 0x08, `device.load_status_matches`); otherwise the load has failed and the session with it |
| **fully seated / latched** | a person confirmed it by hand — no register signal is known for this |
| **sensor before `initialize()`** | the only point the sensor bit is trusted |
| **sensor after `initialize()`** | unreliable — do not use as a safety signal |
| **current transport state** | register 0x01 / status word, not the sensor |

**Load completion is verified, not assumed.** The two motor-completion
polls of the LOAD replay are strict under a masked test
(`device.load_status_matches`, mask 0xfb on the status byte): the reply
must have the captured *state class* (upper nibble) AND the captured
*loader-sensor bit* 0x08 AND the busy bit clear; only bit 0x04, which
differs between vendor sessions (0xf0/0xf4, 0xd8/0xdc), is ignored.
After the feed the capture reads 0xf455 — done, sensor bit CLEAR, the
cassette pulled past the sensor; after the traverse 0xdc55 — done,
sensor bit SET again. A timeout stops the flow right there
(`StrictPollTimeoutError`): a feed that did not engage the cassette
never gets a traverse. After the replay the status word is read again
and must pass the same test against the traverse target, or
`LoadIncompleteError` is raised. Either way the session is FAILED,
nothing further is sent, and a power cycle is required. On the real
scanner the old table answered 0xec55 after the feed (Tests 12/13,
sensor bit still set) and 0xcc55 after the traverse; both fail the
test. Neither an exact value (it rejected the correct vendor load),
nor a state-class range (0xf855 would pass with the cassette still in
front of the sensor), nor the sensor bit alone (a running engine would
pass) is acceptable. The unengaged loads are analysed in Tests 14 and 15 of
[`test-log.md`](test-log.md): the table is now generated from the clean
standalone load, and the load is preceded by the vendor's app-start
jog (`Scanner.jog_magazine()`, `tables_load.JOG`, same strict masked
polls), which every engaging vendor load had and none of ours did. The
jog + reinsert + load flow is **not yet hardware-verified**. Background in
[`load-analysis.md`](load-analysis.md).

`--assume-locked` (in `tools/hwblock.py`) is for controlled development
use only, when a person has physically confirmed the magazine is seated
and locked. It skips the sensor precheck; it is **not** a recovery
mechanism and does **not** bypass the start-state guard. No new motor
experiment was run to investigate the magazine states during the safety
pass.

## Lamp warmup is bounded and fails closed

After a cold start the first white-line measurement can be dark enough
that every channel's gain code clips at maximum. `_gain_with_warmup`
then re-measures (a single-line read, no motor move) every 5 s and
accepts only two consecutive usable measurements that agree within 3 %
— calibrating on a still-rising lamp would fix the gain at a level the
lamp then overshoots. The wait is bounded by `Scanner.warmup_budget_s`
(default 60 s, CLI `--warmup-budget`) both by the clock and by a hard
cap on the number of measurements. Exhausting it, a saturated white
line (65535 at gain 0: an implausible AFE state) or a malformed
measurement raises `LampWarmupError`: no scan, no motor command, the
operation fails like any other and a power cycle is required. USB
errors, timeouts and Ctrl-C during the wait propagate untouched.
A warm lamp takes the same single-measurement path every verified scan
has taken. Offline-verified (fault injection for a dark lamp, gradual
warmup, a never-stable lamp, saturation, malformed data, Ctrl-C and USB
errors); the bounded wait itself has not yet been seen on hardware.

## A cold-started session loads before it scans

The vendor never scans after a power-on without loading: its app start
runs the jog, the user reinserts the magazine, the load's feed and
traverse put the transport at the scan reference position. Test 22
(2026-09-05) showed what a scan straight after `cold_init` sees: 51
white-line measurements over 295 s, all dark (~15/65/50 of 65535) —
not a lamp warming up, no light at the sensor at all; the same cause as
the "flat cold-start images" of Tests 1/9/11. `scan()` therefore raises
`OperationNotAllowedError` (nothing sent) in a session that ran
`cold_init` until `load_magazine()` has completed in that session. A
warm session (0x22) is unaffected: the reference position survives an
eject and a session change, not a power cycle. The former `hwblock
cold` block is retired for the same reason.

## Recovery after an interrupted scan

An interrupted or failed writing operation (Ctrl-C, a crash, a USB
timeout or disconnection, an unexpected register response, a failure
during calibration, image transfer, PARK, or between batch frames)
leaves the hardware state **unknown**. The driver does not try to prove
otherwise. The only recovery is:

1. Power the scanner off; wait until the lights go out and it leaves
   the USB bus.
2. Power it on.
3. Start again from a known state (`doctor` first; then the normal
   flow, or the cold path if it reads `0x00`).

Never start a new session on top of an interrupted one. Restarting the
program is not recovery.

## Verification status

- **Offline verified** (`tests/test_safety.py`, 38 tests, run with the
  full existing offline suite): the start-state matrix (0x22 / 0x00 /
  0x02 / 0x23 / unknown / read timeout / USB error / malformed reply)
  for every public writing entry point, counted at the pyusb boundary
  as **zero OUT transfers and zero state-changing pyusb calls** on
  every refusal; the real `UsbIo.open()`/`Scanner.open()` path over a
  fake device (unsafe/unreadable/interrupted start-state read: refused
  with nothing sent, kernel driver left bound, lock and handle
  released; 0x22/0x00: the reg 0x01 read precedes detach and
  `SET_CONFIGURATION`, one session survives, no second read; a
  configuration failure marks that session failed); short control-OUT
  and bulk-OUT transfers before, containing and after an execute
  pulse, a 0-byte report for a non-empty payload, the legitimate
  zero-length requests of the cold path (complete), and a short
  transfer that leaves the engine running blocking a driver-level
  restart; the cold path (0x00
  permits cold-init only, once, post-verified); fault injection before
  the first write, after the first register write, immediately before
  and after an execute pulse, during calibration bulk-in, during image
  bulk-in, during PARK (verbatim and semantic; the semantic PARK's two
  waits fail closed on timeout), between batch frames,
  during eject and during magazine load, a load whose completion
  status fails the masked completion test (0xec55, 0xf855, 0xcc55,
  0xd455: failed operation, failed session, tool exit 1), plus `KeyboardInterrupt` —
  each asserting the session fails, records phase and write/execute
  history, sends no recovery command, refuses every further operation
  with zero writes, and rejects a new session over the left-behind
  state unless it is an accepted start state again; the process lock
  (second process refused before touching USB; lock released on exit);
  and that `doctor`/`status` are read-only against every start state,
  including 0x23.
- **Hardware verified**: nothing new. The guard was deliberately **not**
  exercised by recreating an unsafe physical state (that is what
  bricked the scanner). The rejection paths are proven with mocked
  register values only. The one permitted hardware check for this pass
  is a single conservative normal-path scan from a power-cycled, known-
  good scanner — not a repeat run, DPI sweep, or full `hwblock`.
- **Still requires hardware or cross-unit verification**: that a
  device-recipient control-IN read of reg 0x01 succeeds on this scanner
  *before* `SET_CONFIGURATION` (Linux configures the device at
  enumeration and pyusb neither configures nor claims an interface for
  such a request, but the driver has only ever read after configuring;
  the read-only `doctor` of Test 11a also ran before the read-only open
  existed). If the read fails on hardware, the driver refuses — it does
  not configure first — and the cause must be investigated offline
  before anything is changed; the accepted
  start-state values (0x22 / 0x00) and the post-cold-init `0x22`
  expectation are this unit's observed constants; the meaning of `0x02`
  after a load (Test 11b) is still unexplained, so the load→new-session
  workflow stays refused until it is understood; the magazine-latch
  distinction above has no reliable sensor signal yet.
