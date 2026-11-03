"""Offline tests for tools/cross_unit_check.py (subprocess mocked, no scanner)."""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import cross_unit_check as xu  # noqa: E402


class Fake:
    """Replacement for xu.run_command. Records argv; `fail` maps the 1-based
    index of a scan (or 'load') to an exit code."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}
        self.scans = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        sub = [a for a in argv[3:] if not a.startswith("-")][0]
        if sub == "scan":
            self.scans += 1
            img = pathlib.Path(argv[argv.index("-o") + 1])
            rc = self.fail.get(self.scans, 0)
            img.write_bytes(b"II*\x00")
            img.with_suffix(".diag.json").write_text(json.dumps({
                "output": str(img), "calibration": {"ok": True}, "dark_b_substituted": False,
                "poll_timeouts": 0, "cr_mismatches": 0, "cal_scanstop_waits": [{"ms": 3}],
                "phase_seconds": {"cal": 1.0}, "unrelated": 1}))
            return rc, "scan output\n"
        if sub == "load":
            return self.fail.get("load", 0), "load output\n"
        return 0, "doctor output\n"


def _run(tmp_path, monkeypatch, fake, *extra):
    monkeypatch.setattr(xu, "run_command", fake)
    out = tmp_path / "xu"
    rc = xu.main(["--out", str(out), "--yes", *extra])
    return rc, out.resolve()


def _subs(fake):
    return [[a for a in c[3:] if not a.startswith("-")][0] for c in fake.calls]


def test_default_sequence_argv(tmp_path, monkeypatch):
    fake = Fake()
    rc, out = _run(tmp_path, monkeypatch, fake)
    assert rc == 0
    py = [sys.executable, "-m", "of135i"]
    exp = [py + ["doctor", "--json", str(out / "doctor-before.json")], py + ["load"]]
    for i, x in enumerate(["off", "enforce", "enforce", "off"], 1):
        exp.append(py + ["-v", "scan", "--frames", "1", "--dpi", "600", "--recalibrate",
                         "--cal-wait-scanstop", x, "-o", str(out / f"run-{i:02d}-{x}.tiff")])
    exp.append(py + ["doctor", "--json", str(out / "doctor-after.json")])
    assert fake.calls == exp
    s = json.loads((out / "summary.json").read_text())
    assert s["sequence"] == ["off", "on", "on", "off"] and "stopped_at" not in s
    assert s["runs"][0]["sidecar"]["output"] == "run-01-off.tiff"   # relative, no personal path
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
    assert s["stopped_at"] == 2 and len(s["runs"]) == 2
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


def test_images_deleted_unless_included(tmp_path, monkeypatch):
    rc, out = _run(tmp_path, monkeypatch, Fake())
    assert not list(out.glob("*.tiff")) and len(list(out.glob("*.diag.json"))) == 4
    with tarfile.open(out.parent / "xu.tar.gz") as t:
        assert not [n for n in t.getnames() if n.endswith(".tiff")]
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(xu, "run_command", Fake())
        out2 = pathlib.Path(d) / "xu2"
        assert xu.main(["--out", str(out2), "--yes", "--include-images"]) == 0
        assert len(list(out2.glob("*.tiff"))) == 4


def test_dry_run_runs_nothing(tmp_path, monkeypatch):
    fake = Fake()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc, out = _run(tmp_path, monkeypatch, fake, "--dry-run")
    assert rc == 0 and fake.calls == []
    assert [p.name for p in out.iterdir()] == ["plan.txt"]
    assert not (out.parent / "xu.tar.gz").exists()
    assert "--cal-wait-scanstop enforce" in buf.getvalue()


def test_out_dir_must_be_empty(tmp_path, monkeypatch):
    (tmp_path / "xu").mkdir()
    (tmp_path / "xu" / "x").write_text("x")
    try:
        _run(tmp_path, monkeypatch, Fake())
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("expected SystemExit")


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
            f(**{k: v for k, v in kw.items() if k in params})
            for obj, name, old in reversed(mp.undo):
                setattr(obj, name, old)
    print(f"{len(fns)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
