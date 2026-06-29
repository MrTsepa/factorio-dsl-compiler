#!/usr/bin/env python
"""Objective layout-cleanliness metrics, for comparing layout generators (v1 vs v2).

Correctness is the verifier's job; this measures *quality* of a passing layout —
how compact, straight, and spaghetti-free it is. Run over a set of .fgr files and
print a table (and a totals row), or `--json` for machine-readable output.

    .venv/bin/python scripts/layout_metrics.py                 # all examples
    .venv/bin/python scripts/layout_metrics.py examples/basic/gears.fgr
    .venv/bin/python scripts/layout_metrics.py --json > metrics.json

Metrics per case:
  area        bounding-box tiles (w*h) — sprawl
  fill%       occupied tiles / area — density (higher = tighter)
  belts       transport-belt tiles laid (belt length)
  turns       belt corners (a belt whose upstream belt comes from a perpendicular
              dir) — needless turns are the visible "jog" problem
  ug          underground-belt + pipe-to-ground entities (tunnels = crossings)
  cross       lane crossings (undergrounds/2 + perpendicular belt-over-pipe spots)
  ok          does the verifier pass
  ms          compile wall-clock (milliseconds)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fgr.dsl import parse                              # noqa: E402
from fgr.ir import DIR_DELTA, OPPOSITE                 # noqa: E402
from fgr.layout import (BELT, PIPE_TO_GROUND, UNDERGROUND,  # noqa: E402
                        compile_graph)
from fgr.verify import verify                          # noqa: E402


def metrics_for(layout) -> dict:
    ents = layout.entities
    xs = [t[0] for e in ents for t in e.tiles()]
    ys = [t[1] for e in ents for t in e.tiles()]
    w = (max(xs) - min(xs) + 1) if xs else 0
    h = (max(ys) - min(ys) + 1) if ys else 0
    area = w * h
    occupied = {t for e in ents for t in e.tiles()}

    belts = {(e.x, e.y): e for e in ents if e.proto == BELT}
    ug = [e for e in ents if e.proto in (UNDERGROUND, PIPE_TO_GROUND)]

    # A belt "turns" when the belt directly behind it (the tile it receives from)
    # flows in a different direction than it does.
    turns = 0
    for (x, y), e in belts.items():
        d = e.direction or 0
        bx, by = x - DIR_DELTA[d][0], y - DIR_DELTA[d][1]   # tile feeding into this belt
        up = belts.get((bx, by))
        if up is not None and (up.direction or 0) != d:
            turns += 1

    return {
        "w": w, "h": h, "area": area,
        "fill%": round(100 * len(occupied) / area, 1) if area else 0.0,
        "belts": len(belts),
        "turns": turns,
        "ug": len(ug),
        "cross": len(ug) // 2,
        "ents": len(ents),
    }


def run(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        g = parse(p.read_text())
        t0 = time.perf_counter()
        try:
            layout = compile_graph(g)
            ms = round((time.perf_counter() - t0) * 1000)
            rep = verify(g, layout)
            row = {"name": p.stem, "ok": rep.ok, "ms": ms, **metrics_for(layout)}
        except Exception as ex:                       # compile/route failure
            row = {"name": p.stem, "ok": False, "ms": round((time.perf_counter() - t0) * 1000),
                   "error": type(ex).__name__ + ": " + str(ex)[:60]}
        rows.append(row)
    return rows


def _default_paths() -> list[Path]:
    return sorted(p for d in ("basic", "complex", "stress")
                  for p in (ROOT / "examples" / d).glob("*.fgr"))


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    paths = [Path(a) for a in args] if args else _default_paths()
    rows = run(paths)

    if as_json:
        print(json.dumps(rows, indent=2))
        return 0

    cols = ["name", "ok", "ms", "w", "h", "area", "fill%", "belts", "turns", "ug", "cross", "ents"]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.rjust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    ok_rows = [r for r in rows if "error" not in r]
    for r in rows:
        if "error" in r:
            print(f"{r['name'].rjust(widths['name'])}  {'FAIL':>{widths['ok']}}  {r['error']}")
        else:
            print("  ".join(str(r.get(c, "")).rjust(widths[c]) for c in cols))
    if ok_rows:
        def tot(c):
            return sum(r[c] for r in ok_rows if isinstance(r.get(c), (int, float)))
        n_ok = sum(1 for r in rows if r.get("ok"))
        print("  ".join("-" * widths[c] for c in cols))
        print(f"{('TOTAL '+str(n_ok)+'/'+str(len(rows))+' ok').rjust(widths['name'])}  "
              f"{'':>{widths['ok']}}  {str(tot('ms')).rjust(widths['ms'])}  "
              f"area={tot('area')}  belts={tot('belts')}  turns={tot('turns')}  "
              f"ug={tot('ug')}  cross={tot('cross')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
