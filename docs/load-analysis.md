# Magazine load: what the vendor does, what we do, and where they differ

Offline analysis, 2026-09-05 night, after Test 12 (docs/test-log.md).
Sources: `traces/20260902-vendor-eject-from-loaded`, `traces/load-only-
fixed`, `traces/vendor-coldload` (analysis area), `of135i/tables_load.py`
(= eject-from-loaded ops 291-640), tonight's `doctor` dumps.

## 1. The three captures, op by op

**eject-from-loaded (2026-09-02).** App start, base table (writes
0x01=0x22 at op 68), a 226 s pause (magazine insertion), the app-start
jog (feed 6690, feed 6690, eject 3090: ops 137-193), then at 251 s:

| ops | what | completion (status word) |
|---|---|---|
| 296-308 | MOTOR mode 0x18 FEEDL 6690 + two 512 B slope tables, GO | poll 102 reads d955 → **f055** (1.6 s) |
| 309-321 | MOTOR mode 0x1c FEEDL 71490 + slope tables, GO | poll 70 reads d155 → **d855** (1.1 s) |
| 322-640 | loaded idle loop: 36=fc, 33=0e, 32=05 ack, read 0x32 → 05, read status → **d855**, ×18 | steady d855 |
| 641-662 | 33=8e, 32=07, 09=08, MOTOR mode 0x18 FEEDL 3090 (= `eject()`), GO, poll → f855, 09=00 | the user's eject |
| 664- | unloaded idle loop: 32=1f | — |

No 0x01 read anywhere; no 0x01 write after op 68. The eject follows
the load by 6.7 s with no pause in between because the user pressed
the button (capture purpose: "eject from loaded").

**load-only-fixed (2026-08-30).** Same jog (ops 110-166), then at 100 s
the **same two motor moves** (794-825: feed 6690 → f455, traverse 71490
→ **dc55**), then the loaded idle loop (847-1260, status **dc55**,
~7 s), then the preview preparation: a mode-0x78 register set (1261,
the loader motor profile + scan-setup registers 84-bf, 03=30) and
**six line reads** — mode 0x00 FEEDL 1 batches, 0x01=0x03, GO, status
ad55, buffer descriptor, bulk-IN 6 144 B (or 62 208 B), 0x01=0x02
(ops 1320-2370) — then calibration (0x01=0x23 at 2425), the preview
pass (mode 0x30, 0x01=0x23 at 3284, ~40 s of image data), and finally
0x01=0x22 written at op 27225 (status e855).

**vendor-coldload.** Session start + base table on an already-loaded
magazine, status d855/dc55, then **only** the mode-0x78 register set
and the six line reads (2106-2370), ending with 0x01=0x02 written and
status dc55. **No feed, no traverse: this is not a load.** It is the
vendor's preview preparation on a magazine loaded before power-on.

## 2. Conclusions

1. **The mechanical load is exactly feed 6690 + traverse 71490.** Our
   LOAD table (eject-from-loaded ops 291-640) contains both, byte-
   identical, plus the loaded idle loop. It is **not truncated** and
   not taken out of context: in both captures that load, nothing
   mechanical follows before the next user action (eject) or the
   preview pass. Pass 13's "six loader pulses" are the six line reads
   of the preview preparation (Pass 14 was right); they move nothing
   (FEEDL 1, mode 0x00, each followed by a bulk-IN of sensor data) and
   `vendor-coldload` shows them run without any load at all.
2. **0x02 in reg 0x01 is the vendor's loaded end state too.** The
   traverse clears the engine-idle bit (Pass 8) and the load flow never
   rewrites 0x22; the vendor even writes 0x02 explicitly at the end of
   each line read. The next thing the vendor writes to 0x01 is 0x22 in
   a scan session's base table, or nothing before an eject. So reg
   0x01 alone cannot distinguish a complete load from ours — the
   status word and the mechanics can.
3. **d855 vs dc55.** Both are the vendor's loaded-idle value: d855
   throughout eject-from-loaded's loop, dc55 throughout load-only's
   and coldload's. They differ in 0x101 bit 0x04 (GL124 name HISPDFLG)
   and are stable within a capture, i.e. session-dependent, not a
   toggle. The LOAD table's own captured completion value is **d855**,
   and that — not a range — is what `load_magazine()` requires
   (`load_completion_target()`, derived from the table). Whether a
   correctly latching load on this unit settles at d855 or dc55 is a
   hardware question to be answered by reading, not by widening the
   rule; the rule must be re-derived if the table is ever regenerated
   from another capture.
4. **Where our runs diverge, and what the bits say.** Same replay,
   same GO pulses, but the completion polls never match:

   | after | capture | Test 12 (×2) and Test 11b |
   |---|---|---|
   | feed 6690 | **f055** = 1111 0000: bits 0x10 set, **0x08 clear** | **ec55** = 1110 1100: 0x10 clear, **0x08 set**, 0x04 set |
   | traverse 71490 | **d855** = 1101 1000: 0x10 set, 0x08 set | **cc55** = 1100 1100: 0x10 clear, 0x08 set, 0x04 set |
   | cold, magazine inserted | (vendor before load: f855) | 4855: 0x08 set, 0x10 clear |

   Bit 0x08 is the loader sensor (Pass 16, hardware-verified). After
   the vendor's feed it is **clear**: the cassette has been pulled past
   the sensor. After ours it is still **set**: the transport ran its
   6690 steps and the cassette did not go with it. That is the loose
   magazine with the blue LED, and it matches Christian's observation
   that the vendor app only loads a magazine inserted afresh (it pulls
   it in from the sensor-trigger position, refusing one already pushed
   in), whereas `load_magazine.py` has asked for the cassette "to the
   stop" first — past the point where the feed engages it. Bit 0x10
   follows: set for the vendor from the first motor move on, dropped
   for us after the feed; a second position sensor is the natural
   reading, unproven. 0x32 agrees (vendor 0x05, ours 0x15 = bit 0x10).
5. **Why it sometimes latches by hand (11b) and sometimes not (12c):**
   unexplained; with the cassette not engaged by the transport its
   position after the traverse is whatever the hand left it, so the
   variation is plausible but not demonstrated.

## 3. What changed in the driver (offline, tests green)

- Both completion polls of LOAD are **strict**: exact match with the
  captured value, and a timeout raises `StrictPollTimeoutError`
  inside the operation — the flow stops after the feed if the cassette
  was not engaged (never runs the traverse), the session is FAILED, a
  power cycle is required. The general state-class leniency of
  `_poll_one` does not apply to strict polls.
- After the replay the status word must equal the table's completion
  value (d855) or `LoadIncompleteError` is raised.
- `tools/sensor_probe.py`: a read-only probe (zero writes, proven
  offline) that prints loader-sensor / status / reg 0x32 transitions
  and interrupt events while the magazine is inserted by hand.

## 4. Next hardware step (no motor, then one load)

1. Scanner power-cycled, magazine fully OUT of the slot. Run
   `tools/sensor_probe.py`; insert the magazine slowly; note where the
   sensor trips (bit 0x08, interrupt event 0x04) relative to the stop.
   Expect the trip point to be short of the stop.
2. Magazine out again, then inserted only to the trip point. Run
   `tools/load_magazine.py`. With the strict polls, the feed either
   engages the cassette (expect f055, sensor bit clearing) or the
   flow stops right there with a failed session and a power cycle —
   no traverse on an unengaged cassette.
3. If the load completes (d855 read twice): confirm the latch by hand
   and note LED colour; `doctor` for the register signature. That
   reading, together with reg 0x01 = 0x02, is the candidate
   `LOADED_READY` signature — to be coded only after it is observed.
