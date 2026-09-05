# PARK completion: what the evidence supports (offline analysis, 2026-09-05)

Scope: replace semantic PARK's Wait B — which waited for register 0x32 to
equal the captured 0x95 (masked 0x18) and stopped Test 23 on 0xb5 — with a
completion rule derived from existing captures, logs and sidecars. No
hardware was touched for this analysis.

## 1. Evidence

Sources: the six table modules' captured PARK phases (`of135i/tables*.py`,
compiled from the public traces in `traces/`), the raw traces themselves
(full poll progressions and timing), and the scan logs / `.diag.json`
sidecars of Tests 18-21 (`hw-2026-09-05-load2/`, `hwblock-20260905-warm/`,
private analysis area). Register 0x101 is read as the "status word"
(wValue 0x018e, wIndex 0x0122; high byte = 0x101, low byte = 0x55 ack).

| source | PARK variant / dpi | 0x101 before the 0x02=0x30 write | observed after, with time | final value | 0x32 (RMW read → later) | 0x35 | next operation | evidence |
|---|---|---|---|---|---|---|---|---|
| trace 03 (QuickScan, 3600 plain) | verbatim 3600 | d5 (busy) | d1 ×2 at 0 s → f8 at 5.2 s | **f8** | 81 → 95 | fb → bb | scan ended session | capture |
| trace 04 (QuickScan, 3600 IR) | verbatim ir3600 | a5 (busy) | 81 ×2 → e8 at 4.9 s | **e8** | a1/81 → 95 | fb → bb | session end | capture |
| vendor 600 dpi | verbatim 600 | a5 | 81 ×2 → e8 at 15.0 s (app loop) | **e8** | 81 (×50 rounds) → 85 → 8d | fb → bb | session end | capture |
| vendor 1200 dpi | verbatim 1200 | a5 | 81 → **a1 at 5.0 s** → e8 at 15.0 s | **e8** | 81 → 85 → 95 | fb → bb | session end | capture |
| vendor 2400 dpi | verbatim 2400 | a5 | 81 → e8 at 15.0 s | **e8** | 81 → 85 → 95 | fb → bb | session end | capture |
| vendor 7200 dpi | verbatim 7200 | a5 | 81 → e8 at 14.8 s | **e8** | 01 → 05 → 15 | fb → bb | session end | capture |
| load-only-fixed (vendor session) | verbatim | — | 81 ×2 → e8 at 2.5 s | **e8** | 81 → 85 → 95 | fb → bb | — | capture |
| Tests 18, 19, 20 batches (our hardware, verbatim, 3600 dual) | verbatim ir3600 ×3 frames each | — | park phase 13.6/14.6/14.8/15.6 s (frames 1-4) | **e8** (read at the next frame's PREP: "last e855 want f855", "ec55 want fc55") | 81 during park ("8155 want 9555" timeout) | — | POSITION + scan of the next frame **correct** (frames matched the vendor reference) | scan logs + sidecars |
| Test 21 (our hardware, 10× + batch) | verbatim ir3600 ×14 | — | park 13.6 s ×10, 14.6-15.6 s batch | e8 (next frame) | 81 | — | all frames correct | sidecars |
| doctor after eject (Tests 17-21) | — | — | — | e8 | 9f | bb | — | doctor JSON |
| Test 23 (our hardware, **semantic**) | semantic ir3600 | — | Wait A (0x35 bit 0x40) completed; Wait B: 0x32 = **b5** for 15 s; 0x101 not read | unknown | b5 | bb | none — fail-closed stop | hwblock summary |
| private capture of the vendor driver under another application (not in the repo) | app's own park, 3600 | — | a1 → a9 → e8 over ~4 s (three returns) | e8 | — | — | next frame correct | private trace |

Values that do NOT appear in the repository and were not used: none of the
sidecars record the status word during PARK itself (only poll-timeout
details, first 20 per scan, and phase seconds); Test 23's `park_waits`
were not written because the block failed before the sidecar. The
post-park 0x101 values on our hardware come from the *next* frame's first
status polls in the scan logs, which is the last read before the next
POSITION — exactly the point that matters.

## 2. What is invariant

Busy (carriage moving or engine running), every observation:
`d1, d5, a1, a5, a9, 81` — **bit 0x01 set** in all; bit 0x40 clear in all
but d1/d5.

Idle after the return, every observation: `f8` (trace 03), `e8` (traces
04, 600-7200, load-only, our hardware ×40+, doctor), `ec` (our hardware,
bit 0x04 variant), and the loaded-idle values `d8/dc` after a load —
**bit 0x01 clear and bit 0x40 set** in all; bit 0x80 set in all.

Bits that vary between sessions for the same physical idle state:
0x20 and 0x10 (f8 vs e8 vs d8 for "idle, home, magazine in"), 0x04
(f0/f4, d8/dc, e8/ec — documented since Test 14), 0x08 (loader sensor
= magazine presence, not a completion signal).

Bit 0x02: never observed set in any status read in this project.

Register 0x32: the vendor's idle loop keeps reading it after the return
and its value changes from 81 (or 01) to a value with **bit 0x04 set**
(85/95/8d/15; Test 23's b5 also has it) — that transition, not 0x95, is
what the vendor app watches. The rest of its bits (0x10, 0x20, 0x80,
0x18) vary by session and dpi. It is an application-loop event flag
with several unexplained bits; it is recorded here as supporting
evidence and NOT used as the completion condition.

## 3. The rule

**PARK is complete when the status word (0x101) reads idle: bit 0x80
and bit 0x40 set, bit 0x01 (busy) and bit 0x02 clear; bits 0x20, 0x10,
0x08, 0x04 ignored; reply exactly two bytes with the 0x55 ack.**

Required mask 0xc3, required value 0xc0. Accepts f8, f0, e8, ec, e0, d8,
dc, c8 …; rejects a1, a9, a5, 81, d1, d5, 9c, ad, bd and every short,
empty, long or malformed reply.

Why not more: requiring a specific class (E or F) would have rejected our
own hardware's d8/dc after a load and would rest on bits shown to vary;
requiring 0x08 set would tie completion to magazine presence. Why not
less: bit 0x01 alone would accept 9c (scan-in-progress class); bit 0x40
alone would accept d1/d5 (busy). The two together are what every busy
observation lacks and every idle observation has.

Timeout: the longest observed return is the frame-4 park (15.6 s in the
verbatim phase, which includes captured pacing) and the vendor's 600-7200
captures read idle at 15 s because that is when the app looked. Wait B's
budget is 30 s — explicit, bounded, generous against the observations,
and still a hard stop.

## 4. Status

Offline only. The predicate and the new Wait B are verified against fake
transports with scripted status sequences and a controllable clock.
Test 23 verified that the fail-closed timeout stops safely; it did not
verify a complete semantic PARK, and neither does this analysis. Verbatim
PARK remains the default; `--park semantic` stays experimental until a
hardware A/B with the new rule has been approved and run.
