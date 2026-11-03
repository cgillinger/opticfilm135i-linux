# Cross-unit protocol

Status: offline-tested (`tests/test_cross_unit_check.py`), not yet run on
any scanner.

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
- A film strip in the magazine, or an empty holder; both are fine.
  The magazine must be inserted to the stop.
- Nothing else holding the device (no other scanning application, no
  virtual machine with USB passthrough).
- About 10 minutes.

## 3. The run

```bash
python3 tools/cross_unit_check.py --out results/xu-01
```

To see the commands first without running anything:

```bash
python3 tools/cross_unit_check.py --out results/xu-01 --dry-run
```

`--dry-run` prints every command, writes `results/xu-01/plan.txt` and
does nothing else. `--out` must not exist or must be empty.

The tool prints the list of commands and asks one question: is the
scanner freshly power-cycled, the magazine inserted to the stop and
nothing else holding the device. Answer `y` to go on. Then it runs,
in this order:

1. `of135i doctor --json doctor-before.json` (read-only).
2. `of135i load`.
3. Four scans, `of135i -v scan --frames 1 --dpi 600 --recalibrate
   --cal-wait-scanstop X -o run-NN-X.tiff`, with X = `off`, `enforce`,
   `enforce`, `off` (`--sequence off,on,on,off`). Each scan calibrates
   afresh.
4. `of135i doctor --json doctor-after.json` (read-only).

It runs no other command: no eject, home, initialize or park, and no
recovery of any kind.

It stops on the first failure of `load` or of a scan. It then runs the
final read-only `doctor` only, writes the evidence and exits non-zero.
After a failure the scanner must be power-cycled before anything else
is sent to it (docs/hardware-safety.md); do not try again without that.
If the exit code is 3, another process holds the scanner; nothing was
sent, and the tool stops without the final `doctor`. Close the other
process and start again.

The `.tiff` images are deleted at the end. Add `--include-images` to
keep them; do that only if asked.

## 4. What to send

Attach `results/xu-01.tar.gz` (the tool prints its path and size) to
issue #35, and say in the comment whether a film strip or an empty
holder was in the scanner, and whether the run completed or where it
stopped.

The archive holds:

- `summary.json`: host facts (platform, Python, pyusb, driver
  revision) and, per run, the gate setting, exit code, seconds and the
  values taken from the sidecar (`calibration`, `dark_b_substituted`,
  `poll_timeouts`, `cr_mismatches`, `cr_mismatch_details`,
  `cal_scanstop_waits`, `phase_seconds`); `stopped_at` is the index of
  the failed run, if any.
- `report.md`: the same as a table.
- `log.txt`: the combined output of every command, with timestamps.
- `run-NN-*.diag.json`: the sidecar of each scan.
- `doctor-before.json`, `doctor-after.json`, `plan.txt`.

There are no images unless `--include-images` was given. Paths are
relative to the result directory, and your home directory is replaced by
`~`. The doctor reports contain the USB ids, the kernel version and the
Python version; look through them if that matters to you.

## 5. What we do with it

We compare `cal_scanstop_waits`, `dark_b_substituted` and
`cr_mismatch_details` across the units, and between `off` and `enforce`
runs on each unit. The result becomes one row per unit in a
compatibility table (unit, host, gate setting, outcome). The raw data
stays in the issue; nothing is published from it without saying so.

## 6. Variants worth running

Only if the default run passed:

- The same at 3600 dpi: `--dpi 3600`.
- A longer sequence: `--sequence off,on,on,off,off,on`.

Use a new `--out` directory each time, and power-cycle the scanner
before each run.
