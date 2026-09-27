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

## 8. Relation to the other packages

Supersedes the two-call protocol of WP-4 §3 and §10 for the frontend
path (WP-4's programs and preconditions remain the building blocks). The
WP-3 submission package must be re-exported after this (its §8 gets the
new hunks). The Python driver's `of135i load` keeps its Enter prompt; it
could adopt the same sensor edge later (candidate, not part of WP-5).
