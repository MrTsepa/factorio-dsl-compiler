#!/usr/bin/env python
"""All WORKING one-belt solutions as a PDF: one page per item that verifies AND
carries its target through the placed hardware (side-aware flow oracle), sorted by
entity count (the tracked metric -- lower is better). Reuses build_pdf's page
composer; the cover carries the scoreboard.

Usage: python scripts/build_solutions_pdf.py [-o out/solutions.pdf]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from build_pdf import _compose, _cover  # noqa: E402
from fgr.blueprint import to_blueprint_string  # noqa: E402
from fgr.dsl import parse  # noqa: E402
from fgr.flow import estimate  # noqa: E402
from fgr.generators import compile_graph  # noqa: E402
from fgr.layout_bank import BankInapplicable, compile_bank  # noqa: E402
from fgr.render import render_blueprint_string  # noqa: E402
from fgr.solver import SolveError, solve  # noqa: E402
from fgr.verify import verify  # noqa: E402

OUT_DIR = ROOT / "out" / "solutions_pdf"
NODE_CAP = 140


def evaluate(fp: Path):
    text = fp.read_text()
    g = parse(text)
    rec = {"rel": str(fp.relative_to(ROOT)), "dsl": text, "status": "SKIP",
           "checks": [], "err": None, "img": None}
    lay, plan, mode = None, None, "bank"
    try:
        try:
            g2, plan, lay = compile_bank(g)
        except BankInapplicable as e:
            mode = "routed"
            rec["bank_skip"] = str(e)[:70]
            g2, plan = solve(g)
            if len(g2.nodes) > NODE_CAP:
                rec.update(status="TOO-BIG", err=f"{len(g2.nodes)} nodes")
                return rec
            lay = compile_graph(g2)
        rep = verify(g2, lay)
        if not rep.ok:
            rec.update(status="VERIFY-FAIL",
                       checks=[(c.name, c.detail[:160]) for c in rep.checks
                               if not c.ok])
            return rec
        est = estimate(g2, lay)
        got = sum(est["outputs_per_s"].values())
        target = min(plan["target_per_s"].values())
        machines = sum(m["copies"] for m in plan["machines"].values())
        rec["flow"] = round(got, 2)
        rec["entities"] = len(lay.entities)
        if got < target - 1e-6:
            rec.update(status="SHORT",
                       err=f"placed capacity {got:.2f}/s vs target {target}/s")
            return rec
        rec["status"] = "PASS"
        rec["stamp"] = (f"{mode} | machines {machines} | blocks "
                        f"{plan.get('blocks', '-')} | entities {len(lay.entities)}"
                        f" | placed flow {got:.2f}/s >= {target}/s"
                        f" | blueprint: solutions_bp/{fp.stem}.bp")
        bp = to_blueprint_string(lay, fp.stem,
                                 description=f"one-belt suite: {fp.stem} "
                                 f">= {target}/s (fgr)")
        bp_dir = ROOT / "out" / "solutions_bp"
        bp_dir.mkdir(parents=True, exist_ok=True)
        (bp_dir / f"{fp.stem}.bp").write_text(bp)
        rec["bp"] = bp
        png = OUT_DIR / f"{fp.stem}.png"
        try:
            render_blueprint_string(to_blueprint_string(lay, fp.stem), png,
                                    timeout=900)
            rec["img"] = str(png)
        except Exception as e:                            # noqa: BLE001
            rec["err"] = f"render failed: {str(e)[:100]}"
        return rec
    except (SolveError, Exception) as e:                  # noqa: BLE001
        rec.update(status="ERROR", err=f"{type(e).__name__}: {str(e)[:140]}")
        return rec


def main() -> int:
    out_pdf = ROOT / "out" / "solutions.pdf"
    if "-o" in sys.argv:
        out_pdf = Path(sys.argv[sys.argv.index("-o") + 1])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    specs = sorted((ROOT / "corner_cases" / "one_belt").glob("*.fgr")) + \
        sorted((ROOT / "examples" / "sized").glob("*.fgr"))
    recs = []
    for fp in specs:
        t0 = time.time()
        rec = evaluate(fp)
        recs.append(rec)
        print(f"{fp.stem:32s} {rec['status']:12s} e={rec.get('entities', '-'):>6} "
              f"({time.time() - t0:.0f}s)", flush=True)
    passing = [r for r in recs if r["status"] == "PASS"]
    passing.sort(key=lambda r: r.get("entities", 1 << 30))
    pages = [_cover(recs)] + [_compose(r) for r in passing]
    pages[0].save(out_pdf, "PDF", save_all=True, append_images=pages[1:],
                  resolution=150.0)
    # companion page: every blueprint with a copy button (a PDF page is an image;
    # this is the clickable access)
    import html as _h
    rows = "".join(
        f"<tr><td class='m'>{r['rel'].split('/')[-1][:-4]}</td>"
        f"<td>{r.get('entities')}</td><td>{r.get('flow')}</td>"
        f"<td><button data-bp=\"{_h.escape(r['bp'])}\" "
        f"onclick=\"navigator.clipboard.writeText(this.dataset.bp);"
        f"this.textContent='Copied!'\">Copy blueprint</button></td></tr>"
        for r in passing)
    (ROOT / "out" / "solutions.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>fgr solutions</title>"
        "<style>body{font:14px sans-serif;margin:24px}td{padding:4px 10px;"
        "border-bottom:1px solid #ddd}.m{font-family:monospace}</style>"
        f"<h1>{len(passing)} working one-belt solutions</h1>"
        "<table><tr><th>item</th><th>entities</th><th>flow/s</th><th></th></tr>"
        + rows + "</table>")
    print(f"\nwrote {out_pdf} ({out_pdf.stat().st_size // 1024} KB): "
          f"{len(passing)} solutions + out/solutions.html + out/solutions_bp/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
