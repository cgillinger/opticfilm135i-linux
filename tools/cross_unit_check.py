#!/usr/bin/env python3
"""Run the cross-unit protocol (docs/cross-unit-protocol.md) and package the evidence for a report.

The tool only ever runs three of135i commands: `doctor` (read-only), `load`
and `scan`. `load` and `scan` MOVE THE FILM HOLDER (motor); `load` also asks
you to take the magazine out and push it back in, so it needs a real
terminal. The printed plan says, for every step, what it sends to the
scanner. The tool sends no recovery command of its own (no eject, home,
initialize or park): after the first failure of load or scan it runs
nothing but the final read-only doctor, writes the evidence and exits
non-zero. The scanner then needs a power cycle (docs/hardware-safety.md).

    python3 tools/cross_unit_check.py --out results/xu-01
    python3 tools/cross_unit_check.py --out results/xu-01 --dry-run

--dry-run prints the plan and exits; it writes nothing and touches no
scanner.

Exit code: 0 all runs completed (a scan that exits 4, the image check, but
left its sidecar counts as completed); 1 a command failed or the tool hit
an unexpected error; 2 not started (bad arguments, output directory in use,
or not confirmed); 3 another process holds the scanner (of135i's
EXIT_BUSY); 130 interrupted (Ctrl-C).
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import os
import platform
import shlex
import subprocess
import sys
import tarfile
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXIT_BUSY = 3
EXIT_INTERRUPTED = 130
#: `of135i scan` exits 4 when the scan, calibration, park and sidecar all
#: finished but the image check could not verify aperture coverage
#: (of135i/cli.py, _cmd_scan: the frame loop, which includes PARK, and
#: _write_diag_sidecar run before `return 4`).
EXIT_COVERAGE_UNVERIFIED = 4
#: The driver's own text for the failure issue #35 is about
#: (of135i/device.py, Scanner._healthy_dark_b).
DARK_B_RESIDUAL_TEXT = "dark_b is residual"
DEFAULT_SEQUENCE = "on,off,off,on"
TOKENS = {"off": "off", "on": "enforce"}
LIFTED = ("calibration", "dark_b_substituted", "poll_timeouts", "cr_mismatches",
          "cr_mismatch_details", "cal_scanstop_waits", "phase_seconds")
PRECONDITION = ("scanner freshly power-cycled, magazine in the slot, "
                "nothing else holding the device")


def parse_sequence(text: str) -> list:
    toks = [t.strip().lower() for t in text.split(",") if t.strip()]
    if not toks or any(t not in TOKENS for t in toks):
        raise argparse.ArgumentTypeError("--sequence takes a comma list of off|on, e.g. on,off,off,on")
    return toks


# ---------------------------------------------------------------------------
# What each step sends to the scanner. Every claim below is tied to the code
# or a doc in the repo (the evidence list is in docs/cross-unit-protocol.md,
# section 4). Where the repo does not establish that something is motor-free,
# the text says "may move the motor" rather than guessing.
# ---------------------------------------------------------------------------

#: The prompt `of135i load` prints (of135i/loadflow.py, REINSERT_PROMPT);
#: tests/test_cross_unit_check.py checks this quote against the source.
LOAD_PROMPT_QUOTE = ("JOG done. Now take the magazine FULLY OUT of the slot, then insert it fresh "
                     "all the way to the mechanical stop. Press Enter when it is at the stop "
                     "(Ctrl-C aborts; a power cycle is then required):")

HOLDER_NOTE = ("MOTOR = the scanner moves the film holder (the part that carries the film "
               "through the scanner; the driver's source and the review call it the carriage).")

ABORT_RULE = ("If the holder grinds, scrapes or stops, switch the scanner off at its power "
              "switch (docs/hardware-safety.md, 'Recovery after an interrupted scan'). Do not "
              "send any recovery command (no eject, home or park) and do not treat restarting "
              "the program as a recovery.")

#: (phase, motor, what), in the order the driver runs them for one frame.
#: Names are the driver's own (session.phase / table names).
SCAN_PHASES = [
    ("open session", "no motor",
     "reads register 0x01 (the start-state check; anything but 0x22 is refused and nothing is "
     "written), then selects the USB configuration (detaching a bound kernel driver first), then "
     "reads the loader sensor."),
    ("base_init", "no motor",
     "register writes: the power-on base table and the analog front-end base values. These set "
     "the motor-mode and feed-length registers but never write the execute register 0x0f, which "
     "starts every move in the driver."),
    ("prep, afe_base", "no motor",
     "register writes, status reads and polls; the lamp is switched on. Neither table writes the "
     "execute register 0x0f."),
    ("cal_dark_a, cal_dark_b, cal_white, cal_gain_check_a, cal_gain_check_b, "
     "cal_shading_measure, cal_shading_upload, cal_shading_verify", "may move the motor",
     "calibration acquisitions: register writes, one execute pulse (0x0f) per acquisition, then "
     "the measured lines are read back; cal_shading_upload only writes the shading table. These "
     "phases write no motor mode (register 0x02 stays 0x00 from afe_base). The driver's own "
     "docstring calls the white-line measurement 'a single-line read, no motor move'; for the "
     "other acquisitions the repo has no such statement and no measurement, so they are not "
     "claimed motor-free here. cal_white is repeated at a fixed interval while the lamp is "
     "still warming up."),
    ("position", "MOTOR",
     "moves the film holder to the frame (feed mode 0x18, then a strict completion check)."),
    ("scan", "MOTOR",
     "the image pass: the holder moves while the image lines are read (mode 0x30)."),
    ("park", "MOTOR",
     "the holder returns (mode register set to 0x30; docs/park-completion-analysis.md: 'carriage "
     "returning, busy'), the lamp is switched off, then the driver waits for the end state."),
]

GATE_TEXT = {
    "off": "gate off: no extra transfer at all (the wire is identical to the driver without the "
           "gate, docs/cal-scanstop-gate.md).",
    "enforce": "gate enforce: after each calibration acquisition the driver additionally READS "
               "status registers 0x100 and 0x101 until bit 0 is clear (or 0.3 s pass, then it "
               "stops and the session is failed). Reads only: no extra write, no motor "
               "(docs/cal-scanstop-gate.md, 'What enforce does').",
}


def step_info(name: str, token: str | None = None) -> dict:
    """Plain-words description of one plan step: title, motor flag, and the
    lines 'sends', 'you' (what the operator does) and 'for'."""
    if name.startswith("doctor"):
        before = name.endswith("before")
        return {
            "title": "doctor (" + ("before" if before else "after") + ")",
            "motor": False,
            "sends": ["USB reads only: device descriptors, chip id, status word, a register dump, "
                      "the loader sensor, and one read of the button endpoint (which would take a "
                      "pending button event if there were one). The session is read-only: the "
                      "driver refuses every write in it. No register writes, no motor."],
            "you": "nothing.",
            "for": ("records the scanner's state before the run." if before
                    else "records the scanner's state after the run. Not run after Ctrl-C."),
        }
    if name == "load":
        return {
            "title": "load",
            "motor": True,
            "sends": [
                "register writes AND motor moves, in these stages:",
                "(a) on a freshly power-cycled scanner (reg 0x01 = 0x00, the precondition) the "
                "cold start first: chip handshake, register tables, front-end bring-up, then "
                "three rounds of loader homing moves, three moves each, nine in all. MOTOR. If "
                "the magazine is present but displaced it may also run the load traverse before "
                "the jog (not yet verified on hardware).",
                "(b) the device-open register table (writes, no execute pulse).",
                "(c) the app-start jog: feed, feed, eject, each completion checked. MOTOR.",
                "(d) it reads register 0x32, prints the prompt below and waits for Enter; no motor "
                "runs while it waits. One more register read follows your Enter (reads only).",
                "(e) after you press Enter, the load: the engaging feed and the long prescan "
                "traverse, then a final status read. MOTOR.",
            ],
            "you": ("take the magazine FULLY OUT of the slot, push it back in to the stop, then "
                    "press Enter. The prompt reads: \"" + LOAD_PROMPT_QUOTE + "\" This needs a "
                    "real terminal (stdin); without --yes the tool refuses to start without one."),
            "for": "puts the magazine and holder at the position all scans start from.",
        }
    x = TOKENS.get(token or "off", "off")
    return {
        "title": f"scan, gate {x}",
        "motor": True,
        "sends": [
            "register writes AND motor moves, phase by phase as listed under 'Scan phases' "
            "below: the preparation does not move the motor; the calibration phases may move it; "
            "position, scan and park MOVE THE FILM HOLDER (MOTOR). A fresh calibration is forced "
            "(--recalibrate). Writes an image and a .diag.json on the host only.",
            GATE_TEXT[x],
        ],
        "you": "nothing.",
        "for": ("one calibrated scan of the frame. The gate setting is the variable under test "
                "(issue #35)."),
    }


def _wrap(prefix: str, text: str, width: int = 100) -> list:
    return textwrap.wrap(text, width=width, initial_indent=prefix,
                         subsequent_indent=" " * len(prefix)) or [prefix.rstrip()]


def render_plan(plan: list, sequence: list, out: Path, scrubbed: bool) -> str:
    """The printed plan and plan.txt: for every step the command, what it
    sends (reads only / register writes / MOTOR), what you do, and why."""
    L = [f"Precondition: {PRECONDITION}.", HOLDER_NOTE, "", "Steps, in order:"]
    scan_tokens = iter(sequence)
    for i, (name, argv) in enumerate(plan, 1):
        info = step_info(name, next(scan_tokens) if name.startswith("run-") else None)
        flag = "   [MOTOR]" if info["motor"] else "   [reads only]"
        L += ["", f"{i}. {info['title']}{flag}",
              f"   command: {shown(argv, out if scrubbed else None)}"]
        for j, line in enumerate(info["sends"]):
            L += _wrap("   sends:   " if j == 0 else "            ", line)
        L += _wrap("   you:     ", info["you"])
        L += _wrap("   for:     ", info["for"])
    L += ["", "Scan phases (the same for every scan step; names are the driver's):"]
    for phase, motor, what in SCAN_PHASES:
        L += _wrap("  - ", f"{phase} [{motor}]: {what}")
    L.append("")
    for para in ("STOP RULE: the tool stops at the first failed load or scan and then sends "
                 "nothing but the final read-only doctor. After a failure, power-cycle the "
                 "scanner; a new run may then be started with a new --out directory.",
                 "A scan that exits 4 but wrote its sidecar is not a failure: the scan, "
                 "calibration and park finished; only the image check could not verify the "
                 "frame.",
                 ABORT_RULE,
                 "The tool runs nothing else (no eject, home, initialize or park)."):
        L += _wrap("", para)
    return "\n".join(L)


def of135i_cmd(*args: str) -> list:
    # Same entry point as `python3 -m of135i` (of135i/__main__.py -> cli.main).
    return [sys.executable, "-m", "of135i", *args]


def build_plan(out: Path, sequence: list, dpi: int, frame: int) -> list:
    """The exact argv lists, in order, as (step name, argv)."""
    plan = [("doctor-before", of135i_cmd("doctor", "--json", str(out / "doctor-before.json"))),
            ("load", of135i_cmd("load"))]
    for i, tok in enumerate(sequence, 1):
        x = TOKENS[tok]
        # -v is a global option of the CLI: it must come before the subcommand.
        plan.append((f"run-{i:02d}", of135i_cmd(
            "-v", "scan", "--frames", str(frame), "--dpi", str(dpi), "--recalibrate",
            "--cal-wait-scanstop", x, "-o", str(out / f"run-{i:02d}-{x}.tiff"))))
    plan.append(("doctor-after", of135i_cmd("doctor", "--json", str(out / "doctor-after.json"))))
    return plan


def scrub(text: str, out: Path) -> str:
    """Remove personal paths: the output dir becomes relative, the home dir '~'."""
    text = text.replace(str(out) + os.sep, "").replace(str(out), ".")
    home = str(Path.home())
    if home and home != os.sep:
        text = text.replace(home, "~")
    return text


def shown(argv: list, out: Path | None = None) -> str:
    """argv as a shell line, the interpreter shown as python3; with `out`,
    also scrubbed of personal paths (for files that go into the tarball)."""
    line = " ".join(shlex.quote("python3" if (i == 0 and a == sys.executable) else a)
                    for i, a in enumerate(argv))
    return scrub(line, out) if out else line


def run_command(argv: list) -> tuple:
    """Run argv, echo its output live and return (exit code, combined stdout+stderr).

    Tests monkeypatch this function. Output is read in raw chunks and
    flushed at once, so a prompt without a trailing newline (the load
    prompt) is visible before the answer is given. The child inherits
    stdin, so the operator answers it directly. On Ctrl-C (which the
    terminal also sends to the child) the tool waits a short time for the
    child to end and re-raises with the partial output in `partial_output`;
    it sends nothing to the scanner itself."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    chunks = []
    fd = proc.stdout.fileno()
    try:
        while True:
            b = os.read(fd, 4096)
            if not b:
                break
            chunks.append(b)
            sys.stdout.write(b.decode("utf-8", "replace"))
            sys.stdout.flush()
        return proc.wait(), b"".join(chunks).decode("utf-8", "replace")
    except BaseException as e:
        try:
            proc.wait(timeout=30)
        except BaseException:
            pass
        e.partial_output = b"".join(chunks).decode("utf-8", "replace")
        raise


def _git_rev() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def host_facts() -> dict:
    facts = {"platform": platform.platform(), "python": platform.python_version()}
    try:
        import usb
        facts["pyusb"] = getattr(usb, "__version__", "unknown")
    except Exception:
        facts["pyusb"] = None
    rev = _git_rev()
    if rev:
        facts["driver_revision"] = rev
    else:
        try:
            sys.path.insert(0, str(REPO))
            import of135i
            facts["driver_version"] = of135i.__version__
        except Exception:
            facts["driver_version"] = None
    return facts


def _relativize(obj, out: Path):
    """Strip the output directory and the home directory from every string."""
    if isinstance(obj, str):
        return scrub(obj, out)
    if isinstance(obj, list):
        return [_relativize(v, out) for v in obj]
    if isinstance(obj, dict):
        return {k: _relativize(v, out) for k, v in obj.items()}
    return obj


def sidecar_files(out: Path, index: int) -> list:
    """The sidecars run `index` left, found by what is on disk: `scan --frames N`
    appends -f<N> to the -o name (of135i/cli.py, _frame_output), so the name is
    not predictable from the argv."""
    return sorted(out.glob(f"run-{index:02d}-*.diag.json"))


def read_sidecar(sc: Path, out: Path):
    """Return the lifted fields of one sidecar (or None). Also rewrites the
    sidecar file in place so that it carries no absolute paths."""
    if not sc.is_file():
        return None
    try:
        data = json.loads(sc.read_text())
    except (OSError, ValueError):
        return {"unreadable": True}
    data = _relativize(data, out)
    try:
        sc.write_text(json.dumps(data, indent=2))
    except OSError:
        pass
    lifted = {k: data[k] for k in LIFTED if k in data}
    if "output" in data:
        lifted["output"] = data["output"]
    return lifted


def run_sidecars(out: Path, index: int) -> dict:
    """Run-record fields for the sidecars of run `index`."""
    found = sidecar_files(out, index)
    lifted = [read_sidecar(sc, out) for sc in found]
    return {"sidecar_present": bool(found), "sidecar": lifted[0] if lifted else None,
            "sidecars": lifted}


ALLOWED_FILES = ("summary.json", "report.md", "log.txt", "plan.txt")
ALLOWED_PATTERNS = ("doctor-*.json", "*.diag.json")


def is_allowed(name: str) -> bool:
    """The archive allowlist: no images, no .cal.npz."""
    return name in ALLOWED_FILES or any(fnmatch.fnmatch(name, p) for p in ALLOWED_PATTERNS)


def remove_unlisted(out: Path) -> None:
    """Delete every file (or symlink, not its target) in `out` that is not on
    the allowlist. `out` was empty at the start, so all of it is this run's."""
    for p in out.iterdir():
        if (p.is_symlink() or p.is_file()) and not is_allowed(p.name):
            p.unlink()


def classify_run(rc: int, sidecar_present: bool, text: str) -> tuple:
    """(outcome, failure_class) for one finished scan run.

    A scan that exits 4 AND left its sidecar finished the scan, the
    calibration and the park (of135i/cli.py, _cmd_scan): only the image
    check could not verify aperture coverage. Exit 4 without a sidecar, or
    any other non-zero exit, is a failure."""
    if rc == 0:
        return "completed", None
    if rc == EXIT_COVERAGE_UNVERIFIED and sidecar_present:
        return "completed-coverage-unverified", None
    return "failed", ("dark_b_residual" if DARK_B_RESIDUAL_TEXT in text else None)


def findings(summary: dict) -> list:
    """Plain statements about what the runs show; used by report.md and by
    the final printout (so the two always agree)."""
    L = []
    runs = summary["runs"]
    cov = [r["index"] for r in runs if r.get("outcome") == "completed-coverage-unverified"]
    if cov:
        L.append("Run(s) " + ", ".join(map(str, cov)) + " exited 4. That is the image check "
                 "(aperture coverage could not be verified), not the transport or the "
                 "calibration: the scan, calibration and park finished and the sidecar was "
                 "written. The run counts as completed.")
    resid = [r["index"] for r in runs if r.get("failure_class") == "dark_b_residual"]
    if resid:
        L.append("Run(s) " + ", ".join(map(str, resid)) + " failed with 'dark_b is residual': "
                 "this is the failure issue #35 is about, and it is a result worth sending. "
                 "The scanner must be power-cycled before anything else is sent to it.")
    if summary.get("result") in ("interrupted", "error"):
        what = ("was interrupted (Ctrl-C)" if summary["result"] == "interrupted"
                else "stopped on an unexpected error of the tool (" + str(summary.get("error")) + ")")
        L.append(f"The run {what} during {summary.get('interrupted_step')}. Nothing more was sent "
                 "to the scanner, not even the final doctor. The scanner must be power-cycled "
                 "before any further command.")
    elif summary.get("failed_step") and not resid:
        L.append(f"Step {summary['failed_step']} failed. Send no further command: power-cycle "
                 "the scanner before using it again.")
    return L


def render_report(summary: dict) -> str:
    L = ["# of135i cross-unit report", ""]
    h = summary["host"]
    L.append(f"- Host: {h.get('platform')}, Python {h.get('python')}, pyusb {h.get('pyusb')}, "
             f"driver {h.get('driver_revision') or h.get('driver_version')}")
    L.append(f"- Sequence: {','.join(summary['sequence'])} at {summary['dpi']} dpi, "
             f"{summary['frame']} frame(s) per scan")
    L.append(f"- Result: {summary['result']}")
    if summary.get("failed_step"):
        L.append(f"- Failed step: {summary['failed_step']}")
    L += ["", "| run | gate | outcome | exit | seconds | sidecar | dark_b substituted | poll timeouts | cr mismatches | waits |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in summary["runs"]:
        s = r.get("sidecar") or {}
        waits = s.get("cal_scanstop_waits")
        outcome = r.get("outcome", "-") + (f" ({r['failure_class']})" if r.get("failure_class") else "")
        L.append(f"| {r['index']} | {r['token']} | {outcome} | {r['exit_code']} | {r['seconds']} | "
                 f"{'yes' if r['sidecar_present'] else 'no'} | {s.get('dark_b_substituted', '-')} | "
                 f"{s.get('poll_timeouts', '-')} | {s.get('cr_mismatches', '-')} | "
                 f"{len(waits) if isinstance(waits, list) else '-'} |")
    notes = findings(summary)
    if notes:
        L += ["", "## What the runs show", ""] + [f"- {n}" for n in notes]
    L += ["", "## What to send", "",
          f"Attach `{summary['tarball']}` to issue #35. It holds the doctor reports, the driver log, "
          "the .diag.json sidecars, summary.json and this file. It holds no images unless "
          "--include-images was given. If a run failed or was interrupted, say so in the comment "
          "and power-cycle the scanner before anything else.", ""]
    return "\n".join(L)


def check_out_dir(out: Path) -> None:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit(f"error: --out {out} exists and is not empty")


def pack(out: Path, include_all: bool = False) -> Path:
    """Tarball next to `out`. Without include_all only allowlisted regular
    files are added; the result is then re-read and every member checked, and
    on any stray member the tarball is deleted and SystemExit raised."""
    tar = out.parent / (out.name + ".tar.gz")
    with tarfile.open(tar, "w:gz") as t:
        if include_all:
            t.add(out, arcname=out.name)
        else:
            for p in sorted(out.iterdir()):
                if _packable(p):
                    t.add(p, arcname=f"{out.name}/{p.name}")
    if not include_all:
        verify_tar(tar, out.name)
    return tar


def _packable(p: Path) -> bool:
    return p.is_file() and not p.is_symlink() and is_allowed(p.name)


def verify_tar(tar: Path, root: str) -> None:
    """Fail closed: every member must be an allowlisted regular file, else the
    tarball is deleted and SystemExit raised."""
    with tarfile.open(tar) as t:
        bad = [m.name for m in t.getmembers()
               if m.name != root and not (m.isfile() and is_allowed(m.name.split("/", 1)[-1]))]
    if bad:
        tar.unlink()
        raise SystemExit(f"error: unexpected file(s) in the archive, tarball deleted: {', '.join(bad)}")


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, metavar="DIR", help="result directory; must not exist or be empty")
    ap.add_argument("--sequence", type=parse_sequence, default=parse_sequence(DEFAULT_SEQUENCE),
                    help="comma list of off|on (on = --cal-wait-scanstop enforce); default "
                         f"{DEFAULT_SEQUENCE} (a gated run first: see docs/cross-unit-protocol.md)")
    ap.add_argument("--dpi", type=int, default=600, help="scan resolution (default 600)")
    ap.add_argument("--frame", type=int, default=1, help="frame NUMBER (1-based), passed as --frames N (default 1)")
    ap.add_argument("--include-images", action="store_true",
                    help="keep the scanned .tiff images in the result (default: images are deleted "
                         "after the run and only the .diag.json sidecars are kept)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan (every command and what it sends to the scanner) and exit; "
                         "writes nothing, not even DIR, and touches no scanner")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation (maintainer use)")
    args = ap.parse_args(argv)
    if args.frame < 1:
        ap.error("--frame must be >= 1")

    out = Path(args.out).resolve()
    plan = build_plan(out, args.sequence, args.dpi, args.frame)
    print(render_plan(plan, args.sequence, out, False))
    if args.dry_run:
        # Writes nothing: no --out directory, no plan.txt.
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            print(f"\nnote: --out {out} exists and is not empty; a real run would refuse it.")
        print("\nDry run: nothing was written and nothing was sent to the scanner.")
        return 0
    check_out_dir(out)
    if not args.yes:
        # `of135i load` asks the operator a question mid-run and reads the
        # answer from stdin, which the child inherits: without a terminal it
        # would end at the prompt after the jog has already moved the holder.
        if not sys.stdin.isatty():
            print("error: the load step asks you to take the magazine out and push it back in, "
                  "and reads your Enter from the terminal; run this tool in a real terminal. "
                  "Nothing was sent.", file=sys.stderr)
            return 2
        try:
            answer = input(f"Is this true: {PRECONDITION}? Run the steps above? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            print("Not confirmed; nothing was run.")
            return 2

    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.txt").write_text(render_plan(plan, args.sequence, out, True) + "\n")
    log = out / "log.txt"
    runs = []
    state = {"failed": None, "busy": False, "stopped_at": None,
             "step": None, "error": None, "current_run": None}

    def _log(argv: list, text: str, t0: float, note: str = "") -> float:
        secs = round(time.monotonic() - t0, 2)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with log.open("a") as f:
            f.write(f"=== {stamp} {shown(argv, out)}{' ' + note if note else ''}\n{scrub(text, out)}\n")
        return secs

    def step(argv: list) -> tuple:
        t0 = time.monotonic()
        try:
            rc, text = run_command(argv)
        except BaseException as e:
            _log(argv, getattr(e, "partial_output", ""), t0, note="INTERRUPTED")
            raise
        return rc, _log(argv, text, t0), text

    def fail(name: str, rc: int, index=None, failure_class=None) -> None:
        state["failed"] = name
        state["busy"] = rc == EXIT_BUSY
        state["stopped_at"] = index
        if state["busy"]:
            print("\nAnother process holds the scanner (exit 3). Nothing was sent. "
                  "Close it and start again.")
        elif failure_class == "dark_b_residual":
            print(f"\n{name} failed with exit code {rc}: 'dark_b is residual'. This is the failure "
                  "issue #35 is about, and it is a result worth sending. Send no further command: "
                  "the scanner must be power-cycled before anything else.")
        else:
            print(f"\n{name} failed with exit code {rc}. Read the driver's advice above. "
                  "Send no further command: power-cycle the scanner before using it again.")

    doctor_after = None
    interrupted = None
    try:
        for name, argv in plan[:-1]:
            state["step"] = name
            if name in ("doctor-before", "load"):
                rc, _, _ = step(argv)
                if rc != 0:
                    fail(name, rc)
                    break
            else:
                i = int(name.split("-")[1])
                image = Path(argv[-1])
                state["current_run"] = {"index": i, "token": args.sequence[i - 1], "image": image}
                rc, secs, text = step(argv)
                state["current_run"] = None
                sc = run_sidecars(out, i)
                outcome, fclass = classify_run(rc, sc["sidecar_present"], text)
                run = {"index": i, "token": args.sequence[i - 1], "outcome": outcome,
                       "exit_code": rc, "seconds": secs, **sc}
                if fclass:
                    run["failure_class"] = fclass
                runs.append(run)
                if outcome == "failed":
                    fail(name, rc, i, fclass)
                    break
        if state["failed"] != "doctor-before" and not state["busy"]:
            state["step"] = plan[-1][0]
            doctor_after, _, _ = step(plan[-1][1])
            if doctor_after != 0 and not state["failed"]:
                state["failed"] = "doctor-after"
    except KeyboardInterrupt:
        interrupted = "interrupted"
    except Exception as e:  # keep the evidence of an unexpected tool error too
        interrupted = "error"
        state["error"] = f"{type(e).__name__}: {e}"
    if interrupted:
        # Nothing more goes to the scanner: no final doctor.
        cur = state["current_run"]
        if cur is not None:
            runs.append({"index": cur["index"], "token": cur["token"], "outcome": "interrupted",
                         "exit_code": None, "seconds": None,
                         **run_sidecars(out, cur["index"])})
        exit_code = EXIT_INTERRUPTED if interrupted == "interrupted" else 1
        result = interrupted
    elif state["busy"]:
        exit_code, result = EXIT_BUSY, "busy"
    elif state["failed"]:
        exit_code, result = 1, "failed"
    else:
        exit_code, result = 0, "ok"

    summary = {"host": host_facts(), "sequence": args.sequence, "dpi": args.dpi, "frame": args.frame,
               "result": result, "failed_step": state["failed"], "doctor_after_exit": doctor_after,
               "runs": runs,
               "tarball": out.name + ".tar.gz"}
    if state["stopped_at"] is not None:
        summary["stopped_at"] = state["stopped_at"]
    if interrupted:
        summary["interrupted_step"] = state["step"]
        if state["error"]:
            summary["error"] = state["error"]
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "report.md").write_text(render_report(summary))

    if not args.include_images:
        remove_unlisted(out)
    try:
        tar = pack(out, include_all=args.include_images)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    print("\nRuns:")
    for r in summary["runs"]:
        print(f"  run {r['index']} (gate {TOKENS[r['token']]}): {r['outcome']}, exit {r['exit_code']}")
    for n in findings(summary):
        print(f"\n{n}")
    print(f"\nEvidence: {tar} ({tar.stat().st_size} bytes)"
          + (" - images ARE included (--include-images)" if args.include_images else ", no images"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
