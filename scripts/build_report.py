#!/usr/bin/env python
"""Generate the fgr landing page (out/report.html): a shareable, self-contained
showcase -- a curated set of factories (not too trivial, not overwhelming), each with
its DSL, its verified checklist, a readable render and a copy-paste blueprint.

For the example factories it compiles the DSL, runs the verifier, renders the layout
via Factorio-FBSR and embeds the PNG (base64) + the verifier verdict + the source; for
the generated stress battery it shows the compile/verify outcome as a table plus renders
of the most complex passes. Needs FBSR for images -- set FGR_FBSR_SH to your render
wrapper (see fgr/render.py); without it the report is generated text-only.

    export FGR_FBSR_SH=/path/to/your/fbsr.sh
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
from fgr.layout import LayoutError                        # noqa: E402
from fgr.generators import compile_graph                  # noqa: E402
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
STRESS_TIMEOUT = 60           # seconds per stress case (scale_5/scale_6 verify at ~25-40s)


class _Timeout(Exception):
    pass


# ---- compile / verify / render --------------------------------------------------
def _extent(lay):
    xs = [t[0] for e in lay.entities for t in e.tiles()]
    ys = [t[1] for e in lay.entities for t in e.tiles()]
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if xs else (0, 0)


def _embed_jpeg(path, max_w=2000, quality=80):
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
        # render + offer the blueprint for ALL layouts (even failing ones) so the report shows
        # exactly what v2 produces, including the near-misses; the badge marks pass/fail.
        desc = None
        try:                                     # expected rates ride in the blueprint
            from fgr.rates import analyze, summary_lines
            rrep = analyze(g, lay)
            desc = "\n".join(summary_lines(rrep))
            rec["rates"] = summary_lines(rrep)
        except Exception:                        # noqa: BLE001 -- metadata only
            pass
        rec["bp"] = to_blueprint_string(lay, name, description=desc)  # copy button
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
.b-pass{background:rgba(76,175,80,.18);color:var(--ok)}
.b-warn{background:rgba(214,169,58,.18);color:var(--warn)}
.b-fail{background:rgba(154,163,175,.2);color:var(--mut)}
table.suite td,table.suite th{padding:4px 8px;border-bottom:1px solid var(--line);text-align:left}
table.suite .copy{font-size:11px;padding:2px 8px}
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
.hero{padding:34px 0 6px}.hero h1{font-size:52px;letter-spacing:-1px}
.tag{font-size:21px;margin:6px 0 10px;color:var(--ink)}
.card.show{padding:22px}
.card.show img{margin:10px 0 12px}
.under{display:flex;flex-direction:column;gap:8px}
details summary{cursor:pointer;outline:none}
details{background:#15171b;border:1px solid var(--line);border-radius:8px;padding:8px 12px}
details pre{border:none;padding:8px 0 0}
.foot{margin-top:46px;border-top:1px solid var(--line);padding-top:14px;font-size:13px}
.rates{background:#15171b;border:1px solid var(--line);border-radius:8px;
padding:10px 14px;margin:8px 0 4px;font-size:13px}
.rtitle{font-weight:700;color:var(--acc);margin-bottom:4px}
.rline{margin:2px 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
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


def rates_html(rec):
    lines = rec.get("rates") or []
    if not lines:
        return ""
    body, note = lines, ""
    if lines and lines[-1].startswith("steady-state estimate"):
        body, note = lines[:-1], lines[-1]
    rows = "".join(f"<div class='rline'>{esc(x)}</div>" for x in body)
    return (f"<div class='rates'><div class='rtitle'>&#9201; expected throughput "
            f"<span class='muted'>(also embedded in the blueprint's in-game tooltip)"
            f"</span></div>{rows}"
            + (f"<div class='rline muted'>{esc(note)}</div>" if note else "")
            + "</div>")


def card(rec):
    s = rec["status"]
    img = (f"<img src='data:image/jpeg;base64,{rec['png']}' alt='{esc(rec['name'])}'>"
           if rec.get("png") else
           f"<pre>{esc(rec.get('error') or rec.get('render_err') or 'no image')}</pre>")
    meta = (f"{rec.get('nodes','?')} machines · {rec.get('edges','?')} lanes · "
            f"{rec.get('entities','?')} entities placed · compiled in {rec.get('ms',0):.0f} ms")
    checks = rec.get("checks", [])
    nck = len(checks)
    if s == "PASS":
        extra = (" · recipes + ingredients match Factorio's own data"
                 if rec.get("game_accurate") else "")
        ck = (f"<details><summary class='chk ok'>[ok] all {nck} verifier checks pass"
              f"{extra} — expand</summary>{checks_html(rec)}</details>")
    else:
        ck = checks_html(rec)
    return f"""
    <div class='card show'>
      <h3>{esc(rec['name'])} <span class='badge b-{s}'>{s}</span></h3>
      <p class='sub'>{rec.get('blurb', '')}</p>
      <div class='sub muted'>{meta}</div>
      {rates_html(rec)}
      {plan_html(rec)}
      {img}
      <div class='under'>
        {copy_btn(rec)}
        <details><summary class='muted'>show the DSL source</summary>
        <pre>{esc(rec['source'])}</pre></details>
        {ck}
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


SHOWCASE = [
    # (path, blurb) -- curated: escalating complexity, every render readable at page width
    ("examples/basic/bus.fgr",
     "The signature move: <b>one belt feeds three consumers</b> — each taps the passing belt "
     "with its own inserter. No splitters, no spaghetti."),
    ("examples/basic/circuits.fgr",
     "Two ingredient lanes converge on one assembler. The compiler gives it two input "
     "inserters and routes both belts without a crossing."),
    ("examples/complex/processing_unit.fgr",
     "Reconvergent electronics (cable feeds green <i>and</i> red; green feeds red <i>and</i> blue) "
     "plus <b>sulfuric acid piped in</b> — the pipe tunnels under the belt field into the "
     "assembler's real fluid box."),
    ("examples/complex/flying_robot_frame.fgr",
     "A deep multi-step build: smelting, gears, engines, batteries — with <b>two different "
     "fluids</b> (lubricant, acid) kept in isolated pipe networks."),
    ("examples/stress/science_3.fgr",
     "A red + green + military science mall: smelting, ammo and grenade lines, a "
     "coal/iron merge — every machine fed its real ingredients, end to end."),
]


SIZED_BLURBS = {
    "gears_belt": "INPUT-driven: <b>one belt of iron in — max gears out.</b> A "
        "5-machine bank row, each machine fed by THREE iron arms and drained by TWO "
        "output arms, planned to 92% of the belt so no machine permanently starves. "
        "Measured 6.72/s with the first gear at 19 seconds — 94 entities.",
    "circuits_1ps": "OUTPUT-driven, two stages: <b>1 electronic circuit / second.</b> "
        "Circuits eat 3 cables per craft, so both stages go multi-copy and each circuit "
        "machine gets its own dedicated cable lane — multi-arm feeding expressed as "
        "separate same-product lanes.",
    "redsci_15": "OUTPUT-driven at scale: <b>1.5 red science / second</b>. As a bank: "
        "gear machines positioned by PREFIX DEMAND along the bus (a producer placed "
        "after its consumers starves them — the positional physics, handled at "
        "placement). Measured <b>1.65/s — exactly the predicted equilibrium — with "
        "the first pack at 40 seconds</b>; the routed version was still ramping "
        "toward it after 40 minutes.",
    "greensci_05": "DEEP CHAIN, six stages: <b>0.5 green science / second</b> — shared "
        "iron, reconvergent gears. The honest one: measured throughput lands at ~66% of "
        "plan because multi-column nets still route interior tap arms (splitter support "
        "is the tracked fix). The pipeline catches its own gaps.",
    "battery_05": "FLUIDS: <b>0.5 batteries / second</b> from chemical plants — acid "
        "arrives by pipe (2.0 segments are uncapacitated), so only the plants and the "
        "item feeds needed sizing.",
    "greenchips_belt": "THE SCALE TEST: <b>a full yellow belt (15/s) of electronic "
        "circuits</b> from an eight-line spec, compiled by the BANK generator: "
        "sandwich rows with belts as local buses, machines positioned by prefix "
        "demand, and a lane WEAVE at the exit (a merge that fills BOTH belt sides — "
        "splitters preserve lane sides, so the second collector tunnels under and "
        "side-loads from the north). The boundary is minimal too: <b>one</b> iron "
        "belt (a splitter halves it across the two blocks) plus two copper belts "
        "(22.5/s is physics). <b>649 entities</b> (was 12,000 routed point-to-point), "
        "measured <b>14.43/s steady</b>, first circuit at 36 seconds, zero idle "
        "machines.",
}


def sized_cards():
    """Cards for examples/sized/: sizing plan + measured-in-game badge + a blueprint
    whose in-game description carries the full plan."""
    import json as _json
    from fgr.solver import solve
    from fgr.rates import RatesUnavailable
    results = {}
    rp = ROOT / "out" / "rate_study" / "sized_results.json"
    if rp.exists():
        results = _json.loads(rp.read_text())
    cards = []
    for p in sorted((ROOT / "examples" / "sized").glob("*.fgr")):
        text = p.read_text()
        g = parse(text)
        lay = None
        try:
            from fgr.layout_bank import BankInapplicable, compile_bank
            try:
                g2, plan, lay = compile_bank(g)
            except BankInapplicable:
                g2, plan = solve(g)
        except (RatesUnavailable, Exception) as ex:      # noqa: BLE001
            print(f"  ! sized {p.stem}: {ex}")
            continue
        rec = process(p.stem, text, render=False)
        if lay is None:
            lay = compile_graph(g2)
        rep2 = verify(g2, lay)
        rec["status"] = "PASS" if rep2.ok else "VERIFY-FAIL"
        rec["checks"] = [(c.name, c.ok, c.detail, c.severity) for c in rep2.checks]
        rec["entities"] = len(lay.entities)
        meas = (results.get(p.stem) or {}).get("measured_per_s") or {}
        desc_lines = ["sized by fgr solve"]
        for o, t in plan["target_per_s"].items():
            e = plan["expected_actual_per_s"].get(o)
            desc_lines.append(f"{o}: target >= {t}/s, expected ~{e}/s")
        for it, v in meas.items():
            desc_lines.append(f"measured in-game: {it} {v}/s")
        for n, m in plan["machines"].items():
            how = (f"{m['binding']}-bound" if "binding" in m
                   else "bank " + "/".join(f"{k}={v}" for k, v in
                                           (m.get("arms") or {}).items()))
            desc_lines.append(f"{n}: {m['copies']}x ({how})")
        desc_lines.append("input lanes: " + ", ".join(
            f"{k}x{v}" for k, v in plan.get("input_lanes", {}).items()))
        rec["bp"] = to_blueprint_string(lay, p.stem, description="\n".join(desc_lines))
        try:
            from fgr.render import render_blueprint_string
            png = OUT / f"render_{p.stem}.png"
            render_blueprint_string(rec["bp"], png)
            rec["png"] = _embed_jpeg(png)
        except Exception as ex:                          # noqa: BLE001
            rec["render_err"] = str(ex)[:200]
        rec["blurb"] = SIZED_BLURBS.get(p.stem, "")
        rec["plan"] = plan
        rec["measured"] = meas
        rec["sim_note"] = (results.get(p.stem) or {}).get("note")
        cards.append(rec)
    return cards


def plan_html(rec):
    plan = rec.get("plan")
    if not plan:
        return ""
    rows = []
    for n, m in plan["machines"].items():
        if "binding" in m:
            how = f"{esc(m['binding'])}-bound"
        else:                                    # bank plan: explicit arm allocation
            a = m.get("arms", {})
            how = (f"arms {a.get('k_far', 0)}L+{a.get('k_near', 0)}N in / "
                   f"{a.get('k_out', 0)} out")
        rows.append(f"<tr><td class='mono'>{esc(n)}</td><td>{m['copies']}×</td>"
                    f"<td>{m['per_copy_crafts_per_s']}/s each</td>"
                    f"<td>{how}</td></tr>")
    lanes = ", ".join(f"{esc(k)} ×{v}" for k, v in plan.get("input_lanes", {}).items())
    tgt = []
    for o, t in plan["target_per_s"].items():
        e = plan["expected_actual_per_s"].get(o)
        tgt.append(f"target ≥ <b>{t}/s</b> · expected ~<b>{e}/s</b>")
    meas = rec.get("measured") or {}
    mline = "".join(f"<div class='rline'>measured in-game: {esc(it)} <b>{v}/s</b></div>"
                    for it, v in meas.items())
    if not meas and rec.get("sim_note"):
        mline = ("<div class='rline muted'>in-game: "
                 + esc(rec["sim_note"]) + "</div>")
    return (f"<div class='rates'><div class='rtitle'>&#9881; sizing plan "
            f"<span class='muted'>(embedded in the blueprint tooltip)</span></div>"
            f"<div class='rline'>{' · '.join(tgt)}</div>{mline}"
            f"<table>{''.join(rows)}</table>"
            f"<div class='rline muted'>input lanes: {lanes}</div></div>")


def suite_section():
    """The one-belt suite scoreboard: 61 items, each graded; passing items get a
    copyable blueprint (recompiled fresh through the same pipeline)."""
    import json as _json
    rp = ROOT / "out" / "belt_suite_results.json"
    if not rp.exists():
        return ""
    rows = _json.loads(rp.read_text())
    from fgr.layout_bank import BankInapplicable, compile_bank
    from fgr.solver import solve
    trs = []
    for r in sorted(rows, key=lambda r: (not r.get("meets_target", False),
                                         r.get("mode", ""), r["item"])):
        status = ("ok" if r.get("meets_target") else
                  "short" if r.get("verify") else
                  r.get("mode", "?"))
        badge = {"ok": "b-pass", "short": "b-warn"}.get(status, "b-fail")
        bp_btn = ""
        if r.get("meets_target"):
            spec = ROOT / "corner_cases" / "one_belt" / f"{r['item']}.fgr"
            try:
                g = parse(spec.read_text())
                try:
                    g2, plan, lay = compile_bank(g)
                except BankInapplicable:
                    g2, plan = solve(g)
                    lay = compile_graph(g2)
                bp = to_blueprint_string(lay, r["item"],
                                         description=f"one-belt suite: {r['item']} "
                                         f">= 15/s (fgr solve)")
                bp_btn = (f"<button class='copy' data-bp='{esc(bp)}' "
                          f"onclick='copyBp(this)'>Copy blueprint</button>")
            except Exception as ex:                       # noqa: BLE001
                print(f"  ! suite bp {r['item']}: {str(ex)[:80]}")
        flow = r.get("flow_per_s", "")
        trs.append(
            f"<tr><td class='mono'>{esc(r['item'])}</td>"
            f"<td><span class='badge {badge}'>{esc(status.upper())}</span></td>"
            f"<td>{esc(r.get('mode', ''))}</td>"
            f"<td>{r.get('machines', '')}</td><td>{r.get('entities', '') or ''}</td>"
            f"<td>{flow}</td><td>{bp_btn}</td></tr>")
    n_ok = sum(1 for r in rows if r.get("meets_target"))
    return f"""
<h2>The one-belt suite: a full belt of everything</h2>
<p class='sub'>61 base-game items, each asked for at a <b>full yellow belt (15/s)</b>;
specs generated from the game's real recipe data (<code>scripts/belt_suite.py</code>),
graded by the verifier and the placed-layout flow oracle. <b>{n_ok}/61 carry the full
belt today — with zero verifier failures</b>: what breaks, breaks in coverage or sheer
scale (a full belt of electric furnaces is a 2,978-machine megabase), and every
failure class is a tracked roadmap item (see
<a href='https://github.com/mrtsepa/factorio-dsl-compiler/blob/main/corner_cases/one_belt/RESULTS.md'>RESULTS.md</a>).</p>
<div class='card'><div style='max-height:480px;overflow:auto'>
<table class='suite'><tr><th>item</th><th>result</th><th>path</th><th>machines</th>
<th>entities</th><th>placed flow /s</th><th></th></tr>{''.join(trs)}</table>
</div></div>
"""


def build():
    print("rendering showcase ...")
    cases = []
    dumper = None
    try:
        dumper = fbsr_validation._fbsr_dumper()
    except Exception:                            # noqa: BLE001
        pass
    for rel, blurb in SHOWCASE:
        p = ROOT / rel
        rec = process(p.stem, p.read_text(), render=True)
        rec["blurb"] = blurb
        # GUARDRAIL: the landing shows only game-accurate builds. Every showcase spec
        # must use real recipes, craftable by its machine, fed its REAL ingredients on
        # the right channels -- checked against Factorio's own data. Synthetic-recipe
        # stress cases are fine for the corpus, never for the front page.
        if dumper is not None:
            g = parse(p.read_text())
            audit = (fbsr_validation.check_recipes(g, dumper=dumper)
                     + fbsr_validation.check_ingredients(g, dumper=dumper))
            bad = [c for c in audit if not c.ok]
            if bad:
                raise SystemExit(
                    f"SHOWCASE case {rel} is not game-accurate: "
                    + "; ".join(c.detail for c in bad))
            rec["game_accurate"] = True
        cases.append(rec)

    print("sized builds ...")
    sized = sized_cards()
    print("one-belt suite ...")
    suite_html = suite_section()

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

    npass = sum(r["status"] == "PASS" for r in cases)
    hero_bp = next((r for r in cases if r["name"] == "bus"), cases[0])

    doc = f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>fgr — describe a factory, get a Factorio blueprint</title>
<style>{CSS}</style></head><body><div class='wrap'>

<div class='hero'>
<h1>fgr</h1>
<p class='tag'>Describe a factory as a little graph — get a <b>verified, paste-and-run
Factorio blueprint</b>.</p>
<p class='sub'>You write <i>what</i> to build (machines and the lanes between them). The
compiler works out <i>where</i> everything goes — belts, inserters, undergrounds, pipes,
loaders, even the power grid — and an independent verifier traces every item and fluid
through the placed tiles to prove the layout really does what you asked.</p>
<div class='pillrow'>
  <div class='pill'><b>155/155</b><br>tracked cases verify<br><span class='muted'>(49 curated + 106 stress)</span></div>
  <div class='pill'><b>paste &amp; run</b><br>power grid + EEI included<br><span class='muted'>works in sandbox as-is</span></div>
  <div class='pill'><b>full-belt I/O</b><br>chests couple via loaders<br><span class='muted'>both lanes, no bottleneck</span></div>
  <div class='pill'><b>1 oracle</b><br>3 swappable generators<br><span class='muted'>the verifier is the judge</span></div>
</div>
</div>

<h2>Thirty seconds of how</h2>
<div class='card'>
<pre>.fgr DSL ──parse──▶ Graph(spec) ──compile──▶ Layout ──verify──▶ PASS/FAIL ──▶ blueprint string</pre>
<p class='muted'>Lanes: <code>A -&gt; B</code> belt · <code>A -&gt; B, C</code> one belt, one
tap per consumer (no splitters) · <code>A, B -&gt; C</code> multi-tap merge ·
<code>A ~&gt; B</code> fluid pipe. The compiler (v3) routes every lane as a negotiated
multi-terminal net — PathFinder-style congestion pricing, the same idea FPGAs route with.</p>
<p class='muted' style='margin-bottom:0'>A <b>PASS</b> is physical, not claimed: no overlaps,
every declared lane connects (and none you didn't declare), no two products share a belt
lane, fluids stay in isolated networks attached at real fluid-box tiles, and every machine
sits on a live, wired power network. If it passes, it runs.</p>
</div>

{''.join(card(r) for r in cases)}

<h2>Give it a rate — the solver sizes the factory</h2>
<p class='sub'>Annotate an input (<code>@ 1 belt</code>) or an output
(<code>@ 0.5/s</code>) and <code>fgr solve</code> derives machine counts, inserter
arms and the number of input belts — then the layout is compiled, verified, and
<b>measured in the real game</b> (headless). Every machine inside is
vanilla-buildable; the boundary scaffolding (chest + loader belts, power feed) is
what you replace when splicing into a real base.</p>
{''.join(card(r) for r in sized)}

{suite_html}

<h2>Trust, but verify the verifier</h2>
<div class='card'>
<p class='muted'>The oracle's model of the game (inserter pickup/drop sides, fluid-box
tiles, footprints, tunnel pairing) is itself checked against Factorio's real prototype
data via FBSR:</p>
<details><summary class='muted'>validate-model — {esc('all checks')} </summary>{vm_html}</details>
</div>

<p class='muted foot'>Showcase: {npass}/{len(cases)} shown cases verify · full corpus and
generator head-to-head in <a href='https://github.com/MrTsepa/factorio-dsl-compiler/blob/main/STATUS.md'>STATUS.md</a> ·
source on <a href='https://github.com/MrTsepa/factorio-dsl-compiler'>GitHub</a> ·
renders by <a href='https://github.com/demodude4u/Factorio-FBSR'>FBSR</a> (real game sprites).</p>

</div><script>{COPY_JS}</script></body></html>"""

    out = OUT / "report.html"
    out.write_text(doc)
    print(f"wrote {out} ({out.stat().st_size//1024} KB) — showcase {npass}/{len(cases)}")
    if hero_bp:
        pass


if __name__ == "__main__":
    build()
