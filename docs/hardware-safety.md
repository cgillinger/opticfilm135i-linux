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
for every operation that can write to the scanner. This document
states what the model guarantees, where those guarantees are enforced,
and what is verified.

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
  execute pulse;
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
  the proxy, so no "recovery" can slip past it. This is the chokepoint:
  the driver has no other path to the bus.
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

## `doctor` and `status` are strictly read-only

Both open a read-only session (`UsbIo.open(readonly=True)`) that can
never be armed and never issues a write; `set_configuration` is not
even sent (the kernel configured the device at enumeration). Offline
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

The loader sensor (`is_magazine_loaded()`, register 0x101 bit 0x08) is
**not** a proof that the magazine is safe to move. The 2026-09-04 tests
showed:

- the sensor can report a magazine that is loose and not mechanically
  latched (Test 11b: sensor "loaded", blue LED, magazine physically
  loose);
- the sensor bit becomes unreliable once a session has written the base
  register table (Test 11d), so it is only trustworthy **before the
  first `initialize()`** of the scanner's power cycle;
- a blue LED does not prove the magazine is latched (the operator has
  seen it blue when Plustek's own indication should be orange).

So distinguish, and do not conflate:

| state | how it is known |
|---|---|
| **detected** | loader sensor bit set (before first `initialize()` only) |
| **physically inserted** | a person pushed the cassette in to the stop |
| **fully seated / locked** | a person confirmed it by hand and by LED colour |
| **sensor before `initialize()`** | the only point the sensor bit is trusted |
| **sensor after `initialize()`** | unreliable — do not use as a safety signal |
| **current transport state** | register 0x01 / status word, not the sensor |

`--assume-loaded` (in `tools/hwblock.py`) is for controlled development
use only, when a person has physically confirmed the magazine is seated
and locked. It skips the sensor precheck; it is **not** a recovery
mechanism and does **not** bypass the start-state guard. No new motor
experiment was run to investigate the magazine states during the safety
pass.

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

- **Offline verified** (`tests/test_safety.py`, 27 tests, run with the
  full existing offline suite): the start-state matrix (0x22 / 0x00 /
  0x02 / 0x23 / unknown / read timeout / USB error / malformed reply)
  for every public writing entry point, counted at the pyusb boundary
  as **zero OUT transfers** on every refusal; the cold path (0x00
  permits cold-init only, once, post-verified); fault injection before
  the first write, after the first register write, immediately before
  and after an execute pulse, during calibration bulk-in, during image
  bulk-in, during PARK (verbatim and semantic), between batch frames,
  during eject and during magazine load, plus `KeyboardInterrupt` —
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
- **Still requires hardware or cross-unit verification**: the accepted
  start-state values (0x22 / 0x00) and the post-cold-init `0x22`
  expectation are this unit's observed constants; the meaning of `0x02`
  after a load (Test 11b) is still unexplained, so the load→new-session
  workflow stays refused until it is understood; the magazine-latch
  distinction above has no reliable sensor signal yet.
