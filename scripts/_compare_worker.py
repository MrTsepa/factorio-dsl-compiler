#!/usr/bin/env python
"""Worker: compile+verify ONE .fgr file with ONE named generator, print one JSON line of
metrics to stdout. Run as a subprocess (with a timeout) by compare_generators.py, so a
generator that hangs or crashes on one case can never take down the whole comparison."""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fgr.dsl import parse                                              # noqa: E402
from fgr.generators import compile_graph                               # noqa: E402
from fgr.ir import DIR_DELTA                                           # noqa: E402
from fgr.layout import BELT, PIPE_TO_GROUND, UNDERGROUND                # noqa: E402
from fgr.verify import verify                                          # noqa: E402


def metrics_for(layout) -> dict:
    ents = layout.entities
    tile_count = Counter(t for e in ents for t in e.tiles())
    xs = [t[0] for t in tile_count]
    ys = [t[1] for t in tile_count]
    w = (max(xs) - min(xs) + 1) if xs else 0
    h = (max(ys) - min(ys) + 1) if ys else 0
    area = w * h
    occupied = tile_count.keys()
    dup = sum(1 for n in tile_count.values() if n > 1)

    belts = {(e.x, e.y): e for e in ents if e.proto == BELT}
    ug = [e for e in ents if e.proto in (UNDERGROUND, PIPE_TO_GROUND)]

    turns = 0
    for (x, y), e in belts.items():
        d = e.direction or 0
        bx, by = x - DIR_DELTA[d][0], y - DIR_DELTA[d][1]
        up = belts.get((bx, by))
        if up is not None and (up.direction or 0) != d:
            turns += 1

    return {"w": w, "h": h, "area": area,
            "fill": round(100 * len(occupied) / area, 1) if area else 0.0,
            "belts": len(belts), "turns": turns, "ug": len(ug),
            "cross": len(ug) // 2, "ents": len(ents), "overlap_tiles": dup}


def main() -> int:
    path, generator = sys.argv[1], sys.argv[2]
    rec = {"path": path, "generator": generator, "ok": False}
    t0 = time.perf_counter()
    try:
        g = parse(Path(path).read_text())
        layout = compile_graph(g, generator)
        ms = round((time.perf_counter() - t0) * 1000)
        rep = verify(g, layout)
        rec.update(ok=rep.ok, ms=ms, **metrics_for(layout))
        if not rep.ok:
            rec["fails"] = sorted(c.name for c in rep.checks if not c.ok)
    except Exception as exc:                                            # noqa: BLE001
        rec.update(ms=round((time.perf_counter() - t0) * 1000),
                   error=f"{type(exc).__name__}: {exc}"[:200])
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
