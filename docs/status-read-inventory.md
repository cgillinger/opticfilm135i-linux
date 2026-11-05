# Status-register reads in the op tables: inventory and policy

Offline analysis, 2026-11-03. No hardware was touched. Raised by issue #35
and the calibration stop gate (docs/cal-scanstop-gate.md): the gate fixes
one instance of a pattern, and the question was whether the pattern
exists elsewhere in the driver.

## 1. The pattern

The driver replays op tables captured from the vendor application. Where
the vendor read an engine-status register once and saw the state it
wanted, the table holds one `cr` op. `_exec_ops` performs that read once,
counts a mismatch (`cr_mismatches` in the `.diag.json`, a debug log
line) and continues. It never waits. Only a `poll` op waits
(`_poll_one`, with a 0.3 s floor, or 1.0 s and a raising timeout where
the caller passes `strict_polls`: JOG, LOAD, POSITION).

So a lone `cr` after a write that starts or stops the engine is an
assumption: "by the time this read goes out, the scanner is where the
vendor's host saw it". Issue #35 is that assumption failing in
calibration on two units that are not the maintainer's. Whether the
cause is host speed or unit variance is unknown (see
docs/cal-scanstop-gate.md, "Evidence so far"); the fix is the same
either way: ask the scanner instead of assuming.

## 2. Method

`tools/status_read_inventory.py` imports every `of135i.tables*` module,
takes every `Phase` (plus `scan_phase()`, identical to each module's
`SCAN`), and for every read of reg 0x100 (wv=0x018e wi=0x0022, bit 0 =
data enable) or reg 0x101 (wi=0x0122, bit 0 = motor enable) records the
op kind (`cr` or `poll`), the nearest preceding 0x0083 register batch
in the same phase and what it did (reg 0x01 bit 0 set = SCAN start,
clear = SCAN stop; pair 0x0f=0x01 = execute pulse), and whether a
`poll` of the same register sits between. The script prints the whole
inventory as markdown; it is not reproduced here. Run it after any
table change:

    python3 tools/status_read_inventory.py > /tmp/inventory.md

Limits of the method: it looks inside one phase at a time, so a read at
the start of a phase that follows the previous phase's stop is judged
by hand in section 4. `tables_base` (cold start) holds register pairs,
not ops, and is outside the script; its one wait is covered in
section 5. Tagging is syntactic: a write of reg 0x01 = 0x22 in POSITION
is the base register table, not a stop command, but it is tagged
"SCAN-clear" like one.

## 3. Numbers

631 reads of 0x100/0x101 in 82 phases (six table sets × 13–14 phases,
plus OPEN/JOG/LOAD). 142 of them are `cr` reads whose nearest preceding
batch stopped or started the engine with no `poll` of that register in
between. By phase:

| class | phases | reads | judgement |
|---|---|---|---|
| A | the seven calibration acquisition phases, all six table sets | 111 | the issue #35 case; gated by `--cal-wait-scanstop enforce` |
| B | SCAN, after the execute pulse that starts the image pass | 27 | vendor watching the engine *start*; our bulk IN blocks until data arrives, so nothing is assumed |
| C | POSITION in the 600/1200/2400/7200 tables, one read before the move's execute pulse | 4 | after the gated end of calibration, and the move's completion is a strict poll |
| | | 142 | |

Everything else is either a `poll` (the motor completions), or a read
with no stop/start before it in the phase (state reads in PREP,
AFE_BASE, CAL_REWRITE, CAL_SHADING_UPLOAD, PARK: 20 reads with no
0x0083 batch before them at all).

Per-phase counts for the 3600 reference tables; the other sets differ
where noted:

| phase | cr 0x100 | cr 0x101 | poll 0x100 | poll 0x101 | SCAN start | SCAN stop | exec pulse |
|---|---|---|---|---|---|---|---|
| PREP | 1 | 3 | 0 | 2 | 0 | 0 | 0 |
| AFE_BASE | 0 | 1 | 0 | 1 | 0 | 2 | 0 |
| CAL_DARK_A / _B, CAL_GAIN_CHECK_A / _B | 1 | 1 | 0 | 1 | 1 | 1 | 1 |
| CAL_WHITE | 1 | 1 | 0 | 2 | 1 | 2 | 1 |
| CAL_SHADING_MEASURE | 0 (1 elsewhere) | 1 | 1 (0 elsewhere) | 2 | 1 | 2 | 1 |
| CAL_SHADING_UPLOAD | 0 (1 elsewhere) | 0 (1 elsewhere) | 0 | 0 | 0 | 0 | 0 |
| CAL_SHADING_VERIFY | 2 (3 elsewhere) | 3 (4 elsewhere) | 0 | 2 | 1 | 2 | 1 |
| CAL_REWRITE (cached path) | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| POSITION | 0 | 1 (2 in 600–7200) | 0 | 1 | 0 | 1 | 1 |
| SCAN | 1 | 5 (4–9 elsewhere) | 0 | 0 | 1 | 0 (1 in 600–7200) | 1 |
| PARK (verbatim table) | 8 (63–67 in 600–7200) | 8 (8–10) | 0 | 0 | 0 | 1 | 0 |
| OPEN | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| JOG | 1 | 3 | 0 | 4 | 0 | 0 | 3 |
| LOAD | 3 | 3 | 0 | 2 | 0 | 1 | 2 |

The plain-3600 CAL_SHADING_MEASURE is the one place the vendor's own
capture shows a repeated 0x100 read, kept as a `poll`; everywhere else
the vendor read once.

## 4. Judgement, place by place

The count of 142 is under one stated rule: a `cr` read of 0x100/0x101
whose nearest preceding 0x0083 write in the same phase carries a stop
(reg 0x01 bit 0 clear) or an execute pulse (0x0f=0x01), with no `poll`
of the same register in between. A wider rule (any earlier stop or pulse
in the phase, not only the nearest preceding batch) would add reads that
this list leaves out: the verbatim PARK's polling reads, which E judges,
and a few state reads in AFE_BASE (G).

"Polled" is also not the same as "waited". Only JOG, LOAD and POSITION
pass `strict_polls`; in every other phase a poll that times out is
counted in `poll_timeouts`, logged as a warning, and the phase continues
(`_poll_one`, non-strict branch). A non-strict poll can also be satisfied
by a state-class match rather than the captured value. So a `poll` in
a table outside those three phases is a bounded look at the scanner, not
a guarantee that the engine reached the state; the judgements below say
where that matters.

**A. Calibration acquisitions (111).** Each of the seven phases ends an
exposure with a SCAN stop and reads 0x100 then 0x101 once. This is the
issue #35 failure. Covered by the stop gate, the default since
2026-11-05 after both reporters' runs confirmed it
(docs/cal-scanstop-gate.md, "Decision"). The transitions between
these phases, and into CAL_SHADING_UPLOAD, are the gate's seven firing
points. The gate arms only on a stop that closes an execute pulse in
the same phase; CAL_SHADING_VERIFY's second stop has no pulse before it,
so nothing is pending when POSITION starts. Our reading of the tables is
that this is intended rather than a gap: no acquisition is open at that
point.

**B. Image pass start (27).** After the execute pulse the vendor read
0x101 every few ms and saw the status byte climb (c5 → e5 → a5 at 3600;
cd → ed → ad at the other resolutions) while the engine came up, then
began bulk reads. Our replayer reads each once, logs the mismatches,
and issues the bulk reads, which block in the kernel until the scanner
has data (60 s timeout). A host that gets here early waits in the bulk
read; a host that gets here late finds the data buffered. In our reading
of the tables none of the 27 single reads is load-bearing, because the
bulk read does the waiting; this is a reading, not a measurement. Leave
as is. (The `_exec_ops` note stands: an idle
wait during the image pass overflows the scanner buffer; it must not be
added here.)

**C. POSITION pre-move read (4).** The 600/1200/2400/7200 tables read
0x101 once (dc55) between the motor-profile bulk upload and the move's
execute pulse; the 3600 and IR tables do not. The engine's last
exposure was gated at the end of CAL_SHADING_VERIFY, the upload itself
is a bulk transfer the scanner acknowledges, and the move's completion
is a strict poll (f455, 1.6 s captured, 1.0 s floor, raising). The
only thing a wait here would add is protection against a motor still
moving from the verify acquisition, which the gate's 0x101 wait
already gives. Leave as is; if a Deck `.diag.json` ever shows
`cr_mismatches` concentrated in `position`, this is the read to look
at.

**D. Motor completions.** OPEN (1), JOG (4), LOAD (2), POSITION (1),
and the semantic PARK's two waits are polls. JOG, LOAD and POSITION run
them strictly (1.0 s floor, `StrictPollTimeoutError`, fail closed).
OPEN's one poll is non-strict; OPEN has no execute pulse, so it waits on
a state, not a move. The semantic PARK (default since 2026-09-17) waits
on 0x35 then on the PARK_COMPLETE class of 0x101 with a 30 s deadline
(docs/park-completion-analysis.md). None of these is a lone read; we have
not looked for other places that assume, beyond the ones listed here.

**E. Verbatim PARK (`--park verbatim`).** The table holds the vendor's
polling of 0x100 as 63–67 single `cr` reads at 600–7200 dpi, spaced by
their captured `dt`; the replayer sleeps those gaps (`_PACE_THRESHOLD`
0.05 s, cap 2 s) and so "waits" by replaying the vendor's clock, not by
reading the scanner. This is the pattern in its purest form. It is not
the default, it exists as the capture-faithful fallback, and the
semantic PARK replaced it for exactly this reason. Keep it as a
diagnostic path only; never make it the default again.

**F. PREP's status polls.** PREP polls 0x101 twice, non-strict, wanting
e855/ec55 (the post-park state) and on our unit after a load reading
d855/dc55 (the post-load state), timing out at 0.3 s and continuing.
Same on the Deck (issue #35) and on the maintainer's unit in every scan
that works. These are state-class reads, not stop waits; the
difference is explained in docs/park-completion-analysis.md and the
scan is correct either way. Leave; the 0.3 s is the only cost.

**G. State reads with no stop before them (20).** PREP, AFE_BASE,
CAL_REWRITE, CAL_SHADING_UPLOAD, PARK: reads of 0x100/0x101 that follow
register-table writes, not engine commands. A mismatch is informational.

## 5. Outside the tables

The cold start (`cold_init`, from `tables_base` pairs) has its own
`poll_status_word` for 0xf000 that gives up at 1.5 s without raising and
continues; it reads 0x4855 on the Deck (issue #35) and on the
maintainer's unit, each time followed by a normal cold start (Test 114).
Same shape as F: a state poll that tolerates a class it has not seen
the vendor see. Not a stop wait. Leave; it is logged.

## 6. Policy from here

1. **No new lone `cr` after a stop or start.** A table or hand-written
   phase that stops or starts the engine or a motor waits for the
   scanner's own "stopped"/"done" bit with a `poll` and a deadline
   before the next OUT transfer. Strict (raising) where a motor is
   involved, since a move started on top of a move is how this hardware
   stalls (docs/hardware-safety.md). Re-run the script before a table
   change is merged; section 4 of its output must not grow.
2. **Class A becomes the default** when either reporter's `.diag.json`
   shows the gate clearing a real wait (`cal_scanstop_waits` with
   `wait_s` well above a single read) and a scan that then completes.
   At that point the SANE backend gets the same wait, and residual
   detection, which it lacks today. *Done 2026-11-05 for the Python
   driver: both reporters' sidecars show it (waits up to 24 and 29 ms)
   and `enforce` is the default. The SANE backend's generated programs
   now wait at the same sites (docs/cal-scanstop-gate.md, "The SANE
   backend"), run on the owner's unit only (Test 118); its
   residual-dark_b check fails without substitution.*
3. **Classes B, C, F, G stay** on the evidence above. They are
   re-judged if a cross-unit `.diag.json` shows `cr_mismatches`
   concentrated in one of them, which is the next item.
4. **Make mismatches locatable.** `cr_mismatches` is one counter per
   scan. Record each mismatch as (phase, register, got, want), capped,
   in the `.diag.json`, so the next report from another unit says
   *which* assumption failed without a debug log. Offline change,
   no wire difference. *Done since: `cr_mismatch_details` in the
   `.diag.json`.*
5. **Verbatim PARK stays non-default** and is not extended.

## 7. What this does not show

Timing. The inventory says where the driver assumes; only
`cal_scanstop_waits` from a unit that shows the failure says how long
the scanner actually takes. Two reporters were asked for that
(issue #35) and both have answered (docs/cal-scanstop-gate.md), so
class A's gate is now the default. The judgements in B and C still rest
on the owner's unit and the vendor captures.
