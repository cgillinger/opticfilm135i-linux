# v0.2.10 — the calibration stop gate: the driver on other people's units

Release notes for the `v0.2.10` tag (2026-11-05) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek. It is developed by
one person on a single OpticFilm 135i unit and has now scanned on two
other units. Previous release notes:
[v0.2.9](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.10/docs/release-notes-v0.2.9.md).

## What this release is

The first release shaped by other units. Two users ran the driver on
their own scanners and found a calibration failure the developer's unit
never shows (issue #35). One of them wrote the fix. It is now the
default.

**The calibration stop gate is the default** (`--cal-wait-scanstop
enforce` on `scan` and `digitize`). After each calibration acquisition
the driver waits, by reading two status registers, until the scanner
reports that it has stopped before it starts the next one. On a fast
host the scanner was still running at that point and the dark
measurement came back as stale data: `dark_b is residual`. Evidence from
three units: without the gate 0 of 3 scans completed on the second unit
and 0 of 2 on the third; with it 7 of 7 and 6 runs / 7 frames. The
developer's unit completed every gated scan and never needed the wait
(Tests 114 to 116), also in a whole-strip sweep through the calling
application (Test 119). `--cal-wait-scanstop off` sends exactly the transfers
of a driver without the gate. If the scanner does not stop within 0.3 s
the calibration fails closed and a power cycle is needed.

Thanks to **bearyjd**, who designed and wrote the gate and ran the first
A/B, and to **Krustenviech**, who reported the failure with the logs
needed to work on it and ran the A/B on a Steam Deck.

## Changes that can break a caller

- **`watch`: a failed eject now exits 75** (it used to exit 1). 75 here
  means "power-cycle, do not restart automatically". A supervisor needs
  to know it (systemd: `RestartPreventExitStatus=75`). Contributed by
  bearyjd.
- **`digitize --film 110 --strips 2` is removed.** One strip per load is
  the routine. A one-strip run behaves exactly as before: 27 scenarios
  gave the same exit codes, output and file contents before and after
  the removal (offline), and a production strip through the calling
  application ran as before on hardware (Test 119).
- **The plain-3600 whole-strip sweep is withdrawn** (`digitize --sweep
  --no-ir` is refused). Its first hardware run displaced the magazine
  (Test 112). Black-and-white film keeps the per-frame cycle. The colour
  sweep is unaffected, but sweeps over more than four apertures are
  refused until a six-aperture sweep has had its own hardware test.

## New

- **Transfer recorder** (`OF135I_TRACE=/new/path/run.jsonl`): an opt-in
  log of every USB transfer of a run, for diagnosis, with a converter
  for offline comparison. Recording off is the default and changes
  nothing. Contributed by bearyjd. One scan with and one without the
  recorder were alike on hardware (Test 117).
- **Cross-unit protocol** (`tools/cross_unit_check.py`,
  `docs/cross-unit-protocol.md`): a fixed, gentle sequence of scans that
  packs its measurements, without images, into an archive for comparison
  between units. Run on the developer's unit (Tests 115, 116).
- **The sidecar says where a status read differed**
  (`cr_mismatch_details` in `.diag.json`), so a report from another unit
  shows which assumption failed.
- **Cold start re-seats a displaced magazine first**, as the vendor
  application does. The ordinary cold start is unchanged on the wire and
  has run on hardware many times since; the re-seating branch itself has
  not run on hardware.
- `of135i version` reports the right number again (it said 0.2.7 through
  v0.2.8 and v0.2.9).

## SANE backend

The backend got the same wait in its generated calibration programs
(`docs/cal-scanstop-gate.md`, "The SANE backend"). On the developer's
unit a 600 dpi and a 3600 dpi scan completed; at 3600 dpi three of the
fourteen waits found the scanner still running and waited 11 to 18 ms
(Test 118). The change is prepared as revision v9 of the submission to
the SANE project (`sane/wp3-package/`) and is not yet on the merge
request, which still holds v8.

## Limits

- The gate has not run on hardware at 1200, 2400 or 7200 dpi, in the
  per-frame cycle of `digitize`, or with 110 film. The mechanism is the same in every profile; that is not the same
  as a run.
- The SANE backend has not run on a unit that shows the failure.
- The cross-unit protocol has not run on a second unit yet.
- The recorder's failure paths (unwritable file, Ctrl-C while recording)
  are offline-tested only.

## Gate

`tools/release_check.py`: FULL (733 tests, 0 skipped). CI green; the
CI workflow now runs the same suite list as the local gate.
