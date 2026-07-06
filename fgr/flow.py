"""Static throughput analysis of a PLACED layout (lane-aware).

The verifier certifies connectivity; this module answers the question it leaves open:
*can the placed hardware actually carry the planned rate?* It rebuilds the carrier
graph the same way verify.py does, then:

1. classifies every belt-ish carrier by LANES USED — inserters drop on the far lane
   only (one side, 7.5/s), side-loads fill one side each, loaders and splitters fill
   both — propagated to a fixed point along straight belt feeds;
2. runs a forward fixed-point over the flow graph with the game-measured calibration
   rates (fgr.rates): machines capped by recipe speed and per-ingredient arrivals,
   arms by measured swing rates, belts by 7.5/s per used lane, loaders by 15/s;
3. reports the sustainable arrival rate at every OUTPUT chest.

The result is an UPPER BOUND: supply on a shared belt is split proportionally between
its arms, while the game allocates by belt position (upstream taps win under
scarcity). Plans that keep lanes under LANE_HEADROOM stay out of the scarcity regime,
which is what makes the bound tight in practice.
"""

from __future__ import annotations

from .ir import Graph, NodeKind
from .layout import (BELT, DIR_DELTA, INSERTER, LOADER, LONG_INSERTER, OPPOSITE,
                     SPLITTER, UNDERGROUND, Layout)
from .rates import (ARM_BELT_PICK, ARM_CHEST_PICK, BELT_FULL, LANE_CAP,
                    LONG_ARM_PICK, RatesUnavailable, _ingredients, _machine_cap)
from . import fbsr_validation as fv
from . import verify as _v

_ITERS = 120           # lane-class fixed point (short-range)
_FLOW_ITERS = 4000     # rate fixed point: rate moves ONE carrier hop per iteration,
#                        and a v3 lane can be hundreds of belt tiles long. The loop
#                        breaks on convergence; the cap only guards pathologies.


def _arm_rate(proto, pick_kind):
    if proto == LONG_INSERTER:
        return LONG_ARM_PICK
    return ARM_BELT_PICK if pick_kind in ("belt", "ug", "splitter", "loader") \
        else ARM_CHEST_PICK


def estimate(graph: Graph, layout: Layout, dumper="auto") -> dict:
    """Per-output sustainable items/s through the placed hardware (+ diagnostics)."""
    if dumper == "auto":
        dumper = fv._fbsr_dumper()
    if dumper is None:
        raise RatesUnavailable("FBSR dumper unavailable")

    report = _v.Report()
    bodies = _v._correspondence(graph, layout, report)
    carrier_at, trans_at = {}, {}
    for name, ent in bodies.items():
        for t in ent.tiles():
            carrier_at[t] = ("body", name)
    for e in layout.entities:
        if e.proto == BELT:
            carrier_at[(e.x, e.y)] = ("belt", (e.x, e.y))
            trans_at[(e.x, e.y)] = e
        elif e.proto == UNDERGROUND:
            carrier_at[(e.x, e.y)] = ("ug", (e.x, e.y))
            trans_at[(e.x, e.y)] = e
        elif e.proto == SPLITTER:
            cid = ("splitter", (e.x, e.y))
            for t in e.tiles():
                carrier_at[t] = cid
                trans_at[t] = e
        elif e.proto == LOADER:
            cid = ("loader", (e.x, e.y))
            for t in e.tiles():
                carrier_at[t] = cid
                trans_at[t] = e

    # ---- typed edges ----------------------------------------------------------------
    arms = []          # (pick_cid, drop_cid, rate)
    pushes = []        # (src_cid, dst_cid, kind) kind: straight | side | drop-like
    accepts = _accepts_factory(trans_at)
    for e in layout.entities:
        if e.proto in (INSERTER, LONG_INSERTER):
            reach = 2 if e.proto == LONG_INSERTER else 1
            dx, dy = DIR_DELTA[e.direction]
            pick = carrier_at.get((e.x + reach * dx, e.y + reach * dy))
            drop = carrier_at.get((e.x - reach * dx, e.y - reach * dy))
            if pick is None or drop is None:
                continue
            arms.append((pick, drop, _arm_rate(e.proto, pick[0])))
        elif e.proto == BELT:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            t = (e.x + dx, e.y + dy)
            if accepts(t, d):
                dst = carrier_at[t]
                te = trans_at[t]
                straight = (te.direction or 0) == d and te.proto != SPLITTER or \
                    te.proto == SPLITTER
                pushes.append((("belt", (e.x, e.y)), dst,
                               "straight" if straight else "side"))
        elif e.proto == SPLITTER:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            cid = ("splitter", (e.x, e.y))
            for t in e.tiles():
                nt = (t[0] + dx, t[1] + dy)
                if accepts(nt, d):
                    pushes.append((cid, carrier_at[nt], "straight"))
        elif e.proto == UNDERGROUND:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            if e.ug_type == "output":
                t = (e.x + dx, e.y + dy)
                if accepts(t, d):
                    te = trans_at[t]
                    pushes.append((("ug", (e.x, e.y)), carrier_at[t],
                                   "straight" if (te.direction or 0) == d else "side"))
            else:
                ex = _v._ug_exit((e.x, e.y), d, trans_at)
                if ex is not None:
                    pushes.append((("ug", (e.x, e.y)), ("ug", ex), "straight"))
        elif e.proto == LOADER:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            rear, front = _v._loader_ends(e)
            cid = ("loader", (e.x, e.y))
            if e.loader_type == "output":
                body = carrier_at.get((rear[0] - dx, rear[1] - dy))
                if body is not None:
                    pushes.append((body, cid, "loader"))
                t = (front[0] + dx, front[1] + dy)
                if accepts(t, d):
                    pushes.append((cid, carrier_at[t], "loader"))
            else:
                body = carrier_at.get((front[0] + dx, front[1] + dy))
                if body is not None:
                    pushes.append((cid, body, "loader"))

    # ---- lane classes (monotone fixed point) ------------------------------------------
    # how many belt SIDES carry items: loader/splitter feeds fill both; a straight
    # belt feed inherits its source's lanes; each side-load adds one; an inserter
    # drop lands on the far lane only (one).
    lanes: dict = {}

    def lane_of(c):
        return lanes.get(c, 0)
    for pick, drop, _r in arms:
        if drop[0] != "body":
            lanes[drop] = max(lane_of(drop), 1)
    for _ in range(_ITERS):
        changed = False
        for src, dst, kind in pushes:
            if dst[0] == "body":
                continue
            if kind == "loader" or src[0] in ("loader", "splitter"):
                cand = 2
            elif kind == "straight":
                cand = lane_of(src)
            else:                                  # side-load: one more side fills
                cand = min(2, lane_of(dst) + 1)
            new = min(2, max(lane_of(dst), cand))
            if new > lane_of(dst):
                lanes[dst] = new
                changed = True
        if not changed:
            break

    def cap(c):
        if c[0] == "body":
            return float("inf")
        if c[0] == "loader":
            return BELT_FULL
        return LANE_CAP * max(lane_of(c), 1)

    # ---- machine / node data -----------------------------------------------------------
    m_cap, m_needs, m_out = {}, {}, {}
    sources, sinks = set(), set()
    for n, nd in graph.nodes.items():
        if nd.kind in (NodeKind.ASSEMBLER, NodeKind.CHEMICAL, NodeKind.FURNACE) and nd.recipe:
            crafts, _prod, items = _machine_cap(nd, dumper)
            m_cap[n] = crafts
            m_out[n] = items / crafts if crafts else 1.0
            m_needs[n] = _ingredients(nd, dumper, types=True)
        elif nd.kind is NodeKind.INPUT:
            sources.add(n)
        elif nd.kind is NodeKind.OUTPUT:
            sinks.add(n)

    # product carried by each machine/source (for ingredient matching)
    product = {n: (graph.nodes[n].item or None) for n in sources}
    for n in m_cap:
        _c, prod, _i = _machine_cap(graph.nodes[n], dumper)
        product[n] = prod
    # ...and by each transport carrier, from the emitting net's name (b:<node>[.k])
    carrier_product = {}
    for e in layout.entities:
        net = (e.meta or {}).get("net") or ""
        if not net.startswith("b:"):
            continue
        owner = net[2:].split(".")[0]
        # bank nets are tagged with the STAGE name; the expanded graph holds copies
        prod = product.get(owner) or product.get(f"{owner}_1")
        if prod is None:
            continue
        if e.proto in (BELT, UNDERGROUND):
            carrier_product[(("ug" if e.proto == UNDERGROUND else "belt"),
                             (e.x, e.y))] = prod
        elif e.proto == SPLITTER:
            carrier_product[("splitter", (e.x, e.y))] = prod
        elif e.proto == LOADER:
            carrier_product[("loader", (e.x, e.y))] = prod

    # ---- forward fixed point -----------------------------------------------------------
    # out_rate[carrier] = items/s it can emit; machines produce, chests source at 15/s
    # per output loader, transport carriers relay min(cap, inflow).
    out_rate = {}
    inflow: dict = {}
    by_src_arms: dict = {}
    for pick, drop, r in arms:
        by_src_arms.setdefault(pick, []).append((drop, r))

    def body_rate(name):
        if name in sources:
            return float("inf")
        if name in m_cap:
            crafts = m_cap[name]
            for ing, (amt, typ) in m_needs[name].items():
                if typ != "item":
                    continue                      # fluids: uncapacitated segments
                got = inflow.get((name, ing), 0.0)
                crafts = min(crafts, got / amt if amt else crafts)
            return crafts * m_out[name]
        return 0.0

    for _ in range(_FLOW_ITERS):
        prev = dict(out_rate)
        # transport carriers: relay
        agg: dict = {}
        for src, dst, kind in pushes:
            r = out_rate.get(src, 0.0)
            agg[dst] = agg.get(dst, 0.0) + r
        # arms: proportional share of the source's emission, capped per arm
        new_inflow: dict = {}
        for pick, targets in by_src_arms.items():
            avail = out_rate.get(pick, 0.0)
            want = sum(r for _t, r in targets)
            scale = 1.0 if want <= avail or want == 0 else avail / want
            for drop, r in targets:
                f = r * min(scale, 1.0)
                if drop[0] == "body":
                    name = drop[1]
                    ing = (product.get(pick[1]) if pick[0] == "body"
                           else carrier_product.get(pick))
                    if ing is not None:
                        new_inflow[(name, ing)] = new_inflow.get((name, ing), 0.0) + f
                else:
                    agg[drop] = agg.get(drop, 0.0) + f
        inflow = new_inflow
        for c, s in agg.items():
            if c[0] == "body":
                out_rate[c] = min(s, BELT_FULL)   # chest: loader-coupled onward
            else:
                out_rate[c] = min(s, cap(c))
        for name in list(m_cap) + list(sources):
            out_rate[("body", name)] = body_rate(name) if name in m_cap else float("inf")
        def _same(a, b):
            if a == b:                             # covers inf == inf
                return True
            if a == float("inf") or b == float("inf"):
                return False
            return abs(a - b) < 1e-6
        if all(_same(out_rate.get(k, 0.0), prev.get(k, 0.0))
               for k in set(out_rate) | set(prev)):
            break

    # ---- collect per-output arrivals ---------------------------------------------------
    arrivals = {}
    for src, dst, kind in pushes:
        if dst[0] == "body" and dst[1] in sinks:
            arrivals[dst[1]] = arrivals.get(dst[1], 0.0) + \
                min(out_rate.get(src, 0.0), BELT_FULL)
    for pick, targets in by_src_arms.items():
        avail = out_rate.get(pick, 0.0)
        want = sum(r for _t, r in targets)
        scale = 1.0 if want <= avail or want == 0 else avail / want
        for drop, r in targets:
            if drop[0] == "body" and drop[1] in sinks:
                arrivals[drop[1]] = arrivals.get(drop[1], 0.0) + r * min(scale, 1.0)

    machines = {n: round(out_rate.get(("body", n), 0.0) / m_out[n], 4) for n in m_cap}
    return {"outputs_per_s": {o: round(v, 4) for o, v in sorted(arrivals.items())},
            "machine_crafts_per_s": machines}


def _accepts_factory(trans_at):
    def accepts(target_tile, d) -> bool:
        e = trans_at.get(target_tile)
        if e is None:
            return False
        td = e.direction or 0
        if e.proto == BELT:
            return td != OPPOSITE[d]
        if e.proto == SPLITTER:
            return d == td
        if e.proto == UNDERGROUND:
            if e.ug_type == "input":
                return td != OPPOSITE[d]
            return d not in (td, OPPOSITE[td])
        if e.proto == LOADER:
            rear, _front = _v._loader_ends(e)
            return e.loader_type == "input" and d == td and target_tile == rear
        return False
    return accepts
