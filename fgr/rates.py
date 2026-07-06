"""Stage A of docs/RATES.md: steady-state rate metadata for a spec (+ its layout).

Everything numeric comes from Factorio's own prototype data through the FBSR dump
pipeline (fgr.fbsr_validation loaders) -- machine crafting speeds, recipe times and
amounts, belt speeds, inserter swing rates. No hard-coded game tables.

The model (documented limits in docs/RATES.md):

* machine cap  = crafting_speed / energy_required * result_amount   [items/s]
* belt lane    = proto speed * 60 ticks * 8 items/tile / 2 lanes    [7.5/s yellow]
* loader       = full belt (both lanes)                              [15/s yellow]
* inserter     = game-measured swing (ticks are quantized; see calibration below)
* fluids (2.0) = segment model: uncapacitated at our scale

A backward demand pass over the DAG yields, for each output chest, how hard every
machine must run per 1 item/s delivered; merges split demand across same-product
suppliers proportional to their caps (an estimate -- exact for trees, the common
case). From that: each output's SOLO max rate, the UNIFORM rate all outputs sustain
together, per-node utilization and the bottleneck. If a layout is supplied, each spec
edge also gets its realized link capacity (loader / belt lane / inserter chain) and
its utilization at the operating point.
"""

from __future__ import annotations

from .ir import Graph, NodeKind
from .layout import INSERTER, LOADER, LONG_INSERTER, Layout
from . import fbsr_validation as fv

_MACHINE_PROTO = {NodeKind.ASSEMBLER: "assembling-machine-2",
                  NodeKind.CHEMICAL: "chemical-plant",
                  NodeKind.FURNACE: "electric-furnace"}


class RatesUnavailable(RuntimeError):
    """Game data (FBSR dumps) not reachable -- rates can't be computed."""


def _dump(name, kind, dumper):
    try:
        return fv._load_dump(name, kind, "vanilla", dumper)
    except fv.FbsrUnavailable as e:
        raise RatesUnavailable(str(e)) from e


def _machine_cap(node, dumper):
    """(crafts_per_s, product_name, product_items_per_s) for a machine node."""
    proto = _MACHINE_PROTO[node.kind]
    speed = _dump(proto, "entity", dumper).get("crafting_speed", 1)
    r = _dump(node.recipe, "recipe", dumper)
    t = r.get("energy_required") or 0.5
    res = (r.get("results") or r.get("products") or
           [{"name": node.recipe, "amount": 1}])[0]
    crafts = speed / t
    return crafts, res.get("name", node.recipe), crafts * res.get("amount", 1)


def _ingredients(node, dumper, types=False):
    r = _dump(node.recipe, "recipe", dumper)
    if types:
        return {i["name"]: (i.get("amount", 1), i.get("type", "item"))
                for i in r.get("ingredients", [])}
    return {i["name"]: i.get("amount", 1) for i in r.get("ingredients", [])}


def analyze(graph: Graph, layout: Layout | None = None, dumper="auto") -> dict:
    """Rate report for a spec (and optionally its compiled layout). See module doc."""
    if dumper == "auto":
        dumper = fv._fbsr_dumper()
    if dumper is None:
        raise RatesUnavailable("FBSR dumper unavailable")

    machines = {n: node for n, node in graph.nodes.items()
                if node.kind in _MACHINE_PROTO and node.recipe}
    caps, product, prod_rate, needs = {}, {}, {}, {}
    for n, node in machines.items():
        caps[n], product[n], prod_rate[n] = _machine_cap(node, dumper)
        needs[n] = _ingredients(node, dumper)
    for n, node in graph.nodes.items():
        if node.kind in (NodeKind.INPUT, NodeKind.FLUID):
            product[n] = node.item

    # who supplies each (consumer, ingredient)? split proportional to supplier caps
    suppliers: dict = {}
    for e in graph.edges:
        if e.src in product:
            suppliers.setdefault((e.dst, product[e.src]), []).append(e.src)

    def demand(node_name, items_per_s, coeff, edge_flow):
        """Propagate: node must OUTPUT items_per_s of its product."""
        if node_name not in machines:            # chest / fluid source: free supply
            return
        crafts = items_per_s / max(prod_rate[node_name] / caps[node_name], 1e-12)
        coeff[node_name] = coeff.get(node_name, 0.0) + crafts
        for ing, amount in needs[node_name].items():
            srcs = suppliers.get((node_name, ing), [])
            if not srcs:
                continue                         # missing feed: the audit's problem
            total_cap = sum(prod_rate.get(s, 1.0) for s in srcs) or 1.0
            for s in srcs:
                share = prod_rate.get(s, 1.0) / total_cap
                flow = crafts * amount * share
                edge_flow[(s, node_name)] = edge_flow.get((s, node_name), 0.0) + flow
                demand(s, flow, coeff, edge_flow)

    outputs = [n for n, node in graph.nodes.items() if node.kind is NodeKind.OUTPUT]
    per_output: dict = {}
    for o in outputs:
        coeff: dict = {}
        flows: dict = {}
        feeders = [e.src for e in graph.edges if e.dst == o and e.src in machines]
        for f in feeders:
            share = 1.0 / len(feeders)
            flows[(f, o)] = flows.get((f, o), 0.0) + share
            demand(f, share, coeff, flows)
        if not feeders:
            continue
        solo = min((caps[n] / c for n, c in coeff.items() if c > 0), default=0.0)
        per_output[o] = {"coeff": coeff, "flows": flows, "solo_max": solo}

    # uniform operating point: all outputs at the same rate R
    total_coeff: dict = {}
    total_flow: dict = {}
    for o, d in per_output.items():
        for n, c in d["coeff"].items():
            total_coeff[n] = total_coeff.get(n, 0.0) + c
        for e, f in d["flows"].items():
            total_flow[e] = total_flow.get(e, 0.0) + f
    uniform = min((caps[n] / c for n, c in total_coeff.items() if c > 0), default=0.0)
    bottleneck = min(((caps[n] / c, n) for n, c in total_coeff.items() if c > 0),
                     default=(0.0, None))[1]

    # link-aware sustained estimate: scale the uniform point down by the most
    # overloaded realized link (a single inserter arm feeding a fast recipe is the
    # classic in-game ceiling the machine math alone won't show)
    link_scale = 1.0
    report = {
        "outputs": {o: {"solo_max_per_s": round(d["solo_max"], 4),
                        "uniform_per_s": round(uniform, 4)}
                    for o, d in per_output.items()},
        "bottleneck": bottleneck,
        "machines": {n: {"recipe": machines[n].recipe,
                         "max_crafts_per_s": round(caps[n], 4),
                         "utilization_at_uniform": round(
                             min(1.0, total_coeff.get(n, 0.0) * uniform / caps[n]), 3)}
                     for n in machines},
        "model": "steady-state estimate; proportional merge splits; see docs/RATES.md",
    }

    if layout is not None:
        report["links"] = _link_report(graph, layout, total_flow, uniform, dumper)
        for v in report["links"].values():
            if v.get("capacity_per_s") and v.get("required_per_s"):
                if v["required_per_s"] > 0:
                    link_scale = min(link_scale,
                                     v["capacity_per_s"] / v["required_per_s"])
        report["sustained_est_per_s"] = {
            o: round(uniform * link_scale, 4) for o in per_output}
    return report


# --- calibrated primitives, measured in-game (scripts/rate_study.py, 2.0.77) --------
# The game QUANTIZES inserter swings to whole ticks, so the analytic formula
# (rotation_speed x 60) chronically under-predicts: a plain inserter measures
# 60/70 ticks = 0.857/s from a chest and 60/64 = 0.9375/s off a compressed belt.
# See docs/rate_analysis.html. The solver imports these too -- one source of truth.
ARM_BELT_PICK = 0.9375   # inserter, belt -> machine/chest
ARM_CHEST_PICK = 0.857   # inserter, chest/machine -> anything
LONG_ARM_PICK = 1.204    # long-handed inserter (60/50 ticks, chest -> chest)
BELT_FULL = 15.0         # loader-fed belt, both lanes
LANE_CAP = 7.5           # ONE side of a belt. Inserters drop on the FAR lane only, so
#                          a collector belt fed by inserter drops carries at most one
#                          lane's worth regardless of how many machines feed it.


def _link_caps(dumper):
    belt = _dump("transport-belt", "entity", dumper).get("speed", 0.03125) * 60 * 8
    swing = {INSERTER: ARM_BELT_PICK, LONG_INSERTER: LONG_ARM_PICK}
    return belt, swing        # belt = FULL belt items/s; swing = items/s per arm


def _link_report(graph: Graph, layout: Layout, flows, uniform, dumper) -> dict:
    """Per spec edge: required items/s at the uniform point vs the realized link's
    capacity. The weakest carrier on the lane sets the cap: any serving inserter
    (swing rate x arm count), else loader/belt (full belt; a tap-fed lane is half)."""
    belt_full, swing = _link_caps(dumper)
    per_edge_arms: dict = {}
    loaders_on: dict = {}
    for e in layout.entities:
        edge = e.meta.get("edge")
        if e.proto in (INSERTER, LONG_INSERTER) and edge is not None:
            per_edge_arms.setdefault(tuple(edge), []).append(e.proto)
        if e.proto == LOADER and edge is not None:
            loaders_on[tuple(edge)] = True
        if e.proto == LOADER and e.meta.get("src") is not None:
            loaders_on[("out", e.meta["src"])] = True

    out: dict = {}
    for e in graph.edges:
        if e.fluid:
            out[f"{e.src}~>{e.dst}"] = {"required_per_s": round(
                flows.get((e.src, e.dst), 0.0) * uniform, 3),
                "capacity_per_s": None, "note": "fluid segment (2.0): uncapacitated"}
            continue
        arms = per_edge_arms.get((e.src, e.dst), [])
        if arms:
            cap = sum(swing.get(p, swing[INSERTER]) for p in arms)
            how = f"{len(arms)} inserter arm(s)"
        elif (e.src, e.dst) in loaders_on:
            cap = belt_full
            how = "loader-coupled (full belt)"
        else:
            cap = belt_full / 2
            how = "belt lane"
        req = flows.get((e.src, e.dst), 0.0) * uniform
        out[f"{e.src}->{e.dst}"] = {
            "required_per_s": round(req, 3), "capacity_per_s": round(cap, 3),
            "via": how, "utilization": round(min(9.99, req / cap), 3) if cap else None}
    return out


def summary_lines(report: dict) -> list[str]:
    """Human-readable digest (also embedded as the blueprint's in-game description)."""
    lines = []
    sustained = report.get("sustained_est_per_s", {})
    for o, d in sorted(report.get("outputs", {}).items()):
        s = (f", ~{sustained[o]}/s sustained through the placed hardware"
             if o in sustained and sustained[o] < d["uniform_per_s"] else "")
        lines.append(f"{o}: up to {d['solo_max_per_s']}/s alone, "
                     f"{d['uniform_per_s']}/s with all outputs running{s}")
    if report.get("bottleneck"):
        b = report["bottleneck"]
        m = report["machines"].get(b, {})
        lines.append(f"bottleneck: {b} ({m.get('recipe')}) at "
                     f"{m.get('max_crafts_per_s')} crafts/s")
    hot = [(k, v) for k, v in (report.get("links") or {}).items()
           if v.get("utilization") and v["utilization"] >= 0.9]
    for k, v in sorted(hot, key=lambda kv: -kv[1]["utilization"])[:4]:
        lines.append(f"link near capacity: {k} at {int(v['utilization'] * 100)}% "
                     f"({v['via']})")
    lines.append("steady-state estimate from Factorio prototype data; "
                 "see docs/RATES.md")
    return lines
