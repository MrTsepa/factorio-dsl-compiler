"""v3 layout generator: v2's placement + a GLOBAL negotiated-congestion router.

v2 (fgr/layout.py) routes lanes one at a time with pins fixed up-front (vx columns,
channel rows); its tracked failure bucket is *congested risers* -- a global contention
problem attacked with local rules. v3 keeps v2's placement passes (LAYER/ORDER/PLACE)
and replaces the whole EMIT stage with a PathFinder-style router (FPGA detailed
routing; see docs/INSPIRATION.md):

  * NETS, not lanes. A producer's belt fan-out is ONE multi-terminal net (a directed
    tree: trunk + tap-inserter branches). A fluid is ONE net per same-fluid group.
  * FLEXIBLE PINS. Sources = any free face of the producer (the search places the
    output inserter); sinks = any free face of the consumer, a tap on the net's own
    committed tree, a merge into another net's branch that flows to the same consumer
    (collector topology emerges instead of being a special case), or a direct
    inserter bridge when bodies are adjacent.
  * NEGOTIATION. All nets route with SOFT congestion costs (foreign claims passable
    at a price that grows each round + a history cost on chronically contended
    resources); rounds rip up and reroute only the conflicted nets. Bounded rounds,
    deterministic order, best-snapshot fallback -- v3 keeps v2's "cannot hang" and
    "cannot give up" properties.
  * EMISSION IS PURE. Routing produces per-net plans (tiles, dirs, inserters); a
    final pass materialises entities from plans. No occ/entity divergence by
    construction.

Legality mirrors fgr/verify.py exactly: inserter `direction` points at its PICKUP;
belts weld via accepting side-feeds (not head-on); underground belts pair with the
nearest same-axis entity within UG_MAX_GAP (axis-line resources keep tunnels apart);
pipes weld by 4-adjacency (a pipe-to-ground only on its mouth side) and pair within
PIPE_UG_GAP; a pipe attaches to a machine only ON a fluid-box external tile.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .ir import (EAST, WEST, NORTH, SOUTH, DIR_DELTA, OPPOSITE, Graph,
                 delta_to_dir)
from .layout import (BELT, INSERTER, PIPE, PIPE_TO_GROUND, TANK, UNDERGROUND,
                     CHEMICAL, FLUID_SOURCE, FLUID_VGAP, PIPE_UG_GAP, SIZE,
                     UG_MAX_GAP, Layout, PlacedEntity,
                     _RECIPE_KINDS, _assign_rows, _fluid_connections, _layers,
                     _node_proto)

CARDINALS = (NORTH, EAST, SOUTH, WEST)

# --- routing costs (integers; relative scale is what matters) -----------------
SURF = 10        # one surface belt/pipe tile
TURN = 4         # a corner (straight spines are the stated cleanliness goal)
BUR_BELT = 8     # one buried belt tile (dives are compact but use scarce lines)
BUR_PIPE = 6     # one buried pipe tile (tunnels preferred for long fluid runs)
JUMP_END = 7     # per dive (entrance+exit overhead)
ROOT = 14        # first output inserter of a net
NEW_ROOT = 44    # an EXTRA output inserter (second face of the same producer)
TAP = 28         # branch off the own tree via a tap inserter
FACE = 10        # land on a consumer face (input inserter)
ATTACH = 8       # consumer face already touching the committed tree (no new belt)
MERGE = 18       # merge into a foreign branch flowing to the same consumer
UG_LEAF = 4      # terminal pick tile is an underground end (prefer plain belts)
P_TURN = 2       # pipe corner

# --- negotiation schedule ------------------------------------------------------
PRES0 = 60       # round-0 price of one foreign-claimed resource
GROWTH = 1.6     # price growth per round
PRES_CAP = 6000
HIST_INC = 30    # history added to every overused resource each round
MAX_ROUNDS = 20


def _axis(d):
    return 0 if d in (EAST, WEST) else 1


# ---------------------------------------------------------------------------
# Net plans (a committed route) and the shared router state.
# ---------------------------------------------------------------------------
@dataclass
class _Plan:
    net: str
    kind: str                                  # "belt" | "pipe"
    ops: dict = field(default_factory=dict)    # tile -> ("belt",dir)|("ug_in",dir)|("ug_out",dir)
    #                                            | ("pipe",)|("p2g",mouth_dir)
    ins: list = field(default_factory=list)    # (tile, dir, meta) inserters (belt nets)
    buried: list = field(default_factory=list)  # (axis, tile) buried interior tiles
    sclaims: set = field(default_factory=set)   # surface tiles claimed (ops + inserters)
    lclaims: set = field(default_factory=set)   # ("LB"/"LP", axis, x, y) tunnel-line claims
    parent: dict = field(default_factory=dict)  # tile -> parent tile (belt tree)
    reach: dict = field(default_factory=dict)   # tile -> frozenset(consumers downstream)
    merges: list = field(default_factory=list)  # (host_net, host_tile, my_tile, consumer)
    welds: set = field(default_factory=set)     # (other_net, tile) soft welds accepted
    unrouted: list = field(default_factory=list)
    box_lands: list = field(default_factory=list)  # (machine, tile) fluid attachments

    def resources(self):
        for t in self.sclaims:
            yield ("S", t[0], t[1])
        yield from self.lclaims


class _State:
    """Shared committed state: resource occupancy, history, and fast entity maps."""

    def __init__(self):
        self.plans: dict[str, _Plan] = {}
        self.res: dict[tuple, set] = {}          # resource -> committed net ids
        self.hist: dict[tuple, int] = {}
        self.carrier: dict[tuple, tuple] = {}    # tile -> (net, proto, dir, ug_type)
        self.pushers: dict[tuple, list] = {}     # tile -> [(net, flow_dir)] belt/ug_out pushes

    def commit(self, plan: _Plan):
        self.plans[plan.net] = plan
        for r in plan.resources():
            self.res.setdefault(r, set()).add(plan.net)
        for t, op in plan.ops.items():
            if op[0] == "belt":
                self.carrier[t] = (plan.net, BELT, op[1], None)
                self._push(t, op[1], plan.net)
            elif op[0] == "ug_in":
                self.carrier[t] = (plan.net, UNDERGROUND, op[1], "input")
            elif op[0] == "ug_out":
                self.carrier[t] = (plan.net, UNDERGROUND, op[1], "output")
                self._push(t, op[1], plan.net)
            elif op[0] == "pipe":
                self.carrier[t] = (plan.net, PIPE, None, None)
            else:                                # p2g
                self.carrier[t] = (plan.net, PIPE_TO_GROUND, op[1], None)
        for t, d, _m in plan.ins:
            self.carrier[t] = (plan.net, INSERTER, d, None)

    def _push(self, t, d, net):
        dx, dy = DIR_DELTA[d]
        self.pushers.setdefault((t[0] + dx, t[1] + dy), []).append((net, d))

    def rip(self, net: str):
        plan = self.plans.pop(net)
        for r in plan.resources():
            s = self.res.get(r)
            if s is not None:
                s.discard(net)
                if not s:
                    del self.res[r]
        for t in list(plan.ops):
            if self.carrier.get(t, (None,))[0] == net:
                del self.carrier[t]
        for t, _d, _m in plan.ins:
            if self.carrier.get(t, (None,))[0] == net:
                del self.carrier[t]
        for t in list(self.pushers):
            kept = [p for p in self.pushers[t] if p[0] != net]
            if kept:
                self.pushers[t] = kept
            else:
                del self.pushers[t]
        return plan

    # --- soft congestion pricing (PathFinder: present + history) -------------
    def price(self, r, net, pres):
        holders = self.res.get(r)
        n = len(holders - {net}) if holders else 0
        return self.hist.get(r, 0) + (n * pres if n else 0)


# ---------------------------------------------------------------------------
# Netlist extraction.
# ---------------------------------------------------------------------------
@dataclass
class _BeltNet:
    net: str
    producer: str
    consumers: list                              # consumer names, routing order


@dataclass
class _FluidNet:
    net: str
    ports: list                                  # (machine, "output"|"input")
    fluid: str


def _fluid_tag(graph, n):
    nd = graph.nodes[n]
    return nd.item or nd.recipe or n


def _build_nets(graph: Graph, bodies) -> tuple[list[_BeltNet], list[_FluidNet]]:
    item_edges = [e for e in graph.edges if not e.fluid]
    belt_nets = []
    by_src: dict[str, list] = {}
    for e in item_edges:
        by_src.setdefault(e.src, []).append(e.dst)
    for p in graph.nodes:                        # graph order -> deterministic
        if p not in by_src:
            continue
        px, py = bodies[p].center

        def dist(c):
            cx, cy = bodies[c].center
            return abs(cx - px) + abs(cy - py)
        consumers = sorted(dict.fromkeys(by_src[p]), key=lambda c: (dist(c), c))
        belt_nets.append(_BeltNet(f"b:{p}", p, consumers))

    # Fluid nets: same-fluid connected components of the DECLARED edges -- but merged
    # only when the component is a full biclique (every producer x consumer pair is
    # declared); a merged net physically connects all pairs, so anything less would
    # manufacture a spurious lane. Non-biclique components fall back to per-producer.
    fluid_edges = [e for e in graph.edges if e.fluid]
    fluid_nets = []
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for e in fluid_edges:
        t = _fluid_tag(graph, e.src)
        parent[find((t, "p", e.src))] = find((t, "c", e.dst))
    groups: dict = {}
    for e in fluid_edges:
        t = _fluid_tag(graph, e.src)
        groups.setdefault(find((t, "p", e.src)), []).append(e)
    for i, (_root, edges) in enumerate(sorted(groups.items(), key=lambda kv: str(kv[0]))):
        prods = list(dict.fromkeys(e.src for e in edges))
        cons = list(dict.fromkeys(e.dst for e in edges))
        declared = {(e.src, e.dst) for e in edges}
        tag = _fluid_tag(graph, edges[0].src)
        if all((p, c) in declared for p in prods for c in cons):
            ports = [(p, "output") for p in prods] + [(c, "input") for c in cons]
            fluid_nets.append(_FluidNet(f"f:{tag}:{i}", ports, tag))
        else:                                    # split: one net per producer
            for p in prods:
                ports = ([(p, "output")]
                         + [(c, "input") for c in cons if (p, c) in declared])
                fluid_nets.append(_FluidNet(f"f:{tag}:{i}:{p}", ports, tag))
    return belt_nets, fluid_nets


# ---------------------------------------------------------------------------
# Geometry helpers.
# ---------------------------------------------------------------------------
def _faces(body):
    """Perimeter faces of a body: (face_tile, outward_dir). An inserter at the face
    reaches the body on one side and the tile `face+outward` on the other."""
    bx, by = body.x, body.y
    bw, bh = body.size
    out = []
    for r in range(bh):
        out.append(((bx - 1, by + r), WEST))
        out.append(((bx + bw, by + r), EAST))
    for c in range(bw):
        out.append(((bx + c, by - 1), NORTH))
        out.append(((bx + c, by + bh), SOUTH))
    return out


class _Ctx:
    """Per-compile static context: bodies, walls, bounds, live fluid boxes."""

    def __init__(self, graph, bodies, fluid_sinks):
        self.graph = graph
        self.bodies = bodies
        self.body_tiles: dict[str, set] = {n: set(b.tiles()) for n, b in bodies.items()}
        self.all_body_tiles: set = set().union(*self.body_tiles.values()) if bodies else set()
        self.tile_owner = {t: n for n, ts in self.body_tiles.items() for t in ts}

        # live fluid-box external tiles, per verifier semantics: an assembler has boxes
        # only when it's a fluid endpoint; chemical/tank/source always do.
        fluid_eps = ({e.src for e in graph.edges if e.fluid}
                     | {e.dst for e in graph.edges if e.fluid})
        self.boxes: dict[str, list] = {}         # machine -> [(tile, flow, mdir)]
        for n, b in bodies.items():
            if b.proto not in (CHEMICAL, TANK, FLUID_SOURCE) and n not in fluid_eps:
                continue
            conns = _fluid_connections(b.proto, b.x, b.y, b.direction, with_dir=True)
            if conns:
                self.boxes[n] = conns
        self.box_tiles = {t: (n, flow) for n, cs in self.boxes.items()
                          for t, flow, _m in cs}

        xs = [t[0] for t in self.all_body_tiles] or [0]
        ys = [t[1] for t in self.all_body_tiles] or [0]
        self.bounds = (min(xs) - 12, max(xs) + 13, min(ys) - 11, max(ys) + 12)

        # static walls: bodies + every live box tile. A net whitelists its own boxes.
        self.walls = set(self.all_body_tiles) | set(self.box_tiles)

    def in_bounds(self, t):
        lo_x, hi_x, lo_y, hi_y = self.bounds
        return lo_x <= t[0] <= hi_x and lo_y <= t[1] <= hi_y

    def blocked(self, t, allow=()):
        return (t in self.walls and t not in allow) or not self.in_bounds(t)


# ---------------------------------------------------------------------------
# Weld legality against the committed state (mirrors verify.py's accepts()).
# ---------------------------------------------------------------------------
def _accepting(state: _State, net, t, flow_dir):
    """Does a FOREIGN transport carrier at t accept an item flowing `flow_dir`?"""
    c = state.carrier.get(t)
    if c is None or c[0] == net:
        return False
    _n, proto, d, ug = c
    if proto == BELT:
        return d != OPPOSITE[flow_dir]
    if proto == UNDERGROUND:
        return ug == "input" and d == flow_dir
    return False


def _foreign_pushers(state: _State, net, t):
    return [(n, d) for (n, d) in state.pushers.get(t, ()) if n != net]


def _pipe_weld(state: _State, net, t):
    """Foreign pipe nets a PLAIN pipe at t would weld to (4-adjacency; a p2g only
    connects on its mouth side)."""
    out = []
    for d in CARDINALS:
        nb = (t[0] + DIR_DELTA[d][0], t[1] + DIR_DELTA[d][1])
        c = state.carrier.get(nb)
        if c is None or c[0] == net:
            continue
        _n, proto, pd, _ug = c
        if proto == PIPE or (proto == PIPE_TO_GROUND and pd == OPPOSITE[d]):
            out.append((c[0], nb))
    return out


# ---------------------------------------------------------------------------
# The router: one Dijkstra per terminal, shared by belt and pipe nets.
# ---------------------------------------------------------------------------
def _dijkstra(ctx: _Ctx, state: _State, net_id, kind, sources, targets, tree_tiles,
              own_l, pres, allow_boxes=frozenset(), forbid_land=frozenset()):
    """Multi-source multi-target search over (tile, in_dir, via) states, prev keyed by
    tile (first settle wins). Moves: step (turn-limited after a jump) and jump (dive
    under anything; interiors claim axis-line resources). Foreign claims and welds are
    SOFT (priced); static walls and own-line pairing breaks are HARD.

    sources: [(cost, tile, in_dir, via, action)]  targets: {tile: [option, ...]}
    Returns (cost, end_tile, end_state, prev, option) for the cheapest accepted
    arrival, else None."""
    max_gap = UG_MAX_GAP if kind == "belt" else PIPE_UG_GAP
    bur = BUR_BELT if kind == "belt" else BUR_PIPE
    turn_cost = TURN if kind == "belt" else P_TURN
    lfam = "LB" if kind == "belt" else "LP"
    Sp = lambda t: state.price(("S", t[0], t[1]), net_id, pres)
    Lp = lambda a, t: state.price((lfam, a, t[0], t[1]), net_id, pres)

    if not targets:
        return None
    # A*: admissible remaining-cost bound = manhattan distance to the nearest target
    # x the cheapest possible per-tile rate (a long dive; soft prices only add).
    t_list = list(targets)
    h_rate = 8 if kind == "belt" else BUR_PIPE

    def h(t):
        return min(abs(t[0] - a[0]) + abs(t[1] - a[1]) for a in t_list) * h_rate

    # States are keyed (tile, lock): lock is the forced direction after a tunnel
    # landing (an exit can't turn), None for a free tile. Keying by tile alone would
    # let a cheap direction-locked landing settle a tile and starve every turn there.
    heap = []
    seq = 0
    for cost, tile, din, via, action in sources:
        heapq.heappush(heap, (cost + h(tile), cost, seq, tile, din, via, action))
        seq += 1
    prev: dict = {}
    best = None
    while heap:
        _f, cost, _s, tile, din, via, action = heapq.heappop(heap)
        key = (tile, din if via == "jump" else None)
        if key in prev:
            continue
        prev[key] = action                       # (parent_key, move, dir) or source action
        opts = targets.get(tile)
        if opts is not None:
            acc = _try_accept(ctx, state, net_id, kind, tile, din, via, opts, pres,
                              prev, key)
            if acc is not None:
                extra, option = acc
                best = (cost + extra, tile, key, prev, option)
                break
        for e in CARDINALS:
            if din is not None and e == OPPOSITE[din]:
                continue
            if via == "jump" and e != din:
                continue                          # a tunnel exit cannot turn
            dx, dy = DIR_DELTA[e]
            nb = (tile[0] + dx, tile[1] + dy)
            # --- step ---
            if ((nb, None) not in prev and nb not in tree_tiles
                    and not ctx.blocked(nb, allow_boxes)):
                c = cost + SURF + Sp(nb) + (turn_cost if (din is not None and e != din) else 0)
                if kind == "belt":
                    # placing belt(tile, e): every foreign pusher into `tile` must be
                    # rejected head-on, else their items weld into this lane.
                    for _n, m in _foreign_pushers(state, net_id, tile):
                        if e != OPPOSITE[m]:
                            c += pres            # soft weld (negotiation may clear it)
                else:
                    for _n, _t in _pipe_weld(state, net_id, nb):
                        c += pres
                heapq.heappush(heap, (c + h(nb), c, seq, nb, e, "step", (key, "step", e)))
                seq += 1
            # --- jump (dive) ---
            if via == "jump" or (din is not None and e != din):
                continue                          # enter a tunnel straight only
            if kind == "pipe" and via in ("mid",):
                continue                          # can't convert a committed pipe in place
            ax = _axis(e)
            jc = cost + JUMP_END + Lp(ax, tile)
            if (lfam, ax, tile[0], tile[1]) in own_l:
                continue                          # own same-axis line here -> mispairing
            for m in range(1, max_gap):
                it = (tile[0] + dx * m, tile[1] + dy * m)
                if (lfam, ax, it[0], it[1]) in own_l:
                    break
                jc += bur + Lp(ax, it)
                land = (tile[0] + dx * (m + 1), tile[1] + dy * (m + 1))
                if ((land, e) in prev or land in tree_tiles or land in forbid_land
                        or ctx.blocked(land, allow_boxes)
                        or (lfam, ax, land[0], land[1]) in own_l):
                    continue
                lc = jc + SURF + Sp(land) + Lp(ax, land)
                heapq.heappush(heap, (lc + h(land), lc, seq, land, e, "jump",
                                      (key, "jump", e)))
                seq += 1
    return best


def _try_accept(ctx, state, net_id, kind, tile, din, via, opts, pres, prev, key):
    """Terminal acceptance at `tile`. Returns (extra_cost, resolved_option) or None."""
    path_tiles = None                             # lazy: only walked when an f needs it
    for opt in opts:
        if kind == "pipe":
            if via == "jump":
                continue                          # must arrive as a plain surface pipe
            return (0, opt)
        if opt[0] == "drop":                      # ("drop", face_tile, ins_dir)
            _k, f, ins_dir = opt
            if path_tiles is None:
                path_tiles = set()
                k = key
                while True:
                    a = prev[k]
                    path_tiles.add(k[0])
                    if a[1:2] in (("step",), ("jump",)):
                        k = a[0]
                    else:
                        break
            if f in path_tiles:
                continue                          # the route itself runs over this face
            extra = FACE + state.price(("S", f[0], f[1]), net_id, pres)
            leaf = _leaf_dir(ctx, state, net_id, tile, din, via, pres)
            if leaf is None:
                continue
            e, weld_pen = leaf
            if via == "jump":
                extra += UG_LEAF
            return (extra + weld_pen, ("drop", f, ins_dir, e))
        if opt[0] == "merge":                     # ("merge", host_net, host_tile, m)
            _k, host, H, m = opt
            if via == "jump" and m != din:
                continue                          # an exit only pushes straight ahead
            if din is not None and m == OPPOSITE[din]:
                continue
            pen = 0
            if via != "jump":
                for _n, pm in _foreign_pushers(state, net_id, tile):
                    if m != OPPOSITE[pm]:
                        pen += pres               # soft weld; negotiation clears it
            return (MERGE + pen, ("merge", host, H, m))
    return None


def _leaf_dir(ctx, state, net_id, tile, din, via, pres):
    """Orientation for a terminal pick tile: must accept the incoming flow; SHOULD not
    push into a foreign accepting carrier nor take a foreign side-feed, but those are
    soft (priced welds the negotiation clears). Returns (dir, weld_penalty) or None."""
    push = _foreign_pushers(state, net_id, tile)
    if via == "jump":                             # ug exit: direction fixed = din
        cand = [din]
    else:
        cand = ([din] if din is not None else []) + [d for d in CARDINALS
                                                     if d != din and d != (OPPOSITE[din] if din is not None else None)]
    best = None
    for e in cand:
        pen = sum(pres for _n, m in push if e != OPPOSITE[m])
        front = (tile[0] + DIR_DELTA[e][0], tile[1] + DIR_DELTA[e][1])
        if _accepting(state, net_id, front, e):
            pen += pres
        if pen == 0:
            return (e, 0)
        if best is None or pen < best[1]:
            best = (e, pen)
    return best


# ---------------------------------------------------------------------------
# Belt net routing: sequential Steiner over consumers with flexible pins.
# ---------------------------------------------------------------------------
def _belt_targets(ctx: _Ctx, state: _State, net_id, consumer, used_faces, own_block):
    """Target map for one consumer: face drops + safe merges into foreign branches."""
    targets: dict = {}
    body = ctx.bodies[consumer]
    for f, outward in _faces(body):
        if ctx.blocked(f) or f in used_faces or f in own_block:
            continue
        pick = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
        if ctx.blocked(pick):
            continue
        targets.setdefault(pick, []).append(("drop", f, outward))
    for host_id in state.plans:                   # committed order = deterministic
        plan = state.plans[host_id]
        if plan.kind != "belt":
            continue
        for H in sorted(plan.reach):
            if plan.reach[H] != frozenset((consumer,)):
                continue
            if plan.ops.get(H, ("",))[0] != "belt":
                continue
            b = plan.ops[H][1]
            for m in CARDINALS:
                if b == OPPOSITE[m]:
                    continue                      # head-on: host rejects the feed
                X = (H[0] - DIR_DELTA[m][0], H[1] - DIR_DELTA[m][1])
                if not ctx.blocked(X):
                    targets.setdefault(X, []).append(("merge", host_id, H, m))
    return targets


def _route_belt_net(ctx: _Ctx, state: _State, net: _BeltNet, pres):
    plan = _Plan(net.net, "belt")
    producer = ctx.bodies[net.producer]
    tree: dict = {}                               # tile -> (op, dir); the growing tree
    parent: dict = {}
    attach: list = []                             # (consumer, attach_tile|None)
    p_tiles = ctx.body_tiles[net.producer]
    used_faces: set = set()                       # faces this net consumed (in or out)

    def own_l(ax, t):
        return ("LB", ax, t[0], t[1]) in plan.lclaims

    for consumer in net.consumers:
        c_tiles = ctx.body_tiles[consumer]
        own_block = plan.sclaims | set(tree)
        targets = _belt_targets(ctx, state, net.net, consumer, used_faces, own_block)

        # (0) zero-move options: attach an input inserter where the committed tree
        # already runs past a face (v2's "direct trunk tap"), or bridge two adjacent
        # bodies with a single inserter.
        best_static = None
        for T, opts in targets.items():
            if T not in tree or tree[T][0] != "belt":
                continue
            for opt in opts:
                if opt[0] != "drop":
                    continue
                _k, f, ins_dir = opt
                if f in tree or f in p_tiles:
                    continue
                c = ATTACH + state.price(("S", f[0], f[1]), net.net, pres)
                if best_static is None or c < best_static[0]:
                    best_static = (c, ("attach", T, f, ins_dir))
        for f, outward in _faces(producer):       # out-inserter bridge: drop IS the body
            if ctx.blocked(f) or f in used_faces or f in own_block:
                continue
            drop = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
            if drop in c_tiles:
                c = ROOT + state.price(("S", f[0], f[1]), net.net, pres)
                if best_static is None or c < best_static[0]:
                    best_static = (c, ("bridge_root", f, outward))
        for t in sorted(tree):                    # tap bridge: tap drop IS the body
            if tree[t][0] != "belt":
                continue
            for e in CARDINALS:
                ins = (t[0] + DIR_DELTA[e][0], t[1] + DIR_DELTA[e][1])
                v = (t[0] + 2 * DIR_DELTA[e][0], t[1] + 2 * DIR_DELTA[e][1])
                if v in c_tiles and not ctx.blocked(ins) and ins not in own_block:
                    c = TAP + state.price(("S", ins[0], ins[1]), net.net, pres)
                    if best_static is None or c < best_static[0]:
                        best_static = (c, ("bridge_tap", t, ins, e))

        # (1) search sources: extend leaves, tap the tree, or open a new root face.
        sources = []
        merge_tiles = {mt for _h, _ht, mt, _c in plan.merges}
        for L in sorted(tree):
            if tree[L][0] != "belt" or L in merge_tiles:
                continue
            # taps (branch anywhere on the tree)
            for e in CARDINALS:
                ins = (L[0] + DIR_DELTA[e][0], L[1] + DIR_DELTA[e][1])
                v = (L[0] + 2 * DIR_DELTA[e][0], L[1] + 2 * DIR_DELTA[e][1])
                if (ctx.blocked(ins) or ctx.blocked(v) or ins in own_block
                        or v in own_block or v in p_tiles):
                    continue
                c = (TAP + state.price(("S", ins[0], ins[1]), net.net, pres)
                     + state.price(("S", v[0], v[1]), net.net, pres) + SURF)
                sources.append((c, v, None, "drop", ("tap", L, ins, e)))
        leaves = set(tree) - set(parent.values())
        for L in sorted(leaves - merge_tiles):
            op, d = tree[L]
            if op == "belt":
                flow_in = _flow_into(parent, tree, L)
                sources.append((0, L, flow_in if flow_in is not None else d, "ext", ("ext", L)))
        root_cost = ROOT if not tree else NEW_ROOT
        for f, outward in _faces(producer):
            if ctx.blocked(f) or f in used_faces or f in own_block:
                continue
            drop = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
            if ctx.blocked(drop) or drop in own_block:
                continue
            c = (root_cost + state.price(("S", f[0], f[1]), net.net, pres)
                 + state.price(("S", drop[0], drop[1]), net.net, pres) + SURF)
            sources.append((c, drop, None, "drop", ("root", f, outward)))

        # A path may step through a tile the chosen source/terminal wants for its
        # inserter (the search has no per-source provenance). Validate post-hoc and
        # retry with the offender hard-blocked -- rare, bounded, deterministic.
        retry_block: set = set()
        res = None
        for _attempt in range(4):
            res = _dijkstra(ctx, state, net.net, "belt", sources, targets,
                            own_block | retry_block, plan.lclaims, pres)
            if res is None:
                break
            clash = _path_ins_clash(res)
            if clash is None:
                break
            retry_block.add(clash)
            res = None
        if best_static is not None and (res is None or best_static[0] <= res[0]):
            _apply_static(ctx, state, plan, tree, parent, attach, used_faces,
                          consumer, best_static[1], net)
            continue
        if res is None:
            plan.unrouted.append(consumer)
            continue
        _apply_path(ctx, state, plan, tree, parent, attach, used_faces, consumer,
                    res, net)

    _finish_belt_plan(plan, tree, parent, attach)
    return plan


def _walk_back(res):
    """Reconstruct the winning route: ([(tile, move, dir), ...] in order, source action)."""
    _cost, _end, end_key, prev, _option = res
    steps = []
    key = end_key
    while True:
        a = prev[key]
        if a[1:2] in (("step",), ("jump",)):
            steps.append((key[0], a[1], a[2]))
            key = a[0]
        else:
            return list(reversed(steps)), a, key[0]


def _path_ins_clash(res):
    """A tile the winning route uses twice (state-keyed search may fold a path over
    itself) or wants for its own inserter while also crossing it; None if consistent."""
    steps, src_action, start = _walk_back(res)
    tiles = [start] + [t for t, _m, _d in steps]
    seen = set()
    for t in tiles:
        if t in seen:
            return t
        seen.add(t)
    option = res[4]
    ins_tiles = []
    if src_action[0] == "tap":
        ins_tiles.append(src_action[2])
    elif src_action[0] == "root":
        ins_tiles.append(src_action[1])
    if option[0] == "drop":
        ins_tiles.append(option[1])
    for t in ins_tiles:
        if t in seen:
            return t
    return None


def _flow_into(parent, tree, L):
    p = parent.get(L)
    if p is None:
        return None
    return delta_to_dir(_sgn(L[0] - p[0]), _sgn(L[1] - p[1])) if _adj(p, L) else tree[L][1]


def _sgn(v):
    return (v > 0) - (v < 0)


def _adj(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def _apply_static(ctx, state, plan, tree, parent, attach, used_faces, consumer,
                  action, net):
    if action[0] == "attach":
        _a, T, f, ins_dir = action
        plan.ins.append((f, ins_dir, {"role": "in", "edge": (net.producer, consumer)}))
        plan.sclaims.add(f)
        used_faces.add(f)
        attach.append((consumer, T))
    elif action[0] == "bridge_root":
        _a, f, outward = action
        plan.ins.append((f, OPPOSITE[outward], {"role": "bridge",
                                                "edge": (net.producer, consumer)}))
        plan.sclaims.add(f)
        used_faces.add(f)
        attach.append((consumer, None))
    else:                                         # bridge_tap
        _a, t, ins, e = action
        plan.ins.append((ins, OPPOSITE[e], {"role": "tap",
                                            "edge": (net.producer, consumer)}))
        plan.sclaims.add(ins)
        attach.append((consumer, t))


def _apply_path(ctx, state, plan, tree, parent, attach, used_faces, consumer, res, net):
    _cost, end, _key, prev, option = res
    path, src_action, start = _walk_back(res)
    if src_action[0] == "root":
        _r, f, outward = src_action
        plan.ins.append((f, OPPOSITE[outward], {"role": "out", "src": net.producer}))
        plan.sclaims.add(f)
        used_faces.add(f)
        tree[start] = ("belt", path[0][2] if path else EAST)
        plan.sclaims.add(start)
        parent[start] = None
    elif src_action[0] == "tap":
        _t, L, ins, e = src_action
        plan.ins.append((ins, OPPOSITE[e], {"role": "tap",
                                            "edge": (net.producer, consumer)}))
        plan.sclaims.add(ins)
        tree[start] = ("belt", path[0][2] if path else e)
        plan.sclaims.add(start)
        parent[start] = L
    else:                                         # ext: start already in tree
        pass
    # path tiles
    pt = start
    for (t, move, d) in path:
        if move == "step":
            if pt in tree and tree[pt][0] == "belt":
                tree[pt] = ("belt", d)            # (re)orient toward the next tile
            tree[t] = ("belt", d)
            plan.sclaims.add(t)
        else:                                     # jump: pt becomes entrance, t exit
            tree[pt] = ("ug_in", d)
            tree[t] = ("ug_out", d)
            plan.sclaims.add(t)
            ax = _axis(d)
            plan.lclaims.add(("LB", ax, pt[0], pt[1]))
            plan.lclaims.add(("LB", ax, t[0], t[1]))
            step = DIR_DELTA[d]
            b = (pt[0] + step[0], pt[1] + step[1])
            while b != t:
                plan.buried.append((ax, b))
                plan.lclaims.add(("LB", ax, b[0], b[1]))
                b = (b[0] + step[0], b[1] + step[1])
        parent[t] = pt
        pt = t
    # terminal option
    if option[0] == "drop":
        _d, f, ins_dir, leaf = option
        if end in tree and tree[end][0] == "belt":
            tree[end] = ("belt", leaf)
        plan.ins.append((f, ins_dir, {"role": "in", "edge": (net.producer, consumer)}))
        plan.sclaims.add(f)
        used_faces.add(f)
        attach.append((consumer, end))
    else:                                         # merge
        _m, host, H, mdir = option
        if end in tree and tree[end][0] == "belt":
            tree[end] = ("belt", mdir)
        plan.merges.append((host, H, end, consumer))
        attach.append((consumer, end))
    # soft welds accepted along the way are found in a post-pass (cheap and exact)


def _finish_belt_plan(plan: _Plan, tree, parent, attach):
    plan.ops = dict(tree)
    plan.parent = dict(parent)
    reach: dict = {}
    for consumer, t in attach:
        while t is not None:
            reach.setdefault(t, set()).add(consumer)
            t = parent.get(t)
    plan.reach = {t: frozenset(s) for t, s in reach.items()}


# ---------------------------------------------------------------------------
# Fluid net routing: sequential Steiner over machine ports.
# ---------------------------------------------------------------------------
def _route_fluid_net(ctx: _Ctx, state: _State, net: _FluidNet, pres):
    plan = _Plan(net.net, "pipe")
    allow: set = set()
    port_boxes: dict = {}
    for machine, want in net.ports:
        boxes = [t for t, flow, _m in ctx.boxes.get(machine, ())
                 if flow in (want, "both")]
        port_boxes[(machine, want)] = boxes
        allow.update(boxes)
    tree: dict = {}
    lands: list = []

    remaining = list(dict.fromkeys(net.ports))
    first = remaining.pop(0)
    fb = port_boxes[first]
    if not fb:
        plan.unrouted.append(first[0])
        remaining = []
    else:
        t0 = fb[0]
        tree[t0] = ("pipe",)
        plan.sclaims.add(t0)
        lands.append((first[0], t0))

    while remaining:
        # nearest unrouted port to the committed tree (compact nets reach farther)
        def best_dist(port):
            bs = port_boxes[port]
            if not bs or not tree:
                return 1 << 30
            return min(abs(b[0] - t[0]) + abs(b[1] - t[1]) for b in bs for t in tree)
        remaining.sort(key=lambda p: (best_dist(p), p[0]))
        port = remaining.pop(0)
        machine, _want = port
        boxes = [b for b in port_boxes[port] if b not in tree]
        if not boxes:
            if any(b in tree for b in port_boxes[port]):
                lands.append((machine, next(b for b in port_boxes[port] if b in tree)))
            else:
                plan.unrouted.append(machine)
            continue
        targets = {b: [("box", machine)] for b in boxes}
        sources = []
        for t in sorted(tree):
            if tree[t][0] != "pipe":
                continue
            sources.append((0, t, None, "mid", ("src", t)))
        # own committed tiles are off-limits for stepping: re-crossing a junction pipe
        # is harmless, but a jump would convert it to a p2g and sever its other links.
        retry_block: set = set(tree)
        res = None
        for _attempt in range(4):
            res = _dijkstra(ctx, state, net.net, "pipe", sources, targets, retry_block,
                            plan.lclaims, pres, allow_boxes=frozenset(allow),
                            forbid_land=frozenset(allow))
            if res is None:
                break
            clash = _path_ins_clash(res)
            if clash is None:
                break
            retry_block.add(clash)
            res = None
        if res is None:
            plan.unrouted.append(machine)
            continue
        path, _src, start = _walk_back(res)
        end = res[1]
        pt = start
        for (t, move, d) in path:
            if move == "step":
                tree[t] = ("pipe",)
                plan.sclaims.add(t)
            else:                                 # jump: pt entrance, t exit
                tree[pt] = ("p2g", OPPOSITE[d])   # mouth faces back at the parent pipe
                tree[t] = ("p2g", d)              # placeholder; fixed to face next below
                plan.sclaims.add(t)
                ax = _axis(d)
                plan.lclaims.add(("LP", ax, pt[0], pt[1]))
                plan.lclaims.add(("LP", ax, t[0], t[1]))
                step = DIR_DELTA[d]
                b = (pt[0] + step[0], pt[1] + step[1])
                while b != t:
                    plan.buried.append((ax, b))
                    plan.lclaims.add(("LP", ax, b[0], b[1]))
                    b = (b[0] + step[0], b[1] + step[1])
            pt = t
        lands.append((machine, end))

    plan.ops = dict(tree)
    plan.box_lands = lands
    return plan


# ---------------------------------------------------------------------------
# Conflict audit: shared resources, welds, orphaned merges (the negotiation signal).
# ---------------------------------------------------------------------------
def _audit(ctx: _Ctx, state: _State):
    overused = {r for r, s in state.res.items() if len(s) > 1}
    weld_nets: set = set()
    weld_tiles: set = set()
    # sanctioned feeds: a client's merge push into its host tile is the merge itself
    sanctioned_push = {(nid, host, H) for nid in state.plans
                       for host, H, _mt, _c in state.plans[nid].merges}
    for nid in state.plans:
        plan = state.plans[nid]
        if plan.kind == "belt":
            sanctioned = {(h, H) for h, H, _mt, _c in plan.merges}
            for t, op in plan.ops.items():
                if op[0] not in ("belt", "ug_out"):
                    continue
                d = op[1]
                front = (t[0] + DIR_DELTA[d][0], t[1] + DIR_DELTA[d][1])
                if front in plan.ops:
                    continue
                c = state.carrier.get(front)
                if c is not None and c[0] != nid and _accepting(state, nid, front, d):
                    if (c[0], front) in sanctioned:
                        continue
                    weld_nets.update((nid, c[0]))
                    weld_tiles.add(front)
                if op[0] == "belt":
                    for n2, m in _foreign_pushers(state, nid, t):
                        if d != OPPOSITE[m] and (n2, nid, t) not in sanctioned_push:
                            weld_nets.update((nid, n2))
                            weld_tiles.add(t)
        else:
            for t, op in plan.ops.items():
                if op[0] == "pipe":
                    for n2, nb in _pipe_weld(state, nid, t):
                        weld_nets.update((nid, n2))
                        weld_tiles.add(nb)
                else:                             # p2g: mouth side only
                    mouth = op[1]
                    nb = (t[0] + DIR_DELTA[mouth][0], t[1] + DIR_DELTA[mouth][1])
                    if nb in plan.ops:
                        continue
                    c = state.carrier.get(nb)
                    if c is not None and c[0] != nid and c[1] in (PIPE, PIPE_TO_GROUND):
                        if c[1] == PIPE or state.carrier[nb][2] == OPPOSITE[mouth]:
                            weld_nets.update((nid, c[0]))
                            weld_tiles.add(nb)
    orphans: set = set()
    for nid in state.plans:
        for host, H, _mt, consumer in state.plans[nid].merges:
            hp = state.plans.get(host)
            if (hp is None or hp.ops.get(H, ("",))[0] != "belt"
                    or hp.reach.get(H) != frozenset((consumer,))):
                orphans.add(nid)
    unrouted = {nid for nid in state.plans if state.plans[nid].unrouted}
    return overused, weld_nets, weld_tiles, orphans, unrouted


# ---------------------------------------------------------------------------
# Negotiation: route all, then rip-up & reroute conflicted nets, bounded rounds.
# ---------------------------------------------------------------------------
def _negotiate(ctx: _Ctx, belt_nets, fluid_nets):
    state = _State()
    order = [(n.net, n) for n in belt_nets] + [(n.net, n) for n in fluid_nets]
    routers = {n.net: (_route_belt_net if isinstance(n, _BeltNet) else _route_fluid_net)
               for _id, n in order}
    pres = PRES0

    for nid, n in order:
        state.commit(routers[nid](ctx, state, n, pres))

    best_plans, best_score = None, (1 << 30)
    for _round in range(MAX_ROUNDS):
        overused, weld_nets, weld_tiles, orphans, unrouted = _audit(ctx, state)
        score = 3 * len(overused) + 3 * len(weld_tiles) + len(orphans) + 5 * len(unrouted)
        if score < best_score:
            best_plans, best_score = dict(state.plans), score
        if score == 0:
            break
        for r in overused:
            state.hist[r] = state.hist.get(r, 0) + HIST_INC
        for t in weld_tiles:
            r = ("S", t[0], t[1])
            state.hist[r] = state.hist.get(r, 0) + HIST_INC
        dirty = set()
        for r in overused:
            dirty |= state.res.get(r, set())
        dirty |= weld_nets | orphans | unrouted
        # rip a host's clients too, TRANSITIVELY: merge webs (rings included) must
        # rip and re-form together, or each partial reroute strands one stale host
        # and the group thrashes forever.
        work = list(dirty)
        while work:
            nid = work.pop()
            for cid in state.plans:
                if cid not in dirty and any(h == nid for h, _H, _mt, _c
                                            in state.plans[cid].merges):
                    dirty.add(cid)
                    work.append(cid)
        pres = min(int(pres * GROWTH), PRES_CAP)
        for nid, _n in order:
            if nid in dirty and nid in state.plans:
                state.rip(nid)
        for nid, n in order:
            if nid in dirty:
                state.commit(routers[nid](ctx, state, n, pres))
    else:
        overused, weld_nets, weld_tiles, orphans, unrouted = _audit(ctx, state)
        score = 3 * len(overused) + 3 * len(weld_tiles) + len(orphans) + 5 * len(unrouted)
        if score < best_score:
            best_plans, best_score = dict(state.plans), score
    return best_plans if best_plans is not None else dict(state.plans)


# ---------------------------------------------------------------------------
# Placement (v2's passes) + emission.
# ---------------------------------------------------------------------------
def _place(graph: Graph, vgap):
    col = _layers(graph)
    cr, _order = _assign_rows(graph, col, vgap)
    fluid_sinks = {e.dst for e in graph.edges if e.fluid}
    cmax = max(col.values()) if col else 0
    item_edges = [e for e in graph.edges if not e.fluid]
    n_out = {g: 0 for g in range(cmax + 1)}
    n_in = {g: 0 for g in range(cmax + 1)}
    for e in item_edges:
        n_out[col[e.src]] = n_out.get(col[e.src], 0) + 1
        n_in[col[e.dst]] = n_in.get(col[e.dst], 0) + 1
    gutter = {g: max(7, n_out.get(g, 0) + n_in.get(g + 1, 0) + 5) for g in range(cmax + 1)}
    Xcol = {0: 0}
    for c in range(1, cmax + 1):
        Xcol[c] = Xcol[c - 1] + 3 + gutter[c - 1]

    layout = Layout()
    bodies = {}
    for n in graph.nodes:
        proto = _node_proto(graph, n, fluid_sinks)
        bw, bh = SIZE[proto]
        node = graph.nodes[n]
        bodies[n] = layout.add(PlacedEntity(
            proto, Xcol[col[n]], cr[n] - 1 if bh == 3 else cr[n],
            recipe=node.recipe if node.kind in _RECIPE_KINDS else None,
            item=node.item, meta={"node": n}))
    return layout, bodies, fluid_sinks


def _emit(layout: Layout, plans: dict):
    for nid in plans:
        plan = plans[nid]
        for t in sorted(plan.ops):
            op = plan.ops[t]
            meta = {"net": nid}
            if op[0] == "belt":
                layout.add(PlacedEntity(BELT, t[0], t[1], direction=op[1], meta=meta))
            elif op[0] == "ug_in":
                layout.add(PlacedEntity(UNDERGROUND, t[0], t[1], direction=op[1],
                                        ug_type="input", meta=meta))
            elif op[0] == "ug_out":
                layout.add(PlacedEntity(UNDERGROUND, t[0], t[1], direction=op[1],
                                        ug_type="output", meta=meta))
            elif op[0] == "pipe":
                layout.add(PlacedEntity(PIPE, t[0], t[1], meta=meta))
            else:
                layout.add(PlacedEntity(PIPE_TO_GROUND, t[0], t[1], direction=op[1],
                                        meta=meta))
        for t, d, meta in plan.ins:
            meta = dict(meta)
            meta["net"] = nid
            layout.add(PlacedEntity(INSERTER, t[0], t[1], direction=d, meta=meta))
    return layout


def _compile_at(graph: Graph, vgap: int) -> Layout:
    layout, bodies, fluid_sinks = _place(graph, vgap)
    ctx = _Ctx(graph, bodies, fluid_sinks)
    belt_nets, fluid_nets = _build_nets(graph, bodies)
    plans = _negotiate(ctx, belt_nets, fluid_nets)
    return _emit(layout, plans)


def compile_graph(graph: Graph, vgap: int | None = None) -> Layout:
    """Generate a candidate :class:`Layout` (the v3 global-router generator).

    One adaptive axis remains from v2 -- vertical clearance between stacked fluid
    machines -- because no router can conjure room that placement didn't leave.
    Everything the v2 co-router searched over (net order, boxed-out lanes) is handled
    by negotiation instead."""
    if vgap is not None:
        return _compile_at(graph, vgap)
    from .verify import verify                    # lazy: verify imports layout modules
    # vgap only spaces stacked FLUID machines; without fluid edges every gap compiles
    # to the same geometry, so escalation would be 3x wasted work.
    gaps = (FLUID_VGAP, 6, 10) if any(e.fluid for e in graph.edges) else (FLUID_VGAP,)
    best, best_score = None, (1 << 30)
    for g in gaps:
        lay = _compile_at(graph, g)
        rep = verify(graph, lay)
        if rep.ok:
            return lay
        score = sum(not c.ok for c in rep.checks)
        if score < best_score:
            best, best_score = lay, score
    return best
