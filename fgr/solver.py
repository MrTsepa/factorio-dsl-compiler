"""The rate solver (Stage B): size a factory to a throughput target.

Input: a spec whose INPUT/OUTPUT nodes may carry ``@ rate`` annotations
(``input iron : iron-plate @ 1 belt`` / ``output out @ 0.3/s``). Two modes fall out
of the same math:

* OUTPUT-driven -- outputs are annotated: the factory is sized to deliver them, and
  the required number of input belts is derived.
* INPUT-driven -- only inputs are annotated: the solver maximises output subject to
  the declared supply (all outputs scale together).

Output: an EXPANDED spec graph (same IR, no new concepts) plus a sizing plan. All
sizing respects the BOUNDARY RULE -- interior machines are vanilla-buildable and
inserter-fed, so per-machine rates are capped by measured ARM capacities; loaders and
infinity chests exist only at the boundary. Multiplicity is expressed structurally:

* a machine that must run faster than one copy allows becomes N copies;
* multi-arm feeding is multiple same-product source edges (one inserter each), so a
  copy never needs more than one arm per ingredient by construction;
* raw items arriving faster than one belt (15/s) become several input chests, i.e.
  several boundary belts -- "the correct number of input belts" is just arithmetic;
* expanded machine consumers are marked ``no_merge`` so the router keeps their
  supplier lanes separate (merging two suppliers onto one tap would collapse two
  arms into one).

Machine caps and recipe data come from Factorio's dumps; link capacities come from
the CALIBRATION table below, measured in the game by scripts/rate_study.py (the
analytic swing formula is wrong -- the game quantizes swings to ticks; see
docs/rate_analysis.html).
"""

from __future__ import annotations

import math

from .ir import Graph, Node, NodeKind
from .rates import RatesUnavailable, _ingredients, _machine_cap
from . import fbsr_validation as fv

# --- calibrated primitives (measured in-game; scripts/rate_study.py, 2.0.77) -------
ARM_BELT_PICK = 0.9375   # inserter, compressed belt -> machine/chest   (60/64 ticks)
ARM_CHEST_PICK = 0.857   # inserter, chest/machine -> anything          (60/70 ticks)
BELT_FULL = 15.0         # loader-fed belt, both lanes
SAFETY = 0.95            # sizing headroom: run machines at <=95% of the binding cap

_MACHINE_KINDS = (NodeKind.ASSEMBLER, NodeKind.CHEMICAL, NodeKind.FURNACE)


class SolveError(RuntimeError):
    pass


def _copy_names(name, n):
    return [name] if n == 1 else [f"{name}_{i + 1}" for i in range(n)]


def solve(graph: Graph, dumper="auto") -> tuple[Graph, dict]:
    """Size `graph` to its ``@ rate`` annotations. Returns (expanded_graph, plan)."""
    if dumper == "auto":
        dumper = fv._fbsr_dumper()
    if dumper is None:
        raise RatesUnavailable("FBSR dumper unavailable")

    machines = {n: nd for n, nd in graph.nodes.items()
                if nd.kind in _MACHINE_KINDS and nd.recipe}
    caps, product, out_amount, needs, item_needs = {}, {}, {}, {}, {}
    for n, nd in machines.items():
        crafts, prod, items = _machine_cap(nd, dumper)
        caps[n], product[n] = crafts, prod
        out_amount[n] = items / crafts if crafts else 1.0
        typed = _ingredients(nd, dumper, types=True)
        needs[n] = {ing: amt for ing, (amt, _t) in typed.items()}
        # FLUID ingredients arrive by pipe (2.0 segments: uncapacitated) -- only ITEM
        # ingredients cost inserter arms (a 20-acid recipe once got arm-limited to
        # 0.047 crafts/s and the solver built 12 plants where 3 sufficed)
        item_needs[n] = {ing: amt for ing, (amt, t) in typed.items() if t == "item"}
    for n, nd in graph.nodes.items():
        if nd.kind in (NodeKind.INPUT, NodeKind.FLUID):
            product[n] = nd.item

    def suppliers(consumer, ingredient):
        return [e.src for e in graph.edges
                if e.dst == consumer and product.get(e.src) == ingredient]

    # ---- per-unit requirement pass: crafts/s of each node per 1 item/s at each output
    outputs = [n for n, nd in graph.nodes.items() if nd.kind is NodeKind.OUTPUT]
    unit: dict[str, dict[str, float]] = {}       # output -> node -> crafts per 1/s
    unit_raw: dict[str, dict[str, float]] = {}   # output -> raw input node -> items/s

    def pull(node, items_per_s, coeff, raw):
        nd = graph.nodes[node]
        if nd.kind in (NodeKind.INPUT, NodeKind.FLUID):
            raw[node] = raw.get(node, 0.0) + items_per_s
            return
        crafts = items_per_s / out_amount[node]
        coeff[node] = coeff.get(node, 0.0) + crafts
        for ing, amount in needs[node].items():
            srcs = suppliers(node, ing)
            if not srcs:
                raise SolveError(f"{node} needs {ing!r} but no lane supplies it "
                                 f"(run the game-accuracy audit)")
            for s in srcs:                        # equal split between suppliers
                pull(s, crafts * amount / len(srcs), coeff, raw)

    for o in outputs:
        coeff: dict = {}
        raw: dict = {}
        feeders = [e.src for e in graph.edges if e.dst == o and not e.fluid]
        for f in feeders:
            pull(f, 1.0 / len(feeders), coeff, raw)
        unit[o], unit_raw[o] = coeff, raw

    # ---- choose the operating point ------------------------------------------------
    out_rates = {o: graph.nodes[o].rate for o in outputs}
    in_caps = {n: nd.rate for n, nd in graph.nodes.items()
               if nd.kind is NodeKind.INPUT and nd.rate is not None}
    if any(r is not None for r in out_rates.values()):
        # OUTPUT-driven: unannotated outputs get 0? No -- they share the factory;
        # treat missing annotations as "match the smallest annotated rate".
        base = min(r for r in out_rates.values() if r is not None)
        target = {o: (r if r is not None else base) for o, r in out_rates.items()}
    elif in_caps:
        # INPUT-driven: maximise a uniform output rate subject to declared supply
        lam = None
        for i, cap in in_caps.items():
            draw = sum(unit_raw[o].get(i, 0.0) for o in outputs)
            if draw > 0:
                lam = min(lam, cap / draw) if lam is not None else cap / draw
        if lam is None:
            raise SolveError("input rates given but no output draws from them")
        target = {o: lam for o in outputs}
    else:
        raise SolveError("no @rate annotations: annotate inputs (max-output mode) "
                         "or outputs (sizing mode)")

    # feasibility vs any declared input caps
    for i, cap in in_caps.items():
        draw = sum(unit_raw[o].get(i, 0.0) * target[o] for o in outputs)
        if draw > cap * 1.0001:
            raise SolveError(f"input {i!r} supplies {cap}/s but the target draws "
                             f"{draw:.2f}/s -- raise the input or lower the target")

    # ---- machine counts -------------------------------------------------------------
    required = {}                                  # node -> crafts/s
    for o in outputs:
        for n, c in unit[o].items():
            required[n] = required.get(n, 0.0) + c * target[o]
    eff, copies, per_copy = {}, {}, {}
    for n, r in required.items():
        arm_in = min((ARM_BELT_PICK / amt for amt in item_needs[n].values()),
                     default=caps[n])
        arm_out = ARM_CHEST_PICK / out_amount[n]
        eff[n] = min(caps[n], arm_in, arm_out) * SAFETY
        copies[n] = max(1, math.ceil(r / eff[n]))
        per_copy[n] = r / copies[n]

    raw_draw = {}                                  # input node -> items/s
    for o in outputs:
        for i, d in unit_raw[o].items():
            raw_draw[i] = raw_draw.get(i, 0.0) + d * target[o]
    lanes = {}
    for i, d in raw_draw.items():
        if graph.nodes[i].kind is NodeKind.FLUID:
            lanes[i] = 1                           # fluids: one source, uncapacitated
        else:
            lanes[i] = max(1, math.ceil(d / BELT_FULL))

    out_total = {o: target[o] for o in outputs}
    out_chests = {o: max(1, math.ceil(t / BELT_FULL)) for o, t in out_total.items()}

    # ---- build the expanded graph ---------------------------------------------------
    g2 = Graph()
    for n, nd in graph.nodes.items():
        if n in copies:
            for c in _copy_names(n, copies[n]):
                g2.add_node(Node(c, nd.kind, item=nd.item, recipe=nd.recipe))
        elif nd.kind is NodeKind.INPUT:
            for c in _copy_names(n, lanes.get(n, 1)):
                g2.add_node(Node(c, nd.kind, item=nd.item, rate=nd.rate))
        elif nd.kind is NodeKind.OUTPUT:
            for c in _copy_names(n, out_chests.get(n, 1)):
                g2.add_node(Node(c, nd.kind, rate=nd.rate))
        else:                                      # fluid sources: single
            g2.add_node(Node(n, nd.kind, item=nd.item, rate=nd.rate))

    def copy_list(n):
        if n in copies:
            return _copy_names(n, copies[n])
        if graph.nodes[n].kind is NodeKind.INPUT:
            return _copy_names(n, lanes.get(n, 1))
        if graph.nodes[n].kind is NodeKind.OUTPUT:
            return _copy_names(n, out_chests.get(n, 1))
        return [n]

    def copy_rate(orig):
        """items/s ONE copy of original node `orig` can supply, at its MAX (eff)
        rate -- packing against the average per-copy rate fragments and fails even
        when total supply exactly covers total demand."""
        if orig in per_copy:
            return eff[orig] * out_amount[orig]
        if graph.nodes[orig].kind is NodeKind.INPUT:
            return BELT_FULL
        return float("inf")                        # fluid source

    # supply-aware assignment: consumers round-robin over supplier copies, but a
    # supplier lane never promises more than it produces
    for e in graph.edges:
        srcs, dsts = copy_list(e.src), copy_list(e.dst)
        if e.fluid:
            for d in dsts:
                for s in srcs:
                    g2.add_edge(s, d, fluid=True)
            continue
        if graph.nodes[e.dst].kind is NodeKind.OUTPUT:
            # deliveries: producers round-robin over the output chest copies
            for k, s in enumerate(srcs):
                g2.add_edge(s, dsts[k % len(dsts)])
            continue
        # machine feeds: pack each consumer copy's per-ingredient draw into supplier
        # lane capacity. A draw exceeding one lane's supply is SPLIT across lanes --
        # each extra lane is an extra inserter arm on the consumer (distinct producer
        # copies, since the router dedupes same-pair edges).
        ing = product[e.src]
        share = 1.0 / max(len(suppliers(e.dst, ing)), 1)
        budget = {s: copy_rate(e.src) for s in srcs}
        si = 0
        for d in dsts:
            need = per_copy.get(e.dst, 0.0) * needs.get(e.dst, {}).get(ing, 0.0) * share
            guard = 0
            while need > 1e-9:
                s = srcs[si]
                take = min(budget[s], need)
                if take > 1e-9:
                    g2.add_edge(s, d)
                    budget[s] -= take
                    need -= take
                if need > 1e-9 or budget[s] <= 1e-9:
                    si = (si + 1) % len(srcs)
                    guard += 1
                    if guard > 2 * len(srcs) + 2:
                        raise SolveError(
                            f"cannot pack {e.dst} demand onto {e.src} lanes "
                            f"({need:.3f}/s unplaced -- supply too tight)")
        # interior consumers must keep supplier lanes separate (arms!)
        if graph.nodes[e.dst].kind in _MACHINE_KINDS:
            for d in dsts:
                g2.no_merge.add(d)

    # expected ACTUAL delivery: machines don't throttle to the plan -- they run as
    # fast as their feeds allow, so ceil() rounding overdelivers. Per output, the
    # realizable rate is the minimum stage capacity along its chain (true caps, no
    # safety margin), plus the input lanes.
    expected = {}
    for o in outputs:
        lim = None
        for n, c in unit[o].items():
            if c <= 0:
                continue
            true_eff = eff[n] / SAFETY
            lim = min(lim, copies[n] * true_eff / c) if lim is not None else \
                copies[n] * true_eff / c
        for i, d in unit_raw[o].items():
            if d <= 0 or graph.nodes[i].kind is NodeKind.FLUID:
                continue
            cap_i = lanes.get(i, 1) * BELT_FULL
            lim = min(lim, cap_i / d) if lim is not None else cap_i / d
        expected[o] = round(lim, 4) if lim is not None else None

    plan = {
        "target_per_s": {o: round(t, 4) for o, t in target.items()},
        "expected_actual_per_s": expected,
        "machines": {n: {"copies": copies[n],
                         "per_copy_crafts_per_s": round(per_copy[n], 4),
                         "cap_per_copy": round(eff[n], 4),
                         "binding": ("machine" if eff[n] >= caps[n] * SAFETY - 1e-9
                                     else "inserter arms")}
                     for n in sorted(copies)},
        "input_lanes": {i: lanes[i] for i in sorted(lanes)},
        "input_draw_per_s": {i: round(d, 3) for i, d in sorted(raw_draw.items())},
        "output_chests": out_chests,
        "expanded": {"nodes": len(g2.nodes), "edges": len(g2.edges)},
    }
    return g2, plan
