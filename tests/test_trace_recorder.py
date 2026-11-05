#!/usr/bin/env python3
"""Offline transfer recorder/converter tests; stdlib runner, no pytest.

Adapted from bearyjd's a02b4b6 and 6c2be93. USB is always FakeUsbDevice.
The upstream calibration suite supplies pre-existing wire digests.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import gzip
import io
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tests"), str(REPO / "tools")]

import usb.core
from of135i import safety, trace
from of135i.usbio import UsbIo
from test_safety import FakeUsbDevice, _cal_buffers, fast_time, make_scanner
from test_cal_wait_scanstop import BASELINE, _baseline_run, _digest
import trace_to_compiled as converter
import replay_trace


@contextmanager
def broken_logging():
    class Handler(logging.Handler):
        calls = 0
        def emit(self, record):
            self.calls += 1
            raise RuntimeError("log sink failed")

    logger = logging.getLogger("of135i")
    saved = logger.handlers, logger.level, logger.propagate
    handler = Handler()
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.WARNING)
    try:
        yield handler
    finally:
        logger.handlers, _, logger.propagate = saved
        logger.setLevel(saved[1])


HEADER = json.dumps({"format": trace.FORMAT}) + "\n"


def bo_line(data="6162", **extra) -> str:
    rec = dict(seq=1, t=0.5, dur=0.01, kind="bulk", dir="out", status="ok", ep=2,
               len=len(data) // 2, data=data)
    rec.update(extra)
    return json.dumps(rec) + "\n"


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "run.jsonl"
        env = patch.dict(os.environ, {trace.TRACE_ENV: str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def device(self, fake=None):
        fake = fake or FakeUsbDevice()
        transport = UsbIo(fake)
        transport.session.arm(0x22)
        self.addCleanup(transport.close)
        return transport.dev, fake

    def records(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def transfers(self):
        return [r for r in self.records() if "kind" in r]

    def convert_file(self, content, *args, name="out.trace.json"):
        """Run the converter CLI on ``content``: (exit code, stderr, out path)."""
        src, out = self.root / "in.jsonl", self.root / name
        src.write_bytes(content if isinstance(content, bytes) else content.encode())
        err = io.StringIO()
        with redirect_stderr(err):
            code = converter.main([str(src), "-o", str(out), *args])
        return code, err.getvalue(), out

    def test_records_scripted_transfers(self):
        dev, fake = self.device()
        dev.ctrl_transfer(0x40, 4, 0x83, 0, b"\x03\x30")
        got = bytes(dev.ctrl_transfer(0xc0, 4, 0x8e, 0x122, 2))
        dev.write(2, b"abc", timeout=123)
        dev.read(0x81, 100, timeout=456)
        cw, cr, bo, bi = self.transfers()
        self.assertEqual(self.records()[0], {"format": trace.FORMAT})
        self.assertEqual([(r["kind"], r["dir"]) for r in (cw, cr, bo, bi)],
                         [("ctrl", "out"), ("ctrl", "in"), ("bulk", "out"), ("bulk", "in")])
        self.assertEqual((cw["bm"], cw["br"], cw["wv"], cw["wi"], cw["wlen"]),
                         (0x40, 4, 0x83, 0, 2))
        self.assertEqual(cw["data"], "0330")
        self.assertEqual(cr["data"], got.hex())
        self.assertEqual((bo["ep"], bo["data"], bo["len"]), (2, "616263", 3))
        self.assertEqual((bi["ep"], bi["len"]), (0x81, 100))
        self.assertEqual([r["seq"] for r in self.transfers()], [1, 2, 3, 4])
        self.assertEqual(len(fake.wire_log), 4)
        stamps = [r["t"] for r in self.transfers()]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(r["dur"] >= 0 and r["status"] == "ok" for r in self.transfers()))

    def test_bulk_in_image_data_never_stored(self):
        image = b"IMAGEDATA" * 16
        fake = FakeUsbDevice()
        def read_into(endpoint, size_or_buffer, timeout=None):
            if isinstance(size_or_buffer, int):
                return image
            size_or_buffer[:] = image
            return len(image)
        with patch.object(fake, "read", side_effect=read_into):
            dev, _ = self.device(fake)
            self.assertEqual(dev.read(0x81, len(image)), image)
            self.assertEqual(dev.read(0x81, bytearray(len(image))), len(image))
        raw = self.path.read_bytes()
        self.assertEqual([r["len"] for r in self.transfers()], [len(image)] * 2)
        self.assertTrue(all("data" not in r for r in self.transfers()))
        for needle in (b"IMAGEDATA", image[:8].hex().encode(), b"SU1BR0VEQVRB"):
            self.assertNotIn(needle, raw)

    def test_bulk_in_length_does_not_copy_buffer(self):
        class Buffer:
            def __len__(self):
                return 100
            def __bytes__(self):
                raise AssertionError("image buffer copied")
        rec = trace.TraceRecorder(str(self.path))
        self.addCleanup(rec.close)
        buf = Buffer()
        self.assertIs(rec.run("bulk", "in", 0x81, None, None, lambda: buf), buf)
        self.assertEqual(self.transfers()[0]["len"], 100)

    def test_buffer_read_integer_result_records_actual_length(self):
        rec = trace.TraceRecorder(str(self.path))
        self.addCleanup(rec.close)
        self.assertEqual(rec.run("bulk", "in", 0x81, None, None, lambda: 17), 17)
        self.assertEqual(self.transfers()[0]["len"], 17)

    def test_payload_cap_and_full_length(self):
        dev, _ = self.device()
        big = bytes(range(200))
        dev.write(2, big)
        dev.ctrl_transfer(0x40, 4, 0x83, 0, big[:128])
        bo, cw = self.transfers()
        self.assertEqual(bo["data"], big[:64].hex())
        self.assertEqual(bo["len"], 200)
        self.assertEqual((cw["data"], cw["wlen"]), (big[:64].hex(), 128))

    def test_control_in_payload_cap(self):
        dev, _ = self.device()
        dev.ctrl_transfer(0xc0, 9, 0, 0, 200)
        rec = self.transfers()[0]
        self.assertEqual(rec["len"], 200)
        self.assertEqual(len(bytes.fromhex(rec["data"])), 64)

    def test_failed_transfer_preserves_exception_identity(self):
        error = usb.core.USBTimeoutError("boom", -7, 110)
        dev, _ = self.device(FakeUsbDevice(fault=lambda ev: error))
        with self.assertRaises(usb.core.USBTimeoutError) as caught:
            dev.ctrl_transfer(0xc0, 4, 0x8e, 0x122, 2)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.transfers()[0]["status"], "error:USBTimeoutError")

    def test_keyboard_interrupt_leaves_valid_prefix(self):
        seen = 0
        def fault(ev):
            nonlocal seen
            seen += 1
            if seen == 40:
                return KeyboardInterrupt()
        fake = FakeUsbDevice(fault=fault, cal_buffers=_cal_buffers())
        with fast_time(), self.assertRaises(KeyboardInterrupt):
            scanner = make_scanner(fake)
            self.addCleanup(scanner.io.close)
            scanner.initialize()
            scanner.scan(frame=1)
        records = self.transfers()
        self.assertEqual(len(fake.wire_log), 39)
        self.assertEqual(len(records), 40)
        self.assertEqual([r["status"] for r in records[:39]], ["ok"] * 39)
        self.assertEqual(records[-1]["status"], "error:KeyboardInterrupt")
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))

    def interrupting_write(self):
        """os.write that lands 5 bytes of a record, then raises Ctrl-C."""
        real_write, calls = os.write, []
        def write(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return real_write(fd, bytes(data[:5]))
            raise KeyboardInterrupt()
        return write

    def test_interrupt_in_trace_write_keeps_original_usb_error(self):
        original = usb.core.USBTimeoutError("original", -7, 110)
        dev, _ = self.device(FakeUsbDevice(
            fault=lambda ev: original if ev["kind"] == "ctrl_in" else None))
        header = self.path.read_bytes()
        with patch.object(trace.os, "write", side_effect=self.interrupting_write()):
            with self.assertRaises(usb.core.USBTimeoutError) as caught:
                dev.ctrl_transfer(0xc0, 4, 0x8e, 0x122, 2)
        self.assertIs(caught.exception, original)
        self.assertFalse(dev._recorder.active)
        dev.write(2, b"later")
        text = self.path.read_bytes()
        self.assertEqual(text[:len(header)], header)
        self.assertEqual(len(text), len(header) + 5)
        self.assertEqual(converter.read_records(text.decode()), [])

    def test_interrupt_in_trace_write_after_success_propagates(self):
        dev, fake = self.device()
        with patch.object(trace.os, "write", side_effect=self.interrupting_write()):
            with self.assertRaises(KeyboardInterrupt):
                dev.write(2, b"abc")
        self.assertEqual(len(fake.wire_log), 1)
        self.assertFalse(dev._recorder.active)
        self.assertEqual(dev._session.state, safety.SessionState.FAILED)

    def test_file_is_private_with_open_umask(self):
        old = os.umask(0)
        try:
            self.device()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_existing_file_refused_once_and_io_continues(self):
        self.path.write_bytes(b"precious\n")
        with self.assertLogs("of135i", logging.WARNING) as logs:
            dev, fake = self.device()
            dev.write(2, b"abc")
            dev.write(2, b"def")
        self.assertEqual(self.path.read_bytes(), b"precious\n")
        self.assertEqual(len(fake.wire_log), 2)
        self.assertEqual(len(logs.records), 1)

    def test_second_open_in_process_runs_unrecorded(self):
        first, _ = self.device()
        with self.assertLogs("of135i", logging.WARNING) as logs:
            second, fake = self.device()
            second.write(2, b"abc")
        self.assertTrue(first._recorder.active)
        self.assertIsNone(second._recorder)
        self.assertEqual((len(logs.records), len(fake.wire_log)), (1, 1))

    def test_symlink_refused(self):
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        self.path.symlink_to(victim)
        with self.assertLogs("of135i", logging.WARNING):
            dev, _ = self.device()
            dev.write(2, b"abc")
        self.assertEqual(victim.read_bytes(), b"keep")

    def test_bad_path_warns_once(self):
        with patch.dict(os.environ, {trace.TRACE_ENV: str(self.root / "missing" / "t")}), \
                self.assertLogs("of135i", logging.WARNING) as logs:
            dev, fake = self.device()
            dev.write(2, b"abc")
        self.assertEqual(len(fake.wire_log), 1)
        self.assertEqual(len(logs.records), 1)

    def test_write_error_disables_recording_once(self):
        dev, fake = self.device()
        dev.write(2, b"one")
        with patch.object(trace.os, "write", side_effect=OSError("disk full")) as write, \
                self.assertLogs("of135i", logging.WARNING) as logs:
            self.assertEqual(dev.write(2, b"two"), 3)
            self.assertEqual(dev.write(2, b"three"), 5)
            self.assertEqual(len(dev.read(0x81, 8)), 8)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(len(fake.wire_log), 4)
        self.assertEqual(len(self.transfers()), 1)

    def test_zero_write_disables_instead_of_looping(self):
        dev, _ = self.device()
        with patch.object(trace.os, "write", return_value=0) as write, \
                self.assertLogs("of135i", logging.WARNING):
            self.assertEqual(dev.write(2, b"abc"), 3)
        self.assertEqual(write.call_count, 1)
        self.assertFalse(dev._recorder.active)

    def test_partial_writes_complete_the_line(self):
        dev, _ = self.device()
        real_write = os.write
        def short_write(fd, data):
            return real_write(fd, data[:7])
        with patch.object(trace.os, "write", side_effect=short_write):
            dev.write(2, b"abc")
        self.assertEqual(self.transfers()[0]["data"], "616263")

    def test_encoding_error_never_reaches_io(self):
        dev, fake = self.device()
        with patch.object(trace.json, "dumps", side_effect=ValueError("encode")), \
                self.assertLogs("of135i", logging.WARNING):
            self.assertEqual(dev.write(2, b"abc"), 3)
        self.assertEqual(len(fake.wire_log), 1)

    def test_logging_failure_preserves_success_and_closes_trace(self):
        dev, fake = self.device()
        fd = dev._recorder._fd
        with broken_logging() as handler, \
                patch.object(trace.os, "write", side_effect=OSError("disk full")):
            self.assertEqual(dev.write(2, b"abc"), 3)
            self.assertEqual(dev.write(2, b"def"), 3)
        self.assertEqual(handler.calls, 1)
        self.assertEqual(len(fake.wire_log), 2)
        self.assertFalse(dev._recorder.active)
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_logging_failure_preserves_original_usb_exception(self):
        original = usb.core.USBTimeoutError("original USB failure", -7, 110)
        dev, _ = self.device(FakeUsbDevice(fault=lambda ev: original))
        with broken_logging() as handler, \
                patch.object(trace.os, "write", side_effect=OSError("disk full")):
            with self.assertRaises(usb.core.USBTimeoutError) as caught:
                dev.ctrl_transfer(0xc0, 4, 0x8e, 0x122, 2)
        self.assertIs(caught.exception, original)
        self.assertEqual(handler.calls, 1)
        self.assertFalse(dev._recorder.active)

    def test_logging_failure_on_open_error_still_disables_trace(self):
        self.path.write_bytes(b"precious")
        with broken_logging() as handler:
            self.assertIsNone(trace.from_env())
        self.assertEqual(handler.calls, 1)
        self.assertEqual(self.path.read_bytes(), b"precious")

    def test_clock_failure_never_prevents_transfer(self):
        dev, fake = self.device()
        with patch.object(trace.time, "perf_counter", side_effect=OSError("clock")), \
                self.assertLogs("of135i", logging.WARNING):
            self.assertEqual(dev.write(2, b"abc"), 3)
        self.assertEqual(len(fake.wire_log), 1)

    def test_initial_clock_failure_closes_file(self):
        real_close = os.close
        with patch.object(trace.time, "perf_counter", side_effect=OSError("clock")), \
                patch.object(trace.os, "close", wraps=real_close) as close, \
                self.assertLogs("of135i", logging.WARNING) as logs:
            self.assertIsNone(trace.from_env())
        self.assertEqual(close.call_count, 1)
        self.assertEqual(len(logs.records), 1)

    def test_close_releases_file(self):
        dev, _ = self.device()
        fd = dev._recorder._fd
        dev.dispose()
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_unset_and_empty_env_are_silent_and_clock_free(self):
        with patch.object(trace.time, "perf_counter", side_effect=AssertionError("clock")), \
                patch.object(trace.os, "open", side_effect=AssertionError("open")), \
                patch.object(trace.log, "warning") as warning:
            for value in (None, ""):
                if value is None:
                    os.environ.pop(trace.TRACE_ENV, None)
                else:
                    os.environ[trace.TRACE_ENV] = value
                dev, _ = self.device()
                self.assertIsNone(dev._recorder)
                self.assertEqual(dev.write(2, b"abc"), 3)
        warning.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_recorder_does_not_touch_poll_clock_or_sleep(self):
        dev, _ = self.device()
        with patch.object(time, "monotonic", side_effect=AssertionError("poll clock")), \
                patch.object(time, "sleep", side_effect=AssertionError("sleep")):
            dev.write(2, b"abc")
            dev.read(0x81, 8)
        self.assertEqual(len(self.transfers()), 2)

    def test_recording_on_matches_upstream_wire_baselines(self):
        for name in sorted(BASELINE):
            with self.subTest(name=name):
                self.path = self.root / (name + ".jsonl")
                os.environ[trace.TRACE_ENV] = str(self.path)
                fake, scanner = _baseline_run(name, None)
                scanner.io.close()
                self.assertEqual((_digest(fake.wire_log), len(fake.wire_log)), BASELINE[name])
                self.assertEqual(len([r for r in self.transfers() if r["kind"] != "intr"]),
                                 BASELINE[name][1])

    def test_recording_off_matches_upstream_wire_baselines(self):
        os.environ.pop(trace.TRACE_ENV, None)
        for name in sorted(BASELINE):
            with self.subTest(name=name):
                fake, scanner = _baseline_run(name, None)
                scanner.io.close()
                self.assertEqual((_digest(fake.wire_log), len(fake.wire_log)), BASELINE[name])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_converter_ops_and_unpadded_long_bulk_payload(self):
        dev, _ = self.device()
        dev.ctrl_transfer(0x40, 4, 0x83, 0, b"\x03\x30")
        dev.ctrl_transfer(0xc0, 4, 0x8e, 0x122, 2)
        dev.write(2, bytes(range(100)))
        dev.read(0x81, 4096)
        dev.write(2, bytes(range(64)))
        ops, skipped = converter.convert(converter.read_records(self.path.read_text()))
        self.assertEqual(skipped, 0)
        self.assertEqual([o["t"] for o in ops], ["cw", "cr", "bo", "bi", "bo"])
        self.assertEqual(ops[0]["data"], "0330")
        self.assertEqual(ops[1]["resp"], "2255")
        self.assertEqual((ops[2]["data"], ops[2]["len"], ops[2]["partial"]), (None, 100, True))
        self.assertEqual(ops[3]["len"], 4096)
        self.assertEqual(ops[4]["data"], bytes(range(64)).hex())
        self.assertNotIn("partial", ops[4])
        self.assertTrue(all(o["dt"] >= 0 for o in ops))

    def test_converter_never_emits_truncated_control_payloads(self):
        dev, _ = self.device()
        dev.ctrl_transfer(0x40, 4, 0x83, 0, bytes(range(128)))
        dev.ctrl_transfer(0xc0, 9, 0, 0, 200)
        cw, cr = converter.convert(converter.read_records(self.path.read_text()))[0]
        self.assertEqual((cw["t"], cw["data"], cw["len"], cw["partial"]), ("cw", None, 128, True))
        self.assertEqual((cr["t"], cr["resp"], cr["len"], cr["partial"]), ("cr", None, 200, True))

    def test_converter_skips_errors_interrupt_and_partial_line(self):
        error = usb.core.USBTimeoutError("t", -7, 110)
        dev, _ = self.device(FakeUsbDevice(fault=lambda ev: error if ev["kind"] == "bulk_in" else None))
        dev.write(2, b"ab")
        for endpoint in (0x81, 0x83):
            with self.assertRaises(usb.core.USBTimeoutError):
                dev.read(endpoint, 8)
        text = self.path.read_text() + '{"seq":9,"kind":"ct'
        ops, skipped = converter.convert(converter.read_records(text))
        self.assertEqual([o["t"] for o in ops], ["bo"])
        self.assertEqual(skipped, 2)

    def test_short_out_fails_session_and_converter_skips(self):
        for kind in ("bulk_out", "ctrl_out"):
            with self.subTest(kind=kind):
                self.path = self.root / (kind + ".jsonl")
                os.environ[trace.TRACE_ENV] = str(self.path)
                dev, fake = self.device(FakeUsbDevice(fault=lambda ev: 2 if ev["kind"] == kind else None))
                with self.assertRaises(safety.ShortTransferError):
                    if kind == "bulk_out":
                        dev.write(2, b"abcdef")
                    else:
                        dev.ctrl_transfer(0x40, 4, 0x83, 0, b"abcdef")
                self.assertEqual(self.transfers()[0]["status"], "short:2")
                self.assertEqual(self.transfers()[0]["len"], 6)
                self.assertEqual(converter.convert(self.transfers()), ([], 1))
                with self.assertRaises(safety.SessionFailedError):
                    dev.write(2, b"more")
                self.assertEqual(len(self.transfers()), 1)

    def test_converter_cli_private_plain_and_gzip_refuses_overwrite(self):
        dev, _ = self.device()
        dev.write(2, b"ab")
        old = os.umask(0)
        try:
            for name in ("out.trace.json", "out.trace.json.gz"):
                out = self.root / name
                self.assertEqual(converter.main([str(self.path), "-o", str(out)]), 0)
                self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
                opener = gzip.open if out.suffix == ".gz" else open
                with opener(out, "rt") as handle:
                    self.assertEqual(json.load(handle)[0]["t"], "bo")
                before = out.read_bytes()
                self.assertEqual(converter.main([str(self.path), "-o", str(out)]), 1)
                self.assertEqual(out.read_bytes(), before)
        finally:
            os.umask(old)

    def test_short_control_in_preserves_reply_and_converter_skips(self):
        reply = b"\x55"
        fake = FakeUsbDevice()
        with patch.object(fake, "ctrl_transfer", return_value=reply):
            dev, _ = self.device(fake)
            self.assertIs(dev.ctrl_transfer(0xc0, 4, 0x8e, 0x122, 2), reply)
        record = self.transfers()[0]
        self.assertEqual((record["status"], record["len"], record["data"]),
                         ("short:1", 1, "55"))
        self.assertEqual(converter.convert([record]), ([], 1))

    def test_short_bulk_in_retains_actual_length(self):
        fake = FakeUsbDevice()
        with patch.object(fake, "read", return_value=b"abc"):
            dev, _ = self.device(fake)
            self.assertEqual(dev.read(0x81, 100), b"abc")
        record = self.transfers()[0]
        self.assertEqual((record["status"], record["len"], record["want"]),
                         ("short:3", 3, 100))
        ops, skipped = converter.convert([record])
        self.assertEqual((ops[0]["t"], ops[0]["len"], ops[0]["want"], skipped),
                         ("bi", 3, 100, 0))

    def test_reads_record_requested_size(self):
        dev, fake = self.device()
        fake.button_events.append(0x05)
        dev.read(0x81, 100)
        dev.read(0x83, 1)
        bi, intr = self.transfers()
        self.assertEqual((bi["status"], bi["len"], bi["want"]), ("ok", 100, 100))
        self.assertEqual((intr["kind"], intr["status"], intr["want"]), ("intr", "ok", 1))

    def test_buffer_read_records_buffer_size_as_want(self):
        rec = trace.TraceRecorder(str(self.path))
        self.addCleanup(rec.close)
        buf = bytearray(32)
        self.assertEqual(rec.run("bulk", "in", 0x81, None, None, lambda: 20, size=buf), 20)
        self.assertEqual((self.transfers()[0]["want"], self.transfers()[0]["status"]),
                         (32, "short:20"))

    def test_bulk_out_payload_sliced_before_copy(self):
        class Payload:
            def __len__(self):
                return 1000
            def __getitem__(self, item):
                return bytes(range(256))[:64] if item == slice(None, trace.DATA_CAP) else None
            def __bytes__(self):
                raise AssertionError("whole payload copied")
        rec = trace.TraceRecorder(str(self.path))
        self.addCleanup(rec.close)
        rec.run("bulk", "out", 2, None, Payload(), lambda: 1000)
        record = self.transfers()[0]
        self.assertEqual((record["len"], record["status"]), (1000, "ok"))
        self.assertEqual(record["data"], bytes(range(64)).hex())

    def test_converter_refuses_symlink(self):
        dev, _ = self.device()
        dev.write(2, b"ab")
        out = self.root / "link.trace.json"
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        out.symlink_to(victim)
        self.assertEqual(converter.main([str(self.path), "-o", str(out)]), 1)
        self.assertEqual(victim.read_bytes(), b"keep")

    def test_converter_timestamps_follow_compile_trace(self):
        # compile_trace.py: cw/cr/bo stamped at submission, bi at completion,
        # dt from the previous op, first dt 0.0, and cr carries no dur.
        ctrl = dict(kind="ctrl", status="ok", br=4, wi=0, wlen=2, len=2)
        records = [dict(ctrl, t=.25, dur=.1, dir="out", bm=0x40, wv=0x83, data="0330"),
                   dict(ctrl, t=1., dur=.2, dir="in", bm=0xc0, wv=0x8e, data="2255"),
                   dict(t=2., dur=.5, kind="bulk", dir="in", status="ok", len=8),
                   dict(t=3., dur=.1, kind="bulk", dir="out", status="ok", len=1, data="00")]
        ops, skipped = converter.convert(records)
        self.assertEqual([o["t"] for o in ops], ["cw", "cr", "bi", "bo"])
        self.assertEqual([o["dt"] for o in ops], [0.0, 0.75, 1.5, 0.5])
        self.assertNotIn("dur", ops[1])
        self.assertEqual(skipped, 0)

    def test_converter_format_matches_recorder(self):
        self.assertEqual(converter.FORMAT, trace.FORMAT)
        self.assertEqual(converter.DATA_CAP, trace.DATA_CAP)

    def test_converter_reads_past_damaged_middle_line(self):
        text = HEADER + bo_line() + "garbage\n" + bo_line("6364") + '{"kind":"bu'
        records, problems, partial = converter.scan_records(text)
        self.assertEqual([r["data"] for r in records], ["6162", "6364"])
        self.assertEqual((len(problems), partial), (1, True))
        self.assertIn("line 3", problems[0])

    def test_converter_cli_refuses_damaged_middle_line_unless_asked(self):
        text = HEADER + bo_line() + "garbage\n" + bo_line("6364")
        code, err, out = self.convert_file(text)
        self.assertEqual(code, 1)
        self.assertIn("1 damaged", err)
        self.assertFalse(out.exists())
        code, err, out = self.convert_file(text, "--skip-damaged")
        self.assertEqual(code, 0)
        self.assertIn("1 damaged", err)
        self.assertEqual(len(json.loads(out.read_text())), 2)

    def test_converter_cli_accepts_partial_final_line(self):
        code, _, out = self.convert_file(HEADER + bo_line() + '{"seq":2,"ki')
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.read_text())), 1)

    def test_converter_skips_and_counts_malformed_records(self):
        bad = [bo_line(t="x"), bo_line(t=float("nan")), bo_line(len=True),
               bo_line(data="zz"), bo_line(kind="usb"), bo_line(dir="up"),
               json.dumps({"seq": 3, "t": 1.0, "kind": "ctrl", "dir": "in",
                           "status": "ok", "len": 2, "data": "2255"}) + "\n",
               '[1, 2]\n', bo_line(status=7)]
        for line in bad:
            with self.subTest(line=line):
                text = HEADER + bo_line() + line + bo_line("6364")
                records, problems, partial = converter.scan_records(text)
                self.assertEqual(len(records), 2)
                self.assertEqual((len(problems), partial), (1, False))
                self.assertEqual(self.convert_file(text, name="o.json")[0], 1)
                (self.root / "o.json").unlink(missing_ok=True)

    def test_converter_requires_data_matching_recorded_length(self):
        long = bytes(range(100))
        bad = [bo_line("aabbccdd", len=2), bo_line("aa", len=2), bo_line(" 6162", len=2),
               bo_line("61 62", len=2), bo_line("6162\n", len=2),
               bo_line(long[:65].hex(), len=100), bo_line(long[:63].hex(), len=100),
               bo_line("", len=1)]
        for line in bad:
            with self.subTest(line=line):
                records, problems, _ = converter.scan_records(HEADER + bo_line() + line + bo_line())
                self.assertEqual((len(records), len(problems)), (2, 1))
        good = [bo_line("", len=0), bo_line(long[:64].hex(), len=100), bo_line("6162", len=2)]
        records, problems, _ = converter.scan_records(HEADER + "".join(good) + bo_line())
        self.assertEqual((len(records), problems), (4, []))

    def test_converter_counts_undecodable_line(self):
        data = HEADER.encode() + bo_line().encode() + b"\xff\xfe\n" + bo_line().encode()
        records, problems, _ = converter.scan_records(data)
        self.assertEqual((len(records), len(problems)), (2, 1))

    def test_converter_requires_format_header(self):
        for content in ("", bo_line(), '{"format":"other/9"}\n' + bo_line(),
                        b"\xff\n", "not json\n"):
            with self.subTest(content=content):
                code, err, out = self.convert_file(content)
                self.assertEqual(code, 2)
                self.assertIn("OF135I_TRACE", err)
                self.assertFalse(out.exists())

    def replay(self, ops, *args):
        """replay_trace.main() on a .gz of ``ops``; opening the device fails the test."""
        src = self.root / "replay.trace.json.gz"
        with gzip.open(src, "wt") as handle:
            json.dump(ops, handle)
        argv = ["replay_trace.py", str(src), "--out", str(self.root / "replay-out"), *args]
        with patch.object(replay_trace.UsbIo, "open", side_effect=AssertionError("opened")), \
                patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()):
            try:
                return replay_trace.main()
            except SystemExit as e:
                return e.code

    def test_replay_refuses_converted_partial_payloads(self):
        dev, _ = self.device()
        dev.write(2, bytes(range(100)))
        ops, _ = converter.convert(converter.read_records(self.path.read_text()))
        code = self.replay(ops)
        self.assertIn("refused", code)
        self.assertIn("partial", code)
        self.assertFalse((self.root / "replay-out").exists())

    def test_replay_refuses_writes_without_real_data(self):
        for op in ({"t": "cw", "bm": 0x40, "br": 4, "wv": 0x83, "wi": 0, "data": None},
                   {"t": "bo"}, {"t": "bo", "data": "zz"}, {"t": "bo", "data": " 0102"},
                   {"t": "bo", "data": "01 02"}, {"t": "bo", "data": "0102\n"},
                   {"t": "bo", "data": "012"}, {"t": "bo", "data": 12}, "cw"):
            with self.subTest(op=op):
                self.assertIn("refused", self.replay([dict(t="bi", len=8, dt=0.0), op]))
        self.assertIn("refused", self.replay({"ops": []}))

    def test_converter_marks_every_op_and_replay_refuses_it(self):
        dev, _ = self.device()
        dev.ctrl_transfer(0x40, 4, 0x83, 0, b"\x03\x30")
        dev.write(2, b"ab")
        dev.read(0x81, 8)
        ops, _ = converter.convert(converter.read_records(self.path.read_text()))
        self.assertEqual([o.get("src") for o in ops], [converter.FORMAT] * 3)
        self.assertFalse(any(o.get("partial") for o in ops))
        code = self.replay(ops, "--dry-run")
        self.assertIn("refused", code)
        self.assertIn("diagnostic", code)
        self.assertFalse((self.root / "replay-out").exists())

    def test_committed_captures_pass_replay_check(self):
        captures = sorted((REPO / "traces").glob("*.trace.json.gz"))
        self.assertEqual(len(captures), 9)
        for path in captures:
            with self.subTest(path=path.name), gzip.open(path, "rt") as handle:
                self.assertIsNone(replay_trace.replay_problem(json.load(handle)))

    def test_replay_accepts_capture_shaped_trace(self):
        ops = [dict(t="cw", bm=0x40, br=4, wv=0x83, wi=0, data="", dt=0.0),
               dict(t="bo", data="0102", dt=0.1), dict(t="bi", len=8, dt=0.1)]
        self.assertEqual(self.replay(ops, "--dry-run"), 0)

    def test_converter_failed_write_leaves_nothing_and_retry_works(self):
        for error in (OSError("disk full"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with patch.object(converter.gzip.GzipFile, "write", side_effect=error):
                    if isinstance(error, OSError):
                        code, err, out = self.convert_file(HEADER + bo_line(), name="o.json.gz")
                        self.assertEqual(code, 1)
                        self.assertIn("disk full", err)
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            self.convert_file(HEADER + bo_line(), name="o.json.gz")
                        out = self.root / "o.json.gz"
                self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["in.jsonl"])
                self.assertEqual(self.convert_file(HEADER + bo_line(), name="o.json.gz")[0], 0)
                with gzip.open(out, "rt") as handle:
                    self.assertEqual(json.load(handle)[0]["t"], "bo")
                out.unlink()


if __name__ == "__main__":
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TraceTests))
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} tests passed")
    sys.exit(0 if result.wasSuccessful() else 1)
