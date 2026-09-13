# WP-4 — magazine handling in the backend: minimal hardware plan

**STATUS: written, NOT run. Needs Christian's explicit go before any
step below touches the scanner.** The design is
`docs/sane-wp4-magazine.md`; the offline half is done and verified
(`release_check` green, the five magazine programs wire-equal to the
Python driver, the option and state-machine tests green against the
built backend).

This is the load flow — the project's most delicate motor sequence, the
one that stalled the mechanism — driven from C++ for the first time. It
does not piggy-back on another session's run.

Nothing here is a new motor sequence. Every transfer is the Python
driver's own, proven byte-for-byte identical offline
(`tests/test_sane_ops.py`: `cold_init` 218 transfers, `open`/`jog`/`load`
199, `eject` 12). What has never happened is a C++ process issuing them.

---

## 0. Environment lock before the first motor write (mandatory, logged)

Same as WP-2, and for the same reason: an ambiguous environment turns a
deviation into a mystery.

1. The Windows VM is off or the scanner is detached from it
   (`autoConnect` can steal the device at re-enumeration).
2. No `of135i` process is running and no stale process lock is held:
   `cat /tmp/of135i-07b3-1436.lock` (absent or a dead pid).
3. No stale magazine mark: `rm -f /tmp/of135i-07b3-1436.lock.magazine`.
   A mark from an earlier session names a device address that no longer
   exists after a power cycle and would be ignored anyway, but starting
   from a clean state makes the log unambiguous.
4. The installed backend is the one just built:

   ```
   sudo tools/sane_install.sh install      # Howdy: look at the camera
   tools/sane_install.sh verify            # exit 0 = right library + device enumerated
   ```

   `verify` must name `/usr/lib64/sane/libsane-genesys.so.1` without
   `LD_LIBRARY_PATH` set, and must print a compiled-in config path of
   `/etc/sane.d` (WP-2's real installation defect).
5. `SANE_DEBUG_GENESYS=8` for the runs below — enough to see every
   magazine transition and poll, low enough not to hex-dump an image
   (level 255 made a scan take 65 s instead of 18 s; never time with it).
6. `OF135I_SANE_POLL_CAP_MS` is ignored on real hardware — the backend
   reads it only when the scanner interface is a mock **and** the
   library is in test mode (`sane/gl126.cpp`, `magazine_policy()`), so a
   stray value in a shell profile cannot shorten a motor wait on the
   unit. Nothing to do here; the gate is in the code, not in this plan
   (Astra review 2026-09-13).

Record in `docs/test-log.md` before starting: date, time, what is in the
magazine, whether the scanner was power-cycled, and the backend's sha.

**Christian listens throughout.** Any scraping sound: cut power
immediately. The jog and the load feed are the two moves that have ever
sounded wrong.

---

## 1. Preconditions

- The scanner is **power-cycled** and reads the idle state:
  `.venv/bin/python -m of135i status` → reg 0x01 = 0x22 (or 0x00 if it
  has never homed since power-on; both are accepted starting points).
  Then **close that process** — it holds the driver lock while it runs.
- The magazine is **loose in the slot** for run A, and a **four- or
  six-frame strip** is in it. Use a strip whose frame 1 is already known
  (the reference strip), so a wrong frame is obvious.
- Nothing else has the device: digiKam closed for runs A–C.

---

## 2. Run A — a whole cycle from `scanimage`, no `of135i` command

This is WP-4's acceptance in one run: **no `of135i` anywhere in it.**

**A1 — release.**

```
scanimage -d 'genesys:libusb:<bus>:<dev>' -n --load-film
```

(Fresh device string from `scanimage -L`; it changes at every power
cycle.)

Expected on the wire, in the debug log:
- if the unit was cold (reg 0x01 = 0x00): the cold-start sequence, nine
  motor moves, then reg 0x01 reads 0x22;
- the device-open register table, then the jog: **feed 6690, feed 6690,
  eject 3090**, each completing with status class F and the
  loader-sensor bit set (0xf8);
- `magazine unknown -> released`;
- the mark file exists and names this device:
  `cat /tmp/of135i-07b3-1436.lock.magazine`.

Expected mechanically: **the magazine pops loose.** Sound: the jog is a
verified transport (Tests 17–23, 45, 51); report anything unusual.

Stop if: any poll fails closed, the exit status is non-zero, or the
magazine does not come loose.

**A2 — the operator's step.**

Take the magazine **fully out** of the slot and push it back in **to the
mechanical stop**. This is the step SANE cannot ask for, which is the
whole reason the flow is two calls.

**A3 — scan frame 1.**

```
scanimage -d 'genesys:libusb:<bus>:<dev>' \
    --mode Color --resolution 3600 --frame 1 \
    --format=tiff -o wp4-f1.tiff
```

Expected, in order:
- `load_document` sees the pending mark, re-reads the hardware
  (reg 0x01 = 0x22, loader sensor set, regs 0x3b/0x3c = 0x00/0x00);
- the **load**: the engaging feed completing 0xf4 (done class, sensor
  bit CLEAR — the cassette was pulled past the sensor) and the traverse
  completing 0xdc;
- `magazine released -> loaded`, mark file gone;
- then the ordinary, already-verified pass: offset/gain/shading
  calibration, POSITION (FEEDL 6562 for frame 1), 233 chunks,
  3762 × 5335, PARK.

If the load does not reach the state it must reach, the backend names
the step that failed — the engaging feed's completion, the traverse's,
or another op — and stops. It does not name a cause, and it does not
invite another attempt: a timeout at the feed has the same shape as the
benign `fc55` outcome of Tests 48/49, but the shape is something to READ
in the log afterwards, not to assume. The session is failed and **this
attempt is over**: §6.

**A4 — eject.**

```
scanimage -d 'genesys:libusb:<bus>:<dev>' -n --eject-film
```

Expected: the eject move (FEEDL 3090), `magazine loaded -> ejected`, the
magazine loose. Then take it out.

**A5 — one observation to record, not a pass/fail.** After A4, run
`.venv/bin/python -m of135i doctor` and note whether reading the
interrupt endpoint (EP 0x83) reports the overflow state. The Python
loader drains that endpoint during its own load; the backend cannot (the
genesys USB abstraction has no interrupt transfer — `docs/sane-wp4-
magazine.md` §7), so the prediction is that a backend-driven load leaves
it overflowed until the next power cycle. Nothing in the SANE flow reads
it, so this changes nothing about the scan; it is a documented
consequence, and the run is where we find out whether the prediction
holds.

**A is the B2 prerequisite met** if A1–A4 ran with no `of135i` command
and the image is a real frame 1.

---

## 3. Run B — the same cycle from digiKam

**Only after run A completed and was reviewed.** Power-cycle first. In digiKam's scanner dialog ("Specifika alternativ för
bildläsare" / Scanner Specific Options):

1. **Load film** → magazine pops loose. Take it out, reseat to the stop.
2. **Läs in** with Frame 1, Colour, 3600 dpi → the load runs, then the
   scan. **Never press Förhandsgranskning** — it is a full 600 dpi run.
3. Read the **Magazine** line between steps: it should say `released …`
   after step 1 and `loaded …` after step 2.
4. Scan **frame 2** on the same load, to confirm the second scan does
   NOT re-load (no mark is pending; `load_document` returns without
   reading a register).
5. **Eject film**, then close the dialog.

digiKam holds the device open for as long as the dialog is open, so the
release is remembered in-process here; the mark file is the mechanism
that makes the `scanimage` case work, not this one.

---

## 4. Run C — the standing requirement: power-cycled with the magazine latched

**Only after runs A and B completed and were reviewed.**
Christian's standing requirement is that this be a supported operation.
Today's recipe from the CLI is `of135i load --double-jog` (Test 51, n =
2). In the backend it is **pressing Load film twice**:

1. With the magazine **latched**, power-cycle the scanner.
2. **Load film** → the cold start runs (expect its usual latched-magazine
   deviations: a status-word timeout, reg 0x32 = 0x1d — these are
   best-effort polls and the sequence continues, by design), then the
   jog, which releases the cassette.
3. Take it out, reseat to the stop.
4. **Load film again** → this is the second jog, now from the loose
   position — the state the vendor's jog always runs from.
5. Take it out, reseat to the stop.
6. Scan frame 1 → the load must reach its two completions (0xf4 at the
   engaging feed, 0xdc at the traverse).

If it does not, that is a deviation like any other: §6 applies. The
`fc55` signature of Tests 48/49 is the outcome we EXPECT to see in that
case, and if the log shows it the diagnosis is easy — but it is read
from the log afterwards, not assumed beforehand, and it does not license
another attempt on its own.

---

## 5. Acceptance

WP-4 is done when **all** of these hold:

| # | Criterion |
|---|---|
| 1 | A1–A4 completed with no `of135i` command anywhere in the cycle |
| 2 | `wp4-f1.tiff` is a real frame 1: full frame, both edges, no banding |
| 3 | The jog, load and eject sounded normal (Christian) |
| 4 | Run B completed from digiKam, including a second frame on the same load with no re-load |
| 5 | Run C released and loaded a latched magazine in one power cycle |
| 6 | Every magazine transition in the log matches the state machine, and no poll failed closed |

Criterion 2 is a **working image** check, not the production eye-check:
the image path is unchanged by WP-4 (same profile, same geometry,
already accepted in Test 74). If anything about the image differs from
Test 74's, that is a finding, not an acceptance question.

---

## 6. Stop rule

**This is the first time C++ drives these motors. A deviation ends the
approved attempt.** There is no recovery step in this plan, because
recovery is itself a motor operation and this plan does not pre-approve
any.

1. **Stop.** Any fail-closed poll, any unexpected status, any exit code
   that is not the expected one, anything mechanical that sounds wrong.
   The backend itself writes nothing further and attempts no recovery —
   that part is automatic and is the design.
2. **Preserve what happened, before touching anything.** The debug
   output of the run, the magazine mark file if it still exists
   (`/tmp/of135i-07b3-1436.lock.magazine`), the exit status, and what
   the operator saw and heard. `of135i status` and `doctor` are
   read-only and may be run; nothing else may.
3. **Then stop for real.** No further motor operation of any kind until
   the log has been reviewed and Christian has explicitly approved what
   happens next. That includes, and is not limited to: rerunning the
   step, moving on to the next run, `of135i load` or `of135i eject`,
   `load --double-jog`, and the QuickScan-in-the-VM route. Each of those
   drives the transport, and after a deviation nobody yet knows what
   state the transport is in.

**Cutting the power is always allowed.** If the mechanism sounds wrong,
Christian cuts power immediately — that rule predates this plan and is
never in question. It is a way to stop, not a permission to start
again: what follows a power cut is step 2, then step 3.

When recovery is later approved, the Python driver is the tool for it
(power-cycle → `of135i load`, or `load --double-jog` from a latched
magazine, → `of135i eject`; QuickScan in the Windows VM if the magazine
is mechanically stuck). WP-4 does not replace it. But that is a decision
taken after reading the log, not a step in this plan.

The runs in §2–§4 are approved one at a time, never as a loop.

---

## 7. What this run does NOT cover

- Frames 3–6 from the frontend on a backend-driven load (the positioning
  is unchanged and already verified; only the load is new).
- The dual-light profiles (IR and 600/1200/2400/7200 dpi) with a
  backend-driven load — same reason.
- Any change to the image path, calibration or geometry. WP-4 touches
  none of them.
- WP-3. The submission package stays **prepared only**: nothing is sent,
  no merge request, no contact. Christian handles any upstream himself.
