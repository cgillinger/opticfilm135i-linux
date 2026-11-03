#!/usr/bin/env python3
"""Run the cross-unit protocol (docs/cross-unit-protocol.md) and package the evidence for a report.

The tool only ever runs three of135i commands: `doctor` (read-only), `load`
and `scan`. It sends no recovery command of its own (no eject, home,
initialize or park): after the first failure of load or scan it runs
nothing but the final read-only doctor, writes the evidence and exits
non-zero. The scanner then needs a power cycle (docs/hardware-safety.md).

    python3 tools/cross_unit_check.py --out results/xu-01
    python3 tools/cross_unit_check.py --out results/xu-01 --dry-run

Exit code: 0 all runs completed; 1 a command failed; 2 not started
(bad arguments, output directory in use, or not confirmed); 3 another
process holds the scanner (of135i's EXIT_BUSY).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXIT_BUSY = 3
TOKENS = {"off": "off", "on": "enforce"}
LIFTED = ("calibration", "dark_b_substituted", "poll_timeouts", "cr_mismatches",
          "cr_mismatch_details", "cal_scanstop_waits", "phase_seconds")
PRECONDITION = ("scanner freshly power-cycled, magazine inserted to the stop, "
                "nothing else holding the device")


def parse_sequence(text: str) -> list:
    toks = [t.strip().lower() for t in text.split(",") if t.strip()]
    if not toks or any(t not in TOKENS for t in toks):
        raise argparse.ArgumentTypeError("--sequence takes a comma list of off|on, e.g. off,on,on,off")
    return toks


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

    Tests monkeypatch this function. Output is read in raw chunks so
    prompts without a trailing newline reach the terminal."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    chunks = []
    fd = proc.stdout.fileno()
    while True:
        b = os.read(fd, 4096)
        if not b:
            break
        chunks.append(b)
        sys.stdout.write(b.decode("utf-8", "replace"))
        sys.stdout.flush()
    return proc.wait(), b"".join(chunks).decode("utf-8", "replace")


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


def read_sidecar(image: Path, out: Path):
    """Return the lifted sidecar fields (or None). Also rewrites the sidecar
    file in place so that it carries no absolute paths."""
    sc = image.with_suffix(".diag.json")  # same rule as of135i.diag.sidecar_path
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
    L += ["", "| run | gate | exit | seconds | sidecar | dark_b substituted | poll timeouts | cr mismatches | waits |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in summary["runs"]:
        s = r.get("sidecar") or {}
        waits = s.get("cal_scanstop_waits")
        L.append(f"| {r['index']} | {r['token']} | {r['exit_code']} | {r['seconds']} | "
                 f"{'yes' if r['sidecar_present'] else 'no'} | {s.get('dark_b_substituted', '-')} | "
                 f"{s.get('poll_timeouts', '-')} | {s.get('cr_mismatches', '-')} | "
                 f"{len(waits) if isinstance(waits, list) else '-'} |")
    L += ["", "## What to send", "",
          f"Attach `{summary['tarball']}` to issue #35. It holds the doctor reports, the driver log, "
          "the .diag.json sidecars, summary.json and this file. It holds no images unless "
          "--include-images was given. If a run failed, say so in the comment and power-cycle the "
          "scanner before anything else.", ""]
    return "\n".join(L)


def check_out_dir(out: Path) -> None:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit(f"error: --out {out} exists and is not empty")


def pack(out: Path) -> Path:
    tar = out.parent / (out.name + ".tar.gz")
    with tarfile.open(tar, "w:gz") as t:
        t.add(out, arcname=out.name)
    return tar


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, metavar="DIR", help="result directory; must not exist or be empty")
    ap.add_argument("--sequence", type=parse_sequence, default=parse_sequence("off,on,on,off"),
                    help="comma list of off|on (on = --cal-wait-scanstop enforce); default off,on,on,off")
    ap.add_argument("--dpi", type=int, default=600, help="scan resolution (default 600)")
    ap.add_argument("--frame", type=int, default=1, help="frames per scan, passed as --frames (default 1)")
    ap.add_argument("--include-images", action="store_true",
                    help="keep the scanned .tiff images in the result (default: images are deleted "
                         "after the run and only the .diag.json sidecars are kept)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print every command that would run and write only DIR/plan.txt")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation (maintainer use)")
    args = ap.parse_args(argv)
    if args.frame < 1:
        ap.error("--frame must be >= 1")

    out = Path(args.out).resolve()
    check_out_dir(out)
    plan = build_plan(out, args.sequence, args.dpi, args.frame)
    def render_plan(scrubbed: bool) -> str:
        return "\n".join([f"Precondition: {PRECONDITION}.", "Commands, in order:"]
                         + [f"  {i}. {shown(a, out if scrubbed else None)}" for i, (_, a) in enumerate(plan, 1)]
                         + ["The tool runs nothing else (no eject, home, initialize or park)."])
    print(render_plan(False))
    plan_text = render_plan(True)
    if args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        (out / "plan.txt").write_text(plan_text + "\n")
        return 0
    if not args.yes:
        if input(f"Is this true: {PRECONDITION}? Run the commands above? [y/N] ").strip().lower() != "y":
            print("Not confirmed; nothing was run.")
            return 2

    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.txt").write_text(plan_text + "\n")
    log = out / "log.txt"
    runs = []
    state = {"failed": None, "busy": False, "stopped_at": None}

    def step(argv: list) -> tuple:
        t0 = time.monotonic()
        rc, text = run_command(argv)
        secs = round(time.monotonic() - t0, 2)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with log.open("a") as f:
            f.write(f"=== {stamp} {shown(argv, out)}\n{scrub(text, out)}\n")
        return rc, secs

    def fail(name: str, rc: int, index=None) -> None:
        state["failed"] = name
        state["busy"] = rc == EXIT_BUSY
        state["stopped_at"] = index
        if state["busy"]:
            print("\nAnother process holds the scanner (exit 3). Nothing was sent. "
                  "Close it and start again.")
        else:
            print(f"\n{name} failed with exit code {rc}. Read the driver's advice above. "
                  "Send no further command: power-cycle the scanner before using it again.")

    for name, argv in plan[:-1]:
        if name in ("doctor-before", "load"):
            rc, _ = step(argv)
            if rc != 0:
                fail(name, rc)
                break
        else:
            i = int(name.split("-")[1])
            rc, secs = step(argv)
            image = Path(argv[-1])
            runs.append({"index": i, "token": args.sequence[i - 1], "exit_code": rc, "seconds": secs,
                         "sidecar_present": image.with_suffix(".diag.json").is_file(),
                         "sidecar": read_sidecar(image, out), "_image": image})
            if rc != 0:
                fail(name, rc, i)
                break

    doctor_after = None
    if state["failed"] != "doctor-before" and not state["busy"]:
        doctor_after, _ = step(plan[-1][1])
        if doctor_after != 0 and not state["failed"]:
            state["failed"] = "doctor-after"
    exit_code = EXIT_BUSY if state["busy"] else (1 if state["failed"] else 0)

    summary = {"host": host_facts(), "sequence": args.sequence, "dpi": args.dpi, "frame": args.frame,
               "result": "ok" if exit_code == 0 else ("busy" if state["busy"] else "failed"),
               "failed_step": state["failed"], "doctor_after_exit": doctor_after,
               "runs": [{k: v for k, v in r.items() if k != "_image"} for r in runs],
               "tarball": out.name + ".tar.gz"}
    if state["stopped_at"] is not None:
        summary["stopped_at"] = state["stopped_at"]
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "report.md").write_text(render_report(summary))

    if not args.include_images:
        for r in runs:
            for p in out.glob(r["_image"].stem + ".*"):
                if not p.name.endswith(".diag.json"):
                    p.unlink()
    tar = pack(out)
    print(f"\nEvidence: {tar} ({tar.stat().st_size} bytes)")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
