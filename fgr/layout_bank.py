"""The BANK generator: classic sandwich-row layouts for rate-sized specs.

Real belt builds don't route point-to-point lanes -- they put machines in rows with
belts as LOCAL buses: a producer drops onto the bus and the consumer two tiles east
picks it up, so no belt cross-section carries the aggregate flow. This module
compiles that shape directly; a hand-built 15/s circuit factory is two orders of
magnitude smaller than a routed one and saturates in seconds, and so are these.

Template (y grows downward), one stage:

    [far N belt]   input (long arms, reach 2): adjacent bus / long-haul / raw
    [near N belt]  input (normal arms): raw / long-haul
    [arm lane N]
    [machines]     3 rows
    [arm lane S]   output arms (and third-input arms picking south)
    [near S belt]  optional third input row -- v2
    [bus rows]     outputs: the ADJACENT bus (next stage's far belt) and/or a
                   LONG-HAUL row for a non-adjacent consumer -- v2

v2 additions (driven by the one-belt suite's break map):
* a stage may consume ANY earlier stage's product: the producer emits a dedicated
  long-haul row that runs east past the build, descends a margin column (crossing
  nothing), and re-enters the consumer's input row flowing WEST -- arms don't care
  about belt direction, and the full supply enters at the row's east end, so tap
  order is starvation-free;
* up to THREE item ingredients: the third rides a south input row; output arms are
  then LONG (they reach over it to the single bus row).

Output ports are arm-reach-gated: the arm lane reaches depth 1 (normal) and depth 2
(long), so a stage has at most two output rows -- and exactly one when a south input
row occupies depth 1. Shapes outside these gates raise BankInapplicable and the
generic solver + v3 router take over: coverage only widens, existing passes cannot
regress.

Everything from v1 is preserved: sizing derived from the template's arm slots (plan
and layout cannot disagree), machines positioned by PREFIX DEMAND along adjacent
buses, blocks tiling when a raw lane would exceed LANE_HEADROOM, boundary chests on
ONE aligned column (X_IN) with splitter fan-out when one belt feeds two rows,
substations in reserved power slots, and the exit LANE WEAVE (splitters preserve
lane sides, so the second collector tunnels under the output row and side-loads
from the north to fill the empty lane).
"""

from __future__ import annotations

import math

from .ir import Graph, Node, NodeKind
from .layout import (ASSEMBLER, BELT, CHEMICAL, CHEST_INPUT, CHEST_OUTPUT, EEI,
                     FLUID_SOURCE, FURNACE, INSERTER, LOADER, LONG_INSERTER, PIPE,
                     PIPE_TO_GROUND, SPLITTER, SUBSTATION, UNDERGROUND, Layout,
                     PlacedEntity, _fluid_connections)
from .rates import (ARM_BELT_PICK, ARM_CHEST_PICK, BELT_FULL, LANE_CAP,
                    LONG_ARM_PICK, RatesUnavailable, _ingredients, _machine_cap)
from .solver import LANE_HEADROOM, SAFETY, SolveError
from . import fbsr_validation as fv

# directions
N, E, S = 0, 4, 8
W_DIR = 12
_MACHINE_KINDS = {NodeKind.ASSEMBLER: ASSEMBLER, NodeKind.CHEMICAL: CHEMICAL,
                  NodeKind.FURNACE: FURNACE}
X_IN = -6                 # the INPUT column: every boundary chest sits here, outside
#                           the factory body, so a player can box-select and replace
#                           the scaffolding with real feeds in one edit


def blocks_hint(raw_unit, target):
    lane_usable = BELT_FULL * LANE_HEADROOM
    b = 1
    for _ing, d in raw_unit.items():
        b = max(b, math.ceil(d * target / lane_usable))
    return b


def raw_items_probe(graph):
    return {nd.item for nd in graph.nodes.values() if nd.kind is NodeKind.INPUT}


class BankInapplicable(RuntimeError):
    """This spec doesn't fit the sandwich template; use the generic path."""


# ---------------------------------------------------------------------------
# Planning: stage order, input-row assignment, arm slots.
# ---------------------------------------------------------------------------
def plan_dag(graph: Graph, dumper):
    """Topologically order machine stages and assign every ingredient to a template
    row. Raises BankInapplicable for shapes the emitter doesn't handle."""

    outputs = [n for n, nd in graph.nodes.items() if nd.kind is NodeKind.OUTPUT]
    if len(outputs) != 1:
        raise BankInapplicable("bank template handles exactly one output")
    machines = [n for n, nd in graph.nodes.items()
                if nd.kind in _MACHINE_KINDS and nd.recipe]
    raws = {n: nd.item for n, nd in graph.nodes.items()
            if nd.kind is NodeKind.INPUT}

    product, needs, fluid_ing = {}, {}, {}
    for n in machines:
        _c, prod, _i = _machine_cap(graph.nodes[n], dumper)
        product[n] = prod
        typed = _ingredients(graph.nodes[n], dumper, types=True)
        needs[n] = {i: a for i, (a, t) in typed.items() if t == "item"}
        fl = [i for i, (a, t) in typed.items() if t == "fluid"]
        if len(fl) > 1:
            raise BankInapplicable(f"stage {n} needs {len(fl)} fluids (template "
                                   f"pipes fit 1)")
        fluid_ing[n] = fl[0] if fl else None
        if fl:
            srcs = [s for s, nd in graph.nodes.items()
                    if nd.kind is NodeKind.FLUID and nd.item == fl[0]]
            if not srcs:
                raise BankInapplicable(f"fluid {fl[0]!r} has no boundary source")

    order, seen = [], set()

    def visit(n):
        if n in seen or n not in machines:
            return
        seen.add(n)
        for e in graph.edges:
            if e.dst == n:
                visit(e.src)
        order.append(n)
    for n in machines:
        visit(n)

    by_product = {}
    for n in order:
        if product[n] in by_product:
            raise BankInapplicable(f"two stages produce {product[n]!r}")
        by_product[product[n]] = n
    raw_items = set(raws.values())

    # per stage: ingredient -> ("raw", item) | ("stage", src_stage)
    sources: dict[str, dict] = {}
    for i, n in enumerate(order):
        if len(needs[n]) > 3:
            raise BankInapplicable(f"stage {n} has {len(needs[n])} ingredients "
                                   f"(template rows fit 3)")
        src = {}
        for ing in needs[n]:
            if ing in raw_items:
                src[ing] = ("raw", ing)
            elif ing in by_product and order.index(by_product[ing]) < i:
                src[ing] = ("stage", by_product[ing])
            else:
                raise BankInapplicable(
                    f"stage {n} needs {ing!r} which is neither a raw input nor an "
                    f"earlier stage's product")
        sources[n] = src

    # row assignment: farN (long arms) / nearN (normal) / nearS (normal, S face).
    # The ADJACENT bus (previous stage's product) is physically the farN row.
    rows: dict[str, dict] = {}
    for i, n in enumerate(order):
        prev = order[i - 1] if i else None
        assign: dict = {}
        pool = list(sources[n].items())
        adj = next(((ing, s) for ing, s in pool
                    if s[0] == "stage" and s[1] == prev), None)
        if adj:
            assign["farN"] = adj
            pool.remove(adj)
        pool.sort(key=lambda kv: -needs[n][kv[0]])
        for slot in ("nearN", "farN", "nearS"):
            if slot in assign or not pool:
                continue
            assign[slot] = pool.pop(0)
        if pool:
            raise BankInapplicable(f"stage {n}: more belt inputs than template rows")
        rows[n] = assign

    # OUTPUT-port gate: consumers = adjacent (bus at depth 1/2) + long-hauls; the
    # arm lane reaches two rows, and a nearS input occupies depth 1.
    cons: dict[str, set] = {n: set() for n in order}
    for n in order:
        for ing, (kind, src) in sources[n].items():
            if kind == "stage":
                cons[src].add(n)
    for i, n in enumerate(order):
        nxt = order[i + 1] if i + 1 < len(order) else None
        n_ports = len([c for c in cons[n] if c != nxt]) + \
            (1 if nxt in cons[n] else 0)
        max_ports = 1 if rows[n].get("nearS") else 2
        if n and n_ports > max_ports:
            raise BankInapplicable(
                f"stage {n} needs {n_ports} output rows (arm reach fits "
                f"{max_ports})")
    return order, raws, outputs[0], product, needs, sources, rows, fluid_ing


_ARM_RATE = {"farN": LONG_ARM_PICK, "nearN": ARM_BELT_PICK, "nearS": ARM_BELT_PICK}


def _stage_slots(cap, needs, assign, out_amount, has_s_input, s_face_slots=3):
    """Best per-machine arm allocation for the row assignment. N face: 3 slots
    (farN long + nearN normal). S face: 3 slots shared by nearS input arms and the
    output arms (LONG when a nearS row exists -- they reach over it)."""
    best = None
    for k_far in range(0, 4):
        for k_near in range(0, 4 - k_far):
            for k_sin in range(0, s_face_slots):
                for k_out in range(1, s_face_slots + 1 - k_sin):
                    x = cap * SAFETY
                    ok = True
                    for slot, k in (("farN", k_far), ("nearN", k_near),
                                    ("nearS", k_sin)):
                        ing = assign.get(slot)
                        if ing is None:
                            if k:
                                ok = False
                            continue
                        if k == 0:
                            ok = False
                            continue
                        x = min(x, k * _ARM_RATE[slot] / needs[ing[0]] * SAFETY)
                    if not ok:
                        continue
                    out_rate = LONG_ARM_PICK if has_s_input else ARM_CHEST_PICK
                    x = min(x, k_out * out_rate / out_amount * SAFETY)
                    key = (round(x, 6), -(k_far + k_near + k_sin + k_out))
                    if best is None or key > best[0]:
                        best = (key, dict(k_far=k_far, k_near=k_near, k_sin=k_sin,
                                          k_out=k_out, rate=x))
    if best is None:
        raise BankInapplicable("no feasible arm allocation")
    return best[1]


# ---------------------------------------------------------------------------
# The compiler.
# ---------------------------------------------------------------------------
def compile_bank(graph: Graph, dumper="auto"):
    """Compile a rate-annotated spec into a bank layout.

    Returns (expanded_graph, plan, layout): the expanded graph declares exactly the
    lanes the bank realizes, so the standard verifier grades it."""
    if dumper == "auto":
        dumper = fv._fbsr_dumper()
    if dumper is None:
        raise RatesUnavailable("FBSR dumper unavailable")
    stages, raws, out_node, product, needs, sources, row_assign, fluid_ing = \
        plan_dag(graph, dumper)

    caps, out_amt = {}, {}
    for n in stages:
        crafts, _p, items = _machine_cap(graph.nodes[n], dumper)
        caps[n] = crafts
        out_amt[n] = items / crafts if crafts else 1.0

    # fluid-stage machines are ROTATED 180 degrees: their fluid boxes face SOUTH,
    # the pipe trunk runs BELOW the stage, and the north side keeps full bus
    # adjacency. The box costs one S-face slot.
    slots = {n: _stage_slots(caps[n], needs[n], row_assign[n], out_amt[n],
                             has_s_input="nearS" in row_assign[n],
                             s_face_slots=(2 if fluid_ing[n] else 3))
             for n in stages}

    # ---- DAG unit demand --------------------------------------------------------------
    consumers: dict[str, list] = {n: [] for n in stages}
    for n in stages:
        for ing, (kind, src) in sources[n].items():
            if kind == "stage":
                consumers[src].append((n, needs[n][ing]))
    terminals = [n for n in stages if not consumers[n]]
    if len(terminals) != 1:
        raise BankInapplicable("more than one terminal stage")
    last_stage = terminals[0]
    unit: dict = {}
    for n in reversed(stages):
        if n == last_stage:
            unit[n] = 1.0 / out_amt[n]
        else:
            unit[n] = sum(unit[c] * amt for c, amt in consumers[n]) / out_amt[n]
    raw_unit: dict[str, float] = {}
    for n in stages:
        for ing, (kind, src) in sources[n].items():
            if kind == "raw":
                raw_unit[ing] = raw_unit.get(ing, 0.0) + unit[n] * needs[n][ing]

    # ---- operating point ----------------------------------------------------------------
    out_rate = graph.nodes[out_node].rate
    in_rates = {raws[n]: nd.rate for n, nd in graph.nodes.items()
                if n in raws and nd.rate is not None}
    if out_rate is not None:
        target = out_rate
    elif in_rates:
        target = min((cap * LANE_HEADROOM) / raw_unit[i]
                     for i, cap in in_rates.items() if raw_unit.get(i))
    else:
        raise SolveError("no @rate annotations")
    for i, cap in in_rates.items():
        if raw_unit.get(i, 0.0) * target > cap * 1.0001:
            raise SolveError(f"input {i!r} supplies {cap}/s but the target draws "
                             f"{raw_unit[i] * target:.2f}/s")
    if target > BELT_FULL + 1e-9:
        raise BankInapplicable("more than one full output belt not yet supported")

    counts = {n: max(1, math.ceil(unit[n] * target / slots[n]["rate"]))
              for n in stages}
    for i, cap in in_rates.items():               # appetite clamp (input-driven)
        usable = cap * LANE_HEADROOM
        for n in stages:
            amt = sum(needs[n][ing] for ing, (k, s) in sources[n].items()
                      if k == "raw" and s == i)
            if amt <= 0:
                continue
            appetite = slots[n]["rate"] / SAFETY * amt
            fit = max(1, math.ceil(usable / appetite)) if appetite else counts[n]
            counts[n] = min(counts[n], fit)

    # ---- long-haul lane budgets: unlike the adjacent bus (local, interleaved flow),
    # a long-haul row carries its AGGREGATE flow through the margin -- and a
    # drop-fed row is ONE lane (7.5/s)
    for i, n in enumerate(stages):
        nxt = stages[i + 1] if i + 1 < len(stages) else None
        for c, amt in consumers[n]:
            if c == nxt:
                continue
            flow = unit[c] * amt * target / max(blocks_hint(raw_unit, target), 1)
            if flow > LANE_CAP * LANE_HEADROOM + 1e-9:
                raise BankInapplicable(
                    f"long-haul {n}->{c} needs {flow:.1f}/s on one drop-fed lane "
                    f"(cap {LANE_CAP * LANE_HEADROOM:.1f})")

    # ---- blocks -------------------------------------------------------------------------
    lane_usable = BELT_FULL * LANE_HEADROOM
    blocks = 1
    for ing, d in raw_unit.items():
        blocks = max(blocks, math.ceil(d * target / lane_usable))
    if blocks > 2:
        raise BankInapplicable(f"{blocks} blocks (a raw item needs more than two "
                               f"belt-rows)")
    per_block = {n: [counts[n] // blocks + (1 if b < counts[n] % blocks else 0)
                     for b in range(blocks)] for n in stages}

    # ---- geometry ----------------------------------------------------------------------
    # WEST-ROOM shifts (see the positioning pass): each stage starts far enough
    # east that its upstream producers fit west of its first machine; the row
    # width must accommodate shift + machine count
    consumers_pre: dict = {n: [] for n in stages}
    for n in stages:
        for ing, (kind, src) in sources[n].items():
            if kind == "stage":
                consumers_pre[src].append((n, needs[n][ing]))
    min_slot_idx = {stages[0]: 0}
    for i in range(1, len(stages)):
        n, prev = stages[i], stages[i - 1]
        room = 0
        if any(c == n for c, _a in consumers_pre[prev]):
            need = slots[n]["rate"] * needs[n].get(product[prev], 0.0)
            per_prod = slots[prev]["rate"] * out_amt[prev]
            room = math.ceil(need / per_prod) if per_prod else 0
        min_slot_idx[n] = min_slot_idx[prev] + room
    machines_wide = max(min_slot_idx[n] + max(per_block[n]) for n in stages)

    def is_power(i):
        return i % 5 == 2
    width_slots, cap_slots = 0, 0
    while cap_slots < machines_wide:
        if not is_power(width_slots):
            cap_slots += 1
        width_slots += 1
    machine_slots = [i for i in range(width_slots) if not is_power(i)]
    power_slots = [i for i in range(width_slots) if is_power(i)]

    def slot_x(i):
        return 2 + i * 3
    W = slot_x(width_slots) + 3

    # ---- PASS 1: row positions ----------------------------------------------------------
    ypos: dict = {}
    lh_links = []                                  # (src, dst, block)
    collector_rows = []                            # (y, block)
    y = 0
    for b in range(blocks):
        for i, n in enumerate(stages):
            a = row_assign[n]
            r: dict = {}
            prev = stages[i - 1] if i else None
            adj_far = (a.get("farN") and a["farN"][1] == ("stage", prev))
            if a.get("farN") and not adj_far:
                r["farN"] = y
                y += 1
            elif adj_far:
                r["farN"] = ypos[(prev, b)]["bus"]
            if a.get("nearN"):
                r["nearN"] = y
                y += 1
            r["arm_in"] = y
            r["mach"] = y + 1
            r["arm_out"] = y + 4
            y += 5
            if a.get("nearS"):
                r["nearS"] = y
                y += 1
            nxt = stages[i + 1] if i + 1 < len(stages) else None
            lh = sorted({c for c, _amt in consumers[n] if c != nxt})
            if n == last_stage:
                block_out = per_block[n][b] * slots[n]["rate"] * out_amt[n]
                n_coll = (1 if blocks > 1
                          else (2 if block_out > LANE_CAP + 1e-9 else 1))
                if n_coll == 2 and a.get("nearS"):
                    raise BankInapplicable("3-ingredient last stage above one "
                                           "collector lane")
                r["bus"] = y
                for _c in range(n_coll):
                    collector_rows.append((y, b))
                    y += 1
            else:
                # long-haul rows FIRST, the adjacent bus LAST: the bus must sit
                # directly above the next stage's rows (its consumers' long arms
                # reach exactly two rows up)
                for c in lh:
                    r[("lh", c)] = y
                    lh_links.append((n, c, b))
                    y += 1
                if any(c == nxt for c, _amt in consumers[n]):
                    r["bus"] = y
                    y += 1
            if fluid_ing[n]:
                r["pair"] = y
                r["trunk"] = y + 1
                y += 2
            ypos[(n, b)] = r
        y += 3

    def dst_row(src, dst, b):
        a = row_assign[dst]
        for slot in ("farN", "nearN", "nearS"):
            ing = a.get(slot)
            if ing and ing[1] == ("stage", src):
                return ypos[(dst, b)][slot]
        raise BankInapplicable(f"no input row on {dst} for {src}")

    # ---- positions: prefix demand along adjacent buses ---------------------------------
    # WEST ROOM: a consumer's bus pick-tiles only receive what is dropped at or
    # west of them, so every stage must sit far enough east that its upstream
    # producers fit west of its FIRST machine (the flow oracle caught a lone
    # consumer pinned to the west edge starving on one arm's worth of supply).
    stage_xs: dict = {}
    for b in range(blocks):
        lo = min_slot_idx[last_stage]
        avail = machine_slots[lo:] or machine_slots
        stage_xs[(last_stage, b)] = [avail[j] for j in _spread(
            per_block[last_stage][b], len(avail))]
        for i in range(len(stages) - 2, -1, -1):
            n = stages[i]
            nb = per_block[n][b]
            nxt = stages[i + 1]
            adj = any(c == nxt for c, _a in consumers[n])
            lo = min_slot_idx[n]
            pool = machine_slots[lo:] or machine_slots
            if not adj or nb == 0 or (nxt, b) not in stage_xs:
                stage_xs[(n, b)] = [pool[j] for j in _spread(nb, len(pool))]
                continue
            cons_xs = stage_xs[(nxt, b)]
            supply = slots[n]["rate"] * out_amt[n]
            need = slots[nxt]["rate"] * needs[nxt].get(product[n], 0.0)
            xs_p, acc, ci = [], 0.0, 0
            for k in range(nb):
                covered = k * supply
                while ci < len(cons_xs) - 1 and acc + need <= covered + 1e-9:
                    acc += need
                    ci += 1
                # aim one slot WEST of the covered consumer: drops must flow INTO
                # its pick tiles, so same-slot only delivers one arm's worth
                tgt = cons_xs[min(ci, len(cons_xs) - 1)]
                cand = [s for s in machine_slots if s < tgt]
                xs_p.append(cand[-1] if cand else tgt)
            seen, fixed = set(), []
            for x in xs_p:
                if x in seen:                      # nearest FREE slot, east preferred
                    free = [s for s in machine_slots if s not in seen]
                    if not free:
                        raise BankInapplicable("more machines than row slots")
                    x = min(free, key=lambda s: (abs(s - x), s < x))
                seen.add(x)
                fixed.append(x)
            stage_xs[(n, b)] = sorted(fixed)

    # ---- PASS 2: emit -------------------------------------------------------------------
    lay = Layout()
    g2 = Graph()
    g2.add_node(Node(out_node, NodeKind.OUTPUT, rate=graph.nodes[out_node].rate))
    copies: dict = {n: [] for n in stages}
    raw_rows_pending: dict = {}
    sub_positions = []
    input_ct = 0
    port_feeders: dict = {}                        # (stage, port_row) -> [copy names]

    def belt_row(yy, net, x0=X_IN + 3, x1=None, direction=E):
        for x in range(x0, x1 if x1 is not None else W):
            lay.add(PlacedEntity(BELT, x, yy, direction=direction,
                                 meta={"net": f"b:{net}"}))

    for b in range(blocks):
        for i, n in enumerate(stages):
            a = row_assign[n]
            r = ypos[(n, b)]
            nb = per_block[n][b]
            sl = slots[n]
            for slot in ("farN", "nearN", "nearS"):
                ing = a.get(slot)
                if ing and ing[1][0] == "raw":
                    raw_rows_pending.setdefault(ing[1][1], []).append(
                        (r[slot], slot == "nearN" and not fluid_ing[n]))
            xs = stage_xs[(n, b)]
            nxt = stages[i + 1] if i + 1 < len(stages) else None
            lh = sorted({c for c, _amt in consumers[n] if c != nxt})
            # output ports (row per port), reach-gated in plan_dag
            if n == last_stage:
                ports = [yy for yy, bb in collector_rows if bb == b]
                port_share = [1.0 / len(ports)] * len(ports)
            else:
                # the BUS is port 0 so its drops take face[0] -- the adjacency reach
                # rule assumes the bus drop sits at the machine's west face; long-haul
                # rows deliver from the east margin, so their drop x is irrelevant.
                # Arms are dealt to ports by DEMAND share, not evenly (the flow
                # oracle caught a 50/50 split starving the higher-demand port).
                ports, port_share = [], []
                if "bus" in r:
                    ports.append(r["bus"])
                    port_share.append(sum(unit[c] * amt for c, amt in consumers[n]
                                          if c == nxt))
                for c in lh:
                    ports.append(r[("lh", c)])
                    port_share.append(unit[c] * needs[c].get(product[n], 0.0))
                tot = sum(port_share) or 1.0
                port_share = [s / tot for s in port_share]
            for k in range(nb):
                x = slot_x(xs[k])
                mname = f"{n}_{len(copies[n]) + 1}"
                copies[n].append(mname)
                g2.add_node(Node(mname, graph.nodes[n].kind,
                                 recipe=graph.nodes[n].recipe))
                lay.add(PlacedEntity(_MACHINE_KINDS[graph.nodes[n].kind], x,
                                     r["mach"], recipe=graph.nodes[n].recipe,
                                     direction=(S if fluid_ing[n] else None),
                                     meta={"node": mname}))
                proto_m = _MACHINE_KINDS[graph.nodes[n].kind]
                box_x = None
                if fluid_ing[n]:
                    conns = _fluid_connections(proto_m, x, r["mach"], S,
                                               with_dir=True)
                    ins = [(t, md) for t, fl, md in conns if fl == "input"]
                    box = next(t for t, md in ins if t[1] == r["arm_out"])
                    box_x = box[0]
                    md = next(md for t, md in ins if t == box)
                    lay.add(PlacedEntity(PIPE_TO_GROUND, box_x, r["arm_out"],
                                         direction=md, meta={}))
                    lay.add(PlacedEntity(PIPE_TO_GROUND, box_x, r["pair"],
                                         direction=S, meta={}))
                face = [x, x + 1, x + 2]
                fi = 0
                for _ in range(sl["k_far"]):
                    lay.add(PlacedEntity(LONG_INSERTER, face[fi], r["arm_in"],
                                         direction=N, meta={"role": "in"}))
                    fi += 1
                for _ in range(sl["k_near"]):
                    lay.add(PlacedEntity(INSERTER, face[fi], r["arm_in"],
                                         direction=N, meta={"role": "in"}))
                    fi += 1
                face = [x, x + 1, x + 2]
                sface = [fx for fx in (x, x + 1, x + 2) if fx != box_x]
                si = 0
                for _ in range(sl["k_sin"]):
                    lay.add(PlacedEntity(INSERTER, sface[si], r["arm_out"],
                                         direction=S, meta={"role": "in"}))
                    si += 1
                for j in range(sl["k_out"]):
                    gi = k * sl["k_out"] + j       # deal arm gi to the port whose
                    acc, tgt = 0.0, ports[0]       # cumulative share bucket it hits
                    frac = (gi + 0.5) / max(nb * sl["k_out"], 1)
                    for p_i, share in enumerate(port_share):
                        acc += share
                        if frac <= acc + 1e-9:
                            tgt = ports[p_i]
                            break
                    real_depth = tgt - r["arm_out"]
                    proto = INSERTER if real_depth == 1 else LONG_INSERTER
                    lay.add(PlacedEntity(proto, sface[si + j], r["arm_out"],
                                         direction=N, meta={"role": "out"}))
                    port_feeders.setdefault((n, tgt), []).append(mname)
            # belt rows owned by this stage
            if "bus" in r and n != last_stage and \
                    any(c == nxt for c, _amt in consumers[n]):
                belt_row(r["bus"], n)
            for c in lh:
                belt_row(r[("lh", c)], n)
            if a.get("farN") and a["farN"][1][0] == "stage" \
                    and not (i and a["farN"][1][1] == stages[i - 1]):
                pass                               # long-haul dst: emitted below
            if fluid_ing[n] and nb:
                fname = f"fl_{fluid_ing[n]}_{b}_{i}"
                g2.add_node(Node(fname, NodeKind.FLUID, item=fluid_ing[n]))
                lay.add(PlacedEntity(FLUID_SOURCE, X_IN, r["trunk"],
                                     item=fluid_ing[n], meta={"node": fname}))
                for x in range(X_IN + 1, W):
                    lay.add(PlacedEntity(PIPE, x, r["trunk"], meta={}))
                for c in _block_slice(copies[n], per_block[n], b):
                    g2.add_edge(fname, c, fluid=True)
            for p in power_slots + [width_slots]:
                sub_positions.append((slot_x(p), r["mach"]))
            sub_positions.append((-8, r["mach"] + 1))

    # ---- long-haul margin runs -----------------------------------------------------------
    next_col = W + 3
    for (src, dst, b) in lh_links:
        y_src = ypos[(src, b)][("lh", dst)]
        y_dst = dst_row(src, dst, b)
        col = next_col
        next_col += 2
        tag = {"net": f"b:{src}"}
        for x in range(W, col):
            lay.add(PlacedEntity(BELT, x, y_src, direction=E, meta=tag))
        lay.add(PlacedEntity(BELT, col, y_src, direction=S, meta=tag))
        for yv in range(y_src + 1, y_dst):
            lay.add(PlacedEntity(BELT, col, yv, direction=S, meta=tag))
        lay.add(PlacedEntity(BELT, col, y_dst, direction=W_DIR, meta=tag))
        for x in range(col - 1, X_IN + 2, -1):
            lay.add(PlacedEntity(BELT, x, y_dst, direction=W_DIR, meta=tag))

    # ---- raw boundaries ------------------------------------------------------------------
    split_ct = 0
    for item, rows_y in sorted(raw_rows_pending.items()):
        demand = raw_unit.get(item, 0.0) * target
        n_boundary = max(1, math.ceil(demand / BELT_FULL))
        near_ok = all(ok for _y, ok in rows_y)
        rows_y = sorted({y for y, _ok in rows_y})
        if n_boundary == 1 and len(rows_y) == 2 and near_ok:
            split_ct += 1
            y0, y1 = rows_y
            input_ct += 1
            iname = f"in_{item}_{input_ct}"
            g2.add_node(Node(iname, NodeKind.INPUT, item=item))
            tag_i = {"net": f"b:{iname}"}
            lay.add(PlacedEntity(CHEST_INPUT, X_IN, y0, item=item,
                                 meta={"node": iname}))
            lay.add(PlacedEntity(LOADER, X_IN + 1, y0, direction=E,
                                 loader_type="output", meta=tag_i))
            lay.add(PlacedEntity(SPLITTER, X_IN + 3, y0, direction=E, meta=tag_i))
            for x in range(X_IN + 4, W):
                lay.add(PlacedEntity(BELT, x, y0, direction=E, meta=tag_i))
            dcol = X_IN - 1 - 2 * split_ct     # unique descent column per item
            lay.add(PlacedEntity(BELT, X_IN + 4, y0 + 1, direction=E, meta=tag_i))
            lay.add(PlacedEntity(BELT, X_IN + 5, y0 + 1, direction=S, meta=tag_i))
            lay.add(PlacedEntity(BELT, X_IN + 5, y0 + 2, direction=W_DIR,
                                 meta=tag_i))
            for x in range(X_IN + 4, dcol, -1):
                lay.add(PlacedEntity(BELT, x, y0 + 2, direction=W_DIR, meta=tag_i))
            lay.add(PlacedEntity(BELT, dcol, y0 + 2, direction=S, meta=tag_i))
            for yv in range(y0 + 3, y1):
                lay.add(PlacedEntity(BELT, dcol, yv, direction=S, meta=tag_i))
            lay.add(PlacedEntity(BELT, dcol, y1, direction=E, meta=tag_i))
            for x in range(dcol + 1, W):
                lay.add(PlacedEntity(BELT, x, y1, direction=E, meta=tag_i))
            raw_rows_pending[item] = {"iname": iname}
        else:
            names = {}
            for yy in rows_y:
                input_ct += 1
                iname = f"in_{item}_{input_ct}"
                g2.add_node(Node(iname, NodeKind.INPUT, item=item))
                lay.add(PlacedEntity(CHEST_INPUT, X_IN, yy, item=item,
                                     meta={"node": iname}))
                lay.add(PlacedEntity(LOADER, X_IN + 1, yy, direction=E,
                                     loader_type="output",
                                     meta={"net": f"b:{iname}"}))
                belt_row(yy, iname)
                names[yy] = iname
            raw_rows_pending[item] = {"rows": names}

    # ---- collectors + exit weave ----------------------------------------------------------
    tag = {"net": f"b:{last_stage}"}
    ys = sorted(yy for yy, _b in collector_rows)
    if len(ys) > 2:
        raise BankInapplicable("more than two collector lanes")
    for yy in ys:
        belt_row(yy, last_stage)
    out_y = ys[0]
    # the weave column sits EAST of every long-haul column (its climb crosses many
    # rows; nothing else may live there), and the output belt extends to meet it
    weave_col = next_col + 1
    end_x = (weave_col + 3) if len(ys) == 2 else W + 6
    for x in range(W, end_x):
        lay.add(PlacedEntity(BELT, x, out_y, direction=E, meta=tag))
    if len(ys) == 2:
        yy = ys[1]
        col = weave_col
        for x in range(W, col):
            lay.add(PlacedEntity(BELT, x, yy, direction=E, meta=tag))
        if yy > out_y + 1:
            lay.add(PlacedEntity(BELT, col, yy, direction=N, meta=tag))
            for yv in range(out_y + 2, yy):
                lay.add(PlacedEntity(BELT, col, yv, direction=N, meta=tag))
        lay.add(PlacedEntity(UNDERGROUND, col, out_y + 1, direction=N,
                             ug_type="input", meta=tag))
        lay.add(PlacedEntity(UNDERGROUND, col, out_y - 1, direction=N,
                             ug_type="output", meta=tag))
        lay.add(PlacedEntity(BELT, col, out_y - 2, direction=E, meta=tag))
        lay.add(PlacedEntity(BELT, col + 1, out_y - 2, direction=S, meta=tag))
        lay.add(PlacedEntity(BELT, col + 1, out_y - 1, direction=S, meta=tag))
    lay.add(PlacedEntity(LOADER, end_x, out_y, direction=E, loader_type="input",
                         meta=tag))
    lay.add(PlacedEntity(CHEST_OUTPUT, end_x + 2, out_y, meta={"node": out_node}))
    if end_x > slot_x(width_slots) + 9:            # exit beyond the field's power
        sub_positions.append((end_x, out_y + 2))

    # ---- power ---------------------------------------------------------------------------
    seen_sub = set()
    for sx_, sy_ in sub_positions:
        if (sx_, sy_) in seen_sub:
            continue
        seen_sub.add((sx_, sy_))
        lay.add(PlacedEntity(SUBSTATION, sx_, sy_, meta={}))
    if sub_positions:
        lay.add(PlacedEntity(EEI, X_IN - 1, min(s[1] for s in sub_positions) - 7,
                             meta={}))

    # ---- expanded spec edges ---------------------------------------------------------------
    for b in range(blocks):
        for i, n in enumerate(stages):
            a = row_assign[n]
            block_copies = _block_slice(copies[n], per_block[n], b)
            for slot in ("farN", "nearN", "nearS"):
                ing = a.get(slot)
                if not ing:
                    continue
                if ing[1][0] == "raw":
                    reg = raw_rows_pending[ing[1][1]]
                    iname = (reg.get("iname") if isinstance(reg, dict) and
                             reg.get("iname") else reg["rows"][ypos[(n, b)][slot]])
                    for c in block_copies:
                        g2.add_edge(iname, c)
                else:
                    src = ing[1][1]
                    src_copies = _block_slice(copies[src], per_block[src], b)
                    # only copies that actually have an arm on this port feed it
                    # (arms are recorded under the SOURCE's port row: the bus row for
                    # adjacent consumers, the source-side lh row for long-hauls)
                    src_r = ypos[(src, b)]
                    port_row = src_r["bus"] if ("bus" in src_r and
                                                ypos[(n, b)][slot] == src_r["bus"]) \
                        else src_r.get(("lh", n))
                    feeders = set(port_feeders.get((src, port_row), []))
                    adjacent = (i and src == stages[i - 1] and slot == "farN")
                    if adjacent:
                        pxs, cxs = stage_xs[(src, b)], stage_xs[(n, b)]
                        k_far = slots[n]["k_far"]
                        for pj, p in enumerate(src_copies):
                            if p not in feeders:
                                continue
                            for cj, c in enumerate(block_copies):
                                if slot_x(pxs[pj]) <= slot_x(cxs[cj]) + \
                                        max(k_far - 1, 0):
                                    g2.add_edge(p, c)
                    else:                          # long-haul: full supply arrives
                        for p in src_copies:       # at the row's east end
                            if p not in feeders:
                                continue
                            for c in block_copies:
                                g2.add_edge(p, c)
    for c in copies[last_stage]:
        g2.add_edge(c, out_node)

    plan = {
        "mode": "bank",
        "target_per_s": {out_node: round(target, 4)},
        "machines": {n: {"copies": counts[n],
                         "per_copy_crafts_per_s": round(slots[n]["rate"], 4),
                         "arms": {k: slots[n][k]
                                  for k in ("k_far", "k_near", "k_sin", "k_out")}}
                     for n in stages},
        "blocks": blocks,
        "collectors": len(ys),
        "expected_actual_per_s": {out_node: round(_expected(
            stages, counts, slots, out_amt, unit, raw_unit, in_rates, len(ys)), 4)},
    }
    return g2, plan, lay


def _expected(stages, counts, slots, out_amt, unit, raw_unit, in_rates, n_coll):
    lim = min(n_coll * LANE_CAP, BELT_FULL)
    for n in stages:
        cap = counts[n] * (slots[n]["rate"] / SAFETY) * out_amt[n]
        per_out = unit[n] * out_amt[n]
        if per_out > 0:
            lim = min(lim, cap / per_out)
    for i, cap in in_rates.items():
        if raw_unit.get(i):
            lim = min(lim, cap / raw_unit[i])
    return lim


def _spread(nb, width_slots):
    if nb >= width_slots:
        return list(range(nb))
    return [round(k * (width_slots - 1) / max(nb - 1, 1)) for k in range(nb)] \
        if nb > 1 else [width_slots // 2]


def _block_slice(all_copies, per_block, b):
    start = sum(per_block[:b])
    return all_copies[start:start + per_block[b]]
