"""Inventory of engine-status register reads in the op tables (offline).

For every Phase in every of135i.tables* module, lists each read of
reg 0x100 (wv=0x018e wi=0x0022, data enable) and reg 0x101 (wi=0x0122,
motor enable), the nearest preceding 0x0083 register batch and whether
it set/cleared SCAN (reg 0x01 bit 0) or carried the execute pulse
(0x0f=0x01), and whether a `poll` of the same register sits between.
Section 4 of the output is the list of "read once after a stop/start,
no wait" cases. The judgement of those is docs/status-read-inventory.md.

Usage (from anywhere; never touches a scanner):
    python3 tools/status_read_inventory.py [OUT.md]
With no argument the markdown goes to stdout.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import importlib, collections
from of135i.tables import Phase
MODS = ["tables","tables_dpi600","tables_dpi1200","tables_dpi2400","tables_dpi7200","tables_ir","tables_load","tables_base"]
REG = {0x0022: 0x100, 0x0122: 0x101}
notes = []

def phases(m):
    out = {}
    for n in dir(m):
        v = getattr(m, n)
        if isinstance(v, Phase) and not n.startswith("_"):
            out.setdefault(id(v), (n, v))
    res = {v.name if False else n: v for n, v in out.values()}
    if hasattr(m, "scan_phase"):
        sp = m.scan_phase()
        if "SCAN" in res and res["SCAN"].ops == sp.ops:
            notes.append(f"{m.__name__}: scan_phase() default has ops identical to module SCAN; listed once as SCAN")
        else:
            res["scan_phase()"] = sp
    return res

def pairs(data):
    if len(data) % 2:
        notes.append(f"odd-length 0x0083 data {data.hex()}")
    return [(data[i], data[i+1]) for i in range(0, len(data) - len(data) % 2, 2)]

def classify(data):
    ps = pairs(data); tags = []
    for r, v in ps:
        if r == 0x01:
            tags.append("SCAN-set" if v & 1 else "SCAN-clear")
    if (0x0f, 0x01) in ps: tags.append("exec-pulse")
    return tags or ["other-regs"], ps

def summ(ps):
    s = " ".join(f"{r:02x}={v:02x}" for r, v in ps)
    return s if len(s) <= 60 else s[:57] + "..."

def opsum(op):
    if op.kind == "cw":
        if op.wv == 0x0083:
            t, ps = classify(op.data); return f"cw83 [{','.join(t)}] {summ(ps)}"
        return f"cw wv={op.wv:#06x} wi={op.wi:#06x} len={len(op.data)}"
    if op.kind in ("cr", "poll"):
        return f"{op.kind} wv={op.wv:#06x} wi={op.wi:#06x} resp={op.resp.hex()}"
    return f"{op.kind} len={len(op.data) if op.kind=='bo' else op.length}"

rows = []; counts = collections.OrderedDict()
for mn in MODS:
    m = importlib.import_module("of135i." + mn)
    ph = phases(m)
    if mn == "tables_base":
        notes.append("tables_base exposes no Phase/Op lists (only BASE_INIT_PAIRS/COLD_INIT_PAIRS/AFE_BASE_PAIRS/LOADER_SPEED_PAIRS, register pairs, no reads); 0 phases scanned.")
    for pn, p in ph.items():
        c = collections.Counter()
        for i, op in enumerate(p.ops):
            if op.kind == "cw" and op.wv == 0x0083:
                t, _ = classify(op.data)
                for x in t:
                    if x != "other-regs": c[x] += 1
            if op.kind in ("cr", "poll") and op.wv == 0x018e and op.wi in REG:
                reg = REG[op.wi]; c[f"{op.kind}{reg:x}"] += 1
                j = next((k for k in range(i-1, -1, -1) if p.ops[k].kind == "cw" and p.ops[k].wv == 0x0083), None)
                if j is None:
                    wi_, tags, summary, between, bulk, polled = "-", ["none"], "(no preceding 0x0083 in phase)", "-", "-", False
                else:
                    tags, ps = classify(p.ops[j].data); wi_ = j; summary = summ(ps)
                    between = i - j - 1
                    bulk = any(o.kind in ("bi", "bo") for o in p.ops[j+1:i])
                    polled = any(o.kind == "poll" and o.wv == 0x018e and o.wi == op.wi for o in p.ops[j+1:i])
                nxt = opsum(p.ops[i+1]) if i + 1 < len(p.ops) else "(end of phase)"
                strict = ("strict" if op.wv == 0x018e else "exact") if op.kind == "poll" else ""
                rows.append(dict(mod=mn, ph=pn, i=i, kind=op.kind, reg=reg, resp=op.resp.hex(), dur=op.dur if op.kind=="poll" else None,
                                 strict=strict, wi=wi_, tags=tags, summ=summary, between=between, bulk=bulk, polled=polled, nxt=nxt, wrop=op))
        counts[(mn, pn)] = (len(p.ops), c)

def f(r):
    return (f"| {r['ph']} | {r['i']} | {r['kind']} | {r['reg']:#x} | {r['resp']} | "
            f"{'' if r['dur'] is None else f'{r[chr(100)+chr(117)+chr(114)]:.4g}'} | {r['strict']} | {r['wi']} | {','.join(r['tags'])} | `{r['summ']}` | "
            f"{r['between']} | {r['bulk']} | {r['nxt']} |")

out = ["# Status register (0x100/0x101) read inventory\n"]
out.append("""## 1. Method and verified assumptions

- Script: `status_read_inventory.py` (same directory). Imports each `of135i.tables*` module, collects module-level `Phase` instances (public names; aliases of the same object deduplicated) plus `scan_phase()` with its default argument (reported as `scan_phase()`; module `SCAN` is the same default and is deduplicated or listed separately by name if it is a distinct object).
- Read = op kind `cr` or `poll` with wv=0x018e and wi in {0x0022 -> reg 0x100, 0x0122 -> reg 0x101}.
- Preceding write = nearest earlier op in the same phase with kind `cw` and wv=0x0083. Pairs verified: `data` is consecutive 2-byte [reg,value] pairs (e.g. `36fc` = 0x36=0xfc; `e000e168` = 0xe0=0x00, 0xe1=0x68); every 0x0083 payload checked for even length (see anomalies).
- Tags: SCAN-set = pair reg 0x01 with bit0=1; SCAN-clear = reg 0x01 bit0=0; exec-pulse = pair (0x0f,0x01); other-regs = none of those. A write can carry several tags.
- "between" = number of ops strictly between the write and the read (any kind). "bulk" = any bi/bo among them. Next op is the op at index+1 in the same phase.
- Strictness rule (`Scanner._strict_status_polls`, device.py:1871): a poll index is strict iff `op.kind == 'poll' and op.wv == 0x018E` (any wi). This is a rule only for callers that pass `strict_polls=`: JOG, LOAD (load_magazine) and POSITION (call sites device.py:1910, 1998, 2571, 2879, 3112, 3245). All other phases run `_exec_ops` with empty strict_polls, so every poll there is non-strict: `_poll_one` floor = max(3*dur*scale, NONSTRICT_POLL_FLOOR_S=0.3 s) instead of 1.0 s, and non-strict polls that time out only log. In the tables the `strict?` column shows "strict-if-run-with-rule" (rule applies) for polls and is only effective in JOG/LOAD/POSITION. A `cr` never waits (single read, mismatch logged only). OPEN is run via `_run_phase` without that rule at the call sites grepped (jog/load only use the rule for JOG/LOAD).
- Also: `_exec_ops` does not wait after an exec pulse (comment in device.py:846-852), so the table's own polls provide any waiting.
""")
for mn in MODS:
    out.append(f"## 2. Module `{mn}`\n")
    rs = [r for r in rows if r["mod"] == mn]
    if not rs: out.append("(no reads)\n"); continue
    out.append("| phase | idx | kind | reg | resp | dur s | strict? | prev cw83 idx | prev tags | prev pairs | ops between | bulk between | next op |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rs:
        r2 = dict(r); 
        d = "" if r["dur"] is None else f"{r['dur']:.4g}"
        s = "strict-if-run-with-rule" if r["strict"] == "strict" else r["strict"]
        out.append(f"| {r['ph']} | {r['i']} | {r['kind']} | {r['reg']:#x} | {r['resp']} | {d} | {s} | {r['wi']} | {','.join(r['tags'])} | `{r['summ']}` | {r['between']} | {r['bulk']} | {r['nxt']} |")
    out.append("")
out.append("## 3. Per-phase counts\n")
out.append("| module | phase | ops | cr 0x100 | cr 0x101 | poll 0x100 | poll 0x101 | SCAN-set | SCAN-clear | exec-pulse |")
out.append("|---|---|---|---|---|---|---|---|---|---|")
for (mn, pn), (n, c) in counts.items():
    out.append(f"| {mn} | {pn} | {n} | {c['cr100']} | {c['cr101']} | {c['poll100']} | {c['poll101']} | {c['SCAN-set']} | {c['SCAN-clear']} | {c['exec-pulse']} |")
sel = [r for r in rows if r["kind"] == "cr" and ("SCAN-clear" in r["tags"] or "exec-pulse" in r["tags"]) and not r["polled"]]
out.append(f"\n## 4. cr reads after SCAN-clear/exec-pulse with no same-register poll in between ({len(sel)})\n")
out.append("| module | phase | idx | reg | resp | prev cw83 idx | prev tags | prev pairs | ops between | bulk between | next op |")
out.append("|---|---|---|---|---|---|---|---|---|---|---|")
for r in sel:
    out.append(f"| {r['mod']} | {r['ph']} | {r['i']} | {r['reg']:#x} | {r['resp']} | {r['wi']} | {','.join(r['tags'])} | `{r['summ']}` | {r['between']} | {r['bulk']} | {r['nxt']} |")
out.append("\n## 5. Parse anomalies / notes\n")
for n in sorted(set(notes)): out.append(f"- {n}")
nr = [r for r in rows if r['wi'] == '-']
out.append(f"- reads with no preceding 0x0083 in their phase: {len(nr)}")
out.append(f"\nTotal rows: {len(rows)}")
text = "\n".join(out) + "\n"
if len(sys.argv) > 1:
    open(sys.argv[1], "w").write(text)
else:
    sys.stdout.write(text)
print("rows", len(rows), "list4", len(sel), "notes", sorted(set(notes)), "nopreceding", len(nr), file=sys.stderr)
print("phases:", len(counts), file=sys.stderr)
