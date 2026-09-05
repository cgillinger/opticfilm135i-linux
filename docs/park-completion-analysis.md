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

## 2. Status values by phase — PARK evidence kept apart from everything else

Only rows marked "PARK end" are evidence for what Wait B may accept.
Counts are independent observations (one per capture or per park on
hardware). "Next op" is what followed and whether it succeeded.

| value | class | observed in | physical meaning (if known) | next op / result | n | PARK evidence? |
|---|---|---|---|---|---|---|
| d1 | D | during the return (trace 03, right after 0x02=0x30) | carriage returning, busy | → f8 | 2 reads | busy, negative |
| 81 | 8 | during the return (traces 04, 600-7200, load-only) | returning, busy | → e8 | 12 reads | busy, negative |
| a1 | A | during the return (trace 1200 at 5 s; private capture) | returning, busy | → e8 | 4 | busy, negative |
| a9 | A | during the return (private capture) | returning, busy | → e8 | 3 | busy, negative |
| d5, a5 | D, A | end of the SCAN pass, before the park write | engine busy | → park | 6 | busy, negative |
| **f8** | F | **PARK end** (trace 03, 3600 plain, read 5.2 s after the write) | idle, home, magazine present | session ended | 1 | **yes** |
| **e8** | E | **PARK end** (traces 04, 600, 1200, 2400, 7200, load-only; our hardware Tests 18-21: the next frame's first status read after 20+ verbatim parks) | idle, home, magazine present | next POSITION + scan correct (hardware) | 6 captures + 20+ parks | **yes** |
| **ec** | E | **PARK end** (our hardware, same reads, the 0x04 variant) | as e8 | next POSITION + scan correct | 20+ | **yes** |
| d8, dc | D | after LOAD only (captures: traverse end; our hardware: loaded-idle before the first frame) | loaded, idle at the load's reference position | POSITION + scan correct — but never reached via a park | many | **no** — a different phase |
| e8, e0 | E | after EJECT (doctor) | idle, magazine in slot / absent | — | several | not PARK evidence (same value as a park end, different phase) |
| f0, f4 | F | after the LOAD feed / POSITION completion | done class of a feed/position move | — | many | not PARK evidence; accepted by the rule as E/F variants with 0x08/0x04 varied |
| 9c, ad, bd | 9, A, B | during the scan pass | data transfer | — | many | negative |
| c8, cc | C | cc: once, after an UNENGAGED load's traverse (Test 12); c8: never | not a park state | — | 1 / 0 | **no** |
| 48, 40, 00 | 4, 0 | cold power-on | never homed | — | many | negative |

## 3. The decisive question, and the rule

**Is there any evidence that a successful PARK ends in class C or D?
No.** Every observed park end is e8, ec or f8 (class E or F). d8/dc
belong to the post-LOAD state; c8/cc were never seen after a park (cc
once after a failed load). The first version of this analysis accepted
class C/D on the reasoning "idle is idle" — that mixed phases, and a
value being inactive after a load does not show the carriage is home
for the next absolute POSITION. Withdrawn.

Bits across the observed park ends: 0x80, 0x40, 0x20 and 0x08 set in
all; 0x10 varies (f8 vs e8, between captures); 0x04 varies (e8 vs ec,
on our hardware); 0x02 and 0x01 clear in all. No capture contradicts
class E/F at the park end.

**Rule (PARK_COMPLETE):** a 2-byte reply with the 0x55 ack whose status
byte satisfies (byte & 0xe3) == 0xe0 — bits 0x80, 0x40, 0x20 set; 0x02
and 0x01 clear. Ignored: 0x10 and 0x04 (vary within the park-end
evidence itself) and 0x08 (the loader sensor, vendor INI LoaderSensorReg
= 0x101,0x08 — magazine presence, a documented meaning; it is set in
every observed park end because a magazine is always present in a scan
session, and it is ignored on that meaning, not on variation; requiring
it would also match every observation).

Accepts e8, ec, f8 (observed) and their 0x10/0x08/0x04 variants (e0, f0,
f4, fc). Rejects d8, dc, c8, cc, every busy value (a1, a5, a9, 81, d1,
d5), the scanning classes (9c, ad, bd), any value with 0x01 or 0x02 set,
any value without 0x20, cold values, and every short, empty, long or
wrongly-acked reply. Timeout 30 s as before.

Consequence: a park that ends in class D on hardware would be an
unobserved state and stops fail-closed (StrictPollTimeoutError with the
value in the diagnostics). That is a report to read, not a fault to
work around, and it is what the pending hardware A/B is for.

## 4. Status

Offline only. The predicate and the new Wait B are verified against fake
transports with scripted status sequences and a controllable clock;
the first version (mask 0xc3) was withdrawn the same day for accepting
class C/D without park evidence.
Test 23 verified that the fail-closed timeout stops safely; it did not
verify a complete semantic PARK, and neither does this analysis. Verbatim
PARK remains the default; `--park semantic` stays experimental until a
hardware A/B with the new rule has been approved and run.
