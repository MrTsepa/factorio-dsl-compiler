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
belts weld via accepting side-feeds (not head-on) -- and so do UNDERGROUND ENDS, whose
belt half takes side input in-game (an entrance also takes its back feed; an exit's
back is the tunnel); underground belts pair with the nearest same-axis entity within
UG_MAX_GAP (axis-line resources keep tunnels apart); pipes weld by 4-adjacency (a
pipe-to-ground only on its mouth side) and pair within PIPE_UG_GAP; a pipe attaches
to a machine only ON a fluid-box external tile.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .ir import (EAST, WEST, NORTH, SOUTH, DIR_DELTA, OPPOSITE, Graph,
                 delta_to_dir)
from .layout import (BELT, CHEST_INPUT, CHEST_OUTPUT, INSERTER, LOADER, PIPE,
                     PIPE_TO_GROUND, TANK, UNDERGROUND,
                     CHEMICAL, FLUID_SOURCE, FLUID_VGAP, PIPE_UG_GAP, SIZE,
                     UG_MAX_GAP, Layout, PlacedEntity,
                     _RECIPE_KINDS, _assign_rows, _fluid_connections, _layers,
                     _node_proto)
from .power import emit_power, patch_power, plan_power, power_tiles

CARDINALS = (NORTH, EAST, SOUTH, WEST)

# --- routing costs (integers; relative scale is what matters) -----------------
SURF = 10        # one surface belt/pipe tile
TURN = 4         # a corner (straight spines are the stated cleanliness goal)
BUR_BELT = 10    # one buried belt tile: same as surface, so with JUMP_END a dive only
#                  ever wins when something actually blocks the surface (no dashed-line
#                  tunnels across open ground); pipes stay cheaper buried on purpose
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
    tag: str = ""                              # product carried (belt nets)
    ops: dict = field(default_factory=dict)    # tile -> ("belt",dir)|("ug_in",dir)|("ug_out",dir)
    #                                            | ("pipe",)|("p2g",mouth_dir)
    ins: list = field(default_factory=list)    # (tile, dir, meta) inserters (belt nets)
    loaders: list = field(default_factory=list)  # (face_tile, outward, ltype, meta): 1x2
    #                                              vanilla loaders on chest faces; tiles =
    #                                              face and face+outward, belt half outer
    buried: list = field(default_factory=list)  # (axis, tile) buried interior tiles
    sclaims: set = field(default_factory=set)   # surface tiles claimed (ops + inserters)
    lclaims: set = field(default_factory=set)   # ("LB"/"LP", axis, x, y) tunnel-line claims
    parent: dict = field(default_factory=dict)  # tile -> parent tile (belt tree)
    reach: dict = field(default_factory=dict)   # tile -> frozenset(consumers downstream)
    merges: list = field(default_factory=list)  # (host_net, host_tile, my_tile, consumer)
    lock: set = field(default_factory=set)      # tiles whose belt ORIENTATION is load-
    #                                             bearing (a loader's straight feed) --
    #                                             never re-oriented / extended through
    manc: list = field(default_factory=list)    # (merge_record, frozenset(ancestor tiles))
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
        for f, o, ltype, _m in plan.loaders:
            g = (f[0] + DIR_DELTA[o][0], f[1] + DIR_DELTA[o][1])
            d = o if ltype == "output" else OPPOSITE[o]
            # container half f, belt half g (both claimed); only the belt half acts
            self.carrier[f] = (plan.net, LOADER, d, (ltype, "container"))
            self.carrier[g] = (plan.net, LOADER, d, (ltype, "belt"))
            if ltype == "output":              # belt half pushes onward
                self._push(g, d, plan.net)

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
        for f, o, _lt, _m in plan.loaders:
            for t in (f, (f[0] + DIR_DELTA[o][0], f[1] + DIR_DELTA[o][1])):
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
    tag: str = ""                                # the product carried (item/recipe)


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
    # one net per (producer, output port): each port is a separate physical tree
    # with its own root inserter -- the rate solver's "k output arms" mechanism
    by_port: dict[tuple, dict] = {}
    for e in item_edges:
        slot = by_port.setdefault((e.src, getattr(e, "port", 0)), {})
        slot[e.dst] = max(slot.get(e.dst, 1), getattr(e, "arms", 1))
    for p in graph.nodes:                        # graph order -> deterministic
        ports = sorted(k[1] for k in by_port if k[0] == p)
        if not ports:
            continue
        px, py = bodies[p].center

        def dist(c):
            cx, cy = bodies[c].center
            return abs(cx - px) + abs(cy - py)
        for port in ports:
            slot = by_port[(p, port)]
            consumers = []
            for c in sorted(slot, key=lambda c: (dist(c), c)):
                consumers.extend([c] * slot[c])  # k arms = k routed legs = k accepts
            name = f"b:{p}" if port == 0 else f"b:{p}.{port}"
            belt_nets.append(_BeltNet(name, p, consumers, _fluid_tag(graph, p)))

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

        # PAIR SINKS: a consumer whose DISTINCT-product item fan-in exceeds its inserter
        # faces can't give every product its own belt. A belt holds two products, one
        # per side (lane), so such sinks pair products up: both side-load one shared
        # pick tile from OPPOSITE sides -- each collapses onto its own lane, no mixing.
        # Used only when forced: half a belt starves throughput, so whole-belt lanes
        # stay the norm and pairing is the overflow mechanism.
        self.pair_partner: dict = {}               # (consumer, tag) -> partner tag
        by_dst: dict = {}
        for e in graph.edges:
            if not e.fluid:
                by_dst.setdefault(e.dst, set()).add(_fluid_tag(graph, e.src))
        self.n_products = {c: len(tags) for c, tags in by_dst.items()}
        for c, tags in by_dst.items():
            faces = 2 * sum(SIZE[_node_proto(graph, c, fluid_sinks)])
            if len(tags) <= faces:
                continue
            ordered = sorted(tags)
            for i in range(0, len(ordered) - 1, 2):
                self.pair_partner[(c, ordered[i])] = ordered[i + 1]
                self.pair_partner[(c, ordered[i + 1])] = ordered[i]

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
    """Does a FOREIGN transport carrier at t accept an item flowing `flow_dir`?
    Mirrors verify.py: underground ends are SIDE-LOADABLE like belts (their belt
    half takes side input), so a belt dead-ending against one welds in-game."""
    c = state.carrier.get(t)
    if c is None or c[0] == net:
        return False
    _n, proto, d, ug = c
    if proto == BELT:
        return d != OPPOSITE[flow_dir]
    if proto == UNDERGROUND:
        if ug == "input":
            return d != OPPOSITE[flow_dir]
        return flow_dir not in (d, OPPOSITE[d])
    if proto == LOADER:
        # only an INPUT loader's belt half accepts, and only straight (no side-loads)
        return ug == ("input", "belt") and flow_dir == d
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
    # A*: admissible remaining-cost bound = manhattan distance to the TARGET BBOX
    # x the cheapest per-tile rate. Bbox distance <= true nearest-target distance,
    # so still admissible -- and O(1) instead of a min over every target (that min
    # was the single hottest line on the scale_* fields).
    h_rate = SURF if kind == "belt" else BUR_PIPE   # jumps cost >= SURF/tile for belts
    t_lo_x = min(t[0] for t in targets)
    t_hi_x = max(t[0] for t in targets)
    t_lo_y = min(t[1] for t in targets)
    t_hi_y = max(t[1] for t in targets)

    def h(t):
        dx = (t_lo_x - t[0]) if t[0] < t_lo_x else (t[0] - t_hi_x if t[0] > t_hi_x else 0)
        dy = (t_lo_y - t[1]) if t[1] < t_lo_y else (t[1] - t_hi_y if t[1] > t_hi_y else 0)
        return (dx + dy) * h_rate

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
            if kind == "belt":
                # the entrance's belt half takes side-loads: a foreign feed into this
                # tile would weld into the tunnel (soft; negotiation clears it)
                for _n, m in _foreign_pushers(state, net_id, tile):
                    if m != OPPOSITE[e]:
                        jc += pres
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
                if kind == "belt":
                    for _n, m in _foreign_pushers(state, net_id, land):
                        if m not in (e, OPPOSITE[e]):
                            lc += pres            # side-load into my exit -> weld
                heapq.heappush(heap, (lc + h(land), lc, seq, land, e, "jump",
                                      (key, "jump", e)))
                seq += 1
    return best


def _try_accept(ctx, state, net_id, kind, tile, din, via, opts, pres, prev, key):
    """Terminal acceptance at `tile`. Returns (extra_cost, resolved_option) or None."""
    path_tiles = None                             # lazy: only walked when an f needs it

    def walk_tiles():
        nonlocal path_tiles
        if path_tiles is None:
            path_tiles = set()
            k = key
            while True:
                a = prev[k]
                path_tiles.add(k[0])
                if a[1:2] in (("step",), ("jump",)):
                    k = a[0]
                else:
                    if a[0] == "tap":             # source inserter tile is taken too
                        path_tiles.add(a[2])
                    elif a[0] == "root":
                        path_tiles.add(a[1])
                    elif a[0] == "lroot":         # source loader tiles
                        path_tiles.add(a[1])
                        path_tiles.add((a[1][0] + DIR_DELTA[a[2]][0],
                                        a[1][1] + DIR_DELTA[a[2]][1]))
                    break
        return path_tiles

    for opt in opts:
        if kind == "pipe":
            if via == "jump":
                continue                          # must arrive as a plain surface pipe
            return (0, opt)
        if opt[0] == "ldrop":                     # ("ldrop", face_tile, outward, pair)
            _k, f, outward, pair = opt            # 1x2 input loader on (f, f+outward);
            inward = OPPOSITE[outward]            # its feed belt sits at f+2*outward,
            #                                       oriented inward (straight into it)
            if din == outward:
                continue                          # arriving head-on against the feed
            if pair:
                # pair sink: the feed belt must be SIDE-loaded so this product rides
                # one lane only -- the partner side-loads the other side, and the
                # loader lifts both lanes into the chest. A straight arrival would
                # fill both lanes and mix.
                if din is None or din in (inward, outward):
                    continue
            elif via == "jump" and din != inward:
                continue                          # an exit pushes straight only
            g = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
            if f in walk_tiles() or g in path_tiles:
                continue                          # route runs over the loader's tiles
            pen = sum(pres for _n2, m in _foreign_pushers(state, net_id, tile)
                      if inward != OPPOSITE[m])
            extra = (FACE + state.price(("S", f[0], f[1]), net_id, pres)
                     + state.price(("S", g[0], g[1]), net_id, pres))
            if via == "jump":
                extra += UG_LEAF
            return (extra + pen, ("ldrop", f, outward, inward))
        if opt[0] == "drop":                      # ("drop", face_tile, ins_dir, side?)
            _k, f, ins_dir = opt[:3]
            prefer_side = len(opt) > 3 and opt[3]
            if f in walk_tiles():
                continue                          # the route itself uses this face tile
            extra = FACE + state.price(("S", f[0], f[1]), net_id, pres)
            if len(opt) > 4:                      # chest fallback: loaders preferred
                extra += 120
            if prefer_side:
                # pair-sink pick tile: the belt must run ALONG the face normal with a
                # LATERAL side-feed, so the opposite lateral side stays open for the
                # partner product's side-load (host approaching along the normal would
                # leave only the face tile free -- where this inserter stands).
                if via == "jump" or (din is not None
                                     and din in (ins_dir, OPPOSITE[ins_dir])):
                    continue
                leaf = None
                push = _foreign_pushers(state, net_id, tile)
                for e in (ins_dir, OPPOSITE[ins_dir]):
                    pen = sum(pres for _n2, m in push if e != OPPOSITE[m])
                    front = (tile[0] + DIR_DELTA[e][0], tile[1] + DIR_DELTA[e][1])
                    if _accepting(state, net_id, front, e):
                        pen += pres
                    if leaf is None or pen < leaf[1]:
                        leaf = (e, pen)
                    if pen == 0:
                        break
            else:
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


def _leaf_dir(ctx, state, net_id, tile, din, via, pres, prefer_side=False):
    """Orientation for a terminal pick tile: must accept the incoming flow; SHOULD not
    push into a foreign accepting carrier nor take a foreign side-feed, but those are
    soft (priced welds the negotiation clears). Returns (dir, weld_penalty) or None.
    ``prefer_side`` orients the belt PERPENDICULAR to the arrival when possible, making
    the pick tile side-fed -- the shape a pair partner can later side-load from the
    opposite side (see _Ctx.pair_partner)."""
    push = _foreign_pushers(state, net_id, tile)
    if via == "jump":                             # ug exit: direction fixed = din
        cand = [din]
    elif din is None:
        cand = list(CARDINALS)
    else:
        perp = [d for d in CARDINALS if d not in (din, OPPOSITE[din])]
        cand = perp + [din] if prefer_side else [din] + perp
    best = None
    my_tag = state.plans[net_id].tag if net_id in state.plans else None
    for e in cand:
        pen = sum(pres for _n, m in push if e != OPPOSITE[m])
        front = (tile[0] + DIR_DELTA[e][0], tile[1] + DIR_DELTA[e][1])
        if _accepting(state, net_id, front, e):
            c = state.carrier.get(front)
            other = state.plans[c[0]].tag if c and c[0] in state.plans else None
            if other is not None and other != my_tag:
                continue                # a terminal spilling into a DIFFERENT product's
                #                         carrier can never be priced away (the mix is
                #                         verifier-fatal); forbid the landing outright
            pen += pres
        if pen == 0:
            return (e, 0)
        if best is None or pen < best[1]:
            best = (e, pen)
    return best


# ---------------------------------------------------------------------------
# Belt net routing: sequential Steiner over consumers with flexible pins.
# ---------------------------------------------------------------------------
def _belt_targets(ctx: _Ctx, state: _State, net_id, tag, consumer, used_faces, own_block):
    """Target map for one consumer: face drops + safe merges into foreign branches
    CARRYING THE SAME PRODUCT -- different products never share a belt (the verifier
    would allow one per side, but half a belt starves throughput; keep lanes whole)."""
    targets: dict = {}
    body = ctx.bodies[consumer]
    partner = ctx.pair_partner.get((consumer, tag))
    chest_sink = body.proto == CHEST_OUTPUT
    for f, outward in _faces(body):
        if ctx.blocked(f) or f in used_faces or f in own_block:
            continue
        pick = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
        if ctx.blocked(pick):
            continue
        if chest_sink and partner is None and ctx.n_products.get(consumer, 1) <= 2:
            # full-belt sink: 1x2 loader on (f, pick), fed straight at pick2. PAIR
            # sinks and 3+-product chests keep inserter terminals: several 3-tile-deep
            # loader assemblies crowding one 1x1 chest were shown not to converge, and
            # a chest splitting its intake across many products is not a full-belt
            # endpoint anyway -- loaders are for the dedicated-output case.
            pick2 = (f[0] + 2 * DIR_DELTA[outward][0], f[1] + 2 * DIR_DELTA[outward][1])
            if (not ctx.blocked(pick2) and pick not in used_faces
                    and pick not in own_block):
                targets.setdefault(pick2, []).append(("ldrop", f, outward, False))
            targets.setdefault(pick, []).append(
                ("drop", f, outward, False, "fb"))
        else:
            targets.setdefault(pick, []).append(("drop", f, outward, partner is not None))
    want = frozenset((consumer,))
    if consumer in ctx.graph.no_merge:
        return targets                    # solver-sized consumer: each supplier lane
        #                                   is a deliberate ARM; merging collapses them
    # tiles already hosting a cross-product pair are FULL (a third feed would land on
    # one of the two occupied lanes)
    locked = {H for pm in state.plans.values() for h, H, _mt, _c in pm.merges
              if pm.kind == "belt" and h in state.plans and pm.tag != state.plans[h].tag}
    for host_id in state.plans:                   # committed order = deterministic
        plan = state.plans[host_id]
        if plan.kind != "belt":
            continue
        if plan.tag == tag:
            for H in sorted(plan.ops):
                if plan.ops[H][0] != "belt" or H in locked:
                    continue
                fr, grounded = _flow_reach(state, plan, H)
                if not grounded or fr != want:
                    continue
                b = plan.ops[H][1]
                for m in CARDINALS:
                    if b == OPPOSITE[m]:
                        continue                  # head-on: host rejects the feed
                    X = (H[0] - DIR_DELTA[m][0], H[1] - DIR_DELTA[m][1])
                    if not ctx.blocked(X):
                        targets.setdefault(X, []).append(("merge", host_id, H, m))
        elif plan.tag == partner:
            # PAIR: side-load the partner's pick tile from the side OPPOSITE its own
            # feed -- each product collapses onto its own lane (no mixing), and the
            # partner's inserter (or full-belt loader) lifts both into the sink.
            pick_tiles = [(t[0] + DIR_DELTA[d][0], t[1] + DIR_DELTA[d][1])
                          for t, d, meta in plan.ins
                          if meta.get("role") == "in"
                          and meta.get("edge", (None, None))[1] == consumer]
            pick_tiles += [(f2[0] + 2 * DIR_DELTA[o2][0], f2[1] + 2 * DIR_DELTA[o2][1])
                           for f2, o2, lt2, meta in plan.loaders
                           if lt2 == "input"
                           and meta.get("edge", (None, None))[1] == consumer]
            for P in pick_tiles:
                if plan.ops.get(P, ("",))[0] != "belt" or P in locked:
                    continue
                e = plan.ops[P][1]
                feeds = [m0 for _n, m0 in state.pushers.get(P, ()) if m0 != OPPOSITE[e]]
                if len(feeds) != 1 or feeds[0] in (e, OPPOSITE[e]):
                    continue                      # need exactly one, side-fed
                m = OPPOSITE[feeds[0]]
                X = (P[0] - DIR_DELTA[m][0], P[1] - DIR_DELTA[m][1])
                if not ctx.blocked(X):
                    targets.setdefault(X, []).append(("merge", host_id, P, m))
    return targets


def _route_belt_net(ctx: _Ctx, state: _State, net: _BeltNet, pres):
    plan = _Plan(net.net, "belt", tag=net.tag)
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
        targets = _belt_targets(ctx, state, net.net, net.tag, consumer, used_faces, own_block)

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
                _k, f, ins_dir = opt[:3]
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
        for L in sorted(leaves - merge_tiles - plan.lock):
            op, d = tree[L]
            if op == "belt":
                flow_in = _flow_into(parent, tree, L)
                sources.append((0, L, flow_in if flow_in is not None else d, "ext", ("ext", L)))
        root_cost = ROOT if not tree else NEW_ROOT
        chest_root = ctx.bodies[net.producer].proto == CHEST_INPUT
        for f, outward in _faces(producer):
            if ctx.blocked(f) or f in used_faces or f in own_block:
                continue
            drop = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
            if ctx.blocked(drop) or drop in own_block:
                continue
            Sp_f = state.price(("S", f[0], f[1]), net.net, pres)
            Sp_d = state.price(("S", drop[0], drop[1]), net.net, pres)
            if chest_root:
                # full-belt root: a 1x2 loader on tiles (f, drop), pushing onto drop2
                drop2 = (f[0] + 2 * DIR_DELTA[outward][0], f[1] + 2 * DIR_DELTA[outward][1])
                if (not ctx.blocked(drop2) and drop2 not in own_block
                        and drop not in used_faces):
                    c = (root_cost + Sp_f + Sp_d
                         + state.price(("S", drop2[0], drop2[1]), net.net, pres) + SURF)
                    sources.append((c, drop2, None, "drop", ("lroot", f, outward)))
                # inserter root stays available but heavily priced: a cramped chest
                # must still route (half-belt beats no belt; the verifier takes both)
                c = (root_cost + 120 + Sp_f + Sp_d + SURF)
                sources.append((c, drop, None, "drop", ("root", f, outward)))
            else:
                c = root_cost + Sp_f + Sp_d + SURF
                sources.append((c, drop, None, "drop", ("root", f, outward)))

        # A path may step through a tile the chosen source/terminal wants for its
        # inserter (the search has no per-source provenance). Validate post-hoc and
        # retry with the offender hard-blocked -- rare, bounded, deterministic.
        retry_block: set = set()
        res = None
        for _attempt in range(8):
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
    elif src_action[0] == "lroot":
        f, o = src_action[1], src_action[2]
        ins_tiles += [f, (f[0] + DIR_DELTA[o][0], f[1] + DIR_DELTA[o][1])]
    if option[0] == "drop":
        ins_tiles.append(option[1])
    elif option[0] == "ldrop":
        f, o = option[1], option[2]
        ins_tiles += [f, (f[0] + DIR_DELTA[o][0], f[1] + DIR_DELTA[o][1])]
    for t in ins_tiles:
        if t in seen:
            return t
        seen.add(t)                               # also catches tap-ins == drop-face
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
    elif src_action[0] == "lroot":                # 1x2 output loader on (f, f+outward)
        _r, f, outward = src_action
        g = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
        plan.loaders.append((f, outward, "output", {"role": "out", "src": net.producer}))
        plan.sclaims.add(f)
        plan.sclaims.add(g)
        used_faces.add(f)
        used_faces.add(g)
        tree[start] = ("belt", path[0][2] if path else outward)
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
    if option[0] == "ldrop":                      # 1x2 input loader on (f, f+outward)
        _d, f, outward, inward = option
        g = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
        if end in tree and tree[end][0] == "belt":
            tree[end] = ("belt", inward)
        plan.loaders.append((f, outward, "input",
                             {"role": "in", "edge": (net.producer, consumer)}))
        plan.sclaims.add(f)
        plan.sclaims.add(g)
        used_faces.add(f)
        used_faces.add(g)
        plan.lock.add(end)                        # the straight feed must stay straight
        attach.append((consumer, end))
    elif option[0] == "drop":
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
        # NOT an `attach`: reach labels must stay grounded in REAL (inserter/bridge)
        # attachments. If merged branches also earned reach, two nets could merge into
        # each other -- reach-valid on both sides, physically a flow cycle that never
        # arrives (the audit would score 0 while the verifier reports missing lanes).
    # soft welds accepted along the way are found in a post-pass (cheap and exact)


def _finish_belt_plan(plan: _Plan, tree, parent, attach):
    plan.ops = dict(tree)
    plan.parent = dict(parent)
    reach: dict = {}
    for consumer, t in attach:                    # REAL (inserter/bridge) attaches only
        while t is not None:
            reach.setdefault(t, set()).add(consumer)
            t = parent.get(t)
    plan.reach = {t: frozenset(s) for t, s in reach.items()}
    # ancestors of each merge point: tiles whose flow continues INTO that merge
    plan.manc = []
    for rec in plan.merges:
        anc = set()
        t = rec[2]
        while t is not None:
            anc.add(t)
            t = parent.get(t)
        plan.manc.append((rec, frozenset(anc)))


def _flow_reach(state: _State, plan: _Plan, tile, seen=frozenset()):
    """Consumers TRULY downstream of `tile` in `plan`: real attaches plus, transitively,
    what its merges feed into. Returns (consumers, grounded); a merge chain that loops
    back onto itself or lands on a missing/rerouted host is NOT grounded. Both the
    naive alternatives fail: counting merges as attaches lets two nets merge into each
    other (a flow cycle the audit can't see); ignoring them under-reports downstream
    flow, so foreign merges upstream of a merge point leak items (spurious lanes)."""
    key = (plan.net, tile)
    if key in seen:
        return frozenset(), False
    seen = seen | {key}
    out = set(plan.reach.get(tile, ()))
    grounded = True
    for (host, H, _mt, _c), anc in plan.manc:
        if tile not in anc:
            continue
        hp = state.plans.get(host)
        if hp is None or hp.ops.get(H, ("",))[0] != "belt":
            grounded = False
            continue
        sub, ok = _flow_reach(state, hp, H, seen)
        out |= sub
        grounded = grounded and ok
    return frozenset(out), grounded


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
        for _attempt in range(8):
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
    weld_pairs: set = set()                        # (net_a, net_b) sorted per weld
    weld_tiles: set = set()

    def _weld(a, b, t):
        weld_nets.update((a, b))
        weld_pairs.add((min(a, b), max(a, b)))
        weld_tiles.add(t)
    # sanctioned feeds: a client's merge push into its host tile is the merge itself
    sanctioned_push = {(nid, host, H) for nid in state.plans
                       for host, H, _mt, _c in state.plans[nid].merges}
    for nid in state.plans:
        plan = state.plans[nid]
        if plan.kind == "belt":
            sanctioned = {(h, H) for h, H, _mt, _c in plan.merges}
            for t, op in plan.ops.items():
                if op[0] not in ("belt", "ug_in", "ug_out"):
                    continue
                d = op[1]
                if op[0] != "ug_in":               # entrances push underground, not ahead
                    front = (t[0] + DIR_DELTA[d][0], t[1] + DIR_DELTA[d][1])
                    c = state.carrier.get(front)
                    if (front not in plan.ops and c is not None and c[0] != nid
                            and _accepting(state, nid, front, d)
                            and (c[0], front) not in sanctioned):
                        _weld(nid, c[0], front)
                # incoming foreign feeds this tile would take: belts and BOTH
                # underground ends accept side-loads in-game (belt-half of the tile)
                for n2, m in _foreign_pushers(state, nid, t):
                    if op[0] == "ug_out":
                        takes = m not in (d, OPPOSITE[d])
                    else:                          # belt / ug_in: back + sides
                        takes = d != OPPOSITE[m]
                    if takes and (n2, nid, t) not in sanctioned_push:
                        _weld(nid, n2, t)
            for f, o, ltype, _m in plan.loaders:
                if ltype != "input":
                    continue                       # an output loader's rear is the chest
                g = (f[0] + DIR_DELTA[o][0], f[1] + DIR_DELTA[o][1])
                d = OPPOSITE[o]                    # its belt half takes a straight feed
                for n2, m in _foreign_pushers(state, nid, g):
                    if m == d and (n2, nid, g) not in sanctioned_push:
                        _weld(nid, n2, g)
        else:
            for t, op in plan.ops.items():
                if op[0] == "pipe":
                    for n2, nb in _pipe_weld(state, nid, t):
                        _weld(nid, n2, nb)
                else:                             # p2g: mouth side only
                    mouth = op[1]
                    nb = (t[0] + DIR_DELTA[mouth][0], t[1] + DIR_DELTA[mouth][1])
                    if nb in plan.ops:
                        continue
                    c = state.carrier.get(nb)
                    if c is not None and c[0] != nid and c[1] in (PIPE, PIPE_TO_GROUND):
                        if c[1] == PIPE or state.carrier[nb][2] == OPPOSITE[mouth]:
                            _weld(nid, c[0], nb)
    orphans: set = set()
    for nid in state.plans:
        for host, H, _mt, consumer in state.plans[nid].merges:
            hp = state.plans.get(host)
            if hp is None or hp.ops.get(H, ("",))[0] != "belt":
                orphans.add(nid)
                continue
            fr, grounded = _flow_reach(state, hp, H)
            if not grounded or fr != frozenset((consumer,)):
                orphans.add(nid)
                continue
            if state.plans[nid].tag != hp.tag:
                # cross-product PAIR: lane separation holds only while the shared pick
                # tile has exactly two feeds from opposite sides (one lane each)
                e = hp.ops[H][1]
                feeds = [m for _n, m in state.pushers.get(H, ()) if m != OPPOSITE[e]]
                if (len(feeds) != 2 or feeds[0] != OPPOSITE[feeds[1]]
                        or feeds[0] in (e, OPPOSITE[e])):
                    orphans.add(nid)
    unrouted = {nid for nid in state.plans if state.plans[nid].unrouted}
    return overused, weld_nets, weld_pairs, weld_tiles, orphans, unrouted


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
    stall = 0
    rounds = MAX_ROUNDS if not ctx.graph.no_merge else max(MAX_ROUNDS, 48)
    # solver-sized graphs run far more nets (ports x arms) than hand specs; a lone
    # weld surviving 20 rounds on a 90-net field is budget, not a livelock
    for _round in range(rounds):
        overused, weld_nets, weld_pairs, weld_tiles, orphans, unrouted = _audit(ctx, state)
        score = 3 * len(overused) + 3 * len(weld_tiles) + len(orphans) + 5 * len(unrouted)
        if score < best_score:
            best_plans, best_score, stall = dict(state.plans), score, 0
        else:
            stall += 1
        if score == 0:
            break
        if stall >= 10:
            break                    # negotiation has stopped improving for 10 straight
            #                          rounds; the outer vgap escalation is the better
            #                          lever at this point (slow convergers DO recover
            #                          after 6-8 flat rounds -- don't cut earlier)
        if pres >= PRES_CAP and overused and _round >= 12:
            break                    # hard tile conflicts survived maxed-out prices:
            #                          the field is simply too tight at this vgap --
            #                          grinding the remaining rounds burns seconds per
            #                          round on big fields (scale_1 spent 38s here)
        for r in overused:
            state.hist[r] = state.hist.get(r, 0) + HIST_INC
        for t in weld_tiles:
            r = ("S", t[0], t[1])
            state.hist[r] = state.hist.get(r, 0) + HIST_INC
        dirty = set()
        for r in overused:
            dirty |= state.res.get(r, set())
        # welds: rip only ONE side per round, alternating -- ripping both together
        # re-collides them blind (the partner's geometry vanishes just when the
        # first net re-routes), a deterministic livelock
        for a, b in weld_pairs:
            dirty.add(a if _round % 2 == 0 else b)
        dirty |= orphans | unrouted
        # an unrouted terminal alone leaves nothing else dirty -- the field never
        # changes and the net stays stuck. Charge history around the failed
        # consumer's faces and rip whoever holds that approach ring.
        for nid in unrouted:
            plan = state.plans[nid]
            for consumer in plan.unrouted:
                body = ctx.bodies.get(consumer)
                if body is None:
                    continue
                for f, outward in _faces(body):
                    pick = (f[0] + DIR_DELTA[outward][0], f[1] + DIR_DELTA[outward][1])
                    for t in (f, pick):
                        r = ("S", t[0], t[1])
                        state.hist[r] = state.hist.get(r, 0) + HIST_INC
                        dirty |= state.res.get(r, set())
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
        overused, weld_nets, weld_pairs, weld_tiles, orphans, unrouted = _audit(ctx, state)
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
    if graph.no_merge:
        # SIZED graphs (rate solver): a boundary input chest must enter its consumer
        # column at the END of the span, so the trunk runs past every machine with a
        # direct pickup. Barycenter placement centres it mid-span, which forces the
        # tree to branch and a single tap arm then throttles half the bank (measured:
        # 7 of 17 machines at zero).
        from .ir import NodeKind as _NK
        inputs = [n for n, nd in graph.nodes.items() if nd.kind is _NK.INPUT]
        for i, n in enumerate(sorted(inputs)):
            rows = [cr[e.dst] for e in graph.edges if e.src == n and e.dst in cr]
            if rows:
                cr[n] = min(rows) - 4 - 3 * i     # staggered: two chests must never
                #                                   share a row (they'd overlap at x=0)
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
        for f, o, ltype, meta in plan.loaders:
            g = (f[0] + DIR_DELTA[o][0], f[1] + DIR_DELTA[o][1])
            tl = (min(f[0], g[0]), min(f[1], g[1]))
            meta = dict(meta)
            meta["net"] = nid
            layout.add(PlacedEntity(LOADER, tl[0], tl[1],
                                    direction=o if ltype == "output" else OPPOSITE[o],
                                    loader_type=ltype, meta=meta))
    return layout


def _compile_at(graph: Graph, vgap: int) -> Layout:
    layout, bodies, fluid_sinks = _place(graph, vgap)
    ctx = _Ctx(graph, bodies, fluid_sinks)
    # PLAN POWER BEFORE ROUTING: substations + EEI claim their 2x2s while the ground
    # is still open (post-routing, dense fields have no free 2x2 left), and their
    # tiles become router walls -- belts/pipes go around or dive under them. Chest
    # perimeters (3 deep: face + loader body + feed) are precious I/O ground a chest
    # cannot spare -- a substation on a face once starved a 4-product sink -- so the
    # planner treats them as occupied and snaps elsewhere.
    halo: set = set()
    busy = {c for (c, _t) in ctx.pair_partner} | {c for c, n in ctx.n_products.items()
                                                  if n > 2}
    for n, b in bodies.items():
        # keep substations out of working ground: a 2-ring around every machine
        # (inserter faces + first gutter lane), 3 around chests (loader depth), and a
        # full 8 apron around pair/multi-product sinks (their riser staging area --
        # a substation there once cost wide_reconverge its convergence)
        depth = 8 if n in busy else (3 if b.proto in (CHEST_INPUT, CHEST_OUTPUT) else 2)
        w, h = b.size
        for tx in range(b.x - depth, b.x + w + depth):
            for ty in range(b.y - depth, b.y + h + depth):
                halo.add((tx, ty))
    # EEI hint: west of the build at the INPUT row -- power feeds in alongside the
    # raw materials (input chests sit in column 0 by construction).
    inputs = [b for b in bodies.values() if b.proto == CHEST_INPUT]
    anchor = inputs if inputs else list(bodies.values())
    hint = (min(b.x for b in anchor) - 6,
            round(sum(b.y for b in anchor) / len(anchor)))
    # coverage target: machine footprints dilated by 3 -- inserters (<=2), loader
    # assemblies (<=3) and most gutter taps. Wider dilation created corner slabs no
    # consumer can occupy and greedy set cover chased those slivers with extra
    # substations (a 3-consumer build once got 8); true outliers are patched after
    # routing by patch_power on real ground.
    cover: set = set()
    for b in bodies.values():
        w, h = b.size
        for tx in range(b.x - 3, b.x + w + 3):
            for ty in range(b.y - 3, b.y + h + 3):
                cover.add((tx, ty))
    pplan = plan_power(ctx.walls | halo, cover=cover, eei_hint=hint)
    ctx.walls |= power_tiles(pplan)
    belt_nets, fluid_nets = _build_nets(graph, bodies)
    plans = _negotiate(ctx, belt_nets, fluid_nets)
    _emit(layout, plans)
    emit_power(layout, patch_power(layout, pplan))
    return layout


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
    gaps = ((FLUID_VGAP, 6, 10)
            if any(e.fluid for e in graph.edges) or graph.no_merge
            else (FLUID_VGAP,))
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
