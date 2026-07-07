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
        fl = sorted(i for i, (a, t) in typed.items() if t == "fluid")
        if len(fl) > 2:
            raise BankInapplicable(f"stage {n} needs {len(fl)} fluids (a chem "
                                   f"plant has two input boxes)")
        fluid_ing[n] = fl
        for f in fl:
            if not any(nd.kind is NodeKind.FLUID and nd.item == f
                       for nd in graph.nodes.values()):
                raise BankInapplicable(f"fluid {f!r} has no boundary source")

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
        if len(needs[n]) > 4:
            raise BankInapplicable(f"stage {n} has {len(needs[n])} ingredients "
                                   f"(template rows fit 4)")
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
            # 4th ingredient: PAIR it with the nearN occupant (two products, one
            # per lane side, both delivered via the east margin which controls
            # entry sides). Per-lane flow is kept legal by the long-haul block
            # sizing (each member is its own chain).
            extra = pool.pop(0)
            mate = assign.get("nearN")
            if (not pool and extra[1][0] == "stage" and mate
                    and mate[1][0] == "stage"
                    and mate[1] != ("stage", prev)):
                assign["nearN"] = [mate, extra]
            else:
                raise BankInapplicable(
                    f"stage {n}: more belt inputs than template rows (pairing "
                    f"needs two stage-sourced inputs on nearN)")
        if "farN" in assign and "nearN" not in assign and adj:
            # bus-only stage: with no near row between, the adjacent bus sits at
            # depth 1 -- it belongs on the NEAR slot (normal arms)
            assign["nearN"] = assign.pop("farN")
        rows[n] = assign

    # OUTPUT-port gate: consumers = adjacent (bus at depth 1/2) + long-hauls; the
    # arm lane reaches two rows, and a nearS input occupies depth 1.
    cons: dict[str, set] = {n: set() for n in order}
    for n in order:
        for ing, (kind, src) in sources[n].items():
            if kind == "stage":
                cons[src].add(n)

    chains: dict = {}
    for i, n in enumerate(order):
        nxt = order[i + 1] if i + 1 < len(order) else None
        lh = sorted((c for c in cons[n] if c != nxt), key=order.index)
        max_ports = 1 if rows[n].get("nearS") else 2
        n_slots = max_ports - (1 if nxt in cons[n] else 0)
        if lh and n_slots < 1:
            raise BankInapplicable(f"stage {n}: no output row left for long-hauls")
        # CASCADE: split lh consumers into <= n_slots chains; a chain's row serves
        # its head from the east margin, and the leftover wraps around the WEST
        # margin down to the next member (combined flow shares the lane)
        def is_pair_dst(c):
            v = rows[c].get("nearN")
            return isinstance(v, list) and any(s == ("stage", n) for _i, s in v)
        lh.sort(key=lambda c: (0 if is_pair_dst(c) else 1, order.index(c)))
        chains[n] = [lh[j::min(n_slots, len(lh))]
                     for j in range(min(n_slots, len(lh)))] if lh else []
    return (order, raws, outputs[0], product, needs, sources, rows, chains,
            fluid_ing)


_ARM_RATE = {"farN": LONG_ARM_PICK, "nearN": ARM_BELT_PICK, "nearS": ARM_BELT_PICK}


def _slot_members(v):
    """A slot holds one (ing, src) or a PAIR [(ing, src), (ing, src)]."""
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def _slot_amount(needs, v):
    return sum(needs[ing] for ing, _s in _slot_members(v))


def _stage_slots(cap, needs, assign, out_amount, has_s_input, s_face_slots=3,
                 n_face_slots=3):
    """Best per-machine arm allocation for the row assignment. N face: 3 slots
    (farN long + nearN normal). S face: 3 slots shared by nearS input arms and the
    output arms (LONG when a nearS row exists -- they reach over it)."""
    best = None
    for k_far in range(0, n_face_slots + 1):
        for k_near in range(0, n_face_slots + 1 - k_far):
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
                        amt = _slot_amount(needs, ing)
                        x = min(x, k * _ARM_RATE[slot] / amt * SAFETY)
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
    (stages, raws, out_node, product, needs, sources, row_assign, lh_chains,
     fluid_ing) = plan_dag(graph, dumper)

    caps, out_amt = {}, {}
    for n in stages:
        crafts, _p, items = _machine_cap(graph.nodes[n], dumper)
        caps[n] = crafts
        out_amt[n] = items / crafts if crafts else 1.0

    # fluid-stage machines are ROTATED 180 degrees: their fluid boxes face SOUTH,
    # the pipe trunk runs BELOW the stage, and the north side keeps full bus
    # adjacency. The box costs one S-face slot.
    # rotation per fluid stage: boxes SOUTH (trunk below) when the stage is last
    # or takes an adjacent bus from above; boxes NORTH (trunk above) when it has an
    # adjacent consumer below. Both at once cannot host the trunk rows.
    consumers0: dict = {n: [] for n in stages}
    for n in stages:
        for ing, (kind, s) in sources[n].items():
            if kind == "stage":
                consumers0[s].append(n)
    rotated = {}
    for i, n in enumerate(stages):
        if not fluid_ing[n]:
            rotated[n] = False
            continue
        prev = stages[i - 1] if i else None
        nxt = stages[i + 1] if i + 1 < len(stages) else None
        bus_in = row_assign[n].get("farN") and             row_assign[n]["farN"][1] == ("stage", prev)
        bus_out = nxt in consumers0[n]
        if bus_in and bus_out:
            raise BankInapplicable(f"fluid stage {n} is bus-fed AND bus-feeding "
                                   f"(no side left for pipe trunks)")
        rotated[n] = bool(bus_in) or not bus_out
    slots = {n: _stage_slots(caps[n], needs[n], row_assign[n], out_amt[n],
                             has_s_input="nearS" in row_assign[n],
                             s_face_slots=3 - (len(fluid_ing[n]) if rotated[n] else 0),
                             n_face_slots=3 - (0 if rotated[n] else len(fluid_ing[n])))
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

    counts = {}
    for n in stages:
        margin = 1.3 if isinstance(row_assign[n].get("nearN"), list) else 1.0
        counts[n] = max(1, math.ceil(unit[n] * target * margin / slots[n]["rate"]))
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
    # drop-fed row is ONE lane (7.5/s). Rather than reject, RAISE the block count
    # until every link's per-block flow fits its lane.
    def chain_flow(n, chain):
        return sum(unit[c] * needs[c].get(product[n], 0.0) for c in chain) * target
    lh_blocks = 1
    for n in stages:
        for chain in lh_chains[n]:
            lh_blocks = max(lh_blocks, math.ceil(
                chain_flow(n, chain) / (LANE_CAP * LANE_HEADROOM)))

    # ---- blocks -------------------------------------------------------------------------
    lane_usable = BELT_FULL * LANE_HEADROOM
    blocks = lh_blocks
    for ing, d in raw_unit.items():
        blocks = max(blocks, math.ceil(d * target / lane_usable))
    # every block must cover its collector share on its own (a floored block
    # delivered 6.9/s into a 7.5 lane -- the measured 14.7-cluster): round UP
    per_block = {n: [math.ceil(counts[n] / blocks)] * blocks for n in stages}
    for n in stages:
        counts[n] = sum(per_block[n])

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
            if fluid_ing[n] and not rotated[n]:
                y += 1                             # spacer: trunks must never touch
                for fi_, f in enumerate(fluid_ing[n]):
                    r[("trunk", fi_)] = y
                    r[("pair", fi_)] = y + 1
                    y += 2
            adj_far = (a.get("farN") and a["farN"][1] == ("stage", prev))
            adj_near = (a.get("nearN") and not isinstance(a["nearN"], list)
                        and a["nearN"][1] == ("stage", prev))
            if a.get("farN") and not adj_far:
                r["farN"] = y
                y += 1
            elif adj_far:
                r["farN"] = ypos[(prev, b)]["bus"]
            if a.get("nearN") and not adj_near:
                r["nearN"] = y
                y += 1
            elif adj_near:
                r["nearN"] = ypos[(prev, b)]["bus"]
            r["arm_in"] = y
            r["mach"] = y + 1
            r["arm_out"] = y + 4
            y += 5
            if a.get("nearS"):
                r["nearS"] = y
                y += 1
            nxt = stages[i + 1] if i + 1 < len(stages) else None
            lh = [ch[0] for ch in lh_chains[n]]
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
                for ch in lh_chains[n]:
                    r[("lh", ch[0])] = y
                    lh_links.append((n, tuple(ch), b))
                    y += 1
                if any(c == nxt for c, _amt in consumers[n]):
                    r["bus"] = y
                    y += 1
            if rotated[n]:
                for fi_, f in enumerate(fluid_ing[n]):
                    r[("pair", fi_)] = y
                    r[("trunk", fi_)] = y + 1
                    y += 2
                y += 1                             # spacer: trunks must never touch
            ypos[(n, b)] = r
        y += 3

    def dst_row(src, dst, b):
        """(row_y, pair_side) -- pair_side is None for plain rows, else 'N'/'S'
        entry for this source's product."""
        a = row_assign[dst]
        for slot in ("farN", "nearN", "nearS"):
            v = a.get(slot)
            for mi, ing in enumerate(_slot_members(v)):
                if ing[1] == ("stage", src):
                    side = None
                    if isinstance(v, list):
                        side = "N" if mi == 0 else "S"
                    return ypos[(dst, b)][slot], side
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
                for ing in _slot_members(a.get(slot)):
                    if ing[1][0] == "raw":
                        raw_rows_pending.setdefault(ing[1][1], []).append(
                            (r[slot], slot == "nearN" and not fluid_ing[n]))
            xs = stage_xs[(n, b)]
            nxt = stages[i + 1] if i + 1 < len(stages) else None
            lh = [ch[0] for ch in lh_chains[n]]
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
                for ch in lh_chains[n]:
                    ports.append(r[("lh", ch[0])])
                    port_share.append(sum(unit[c] * needs[c].get(product[n], 0.0)
                                          for c in ch))
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
                                     direction=(S if (fluid_ing[n] and rotated[n])
                                                else None),
                                     meta={"node": mname}))
                proto_m = _MACHINE_KINDS[graph.nodes[n].kind]
                box_xs = []
                nbox_xs = []
                if fluid_ing[n]:
                    mdir = S if rotated[n] else 0
                    shaft_row = r["arm_out"] if rotated[n] else r["arm_in"]
                    conns = _fluid_connections(proto_m, x, r["mach"], mdir,
                                               with_dir=True)
                    ins = [(t, md) for t, fl_, md in conns if fl_ == "input"
                           and t[1] == shaft_row]
                    for fi_, f in enumerate(fluid_ing[n]):
                        t, md = ins[fi_]
                        (box_xs if rotated[n] else nbox_xs).append(t[0])
                        lay.add(PlacedEntity(PIPE_TO_GROUND, t[0], shaft_row,
                                             direction=md, meta={}))
                        lay.add(PlacedEntity(PIPE_TO_GROUND, t[0],
                                             r[("pair", fi_)],
                                             direction=(S if rotated[n] else N),
                                             meta={}))
                face = [fx for fx in (x, x + 1, x + 2) if fx not in nbox_xs]
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
                sface = [fx for fx in (x, x + 1, x + 2) if fx not in box_xs]
                si = 0
                for _ in range(sl["k_sin"]):
                    lay.add(PlacedEntity(INSERTER, sface[si], r["arm_out"],
                                         direction=S, meta={"role": "in"}))
                    si += 1
                for j in range(sl["k_out"]):
                    gi = k * sl["k_out"] + j
                    # largest-remainder arm allocation with a >=1 floor per port:
                    # frac buckets starved ports whose share < 1/(arms per block)
                    if gi == 0:
                        n_arms = max(nb * sl["k_out"], len(ports))
                        alloc = [max(1, round(s * n_arms)) for s in port_share]
                        while sum(alloc) > n_arms and max(alloc) > 1:
                            alloc[alloc.index(max(alloc))] -= 1
                        while sum(alloc) < n_arms:
                            alloc[alloc.index(max(alloc))] += 1
                        cuts = []
                        acc_a = 0
                        for a_ in alloc:
                            acc_a += a_
                            cuts.append(acc_a)
                        r["_cuts"] = cuts
                    tgt = ports[-1]
                    for p_i, cut in enumerate(r["_cuts"]):
                        if gi < cut:
                            tgt = ports[p_i]
                            break
                    real_depth = tgt - r["arm_out"]
                    proto = INSERTER if real_depth == 1 else LONG_INSERTER
                    lay.add(PlacedEntity(proto, sface[si + j], r["arm_out"],
                                         direction=N, meta={"role": "out"}))
                    d = port_feeders.setdefault((n, tgt), {})
                    d[mname] = min(d.get(mname, 1 << 30), sface[si + j])
            # belt rows owned by this stage
            if "bus" in r and n != last_stage and \
                    any(c == nxt for c, _amt in consumers[n]):
                belt_row(r["bus"], n)
            for c in lh:
                belt_row(r[("lh", c)], n)
            if a.get("farN") and a["farN"][1][0] == "stage" \
                    and not (i and a["farN"][1][1] == stages[i - 1]):
                pass                               # long-haul dst: emitted below
            for fi_, f in enumerate(fluid_ing[n]):
                if not nb:
                    continue
                fname = f"fl_{f}_{b}_{i}"
                g2.add_node(Node(fname, NodeKind.FLUID, item=f))
                lay.add(PlacedEntity(FLUID_SOURCE, X_IN, r[("trunk", fi_)],
                                     item=f, meta={"node": fname}))
                for x in range(X_IN + 1, W):
                    lay.add(PlacedEntity(PIPE, x, r[("trunk", fi_)], meta={}))
                for c in _block_slice(copies[n], per_block[n], b):
                    g2.add_edge(fname, c, fluid=True)
            for p in power_slots + [width_slots]:
                sub_positions.append((slot_x(p), r["mach"]))
            sub_positions.append((-8, r["mach"] + 1))

    # ---- long-haul margin runs -----------------------------------------------------------
    # each link owns a margin column (spaced 3); horizontal runs HOP over earlier
    # links' columns with underground belts (channel routing: a run at row y must
    # not weld into a foreign descent crossing that row)
    next_col = W + 3
    taken_cols: list = []                          # (col, y_lo, y_hi)

    def _run_east(x0, x1, yy, tag, cols=None):
        cols = taken_cols if cols is None else cols
        x = x0
        while x < x1:
            block_col = next((c for c, lo, hi in cols
                              if c == x + 1 and lo <= yy <= hi), None)
            if block_col is not None and x + 2 < x1:
                lay.add(PlacedEntity(UNDERGROUND, x, yy, direction=E,
                                     ug_type="input", meta=tag))
                lay.add(PlacedEntity(UNDERGROUND, x + 2, yy, direction=E,
                                     ug_type="output", meta=tag))
                x += 3
            else:
                lay.add(PlacedEntity(BELT, x, yy, direction=E, meta=tag))
                x += 1

    def _run_west(x0, x1, yy, tag, cols=None):
        """West-flowing run from x0 down to x1 (exclusive)."""
        cols = taken_cols if cols is None else cols
        x = x0
        while x > x1:
            block_col = next((c for c, lo, hi in cols
                              if c == x - 1 and lo <= yy <= hi), None)
            if block_col is not None and x - 2 > x1:
                lay.add(PlacedEntity(UNDERGROUND, x, yy, direction=W_DIR,
                                     ug_type="input", meta=tag))
                lay.add(PlacedEntity(UNDERGROUND, x - 2, yy, direction=W_DIR,
                                     ug_type="output", meta=tag))
                x -= 3
            else:
                lay.add(PlacedEntity(BELT, x, yy, direction=W_DIR, meta=tag))
                x -= 1

    # PAIR rows need N-side deliverers processed FIRST: the row's east end is then
    # fixed before any S-side column is allocated east of it (an S descent crossing
    # a later row extension was the measured clash)
    def _link_side(link):
        src, chain, b = link
        try:
            _y, side = dst_row(src, chain[0], b)
        except BankInapplicable:
            side = None
        return 1 if side == "S" else 0
    lh_links.sort(key=lambda lk: (lk[2], _link_side(lk)))

    wcol_next = [X_IN - 3]                         # west margin column allocator
    #                                                (shared: consolidation splits
    #                                                AND cascade wraps draw from it)

    taken_wcols: list = []

    def alloc_wcol():
        c = wcol_next[0]
        wcol_next[0] -= 3
        return c
    pair_rows_done: dict = {}                      # (y, b) -> east extent emitted

    def deliver(src, dst, b, y_src_col, tag):
        """Bring flow from the allocated column down into dst's input row."""
        y_dst, side = dst_row(src, dst, b)
        col = y_src_col
        if side is None:
            lay.add(PlacedEntity(BELT, col, y_dst, direction=W_DIR, meta=tag))
            _run_west(col - 1, X_IN + 2, y_dst, tag)
            return y_dst
        # PAIR row: emit the shared west-flowing row ONCE (up to the first
        # deliverer's column); later deliverers side-load their lane
        if (y_dst, b) not in pair_rows_done:
            pair_rows_done[(y_dst, b)] = col
            members = _slot_members(row_assign[dst].get("nearN"))
            prods = "|".join(product[m[1][1]] for m in members)
            # the row INCLUDES (col, y_dst): both deliverers side-load that tile
            # (north pushes south into it, south pushes north)
            _run_west(col, X_IN + 2, y_dst,
                      {"net": tag["net"], "pair_products": prods})
        row_e = pair_rows_done[(y_dst, b)]
        if side == "N":
            # descent's last tile pushes straight south into a row tile; make sure
            # the row REACHES this column -- hop-aware, since earlier deliverers'
            # descent columns may cross the extension
            if col > row_e:
                _run_west(col - 1, row_e - 1, y_dst,
                          {"net": tag["net"], "pair_products": "1"})
                pair_rows_done[(y_dst, b)] = col
            return y_dst                           # push from (col, y_dst-1) S
        # side == 'S': continue past the row, curve west (hop-aware: other pair
        # deliverers' descents cross this margin row), push north into the row's
        # east-end tile
        lay.add(PlacedEntity(BELT, col, y_dst + 1, direction=W_DIR, meta=tag))
        _run_west(col - 1, row_e, y_dst + 1, tag)
        lay.add(PlacedEntity(BELT, row_e, y_dst + 1, direction=N, meta=tag))
        return y_dst + 1

    for (src, chain, b) in lh_links:
        y_src = ypos[(src, b)][("lh", chain[0])]
        y_dst, side0 = dst_row(src, chain[0], b)
        col = next_col
        next_col += 3
        tag = {"net": f"b:{src}"}
        _run_east(W, col, y_src, tag)
        lay.add(PlacedEntity(BELT, col, y_src, direction=S, meta=tag))
        stop_y = y_dst if side0 != "S" else y_dst + 1
        for yv in range(y_src + 1, stop_y):
            lay.add(PlacedEntity(BELT, col, yv, direction=S, meta=tag))
        end_y = deliver(src, chain[0], b, col, tag)
        taken_cols.append((col, min(y_src, end_y), max(y_src, end_y)))
        # CASCADE: the leftover wraps around the WEST margin to later members
        prev_y = y_dst
        for c2 in chain[1:]:
            y2, side2 = dst_row(src, c2, b)
            if side2 is not None:
                raise BankInapplicable("pair row mid-cascade not supported")
            wcol = alloc_wcol()
            _run_west(X_IN + 2, wcol, prev_y, tag, cols=taken_wcols)
            lay.add(PlacedEntity(BELT, wcol, prev_y, direction=S, meta=tag))
            for yv in range(prev_y + 1, y2):
                lay.add(PlacedEntity(BELT, wcol, yv, direction=S, meta=tag))
            lay.add(PlacedEntity(BELT, wcol, y2, direction=E, meta=tag))
            _run_east(wcol + 1, X_IN + 3, y2, tag, cols=taken_wcols)
            belt_row(y2, src)                      # c2's row flows EAST, fed west
            taken_wcols.append((wcol, min(prev_y, y2), max(prev_y, y2)))
            prev_y = y2

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
            dcol = alloc_wcol()                # shared west-margin allocator
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
    # N collectors merge onto ONE output belt via alternating-lane rails: the first
    # collector IS the output row (its drops ride the south lane); every later
    # collector approaches on its own margin column -- from the SOUTH (climb, then
    # push north: entry side = south lane) or, when the south lane group is full,
    # from the NORTH (the weave: tunnel under the output row, curve, side-load from
    # above filling the north lane). Each lane group's flow must fit 7.5/s.
    tag = {"net": f"b:{last_stage}"}
    ys = sorted(yy for yy, _b in collector_rows)
    for yy in ys:
        belt_row(yy, last_stage)
    out_y = ys[0]
    # a collector lane physically carries at most 7.5/s (oversupply backs up into
    # the machines -- the measured greenchips behaviour); group by that reality
    per_coll = min(LANE_CAP, (target / len(ys)) if ys else 0.0)
    south_used = per_coll                          # collector 0's own drops
    merge_col = next_col + 1
    end_x = merge_col + 2 * max(len(ys) - 1, 0) + 4
    for x in range(W, end_x):
        lay.add(PlacedEntity(BELT, x, out_y, direction=E, meta=tag))
    for k, yy in enumerate(ys[1:], start=1):
        col = merge_col + 2 * (k - 1)
        for x in range(W, col):
            lay.add(PlacedEntity(BELT, x, yy, direction=E, meta=tag))
        if south_used + per_coll <= LANE_CAP + 1e-9:
            south_used += per_coll
            # SOUTH approach: climb to just below the output row, push north
            lay.add(PlacedEntity(BELT, col, yy, direction=N, meta=tag))
            for yv in range(out_y + 2, yy):
                lay.add(PlacedEntity(BELT, col, yv, direction=N, meta=tag))
            lay.add(PlacedEntity(BELT, col, out_y + 1, direction=N, meta=tag))
        else:
            # NORTH approach (the weave): tunnel under the output row
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
    # relay substations along the merge zone (north strip, clear of the approach
    # columns): long rails otherwise outrun the field grid's wire reach
    sx0 = slot_x(width_slots)
    x_relay = sx0 + 14
    last_relay = sx0
    while x_relay <= end_x + 2:
        sub_positions.append((x_relay, out_y - 4))
        last_relay = x_relay
        x_relay += 14
    if end_x - last_relay > 8:                     # the exit loader must be covered
        sub_positions.append((end_x - 1, out_y - 4))

    # ---- power ---------------------------------------------------------------------------
    # substations are the LAST geometry: nudge any that would collide with the
    # dynamically-allocated margins (long-haul columns, merge rails) until free
    occ = set()
    for e in lay.entities:
        occ.update(e.tiles() if hasattr(e, "tiles") else [(e.x, e.y)])
    seen_sub = set()
    for sx_, sy_ in sub_positions:
        placed_at = None
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (2, 0), (-2, 0), (3, 0), (-3, 0),
                       (0, 1), (0, -1), (4, 0), (-4, 0)):
            cx, cy = sx_ + dx, sy_ + dy
            tiles = {(cx, cy), (cx + 1, cy), (cx, cy + 1), (cx + 1, cy + 1)}
            if tiles & occ or (cx, cy) in seen_sub:
                continue
            placed_at = (cx, cy)
            break
        if placed_at is None:
            continue
        seen_sub.add(placed_at)
        occ.update({(placed_at[0], placed_at[1]), (placed_at[0] + 1, placed_at[1]),
                    (placed_at[0], placed_at[1] + 1),
                    (placed_at[0] + 1, placed_at[1] + 1)})
        lay.add(PlacedEntity(SUBSTATION, placed_at[0], placed_at[1], meta={}))
    if sub_positions:
        lay.add(PlacedEntity(EEI, X_IN - 1, min(s[1] for s in sub_positions) - 7,
                             meta={}))

    # ---- expanded spec edges ---------------------------------------------------------------
    for b in range(blocks):
        for i, n in enumerate(stages):
            a = row_assign[n]
            block_copies = _block_slice(copies[n], per_block[n], b)
            for slot in ("farN", "nearN", "nearS"):
              for ing in _slot_members(a.get(slot)):
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
                    if "bus" in src_r and ypos[(n, b)][slot] == src_r["bus"]:
                        port_row = src_r["bus"]
                    else:                          # chain member: the HEAD's row
                        head = next(ch[0] for ch in lh_chains[src] if n in ch)
                        port_row = src_r.get(("lh", head))
                    feeders = port_feeders.get((src, port_row), {})
                    adjacent = (i and src == stages[i - 1]
                                and slot in ("farN", "nearN"))
                    if adjacent:
                        cxs = stage_xs[(n, b)]
                        k_far = slots[n]["k_far" if slot == "farN" else "k_near"]
                        for pj, p in enumerate(src_copies):
                            if p not in feeders:
                                continue
                            drop_x = feeders[p]    # the copy's WESTMOST bus drop
                            for cj, c in enumerate(block_copies):
                                if drop_x <= slot_x(cxs[cj]) + max(k_far - 1, 0):
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
