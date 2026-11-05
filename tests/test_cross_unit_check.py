"""Offline tests for tools/cross_unit_check.py (subprocess mocked, no scanner)."""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import pathlib
import re
import sys
import tarfile
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import cross_unit_check as xu  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from of135i import cli, diag  # noqa: E402


class Fake:
    """Replacement for xu.run_command. Records argv; `fail` maps the 1-based
    index of a scan (or 'load') to an exit code; `no_sidecar` is a set of scan
    indices that leave no .diag.json; `text` maps a scan index to its output;
    `raises` maps a scan index to an exception to raise instead."""

    def __init__(self, fail=None, no_sidecar=(), text=None, raises=None):
        self.calls = []
        self.fail = fail or {}
        self.no_sidecar = set(no_sidecar)
        self.text = text or {}
        self.raises = raises or {}
        self.scans = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        sub = [a for a in argv[3:] if not a.startswith("-")][0]
        if sub == "scan":
            self.scans += 1
            # Name the outputs exactly as the driver does for `--frames N`
            # (cli._cmd_scan: multi=True -> _frame_output; diag.sidecar_path).
            frames = argv[argv.index("--frames") + 1]
            img = pathlib.Path(cli._frame_output(argv[argv.index("-o") + 1], int(frames)))
            if self.scans in self.raises:
                exc = self.raises[self.scans]
                exc.partial_output = "partial scan output\n"
                raise exc
            rc = self.fail.get(self.scans, 0)
            img.write_bytes(b"II*\x00")
            img.with_suffix(".overscan.tiff").write_bytes(b"II*\x00")
            pathlib.Path(diag.cal_data_path(str(img))).write_bytes(b"npz")
            if self.scans not in self.no_sidecar:
                pathlib.Path(diag.sidecar_path(str(img))).write_text(json.dumps({
                    "output": str(img), "calibration": {"ok": True}, "dark_b_substituted": False,
                    "poll_timeouts": 0, "cr_mismatches": 0, "cal_scanstop_waits": [{"ms": 3}],
                    "phase_seconds": {"cal": 1.0}, "unrelated": 1}))
            return rc, self.text.get(self.scans, "scan output\n")
        if sub == "load":
            return self.fail.get("load", 0), "load output\n"
        return 0, "doctor output\n"


def _run(tmp_path, monkeypatch, fake, *extra):
    monkeypatch.setattr(xu, "run_command", fake)
    out = tmp_path / "xu"
    rc = xu.main(["--out", str(out), "--yes", *extra])
    return rc, out.resolve()


def _quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


def _subs(fake):
    return [[a for a in c[3:] if not a.startswith("-")][0] for c in fake.calls]


def test_default_sequence_argv(tmp_path, monkeypatch):
    fake = Fake()
    rc, out = _run(tmp_path, monkeypatch, fake)
    assert rc == 0
    py = [sys.executable, "-m", "of135i"]
    exp = [py + ["doctor", "--json", str(out / "doctor-before.json")], py + ["load"]]
    for i, x in enumerate(["enforce", "off", "off", "enforce"], 1):
        exp.append(py + ["-v", "scan", "--frames", "1", "--dpi", "600", "--recalibrate",
                         "--cal-wait-scanstop", x, "-o", str(out / f"run-{i:02d}-{x}.tiff")])
    exp.append(py + ["doctor", "--json", str(out / "doctor-after.json")])
    assert fake.calls == exp
    s = json.loads((out / "summary.json").read_text())
    assert s["sequence"] == ["on", "off", "off", "on"] and "stopped_at" not in s
    assert s["result"] == "ok" and [r["outcome"] for r in s["runs"]] == ["completed"] * 4
    assert s["runs"][0]["sidecar"]["output"] == "run-01-enforce-f1.tiff"   # relative, no personal path
    assert s["runs"][0]["sidecar"]["cal_scanstop_waits"] == [{"ms": 3}]
    assert "unrelated" not in s["runs"][0]["sidecar"]
    assert (out / "report.md").is_file() and (out / "log.txt").is_file()
    assert (out.parent / "xu.tar.gz").is_file()
    assert str(out) not in (out / "log.txt").read_text()


def test_stop_on_failure(tmp_path, monkeypatch):
    fake = Fake(fail={2: 1})
    rc, out = _run(tmp_path, monkeypatch, fake)
    assert rc != 0
    assert fake.scans == 2
    assert _subs(fake) == ["doctor", "load", "scan", "scan", "doctor"]
    s = json.loads((out / "summary.json").read_text())
    assert s["stopped_at"] == 2 and len(s["runs"]) == 2 and s["result"] == "failed"
    assert s["runs"][1]["outcome"] == "failed" and "failure_class" not in s["runs"][1]
    assert (out.parent / "xu.tar.gz").is_file()


def test_load_failure_runs_no_scan(tmp_path, monkeypatch):
    fake = Fake(fail={"load": 1})
    rc, out = _run(tmp_path, monkeypatch, fake)
    assert rc != 0 and _subs(fake) == ["doctor", "load", "doctor"]


def test_busy_skips_final_doctor(tmp_path, monkeypatch):
    fake = Fake(fail={2: 3})
    rc, out = _run(tmp_path, monkeypatch, fake)
    assert rc == 3
    assert _subs(fake) == ["doctor", "load", "scan", "scan"]
    assert json.loads((out / "summary.json").read_text())["result"] == "busy"


def test_exit_4_with_sidecar_is_completed_and_continues(tmp_path, monkeypatch):
    fake = Fake(fail={2: 4})
    (rc, out), printed = _quiet(lambda: _run(tmp_path, monkeypatch, fake))
    assert rc == 0                                   # the tool's own exit code
    assert fake.scans == 4 and _subs(fake)[-1] == "doctor"
    s = json.loads((out / "summary.json").read_text())
    assert s["result"] == "ok" and s["failed_step"] is None
    assert [r["outcome"] for r in s["runs"]] == ["completed", "completed-coverage-unverified",
                                                 "completed", "completed"]
    assert s["runs"][1]["exit_code"] == 4
    report = (out / "report.md").read_text()
    for text in (report, printed):
        assert "exited 4" in text and "image check" in text and "not the transport" in text


def test_exit_4_without_sidecar_is_a_failure(tmp_path, monkeypatch):
    fake = Fake(fail={2: 4}, no_sidecar={2})
    rc, out = _run(tmp_path, monkeypatch, fake)
    assert rc == 1 and fake.scans == 2
    s = json.loads((out / "summary.json").read_text())
    assert s["runs"][1]["outcome"] == "failed" and s["stopped_at"] == 2


def test_dark_b_residual_is_classified(tmp_path, monkeypatch):
    msg = "error: CalibrationError: dark_b is residual and no healthy dark_b has been measured\n"
    fake = Fake(fail={1: 1}, text={1: msg})
    (rc, out), printed = _quiet(lambda: _run(tmp_path, monkeypatch, fake))
    assert rc == 1 and fake.scans == 1
    assert _subs(fake) == ["doctor", "load", "scan", "doctor"]      # still only the read-only doctor
    s = json.loads((out / "summary.json").read_text())
    assert s["runs"][0]["failure_class"] == "dark_b_residual" and s["runs"][0]["outcome"] == "failed"
    report = (out / "report.md").read_text()
    for text in (report, printed):
        assert "issue #35" in text and "worth sending" in text and "power-cycled" in text
    # Detection is by the output text only: another failure gets no class.
    fake2 = Fake(fail={1: 1}, text={1: "error: something else\n"})
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(xu, "run_command", fake2)
        out2 = pathlib.Path(d) / "x"
        _quiet(xu.main, ["--out", str(out2), "--yes"])
        assert "failure_class" not in json.loads((out2 / "summary.json").read_text())["runs"][0]


def test_interrupt_keeps_evidence_and_sends_nothing_more(tmp_path, monkeypatch):
    fake = Fake(raises={2: KeyboardInterrupt()})
    (rc, out), printed = _quiet(lambda: _run(tmp_path, monkeypatch, fake))
    assert rc == 130
    assert _subs(fake) == ["doctor", "load", "scan", "scan"]        # no final doctor
    s = json.loads((out / "summary.json").read_text())
    assert s["result"] == "interrupted" and s["interrupted_step"] == "run-02"
    assert [r["outcome"] for r in s["runs"]] == ["completed", "interrupted"]
    assert (out / "report.md").is_file() and (out.parent / "xu.tar.gz").is_file()
    assert "partial scan output" in (out / "log.txt").read_text()
    assert "power-cycled before any further command" in printed
    assert "power-cycled before any further command" in (out / "report.md").read_text()


def test_unexpected_error_keeps_evidence(tmp_path, monkeypatch):
    fake = Fake(raises={1: RuntimeError("boom")})
    (rc, out), printed = _quiet(lambda: _run(tmp_path, monkeypatch, fake))
    assert rc == 1 and _subs(fake) == ["doctor", "load", "scan"]
    s = json.loads((out / "summary.json").read_text())
    assert s["result"] == "error" and "RuntimeError: boom" in s["error"]
    assert (out.parent / "xu.tar.gz").is_file()


def test_images_deleted_unless_included(tmp_path, monkeypatch):
    rc, out = _run(tmp_path, monkeypatch, Fake())
    assert len(list(out.glob("*.diag.json"))) == 4
    assert all(xu.is_allowed(p.name) for p in out.iterdir())
    assert not list(out.glob("*.tiff")) and not list(out.glob("*.npz"))
    with tarfile.open(out.parent / "xu.tar.gz") as t:
        names = [n.split("/", 1)[-1] for n in t.getnames() if n != "xu"]
        assert names and all(xu.is_allowed(n) for n in names)
        assert sum(n.endswith(".diag.json") for n in names) == 4
        assert "run-01-enforce-f1.diag.json" in names
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(xu, "run_command", Fake())
        out2 = pathlib.Path(d) / "xu2"
        assert xu.main(["--out", str(out2), "--yes", "--include-images"]) == 0
        assert len(list(out2.glob("*.tiff"))) == 8 and len(list(out2.glob("*.npz"))) == 4
        with tarfile.open(out2.parent / "xu2.tar.gz") as t:
            assert len([n for n in t.getnames() if n.endswith(".npz")]) == 4


def test_sidecars_are_found_and_lifted(tmp_path, monkeypatch):
    (rc, out), printed = _quiet(lambda: _run(tmp_path, monkeypatch, Fake()))
    s = json.loads((out / "summary.json").read_text())
    for r in s["runs"]:
        assert r["sidecar_present"] and r["sidecar"]["cal_scanstop_waits"] == [{"ms": 3}]
        assert len(r["sidecars"]) == 1
    report = (out / "report.md").read_text()
    assert "| yes |" in report and "| no |" not in report
    assert "no images" in printed


def test_post_pack_check_fails_closed(tmp_path, monkeypatch):
    # A stray image survives the delete step and the add filter (both bypassed
    # here); the post-pack verification must still catch it, delete the tarball
    # and make the tool exit 1.
    monkeypatch.setattr(xu, "remove_unlisted", lambda out: None)
    monkeypatch.setattr(xu, "_packable", lambda p: p.is_file())
    monkeypatch.setattr(xu, "run_command", Fake())
    rc, _ = _quiet(xu.main, ["--out", str(tmp_path / "xu"), "--yes"])
    assert rc == 1 and not (tmp_path / "xu.tar.gz").exists()


def test_verify_tar_rejects_images(tmp_path, monkeypatch):
    d = tmp_path / "res"
    d.mkdir()
    (d / "summary.json").write_text("{}")
    (d / "x.tiff").write_bytes(b"x")
    (d / "x.cal.npz").write_bytes(b"x")
    tar = xu.pack(d)                                  # normal pack skips them
    with tarfile.open(tar) as t:
        assert t.getnames() == ["res/summary.json"]
    with tarfile.open(tar, "w:gz") as t:
        t.add(d, arcname="res")
    try:
        xu.verify_tar(tar, "res")
    except SystemExit:
        assert not tar.exists()
    else:
        raise AssertionError("expected SystemExit")


def test_remove_unlisted_keeps_allowlist_and_symlink_targets(tmp_path, monkeypatch):
    outside = tmp_path / "outside.tiff"
    outside.write_bytes(b"x")
    d = tmp_path / "res"
    d.mkdir()
    for n in ("summary.json", "doctor-before.json", "run-01-on-f1.diag.json", "a.tiff", "a.cal.npz"):
        (d / n).write_text("x")
    (d / "link.tiff").symlink_to(outside)
    xu.remove_unlisted(d)
    assert sorted(p.name for p in d.iterdir()) == ["doctor-before.json", "run-01-on-f1.diag.json", "summary.json"]
    assert outside.exists()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(xu, "run_command", fake)
    out = tmp_path / "xu"
    (rc, printed) = _quiet(xu.main, ["--out", str(out), "--dry-run"])
    assert rc == 0 and fake.calls == []
    assert not out.exists()                          # not even the directory
    assert list(tmp_path.iterdir()) == []
    assert "--cal-wait-scanstop enforce" in printed and "MOTOR" in printed
    # The real run with the same --out is therefore not refused.
    rc, out2 = _run(tmp_path, monkeypatch, fake)
    assert rc == 0 and (out2 / "plan.txt").is_file()


def test_dry_run_notes_a_used_out_dir(tmp_path, monkeypatch):
    (tmp_path / "xu").mkdir()
    (tmp_path / "xu" / "x").write_text("x")
    rc, printed = _quiet(xu.main, ["--out", str(tmp_path / "xu"), "--dry-run"])
    assert rc == 0 and "would refuse" in printed
    assert [p.name for p in (tmp_path / "xu").iterdir()] == ["x"]


def test_out_dir_must_be_empty(tmp_path, monkeypatch):
    (tmp_path / "xu").mkdir()
    (tmp_path / "xu" / "x").write_text("x")
    try:
        _run(tmp_path, monkeypatch, Fake())
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("expected SystemExit")


def test_refuses_without_a_terminal(tmp_path, monkeypatch):
    class NoTty:
        def isatty(self):
            return False

    fake = Fake()
    monkeypatch.setattr(xu, "run_command", fake)
    monkeypatch.setattr(sys, "stdin", NoTty())
    rc, _ = _quiet(xu.main, ["--out", str(tmp_path / "xu")])
    assert rc == 2 and fake.calls == [] and not (tmp_path / "xu").exists()


def test_plan_states_what_every_step_sends(tmp_path, monkeypatch):
    out = tmp_path / "xu"
    plan = xu.build_plan(out, xu.parse_sequence(xu.DEFAULT_SEQUENCE), 600, 1)
    text = xu.render_plan(plan, xu.parse_sequence(xu.DEFAULT_SEQUENCE), out, False)
    steps = re.split(r"\n(?=\d+\. )", text.split("Steps, in order:\n", 1)[1].split("\nScan phases")[0].strip())
    steps = [" ".join(st.split()) for st in steps]
    text = " ".join(text.split())
    assert len(steps) == len(plan) == 7
    for st in steps:
        for key in ("command:", "sends:", "you:", "for:"):
            assert key in st
    assert "[reads only]" in steps[0] and "[reads only]" in steps[-1]
    assert "No register writes, no motor" in steps[0]
    for st in steps[1:-1]:
        assert "[MOTOR]" in st
    assert "gate enforce" in steps[2] and "no extra write" in steps[2]
    assert "gate off" in steps[3] and "no extra transfer" in steps[3]
    # load is not silent: jog, prompt, load, and a real terminal.
    load = steps[1]
    assert "JOG done." in load and "FULLY OUT" in load and "real terminal" in load
    assert "jog" in load and "traverse" in load
    # phase list: the driver's own phase names, in order, each with a motor verdict
    names = ["- base_init ", "- prep, afe_base ", "- cal_dark_a", "- position ", "- scan ", "- park "]
    assert "cal_white" in text and "cal_shading_verify" in text
    phases = text.split("Scan phases", 1)[1]
    pos = [phases.index(n) for n in names]
    assert pos == sorted(pos)
    assert "may move the motor" in text and "[MOTOR]: moves the film holder" in text
    assert "carriage" in text and "power switch" in text
    assert "STOP RULE" in text and "new --out directory" in text


def test_plan_file_carries_the_same_statements(tmp_path, monkeypatch):
    rc, out = _run(tmp_path, monkeypatch, Fake())
    plan = (out / "plan.txt").read_text()
    assert "MOTOR" in plan and "sends:" in plan and "Scan phases" in plan
    assert str(out) not in plan                      # scrubbed like the other evidence files


def test_load_prompt_quote_matches_the_driver():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from of135i import loadflow
    norm = lambda t: " ".join(t.split())   # noqa: E731
    assert norm(xu.LOAD_PROMPT_QUOTE) == norm(loadflow.REINSERT_PROMPT)


def test_run_command_shows_a_prompt_before_the_answer():
    """A real child writes a prompt with input() and waits on stdin. The prompt
    has no trailing newline and the child's stdout is a pipe; it must reach our
    stdout before the answer is given, so the operator can see it."""
    child = "import sys\nprint('moving...')\nx = input('Press Enter when at the stop: ')\nprint('got', repr(x))\n"

    class Capture:
        def __init__(self):
            self.text = ""
            self.seen = threading.Event()

        def write(self, s):
            self.text += s
            if "Press Enter when at the stop: " in self.text:
                self.seen.set()
            return len(s)

        def flush(self):
            pass

    r, w = os.pipe()
    saved_fd0 = os.dup(0)
    saved_stdout = sys.stdout
    cap = Capture()
    result = {}
    try:
        os.dup2(r, 0)                      # the child inherits this as its stdin
        sys.stdout = cap
        t = threading.Thread(target=lambda: result.update(
            rc=xu.run_command([sys.executable, "-c", child])))
        t.start()
        assert cap.seen.wait(20), "the child's prompt did not become visible"
        assert t.is_alive(), "the child must still be waiting for the answer"
        os.write(w, b"\n")
        t.join(20)
        assert not t.is_alive()
    finally:
        sys.stdout = saved_stdout
        os.dup2(saved_fd0, 0)
        for fd in (r, w, saved_fd0):
            os.close(fd)
    rc, text = result["rc"]
    assert rc == 0 and "Press Enter when at the stop: " in text and "got ''" in text


def test_run_command_waits_for_the_child_on_interrupt():
    """Ctrl-C reaches the tool and the child alike: the tool re-raises with the
    partial output once the child has ended, and sends nothing itself."""
    child = ("import sys, time\nprint('partial', flush=True)\n"
             "time.sleep(0.3)\n")

    class Boom:
        calls = 0

        def write(self, s):
            Boom.calls += 1
            raise KeyboardInterrupt()

        def flush(self):
            pass

    saved = sys.stdout
    sys.stdout = Boom()
    try:
        xu.run_command([sys.executable, "-c", child])
    except KeyboardInterrupt as e:
        sys.stdout = saved
        assert "partial" in e.partial_output
    else:
        sys.stdout = saved
        raise AssertionError("expected KeyboardInterrupt")


def main() -> int:
    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and inspect.isfunction(f)]

    class MP:
        def __init__(self):
            self.undo = []

        def setattr(self, obj, name, val):
            self.undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

    for f in fns:
        params = inspect.signature(f).parameters
        with tempfile.TemporaryDirectory() as d:
            mp = MP()
            kw = {"tmp_path": pathlib.Path(d), "monkeypatch": mp}
            try:
                f(**{k: v for k, v in kw.items() if k in params})
            finally:
                for obj, name, old in reversed(mp.undo):
                    setattr(obj, name, old)
    print(f"{len(fns)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
