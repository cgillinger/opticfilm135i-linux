# Calibration stop gate (`--cal-wait-scanstop`)

Status 2026-11-02: **ported, offline-tested, NOT hardware-verified on the
owner's unit. Opt-in, default `off`.** Making it the default waits for
the hardware plan below (Test 114) and the Steam Deck reporter's runs.

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
(`cr_mismatches` in the diag, debug log) and continues straight into
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
- No gate-off comparison at 3600 dpi; no film-strip run; nobody has
  measured how long the bits take to clear (that is what `wait_s` is
  for), so the 0.3 s budget is a guess inherited from the poll floor.
- The reporter's other symptoms are **not** explained by the gate: the
  `prep` phase's two 0x0122 polls time out (`d855` vs `e855`, `dc55` vs
  `ec55`; class D where the capture has class E, high byte bit 0x20),
  which is not one of the three benign signatures in
  docs/production-telemetry-2026-10.md, and `cold_init` logs
  `poll_status_word` timing out at `0x4855` (class 4, never seen here).
  Both may be unit- or host-specific; open.
- The reporter's three `.diag.json` attachments need a logged-in GitHub
  session to fetch and have not been analysed.

## Behaviour risk

Today a residual dark_b on frame ≥ 2 is recovered by substitution and
the scan completes. With `enforce`, a bit that takes longer than 0.3 s
to clear becomes a hard failure that needs a power cycle. Until
`wait_s` has been measured on the owner's unit the gate stays opt-in.

## Hardware plan: Test 114 (owner's unit) — not run, needs the owner's go

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

Decision after Test 114: if the gate removes the substitutions and the
largest `wait_s` is well inside 0.3 s, `enforce` becomes the default and
the "frame 1 is always healthy" assumption in `_healthy_dark_b` is
revisited; if not, the gate stays opt-in and the mechanism is wrong or
incomplete.

## Out of scope here

- The SANE backend (`sane/gl126.cpp`) has its own calibration runner
  and its own residual-dark_b check (it fails without substitution). It
  has **no** stop gate. Porting is a separate step after Test 114.
- `--cal-data <file.npz>` (reusing a calibration across sessions, the
  reporter's request) is a feature request, not part of this change.
- `--diag-readback-regs` (the reporter's "PR #12") exists only in the
  fork.
