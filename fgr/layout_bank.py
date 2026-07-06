"""The BANK generator: classic sandwich-row layouts for rate-sized chains.

Real belt builds don't route point-to-point lanes -- they put machines in rows with
belts as LOCAL buses: a cable machine drops onto the bus and the circuit machine two
tiles east picks it up, so no belt cross-section ever carries the aggregate flow.
That is why a hand-built 15/s circuit factory is ~2 orders of magnitude smaller than
a routed one and saturates in seconds. This module compiles that shape directly.

Template (y grows downward, belts flow EAST), one stage:

    [far belt]     second ingredient (bus from the previous stage, or a raw)
    [near belt]    first ingredient
    [arm lane]     normal inserters pick near (reach 1), LONG-HANDED pick far (reach 2)
    [machines]     3 rows
    [arm lane]     output arms: normal drop to the row below, LONG drop two below
    [bus belt]     this stage's product = next stage's far belt
    ...
    [collector(s)] last stage drops here; a drop-fed belt carries ONE lane (7.5/s),
                   so two half-rate collectors merge through a SPLITTER into a single
                   both-lanes output belt -> loader -> chest.

Sizing is derived from the TEMPLATE's arm slots (3 top, 3 bottom per machine) and the
game-measured calibration rates, so the plan and the layout cannot disagree. Blocks
tile vertically when a raw lane would exceed LANE_HEADROOM. Substation columns sit in
reserved 2-wide gaps of the machine rows; belts run through the gaps uninterrupted.

Applicability (v1): belt-only graphs, one output, every machine stage's item
ingredients drawn from raw inputs plus the immediately-previous stage. Anything else
falls back to the generic solver + v3 router.
"""

from __future__ import annotations

import math

from .ir import Graph, Node, NodeKind
from .layout import (ASSEMBLER, BELT, CHEMICAL, CHEST_INPUT, CHEST_OUTPUT, EEI,
                     FURNACE, INSERTER, LOADER, LONG_INSERTER, SPLITTER, SUBSTATION,
                     UNDERGROUND, Layout, PlacedEntity)
from .rates import (ARM_BELT_PICK, ARM_CHEST_PICK, BELT_FULL, LANE_CAP,
                    LONG_ARM_PICK, RatesUnavailable, _ingredients, _machine_cap)
from .solver import LANE_HEADROOM, SAFETY, SolveError
from . import fbsr_validation as fv

# directions
N, E, S = 0, 4, 8
W_DIR = 12
_MACHINE_KINDS = {NodeKind.ASSEMBLER: ASSEMBLER, NodeKind.CHEMICAL: CHEMICAL,
                  NodeKind.FURNACE: FURNACE}
POWER_PITCH = 15          # substation column every N machine slots (supply area 18)
X_IN = -6                 # the INPUT column: every boundary chest sits here, outside
#                           the factory body, so a player can box-select and replace
#                           the scaffolding with real feeds in one edit


class BankInapplicable(RuntimeError):
    """This spec doesn't fit the sandwich template; use the generic path."""


def plan_chain(graph: Graph, dumper):
    """Order machine stages and check template applicability. Returns
    (stages, raws, output) where stages = [(node, recipe-info)] topological."""
    if any(e.fluid for e in graph.edges):
        raise BankInapplicable("fluids not yet supported by the bank template")
    outputs = [n for n, nd in graph.nodes.items() if nd.kind is NodeKind.OUTPUT]
    if len(outputs) != 1:
        raise BankInapplicable("bank template handles exactly one output")
    machines = [n for n, nd in graph.nodes.items()
                if nd.kind in _MACHINE_KINDS and nd.recipe]
    raws = {n: nd.item for n, nd in graph.nodes.items()
            if nd.kind is NodeKind.INPUT}

    product, needs = {}, {}
    for n in machines:
        _c, prod, _i = _machine_cap(graph.nodes[n], dumper)
        product[n] = prod
        needs[n] = _ingredients(graph.nodes[n], dumper)

    # topological order along declared edges
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

    raw_items = set(raws.values())
    for i, n in enumerate(order):
        prev_prod = product[order[i - 1]] if i else None
        for ing in needs[n]:
            if ing in raw_items or ing == prev_prod:
                continue
            raise BankInapplicable(
                f"stage {n} needs {ing!r} which is neither a raw input nor the "
                f"previous stage's product")
        if len(needs[n]) > 2:
            raise BankInapplicable(f"stage {n} has {len(needs[n])} ingredients "
                                   f"(template rows fit 2)")
    return order, raws, outputs[0], product, needs


def _stage_slots(cap, needs, prev_prod, out_amount, last):
    """Best per-machine arm allocation. Top face: 3 slots shared between the FAR belt
    (long arms, 1.204) and the NEAR belt (normal, 0.9375). Bottom face: up to 3
    output arms (0.857 each; interior stages drop on the bus with normal arms; the
    last stage may split across two collectors -- normal + long)."""
    ings = list(needs.items())
    far_ing = prev_prod if prev_prod in needs else (ings[0][0] if len(ings) > 1 else None)
    if len(ings) == 1:
        far_ing = None
    near_ing = next((i for i, _a in ings if i != far_ing), None)
    best = None
    for k_out in (1, 2, 3):
        for k_far in range(0, 4):
            k_near_max = 3 - k_far
            for k_near in range(0, k_near_max + 1):
                x = cap * SAFETY
                if far_ing is not None:
                    if k_far == 0:
                        continue
                    x = min(x, k_far * LONG_ARM_PICK / needs[far_ing] * SAFETY)
                elif k_far:
                    continue
                if near_ing is not None:
                    if k_near == 0:
                        continue
                    x = min(x, k_near * ARM_BELT_PICK / needs[near_ing] * SAFETY)
                elif k_near:
                    continue
                x = min(x, k_out * ARM_CHEST_PICK / out_amount * SAFETY)
                arms = k_far + k_near + k_out
                key = (round(x, 6), -arms)
                if best is None or key > best[0]:
                    best = (key, dict(k_far=k_far, k_near=k_near, k_out=k_out,
                                      far=far_ing, near=near_ing, rate=x))
    if best is None:
        raise BankInapplicable("no feasible arm allocation")
    return best[1]


def compile_bank(graph: Graph, dumper="auto"):
    """Compile a rate-annotated spec into a bank layout.

    Returns (expanded_graph, plan, layout): the expanded graph declares exactly the
    lanes the bank realizes (bus bicliques), so the standard verifier grades it."""
    if dumper == "auto":
        dumper = fv._fbsr_dumper()
    if dumper is None:
        raise RatesUnavailable("FBSR dumper unavailable")
    stages, raws, out_node, product, needs = plan_chain(graph, dumper)

    caps, out_amt = {}, {}
    for n in stages:
        crafts, _p, items = _machine_cap(graph.nodes[n], dumper)
        caps[n] = crafts
        out_amt[n] = items / crafts if crafts else 1.0

    # ---- per-stage slot allocation & target -------------------------------------------
    slots = {}
    for i, n in enumerate(stages):
        prev = product[stages[i - 1]] if i else None
        slots[n] = _stage_slots(caps[n], needs[n], prev, out_amt[n],
                                last=(i == len(stages) - 1))

    # unit demand: crafts of each stage per 1 item/s of final product
    unit = {stages[-1]: 1.0 / out_amt[stages[-1]]}
    for i in range(len(stages) - 2, -1, -1):
        n, nxt = stages[i], stages[i + 1]
        unit[n] = unit[nxt] * needs[nxt].get(product[n], 0.0) / out_amt[n]
    raw_unit = {}                                # raw item -> items/s per 1 output/s
    for n in stages:
        for ing, amt in needs[n].items():
            if ing in set(raws.values()) and ing != (product[stages[stages.index(n) - 1]] if stages.index(n) else None):
                raw_unit[ing] = raw_unit.get(ing, 0.0) + unit[n] * amt

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

    counts = {n: max(1, math.ceil(unit[n] * target / slots[n]["rate"]))
              for n in stages}
    # input-driven appetite clamp: built machines pull at their slot rate, not the
    # plan -- shed copies so a declared input is never overdrawn (<=1 partial machine)
    for i, cap in in_rates.items():
        usable = cap * LANE_HEADROOM
        for n in stages:
            amt = needs[n].get(i, 0)
            if amt <= 0:
                continue
            appetite = slots[n]["rate"] / SAFETY * amt
            fit = max(1, math.ceil(usable / appetite)) if appetite else counts[n]
            counts[n] = min(counts[n], fit)

    # ---- block decomposition ------------------------------------------------------------
    lane_usable = BELT_FULL * LANE_HEADROOM
    blocks = 1
    for ing, d in raw_unit.items():
        blocks = max(blocks, math.ceil(d * target / lane_usable))
    out_items = target
    collectors_total = max(1, math.ceil(out_items / LANE_CAP))
    if collectors_total > 2 * blocks:
        blocks = math.ceil(collectors_total / 2)
    if out_items > BELT_FULL + 1e-9:
        raise BankInapplicable("more than one full output belt not yet supported")

    per_block = {n: [counts[n] // blocks + (1 if b < counts[n] % blocks else 0)
                     for b in range(blocks)] for n in stages}

    # ---- emit ---------------------------------------------------------------------------
    lay = Layout()
    g2 = Graph()
    for n, nd in graph.nodes.items():
        if nd.kind is NodeKind.OUTPUT:
            g2.add_node(Node(n, nd.kind, rate=nd.rate))

    machines_wide = max(max(per_block[n]) for n in stages)
    # slot indices with i % 5 == 2 are POWER slots (substation columns): a substation
    # centred in the machine band covers +-9 tiles, i.e. ~5 slots of 3 tiles
    def is_power(i):
        return i % 5 == 2

    width_slots = 0
    cap_slots = 0
    while cap_slots < machines_wide:
        if not is_power(width_slots):
            cap_slots += 1
        width_slots += 1
    machine_slots = [i for i in range(width_slots) if not is_power(i)]
    power_slots = [i for i in range(width_slots) if is_power(i)]
    if not power_slots:
        power_slots = [width_slots]
        width_slots += 1

    def slot_x(i):                                         # left edge of slot i's body
        return 2 + i * 3
    W = slot_x(width_slots) + 3                            # east edge (power col incl.)

    copies: dict = {n: [] for n in stages}
    raw_rows_pending: dict = {}                            # item -> [(y, block, stage, role)]
    input_ct = 0
    y = 0
    collector_belts = []                                   # (y_row, rate)
    sub_positions = []
    stage_xs: dict = {}                                    # (stage, block) -> slot list

    # POSITIONS: the bus flows EAST, so a machine can only feed consumers east of its
    # drops. Consumers of the LAST stage spread uniformly; every producer stage is
    # then placed by PREFIX DEMAND: producer k sits at (or west of) the first
    # consumer whose cumulative draw exceeds k-1 producers' supply -- cumulative
    # supply stays ahead of cumulative demand at every x (no positional starvation).
    for b in range(blocks):
        last = stages[-1]
        stage_xs[(last, b)] = [machine_slots[j]
                               for j in _spread(per_block[last][b], len(machine_slots))]
        for i in range(len(stages) - 2, -1, -1):
            n, nxt = stages[i], stages[i + 1]
            nb = per_block[n][b]
            cons = stage_xs[(nxt, b)]
            if nb == 0 or not cons:
                stage_xs[(n, b)] = []
                continue
            supply = slots[n]["rate"] * out_amt[n]         # items/s per producer
            need = slots[nxt]["rate"] * needs[nxt].get(product[n], 0.0)
            xs_p, acc = [], 0.0
            ci = 0
            for k in range(nb):
                # place producer k at the slot of the first uncovered consumer
                covered = k * supply
                while ci < len(cons) - 1 and acc + need <= covered + 1e-9:
                    acc += need
                    ci += 1
                xs_p.append(cons[min(ci, len(cons) - 1)])
                if ci < len(cons):
                    acc += 0.0
            # slots may repeat when producers outnumber consumers locally: push
            # duplicates to the next free machine slot eastward
            seen, fixed = set(), []
            for x in xs_p:
                while x in seen and x < machine_slots[-1]:
                    nx = [s for s in machine_slots if s > x]
                    x = nx[0] if nx else x
                    if x in seen and not nx:
                        break
                seen.add(x)
                fixed.append(x)
            stage_xs[(n, b)] = sorted(fixed)

    for b in range(blocks):
        for i, n in enumerate(stages):
            sl = slots[n]
            nb = per_block[n][b]
            if nb == 0:
                continue
            far_src = ("bus", stages[i - 1]) if (i and sl["far"] == product[stages[i - 1]]) \
                else (("raw", sl["far"]) if sl["far"] else None)
            near_src = ("raw", sl["near"]) if sl["near"] else None
            # rows for this stage
            y_far = y if far_src else None
            y_near = (y + 1) if far_src else y
            if near_src is None:
                y_near = None
            y_arm_in = (y_near if y_near is not None else y_far) + 1
            y_mach = y_arm_in + 1
            y_arm_out = y_mach + 3
            y_bus = y_arm_out + 1

            # far belt: previous stage's bus already emitted at this row (see below);
            # raw far/near belts: emit belt + west input chest/loader
            for role, yy in (("far", y_far), ("near", y_near)):
                src = far_src if role == "far" else near_src
                if src is None or yy is None:
                    continue
                if src[0] == "raw":
                    raw_rows_pending.setdefault(src[1], []).append((yy, b, i, role))
                # bus rows are emitted by the PREVIOUS stage's output pass

            # machines + in-arms
            xs = stage_xs[(n, b)]
            for k in range(nb):
                x = slot_x(xs[k])                          # left edge of the 3x3 body
                mname = f"{n}_{len(copies[n]) + 1}"
                copies[n].append(mname)
                proto = _MACHINE_KINDS[graph.nodes[n].kind]
                g2.add_node(Node(mname, graph.nodes[n].kind,
                                 recipe=graph.nodes[n].recipe))
                lay.add(PlacedEntity(proto, x, y_mach, recipe=graph.nodes[n].recipe,
                                     meta={"node": mname}))
                # top arms: k_far long + k_near normal across the 3 face tiles
                face = [x, x + 1, x + 2]
                fi = 0
                for _ in range(sl["k_far"]):
                    lay.add(PlacedEntity(LONG_INSERTER, face[fi], y_arm_in, direction=N,
                                         meta={"role": "in"}))
                    fi += 1
                for _ in range(sl["k_near"]):
                    lay.add(PlacedEntity(INSERTER, face[fi], y_arm_in, direction=N,
                                         meta={"role": "in"}))
                    fi += 1
            # output arms + bus/collector rows
            last = (i == len(stages) - 1)
            if not last:
                for k in range(nb):
                    x = slot_x(xs[k])
                    face = [x, x + 1, x + 2]
                    for j in range(sl["k_out"]):
                        lay.add(PlacedEntity(INSERTER, face[j], y_arm_out, direction=N,
                                             meta={"role": "out"}))
                for x in range(-2, W):
                    lay.add(PlacedEntity(BELT, x, y_bus, direction=E,
                                         meta={"net": f"b:{n}"}))
                y = y_bus                                  # next stage's far belt row
            else:
                # collectors: one lane per 7.5/s of block output. A second collector
                # sits one row below, fed by LONG arms picking the machine's middle
                # row over the first collector; the exit weave merges the two lanes.
                block_out = nb * sl["rate"] * out_amt[n]
                # global budget: one output belt = two lanes total. Multi-block
                # builds get one collector each; only a single block may take two.
                n_coll = (1 if blocks > 1
                          else (2 if block_out > LANE_CAP + 1e-9 else 1))
                oi = 0
                for k in range(nb):
                    x = slot_x(xs[k])
                    face = [x, x + 1, x + 2]
                    for j in range(sl["k_out"]):
                        if n_coll == 2 and oi % 2 == 1:
                            lay.add(PlacedEntity(LONG_INSERTER, face[j], y_arm_out,
                                                 direction=N, meta={"role": "out"}))
                        else:
                            lay.add(PlacedEntity(INSERTER, face[j], y_arm_out,
                                                 direction=N, meta={"role": "out"}))
                        oi += 1
                for c in range(n_coll):
                    for x in range(-2, W):
                        lay.add(PlacedEntity(BELT, x, y_bus + c, direction=E,
                                             meta={"net": f"b:{n}"}))
                    collector_belts.append(y_bus + c)
                y = y_bus + n_coll - 1
            # substations in the reserved power slots of EVERY stage's machine band
            # (a substation reaches -8..+9 from its top-left; margins get their own)
            for p in power_slots + [width_slots]:
                sub_positions.append((slot_x(p), y_mach))
            sub_positions.append((-8, y_mach + 1))     # west margin: powers the
            #                                                loaders, within wire reach
            #                                                of the field subs; the
            #                                                descent column (-9) stays
            #                                                one tile clear
        y += 4                                            # gap between blocks

    # ---- raw boundaries: consolidate input belts --------------------------------------
    # A boundary belt is loader-fed and consumed at the row ends -- it is NOT
    # tap-drained, so it may run at 100%. When an item's total draw fits ONE belt but
    # feeds TWO block rows, a single chest feeds a SPLITTER whose outputs run to both
    # rows (each row then carries half its old load). Splitters preserve lane sides,
    # which is exactly right for splitting a full two-lane feed.
    split_ct = 0
    for item, rows in sorted(raw_rows_pending.items()):
        demand = raw_unit.get(item, 0.0) * target
        n_boundary = max(1, math.ceil(demand / BELT_FULL))
        near_only = all(role == "near" for _y, _b, _i, role in rows)
        if n_boundary == 1 and len(rows) == 2 and near_only:
            # near rows sit directly above their arm lane, so the two margin rows
            # below (arm lane + machine band, empty west of the field) host the
            # U-turn; a FAR-row consolidation would collide with the near belt
            split_ct += 1
            y0 = min(r[0] for r in rows)
            y1 = max(r[0] for r in rows)
            input_ct += 1
            iname = f"in_{item}_{input_ct}"
            g2.add_node(Node(iname, NodeKind.INPUT, item=item))
            tag_i = {"net": f"b:{iname}"}
            lay.add(PlacedEntity(CHEST_INPUT, X_IN, y0, item=item,
                                 meta={"node": iname}))
            lay.add(PlacedEntity(LOADER, X_IN + 1, y0, direction=E,
                                 loader_type="output", meta=tag_i))
            lay.add(PlacedEntity(SPLITTER, X_IN + 3, y0, direction=E, meta=tag_i))
            # branch 1: straight east into row y0
            for x in range(X_IN + 4, W):
                lay.add(PlacedEntity(BELT, x, y0, direction=E, meta=tag_i))
            # branch 2: U-turn west through the free margin rows (curves keep both
            # lanes), descend the column west of the chest line, re-enter row y1
            dcol = X_IN - 3
            lay.add(PlacedEntity(BELT, X_IN + 4, y0 + 1, direction=E, meta=tag_i))
            lay.add(PlacedEntity(BELT, X_IN + 5, y0 + 1, direction=S, meta=tag_i))
            lay.add(PlacedEntity(BELT, X_IN + 5, y0 + 2, direction=W_DIR, meta=tag_i))
            for x in range(X_IN + 4, dcol, -1):
                lay.add(PlacedEntity(BELT, x, y0 + 2, direction=W_DIR, meta=tag_i))
            lay.add(PlacedEntity(BELT, dcol, y0 + 2, direction=S, meta=tag_i))
            for yv in range(y0 + 3, y1):
                lay.add(PlacedEntity(BELT, dcol, yv, direction=S, meta=tag_i))
            lay.add(PlacedEntity(BELT, dcol, y1, direction=E, meta=tag_i))
            for x in range(dcol + 1, W):
                lay.add(PlacedEntity(BELT, x, y1, direction=E, meta=tag_i))
            for yy, b, i, role in rows:
                copies[("rawrow", b, i, role)] = iname
        else:
            for yy, b, i, role in rows:
                input_ct += 1
                iname = f"in_{item}_{input_ct}"
                g2.add_node(Node(iname, NodeKind.INPUT, item=item))
                lay.add(PlacedEntity(CHEST_INPUT, X_IN, yy, item=item,
                                     meta={"node": iname}))
                lay.add(PlacedEntity(LOADER, X_IN + 1, yy, direction=E,
                                     loader_type="output", meta={"net": f"b:{iname}"}))
                for x in range(X_IN + 3, W):
                    lay.add(PlacedEntity(BELT, x, yy, direction=E,
                                         meta={"net": f"b:{iname}"}))
                copies[("rawrow", b, i, role)] = iname

    # ---- merge collectors -> single output belt -> chest --------------------------------
    last_stage = stages[-1]
    tag = {"net": f"b:{last_stage}"}
    ys = sorted(collector_belts)
    if len(ys) > 2:
        raise BankInapplicable("more than two collector lanes (one output belt "
                               "carries two sides)")
    out_y = ys[0]
    end_x = W + 6
    for x in range(W, end_x):
        lay.add(PlacedEntity(BELT, x, out_y, direction=E, meta=tag))
    if len(ys) == 2:
        # LANE WEAVE: inserter drops ride the collector's far (south) lane, and a
        # side-load fills the lane on the ENTRY side -- so a plain merge or a
        # SPLITTER (which preserves lane sides) still caps the output at one lane.
        # Collector 2 instead tunnels UNDER the output row and side-loads from the
        # NORTH, landing its items on the empty north lane: a full two-lane belt.
        yy = ys[1]
        col = W + 1
        for x in range(W, col):
            lay.add(PlacedEntity(BELT, x, yy, direction=E, meta=tag))
        if yy > out_y + 1:                        # adjacent collectors: the UG
            lay.add(PlacedEntity(BELT, col, yy, direction=N, meta=tag))
            for yv in range(out_y + 2, yy):       # entrance IS the climb tile
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

    # ---- power + boundary ---------------------------------------------------------------
    seen_sub = set()
    for sx_, sy_ in sub_positions:
        key = (sx_, sy_)
        if key in seen_sub:
            continue
        seen_sub.add(key)
        lay.add(PlacedEntity(SUBSTATION, sx_, sy_, meta={}))
    if sub_positions:
        lay.add(PlacedEntity(EEI, X_IN - 1, min(s[1] for s in sub_positions) - 7,
                             meta={}))                     # same line as the inputs,
        #                                                    above the build

    # ---- expanded spec edges (what the layout physically realizes) ----------------------
    raw_rows = {k: v for k, v in copies.items() if isinstance(k, tuple)}
    for b in range(blocks):
        for i, n in enumerate(stages):
            sl = slots[n]
            block_copies = _block_slice(copies[n], per_block[n], b)
            for role in ("far", "near"):
                iname = raw_rows.get(("rawrow", b, i, role))
                if iname:
                    for c in block_copies:
                        g2.add_edge(iname, c)
            if i and sl["far"] == product[stages[i - 1]]:
                prev = stages[i - 1]
                pxs = stage_xs[(prev, b)]
                cxs = stage_xs[(n, b)]
                for pj, p in enumerate(_block_slice(copies[prev], per_block[prev], b)):
                    for cj, c in enumerate(block_copies):
                        # the bus flows EAST: p reaches c iff p's westmost drop tile
                        # is at or west of c's eastmost pick tile
                        if slot_x(pxs[pj]) <= slot_x(cxs[cj]) + sl["k_far"] - 1:
                            g2.add_edge(p, c)
    for c in copies[stages[-1]]:
        g2.add_edge(c, out_node)

    plan = {
        "mode": "bank",
        "target_per_s": {out_node: round(target, 4)},
        "machines": {n: {"copies": counts[n],
                         "per_copy_crafts_per_s": round(slots[n]["rate"], 4),
                         "arms": {k: slots[n][k] for k in ("k_far", "k_near", "k_out")}}
                     for n in stages},
        "blocks": blocks,
        "collectors": len(collector_belts),
        "expected_actual_per_s": {out_node: round(_expected(
            stages, counts, slots, out_amt, unit, raw_unit, in_rates,
            len(collector_belts)), 4)},
    }
    return g2, plan, lay


def _expected(stages, counts, slots, out_amt, unit, raw_unit, in_rates, n_coll):
    """Physical equilibrium estimate: min over stage capacities, declared raw
    supplies and collector lanes, in final-product units."""
    lim = n_coll * LANE_CAP
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
    """Evenly pace nb machines across width_slots (keeps bus flow local)."""
    if nb >= width_slots:
        return list(range(nb))
    return [round(k * (width_slots - 1) / max(nb - 1, 1)) for k in range(nb)] \
        if nb > 1 else [width_slots // 2]


def _block_slice(all_copies, per_block, b):
    start = sum(per_block[:b])
    return all_copies[start:start + per_block[b]]
