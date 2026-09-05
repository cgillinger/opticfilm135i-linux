# Frame position drift after a DPI change: what can be settled offline (2026-09-05)

Observation (Test 7, 2026-09-03): the first 3600 dpi scan after a session
that scanned at 2400 dpi started 1059 rows (7.5 mm) too far in; the same
session's second scan and a new 3600 session after a 3600 park were
correct. Re-loading the magazine resets the position. Within one DPI the
park position is repeatable to a few rows (Test 21: 4 rows over 10
scans).

## What the tables say

| | 3600 plain | 3600 dual | 600 | 1200 | 2400 | 7200 |
|---|---|---|---|---|---|---|
| POSITION mode / FEEDL frame 1 | 0x18 / 6743 | 0x18 / 6746 | 6746 | 6746 | 6746 | 6746 |
| frame pitch | 10760 | 10760 | 10760 | 10760 | 10760 | 10760 |
| SCAN lines (rows × lights) | 5137 | 10622 | 1764 | 3552 | 7088 | 21248 |
| scanned length at the nominal dpi | 36.2 mm | 37.5 mm | 37.3 mm | 37.6 mm | 37.5 mm | 37.5 mm |
| PARK structure | identical in all six: teardown, `0x02=0x30`, two `0x8b` writes, RMW, idle loop | | | | | |
| PARK `0x8b` wIndex 0x0b / 0x0f | 0c000100 / e0ff | 0c000100 / c0ff | 22000100 / f8ff | 22000100 / feff | 22000100 / fcff | 22000100 / f0ff |
| captured status after the park | f8 | e8 | e8 | e8 | e8 | e8 |

So: the position command, the pitch and the scanned length are the same
at every DPI (the scan end lies at the same place on the transport if
one FEEDL unit is one motor step in every profile), the park sequence is
the same, and the park's captured end state is the same idle class. The
only per-DPI difference in PARK is the pair of `0x8b` control writes,
whose meaning is unknown (replay-analysis.md keeps them verbatim).

## Hypotheses, ranked

1. **The return is a sensor seek, and the 2400 session's return had not
   finished when the session closed.** A private capture of the vendor
   driver's own returns shows a two-phase status progression (a1 → a9 →
   e8), the signature of a fast approach followed by a slow seek. The
   verbatim PARK does not wait for the status word (its 0x32 poll times
   out after 1 s and continues, see park-completion-analysis.md); it
   relies on ~14 s of captured pacing. If a 2400 return is slower or
   longer (the `0x8b` writes could be speed parameters), the session
   ends mid-return and the scanner stops where the driver left it —
   which is exactly what an absolute-FEEDL POSITION then gets wrong.
   Testable: the status word after a 2400 park (doctor: e8 idle or still
   busy?), and whether the drift disappears when the next session first
   waits for PARK_COMPLETE.
2. **The `0x8b` writes parameterise the return distance or speed per
   DPI**, so different tables leave the carriage at different rest
   positions by design. Testable the same way: doctor after a 2400 park
   shows idle, yet the next 3600 scan is shifted.
3. **One FEEDL unit is not one motor step in every profile** (a slope
   table difference), so the 2400 session's moves and the 3600 session's
   are in different units. Argues against: all six POSITION phases use
   the same slope table (fedbaefb) and the same 0x7e/0x7f profile.

## The cheapest experiment (needs hardware, not proposed for now)

Same magazine, one load, frame 1 only, all already-verified operations:

1. 3600 scan (reference start row, as Test 21).
2. 2400 scan; **doctor** immediately after its park: status word idle
   (e8/f8) or busy?
3. 3600 scan: measure the start row → reproduces Test 7's shift or not.
4. Power-cycle, load, 2400 scan, then in a new session **wait for
   PARK_COMPLETE on the status word before POSITION** (the same read
   semantic PARK's Wait B uses, no new command), then the 3600 scan.

If 4 fixes it, hypothesis 1 holds and the fix is a bounded wait at
session start (read-only until the status is idle). If 2 shows idle and
3 still shifts, the `0x8b` payloads are the lead and the fix is a real
homing step before POSITION — the vendor's own `0x02=0x30` return, which
is what every session's park already does, run once at session start
with the PARK_COMPLETE wait. Neither is implemented until the experiment
says which.

## Status

Analysis only. No code change. Workaround unchanged: re-load the
magazine after changing DPI between sessions.
