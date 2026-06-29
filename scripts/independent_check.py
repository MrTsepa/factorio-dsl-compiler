#!/usr/bin/env python
"""An INDEPENDENT connectivity check for a compiled .fgr layout -- a second opinion
to cross-check fgr.verify (do NOT import fgr.verify here).

It re-derives, from the placed entities alone, whether each declared lane is
physically realised, using its own traversal:
  * items: flood reachability over carriers (belts/splitters/undergrounds) driven by
    belt flow + inserter pickup/drop, stopping at named bodies;
  * fluids: flood the pipe network and require it to touch the source's OUTPUT
    fluid-box and the sink's INPUT fluid-box.
Prints JSON: {item_ok, fluid_ok, missing:[...], extra:[...]} so an auditor can
compare it against the verifier's verdict.

    .venv/bin/python scripts/independent_check.py path/to/spec.fgr
"""
from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fgr.dsl import parse                                          # noqa: E402
from fgr.ir import DIR_DELTA, OPPOSITE                             # noqa: E402
from fgr.layout import (BELT, INSERTER, PIPE, PIPE_TO_GROUND, PIPE_UG_GAP, SPLITTER,
                        UG_MAX_GAP, UNDERGROUND, compile_graph, _fluid_connections)  # noqa: E402

CARD = (0, 4, 8, 12)


def check(path: Path) -> dict:
    g = parse(path.read_text())
    lay = compile_graph(g)
    bodies = {e.meta["node"]: e for e in lay.entities if e.meta.get("node")}

    # ---- carriers (items) -------------------------------------------------------
    carrier = {}            # tile -> id
    kind = {}               # tile -> entity (transport)
    for name, b in bodies.items():
        for t in b.tiles():
            carrier[t] = ("body", name)
    for e in lay.entities:
        if e.proto == BELT:
            carrier[(e.x, e.y)] = ("belt", (e.x, e.y)); kind[(e.x, e.y)] = e
        elif e.proto == UNDERGROUND:
            carrier[(e.x, e.y)] = ("ug", (e.x, e.y)); kind[(e.x, e.y)] = e
        elif e.proto == SPLITTER:
            cid = ("spl", (e.x, e.y))
            for t in e.tiles():
                carrier[t] = cid; kind[t] = e

    def ug_exit(t, d):
        dx, dy = DIR_DELTA[d]
        for k in range(1, UG_MAX_GAP + 1):
            f = (t[0] + dx * k, t[1] + dy * k)
            e = kind.get(f)
            if e is not None and e.proto == UNDERGROUND and (e.direction or 0) == d:
                return f if e.ug_type == "output" else None
        return None

    def accepts(tile, d):
        e = kind.get(tile)
        if e is None:
            return False
        td = e.direction or 0
        if e.proto == BELT:
            return td != OPPOSITE[d]
        if e.proto == SPLITTER:
            return d == td
        if e.proto == UNDERGROUND:
            return e.ug_type == "input" and d == td
        return False

    adj: dict = {}

    def link(a, b):
        adj.setdefault(a, set()).add(b)

    for e in lay.entities:
        if e.proto == INSERTER:
            dx, dy = DIR_DELTA[e.direction]
            p, q = carrier.get((e.x + dx, e.y + dy)), carrier.get((e.x - dx, e.y - dy))
            if p and q:
                link(p, q)
        elif e.proto == BELT:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            if accepts((e.x + dx, e.y + dy), d):
                link(("belt", (e.x, e.y)), carrier[(e.x + dx, e.y + dy)])
        elif e.proto == SPLITTER:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            for t in e.tiles():
                if accepts((t[0] + dx, t[1] + dy), d):
                    link(("spl", (e.x, e.y)), carrier[(t[0] + dx, t[1] + dy)])
        elif e.proto == UNDERGROUND:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            if e.ug_type == "output":
                if accepts((e.x + dx, e.y + dy), d):
                    link(("ug", (e.x, e.y)), carrier[(e.x + dx, e.y + dy)])
            else:
                x = ug_exit((e.x, e.y), d)
                if x:
                    link(("ug", (e.x, e.y)), ("ug", x))

    body_ids = {("body", n) for n in bodies}
    found = set()
    for src in bodies:
        seen = set(adj.get(("body", src), ()))
        q = deque(seen)
        while q:
            cur = q.popleft()
            if cur in body_ids:
                found.add((src, cur[1]))
                continue
            for nb in adj.get(cur, ()):
                if nb not in seen:
                    seen.add(nb); q.append(nb)
    item_spec = {(e.src, e.dst) for e in g.edges if not e.fluid}
    item_missing = sorted(item_spec - found)
    item_extra = sorted(found - item_spec)

    # ---- pipes (fluids) ---------------------------------------------------------
    pipes = {(e.x, e.y): e for e in lay.entities if e.proto in (PIPE, PIPE_TO_GROUND)}
    pp = {t: t for t in pipes}

    def find(t):
        while pp[t] != t:
            pp[t] = pp[pp[t]]; t = pp[t]
        return t
    for t, e in pipes.items():
        for d in CARD:
            nb = (t[0] + DIR_DELTA[d][0], t[1] + DIR_DELTA[d][1])
            if nb in pipes:
                pp[find(t)] = find(nb)
        if e.proto == PIPE_TO_GROUND:
            d = e.direction or 0                  # open mouth; tunnel runs the opposite way
            dx, dy = DIR_DELTA[OPPOSITE[d]]
            for k in range(1, PIPE_UG_GAP + 1):
                f = (t[0] + dx * k, t[1] + dy * k)
                fe = pipes.get(f)
                if fe is None or fe.proto != PIPE_TO_GROUND:
                    continue                      # surface pipe above the tunnel: ignore
                fd = fe.direction or 0
                if fd == OPPOSITE[d]:
                    pp[find(t)] = find(f); break  # matching mouth -> pair
                if fd == d:
                    break                         # same-axis underground blocks the line
    out_nets, in_nets = {}, {}
    for name, b in bodies.items():
        on, inn = set(), set()
        for tile, flow in _fluid_connections(b.proto, b.x, b.y, b.direction):
            if tile in pipes:
                n = find(tile)
                if flow in ("output", "both"):
                    on.add(n)
                if flow in ("input", "both"):
                    inn.add(n)
        out_nets[name], in_nets[name] = on, inn
    fluid_missing = sorted(f"{e.src}~>{e.dst}" for e in g.edges if e.fluid
                           and not (out_nets.get(e.src) and in_nets.get(e.dst)
                                    and out_nets[e.src] & in_nets[e.dst]))

    return {"item_ok": not item_missing and not item_extra,
            "fluid_ok": not fluid_missing,
            "item_missing": [f"{a}->{b}" for a, b in item_missing],
            "item_extra": [f"{a}->{b}" for a, b in item_extra],
            "fluid_missing": fluid_missing}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1]))))
