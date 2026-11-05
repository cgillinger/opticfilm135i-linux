# Calibration stop gate (`--cal-wait-scanstop`)

Status 2026-11-05: **`enforce` is the default** (this change; section
"Decision" below). `--cal-wait-scanstop off` is the way back and sends
exactly the transfers of a driver without the gate. Run on hardware on
three units: the owner's (Test 114: harmless there, every wait <= 8 ms
against the 0.3 s budget, no residual dark_b with or without the gate;
Test 115: the same in a gated scan directly after a cold load), a second
unit on master (bearyjd, 2026-11-04: gate off 0 of 3 scans completed,
gate on 3 of 3 plus four warm-up scans, longest wait 24 ms) and a third,
a Steam Deck (Krustenviech, 2026-11-05: gate on 6 runs / 7 frames
completed, two ungated runs failed, longest wait 29 ms).

Credit: bearyjd designed and wrote the gate and its tests and ran the
first A/B; Krustenviech reported the failure, supplied the Steam Deck
logs and ran the A/B on the Deck that shows the gate removes it there.

Origin: issue #35 (Krustenviech, Steam Deck) and bearyjd's fork
`feat/cal-wait-scanstop`, commit f462b0b, ported onto master with the
fork's offline tests. The port is a straight port of the mechanism; the
fork's other calibration features (integrity telemetry, read-back guard,
`dark_fallback`, `--diag-readback-regs`) are **not** included.

## The problem

Test 32 (docs/test-log.md, 2026-09-06) proved that a "residual" dark_b is
the same frame's dark_a tail (8 words repeated 384×), read back 3–4× too
fast (0.5–0.7 ms vs 1.9 ms): the device performed no exposure. On the
owner's unit this hit frames 2–4 of a batch, a varying subset per run,
never frame 1, and Test 33's `_healthy_dark_b()` substitutes the
session's last healthy dark_b. With no healthy dark_b yet (frame 1) it
fails closed with `CalibrationError`.

Issue #35: on a Steam Deck (kernel 6.18, Python 3.13, pyusb 1.3.1) the
residual dark_b appears **on frame 1**, in every fresh session, also with
an empty holder, at 600 and 3600 dpi; a few scans succeeded with an
identical setup. The substitution cannot help there, and after the
failure reg 0x01 reads 0x02, so a power cycle is needed.

## The mechanism (why a faster host makes it worse)

Every calibration acquisition phase (`cal_dark_a`, `cal_dark_b`,
`cal_white`, `cal_gain_check_a`, `cal_gain_check_b`,
`cal_shading_measure`, `cal_shading_verify`) ends the same way in all
tables (600/1200/2400/3600/7200, IR):

    cw 0x0083 0102        SCAN-clear (reg 0x01, bit 0 cleared)
    cr 0x008e wi=0x0020   55
    cr 0x018e wi=0x0022   f055   reg 0x100: data-enable bit 0 CLEAR
    cr 0x018e wi=0x0122   dc55   reg 0x101: motor-enable bit 0 CLEAR
    -> next phase's register writes, SCAN-set, execute pulse

In the capture the vendor host read each register once and saw both
bits clear, so the engine had stopped within the ~4 ms between its
transfers. (For the plain-3600 `cal_shading_measure` the capture shows
the 0x100 read repeated, i.e. the vendor *polled* it there until
`f055`.) Our replayer reads once, counts a mismatch
(`cr_mismatches` in the diag, debug log; each one is also recorded in
`cr_mismatch_details`: phase, op index, register, got/want, first 20 per
scan) and continues straight into
the next phase. Nothing waits for the engine to stop. A faster host
re-arms the next acquisition while the previous one is still running;
the fork's logs put that at 5.7–11.9 ms after the clear, and the reads
that followed were stale.

The `poll` form of the 0x100 read (plain-3600 `cal_shading_measure`) is
no protection either: `_poll_one`'s state-class leniency accepts `f155`
(bit 0 set) as a class match for `f055`.

## What `enforce` does

After the SCAN-clear that closes an execute pulse in a calibration
acquisition phase, and before the **next OUT transfer of any kind**
(control write or bulk OUT): poll reg 0x100 until bit 0 is clear, then
reg 0x101 until bit 0 is clear. Only 2-byte replies with the 0x55 ack
count. Both waits share one deadline, `NONSTRICT_POLL_FLOOR_S` (0.3 s)
less the time the phase's own post-clear 0x100 poll already spent. Every
read is bounded by the time left; a reply that arrives after the
deadline does not count. At the deadline `CalibrationError` is raised
before any write, the session is FAILED, nothing further is sent, and a
power cycle is required (docs/hardware-safety.md). The deadline runs
from the gate's start, not from the clear, so a legitimate host gap
after a clear (the lamp warm-up re-running `cal_white` after its 5 s
interval) is not a refusal.

Each wait is recorded in the `.diag.json` as `cal_scanstop_waits`:
`phase`, `reads_100`, `reads_101`, `last_100`, `last_101`, `outcome`
(`clear`/`timeout`), `wait_s` (the gate's own time) and `since_clear_s`.

Where the gate fires in a fresh calibration (seven times): before the
first write of `cal_dark_b`, `cal_white`, `cal_gain_check_a`,
`cal_gain_check_b`, `cal_shading_measure`, `cal_shading_upload`
(before the shading descriptor and bulk-OUT), and inside
`cal_shading_verify` before the re-upload descriptor. **Nothing arms
after that**: `position`, `scan`, `park` and `cal_rewrite` (the cached
calibration path) are not acquisition phases, so the image pass is
never delayed, and the `_exec_ops` NOTE (no idle wait during the image
pass; it overflows the scanner buffer) is untouched.

`off` (default) adds no read, sleep or clock call;
`tests/test_cal_wait_scanstop.py` pins the default wire against sha256
digests of the transfer log taken on master `1d0091a` before the gate
existed (plain 3600, dual 600, IR 3600, frame 1).

## Evidence so far, honestly

- Fork, one unit, 600 dpi, **slide holder slot 4**, after a warm-up
  scan, interleaved off/on/on/off/off/on: gate off degraded 3/3, gate on
  clean 3/3. One clean 3600 dpi four-slide run with the gate on. The
  fork's own CHANGELOG for its default-on branch says the hardware
  evidence is pending and must not merge before it is complete.
- No gate-off comparison at 3600 dpi on an affected unit; no film-strip
  run on one. How long the bits take to clear was unmeasured when the
  gate was ported; it has since been measured on three units (Test 114,
  "Second unit, on master" and "Third unit" below). The 0.3 s budget was
  inherited from the poll floor; the longest wait seen so far is 29 ms.
- The reporter's other symptoms are **not** explained by the gate, and
  Test 114 showed both on the owner's unit as well: the `prep` phase's
  0x0122 polls time out at the floor reading `d855`/`dc55` where the
  capture has `e855`/`ec55` (the known post-park state classes,
  docs/park-completion-analysis.md; they never reach the per-frame
  diag, whose counters reset after prep), and the cold start's
  `poll_status_word` gave up at `0x4855` once here too, followed by a
  normal cold start. Neither singles out the Deck; neither explains the
  dark_b failure.
- The reporter's first three `.diag.json` attachments needed a
  logged-in GitHub session and were not analysed; the seven sidecars of
  the gated A/B on the Deck are ("Third unit" below).

## Behaviour risk

Before the gate, a residual dark_b on frame >= 2 was recovered by
substitution and the scan completed. With `enforce`, a bit that takes
longer than 0.3 s to clear becomes a hard failure (`CalibrationError`)
that needs a power cycle. The longest wait seen on any unit is 29 ms. A
unit or condition that has not been seen could still exceed the budget;
`--cal-wait-scanstop off` is the way back.

## Test 114 result (owner's unit, 2026-11-02)

Run as planned below, plus a gated 600 dpi dual and a gated IR frame 1:
six runs, 18 frames, exit 0 everywhere, **no** `dark_b_substituted`
anywhere (gate off included), all 70 gate waits `clear` at the first
read of each register (`wait_s` ≤ 0.008 s, `since_clear_s` 0.020–0.042 s,
0.148/0.184 s in the IR shading phases from host time between phases).
`cr_mismatches` did not drop with the gate. Full table in
docs/test-log.md. The gate is safe on this unit; whether it fixes #35
only the reporter's Deck run can show.

## Second unit, on master (bearyjd, 2026-11-04)

Reported on issue #35 with the `.diag.json` sidecars and `doctor`
reports in a gist; full entry in docs/test-log.md. One unit, one
operator, upstream master 6270867, 600 dpi, frame 4, order as asked
(off, on, on, off, off, on), a gated warm-up scan after every load.

| gate | scans | result |
|---|---|---|
| off | 3 | all three failed in `cal_dark_b` after about 2 s (`dark_b is residual and no healthy dark_b has been measured this session`); register 0x01 left at 0x02, power cycle needed each time |
| enforce | 3 + 4 warm-ups | all seven completed; `dark_b_substituted` false, `poll_timeouts` 0, 7 gate waits per scan, all `clear` |

Three failures are consistent, not a rate. Every completed scan exited
4 (aperture coverage not verified, "no aperture found in profile"); that
is the image check and is unexplained here (the report says "slide frame
4"; which holder was in the unit has not been confirmed).

### What the two units' numbers say

Measured, from the sidecars (`cal_scanstop_waits`; 49 waits on the
second unit, 70 on the owner's in Test 114). The gate sleeps 4 ms
between reads that find the bit set, so the time of one control read is
`(wait_s − 4 ms × (reads_100 − 1)) / (reads_100 + 1)`.

| | second unit and its host | owner's unit and host |
|---|---|---|
| one control read | 0.30–0.67 ms, median 0.39 | 3.5–4.0 ms, median 3.9 |
| gate starts after the scan-clear (dark/white/gain phases) | median 1.5–1.8 ms | median 12.3 ms |
| reads of register 0x100 until bit 0 is clear | 2–6 (never the first) | 1 (always the first) |
| `wait_s` | 5.0–24.4 ms, median 14.0 | 7.0–8.0 ms (two reads, no wait) |
| bit 0 seen clear after the scan-clear, dark/white/gain phases | 14–18 ms in scans later in a session; 15–26 ms in the first scan after a load | by the first read, sampled about 16 ms after the clear |
| the same, shading phases | 24–32 ms | by the first read, about 16–38 ms after the clear (later in the IR run: host time between phases) |
| calibration result | offsets 266/265/266, gains 42/32–33/38–39 | offsets 266/265/266, gains 44/32/39 (600 dpi run) |

On the second unit the last read that still found the engine running
came about 9–13 ms after the scan-clear in the later scans and about
20 ms after it in the first scan after a load.

What follows from that, as a reading of two units and not a proof:

- **The difference that matters is the host's pace, not the
  calibration.** The two units calibrate to nearly the same offsets and
  gains. The second unit's host completes a control transfer about ten
  times faster, so without the gate it reaches the next acquisition's
  writes within a few milliseconds of the scan-clear, while the engine
  is still running; the owner's host gets there after about 12 ms or
  more, when it has usually stopped. This is the mechanism section's
  "a faster host makes it worse", now with numbers.
- **The owner's unit is not shown to be immune; its host hides the
  window.** On the owner's unit the engine was only ever seen stopped,
  at about 16 ms; when it actually stops is not known. The second unit
  stops at roughly 10–15 ms in later scans, which leaves the owner's
  host a margin of a few milliseconds at best if the units are alike.
  The residual dark_b that the owner's unit has shown on later batch
  frames since Test 32 would fit a host on that margin; that link is a
  hypothesis.
- **The units, or their conditions, also differ.** The second unit
  needs 15–26 ms in the first scan after a load. Test 115 measured that
  case on the owner's unit (a gated scan directly after a cold load):
  stopped at the first read, about 16 ms after the clear, where the
  second unit was still running at about 20 ms. Two runs cannot say
  whether that is the units or the conditions.
- **The window is visible on the owner's unit as well.** In Test 115 the
  table's own single read of register 0x100, a few milliseconds after
  the scan-clear, returned `f155` (bit 0 still set) in several
  calibration phases of three scans, two of them ungated
  (`cr_mismatch_details`). The driver went on and those scans were
  fine: the next acquisition's writes came late enough. This is direct
  evidence that the owner's unit is not different in kind.
- **The capture's own pace is close to the owner's host.** The vendor
  capture shows about 4 ms between transfers and both bits clear at the
  single read. A replay at that pace never meets the window, which is
  probably why it went unseen on the developer unit for so long.
- **"Works on the developer unit" is weak evidence for timing.** Any
  place where the tables read a status once after a stop or a start
  (docs/status-read-inventory.md) can be fine on a slow host and wrong
  on a fast one. Timing claims need a run on a fast host or a gate that
  waits for the state.

Open questions:

- Why a control read takes about 3.9 ms on the owner's host. The scanner
  is on a high-speed (480 Mbit/s) root port there, with no hub in
  between, so the link speed is not the reason.
- Why the second unit is still running at about 20 ms in its first scan
  after a load when the owner's has stopped by about 16 ms (Test 115).
- The exit 4 on the second unit.

## Third unit: Steam Deck (Krustenviech, 2026-11-05)

Reported on issue #35 on 2026-11-05 with seven `.diag.json` sidecars in
a gist; full entry in docs/test-log.md. The original reporter of the
issue, on the Steam Deck. One unit, one operator. Master with PR #46 (the
gate), 600 dpi (dual), film strip, frame 1 (one run `--frames 1-2`, so
seven frames from six gated runs).

| gate | runs | result |
|---|---|---|
| enforce | 6 (7 frames) | all completed: `dark_b_substituted` false, `poll_timeouts` 0, aperture coverage verified, `cr_mismatches` 45-49, 15.5-15.7 s per frame-1 scan |
| off | 2 ("in between") | both failed in `cal_dark_b`: `dark_b is residual and no healthy dark_b has been measured this session`; session start state 0x22 (idle, homed); failure after 83 writes and 2 execute pulses |

49 gate waits, all `clear`. Reads of register 0x100 per wait: 1 (3
waits), 2 (6), 3 (3), 4 (16), 5 (12), 6 (3), 7 (6); register 0x101 always
one read. `wait_s` 0.9-29.0 ms, `since_clear_s` 13.7-30.6 ms. One control
read takes about 0.5-0.7 ms on that host (the gate sleeps 4 ms between
busy reads). The slowest waits (28-29 ms, 7 reads) are in the first scan
after each load; later scans 14-20 ms. Calibration: offsets 266/265/266
(identical to the other two units), gains 49/36-37/42, dark_a means about
18 600-21 400, dark_b about 21 000-23 900.

This is the confirmation on the unit that reported the failure: without
the gate it fails at the first calibration, with it every scan completes.

### Three units side by side

| | owner's unit | bearyjd's unit | Steam Deck (Krustenviech) |
|---|---|---|---|
| one control read | 3.5-4.0 ms (median 3.9) | 0.30-0.67 ms (median 0.39) | about 0.5-0.7 ms |
| reads of 0x100 until clear | 1 (always) | 2-6 | 1-7 |
| `wait_s` | 7.0-8.0 ms | 5.0-24.4 ms (median 14.0) | 0.9-29.0 ms |
| longest wait in the first scan after a load | about 8 ms (Test 115, one read each) | about 24 ms | 28-29 ms |
| ungated scans completed | 12 of 12 (Tests 114-115) | 0 of 3 | 0 of 2 |
| gated scans completed | 14 of 14 frames (Tests 114-115) | 7 of 7 | 6 runs / 7 frames, all |
| offsets | 266/265/266 | 266/265/266 | 266/265/266 |
| gains (600 dpi) | 44/32/39 | 42/32-33/38-39 | 49/36-37/42 |

Across the three units: ungated scans failed 5 of 5 on the two fast hosts
and 0 of 12 on the owner's; gated scans completed on all three; the
longest wait seen is 29 ms against the 0.3 s budget. Reading, not proof:
the units calibrate alike (offsets identical, gains within a few codes)
and the failure follows the host's pace, as in the second-unit analysis
above. The Deck's wait times are of the same order as bearyjd's, with a
longer tail (29 ms against 24 ms).

## Decision: default since this change (2026-11-05)

The owner decided that `enforce` is the default, for the library
(`Scanner.cal_wait_scanstop`) and for every subcommand that has the option
(`scan`, `digitize`). Reason: gated scans completed on all three units,
including the two where the ungated scan fails (5 of 5), and the longest
wait is 29 ms against a 0.3 s budget. Reading the log and the two
external reports, this is what has and has not been run **with the gate**:

Run gated (with the gate on):

- Owner's unit, Test 114: plain 3600 dpi, film strip, frames 1-4 with
  `--recalibrate`, twice (28 waits each); 600 dpi dual, frame 1; IR 3600
  dpi, frame 1. Test 115: 600 dpi, frame 1, four gated scans in two runs
  (the cross-unit tool). Test 116, with this change: four more through
  the tool's default sequence, and one scan with no gate option at all,
  which ran gated (7 waits, all cleared at the first read): the default
  itself, on hardware.
- bearyjd's unit (external report, 2026-11-04): 600 dpi, frame 4, seven
  scans; which holder was in the unit is not confirmed.
- Steam Deck (external report, 2026-11-05): 600 dpi, film strip, frame 1,
  six runs / seven frames.
- Earlier, with the fork's version of the gate and not master: one clean
  3600 dpi four-slide run (slide holder).

Not run with the gate, as far as docs/test-log.md and the two external
reports show:

- 1200, 2400 and 7200 dpi on any unit.
- Plain 3600 dpi and IR 3600 dpi on the two external units (the owner's
  unit ran them).
- The whole-strip sweep (`digitize --sweep`) and `digitize` in general.
- 110 film.
- The slide holder with master's gate (bearyjd's holder is unconfirmed).
- The SANE backend, which has no gate.

Callers of those paths are gated from this change on without a hardware
run of that combination. The mechanism is the same executor change in
every table (docs/cal-scanstop-gate.md, "The mechanism"), but "the same
code" is not "run on hardware". All gated hardware runs above used the
driver's own 7-acquisition calibration; the cached-calibration path is
not gated by design (offline test).

A gate timeout is a fail-closed `CalibrationError`: nothing further is
sent, the session is FAILED and a power cycle is required
(docs/hardware-safety.md). `--cal-wait-scanstop off` is the way back. The
error text of a residual dark_b with the gate off now names the gate.

## Hardware plan: Test 114 (owner's unit) — as run

Purpose: does the gate remove the residual dark_b on frames 2–4, and
how long do the bits take to clear?

Precondition: `doctor` reads 0x22, warm session, a **film strip** with
≥ 4 frames (not the slide holder), the usual load flow.

1. Gate off, baseline: `scan --frames 1-4 --dpi 3600 --recalibrate -o a`
   (`--recalibrate` forces a fresh calibration per frame so every frame
   has its own dark_b; the cache would otherwise hide frames 2–4). Expect
   `dark_b_substituted: true` on some of frames 2–4 (Test 32).
2. Gate on: the same command with `--cal-wait-scanstop enforce -o b`.
   Prediction if the mechanism is right: `dark_b_substituted: false` on
   all frames, `cr_mismatches` lower, `cal_scanstop_waits` all `clear`.
3. Interleave: off, on, on, off (one 1–4 run each), to separate the gate
   from run-to-run variation (Test 32 saw f2/f3/f4 one run, f2/f4 the
   next).
4. Record from every `.diag.json`: per frame `dark_b_substituted`,
   `cr_mismatches`, and for the gated runs every `wait_s`,
   `since_clear_s`, `reads_100`, `reads_101`, `last_100`, `last_101`.
   The maximum `wait_s` sizes the margin against 0.3 s.
5. One gated run at 600 dpi (dual tables, 7 acquisitions, the gate also
   precedes the shading bulk-OUT) and one with IR, frame 1 only.

Stop rules: a `CalibrationError` from the gate is a FAILED session, power
cycle, and the run is logged as such; do not retry without reading the
diag first. Any new register value or any poll that times out where the
baseline run did not is logged, not worked around.

Decision after Test 114 (as planned): if the gate removes the
substitutions and the largest `wait_s` is well inside 0.3 s, `enforce`
becomes the default and the "frame 1 is always healthy" assumption in
`_healthy_dark_b` is revisited. Test 114 could not show the removal (no
residual dark_b occurred on the owner's unit); the two external units
did, and the decision was taken on that evidence ("Decision" below).

## Out of scope here

- The SANE backend (`sane/gl126.cpp`) has its own calibration runner
  and its own residual-dark_b check (it fails without substitution). It
  has **no** stop gate. Porting is a separate step. Given the second
  unit's result (0 of 3 without the gate in the Python driver), the
  backend as submitted should be expected to fail calibration on a host
  as fast as that one; this has not been run. The default change here
  does not touch the backend.
- `--cal-data <file.npz>` (reusing a calibration across sessions, the
  reporter's request) is a feature request, not part of this change.
- `--diag-readback-regs` (the reporter's "PR #12") exists only in the
  fork.
