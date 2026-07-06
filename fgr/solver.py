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
from .rates import (ARM_BELT_PICK, ARM_CHEST_PICK, BELT_FULL, LANE_CAP,
                    RatesUnavailable, _ingredients, _machine_cap)
from . import fbsr_validation as fv
K_IN = 3                 # max input arms per ingredient per machine
K_OUT = 2                # max output arms per machine (each = a port-subnet)
SAFETY = 0.95            # sizing headroom: run machines at <=95% of the binding cap
LANE_HEADROOM = 0.92     # never PLAN a belt above 92% of capacity: taps drain a belt
#                          in priority order, and at 100% load the tail machines
#                          starve on every compression hiccup -- measured on the
#                          17-machine gears bank (tail ran ~11% below its siblings)

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
            if draw > 0:                           # plan to 92% of declared supply:
                usable = cap * LANE_HEADROOM       # a 100%-loaded belt starves its
                lam = min(lam, usable / draw) if lam is not None else usable / draw
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
    eff, copies, per_copy, arms_in, ports_out = {}, {}, {}, {}, {}
    for n, r in required.items():
        # Neither INPUT nor OUTPUT arms cap a machine at one inserter: a machine may
        # run up to K_IN arms per ingredient (k routed legs) and K_OUT output arms
        # (k port-subnets, each with its own root inserter). Faces are finite --
        # K_IN=3 and K_OUT=2 keep a 3x3 machine's perimeter feasible even for
        # three-ingredient recipes.
        arm_cap = min((K_IN * ARM_BELT_PICK / amt for amt in item_needs[n].values()),
                      default=caps[n])
        arm_out = K_OUT * ARM_CHEST_PICK / out_amount[n]
        eff[n] = min(caps[n], arm_cap, arm_out) * SAFETY
        copies[n] = max(1, math.ceil(r / eff[n]))
        per_copy[n] = r / copies[n]
    def _arms(n):
        arms_in[n] = {ing: min(K_IN, math.ceil(per_copy[n] * amt / ARM_BELT_PICK))
                      for ing, amt in item_needs[n].items()}
        ports_out[n] = min(K_OUT, max(1, math.ceil(
            per_copy[n] * out_amount[n] / ARM_CHEST_PICK)))
    for n in required:
        _arms(n)

    # APPETITE-based input sizing: machines pull at their true caps, not at the plan
    # (they cannot be throttled), so lanes must cover what the built machines CAN
    # draw -- sizing lanes to the target draw loads belts to 100% and the tail taps
    # starve on every compression hiccup (measured on the 17-machine gears bank).
    def raw_appetite():
        """items/s each raw input's DIRECT consumers can pull at their true caps
        (appetite = target draw scaled by the consumers' appetite/target ratio)."""
        out = {}
        for i in {i for o in outputs for i in unit_raw[o]}:
            direct = [e.dst for e in graph.edges if e.src == i and e.dst in copies]
            ratio = max(((eff[n] / SAFETY) / max(per_copy[n], 1e-12)
                         for n in direct), default=1.0)
            base = sum(unit_raw[o].get(i, 0.0) * target[o] for o in outputs)
            out[i] = base * ratio
        return out

    # input-driven clamp: built machines pull at appetite; if a stage adjacent to a
    # DECLARED input can overdraw the usable supply, shed copies until it fits (a
    # 100%-loaded declared belt is the constant-starvation case the user observed)
    for i, cap in in_caps.items():
        usable = cap * LANE_HEADROOM
        direct = sorted({e.dst for e in graph.edges if e.src == i and e.dst in copies})
        for n in direct:
            amt = needs[n].get(product[i], 0)
            if amt <= 0:
                continue
            per_copy_appetite = (eff[n] / SAFETY) * amt
            others = sum((eff[m] / SAFETY) * needs[m].get(product[i], 0) * copies[m]
                         for m in direct if m != n)
            avail = usable - others
            fit = (max(1, math.ceil(avail / per_copy_appetite))
                   if per_copy_appetite else copies[n])
            if fit < copies[n]:                    # ceil: at most ONE partial machine
                copies[n] = fit                    # instead of a permanently starving
                per_copy[n] = min(required[n] / copies[n], eff[n])  # tail
                _arms(n)

    raw_draw = {}                                  # input node -> items/s (at target)
    for o in outputs:
        for i, d in unit_raw[o].items():
            raw_draw[i] = raw_draw.get(i, 0.0) + d * target[o]
    appetite = raw_appetite()
    lanes = {}
    for i, d in raw_draw.items():
        if graph.nodes[i].kind is NodeKind.FLUID:
            lanes[i] = 1                           # fluids: one source, uncapacitated
        else:
            a = max(d, appetite.get(i, d))
            declared = graph.nodes[i].rate
            usable = (declared if declared is not None
                      else lanes.get(i, 0) or a) or a
            n_lanes = max(1, math.ceil(a / (BELT_FULL * LANE_HEADROOM)))
            if declared is not None:
                n_lanes = min(n_lanes, max(1, math.ceil(declared / BELT_FULL)))
            lanes[i] = n_lanes

    # DELIVERY lanes: the belt feeding an output chest is filled by inserter drops
    # (far lane only) -- one side, 7.5/s, NOT the full 15/s a loader-fed belt moves.
    # Size chest count per LANE, with the same headroom rule as input lanes.
    out_total = {o: target[o] for o in outputs}
    out_chests = {o: max(1, math.ceil(t / (LANE_CAP * LANE_HEADROOM)))
                  for o, t in out_total.items()}

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
            return BELT_FULL * LANE_HEADROOM
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
            # deliveries: producers round-robin over the output chest copies, using
            # every output port so each root inserter carries <= one arm's rate
            for k, s in enumerate(srcs):
                for port in range(ports_out.get(e.src, 1)):
                    g2.add_edge(s, dsts[k % len(dsts)], port=port)
            continue
        # machine feeds: pack each consumer copy's per-ingredient draw into supplier
        # capacity in ARM-sized chunks. Chunks from the same supplier copy become one
        # edge with arms=k (k routed legs, k inserters); chunks that spill to another
        # supplier become a separate edge. Producer copies distribute their consumers
        # across their output PORTS (each port = its own subnet + root inserter),
        # never promising more than one arm's rate per port.
        ing = product[e.src]
        share = 1.0 / max(len(suppliers(e.dst, ing)), 1)
        budget = {s: copy_rate(e.src) for s in srcs}
        is_input = graph.nodes[e.src].kind is NodeKind.INPUT
        n_ports = 1 if is_input else ports_out.get(e.src, 1)
        pload = {s: [0.0] * n_ports for s in srcs}
        si = 0
        for d in dsts:
            need = per_copy.get(e.dst, 0.0) * needs.get(e.dst, {}).get(ing, 0.0) * share
            got = {}                               # src copy -> arms taken
            guard = 0
            while need > 1e-9:
                s = srcs[si]
                take = min(budget[s], need, ARM_BELT_PICK)
                if take > 1e-9:
                    got[s] = got.get(s, 0) + 1
                    budget[s] -= take
                    need -= take
                if need > 1e-9 or budget[s] <= 1e-9:
                    si = (si + 1) % len(srcs)
                    guard += 1
                    if guard > 3 * len(srcs) + 3:
                        raise SolveError(
                            f"cannot pack {e.dst} demand onto {e.src} lanes "
                            f"({need:.3f}/s unplaced -- supply too tight)")
            for s, k in got.items():
                port = min(range(n_ports), key=lambda p: pload[s][p])
                flow = per_copy.get(e.dst, 0.0) * needs.get(e.dst, {}).get(ing, 0.0) \
                    * share * (k / max(sum(got.values()), 1))
                pload[s][port] += flow
                g2.add_edge(s, d, arms=k, port=port)
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
        deliver = out_chests.get(o, 1) * LANE_CAP   # single-lane collectors
        lim = min(lim, deliver) if lim is not None else deliver
        expected[o] = round(lim, 4) if lim is not None else None

    plan = {
        "target_per_s": {o: round(t, 4) for o, t in target.items()},
        "expected_actual_per_s": expected,
        "machines": {n: {"copies": copies[n],
                         "per_copy_crafts_per_s": round(per_copy[n], 4),
                         "cap_per_copy": round(eff[n], 4),
                         "arms_in_per_copy": arms_in[n],
                         "output_arms_per_copy": ports_out[n],
                         "binding": ("machine" if eff[n] >= caps[n] * SAFETY - 1e-9
                                     else "output arm" if eff[n] < caps[n] * SAFETY - 1e-9
                                     and abs(eff[n] - (ARM_CHEST_PICK / out_amount[n]) * SAFETY) < 1e-6
                                     else "input arms")}
                     for n in sorted(copies)},
        "input_lanes": {i: lanes[i] for i in sorted(lanes)},
        "input_draw_per_s": {i: round(d, 3) for i, d in sorted(raw_draw.items())},
        "output_chests": out_chests,
        "expanded": {"nodes": len(g2.nodes), "edges": len(g2.edges)},
    }
    return g2, plan
