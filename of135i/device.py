"""Scan sequencing for the Plustek OpticFilm 135i (GL126).

`Scanner` drives the phase list in tables.py (generated from the
verified 3600 dpi single-frame trace, see driver/gen_tables.py and
driver-design.md's "Scan sequence" section) over a UsbIo transport,
replaying each phase's FULL verbatim op stream -- not just its
register batches -- and injecting the calibration.py values at the
documented injection points.

2026-08-30 rework: hardware A/B testing proved that executing only
the register batches + a poll subset (as this module used to) is
insufficient -- the verbatim replayer (replay_trace.py), which
executes the full op stream (all single control reads, exact op
ordering, captured dt pacing), measures correct calibration levels
where the slimmed execution saturates. Some of the skipped reads/
pacing evidently carry real state transitions on the device (lamp
settling etc.), so `_exec_ops` below reproduces replay_trace.py's
executor semantics (see its module docstring) exactly, phase by
phase, plus the hardware-discovered engine-busy wait after every
execute pulse.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from . import calibrate, tables, tables_base, tables_ir
from .tables import Op, Phase
from .usbio import EP_BULK_IN, EP_BULK_OUT, Of135iError, UsbIo

log = logging.getLogger("of135i")

# replay_trace.py's pacing: sleep a captured inter-op gap when it
# exceeds this threshold, capped so a real outlier can't stall a run.
_PACE_THRESHOLD = 0.05
_PACE_CAP = 2.0


def _has_execute_pulse(data: bytes) -> bool:
    """True if a cw wv=0x83 register-batch payload contains the pair
    (0x0f, 0x01) -- register 0x0f = "GO"."""
    return any(data[i] == 0x0F and data[i + 1] == 0x01 for i in range(0, len(data) - 1, 2))


class Scanner:
    """Drives the of135i scan sequence over a UsbIo transport."""

    def __init__(self, io: UsbIo):
        self.io = io

    @classmethod
    def open(cls) -> "Scanner":
        return cls(UsbIo.open())

    def close(self) -> None:
        self.io.close()

    def __enter__(self) -> "Scanner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------- op execution

    def _exec_ops(self, ops: list[Op]) -> list[bytes]:
        """Execute a verbatim op list with replay_trace.py semantics:

          - cw: ctrl_transfer verbatim; if it's a wv=0x82 buffer
            descriptor, flush/(re)start the current measurement buffer
            (read descriptors start a new one, write descriptors just
            flush); if it's a wv=0x83 register batch containing the
            execute pulse (0x0f=0x01), wait for the engine-idle bit
            afterwards.
          - cr: perform the read; log (don't raise) on a captured-
            response mismatch.
          - poll: poll until the final captured response, timing out
            after max(3*dur, 10s); log-and-continue.
          - bo: bulk write verbatim.
          - bi: bulk read, routed into the current measurement buffer
            (if any).
          - dt pacing: sleep dt before the op when it exceeds
            _PACE_THRESHOLD, capped at _PACE_CAP.

        Returns the list of collected read buffers, in the order their
        buffer-read descriptors were encountered (most phases produce
        0 or 1; "scan" produces one per image chunk plus the trailing
        drain).
        """
        dev = self.io.dev
        collected: list[bytes] = []
        cur: bytearray | None = None

        def flush() -> None:
            nonlocal cur
            if cur is not None:
                collected.append(bytes(cur))
                cur = None

        for op in ops:
            if op.dt > _PACE_THRESHOLD:
                time.sleep(min(op.dt, _PACE_CAP))

            if op.kind == "cw":
                if op.wv == 0x0082 and op.br == 0x04 and len(op.data) == 8:
                    flush()
                    if op.wi != 1:
                        cur = bytearray()
                dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.data)
                # NOTE: no engine-idle wait here. The verbatim replayer
                # never waits and works (verified on hardware twice,
                # 2026-08-30): the trace's own polls carry the timing,
                # and during the image pass the scanner streams while
                # bulk reads drain it -- blocking until idle overflows
                # the scanner's buffer and yields stale RAM instead of
                # live data. _wait_engine_idle() remains in _motor_run
                # for our own out-of-trace moves (home/eject).
            elif op.kind == "cr":
                got = bytes(dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.length))
                if got != op.resp:
                    log.debug(
                        "cr wv=%#06x wi=%#06x: got %s want %s -- continuing",
                        op.wv, op.wi, got.hex(), op.resp.hex(),
                    )
            elif op.kind == "poll":
                self._poll_one(op)
            elif op.kind == "bo":
                dev.write(EP_BULK_OUT, op.data, timeout=5000)
            elif op.kind == "bi":
                data = dev.read(EP_BULK_IN, op.length, timeout=15000)
                if cur is not None:
                    cur.extend(data)
            else:
                raise ValueError(f"unknown op kind {op.kind!r}")

        flush()
        return collected

    def _poll_one(self, op: Op) -> None:
        want = op.resp
        timeout = max(3 * op.dur, 10.0)
        deadline = time.monotonic() + timeout
        dev = self.io.dev
        while True:
            got = bytes(dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.length))
            if got == want:
                return
            if time.monotonic() > deadline:
                log.warning(
                    "poll wv=%#06x wi=%#06x timed out after %.1fs "
                    "(last %s, want %s) -- continuing",
                    op.wv, op.wi, timeout, got.hex(), want.hex(),
                )
                return
            time.sleep(0.004)

    def _run_phase(self, phase: Phase, **inject) -> list[bytes]:
        """Execute a phase's full op stream (with injections applied),
        returning its collected read buffers."""
        ops = phase.patched(**inject) if inject else phase.ops
        return self._exec_ops(ops)

    # -------------------------------------------------------------- init

    def initialize(self, ir: bool = False) -> None:
        """Full device initialization.

        First the power-on base register table + AFE base values (what
        the vendor driver programs at device open; without these the
        scanner keeps whatever state the previous session left, which
        breaks calibration -- observed 2026-08-30 as a saturated white
        measurement after a VM session). Then the pre-scan phases from
        the capture (tables.PREP + AFE_BASE), full op stream.
        """
        self.io.write_regs(tables_base.BASE_INIT_PAIRS)
        for adr, val in tables_base.AFE_BASE_PAIRS:
            self.io.write_regs([(0x51, adr), (0x5D, 0x00), (0x5E, val)])
        # IR mode: trace 04's own prep carries IR-LED setup that the
        # plain prep lacks (a dim IR pass was observed without it,
        # 2026-08-30) -- run the matching phase set.
        t = tables_ir if ir else tables
        self._run_phase(t.PREP)
        self._run_phase(t.AFE_BASE)

    def _wait_engine_idle(self, timeout: float = 90.0) -> None:
        """Wait until the scan/motor engine is idle again.

        Discovered on hardware 2026-08-30 (resolves open question 5 in
        protocol-notes.md): bit 0x20 of reg 0x01 CLEARS while the
        engine executes (0x22 -> 0x02 observed during homing) and sets
        again on completion. Waiting on this bit replaces mimicking the
        capture's poll timing, which raced the hardware and returned
        stale buffer data. Applied after every cw wv=0x83 batch
        containing the execute pulse (0x0f=0x01), before any
        subsequent buffer reads -- see _exec_ops.
        """
        try:
            self.io.wait_reg(0x01, 0x20, timeout=timeout, mask=0x20)
        except Of135iError as e:
            log.warning("engine-idle wait: %s -- continuing", e)

    # -------------------------------------------------------------- motor

    def _motor_run(self, mode: int, feedl: int) -> None:
        """Decoded motor sequence (protocol-notes.md pass 2/3/4):
        02=mode, 3d/3e/3f=FEEDL (24-bit, hi/mid/lo), 0f=01 executes.

        Not trace-derived (home()/eject() move to targets the capture
        never visited), so this stays hand-written rather than table-
        driven."""
        self.io.write_regs([(0x09, 0x08)])
        self.io.write_regs([
            (0x02, mode), (0xAE, 0x00), (0xAF, 0xFF),
            (0x3D, (feedl >> 16) & 0xFF),
            (0x3E, (feedl >> 8) & 0xFF),
            (0x3F, feedl & 0xFF),
        ])
        self.io.write_regs([(0x0F, 0x01)])
        # No motor-busy bit is confirmed yet (driver-design.md open
        # items / protocol-notes.md pass 4 "Motor/feed" note): reg 0x01
        # was observed unchanged across a real homing move on hardware.
        # Poll it briefly (as of135i_poc.py's motor_run does) so a
        # deployed driver at least has somewhere to add a real busy
        # check later, but don't block indefinitely on it.
        time.sleep(0.1)
        self._wait_engine_idle()
        self.io.write_regs([(0x09, 0x00)])

    def home(self) -> None:
        """Homing sequence: mode 0x30, FEEDL=1 (protocol-notes.md pass 4)."""
        self._motor_run(0x30, 1)

    def eject(self) -> None:
        """Eject the film magazine: mode 0x18, FEEDL=3090 (06-eject,
        protocol-notes.md pass 2/4)."""
        self._motor_run(0x18, 3090)

    # -------------------------------------------------------------- scan

    def scan(
        self, frame: int = 1, lines: int | None = None, ir: bool = False
    ) -> tuple[bytes, int] | tuple[bytes, int, dict]:
        """Run the full calibration + scan sequence for one frame.

        With ir=False (default): returns (raw_bytes, width) -- raw_bytes
        is the pixel-interleaved RGB16LE image buffer (image.assemble()
        turns it into an ndarray), width is tables.IMAGE_WIDTH. This
        path is byte-identical to before ir was added (see
        tests/test_calibrate.py's sequence test).

        With ir=True: runs the IR-enabled phase set (tables_ir.py,
        compiled from traces/04-singel-3600-IRpa.trace.json.gz -- see
        gen_tables.py's main_ir() for the phase mapping) via _scan_ir(),
        returning (raw_bytes, width, meta) -- meta is
        {"width": 5184, "alternating": True} (see image.split_ir() for
        turning raw_bytes into separate visible/IR arrays).
        """
        if ir:
            return self._scan_ir(frame=frame, lines=lines)

        # ---- establish the canonical start position --------------------
        # The captured flow always begins from the HOME position (the
        # preceding preview segment ends with a homing move). A
        # firmware-side magazine feed parks the transport elsewhere,
        # which shifts every absolute FEEDL below AND puts the wrong
        # part of the film path in the light during calibration
        # (observed 2026-08-30: saturated white measurement, striped
        # image). Home first -- _wait_engine_idle() inside _motor_run
        # already blocks until the move settles, so no extra sleep is
        # needed here.
        log.info("homing before scan")
        self.home()

        # ---- dark pair (offset bracket, gain=0) ------------------------
        dark_a_raw = self._run_phase(tables.CAL_DARK_A)[0]
        dark_b_raw = self._run_phase(tables.CAL_DARK_B)[0]
        dark_a = np.frombuffer(dark_a_raw, dtype="<u2").reshape(-1, 3)
        dark_b = np.frombuffer(dark_b_raw, dtype="<u2").reshape(-1, 3)

        # ---- white line (gain=0) -> compute AFE gain -------------------
        white_raw = self._run_phase(tables.CAL_WHITE)[0]
        white = np.frombuffer(white_raw, dtype="<u2").reshape(-1, 3)
        gain_r, gain_g, gain_b = calibrate.gain_codes(white)
        log.info("computed gain codes: R=%#04x G=%#04x B=%#04x", gain_r, gain_g, gain_b)

        # ---- gain-check pair (offset bracket, gain=computed) -----------
        self._run_phase(
            tables.CAL_GAIN_CHECK_A,
            gain_r=bytes([gain_r]), gain_g=bytes([gain_g]), gain_b=bytes([gain_b]),
        )
        self._run_phase(tables.CAL_GAIN_CHECK_B)

        # ---- shading measurement (offset=computed final) ---------------
        off_r, off_g, off_b = calibrate.offset_codes(dark_a, dark_b)
        log.info("offset codes: R=%#06x G=%#06x B=%#06x", off_r, off_g, off_b)
        shading_meas_raw = self._run_phase(
            tables.CAL_SHADING_MEASURE,
            offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
            offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
            offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF]),
        )[0]
        shading_meas = np.frombuffer(shading_meas_raw, dtype="<u2").reshape(128, 3762, 3)
        shading = calibrate.shading_table(shading_meas)

        # ---- upload + verify (re-measure, re-upload once) ---------------
        self._run_phase(tables.CAL_SHADING_UPLOAD, shading_table=shading)

        # cal_shading_verify genuinely re-measures then re-uploads a
        # shading table computed from *that* read, so it can't be run
        # as a single _run_phase() call: shading_table2 depends on data
        # only available after the phase's own read completes. Run its
        # ops up to (not including) the re-upload's buffer-write
        # descriptor (split_at), compute, then run the rest patched.
        verify_phase = tables.CAL_SHADING_VERIFY
        verify_raw = self._exec_ops(verify_phase.ops[:verify_phase.split_at])[0]
        verify_meas = np.frombuffer(verify_raw, dtype="<u2").reshape(128, 3762, 3)
        shading2 = calibrate.shading_table2(verify_meas, shading_meas)
        remaining = verify_phase.patched(shading_table2=shading2)[verify_phase.split_at:]
        self._exec_ops(remaining)

        # ---- position: absolute feed from home to `frame` ---------------
        feedl = tables.feedl_for_frame(frame)
        log.info("positioning to frame %d (FEEDL=%d)", frame, feedl)
        self._run_phase(
            tables.POSITION,
            feedl_hi=bytes([(feedl >> 16) & 0xFF]),
            feedl_mid=bytes([(feedl >> 8) & 0xFF]),
            feedl_lo=bytes([feedl & 0xFF]),
        )

        # ---- scan: 3 slope tables, line count, execute, image data ------
        n_lines = lines if lines is not None else tables.DEFAULT_LINES
        buffers = self._run_phase(
            tables.SCAN,
            lines_hi=bytes([(n_lines >> 8) & 0xFF]),
            lines_lo=bytes([n_lines & 0xFF]),
        )
        # buffers[:IMAGE_CHUNK_COUNT] are the image data (capture
        # fidelity: the first image descriptor carries wIndex=8,
        # subsequent ones 0 -- meaning unknown, baked into tables.py).
        # The trailing buffer (buffers[-1]) is a drain of unclear
        # purpose (not part of the documented image size); already
        # read verbatim above, discarded here.
        image = b"".join(buffers[:tables.IMAGE_CHUNK_COUNT])

        # ---- park ---------------------------------------------------------
        # PARK's own first op is the captured end-of-access control
        # write (cw wv=0x8d) -- no separate call needed here.
        self._run_phase(tables.PARK)

        return image, tables.IMAGE_WIDTH

    # ----------------------------------------------------------- scan (IR)

    def _scan_ir(self, frame: int = 1, lines: int | None = None) -> tuple[bytes, int, dict]:
        """IR-enabled counterpart of scan(): same structure, driven by
        tables_ir.py's phases (compiled from traces/04-singel-3600-
        IRpa.trace.json.gz -- see gen_tables.py's main_ir() for the full
        phase mapping) instead of tables.py's.

        Every calibration buffer in this mode is at the RAW sensor width
        (tables_ir.IMAGE_WIDTH = 5184, not the windowed 3762 the plain
        scan uses) and covers ALTERNATING lines -- even index = IR pass,
        odd index = visible pass (see ../cal-data/ir/ir-analysis.md and
        image.split_ir(), which applies the same convention to the
        final image). Measurements that feed a per-channel computation
        (gain, shading) are de-interleaved into their visible/IR halves
        BEFORE computing, so the IR pass's very different signal level
        (bright/flat, R=G=B) never gets averaged into the visible
        channel's numbers or vice versa. The shading table is uploaded
        to TWO scanner-RAM addresses (one per light source); AFE gain/
        offset (regs 2-7) and FEEDL/line-count injections are otherwise
        the same single-register mechanism as the plain scan.
        """
        log.info("homing before scan (ir mode)")
        self.home()

        W = tables_ir.IMAGE_WIDTH

        # ---- dark pair (offset bracket, gain=0) ------------------------
        # Content unused by offset_codes() (same as the plain path); the
        # doubled buffer size (alternating IR/visible) doesn't matter --
        # flattened to (N, 3) either way.
        dark_a_raw = self._run_phase_ir(tables_ir.CAL_DARK_A)[0]
        dark_b_raw = self._run_phase_ir(tables_ir.CAL_DARK_B)[0]
        dark_a = np.frombuffer(dark_a_raw, dtype="<u2").reshape(-1, 3)
        dark_b = np.frombuffer(dark_b_raw, dtype="<u2").reshape(-1, 3)

        # ---- white line (gain=0) -> compute AFE gain -------------------
        # 2 raw lines (alternating): index 0 = IR, index 1 = visible.
        # Only ONE set of AFE gain registers exists (regs 2/3/4, same as
        # the plain path) -- computed from the VISIBLE line only, since
        # mixing in the IR line's near-flat/bright broadcast values
        # would badly skew the 99.9th-percentile peak calculation.
        white_raw = self._run_phase_ir(tables_ir.CAL_WHITE)[0]
        white = np.frombuffer(white_raw, dtype="<u2").reshape(-1, W, 3)
        white_visible = white[1::2].reshape(-1, 3)
        gain_r, gain_g, gain_b = calibrate.gain_codes(white_visible)
        log.info("computed gain codes (ir mode): R=%#04x G=%#04x B=%#04x", gain_r, gain_g, gain_b)

        # ---- gain-check pair (offset bracket, gain=computed) -----------
        self._run_phase_ir(
            tables_ir.CAL_GAIN_CHECK_A,
            gain_r=bytes([gain_r]), gain_g=bytes([gain_g]), gain_b=bytes([gain_b]),
        )
        self._run_phase_ir(tables_ir.CAL_GAIN_CHECK_B)

        # ---- shading measurement (offset=computed final) ---------------
        off_r, off_g, off_b = calibrate.offset_codes(dark_a, dark_b)
        log.info("offset codes (ir mode): R=%#06x G=%#06x B=%#06x", off_r, off_g, off_b)
        shading_meas_raw = self._run_phase_ir(
            tables_ir.CAL_SHADING_MEASURE,
            offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
            offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
            offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF]),
        )[0]
        shading_meas = np.frombuffer(shading_meas_raw, dtype="<u2").reshape(-1, W, 3)
        shading_meas_ir = shading_meas[0::2]        # (lines, W, 3), IR pass
        shading_meas_visible = shading_meas[1::2]   # (lines, W, 3), visible pass
        shading_visible = calibrate.shading_table(shading_meas_visible, width=W)
        # The IR channel's shading table follows the SAME wire format
        # (width*3 pairs, per gen_tables.py's main_ir() -- the upload
        # is 63192 B either way) even though R=G=B physically for IR;
        # shading_table() doesn't need to know that.
        shading_ir = calibrate.shading_table(shading_meas_ir, width=W)

        # ---- upload + verify (re-measure, re-upload once) ---------------
        self._run_phase_ir(
            tables_ir.CAL_SHADING_UPLOAD,
            shading_table_visible=shading_visible, shading_table_ir=shading_ir,
        )

        verify_phase = tables_ir.CAL_SHADING_VERIFY
        verify_raw = self._exec_ops(verify_phase.ops[:verify_phase.split_at])[0]
        verify_meas = np.frombuffer(verify_raw, dtype="<u2").reshape(-1, W, 3)
        verify_ir = verify_meas[0::2]
        verify_visible = verify_meas[1::2]
        shading2_visible = calibrate.shading_table2(
            verify_visible, shading_meas_visible, width=W,
            targets=calibrate.SHADING2_TARGETS_IRMODE_VISIBLE,
        )
        shading2_ir = calibrate.shading_table2(
            verify_ir, shading_meas_ir, width=W,
            targets=calibrate.SHADING2_TARGETS_IRMODE_IR,
        )
        remaining = verify_phase.patched(
            shading_table2_visible=shading2_visible, shading_table2_ir=shading2_ir,
        )[verify_phase.split_at:]
        self._exec_ops(remaining)

        # ---- position: absolute feed from home to `frame` ---------------
        feedl = tables_ir.feedl_for_frame(frame)
        log.info("positioning to frame %d (FEEDL=%d, ir mode)", frame, feedl)
        self._run_phase_ir(
            tables_ir.POSITION,
            feedl_hi=bytes([(feedl >> 16) & 0xFF]),
            feedl_mid=bytes([(feedl >> 8) & 0xFF]),
            feedl_lo=bytes([feedl & 0xFF]),
        )

        # ---- scan: 3 slope tables, line count, execute, image data ------
        n_lines = lines if lines is not None else tables_ir.DEFAULT_LINES
        buffers = self._run_phase_ir(
            tables_ir.SCAN,
            lines_hi=bytes([(n_lines >> 8) & 0xFF]),
            lines_lo=bytes([n_lines & 0xFF]),
        )
        # buffers[:IMAGE_CHUNK_COUNT] (659) are the real image data; the
        # 660th descriptor is issued but cancelled with no data (see
        # ir-analysis.md/gen_tables.py's main_ir()) and is not among the
        # collected buffers at all (_exec_ops only appends a buffer on
        # flush, and a cancelled read never gets any bi data to flush).
        image = b"".join(buffers[:tables_ir.IMAGE_CHUNK_COUNT])

        # ---- park ---------------------------------------------------------
        self._run_phase_ir(tables_ir.PARK)

        return image, tables_ir.IMAGE_WIDTH, {"width": tables_ir.IMAGE_WIDTH, "alternating": True}

    def _run_phase_ir(self, phase, **inject) -> list[bytes]:
        """Same as _run_phase(), for a tables_ir.py Phase (a distinct
        but structurally identical class -- see tables_ir.py's Phase
        docstring) -- kept as a separate method only for the type hint;
        Phase.patched()/_exec_ops() work on either module's Phase/Op
        unchanged (both are plain dataclasses with the same fields)."""
        ops = phase.patched(**inject) if inject else phase.ops
        return self._exec_ops(ops)
