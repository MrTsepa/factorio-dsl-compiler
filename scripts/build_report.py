#!/usr/bin/env python
"""Generate a single self-contained HTML report (report.html) for the fgr compiler.

For every example and a battery of stress-test graphs it: compiles the DSL, runs
the verifier, renders the layout via Factorio-FBSR, and embeds the PNG (base64) +
the verifier verdict + the source. Needs the FBSR service running for images:

    ( cd ../factorio-patch-prediction && scripts/fbsr_service.sh & )
    .venv/bin/python scripts/build_report.py
"""
from __future__ import annotations

import base64
import html
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")) if (ROOT / "src").exists() else sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT))

from fgr.dsl import DslError, parse                       # noqa: E402
from fgr.layout import LayoutError, compile_graph         # noqa: E402
from fgr.verify import verify                             # noqa: E402
from fgr.blueprint import to_blueprint_string             # noqa: E402
from fgr import fbsr_validation                           # noqa: E402

try:
    from fgr.render import RenderError, render_blueprint_string
except Exception:  # pragma: no cover
    RenderError = Exception
    render_blueprint_string = None

TMP = Path("/private/tmp/claude-501") / "_fgr_report"
TMP.mkdir(parents=True, exist_ok=True)


# ---- stress-graph builders (mirror scratchpad/stress.py) -------------------------
REC, ITEM = "iron-gear-wheel", "iron-plate"


def _multi_in_dedicated(k):
    return "\n".join([f"input in{i} : {ITEM}" for i in range(k)] + [f"assembler asm : {REC}", "output out"]
                     + [f"in{i} -> asm" for i in range(k)] + ["asm -> out"])


def _multi_in_merged(k):
    return "\n".join([f"input in{i} : {ITEM}" for i in range(k)] + [f"assembler asm : {REC}", "output out"]
                     + [", ".join(f"in{i}" for i in range(k)) + " -> asm", "asm -> out"])


def _fanout_shared(k):
    return "\n".join(["input src : " + ITEM] + [f"output o{i}" for i in range(k)]
                     + ["src -> " + ", ".join(f"o{i}" for i in range(k))])


def _fanout_dedicated(k):
    return "\n".join(["input src : " + ITEM] + [f"output o{i}" for i in range(k)]
                     + [f"src -> o{i}" for i in range(k)])


def _deep_chain(k):
    return "\n".join(["input src : " + ITEM] + [f"assembler a{i} : {REC}" for i in range(k)] + ["output out"]
                     + ["src -> a0"] + [f"a{i} -> a{i+1}" for i in range(k - 1)] + [f"a{k-1} -> out"])


def _wide_parallel(k):
    L = []
    for i in range(k):
        L += [f"input in{i} : {ITEM}", f"assembler a{i} : {REC}", f"output o{i}",
              f"in{i} -> a{i}", f"a{i} -> o{i}"]
    return "\n".join(L)


def _bipartite(m, n):
    L = ["input feed : " + ITEM] + [f"assembler s{i} : {REC}" for i in range(m)] \
        + [f"assembler c{j} : {REC}" for j in range(n)] + [f"output out{j}" for j in range(n)]
    L += ["feed -> " + ", ".join(f"s{i}" for i in range(m))]
    L += [f"s{i} -> c{j}" for i in range(m) for j in range(n)]
    L += [f"c{j} -> out{j}" for j in range(n)]
    return "\n".join(L)


def _ratio():
    L = ["input copper : copper-plate", "input iron : iron-plate"] \
        + [f"assembler cable{i} : copper-cable" for i in range(3)] \
        + [f"assembler circ{j} : electronic-circuit" for j in range(2)] \
        + ["output chips0", "output chips1", "copper -> cable0, cable1, cable2"]
    L += [f"cable{i} -> circ0, circ1" for i in range(3)]
    L += ["iron -> circ0, circ1", "circ0 -> chips0", "circ1 -> chips1"]
    return "\n".join(L)


def _full_ports():
    return "\n".join([f"input in{i} : {ITEM}" for i in range(3)] + ["assembler hub : " + REC]
                     + [f"output o{i}" for i in range(3)]
                     + [f"in{i} -> hub" for i in range(3)] + [f"hub -> o{i}" for i in range(3)])


STRESS = (
    [(f"multi_in_dedicated[{k}]", _multi_in_dedicated(k)) for k in (2, 3, 4, 5)]
    + [(f"multi_in_merged[{k}]", _multi_in_merged(k)) for k in (2, 3, 4, 5)]
    + [(f"fanout_shared[{k}]", _fanout_shared(k)) for k in (2, 3, 5, 8)]
    + [(f"fanout_dedicated[{k}]", _fanout_dedicated(k)) for k in (2, 3)]
    + [(f"deep_chain[{k}]", _deep_chain(k)) for k in (3, 6, 10, 15)]
    + [(f"wide_parallel[{k}]", _wide_parallel(k)) for k in (3, 6, 12)]
    + [(f"bipartite[{m}x{n}]", _bipartite(m, n)) for (m, n) in ((2, 2), (3, 3), (4, 4))]
    + [("ratio_3wire_2circuit", _ratio()), ("full_ports_3x3", _full_ports())]
)


# ---- compile / verify / render --------------------------------------------------
def _extent(lay):
    xs = [t[0] for e in lay.entities for t in e.tiles()]
    ys = [t[1] for e in lay.entities for t in e.tiles()]
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if xs else (0, 0)


def process(name, text, render=True):
    """Return a dict with compile/verify/render results for one DSL graph."""
    rec = {"name": name, "source": text}
    t0 = time.time()
    try:
        g = parse(text)
        rec["nodes"], rec["edges"] = len(g.nodes), len(g.edges)
        lay = compile_graph(g)
        rep = verify(g, lay)
        rec["ms"] = (time.time() - t0) * 1000
        rec["entities"] = len(lay.entities)
        rec["grid"] = "%dx%d" % _extent(lay)
        rec["checks"] = [(c.name, c.ok, c.detail, c.severity) for c in rep.checks]
        rec["status"] = "PASS" if rep.ok else "VERIFY-FAIL"
        if render and rep.ok and render_blueprint_string is not None:
            try:
                out = TMP / (name.replace("[", "_").replace("]", "").replace("x", "x") + ".png")
                render_blueprint_string(to_blueprint_string(lay, name), out)
                rec["png"] = base64.b64encode(out.read_bytes()).decode()
            except RenderError as ex:
                rec["render_err"] = str(ex)[:200]
    except (DslError, LayoutError) as ex:
        rec["ms"] = (time.time() - t0) * 1000
        rec["status"] = "ERROR"
        rec["error"] = f"{type(ex).__name__}: {ex}"
    return rec


# ---- HTML emission --------------------------------------------------------------
CSS = """
:root{--bg:#1c1f24;--card:#262b33;--ink:#e8eaed;--mut:#9aa3af;--ok:#4caf50;--fail:#e05656;
--warn:#d6a93a;--line:#3a414b;--acc:#ffb454}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:32px 24px 80px}
h1{font-size:30px;margin:0 0 4px}h2{font-size:22px;margin:44px 0 14px;border-bottom:1px solid var(--line);
padding-bottom:8px}h3{font-size:17px;margin:0 0 8px}
.sub{color:var(--mut);margin:0 0 8px}
a{color:var(--acc)}code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:#15171b;border:1px solid var(--line);border-radius:8px;padding:12px 14px;
overflow:auto;font-size:13px;margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:18px 0}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700}
.b-PASS{background:rgba(76,175,80,.18);color:var(--ok)}
.b-VERIFY-FAIL{background:rgba(224,86,86,.18);color:var(--fail)}
.b-ERROR{background:rgba(214,169,58,.18);color:var(--warn)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
img{width:100%;border-radius:8px;border:1px solid var(--line);background:#0d0f12;display:block}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600}tr:hover td{background:rgba(255,255,255,.02)}
.mono{font-family:ui-monospace,monospace}.muted{color:var(--mut)}
.chk{font-size:13px;margin:2px 0}.chk .m{font-family:ui-monospace,monospace;font-weight:700}
.ok{color:var(--ok)}.fail{color:var(--fail)}.warn{color:var(--warn)}
.pillrow{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
.pill{background:#15171b;border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:13px}
.pill b{color:var(--acc);font-size:18px}
details summary{cursor:pointer;color:var(--mut);font-size:13px;margin-top:8px}
"""


def esc(s):
    return html.escape(str(s))


def checks_html(rec):
    out = []
    for nm, ok, detail, sev in rec.get("checks", []):
        cls = "ok" if ok else ("fail" if sev == "error" else "warn")
        mark = "ok " if ok else ("FAIL" if sev == "error" else "warn")
        d = f" — <span class='muted'>{esc(detail)}</span>" if detail else ""
        out.append(f"<div class='chk'><span class='m {cls}'>[{mark}]</span> {esc(nm)}{d}</div>")
    return "".join(out)


def example_card(rec):
    s = rec["status"]
    img = (f"<img src='data:image/png;base64,{rec['png']}' alt='{esc(rec['name'])}'>"
           if rec.get("png") else
           f"<pre>{esc(rec.get('error') or rec.get('render_err') or 'no image')}</pre>")
    meta = (f"{rec.get('nodes','?')} nodes · {rec.get('edges','?')} lanes · "
            f"{rec.get('entities','?')} entities · {rec.get('grid','?')} tiles · {rec.get('ms',0):.0f} ms")
    return f"""
    <div class='card'>
      <h3>{esc(rec['name'])} <span class='badge b-{s}'>{s}</span></h3>
      <div class='sub'>{meta}</div>
      <div class='grid2'>
        <div>{img}</div>
        <div>
          <pre>{esc(rec['source'])}</pre>
          <div style='margin-top:10px'>{checks_html(rec)}</div>
        </div>
      </div>
    </div>"""


def stress_row(rec):
    s = rec["status"]
    detail = rec.get("error", "")
    if not detail and s == "VERIFY-FAIL":
        detail = "; ".join(n for n, ok, d, sev in rec["checks"] if not ok and sev == "error")
    return (f"<tr><td class='mono'>{esc(rec['name'])}</td>"
            f"<td>{rec.get('nodes','')}</td><td>{rec.get('edges','')}</td>"
            f"<td>{rec.get('entities','')}</td><td class='mono'>{rec.get('grid','')}</td>"
            f"<td>{rec.get('ms',0):.0f}</td>"
            f"<td><span class='badge b-{s}'>{s}</span> <span class='muted'>{esc(detail)}</span></td></tr>")


def build():
    print("compiling + rendering examples ...")
    ex_files = ["gears", "circuits", "science", "fanout", "bus", "merge"]
    examples = [process(f, (ROOT / "examples" / f"{f}.fgr").read_text()) for f in ex_files]

    print("running stress battery ...")
    stress = [process(name, text) for name, text in STRESS]

    print("validate-model ...")
    try:
        vm = fbsr_validation.validate()
        vm_html = "".join(
            f"<div class='chk'><span class='m {'ok' if c.ok else 'fail'}'>"
            f"[{'ok ' if c.ok else 'FAIL'}]</span> {esc(c.name)}"
            + (f" — <span class='muted'>{esc(c.detail)}</span>" if c.detail else "") + "</div>"
            for c in vm)
    except Exception as ex:
        vm_html = f"<p class='muted'>validate-model unavailable: {esc(ex)}</p>"

    n_pass = sum(1 for r in stress if r["status"] == "PASS")
    n_ex_pass = sum(1 for r in examples if r["status"] == "PASS")

    parts = [f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>fgr compiler report</title><style>{CSS}</style></head><body><div class='wrap'>
<h1>fgr — Factorio DSL compiler report</h1>
<p class='sub'>A high-level DSL compiles to a real Factorio layout; a generator-agnostic
verifier traces material flow to confirm it realizes the spec; FBSR renders it.</p>
<div class='pillrow'>
  <div class='pill'><b>{n_ex_pass}/{len(examples)}</b><br>examples pass</div>
  <div class='pill'><b>{n_pass}/{len(stress)}</b><br>stress cases pass</div>
  <div class='pill'><b>{sum(r.get('entities',0) for r in examples+stress)}</b><br>entities placed</div>
</div>

<h2>How it works</h2>
<div class='card'>
<p>You write a production graph. The compiler places machines, inserters, belts,
splitters and underground crossings; the verifier independently checks the placed
tiles really carry items along every declared lane and nothing spurious.</p>
<pre>.fgr DSL ──parse──▶ Graph(spec) ──generate──▶ Layout ──verify──▶ PASS/FAIL ──▶ FBSR ──▶ PNG</pre>
<p class='muted' style='margin-bottom:0'>Lane shapes:
<code>A -&gt; B</code> dedicated belt · <code>A -&gt; B -&gt; C</code> chain ·
<code>A -&gt; B, C</code> one belt split to many (splitter bus) ·
<code>A, B -&gt; C</code> many sources merged onto one belt. Inputs are infinity chests
stocked with their item.</p>
</div>

<h2>Verifier model vs. Factorio data (FBSR)</h2>
<div class='card'>{vm_html}</div>

<h2>Examples</h2>
{''.join(example_card(r) for r in examples)}

<h2>Stress test</h2>
<p class='sub'>Hard graphs — wide multi-in/out, deep chains, dense many-to-many, large
fan-out/merge — run through compile + verify to find where the generator breaks.</p>
<div class='card'><table>
<tr><th>case</th><th>nodes</th><th>lanes</th><th>ents</th><th>grid</th><th>ms</th><th>outcome</th></tr>
{''.join(stress_row(r) for r in stress)}
</table></div>

<h3>Bottlenecks surfaced</h3>
<div class='card'>
<p><b>A · Ports — solved.</b> Inserters use a node's whole perimeter (12 tiles on a 3×3,
4 on a chest), so wide multi-in/out no longer hits a one-side cap.</p>
<p><b>B · Routing — now rip-up/retry.</b> Lanes route with A* + rip-up/retry + congestion
history, plus underground tunnels and full-perimeter ports, so dense many-to-many (a 4×4
crossbar) and ratio builds now route. It's still heuristic: very tight graphs could exhaust
the rip-up budget, and layouts are valid but not minimal.</p>
<p><b>C · No quantity/ratio in the DSL (modeling gap).</b> "3 wire → 2 circuits" is a
throughput ratio, not a topology; it must be hand-instantiated as a dense graph that then
hits A and B — and connectivity verification doesn't check rates anyway.</p>
<p class='muted' style='margin-bottom:0'>Scaled fine: deep chains (15), wide parallel (12),
fan-out/merge (8), full 3-in/3-out. No perf cliff (&lt;50 ms).</p>
</div>

<h2>Renders of selected passing stress cases</h2>
{''.join(example_card(r) for r in stress if r.get("png"))}

</div></body></html>"""]

    out = ROOT / "report.html"
    out.write_text("".join(parts))
    print(f"wrote {out} ({out.stat().st_size//1024} KB)  — examples {n_ex_pass}/{len(examples)}, stress {n_pass}/{len(stress)}")


if __name__ == "__main__":
    build()
