# Cross-unit protocol

Status: run three times on the developer unit (docs/test-log.md). Test
115, twice with the earlier sequence `on,off,off,on`: the first run found
that the tool missed the sidecars and packed the images, the second was
made with the fixed tool and produced a complete report and an archive
without images. Test 116, once with the present default `on,on,on,on`
(gated scans only, section 3) and the after-the-run text. The tool has
not been run on a second unit. The stop-on-failure, exit-4 and interrupt
paths and the NOTE for a sequence with `off` have only been run offline
(`tests/test_cross_unit_check.py`).

## 1. Why

The driver has one developer unit. Issue #35 reports an intermittent
calibration failure on other units that the developer unit shows only
in part, and two users with their own scanners have offered to run
tests. So that their results can be compared with each other and with
the developer unit, everyone runs the same command sequence and sends
the same files. `tools/cross_unit_check.py` runs that sequence and
packs the evidence.

## 2. What you need

- The driver from master (`git pull`, then run it from the checkout).
- The udev rule installed (`udev/`, see the README), so that the
  scanner can be opened without root.
- The scanner on a fresh power cycle: switched off, then on, and no
  other command sent to it since.
- The film-strip holder, with a film strip in it or empty; both are
  fine. The image check at the end of each scan looks for the edges of
  the holder's aperture and was built on the film-strip holder. If it
  does not find them, the scan exits 4; the tool records that and goes
  on (section 5). That is what the slide holder gives (seven scans on
  one unit, outside this tool). If the slide holder is the only one you
  have, run with it and say so in the comment; the tool itself has not
  yet been run with a slide holder.
- The magazine in the slot, as for any `of135i load` (README, "Usage").
  You push it to the stop later, when `load` asks for it. One case to
  know: if the scanner was switched off while the magazine was still
  loaded from an earlier session (which is the situation after a failed
  scan and its power cycle), the README says the first plain `load` may
  stop at the feed, and describes the way out (`of135i load --release`,
  power-cycle again, then `load`). That was seen on the developer unit
  (docs/test-log.md Test 49); the tool does not run it for you. If the
  `load` step fails that way, do it by hand and start a new run.
- Nothing else holding the device (no other scanning application, no
  virtual machine with USB passthrough).
- A real terminal window. The `load` step asks you to do something with
  the magazine and reads your Enter key (section 4), so the tool cannot
  run with its input piped or redirected; without `--yes` it refuses to
  start in that case, before anything is sent.
- A few minutes. On the developer unit the whole sequence took about
  two minutes, the operator's part included (Test 115).

## 3. The run

Read what it will do first. This prints every step and what it sends to
the scanner, and exits; it writes nothing (not even the `--out`
directory) and does not touch the scanner:

```bash
python3 tools/cross_unit_check.py --out results/xu-01 --dry-run
```

Then run it:

```bash
python3 tools/cross_unit_check.py --out results/xu-01
```

`--out` must not exist or must be empty. The real run writes the same
plan to `results/xu-01/plan.txt`.

The tool prints the plan and asks one question: is the scanner freshly
power-cycled, the magazine in the slot and nothing else
holding the device. Answer `y` to go on. The `load` step then asks you
a second thing, mid-run (section 4). The steps, in this order:

1. `of135i doctor --json doctor-before.json` (reads only).
2. `of135i load` (MOTOR; you take the magazine out and push it back in).
3. Four scans, `of135i -v scan --frames 1 --dpi 600 --recalibrate
   --cal-wait-scanstop X -o run-NN-X.tiff`, with X = `enforce` in all
   four by default (`--sequence on,on,on,on`). Each scan calibrates
   afresh. MOTOR.
4. `of135i doctor --json doctor-after.json` (reads only).

It runs no other command: no eject, home, initialize or park, and no
recovery of any kind.

**Stop rule.** The tool stops at the first failed `load` or scan. It
then sends the scanner nothing but the final read-only `doctor`, writes
the evidence and exits non-zero. After a failure the scanner must be
power-cycled before anything else is sent to it
(docs/hardware-safety.md). Once you have power-cycled it, a new run may
be started, with a new `--out` directory. If the exit code is 3, another
process holds the scanner; nothing was sent, and the tool stops without
the final `doctor`. Close the other process and start again.

**Why `on,on,on,on`.** The ungated failure is already established on the
affected units (issue #35: 5 of 5 ungated scans failed on the two fast
hosts, docs/cal-scanstop-gate.md); repeating it only costs a power
cycle, with the magazine still loaded. The gate is also the driver's
default now, so the default sequence is four ordinary scans. The
developer unit's own runs used `on,off,off,on`, a symmetric order with a
gated run first.

**NOTE on a sequence with `off`** (`--sequence`, for example
`on,off,off,on`). Run one only if you were asked to. On a unit affected
by issue #35 the ungated scan is expected to fail in calibration
(`dark_b is residual`). That harms nothing that has been seen, but the
tool stops at the failed scan, register 0x01 reads 0x02 (not an accepted
start state, so the driver sends nothing more), and the scanner needs a
power cycle with the magazine still loaded; the README's `load --release`
note may then apply (sections 9B and 10). The tool prints this NOTE and
writes it to `plan.txt` when the sequence contains `off`, and not
otherwise.

**Ctrl-C or an unexpected error of the tool.** The evidence so far is
still written: `summary.json` (result `interrupted` or `error`),
`report.md` and the tarball. After an interrupt the tool sends nothing
more, not even the final `doctor`, and prints that the scanner must be
power-cycled before any further command. The exit code is 130 (1 for
an error).

**Abort rule.** If the holder grinds, scrapes or stops, switch the
scanner off at its power switch (docs/hardware-safety.md, "Recovery after
an interrupted scan"). Do not send any recovery command (no eject, home
or park), and do not treat restarting the program as a recovery.

`--frame N` (default 1) is the frame number handed to `--frames`, not a
count. Because that is the multi-frame form of the option, the driver
names its outputs `run-NN-<gate>-f<N>.tiff` (plus `.overscan.tiff`,
`.cal.npz` and `.diag.json`), and the tool finds the sidecars by that
pattern on disk.

The archive is built from an allowlist: only `summary.json`, `report.md`,
`log.txt`, `plan.txt`, `doctor-*.json` and `*.diag.json` are kept. Every
other file in the result directory (images, `.cal.npz`) is deleted
before packing, and the finished tarball is re-read and refused if it
holds anything else. Add `--include-images` to keep and pack everything;
do that only if asked.

## 4. What each step sends to the scanner

MOTOR in this document means that the scanner **moves the film holder**
(the part that carries the film through the scanner; the driver's source
and the review of this protocol call it the carriage). The same table is
printed by the tool and written to `plan.txt`.

| # | step | reads only? | register writes | motor | you | what it is for |
|---|---|---|---|---|---|---|
| 1 | `doctor` (before) | yes: USB reads only (descriptors, chip id, status word, register dump, loader sensor, one read of the button endpoint) | no; the driver refuses any write in this read-only session | no | nothing | records the starting state |
| 2 | `load` | no | yes | **MOTOR**: cold-start homing, the jog, the load feed and the traverse | take the magazine out, push it back in to the stop, press Enter | puts the magazine and holder at the position scans start from |
| 3-6 | `scan` (gate `enforce` or `off`) | no | yes | **MOTOR**: position, image pass and park move the film holder; the calibration phases *may* move the motor (not established as motor-free) | nothing | one calibrated scan; the gate setting is the variable under test |
| 7 | `doctor` (after) | yes, as step 1 | no | no | nothing | records the state after the run; not run after Ctrl-C |

### `load`, stage by stage

`of135i load` is not a silent step. In the order `of135i/loadflow.py`
(`run`) executes it:

1. Reads the loader sensor (read only); stops if no magazine is present.
2. Opens the device for writing (see the scan phases below), then runs the
   device-open sequence. On a freshly power-cycled scanner (register
   0x01 = 0x00, the precondition above) this is the **cold start** first:
   chip handshake, register tables, front-end bring-up, then three
   rounds of loader homing moves of three moves each, nine in all.
   **MOTOR.** If the magazine is present but displaced, the cold start
   may also run the load traverse before the jog (implemented from a
   vendor recovery capture, not yet verified on hardware:
   docs/hardware-safety.md). Then the device-open register table
   (writes, no execute pulse).
3. The app-start **jog**: feed, feed, eject, each completion checked.
   **MOTOR.**
4. It reads register 0x32, **prints a prompt and waits for your Enter**.
   No motor runs while it waits; one more register read follows. The
   prompt reads:

   > JOG done. Now take the magazine FULLY OUT of the slot, then insert
   > it fresh all the way to the mechanical stop.
   > Press Enter when it is at the stop (Ctrl-C aborts; a power cycle is
   > then required):

   The tool shows the child's output as it arrives (a test runs a real
   child that prompts with `input()` and checks the prompt reaches the
   terminal before the answer is given), and the child reads your answer
   from the terminal. That is why a real terminal is needed.
5. After your Enter: the load. The engaging feed and the long prescan
   traverse, then a final status read. **MOTOR.**

### `scan`, phase by phase

The order is the driver's own (`Scanner.scan`, `_scan_dual` at 600 dpi,
`_scan_plain` at 3600 dpi; the phase names are the same):

| phase | motor | what it sends |
|---|---|---|
| open session | no | reads register 0x01 (start-state check; anything but 0x22 is refused and nothing is written), then selects the USB configuration (detaching a bound kernel driver first), then reads the loader sensor |
| `base_init` | no | register writes: the power-on base table and the front-end base values. They set the motor-mode and feed-length registers but never write the execute register 0x0f, which starts every move in the driver |
| `prep`, `afe_base` | no | register writes, status reads and polls; the lamp is switched on. Neither table writes 0x0f |
| `cal_dark_a`, `cal_dark_b`, `cal_white`, `cal_gain_check_a`, `cal_gain_check_b`, `cal_shading_measure`, `cal_shading_upload`, `cal_shading_verify` | **may move the motor** | register writes, one execute pulse (0x0f) per acquisition, then the measured lines are read back; `cal_shading_upload` only writes the shading table. No motor mode is written in these phases. The white-line measurement is described in the code as "a single-line read, no motor move"; for the other acquisitions the repo has no such statement and no measurement, so they are not claimed motor-free. `cal_white` is repeated at a fixed interval while the lamp is still warming up |
| `position` | **MOTOR** | moves the film holder to the frame (feed mode 0x18), then a strict completion check |
| `scan` | **MOTOR** | the image pass: the holder moves while the image lines are read (mode 0x30) |
| `park` | **MOTOR** | the holder returns (mode register set to 0x30), the lamp is switched off, then the driver waits for the end state |

After the scan, writing the image and the `.diag.json` happens on the
host only.

**What the gate changes on the wire.** With `--cal-wait-scanstop off`
nothing: the transfers are the same as in a driver without the gate. With
`enforce`, after each calibration acquisition the driver additionally
*reads* status registers 0x100 and 0x101 until bit 0 is clear (at most
0.3 s; at the deadline it raises `CalibrationError`, nothing further is
sent and the session is failed). It adds no write and no motor move
(docs/cal-scanstop-gate.md, "What `enforce` does"; docs/hardware-safety.md,
"Calibration stop gate").

### Where each claim comes from

- `doctor` reads only: `of135i/cli.py` `_cmd_doctor` opens
  `UsbIo.open(readonly=True)`; the read-only session state is
  `SessionState.READONLY`, and `HardwareSession.write_allowed()` raises
  `ReadOnlySessionError` before any OUT transfer (`of135i/safety.py`,
  "READONLY = ... never any writes"; `of135i/usbio.py`, `open`:
  "readonly=True ... never issues those at all"). `collect_doctor`
  (`of135i/diag.py`) makes control-IN reads and one interrupt-endpoint
  read.
- `load` stages: `of135i/loadflow.py` `run` and `REINSERT_PROMPT`;
  `Scanner.cold_init` and `_cold_init_body` (three homing rounds,
  `_cold_motor_move`, "NINE motor completions are STRICT"),
  `Scanner.jog_magazine`, `Scanner.load_magazine`
  (`of135i/device.py`). In `tables_load.JOG` the execute register 0x0f is
  written three times (feeds of 6690, 6690 and 3090 steps), in
  `tables_load.LOAD` twice (mode 0x18 feed, mode 0x1c traverse), in
  `tables_load.OPEN` never.
- Scan phases: `Scanner.initialize`, `_calibrate_dual` / `_plain_calibration`,
  `_scan_dual` / `_scan_plain`, `_park` (`of135i/device.py`). The motor
  register writes in the op tables (`of135i/tables*.py`, `tables_base.py`)
  were decoded: `prep` has none, `afe_base` and the base pairs set
  register 0x02 and 0x3d-0x3f without any 0x0f, each calibration
  acquisition has only the 0x0f pulse, `position` writes mode 0x18 with
  its feed length, `scan` mode 0x30, `park` mode 0x30. That 0x0f is what
  starts a move: `docs/protocol-notes.md` ("0x0f = execute", "0f=01 = GO"),
  and every motor routine in `device.py` (`_motor_run`, `_cold_motor_move`)
  ends its register batch with it. The feed-mode and scan-mode meaning of
  0x18 and 0x30: `docs/protocol-notes.md`, Pass 3 and Pass 14. The park
  return: `docs/park-completion-analysis.md` ("carriage returning, busy")
  and the comment on Wait A in `park_semantic`.
- The single-line white measurement: `Scanner._gain_with_warmup`
  docstring ("a single-line read, no motor move"). No document in the repo
  says that the other calibration acquisitions leave the holder still,
  hence "may move the motor".
- The gate: `Scanner._cal_wait_scan_stop` (control-IN reads only),
  docs/cal-scanstop-gate.md.
- Abort rule: docs/hardware-safety.md, "Recovery after an interrupted
  scan" and "What fail closed means" (no recovery sequence of any kind).

## 5. Results that need a word from you

- **A scan that exits 4.** `of135i scan` returns 4 when the scan, the
  calibration, the park and the sidecar all finished but the image check
  could not verify aperture coverage (`of135i/cli.py`; the frame loop
  and the sidecar come before the `return 4`). The tool records such a
  run as `completed-coverage-unverified`, goes on with the next scan,
  exits 0 if nothing else failed, and says in `report.md` and at the end
  which runs exited 4 and that this is the image check, not the transport
  or the calibration. An exit 4 *without* a sidecar is a failure like any
  other non-zero exit.
- **`dark_b is residual`.** If a scan fails and its output contains the
  driver's text `dark_b is residual`, the run gets
  `"failure_class": "dark_b_residual"` in `summary.json`. That is the
  failure issue #35 is about, and it is a result worth sending. The
  scanner must be power-cycled before anything else. (The class comes
  from that output text only.)
- Every run has an `outcome` in `summary.json`: `completed`,
  `completed-coverage-unverified`, `failed` or `interrupted`.

## 6. What to send

Attach `results/xu-01.tar.gz` (the tool prints its path and size) to
issue #35, and say in the comment which holder was in the scanner (the
film-strip holder or the slide holder), whether there was film in it,
and whether the run completed or where it stopped.

The archive holds:

- `summary.json`: host facts (platform, Python, pyusb, driver
  revision), the result of the run and, per run, the gate setting,
  outcome, exit code, seconds and the values taken from the sidecar
  (`calibration`, `dark_b_substituted`, `poll_timeouts`, `cr_mismatches`,
  `cr_mismatch_details`, `cal_scanstop_waits`, `phase_seconds`);
  `stopped_at` is the index of the failed run, if any.
- `report.md`: the same as a table, with a plain statement of any exit 4,
  `dark_b is residual` or interrupted run.
- `log.txt`: the combined output of every command, with timestamps.
- `run-NN-<gate>-f<N>.diag.json`: the sidecar of each scan.
- `doctor-before.json`, `doctor-after.json`, `plan.txt`.

There are no images and no `.cal.npz` unless `--include-images` was given. Paths are
relative to the result directory, and your home directory is replaced by
`~`. The doctor reports contain the USB ids, the kernel version and the
Python version; look through them if that matters to you.

## 7. What we do with it

We compare `cal_scanstop_waits`, `dark_b_substituted` and
`cr_mismatch_details` across the units, and between `off` and `enforce`
runs on each unit. The result becomes one row per unit in a
compatibility table (unit, host, gate setting, outcome). The raw data
stays in the issue; nothing is published from it without saying so.

## 8. Variants worth running

Only if the default run passed:

- The same at 3600 dpi: `--dpi 3600`.
- A longer gated sequence: `--sequence on,on,on,on,on,on`. A sequence
  with `off` only when asked (section 3, NOTE).

Use a new `--out` directory each time, and power-cycle the scanner
before each run.

## 9. Risks

The list is built only from what this repository documents. Where the
repo does not say, this section says so.

### 9A. What could damage the scanner

Only events with a documented mechanical side: a motor driving against a
stop, stalling or grinding, a displaced or jammed magazine, force by
hand. The scanner in question is a single unit that cannot be replaced
(docs/hardware-safety.md, first lines).

**1. Motor commands sent in an undefined state.**

- What happened (developer unit). Test 11d (2026-09-04,
  docs/hardware-safety.md, first paragraph): an aborted session left the
  scan engine running (register 0x01 = 0x23); a new session was started
  on top of it, its writes and execute pulse hit the running engine and
  the firmware hung; the scanner stayed enumerated but answered nothing
  until the power was cut. Tests 44 and 46 (docs/test-log.md, 2026-09-07;
  docs/protocol-notes.md): the driver's eject stalled twice, with a harsh
  or "slightly louder" motor sound and the magazine mechanically stuck,
  when it was issued from the base-table-only state (registers 0x3b/0x3c
  = 0xff/0xff) the SANE open had created. A power cycle alone did not free
  the magazine in the earlier stalls (docs/protocol-notes.md); a
  vendor-software start did.
- Recognise it: a grinding, scraping or harsh sound; the holder stopping;
  a scanner that stays on the bus but does not answer.
- Do: the abort rule. Switch the scanner off at its power switch at once
  (docs/hardware-safety.md, "Recovery after an interrupted scan"). Send no
  recovery command (no eject, home or park); restarting the program is not
  a recovery.
- How long a stalled move can last: the repo does not say. It records the
  bound after which the driver stops waiting (30 s for each cold-start
  move, `COLD_MOVE_TIMEOUT` in `of135i/device.py`; the park waits are
  longer) and, for Test 112's stalled park, that the operator cut the
  power at once; it does not record how long a stall would have run.
- Why this tool does not do it: before a session's first write the driver
  reads register 0x01 and accepts only 0x22 (idle, homed) and 0x00 (cold,
  cold-start path only); anything else is refused with nothing sent
  (docs/hardware-safety.md, "Accepted start states"). The tool runs only
  `doctor` (read-only), `load` and `scan`; after the first failure it
  sends nothing but the final read-only `doctor` (no `doctor` at all
  after Ctrl-C or a busy scanner), and it sends no eject, home, initialize
  or park. The SANE backend, the source of the Test 44/46 state, is not
  involved.

**2. A displaced magazine, and the re-seating step that has not run on
hardware.**

- What happened. Test 112 (2026-10-31, docs/test-log.md): a whole-strip
  sweep mode, since withdrawn and not used by this protocol, started its
  pass about 19 mm late; the magazine ended pushed against the front edge
  of the slot, the park stalled with a loud noise and the operator cut the
  power at once. After the power cycle the cold start's jog did not
  complete within 30 s (loader bit clear) and the magazine could not be
  pulled out by hand; it was released with the vendor application. The
  owner found no visible damage, and a normal sweep the same evening ran
  normally.
- What came out of it: `cold_init` now runs a re-seating traverse before
  the jog when a magazine is present at the opening and the loader bit is
  clear at the first read. It was implemented from a vendor capture and
  has **not been run on hardware** (docs/hardware-safety.md, "Power-cycled
  with the magazine present and the jog failing (Test 112)"). The driver
  logs which path it takes (`of135i/device.py`, `cold_init`). The normal
  path logs

      cold_init: loader sensor set (status 0x....) -- no re-seating

  and has been seen on the developer unit five times (Test 114 once,
  Test 115 twice, Tests 116 and 117 once each). The path that has never
  run on hardware logs

      cold_init: loader sensor clear (status 0x....) with a magazine present -- re-seating traverse before the jog (vendor recovery, Test 112; NOT hardware-verified)

  If you see this second line in the output of `load`, you are on that
  path: watch the scanner and apply the abort rule at the first unusual
  sound. If the traverse does not complete in 30 s the driver stops with
  nothing further written; power-cycle and do not retry blindly.
- Recognise it: that log line; a magazine that sits crooked or against the
  front of the slot; a jog that stops.
- Do: do not pull on a magazine that does not come out freely (next item),
  power-cycle, and report the log line and what you saw on the issue.

**3. Pulling a magazine that does not come out freely.** In Test 112 the
magazine could not be pulled out by hand and was released by the vendor
software. The repo records no test of pulling by force and no damage from
it; the rule here is to not do it. If the magazine does not come out
freely, power-cycle and use the `load --release` procedure (README, Usage
step 1), or report it on the issue; do not force it.

**4. Running any command after a failure or an interrupt instead of
power-cycling.** This is item 1 again from the user's side: the state
after a failure is unknown (register 0x01 reads 0x02 or 0x23, not an
accepted start state; docs/hardware-safety.md, "What fail closed means").
Do not run `eject`, `home`, `park`, another `load` or another run of this
tool until the scanner has been power-cycled (off, wait until its lights
are out and it has left the USB bus, on). Eject after a failure is a
motor command from a state the driver does not know; the eject stalls of
Tests 44 and 46 were of that kind.

### 9B. What costs you a scan or a power cycle, but does not harm the scanner

These are inconveniences. "Harmless" is said only where the repo says it;
otherwise the entry says no damage has been seen and where that is
recorded.

**5. The expected ungated failure on an affected unit** (only with a
sequence containing `off`, section 3). A scan with the gate off fails in
`cal_dark_b` (`dark_b is residual ...`); register 0x01 reads 0x02 and the
status word 0xec55 afterwards. It cost the other units a power cycle each
time. Their reports (docs/test-log.md, 2026-11-04 and 2026-11-05) record
supervised runs and, on the second unit, that the motor never stalled; no
damage has been reported. Recognise it: the tool stops, exit code 1, and
says `dark_b is residual`. Do: power-cycle, do not run eject (section 10),
send the tarball.

**6. Ctrl-C at the prompt or during a scan.** At the `load` prompt the
prompt itself says a power cycle is then required. During a scan the
driver marks the session failed and sends nothing more; an interrupted
scan can leave the engine running (register 0x01 = 0x23, the state
behind Test 11d), which is why nothing may be sent afterwards (item 4).
The tool still writes `summary.json`, `report.md` and the tarball, sends
nothing more and exits 130. The repo records no hardware Ctrl-C of a scan
in this protocol; the interrupt paths are covered by offline fault
injection (docs/hardware-safety.md, "Verification status"). Do:
power-cycle; no command before that.

**7. The reinsert step done wrong.** If the magazine is not taken fully
out and pushed back to the mechanical stop at the `load` prompt before
Enter, the load feed does not engage (status 0xfc55). The driver's own
message says: "This is almost always because the magazine was not taken
fully out and reinserted to the mechanical stop when the prompt asked for
it -- the scanner is fine and nothing is stuck." (`of135i/loadflow.py`,
`FEED_NOT_ENGAGED_MSG`; Tests 48 and 49, 2 of 2 the same benign
signature.) The session is failed and a power cycle is required. Do:
power-cycle and run `load` again, reinserting to the stop before Enter.

**8. A magazine left loaded through a power cycle.** The tool never
ejects, so after a failed run the magazine is still loaded. README, Usage
step 1: if the scanner is power-cycled with the magazine still latched,
the first plain `load` stops at the feed instead of engaging (Test 49).
The way out, from the README: `of135i load --release` (releases the
magazine, then stops), power-cycle again, then `of135i load` from the now
loose magazine. No damage has been recorded from this state; it cost a
second power cycle (Tests 49 and 51).

**9. Another program or a virtual machine holding the device.** Another
`of135i` process makes the tool stop with exit code 3: nothing was sent
and no power cycle is needed; close the other program and start again.
The lock is between `of135i` processes only; a SANE session takes no lock
and a virtual machine with USB passthrough is not covered by it, and
"two processes configuring or driving the scanner at once is itself a
hazard" (docs/hardware-safety.md, "Cross-process exclusion"). Close any
scanning application and any virtual machine that has the scanner before
you start (section 2).

**10. A scan that exits 4.** The image check could not verify aperture
coverage; the scan, calibration and park finished and the sidecar was
written (section 5). It counts as completed.

**Information, not a risk: the calibration phases.** The calibration
acquisitions are listed as "may move the motor": each is one execute
pulse (0x0f) with no motor mode written; the repo has no statement that
they leave the holder still, and none that they move it (section 4).

### What the protocol deliberately does not do

No sweep mode, no eject, no home, no initialize, no park, no recovery of
any kind, and nothing but frame 1 at 600 dpi unless you pass `--dpi` or
`--frame`. Which of this has run: the gated scans it consists of have
completed on three units (the developer unit: Tests 114 and 115; the
second unit: 7 gated scans at 600 dpi, frame 4; the Steam Deck: 6 runs /
7 frames at 600 dpi, frame 1; docs/cal-scanstop-gate.md). The tool itself
has been run on the developer unit only: twice with `on,off,off,on`
(Test 115) and once with the default (Test 116).
The default `on,on,on,on` has been run once, on the developer unit
(Test 116).

## 10. After the run

The tool never ejects, so the magazine is still loaded when it ends. It
prints one of these texts at the end, by result, and the plan (also
written to `plan.txt`) carries both.

**Result ok (every scan completed, exit 4 included).** Eject it yourself:

```bash
python3 -m of135i eject
```

This is a MOTOR step: the scanner ejects the magazine. What the repo
establishes about ejecting from the state a completed scan leaves:
`Scanner.eject` documents the variant it uses as the one that freed the
magazine each time the vendor software was used, and docs/test-log.md
(Test 44, correction) records that every verified eject was issued
straight after the magazine flow or after a scan's PARK; it refuses
read-only (nothing written) from the base-table-only state (registers
0x3b/0x3c = 0xff/0xff), and its start state must be 0x22 or 0x00
(docs/hardware-safety.md, "Protected entry points"); the README's normal
workflow ends with the same eject (Usage, step 4); and the owner ran this
eject after the four scans of both runs of Test 115. The scanner leaves
the USB bus about five minutes after a session ends and only a power
cycle brings it back (README, "When something goes wrong"), so eject soon.
If the eject reports an error, send nothing more and follow the driver's
message (a power cycle).

**Result failed, interrupted or error.** Do NOT run eject or any other
command. A motor command from a state the driver does not know is the
hardware risk of section 9A (items 1 and 4). Power-cycle the scanner (off,
wait until its lights are out, on). The magazine is then still loaded:
if the next `load` stops at the feed, follow the README's `load --release`
procedure (Usage, step 1; section 9B, item 8). Never pull a magazine out
that does not come out freely.

**Result busy (exit 3).** Nothing was sent. Do not run eject or any other
command until the other program has ended.

The tool still does not run eject itself.
