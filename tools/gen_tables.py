#!/usr/bin/env python3
"""Generate driver/of135i/tables.py from the compiled 3600 dpi scan trace.

Source: traces/03-singel-3600-IRav.trace.json.gz (compiled by
compile_trace.py from captures/segments/03-singel-3600-IRav.pcap).

This script is the *only* place phase boundaries and injection-point
op offsets are decided. tables.py itself is generated, verbatim op
data plus a small dataclass scaffold, so it can be regenerated any
time the source trace changes.

Usage:
    .venv/bin/python driver/gen_tables.py

Phase boundaries (op-index ranges into the 9540-op trace) were found
by two mechanical anchors, per the task spec:
  - buffer-read descriptors: cw wv=0x82 wi=0 (a bulk-IN request)
  - scan-execute writes: a cw wv=0x83 batch containing the pair
    (0x0f, 0x01) (register 0x0f = "GO")
These anchors were located with an exploratory scan; the resulting
ranges are hardcoded below because the mapping from anchors to
*named* phases (per driver-design.md's "Scan sequence" section)
requires judgement the mechanical split alone can't provide -- see
NOTES at the bottom of this file for the specific decisions made.

2026-08-30 rework: hardware A/B testing proved the slimmed
(register-batches + poll-subset) execution this file used to emit is
insufficient -- the verbatim replayer (replay_trace.py), which
executes the FULL 9540-op stream (all 889 single control reads, exact
op ordering, captured dt pacing), measures correct calibration levels
where the slimmed driver saturates. Phases now carry the FULL ordered
op list (cw/cr/poll/bo/bi, verbatim) for their op range; device.py's
executor replays it with replayer semantics. See driver-design.md and
the task report for the diagnosis.
"""

from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACE = HERE.parent / "traces" / "03-singel-3600-IRav.trace.json.gz"
OUT = HERE.parent / "of135i" / "tables.py"

# IR-enabled trace (see the module docstring addendum near main_ir() below,
# and driver/gen_tables.py's __main__ block).
TRACE_IR = HERE.parent / "traces" / "04-singel-3600-IRpa.trace.json.gz"
OUT_IR = HERE.parent / "of135i" / "tables_ir.py"


def load_ops(trace_path=TRACE):
    with gzip.open(trace_path, "rt") as fh:
        return json.load(fh)


def pairs_of(data: bytes):
    return [(data[i], data[i + 1]) for i in range(0, len(data), 2)]


def val_offset(batch: bytes, reg: int) -> int:
    """Byte offset of reg's *value* byte within a (reg,val) pair batch."""
    for i in range(0, len(batch), 2):
        if batch[i] == reg:
            return i + 1
    raise ValueError(f"reg 0x{reg:02x} not found in batch {batch.hex()}")


# --------------------------------------------------------------- phase table
#
# (name, start_index, end_index_inclusive). Ranges cover every op
# belonging to the phase (all of cw/cr/poll/bo/bi, verbatim). See
# NOTES at the end of this file for how these boundaries were chosen
# and where the trace didn't match the spec's phase names cleanly.

PHASE_BOUNDS = [
    ("prep", 0, 38),
    ("afe_base", 39, 109),
    ("cal_dark_a", 110, 138),
    ("cal_dark_b", 139, 167),
    ("cal_white", 168, 239),
    ("cal_gain_check_a", 240, 276),
    ("cal_gain_check_b", 277, 305),
    ("cal_shading_measure", 306, 755),
    ("cal_shading_upload", 756, 761),
    ("cal_shading_verify", 762, 1216),
    ("position", 1217, 1264),
    ("scan", 1265, 9404),
    ("park", 9405, 9539),
]

# Injection points: byte-level (gain/offset/FEEDL/line-count) or
# full-bulk-OUT-payload (shading table) placeholders, found the same
# way as before -- by pattern-matching the decoded (reg,val) pairs of
# cw wv=0x83 batches within a phase's op range -- but now referencing
# an index into the phase's *full* op list (ops[i]) rather than a
# reg-batches-only sublist.
GAIN_INJECT = {"gain_r": 0x02, "gain_g": 0x03, "gain_b": 0x04}
OFFSET_INJECT = {"offset_r": 0x05, "offset_g": 0x06, "offset_b": 0x07}


def make_op(o: dict) -> dict:
    """Normalize one raw trace op dict into the fields tables.Op needs."""
    kind = o["t"]
    out = dict(kind=kind, dt=o.get("dt", 0.0), bm=0, br=0, wv=0, wi=0,
               data=b"", length=0, resp=b"", dur=0.0)
    if kind == "cw":
        out.update(bm=o["bm"], br=o["br"], wv=o["wv"], wi=o["wi"],
                    data=bytes.fromhex(o["data"]) if o["data"] else b"")
    elif kind == "cr":
        out.update(bm=o["bm"], br=o["br"], wv=o["wv"], wi=o["wi"],
                    length=o["len"], resp=bytes.fromhex(o["resp"]))
    elif kind == "poll":
        out.update(bm=o["bm"], br=o["br"], wv=o["wv"], wi=o["wi"],
                    length=o["len"], resp=bytes.fromhex(o["resps"][-1]),
                    dur=o["dur"])
    elif kind == "bo":
        out.update(data=bytes.fromhex(o["data"]))
    elif kind == "bi":
        out.update(length=o["len"])
    else:
        raise ValueError(f"unknown op type {kind!r}")
    return out


def find_afe_batch(phase_ops, reg, last=False):
    """Find the local op index of the single AFE-indirect write
    `51 <reg> 5d <hi> 5e <lo>` (a cw wv=0x83 batch) within phase_ops. A
    given AFE register can legitimately be programmed more than once
    in the same phase (e.g. cal_shading_measure writes an intermediate
    offset, then the final one right before its execute pulse); pass
    last=True to get the final occurrence instead of the first."""
    hits = []
    for i, op in enumerate(phase_ops):
        if op["kind"] == "cw" and op["wv"] == 0x83:
            p = pairs_of(op["data"])
            if len(p) == 3 and p[0] == (0x51, reg) and p[1][0] == 0x5D and p[2][0] == 0x5E:
                hits.append(i)
    if not hits:
        raise ValueError(f"AFE write for reg 0x{reg:02x} not found")
    return hits[-1] if last else hits[0]


def build_phase(ops, name, start, end):
    return dict(
        name=name, start=start, end=end,
        phase_ops=[make_op(ops[i]) for i in range(start, end + 1)],
    )


def find_buf_descs(phase_ops):
    """[(local_index, kind, addr, length), ...] for every cw wv=0x82
    buffer descriptor in a phase's op list, in order."""
    out = []
    for i, op in enumerate(phase_ops):
        if op["kind"] == "cw" and op["wv"] == 0x82 and len(op["data"]) == 8:
            addr, ln = struct.unpack("<II", op["data"])
            kind = "write" if op["wi"] == 1 else "read"
            out.append((i, kind, addr, ln))
    return out


def bo_indices(phase_ops, start_after=0):
    return [i for i, op in enumerate(phase_ops) if i >= start_after and op["kind"] == "bo"]


def main():
    ops = load_ops()
    phases = [build_phase(ops, name, s, e) for name, s, e in PHASE_BOUNDS]
    by_name = {p["name"]: p for p in phases}

    # ---- injection points ----------------------------------------------
    injections = {p["name"]: {} for p in phases}

    gc = by_name["cal_gain_check_a"]["phase_ops"]
    for inject_name, reg in GAIN_INJECT.items():
        i = find_afe_batch(gc, reg)
        injections["cal_gain_check_a"][inject_name] = ("byte", i, val_offset(gc[i]["data"], 0x5E))

    sm = by_name["cal_shading_measure"]["phase_ops"]
    for inject_name, reg in OFFSET_INJECT.items():
        # last=True: cal_shading_measure writes an intermediate offset
        # first, then the final one (matching cal-analysis.md's
        # documented 0x010b/0x010a/0x010b) right before its execute.
        i = find_afe_batch(sm, reg, last=True)
        injections["cal_shading_measure"][inject_name + "_hi"] = ("byte", i, val_offset(sm[i]["data"], 0x5D))
        injections["cal_shading_measure"][inject_name + "_lo"] = ("byte", i, val_offset(sm[i]["data"], 0x5E))

    # FEEDL: the 32-pair batch in "position" containing (0x3d,0x00)/
    # (0x3e,0x1a)/(0x3f,0x57) alongside (0x02,0x18).
    pos = by_name["position"]["phase_ops"]
    feedl_i = None
    for i, op in enumerate(pos):
        if op["kind"] == "cw" and op["wv"] == 0x83:
            p = pairs_of(op["data"])
            regs = {r for r, _ in p}
            if 0x3D in regs and 0x3E in regs and 0x3F in regs and (0x02, 0x18) in p:
                feedl_i = i
                break
    if feedl_i is None:
        raise SystemExit("FEEDL batch not found in position phase")
    fb = pos[feedl_i]["data"]
    injections["position"] = {
        "feedl_hi": ("byte", feedl_i, val_offset(fb, 0x3D)),
        "feedl_mid": ("byte", feedl_i, val_offset(fb, 0x3E)),
        "feedl_lo": ("byte", feedl_i, val_offset(fb, 0x3F)),
    }

    # line count: the batch in "scan" containing (0x26,..)/(0x27,..)
    # alongside motor mode 0x02=0x30 (home-style pulse before imaging).
    sc = by_name["scan"]["phase_ops"]
    lc_i = None
    for i, op in enumerate(sc):
        if op["kind"] == "cw" and op["wv"] == 0x83:
            p = pairs_of(op["data"])
            regs = {r for r, _ in p}
            if 0x26 in regs and 0x27 in regs and len(p) > 10:
                lc_i = i
                break
    if lc_i is None:
        raise SystemExit("line-count batch not found in scan phase")
    lb = sc[lc_i]["data"]
    injections["scan"] = {
        "lines_hi": ("byte", lc_i, val_offset(lb, 0x26)),
        "lines_lo": ("byte", lc_i, val_offset(lb, 0x27)),
    }

    # ---- shading upload / verify: bo-payload injection points ----------
    # cal_shading_upload's 4 bo chunks and cal_shading_verify's 4
    # bo chunks (the re-upload, after its own read) do not embed
    # computed shading-table bytes -- those are computed per-run by
    # calibrate.shading_table() from a live 128-line measurement.
    # Device.py splices the computed bytes across the *captured chunk
    # boundaries* (same wire framing as the capture) via these
    # injection points.
    up = by_name["cal_shading_upload"]["phase_ops"]
    up_bo = bo_indices(up)
    assert len(up_bo) == 4, up_bo
    injections["cal_shading_upload"]["shading_table"] = ("bo", tuple(up_bo))

    sv = by_name["cal_shading_verify"]["phase_ops"]
    sv_descs = find_buf_descs(sv)
    assert len(sv_descs) == 2, sv_descs
    (read_i, read_kind, _, _), (write_i, write_kind, _, _) = sv_descs
    assert read_kind == "read" and write_kind == "write", sv_descs
    split_at = write_i  # local op index: everything from here on is the re-upload
    sv_bo = bo_indices(sv, start_after=split_at)
    assert len(sv_bo) == 4, sv_bo
    injections["cal_shading_verify"]["shading_table2"] = ("bo", tuple(sv_bo))
    by_name["cal_shading_verify"]["split_at"] = split_at

    # ---- scan-phase image-block dedup -----------------------------------
    # The 223 image-data buffer reads (519156 B each) share one
    # identical op pattern: a single cw wv=0x82 read descriptor,
    # followed by a status cr, followed by 33 bi chunks (32x16384 B +
    # a final smaller chunk). Deduped here as one IMAGE_BLOCK_OPS
    # constant (the interior cr+bi run) reused via a generated loop,
    # keeping tables.py from embedding ~7500 near-duplicate Op() calls.
    sc_descs = find_buf_descs(sc)
    image_descs = [d for d in sc_descs if d[1] == "read" and d[3] == 519156]
    assert len(image_descs) == 223, len(image_descs)
    trailing_descs = [d for d in sc_descs if d[1] == "read" and d[3] != 519156]
    assert len(trailing_descs) == 1, trailing_descs
    trailing_i, _, trailing_addr, trailing_len = trailing_descs[0]

    image_block_ops = None
    image_block_pattern = None
    for k in range(len(image_descs)):
        i0 = image_descs[k][0]
        i1 = image_descs[k + 1][0] if k + 1 < len(image_descs) else trailing_i
        block = sc[i0 + 1:i1]
        pattern = tuple((o["kind"], o["length"]) for o in block)
        if image_block_ops is None:
            image_block_ops = block
            image_block_pattern = pattern
        elif pattern != image_block_pattern:
            raise SystemExit(f"image block {k} pattern differs from block 0")
    image_desc_data = sc[image_descs[0][0]]["data"]
    for i, _, addr, ln in image_descs:
        assert addr == 0x10000000 and ln == 519156

    # Everything in "scan" up to (not including) the first image read
    # descriptor, and everything from the trailing-drain descriptor
    # onward, is emitted literally; the 223 repeats in between are
    # generated by a loop over image_desc_data + IMAGE_BLOCK_OPS.
    scan_head = sc[:image_descs[0][0]]
    scan_tail = sc[trailing_i:]
    # Re-locate the line-count injection index within scan_head (it
    # must fall before the image reads -- verified by construction).
    assert lc_i < image_descs[0][0], "line-count batch expected before image reads"

    # ---- slope tables (position + scan phases) --------------------------
    pos_bo_i = bo_indices(pos)
    assert len(pos_bo_i) == 2, pos_bo_i
    pos_bo = [pos[i]["data"] for i in pos_bo_i]
    assert pos_bo[0] == pos_bo[1], "expected position phase's 2 slope writes identical"
    slope_position = pos_bo[0]

    scan_bo_i = [i for i, op in enumerate(scan_head) if op["kind"] == "bo"]
    assert len(scan_bo_i) == 3, scan_bo_i
    scan_bo = [scan_head[i]["data"] for i in scan_bo_i]
    assert scan_bo[0] == scan_bo[1] == scan_bo[2], "expected scan phase's 3 slope writes identical"
    slope_scan = scan_bo[0]

    # ---- render tables.py -------------------------------------------------
    lines = []
    w = lines.append
    w('"""Derived scan-sequence constants for the of135i driver.')
    w("")
    w("AUTO-GENERATED by driver/gen_tables.py from")
    w(f"{TRACE.relative_to(HERE.parent)} -- do not edit by hand.")
    w("Regenerate with: .venv/bin/python driver/gen_tables.py")
    w("")
    w("Structures the verified 3600 dpi single-frame scan flow (see")
    w("driver-design.md 'Scan sequence' and protocol-notes.md) as an")
    w("ordered list of PHASES. Each Phase holds its FULL verbatim op")
    w("stream (control writes/reads, coalesced status polls, bulk-OUT")
    w("and bulk-IN, in captured order with captured dt pacing) for its")
    w("trace op-index range -- see device.py's executor, which replays")
    w("phases with replay_trace.py semantics (same cw/cr/poll/bo/bi")
    w("handling, same dt pacing, plus the hardware-discovered engine-")
    w("busy wait after any execute pulse). Injection points (AFE")
    w("gain/offset codes, FEEDL, line count, shading upload payload)")
    w("are named placeholders patched by device.py at runtime -- see")
    w("Phase.injections/patched() and calibrate.py.")
    w('"""')
    w("")
    w("from __future__ import annotations")
    w("")
    w("from dataclasses import dataclass, field, replace")
    w("")
    w("")
    w("@dataclass(frozen=True)")
    w("class Op:")
    w('    """One verbatim operation from the captured trace, in order.')
    w("")
    w("    kind: 'cw' (control write), 'cr' (control read), 'poll'")
    w("        (coalesced repeated control read), 'bo' (bulk OUT), or")
    w("        'bi' (bulk IN).")
    w("    dt: seconds since the previous op in the capture -- replayed")
    w("        as a pacing sleep when above device.py's threshold.")
    w("    bm/br/wv/wi: control-transfer setup fields (cw/cr/poll only).")
    w("    data: payload bytes (cw control-write data, bo bulk-OUT")
    w("        payload); b'' for cr/poll/bi.")
    w("    length: expected reply length (cr/poll) or bulk-IN chunk")
    w("        length (bi); 0 for cw/bo.")
    w("    resp: expected response bytes (cr: the captured reply; poll:")
    w("        the final settled reply) -- informational, a live")
    w("        mismatch is logged, not enforced.")
    w("    dur: captured poll duration in seconds, used to scale the")
    w("        replay timeout (poll only).")
    w('    """')
    w("")
    w("    kind: str")
    w("    dt: float = 0.0")
    w("    bm: int = 0")
    w("    br: int = 0")
    w("    wv: int = 0")
    w("    wi: int = 0")
    w("    data: bytes = b\"\"")
    w("    length: int = 0")
    w("    resp: bytes = b\"\"")
    w("    dur: float = 0.0")
    w("")
    w("")
    w("@dataclass")
    w("class Phase:")
    w('    """One named step of the scan sequence, as its full verbatim')
    w("    op stream.")
    w("")
    w("    ops: ordered verbatim ops (see Op) for this phase's trace")
    w("        op-index range, captured (frame-1, 3600 dpi) values baked")
    w("        in; callers that want computed values patch a copy via")
    w("        `patched()`.")
    w("    injections: name -> spec for the ops that carry computed")
    w("        values instead of their captured ones:")
    w('          ("byte", op_index, byte_offset) -- a single/multi-byte')
    w("            splice into ops[op_index].data at byte_offset.")
    w('          ("bo", (op_index, ...)) -- a full payload, split across')
    w("            the named bo ops' *original* chunk lengths, in order")
    w("            (the shading-table upload/re-upload).")
    w("    split_at: for cal_shading_verify only -- the local op index")
    w("        of its re-upload's buffer-write descriptor. Everything")
    w("        before it (the re-measurement) can run standalone; the")
    w("        shading_table2 injection depends on that measurement, so")
    w("        device.py runs ops[:split_at], computes it, then runs")
    w("        patched(shading_table2=...)[split_at:].")
    w('    """')
    w("")
    w("    name: str")
    w("    op_range: tuple[int, int]   # trace op-index range this phase covers (provenance)")
    w("    ops: list[Op] = field(default_factory=list)")
    w("    injections: dict[str, tuple] = field(default_factory=dict)")
    w("    split_at: int | None = None")
    w("")
    w("    def patched(self, **values: bytes) -> list[Op]:")
    w('        """Return a copy of ops with named injection points')
    w("        overwritten by the given values (see `injections` above).")
    w("")
    w("        Unknown names in `values` are ignored (a phase may not")
    w('        carry every injection point another phase does).')
    w('        """')
    w("        out = list(self.ops)")
    w("        for name, val in values.items():")
    w("            spec = self.injections.get(name)")
    w("            if spec is None:")
    w("                continue")
    w('            if spec[0] == "byte":')
    w("                _, idx, off = spec")
    w("                data = bytearray(out[idx].data)")
    w("                data[off:off + len(val)] = val")
    w("                out[idx] = replace(out[idx], data=bytes(data))")
    w('            elif spec[0] == "bo":')
    w("                _, idxs = spec")
    w("                total = sum(len(out[idx].data) for idx in idxs)")
    w("                if len(val) > total:")
    w('                    raise ValueError(')
    w('                        f"injection {name!r}: got {len(val)} B, "')
    w('                        f"expected at most {total} B"')
    w("                    )")
    w("                # The captured chunk lengths include trailing USB-")
    w("                # packet padding beyond the real payload (e.g. shading")
    w("                # uploads: 46080 B wire vs. 45856 B of real data) --")
    w("                # zero-pad a shorter computed value out to the same shape.")
    w("                padded = bytes(val) + bytes(total - len(val))")
    w("                pos = 0")
    w("                for idx in idxs:")
    w("                    n = len(out[idx].data)")
    w("                    out[idx] = replace(out[idx], data=padded[pos:pos + n])")
    w("                    pos += n")
    w("        return out")
    w("")
    w("")

    def emit_bytes_const(name, data: bytes):
        w(f"{name} = bytes.fromhex(")
        w(f'    "{data.hex()}"')
        w(")")
        w("")

    w("# ---------------------------------------------------------------- slope tables")
    w("# Two distinct 512 B motor slope tables (classic Genesys decreasing")
    w("# step-time curves). SLOPE_TABLE_POSITION is written to 0x1000c000 and")
    w("# 0x10010000 in the 'position' phase; SLOPE_TABLE_SCAN is written to")
    w("# 0x10000000, 0x10004000 and 0x10008000 (identical payload each time)")
    w("# in the 'scan' phase.")
    emit_bytes_const("SLOPE_TABLE_POSITION", slope_position)
    emit_bytes_const("SLOPE_TABLE_SCAN", slope_scan)

    slope_names = {slope_position: "SLOPE_TABLE_POSITION", slope_scan: "SLOPE_TABLE_SCAN"}

    def render_op(op, indent="    "):
        args = [repr(op["kind"]), repr(round(op["dt"], 4))]
        kw = []
        if op["bm"]:
            kw.append(f'bm={op["bm"]:#04x}')
        if op["br"]:
            kw.append(f'br={op["br"]:#04x}')
        if op["wv"]:
            kw.append(f'wv={op["wv"]:#06x}')
        if op["wi"]:
            kw.append(f'wi={op["wi"]:#06x}')
        if op["data"]:
            dname = slope_names.get(op["data"])
            kw.append(f"data={dname}" if dname else f"data=bytes.fromhex({op['data'].hex()!r})")
        if op["length"]:
            kw.append(f'length={op["length"]}')
        if op["resp"]:
            kw.append(f"resp=bytes.fromhex({op['resp'].hex()!r})")
        if op["dur"]:
            kw.append(f'dur={round(op["dur"], 4)}')
        return f"{indent}Op({', '.join(args + kw)}),"

    w("# ----------------------------------------------------------- image block")
    w("# The interior of every one of the 223 image-data buffer reads in")
    w("# 'scan' (519156 B each, one status cr + 33 bi chunks: 32x16384 B")
    w("# + a final shorter chunk) -- byte-identical every repeat. Reused")
    w("# via SCAN's generated loop below instead of embedding ~7500 Op()s.")
    w("IMAGE_BLOCK_OPS: list[Op] = [")
    for op in image_block_ops:
        w(render_op(op))
    w("]")
    w("")
    w("IMAGE_READ_ADDR = 0x10000000")
    w(f"IMAGE_DESC_DATA = bytes.fromhex({image_desc_data.hex()!r})   # [addr u32][len u32] LE, addr=0x10000000 len=519156")
    w("IMAGE_CHUNK_LEN = 519156          # one image-data bulk-read chunk (line-aligned)")
    w("IMAGE_CHUNK_COUNT = 223            # frame 1 @ 3600 dpi, 5137 lines")
    w("IMAGE_WIDTH = 3762                  # px/line, pixel-interleaved RGB16LE")
    w(f"IMAGE_TRAILING_DRAIN_ADDR = {trailing_addr:#010x}")
    w(f"IMAGE_TRAILING_DRAIN_LEN = {trailing_len}    # extra buffer drain after the 223 image chunks;")
    w("# purpose unclear (not part of the 115,771,788 B image per protocol-notes.md")
    w("# pass 6) -- included verbatim (as SCAN's tail ops) for wire fidelity, read")
    w("# and discarded.")
    w("")
    w("DEFAULT_LINES = 5137        # reg 0x26:0x27 in this trace (frame 1 @ 3600 dpi);")
    w("# an injection point (tables.SCAN.injections['lines_hi'/'lines_lo']) for")
    w("# future dpi/frame-height profiles -- see driver-design.md open items.")
    w("")
    w("")

    for p in phases:
        var = p["name"].upper()
        inj = injections.get(p["name"], {})
        w(f"_{var}_INJECTIONS = {inj!r}")
    w("")
    w("")

    w("PHASES: list[Phase] = []")
    w("")

    for p in phases:
        var = p["name"].upper()
        if p["name"] != "scan":
            w(f"{var} = Phase(")
            w(f'    name="{p["name"]}",')
            w(f"    op_range=({p['start']}, {p['end']}),")
            w("    ops=[")
            for op in p["phase_ops"]:
                w(render_op(op, indent="        "))
            w("    ],")
            w(f"    injections=_{var}_INJECTIONS,")
            split = p.get("split_at")
            if split is not None:
                w(f"    split_at={split},")
            w(")")
            w(f"PHASES.append({var})")
            w("")
        else:
            w("_scan_head: list[Op] = [")
            for op in scan_head:
                w(render_op(op, indent="    "))
            w("]")
            w("")
            w("_scan_tail: list[Op] = [")
            for op in scan_tail:
                w(render_op(op, indent="    "))
            w("]")
            w("")
            w("_scan_images: list[Op] = []")
            w("for _i in range(IMAGE_CHUNK_COUNT):")
            w("    _wi = 0x0008 if _i == 0 else 0")
            w('    _scan_images.append(Op("cw", 0.004, bm=0x40, br=0x04, wv=0x0082, wi=_wi, data=IMAGE_DESC_DATA))')
            w("    _scan_images.extend(IMAGE_BLOCK_OPS)")
            w("del _i, _wi")
            w("")
            w(f"{var} = Phase(")
            w(f'    name="{p["name"]}",')
            w(f"    op_range=({p['start']}, {p['end']}),")
            w("    ops=_scan_head + _scan_images + _scan_tail,")
            w(f"    injections=_{var}_INJECTIONS,")
            w(")")
            w(f"PHASES.append({var})")
            w("")

    w("")
    w("# --------------------------------------------------------------------- FEEDL")
    w("# FEEDL = target absolute position, counted in 1/7200 inch (HWDPI),")
    w("# from home. Verified in protocol-notes.md pass 3 against SilverFast:")
    w("# frame 1 = 6548 (base offset) there; this driver's own capture (this")
    w("# trace) used FEEDL=6743 for frame 1 -- a slightly different base")
    w("# offset/scan-window convention, kept as this trace's own ground")
    w("# truth. Pitch between frames (10760 steps = 38.0 mm film pitch) is")
    w("# shared across both observations.")
    w("FEEDL_FRAME1 = 6743")
    w("FEEDL_PITCH = 10760")
    w("")
    w("")
    w("def feedl_for_frame(frame: int) -> int:")
    w('    """Absolute FEEDL target for `frame` (1-based), from home."""')
    w("    return FEEDL_FRAME1 + (frame - 1) * FEEDL_PITCH")
    w("")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT} ({len(lines)} lines)")
    total = 0
    for p in phases:
        n = len(p["phase_ops"])
        total += n
        print(f"  {p['name']:22s} op[{p['start']:5d}:{p['end']:5d}]  ops={n:5d}"
              + (f"  split_at={p['split_at']}" if p.get('split_at') is not None else ""))
    print(f"  {'TOTAL':22s}                ops={total:5d}")


# ============================================================================
# Dual-light (alternating IR/visible line) trace compilation
#
# 2026-08-30: traces/04-singel-3600-IRpa.trace.json.gz (the IR-enabled
# 3600 dpi single-frame capture) -> of135i/tables_ir.py.
# 2026-09-02: four QuickScan whole-strip captures at 600/1200/2400/7200
# dpi (traces/20260902-vendor-<dpi>dpi.trace.json.gz) -> of135i/
# tables_dpi<dpi>.py. Those captures turned out to be IR-mode captures
# as well (QuickScan's IR dust removal was on): every calibration buffer
# and the image are alternating IR/visible lines, the shading table is
# uploaded to two addresses, exactly the trace-04 structure -- so one
# compiler (compile_dual) serves all five modules. See docs/protocol-
# notes.md pass 17/18 for the analysis.
#
# All five modules share the Op/Phase scaffold from of135i/tables.py and
# expose the same interface (see render in compile_dual): IMAGE_WIDTH,
# SHADING_LINES, LINES_PER_CHUNK, IMAGE_CHUNK_COUNT, DEFAULT_LINES,
# scan_phase(n_chunks), the phase objects, feedl_for_frame(), and the
# injection names gain_*, offset_*_hi/lo, feedl_*, lines_top/hi/lo,
# shading_table_a/_b (upload to 0x10014000 / 0x10034000) and
# shading_table2_a/_b (the re-upload). Table A is applied by the
# scanner to the EVEN (IR) lines, table B to the ODD (visible) lines
# -- established from the vendor's own uploads vs. its measurements
# (per-line-subset offsets match to ~1 count, and the second upload's
# gain = T*0x4000/white with T = 61440 for A / 90112 for B reproduces
# the vendor tables with cv 0.0003 on every capture, 3600 included;
# see calibrate.SHADING2_TARGET_A/_B).

# Phase op-index ranges for trace 04 (hand-derived 2026-08-30 with the
# same two anchors as main() -- buffer descriptors and 0f=01 execute
# pulses -- refined by each phase's characteristic AFE write; the
# per-phase op counts sum to exactly the trace's 23903 ops):
#   prep 0-57, afe_base 58-126 (base register table starts at 58, AFE
#   base at 66), cal_dark_a 127-155, cal_dark_b 156-184 (6144 B dark
#   reads: 2 alternating lines), cal_white 185-256 (62208 B: one IR +
#   one visible raw line at 5184 px), cal_gain_check_a 257-293 (computed
#   gain injected), cal_gain_check_b 294-322, cal_shading_measure
#   323-968 (7962624 B = 256 lines x 5184 px x 3 ch x 2 B, alternating;
#   final AFE offset injected at the LAST occurrence), cal_shading_upload
#   969-985 (two buffer-write descriptors 0x10014000 / 0x10034000, 63192
#   B / 5 bo chunks each), cal_shading_verify 986-1647 (re-measure +
#   re-upload to the same two addresses), position 1648-1693 (FEEDL
#   6746; the position slope table is byte-identical to tables.py's),
#   scan 1694-23765 (659 completed 497664 B image chunks = 16 lines each;
#   a 660th descriptor is issued then cancelled with no data -- kept
#   verbatim as the scan tail), park 23766-23902.
PHASE_BOUNDS_IR = [
    ("prep", 0, 57),
    ("afe_base", 58, 126),
    ("cal_dark_a", 127, 155),
    ("cal_dark_b", 156, 184),
    ("cal_white", 185, 256),
    ("cal_gain_check_a", 257, 293),
    ("cal_gain_check_b", 294, 322),
    ("cal_shading_measure", 323, 968),
    ("cal_shading_upload", 969, 985),
    ("cal_shading_verify", 986, 1647),
    ("position", 1648, 1693),
    ("scan", 1694, 23765),
    ("park", 23766, 23902),
]

# The DPI captures (QuickScan, whole strip, IR on). Vendor image geometry
# per capture -- pixel width and lines per image chunk -- is read off the
# trace (chunk length / (width*6)); the width itself is the vendor's
# per-dpi sensor readout (876/1752/5256/10512: full sensor at 600 and
# 1200, the 3600 dpi full-width readout at 2400 (resampled by the app),
# 2x that at 7200), checked against the captured chunk lengths below.
DPI_TRACES = {
    600: ("20260902-vendor-600dpi.trace.json.gz", 876),
    1200: ("20260902-vendor-1200dpi.trace.json.gz", 1752),
    2400: ("20260902-vendor-2400dpi.trace.json.gz", 5256),
    7200: ("20260902-vendor-7200dpi.trace.json.gz", 10512),
}

# Frame height for a single-frame dpi-profile scan, in PHYSICAL lines:
# trace 04's line-count register (10622 = 2 x 5311 alternating lines)
# covers frame 1 at 3600 dpi; scaled by dpi/3600 and rounded to whole
# image chunks (see compile_dual). The vendor's strip captures set
# 374 + 12004*dpi/600 lines instead (the whole magazine travel).
FRAME_PHYSICAL_LINES_3600 = 5311

SHADING_ADDR_A = 0x10014000   # applied to even (IR) lines
SHADING_ADDR_B = 0x10034000   # applied to odd (visible) lines


def group_bo_by_write_desc(phase_ops, write_descs, end):
    """Group bulk-OUT ops by which buffer-write descriptor precedes them.

    `write_descs` is a list of (local_index, 'write', addr, len) tuples
    (as returned by find_buf_descs, filtered to kind=='write'), in trace
    order. Returns [(addr, (bo_local_idx, ...)), ...] in the same order."""
    groups = []
    for k, (idx, _kind, addr, _ln) in enumerate(write_descs):
        next_idx = write_descs[k + 1][0] if k + 1 < len(write_descs) else end
        bos = tuple(i for i in range(idx, next_idx) if phase_ops[i]["kind"] == "bo")
        groups.append((addr, bos))
    return groups


def split_image_reads(phase_ops):
    """Split a scan phase into (head, image_descs, block_ops, tail).

    The image-data buffer reads share one op pattern: a cw wv=0x82 read
    descriptor followed by a fixed run of status cr + bi chunks. All
    reads whose following ops match the first read's pattern are image
    chunks; `tail` is everything after the last matching block --
    trace 04's cancelled 660th descriptor (see ir-analysis.md) or, for
    the strip captures, just the end-of-access that stops the engine
    with lines still pending. `head` is everything before the first
    image descriptor."""
    descs = find_buf_descs(phase_ops)
    reads = [d for d in descs if d[1] == "read"]
    if not reads:
        raise ValueError("split_image_reads: no buffer-read descriptors found")
    ref_pattern = None
    ref_block = None
    n_match = 0
    for k in range(len(reads)):
        i0 = reads[k][0]
        i1 = reads[k + 1][0] if k + 1 < len(reads) else len(phase_ops)
        block = phase_ops[i0 + 1:i1]
        pattern = tuple((o["kind"], o["length"]) for o in block)
        if ref_pattern is None:
            ref_pattern, ref_block = pattern, block
            n_match = 1
        elif pattern == ref_pattern:
            n_match += 1
        elif k + 1 == len(reads) and pattern[:len(ref_pattern)] == ref_pattern:
            # Last chunk followed by extra ops (no trailing descriptor):
            # the block matches, the rest is the tail.
            n_match += 1
            tail_start = i0 + 1 + len(ref_pattern)
            return phase_ops[:reads[0][0]], reads[:n_match], ref_block, phase_ops[tail_start:]
        else:
            break
    image_descs = reads[:n_match]
    if n_match == len(reads):
        tail_start = image_descs[-1][0] + 1 + len(ref_pattern)
    else:
        tail_start = reads[n_match][0]
    return phase_ops[:image_descs[0][0]], image_descs, ref_block, phase_ops[tail_start:]


# ---------------------------------------------------------------- alignment

def _landmarks(ops, lo, hi):
    """Structural landmark sequence of ops[lo:hi]: every control write
    (keyed by wValue/wIndex, plus the first register for a register
    batch) and every run of bulk-OUT ops collapsed to one entry; status
    reads/polls are skipped (their coalescing differs between captures).
    Returns [(op_index, key), ...]."""
    out = []
    last_bo = False
    for i in range(lo, hi):
        o = ops[i]
        if o["t"] == "cw":
            if o["wv"] == 0x83:
                key = ("cw83", o["data"][:2])
            elif o["wv"] == 0x82:
                key = ("cw82", o["wi"])
            else:
                key = ("cw", o["wv"], o["wi"])
            out.append((i, key))
            last_bo = False
        elif o["t"] == "bo":
            if not last_bo:
                out.append((i, ("bo",)))
            last_bo = True
        elif o["t"] == "bi":
            last_bo = False
    return out


def _align(ref_lm, lm, what):
    """Require ref_lm's keys to be a prefix of lm's keys; return the
    number matched (== len(ref_lm))."""
    n = 0
    while n < min(len(ref_lm), len(lm)) and ref_lm[n][1] == lm[n][1]:
        n += 1
    if n != len(ref_lm):
        got = lm[n] if n < len(lm) else None
        raise SystemExit(f"{what}: structural mismatch after {n}/{len(ref_lm)} landmarks "
                         f"(ref {ref_lm[n]} vs {got})")
    return n


def find_dpi_bounds(ops, ref_ops):
    """Derive a DPI trace's phase bounds mechanically by aligning it to
    trace 04 (ref_ops, bounds PHASE_BOUNDS_IR).

    Anchors (all checked, the alignment raises on any structural
    difference):
      - the base register table = first >8-pair batch containing the
        pair (0x02, 0x78) -> afe_base start; the first of the two
        end-of-access pairs (cw wv=0x8c wi=0x10) before it; prep starts three idle-loop
        iterations (batches "3295") before that -- the same lead-in as
        trace 04 -- or, when the capture starts mid-way (the 1200 dpi
        one begins with the app start), as many as exist;
      - from the first AFE base write (5100...) through the end of
        cal_shading_verify the landmark sequence must match trace 04
        op-for-op (bo runs collapsed: the uploads chunk differently);
        each trace-04 boundary maps to the DPI op of the same landmark;
      - the DPI captures have NO positioning move (a whole-strip scan
        from the loaded position): the exposure block (0a48 ...) is
        followed directly by the two position slope-table uploads and
        the three scan slope-table uploads. "position" is synthesized
        by compile_dual from the DPI exposure block + trace 04's FEEDL
        batch/execute/poll (see there); "scan" starts at the first scan
        slope-table upload (to 0x10000000);
      - park starts at the first end-of-access (cw wv=0x8d) after the
        image reads and is cut where trace 04's park ends (aligned).
    Returns {name: (start, end)} for every PHASE_BOUNDS_IR phase except
    position, plus "exposure" (start, end) and "pos_slopes" (start,
    end) for the two pieces compile_dual stitches into position.
    """
    ref_b = {n: (s, e) for n, s, e in PHASE_BOUNDS_IR}
    n_ops = len(ops)

    def batch_pairs(o):
        return pairs_of(bytes.fromhex(o["data"])) if o["data"] else []

    bt = next(i for i, o in enumerate(ops)
              if o["t"] == "cw" and o["wv"] == 0x83 and len(o["data"]) > 16
              and (0x02, 0x78) in batch_pairs(o))
    # Two end-of-access pairs precede the base table, with the 03=20/
    # 03=30/31=fe writes between them; prep starts before the FIRST pair.
    r0320 = max(i for i in range(bt) if ops[i]["t"] == "cw" and ops[i]["wv"] == 0x83 and ops[i]["data"] == "0320")
    ea = max(i for i in range(r0320) if ops[i]["t"] == "cw" and ops[i]["wv"] == 0x8c and ops[i]["wi"] == 0x10)
    idle = [i for i in range(ea) if ops[i]["t"] == "cw" and ops[i]["wv"] == 0x83 and ops[i]["data"] == "3295"]
    last_busy = max([i for i in range(ea) if ops[i]["t"] in ("bo", "bi")] or [-1])
    idle = [i for i in idle if i > last_busy]
    prep_start = idle[-3] if len(idle) >= 3 else (idle[0] if idle else ea)

    afe0 = next(i for i in range(bt, n_ops) if ops[i]["t"] == "cw" and ops[i]["wv"] == 0x83 and ops[i]["data"].startswith("5100"))
    ref_afe0 = next(i for i in range(ref_b["afe_base"][0], ref_b["afe_base"][1] + 1)
                    if ref_ops[i]["t"] == "cw" and ref_ops[i]["wv"] == 0x83 and ref_ops[i]["data"].startswith("5100"))
    ref_lm = _landmarks(ref_ops, ref_afe0, ref_b["cal_shading_verify"][1] + 1)
    lm = _landmarks(ops, afe0, n_ops)
    _align(ref_lm, lm, "calibration phases")

    def map_op(r):
        """DPI op index for trace-04 op r (within the aligned range)."""
        j = next(k for k, (i, _) in enumerate(ref_lm) if i >= r)
        return lm[j][0] - (ref_lm[j][0] - r)

    bounds = {"prep": (prep_start, bt - 1), "afe_base": (bt, map_op(ref_b["cal_dark_a"][0]) - 1)}
    names = ["cal_dark_a", "cal_dark_b", "cal_white", "cal_gain_check_a", "cal_gain_check_b",
             "cal_shading_measure", "cal_shading_upload", "cal_shading_verify"]
    for k, name in enumerate(names):
        s = map_op(ref_b[name][0])
        if k + 1 < len(names):
            e = map_op(ref_b[names[k + 1]][0]) - 1
        else:
            # verify ends right before the exposure block (0a48 batch).
            e = next(i for i in range(s, n_ops) if ops[i]["t"] == "cw" and ops[i]["wv"] == 0x83 and ops[i]["data"] == "0a48") - 1
        bounds[name] = (s, e)

    exp_start = bounds["cal_shading_verify"][1] + 1
    pos_desc0 = next(i for i in range(exp_start, n_ops) if ops[i]["t"] == "cw" and ops[i]["wv"] == 0x82
                     and ops[i]["wi"] == 1 and ops[i]["data"].startswith("00c00010"))
    scan_start = next(i for i in range(exp_start, n_ops) if ops[i]["t"] == "cw" and ops[i]["wv"] == 0x82
                      and ops[i]["wi"] == 1 and ops[i]["data"].startswith("00000010"))
    bounds["exposure"] = (exp_start, pos_desc0 - 1)
    bounds["pos_slopes"] = (pos_desc0, scan_start - 1)
    # Check the two pieces against trace 04's position phase minus its
    # FEEDL batch (+ its ack read) and its execute/poll trio.
    ref_pos = ref_b["position"]
    ref_feedl = _find_feedl_batch([make_op(ref_ops[i]) for i in range(ref_pos[0], ref_pos[1] + 1)]) + ref_pos[0]
    ref_exec = next(i for i in range(ref_feedl, ref_pos[1] + 1) if ref_ops[i]["t"] == "cw" and ref_ops[i]["data"] == "0f01")
    _align(_landmarks(ref_ops, ref_pos[0], ref_feedl), _landmarks(ops, exp_start, pos_desc0), "exposure block")
    _align(_landmarks(ref_ops, ref_feedl + 2, ref_exec), _landmarks(ops, pos_desc0, scan_start), "position slope uploads")

    park = next(i for i in range(scan_start, n_ops) if ops[i]["t"] == "cw" and ops[i]["wv"] == 0x8d)
    bounds["scan"] = (scan_start, park - 1)
    ref_park = _landmarks(ref_ops, ref_b["park"][0], ref_b["park"][1] + 1)
    park_lm = _landmarks(ops, park, n_ops)
    _align(ref_park, park_lm, "park")
    end = park_lm[len(ref_park) - 1][0] + (ref_b["park"][1] - ref_park[-1][0])
    bounds["park"] = (park, end)
    return bounds, (ref_feedl, ref_exec)


def _find_feedl_batch(phase_ops):
    """Local index of the positioning batch: the register batch carrying
    0x3d/0x3e/0x3f (FEEDL) together with (0x02, 0x18)."""
    for i, op in enumerate(phase_ops):
        if op["kind"] == "cw" and op["wv"] == 0x83:
            p = pairs_of(op["data"])
            regs = {r for r, _ in p}
            if 0x3D in regs and 0x3E in regs and 0x3F in regs and (0x02, 0x18) in p:
                return i
    raise SystemExit("FEEDL batch not found in position phase")


# ------------------------------------------------------------ compile_dual

def compile_dual(dpi):
    """Compile one dual-light trace into its of135i/tables_*.py module.

    dpi=3600: trace 04 with PHASE_BOUNDS_IR -> of135i/tables_ir.py.
    dpi in DPI_TRACES: the strip capture with find_dpi_bounds() ->
    of135i/tables_dpi<dpi>.py, its position phase synthesized as
        [DPI exposure block] + [trace 04 FEEDL batch + ack read]
        + [DPI position slope uploads] + [trace 04 execute + ack + poll]
    -- the positioning move is dpi-independent (FEEDL counts 1/7200 in;
    the position slope table is byte-identical in every capture) and
    the DPI scan batch that follows rewrites every motor/exposure
    register the trace-04 batch touched.
    """
    ref_ops = load_ops(TRACE_IR)
    if dpi == 3600:
        ops = ref_ops
        trace_path = TRACE_IR
        out = OUT_IR
        modname = "tables_ir"
        width = 5184
        phases = [build_phase(ops, name, s, e) for name, s, e in PHASE_BOUNDS_IR]
        position_note = "from the trace"
    else:
        fname, width = DPI_TRACES[dpi]
        trace_path = HERE.parent / "traces" / fname
        out = HERE.parent / "of135i" / f"tables_dpi{dpi}.py"
        modname = f"tables_dpi{dpi}"
        ops = load_ops(trace_path)
        bounds, (ref_feedl, ref_exec) = find_dpi_bounds(ops, ref_ops)
        phases = []
        for name, _, _ in PHASE_BOUNDS_IR:
            if name == "position":
                e_s, e_e = bounds["exposure"]
                p_s, p_e = bounds["pos_slopes"]
                pos_ops = ([make_op(ops[i]) for i in range(e_s, e_e + 1)]
                           + [make_op(ref_ops[i]) for i in (ref_feedl, ref_feedl + 1)]
                           + [make_op(ops[i]) for i in range(p_s, p_e + 1)]
                           + [make_op(ref_ops[i]) for i in range(ref_exec, ref_exec + 3)])
                phases.append(dict(name="position", start=e_s, end=p_e, phase_ops=pos_ops))
            else:
                s, e = bounds[name]
                phases.append(build_phase(ops, name, s, e))
        position_note = ("synthesized: this trace's exposure block and position slope uploads "
                         f"(ops {bounds['exposure'][0]}-{bounds['pos_slopes'][1]}) around trace 04's "
                         f"FEEDL batch (op {ref_feedl}) and execute/poll (ops {ref_exec}-{ref_exec + 2})")
    by_name = {p["name"]: p for p in phases}
    injections = {p["name"]: {} for p in phases}

    gc = by_name["cal_gain_check_a"]["phase_ops"]
    for inject_name, reg in GAIN_INJECT.items():
        i = find_afe_batch(gc, reg)
        injections["cal_gain_check_a"][inject_name] = ("byte", i, val_offset(gc[i]["data"], 0x5E))

    sm = by_name["cal_shading_measure"]["phase_ops"]
    for inject_name, reg in OFFSET_INJECT.items():
        i = find_afe_batch(sm, reg, last=True)
        injections["cal_shading_measure"][inject_name + "_hi"] = ("byte", i, val_offset(sm[i]["data"], 0x5D))
        injections["cal_shading_measure"][inject_name + "_lo"] = ("byte", i, val_offset(sm[i]["data"], 0x5E))

    pos = by_name["position"]["phase_ops"]
    feedl_i = _find_feedl_batch(pos)
    fb = pos[feedl_i]["data"]
    injections["position"] = {
        "feedl_hi": ("byte", feedl_i, val_offset(fb, 0x3D)),
        "feedl_mid": ("byte", feedl_i, val_offset(fb, 0x3E)),
        "feedl_lo": ("byte", feedl_i, val_offset(fb, 0x3F)),
    }
    pf = dict(pairs_of(fb))
    feedl_frame1 = (pf[0x3D] << 16) | (pf[0x3E] << 8) | pf[0x3F]

    sc = by_name["scan"]["phase_ops"]
    scan_head, image_descs, image_block_ops, scan_tail = split_image_reads(sc)
    lc_i = None
    for i, op in enumerate(scan_head):
        if op["kind"] == "cw" and op["wv"] == 0x83:
            p = pairs_of(op["data"])
            regs = {r for r, _ in p}
            if 0x26 in regs and 0x27 in regs and len(p) > 10:
                lc_i = i
                break
    if lc_i is None:
        raise SystemExit("line-count batch not found in scan head")
    lb = scan_head[lc_i]["data"]
    injections["scan"] = {
        "lines_hi": ("byte", lc_i, val_offset(lb, 0x26)),
        "lines_lo": ("byte", lc_i, val_offset(lb, 0x27)),
    }
    lp = dict(pairs_of(lb))
    if 0x25 in lp:
        injections["scan"]["lines_top"] = ("byte", lc_i, val_offset(lb, 0x25))
    captured_lines = (lp.get(0x25, 0) << 16) | (lp[0x26] << 8) | lp[0x27]

    # ---- shading upload / verify: two bo-payload groups (A/B) ---------
    ADDR_NAMES = {SHADING_ADDR_A: "shading_table_a", SHADING_ADDR_B: "shading_table_b"}
    ADDR2_NAMES = {SHADING_ADDR_A: "shading_table2_a", SHADING_ADDR_B: "shading_table2_b"}
    up = by_name["cal_shading_upload"]["phase_ops"]
    up_descs = [d for d in find_buf_descs(up) if d[1] == "write"]
    assert len(up_descs) == 2, up_descs
    upload_len = up_descs[0][3]
    for addr, bos in group_bo_by_write_desc(up, up_descs, len(up)):
        injections["cal_shading_upload"][ADDR_NAMES[addr]] = ("bo", bos)
    sv = by_name["cal_shading_verify"]["phase_ops"]
    sv_descs = find_buf_descs(sv)
    sv_reads = [d for d in sv_descs if d[1] == "read"]
    sv_writes = [d for d in sv_descs if d[1] == "write"]
    assert len(sv_reads) == 1 and len(sv_writes) == 2, sv_descs
    split_at = sv_writes[0][0]
    for addr, bos in group_bo_by_write_desc(sv, sv_writes, len(sv)):
        injections["cal_shading_verify"][ADDR2_NAMES[addr]] = ("bo", bos)
    by_name["cal_shading_verify"]["split_at"] = split_at

    # ---- geometry --------------------------------------------------------
    chunk_len = image_descs[0][3]
    for _, _, addr, ln in image_descs:
        assert addr == 0x10000000 and ln == chunk_len
    assert chunk_len % (width * 6) == 0, (chunk_len, width)
    lines_per_chunk = chunk_len // (width * 6)
    shading_len = by_name["cal_shading_measure"]["phase_ops"]
    shading_read = [d for d in find_buf_descs(shading_len) if d[1] == "read"][-1][3]
    assert shading_read % (width * 6) == 0, (shading_read, width)
    shading_lines = shading_read // (width * 6)
    total_block = sum(o["length"] for o in image_block_ops if o["kind"] == "bi")
    assert total_block == chunk_len, (total_block, chunk_len)
    image_desc_data = sc[image_descs[0][0]]["data"]
    wi_first = sc[image_descs[0][0]]["wi"]
    wi_rest = sc[image_descs[1][0]]["wi"]
    for idx, _, _, _ in image_descs[1:]:
        assert sc[idx]["wi"] == wi_rest
    desc_dt = round(sc[image_descs[0][0]]["dt"], 4)
    if dpi == 3600:
        chunk_count = len(image_descs)          # 659: the verified flow
        default_lines = captured_lines          # 10622
    else:
        frame_lines = 2 * round(FRAME_PHYSICAL_LINES_3600 * dpi / 3600)
        chunk_count = max(1, round(frame_lines / lines_per_chunk))
        default_lines = chunk_count * lines_per_chunk

    # ---- slope tables ----------------------------------------------------
    pos_bo_i = bo_indices(pos)
    assert len(pos_bo_i) == 2, pos_bo_i
    pos_bo = [pos[i]["data"] for i in pos_bo_i]
    assert pos_bo[0] == pos_bo[1]
    slope_position = pos_bo[0]
    scan_bo_i = [i for i, op in enumerate(scan_head) if op["kind"] == "bo"]
    assert len(scan_bo_i) == 3, scan_bo_i
    scan_bo = [scan_head[i]["data"] for i in scan_bo_i]
    assert scan_bo[0] == scan_bo[1] == scan_bo[2]
    slope_scan = scan_bo[0]
    slope_names = {slope_position: "SLOPE_TABLE_POSITION", slope_scan: "SLOPE_TABLE_SCAN"}

    # ---- render ------------------------------------------------------------
    lines = []
    w = lines.append
    w(f'"""Derived scan-sequence constants for the of135i driver -- {dpi} dpi,')
    w("dual-light (alternating IR/visible line) scan mode.")
    w("")
    w("AUTO-GENERATED by tools/gen_tables.py's compile_dual() from")
    w(f"{trace_path.relative_to(HERE.parent)} -- do not edit by hand.")
    w("Regenerate with: .venv/bin/python tools/gen_tables.py")
    w("")
    w("Same structure as of135i/tables.py (an ordered list of PHASES, each")
    w("its full verbatim op stream, replayed by device.py's executor), with")
    w("the dual-light specifics: every calibration buffer and the image are")
    w("ALTERNATING lines (even = IR pass, odd = visible pass) at the raw")
    w(f"sensor readout width ({width} px), and the shading table is uploaded")
    w("to two addresses -- A (0x10014000, applied to the even/IR lines) and")
    w("B (0x10034000, applied to the odd/visible lines). Injection names:")
    w("gain_r/g/b, offset_*_hi/lo, feedl_hi/mid/lo, lines_top/hi/lo,")
    w("shading_table_a/b and shading_table2_a/b. See tools/gen_tables.py")
    w("(compile_dual) for the phase derivation and docs/protocol-notes.md")
    w("pass 17/18 for the analysis.")
    w(f"Position phase: {position_note}.")
    w('"""')
    w("")
    w("from __future__ import annotations")
    w("")
    w("from .tables import Op, Phase")
    w("")
    w(f"DPI = {dpi}")
    w("")

    def emit_bytes_const(name, data: bytes):
        w(f"{name} = bytes.fromhex(")
        w(f'    "{data.hex()}"')
        w(")")
        w("")

    w("# ---------------------------------------------------------------- slope tables")
    w("# SLOPE_TABLE_POSITION (0x1000c000 / 0x10010000 in 'position') is the")
    w("# same in every capture; SLOPE_TABLE_SCAN (0x10000000/4000/8000 in")
    w("# 'scan') is this dpi's own scan-speed profile.")
    emit_bytes_const("SLOPE_TABLE_POSITION", slope_position)
    emit_bytes_const("SLOPE_TABLE_SCAN", slope_scan)

    def render_op(op, indent="    "):
        args = [repr(op["kind"]), repr(round(op["dt"], 4))]
        kw = []
        if op["bm"]:
            kw.append(f'bm={op["bm"]:#04x}')
        if op["br"]:
            kw.append(f'br={op["br"]:#04x}')
        if op["wv"]:
            kw.append(f'wv={op["wv"]:#06x}')
        if op["wi"]:
            kw.append(f'wi={op["wi"]:#06x}')
        if op["data"]:
            dname = slope_names.get(op["data"])
            kw.append(f"data={dname}" if dname else f"data=bytes.fromhex({op['data'].hex()!r})")
        if op["length"]:
            kw.append(f'length={op["length"]}')
        if op["resp"]:
            kw.append(f"resp=bytes.fromhex({op['resp'].hex()!r})")
        if op["dur"]:
            kw.append(f'dur={round(op["dur"], 4)}')
        return f"{indent}Op({', '.join(args + kw)}),"

    w("# ----------------------------------------------------------- image block")
    w(f"# The interior of every image-data buffer read in 'scan' ({chunk_len} B =")
    w(f"# {lines_per_chunk} alternating lines x {width} px x 3 ch x 2 B: one status cr + bi")
    w("# chunks), byte-identical every repeat; reused by scan_phase().")
    w("IMAGE_BLOCK_OPS: list[Op] = [")
    for op in image_block_ops:
        w(render_op(op))
    w("]")
    w("")
    w("IMAGE_READ_ADDR = 0x10000000")
    w(f"IMAGE_DESC_DATA = bytes.fromhex({image_desc_data.hex()!r})   # [addr u32][len u32] LE")
    w(f"IMAGE_CHUNK_LEN = {chunk_len}")
    w(f"IMAGE_WIDTH = {width}                  # px/line, pixel-interleaved RGB16LE, ALTERNATING even=IR/odd=visible lines")
    w(f"LINES_PER_CHUNK = {lines_per_chunk}")
    w(f"SHADING_LINES = {shading_lines}           # lines per shading measurement (alternating)")
    w(f"SHADING_UPLOAD_LEN = {upload_len}       # per address")
    w(f"IMAGE_DESC_WI_FIRST = {wi_first:#06x}         # capture fidelity: the first image descriptor's wIndex")
    w(f"IMAGE_DESC_WI_REST = {wi_rest:#06x}")
    w(f"IMAGE_DESC_DT = {desc_dt!r}")
    w(f"CAPTURED_LINES = {captured_lines}          # reg 0x25:0x26:0x27 in the trace")
    w(f"CAPTURED_CHUNKS = {len(image_descs)}        # image chunks completed in the trace")
    if dpi == 3600:
        w(f"IMAGE_CHUNK_COUNT = {chunk_count}         # the verified single-frame flow (see gen_tables.py)")
        w(f"DEFAULT_LINES = {default_lines}          # reg 0x26:0x27 as captured (frame 1 @ 3600 dpi, IR-enabled)")
    else:
        w(f"IMAGE_CHUNK_COUNT = {chunk_count}         # one frame: round(2*{FRAME_PHYSICAL_LINES_3600}*{dpi}/3600 / LINES_PER_CHUNK)")
        w(f"DEFAULT_LINES = {default_lines}          # IMAGE_CHUNK_COUNT * LINES_PER_CHUNK (alternating lines)")
    w("")
    w("")
    for p in phases:
        var = p["name"].upper()
        w(f"_{var}_INJECTIONS = {injections.get(p['name'], {})!r}")
    w("")
    w("")
    w("PHASES: list[Phase] = []")
    w("")
    for p in phases:
        var = p["name"].upper()
        if p["name"] != "scan":
            w(f"{var} = Phase(")
            w(f'    name="{p["name"]}",')
            w(f"    op_range=({p['start']}, {p['end']}),")
            w("    ops=[")
            for op in p["phase_ops"]:
                w(render_op(op, indent="        "))
            w("    ],")
            w(f"    injections=_{var}_INJECTIONS,")
            if p.get("split_at") is not None:
                w(f"    split_at={p['split_at']},")
            w(")")
            w(f"PHASES.append({var})")
            w("")
        else:
            w("_scan_head: list[Op] = [")
            for op in scan_head:
                w(render_op(op, indent="    "))
            w("]")
            w("")
            w("_scan_tail: list[Op] = [")
            for op in scan_tail:
                w(render_op(op, indent="    "))
            w("]")
            w("")
            w(f"_SCAN_OP_RANGE = ({p['start']}, {p['end']})")
            w("")
            w("")
            w("def scan_phase(n_chunks: int = IMAGE_CHUNK_COUNT) -> Phase:")
            w('    """The scan phase for n_chunks image-data reads: the captured head')
            w("    (slope tables, scan register batch with the line-count injection,")
            w("    execute), n_chunks x (descriptor + IMAGE_BLOCK_OPS), the captured")
            w('    tail. Line count to program = n_chunks * LINES_PER_CHUNK."""')
            w("    ops = list(_scan_head)")
            w("    for i in range(n_chunks):")
            w("        wi = IMAGE_DESC_WI_FIRST if i == 0 else IMAGE_DESC_WI_REST")
            w('        ops.append(Op("cw", IMAGE_DESC_DT, bm=0x40, br=0x04, wv=0x0082, wi=wi, data=IMAGE_DESC_DATA))')
            w("        ops.extend(IMAGE_BLOCK_OPS)")
            w("    ops.extend(_scan_tail)")
            w('    return Phase(name="scan", op_range=_SCAN_OP_RANGE, ops=ops, injections=_SCAN_INJECTIONS)')
            w("")
            w("")
            w("SCAN = scan_phase()")
            w("PHASES.append(SCAN)")
            w("")
    w("")
    w("# --------------------------------------------------------------------- FEEDL")
    w("# Absolute positioning target in 1/7200 in from home (tables.py's")
    w("# convention). FEEDL_FRAME1 is trace 04's captured frame-1 value (the")
    w("# dpi captures have no positioning move -- see the module docstring);")
    w("# FEEDL_PITCH (38.0 mm) is tables.FEEDL_PITCH.")
    w(f"FEEDL_FRAME1 = {feedl_frame1}")
    w("FEEDL_PITCH = 10760")
    w("")
    w("")
    w("def feedl_for_frame(frame: int) -> int:")
    w('    """Absolute FEEDL target for `frame` (1-based), from home."""')
    w("    return FEEDL_FRAME1 + (frame - 1) * FEEDL_PITCH")
    w("")

    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out} ({len(lines)} lines)")
    for p in phases:
        n = len(p["phase_ops"])
        print(f"  {p['name']:22s} op[{p['start']:6d}:{p['end']:6d}]  ops={n:6d}"
              + (f"  split_at={p['split_at']}" if p.get('split_at') is not None else ""))
    print(f"  width {width}, {lines_per_chunk} lines/chunk, chunks {chunk_count} (captured {len(image_descs)}), "
          f"lines {default_lines} (captured {captured_lines}), shading {shading_lines} lines, upload {upload_len} B")


if __name__ == "__main__":
    main()
    for _dpi in (3600, *sorted(DPI_TRACES)):
        compile_dual(_dpi)

# --------------------------------------------------------------------- NOTES
#
# Phase boundaries and injection points are unchanged from the
# original (slimmed-execution) version of this file -- only *what*
# gets captured per phase changed (the full op stream, not just
# wv=0x83/0x82 cw ops). See git history for the original derivation
# notes on the boundary choices themselves.
