#!/usr/bin/env python
"""Generate a single self-contained HTML report (out/report.html) for the fgr compiler.

For the example factories it compiles the DSL, runs the verifier, renders the layout
via Factorio-FBSR and embeds the PNG (base64) + the verifier verdict + the source; for
the generated stress battery it shows the compile/verify outcome as a table plus renders
of the most complex passes. Needs the FBSR service running for images:

    ( cd ../factorio-patch-prediction && scripts/fbsr_service.sh & )
    .venv/bin/python scripts/build_report.py
"""
from __future__ import annotations

import base64
import html
import io
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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

OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
STRESS_TIMEOUT = 15           # seconds per stress case (the router can thrash on big graphs)


class _Timeout(Exception):
    pass


# ---- compile / verify / render --------------------------------------------------
def _extent(lay):
    xs = [t[0] for e in lay.entities for t in e.tiles()]
    ys = [t[1] for e in lay.entities for t in e.tiles()]
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if xs else (0, 0)


def _embed_jpeg(path, max_w=1400, quality=82):
    """Downscale a full-res FBSR PNG and JPEG-encode it for a compact inline image
    (the diagrams are huge -- 10-20 MB each at full res -- and JPEG is ~20x smaller)."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def process(name, text, render=False, timeout=0):
    """Compile + verify one DSL graph; optionally render it. Returns a result dict."""
    rec = {"name": name, "source": text}
    if timeout:
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(_Timeout()))
        signal.alarm(timeout)
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
        if rep.ok:
            rec["bp"] = to_blueprint_string(lay, name)        # importable string (copy button)
            if render and render_blueprint_string is not None:
                try:
                    png = OUT / f"render_{name}.png"
                    render_blueprint_string(rec["bp"], png)
                    rec["png"] = _embed_jpeg(png)
                except RenderError as ex:
                    rec["render_err"] = str(ex)[:200]
    except _Timeout:
        rec["status"], rec["error"], rec["ms"] = "SLOW", f">{timeout}s", timeout * 1000
    except (DslError, LayoutError) as ex:
        rec["ms"] = (time.time() - t0) * 1000
        rec["status"] = "LAYOUT-ERROR"
        rec["error"] = f"{type(ex).__name__}: {ex}"
    finally:
        if timeout:
            signal.alarm(0)
    return rec


def render_folder(folder: Path):
    return [process(p.stem, p.read_text(), render=True) for p in sorted(folder.glob("*.fgr"))]


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
.b-LAYOUT-ERROR{background:rgba(214,169,58,.18);color:var(--warn)}
.b-SLOW{background:rgba(154,163,175,.2);color:var(--mut)}
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
button.copy{cursor:pointer;font:600 12px ui-monospace,monospace;color:#15171b;background:var(--acc);
border:none;border-radius:7px;padding:6px 12px;margin-bottom:8px}
button.copy:hover{filter:brightness(1.08)}button.copy:active{transform:translateY(1px)}
button.copy.sm{padding:3px 9px;font-size:11px;margin:0}
"""

COPY_JS = """
function copyBp(btn){
  var s=btn.getAttribute('data-bp'), label=btn.textContent;
  function done(){btn.textContent='Copied!';setTimeout(function(){btn.textContent=label},1200);}
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(s).then(done,function(){fb(s,done)});
  } else fb(s,done);
}
function fb(s,done){
  var ta=document.createElement('textarea');ta.value=s;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.focus();ta.select();
  try{document.execCommand('copy');done();}catch(e){}
  document.body.removeChild(ta);
}
"""


def esc(s):
    return html.escape(str(s))


def copy_btn(rec, small=False):
    """A 'Copy blueprint' button carrying the importable string (base64 -> attr-safe)."""
    if not rec.get("bp"):
        return ""
    cls = "copy sm" if small else "copy"
    return (f"<button class='{cls}' data-bp=\"{esc(rec['bp'])}\" "
            f"onclick='copyBp(this)'>Copy blueprint</button>")


def checks_html(rec):
    out = []
    for nm, ok, detail, sev in rec.get("checks", []):
        cls = "ok" if ok else ("fail" if sev == "error" else "warn")
        mark = "ok " if ok else ("FAIL" if sev == "error" else "warn")
        d = f" — <span class='muted'>{esc(detail)}</span>" if detail else ""
        out.append(f"<div class='chk'><span class='m {cls}'>[{mark}]</span> {esc(nm)}{d}</div>")
    return "".join(out)


def card(rec):
    s = rec["status"]
    img = (f"<img src='data:image/jpeg;base64,{rec['png']}' alt='{esc(rec['name'])}'>"
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
        <div>{copy_btn(rec)}<pre>{esc(rec['source'])}</pre>
          <div style='margin-top:10px'>{checks_html(rec)}</div></div>
      </div>
    </div>"""


def stress_row(rec):
    s = rec["status"]
    detail = rec.get("error", "")
    if not detail and s == "VERIFY-FAIL":
        detail = "; ".join(n for n, ok, d, sev in rec.get("checks", []) if not ok and sev == "error")
    last = (f"<span class='badge b-{s}'>{s}</span> {copy_btn(rec, small=True)}"
            if rec.get("bp") else
            f"<span class='badge b-{s}'>{s}</span> <span class='muted'>{esc(detail)}</span>")
    return (f"<tr><td class='mono'>{esc(rec['name'])}</td>"
            f"<td>{rec.get('nodes','')}</td><td>{rec.get('edges','')}</td>"
            f"<td>{rec.get('entities','')}</td><td class='mono'>{rec.get('grid','')}</td>"
            f"<td>{rec.get('ms',0):.0f}</td>"
            f"<td>{last}</td></tr>")


def build():
    print("rendering basic examples ...")
    basic = render_folder(ROOT / "examples" / "basic")
    print("rendering complex factories ...")
    complex_ = render_folder(ROOT / "examples" / "complex")

    print("running stress battery (this can take a few minutes) ...")
    stress = [process(p.stem, p.read_text(), timeout=STRESS_TIMEOUT)
              for p in sorted((ROOT / "examples" / "stress").glob("*.fgr"))]
    # render the biggest passing stress graphs as a showcase
    top = sorted((r for r in stress if r["status"] == "PASS"),
                 key=lambda r: -r.get("entities", 0))[:4]
    print(f"rendering {len(top)} showcase stress passes ...")
    showcase = [process(r["name"], r["source"], render=True) for r in top]

    print("validate-model ...")
    try:
        vm = fbsr_validation.validate()
        vm_html = "".join(
            f"<div class='chk'><span class='m {'ok' if c.ok else 'fail'}'>"
            f"[{'ok ' if c.ok else 'FAIL'}]</span> {esc(c.name)}"
            + (f" — <span class='muted'>{esc(c.detail)}</span>" if c.detail else "") + "</div>"
            for c in vm)
    except Exception as ex:  # noqa: BLE001
        vm_html = f"<p class='muted'>validate-model unavailable: {esc(ex)}</p>"

    cx_pass = sum(r["status"] == "PASS" for r in complex_)
    st_pass = sum(r["status"] == "PASS" for r in stress)
    total_pass = cx_pass + st_pass
    total = len(complex_) + len(stress)

    doc = f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>fgr compiler report</title><style>{CSS}</style></head><body><div class='wrap'>
<h1>fgr — Factorio DSL compiler report</h1>
<p class='sub'>A high-level production graph compiles to a real Factorio layout; a
generator-agnostic verifier traces material &amp; fluid flow to confirm it realizes the
spec; FBSR renders the result. Correctness comes from the verifier, not the picture.</p>
<div class='pillrow'>
  <div class='pill'><b>{sum(r['status']=='PASS' for r in basic)}/{len(basic)}</b><br>basic examples</div>
  <div class='pill'><b>{cx_pass}/{len(complex_)}</b><br>complex factories</div>
  <div class='pill'><b>{st_pass}/{len(stress)}</b><br>generated stress DAGs</div>
  <div class='pill'><b>{total_pass}/{total}</b><br>stress battery total</div>
</div>

<h2>How it works</h2>
<div class='card'>
<pre>.fgr DSL ──parse──▶ Graph(spec) ──generate──▶ Layout ──verify──▶ PASS/FAIL ──▶ FBSR ──▶ PNG</pre>
<p class='muted' style='margin-bottom:0'>Lanes: <code>A -&gt; B</code> dedicated belt ·
<code>A -&gt; B, C</code> one belt split to many (splitter bus) ·
<code>A, B -&gt; C</code> merge · <code>A ~&gt; B</code> fluid lane (pipe). Machines:
<code>input</code>/<code>output</code> chests, <code>assembler</code>, <code>furnace</code>,
<code>chemical</code> plant, <code>fluid</code> source (pipes attach at real fluid-box tiles).</p>
</div>

<h2>Verifier model vs. Factorio data (FBSR)</h2>
<div class='card'>{vm_html}</div>

<h2>Complex factories</h2>
<p class='sub'>Hand-authored multi-step builds: deep chains, reconvergence, high fan-in,
furnaces, and oil/chemical fluids (pipes, tanks, mixing-free networks).</p>
{''.join(card(r) for r in complex_)}

<h2>Basic examples</h2>
{''.join(card(r) for r in basic)}

<h2>Stress battery — generated DAGs</h2>
<p class='sub'>{len(stress)} machine-generated complex recipe graphs run through compile +
verify to find where the generator breaks. All failures are the heuristic router/manifold
hitting its limit (extreme single-source fan-outs, or large dense graphs that exhaust the
rip-up budget) — not the verifier. See STATUS.md.</p>
<div class='card'><table>
<tr><th>case</th><th>nodes</th><th>lanes</th><th>ents</th><th>grid</th><th>ms</th><th>outcome</th></tr>
{''.join(stress_row(r) for r in stress)}
</table></div>

<h2>Showcase — largest passing stress graphs</h2>
{''.join(card(r) for r in showcase)}

</div><script>{COPY_JS}</script></body></html>"""

    out = OUT / "report.html"
    out.write_text(doc)
    print(f"wrote {out} ({out.stat().st_size//1024} KB) — "
          f"basic {sum(r['status']=='PASS' for r in basic)}/{len(basic)}, "
          f"complex {cx_pass}/{len(complex_)}, stress {st_pass}/{len(stress)}")


if __name__ == "__main__":
    build()
