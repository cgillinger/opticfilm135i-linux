#!/usr/bin/env python3
"""Offline tests for tools/release_check.py's result parser and verdict.

No suite is run here: `classify` and `verdict` are pure functions, and the
end-to-end cases stub `run_suite`. The point being pinned (review of
2026-09-15): "1 tests passed, 1 skipped" and "0 tests passed" used to be
reported as PASS, so an incomplete run could be presented as full
verification. FULL VERIFICATION PASSED may only appear when every
mandatory test actually ran and passed.

Run with:
    .venv/bin/python tests/test_release_check.py
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import release_check as rc  # noqa: E402


# ------------------------------------------------------------- classify


def test_classify_normal_success():
    assert rc.classify(0, "\n7 tests passed.\n") == (rc.PASS, 7, 0)
    print("test_classify_normal_success OK")


def test_classify_mixed_passed_and_skipped_is_partial():
    """A mandatory suite with skipped tests is not fully verified."""
    assert rc.classify(0, "\n1 tests passed, 1 skipped.\n") == (rc.PARTIAL, 1, 1)
    assert rc.classify(0, "\n12 tests passed, 3 skipped.\n") == (rc.PARTIAL, 12, 3)
    print("test_classify_mixed_passed_and_skipped_is_partial OK")


def test_classify_entirely_skipped():
    assert rc.classify(0, "\n0 tests passed, 3 skipped.\n") == (rc.SKIP, 0, 3)
    print("test_classify_entirely_skipped OK")


def test_classify_zero_tests_is_never_a_pass():
    """Zero tests run and nothing skipped: an empty run proves nothing."""
    status, passed, skipped = rc.classify(0, "\n0 tests passed.\n")
    assert status == rc.FAIL and (passed, skipped) == (0, 0), (status, passed, skipped)
    print("test_classify_zero_tests_is_never_a_pass OK")


def test_classify_failures():
    # Non-zero exit wins even when the result line looks fine.
    assert rc.classify(1, "\n7 tests passed.\n") == (rc.FAIL, 7, 0)
    assert rc.classify(2, "Traceback ...") == (rc.FAIL, 0, 0)
    # A clean exit with no recognisable result line is a failure too.
    assert rc.classify(0, "") == (rc.FAIL, 0, 0)
    assert rc.classify(0, "all good\n") == (rc.FAIL, 0, 0)
    print("test_classify_failures OK")


# -------------------------------------------------------------- verdict


def _r(name, group, status, passed, skipped):
    return (name, group, status, passed, skipped)


ALL_PASS = [
    _r("test_a", "core", rc.PASS, 5, 0),
    _r("test_b", "compiler", rc.PASS, 3, 0),
    _r("test_c", "backend", rc.PASS, 2, 0),
]


def _verdict(results, **kw):
    kw.setdefault("no_backend", False)
    kw.setdefault("dirty", False)
    kw.setdefault("allow_dirty", False)
    code, lines = rc.verdict(results, **kw)
    return code, "\n".join(lines)


def test_verdict_full_verification_only_when_everything_ran_and_passed():
    code, text = _verdict(ALL_PASS)
    assert code == 0 and text.startswith("FULL VERIFICATION PASSED"), (code, text)
    assert "10 tests, 0 skipped" in text, text
    # --no-backend given but the backend suites ran in full anyway: still
    # full verification -- the verdict follows what ran, not the flag.
    code, text = _verdict(ALL_PASS, no_backend=True)
    assert code == 0 and text.startswith("FULL VERIFICATION PASSED"), (code, text)
    print("test_verdict_full_verification_only_when_everything_ran_and_passed OK")


def test_verdict_partial_suite_blocks_full_verification():
    for group in ("core", "compiler", "backend"):
        results = list(ALL_PASS)
        results[["core", "compiler", "backend"].index(group)] = \
            _r("test_x", group, rc.PARTIAL, 1, 1)
        code, text = _verdict(results)
        assert code == 1, (group, code, text)
        assert "PARTIAL" in text and "FULL VERIFICATION" not in text, (group, text)
        assert "ran 1 and skipped 1" in text, text
    print("test_verdict_partial_suite_blocks_full_verification OK")


def test_verdict_entirely_skipped_mandatory_suite_is_partial():
    results = list(ALL_PASS)
    results[1] = _r("test_b", "compiler", rc.SKIP, 0, 3)
    code, text = _verdict(results)
    assert code == 1 and "PARTIAL (compiler)" in text, (code, text)
    assert "skipped entirely" in text and "g++" in text, text
    assert "FULL VERIFICATION" not in text
    # Backend skipped WITHOUT --no-backend: mandatory, so partial.
    results = list(ALL_PASS)
    results[2] = _r("test_c", "backend", rc.SKIP, 0, 2)
    code, text = _verdict(results)
    assert code == 1 and "PARTIAL (backend)" in text, (code, text)
    print("test_verdict_entirely_skipped_mandatory_suite_is_partial OK")


def test_verdict_no_backend_excuses_only_the_backend_group():
    # The acknowledged limited run: backend skipped, everything else ran.
    results = list(ALL_PASS)
    results[2] = _r("test_c", "backend", rc.SKIP, 0, 2)
    code, text = _verdict(results, no_backend=True)
    assert code == 0 and text.startswith("LIMITED RUN PASSED"), (code, text)
    assert "NOT full verification" in text and "FULL VERIFICATION" not in text, text
    # A partial backend suite under --no-backend is likewise excused, but
    # named.
    results[2] = _r("test_c", "backend", rc.PARTIAL, 1, 1)
    code, text = _verdict(results, no_backend=True)
    assert code == 0 and "test_c ran 1 and skipped 1" in text, (code, text)
    # --no-backend must not hide a skip in another group.
    results = list(ALL_PASS)
    results[1] = _r("test_b", "compiler", rc.SKIP, 0, 3)
    results[2] = _r("test_c", "backend", rc.SKIP, 0, 2)
    code, text = _verdict(results, no_backend=True)
    assert code == 1 and "PARTIAL (compiler)" in text, (code, text)
    assert "LIMITED RUN PASSED" not in text and "FULL VERIFICATION" not in text
    results = list(ALL_PASS)
    results[0] = _r("test_a", "core", rc.PARTIAL, 4, 1)
    results[2] = _r("test_c", "backend", rc.SKIP, 0, 2)
    code, text = _verdict(results, no_backend=True)
    assert code == 1 and "PARTIAL (core)" in text, (code, text)
    print("test_verdict_no_backend_excuses_only_the_backend_group OK")


def test_verdict_failure_and_dirty_checkout():
    results = list(ALL_PASS)
    results[0] = _r("test_a", "core", rc.FAIL, 4, 0)
    code, text = _verdict(results)
    assert code == 1 and text.startswith("release check FAILED") and "test_a" in text, text
    # A failure plus an unexcused skip: both are named.
    results[1] = _r("test_b", "compiler", rc.SKIP, 0, 3)
    code, text = _verdict(results)
    assert code == 1 and "Also incomplete" in text and "test_b" in text, text
    # A zero-test suite classifies as FAIL and is reported as such.
    results = list(ALL_PASS)
    results[0] = (("test_a", "core") + rc.classify(0, "0 tests passed.\n"))
    code, text = _verdict(results)
    assert code == 1 and "FAILED" in text, (code, text)
    # Dirty checkout: fails unless allowed; never upgrades to FULL.
    code, text = _verdict(ALL_PASS, dirty=True)
    assert code == 1 and "uncommitted" in text, (code, text)
    code, text = _verdict(ALL_PASS, dirty=True, allow_dirty=True)
    assert code == 0 and text.startswith("FULL VERIFICATION PASSED"), (code, text)
    print("test_verdict_failure_and_dirty_checkout OK")


# ----------------------------------------------------------- end to end


def _main_with(canned: dict, argv: list[str]) -> tuple[int, str]:
    """Run main() with run_suite stubbed: `canned` maps suite name ->
    (returncode, output); unnamed suites report a clean pass."""
    real_run, real_suites = rc.run_suite, rc.SUITES
    rc.SUITES = tuple((n, g) for n, g in real_suites if n in canned) or real_suites

    def fake_run(name):
        code, out = canned.get(name, (0, "3 tests passed.\n"))
        return (*rc.classify(code, out), out)

    rc.run_suite = fake_run
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = rc.main(argv)
    finally:
        rc.run_suite, rc.SUITES = real_run, real_suites
    return code, buf.getvalue()


def test_main_reports_counts_and_exit_code():
    argv = ["--allow-dirty"]
    # Normal success across a core, a compiler and a backend suite.
    code, out = _main_with({"test_safety": (0, "9 tests passed.\n"),
                            "test_sane_ops": (0, "4 tests passed.\n"),
                            "test_sane_magazine": (0, "2 tests passed.\n")}, argv)
    assert code == 0 and "FULL VERIFICATION PASSED" in out, out
    assert "total passed                         15" in out, out
    assert "total skipped                        0" in out, out
    # The two mis-classified outputs from the review.
    code, out = _main_with({"test_safety": (0, "1 tests passed, 1 skipped.\n"),
                            "test_sane_ops": (0, "4 tests passed.\n")}, argv)
    assert code == 1 and "PARTIAL" in out and "FULL VERIFICATION" not in out, out
    assert "test_safety                  PARTIAL   1 passed, 1 skipped" in out, out
    code, out = _main_with({"test_safety": (0, "0 tests passed.\n")}, argv)
    assert code == 1 and "ran no tests" in out and "FAILED" in out, out
    # Entirely skipped backend suite: partial by default, limited with the flag.
    canned = {"test_safety": (0, "9 tests passed.\n"),
              "test_sane_magazine": (0, "0 tests passed, 14 skipped.\n")}
    code, out = _main_with(canned, argv)
    assert code == 1 and "PARTIAL (backend)" in out, out
    assert "total skipped                        14" in out, out
    code, out = _main_with(canned, argv + ["--no-backend"])
    assert code == 0 and "LIMITED RUN PASSED" in out and "FULL VERIFICATION" not in out, out
    # A failure prints the suite's tail and exits 1.
    code, out = _main_with({"test_safety": (1, "boom\nTraceback\n")}, argv)
    assert code == 1 and "FAILED" in out and "Traceback" in out, out
    print("test_main_reports_counts_and_exit_code OK")


def main() -> int:
    tests = [
        test_classify_normal_success,
        test_classify_mixed_passed_and_skipped_is_partial,
        test_classify_entirely_skipped,
        test_classify_zero_tests_is_never_a_pass,
        test_classify_failures,
        test_verdict_full_verification_only_when_everything_ran_and_passed,
        test_verdict_partial_suite_blocks_full_verification,
        test_verdict_entirely_skipped_mandatory_suite_is_partial,
        test_verdict_no_backend_excuses_only_the_backend_group,
        test_verdict_failure_and_dirty_checkout,
        test_main_reports_counts_and_exit_code,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
