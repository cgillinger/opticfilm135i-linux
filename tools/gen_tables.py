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
OUT = HERE / "of135i" / "tables.py"


def load_ops():
    with gzip.open(TRACE, "rt") as fh:
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


if __name__ == "__main__":
    main()

# --------------------------------------------------------------------- NOTES
#
# Phase boundaries and injection points are unchanged from the
# original (slimmed-execution) version of this file -- only *what*
# gets captured per phase changed (the full op stream, not just
# wv=0x83/0x82 cw ops). See git history for the original derivation
# notes on the boundary choices themselves.
