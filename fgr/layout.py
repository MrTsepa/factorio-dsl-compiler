"""A reference layout generator: production graph -> placed entities.

This is *one* concrete generator. The whole point of the POC is that the
generator is interchangeable — it could be a search or a model — because the
:mod:`fgr.verify` oracle grades any layout against the spec. This particular
generator is deterministic: it layers the graph into columns, drops chests and
assemblers on a tile grid, attaches inserters, and routes each "belt lane" with
a breadth-first grid router.

Coordinates: tile space, x to the right, y down (Factorio convention). An entity
is recorded by the tile coordinate of its top-left tile plus its (w, h) size.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field

from .ir import EAST, WEST, NORTH, SOUTH, DIR_DELTA, OPPOSITE, Graph, NodeKind, delta_to_dir

CARDINALS = (EAST, SOUTH, NORTH, WEST)  # fixed order -> deterministic routing
UG_PENALTY = 4  # extra cost of an underground hop; tunnel only when it beats a detour

# --- entity prototypes & footprints -----------------------------------------
# Inputs are infinity chests stocked with their item — a regular chest can't carry
# contents in a blueprint, but an infinity chest is "always full of the material"
# (and works in-game in sandbox/editor), which is what makes the factory runnable.
CHEST_INPUT = "infinity-chest"
CHEST_OUTPUT = "steel-chest"
ASSEMBLER = "assembling-machine-2"
INSERTER = "inserter"
BELT = "transport-belt"
UNDERGROUND = "underground-belt"
SPLITTER = "splitter"

INPUT_FILL = 4800  # items the infinity chest maintains (~a full chest)

# Footprints in tiles. The splitter's 2x1 is its DEFAULT (north/south) orientation;
# PlacedEntity.size swaps it to 1x2 when the splitter faces east/west.
SIZE = {CHEST_INPUT: (1, 1), CHEST_OUTPUT: (1, 1), ASSEMBLER: (3, 3),
        INSERTER: (1, 1), BELT: (1, 1), UNDERGROUND: (1, 1), SPLITTER: (2, 1)}

UG_MAX_GAP = 5  # underground-belt max_distance (entrance/exit at most this far apart)

# --- placement spacing knobs -------------------------------------------------
COL_STRIDE = 13  # tiles between successive column origins (assembler=3 + wide corridor)
ROW_STRIDE = 7   # tiles between stacked nodes inside a column (assembler=3 + gap)


class LayoutError(RuntimeError):
    """Raised when this generator cannot produce a layout (e.g. routing fails)."""


@dataclass
class PlacedEntity:
    """A single entity dropped on the grid (top-left tile = (x, y))."""

    proto: str
    x: int
    y: int
    direction: int | None = None
    recipe: str | None = None
    item: str | None = None     # infinity-chest only: the item it is stocked with
    ug_type: str | None = None  # underground-belt only: "input" (entrance) | "output" (exit)
    meta: dict = field(default_factory=dict)  # debug only; the verifier ignores this

    @property
    def size(self) -> tuple[int, int]:
        if self.proto == SPLITTER:  # 2 wide perpendicular to flow
            return (1, 2) if self.direction in (EAST, WEST) else (2, 1)
        return SIZE[self.proto]

    def tiles(self) -> list[tuple[int, int]]:
        w, h = self.size
        return [(self.x + dx, self.y + dy) for dy in range(h) for dx in range(w)]

    @property
    def center(self) -> tuple[float, float]:
        w, h = self.size
        return (self.x + w / 2, self.y + h / 2)


@dataclass
class Layout:
    """A candidate layout: a flat list of placed entities."""

    entities: list[PlacedEntity] = field(default_factory=list)

    def add(self, e: PlacedEntity) -> PlacedEntity:
        self.entities.append(e)
        return e


def _layer(graph: Graph) -> dict[str, int]:
    """Assign each node a column index = longest dependency depth from a source."""
    col: dict[str, int] = {}

    def depth(name: str, stack: tuple[str, ...] = ()) -> int:
        if name in stack:
            raise LayoutError(f"cycle through node {name!r}; the graph must be a DAG")
        if name in col:
            return col[name]
        preds = graph.predecessors(name)
        d = 0 if not preds else 1 + max(depth(p, stack + (name,)) for p in preds)
        col[name] = d
        return d

    for n in graph.nodes:
        depth(n)
    return col


def _proto_for(kind: NodeKind) -> str:
    return {NodeKind.INPUT: CHEST_INPUT, NodeKind.OUTPUT: CHEST_OUTPUT,
            NodeKind.ASSEMBLER: ASSEMBLER}[kind]


def _place_nodes(graph: Graph, col: dict[str, int]) -> dict[str, PlacedEntity]:
    """Drop each node's body and return name -> placed body entity."""
    columns: dict[int, list[str]] = {}
    for name in graph.nodes:  # insertion order -> stable row order within a column
        columns.setdefault(col[name], []).append(name)

    bodies: dict[str, PlacedEntity] = {}
    for c, names in columns.items():
        for row, name in enumerate(names):
            node = graph.nodes[name]
            proto = _proto_for(node.kind)
            bodies[name] = PlacedEntity(
                proto, x=c * COL_STRIDE, y=row * ROW_STRIDE,
                recipe=node.recipe, item=node.item, meta={"node": name},
            )
    return bodies


# A perimeter "port" is a tile just outside the body where an inserter sits, paired
# with the belt anchor one tile further out. out_dir points away from the body
# (an output inserter faces inward to grab the body and drops here; an input
# inserter faces outward to grab the belt and drops into the body), in_dir points in.
def _perimeter(body: PlacedEntity):
    """All inserter ports around a body: (perimeter_tile, anchor_tile, out_dir, in_dir, side)."""
    x, y = body.x, body.y
    w, h = body.size
    ports = []
    for r in _center_first(range(y, y + h)):          # west
        ports.append(((x - 1, r), (x - 2, r), WEST, EAST, "W"))
    for r in _center_first(range(y, y + h)):          # east
        ports.append(((x + w, r), (x + w + 1, r), EAST, WEST, "E"))
    for c in _center_first(range(x, x + w)):          # north
        ports.append(((c, y - 1), (c, y - 2), NORTH, SOUTH, "N"))
    for c in _center_first(range(x, x + w)):          # south
        ports.append(((c, y + h), (c, y + h + 1), SOUTH, NORTH, "S"))
    return ports


def _center_first(seq):
    seq = list(seq)
    mid = len(seq) // 2
    order = [mid]
    for off in range(1, len(seq)):
        if mid - off >= 0:
            order.append(mid - off)
        if mid + off < len(seq):
            order.append(mid + off)
    seen, out = set(), []
    for i in order:
        if i not in seen:
            seen.add(i)
            out.append(seq[i])
    return out


def _alloc_ports(body, n, kind, used, top_east=False):
    """Pick n free perimeter ports. Inputs prefer the west side, outputs the east,
    each overflowing to north/south then the far side — so all 4 sides are usable."""
    by_side = {s: [p for p in _perimeter(body) if p[4] == s] for s in "WENS"}
    sides = ["W", "N", "S", "E"] if kind == "in" else ["E", "N", "S", "W"]
    seq = []
    for s in sides:
        side_ports = by_side[s]
        if s == "E" and top_east:                     # a shared-belt bus must start at the top
            side_ports = sorted(side_ports, key=lambda p: p[0][1])
        seq += side_ports
    avail = [p for p in seq if p[0] not in used]
    if len(avail) < n:
        raise LayoutError(
            f"node {body.meta.get('node')!r} needs {n} {kind} ports but only "
            f"{len(avail)} free perimeter tiles remain")
    chosen = avail[:n]
    used.update(p[0] for p in chosen)
    return chosen


def compile_graph(graph: Graph) -> Layout:
    """Generate a candidate :class:`Layout` for ``graph``.

    Lanes come in three shapes: a dedicated edge ``A -> B`` (one belt), a shared
    belt ``A -> B, C`` (one output off A, a splitter bus fans out), and a merge
    ``A, B -> C`` (each source gets an output, a splitter chain combines them onto
    ONE belt into a single input inserter on C).
    """
    col = _layer(graph)
    bodies = _place_nodes(graph, col)
    layout = Layout(list(bodies.values()))
    blocked: set[tuple[int, int]] = set()
    for b in bodies.values():
        blocked.update(b.tiles())

    shared = [(s, list(dsts)) for (s, dsts) in graph.shared_belts]
    shared_pairs = {(s, d) for s, dsts in shared for d in dsts}
    merges = [(tuple(srcs), dst) for (srcs, dst) in graph.merges]
    merge_pairs = {(s, dst) for srcs, dst in merges for s in srcs}

    used_perim: dict[str, set] = {name: set() for name in bodies}

    # Input inserters: one per dedicated/shared incoming edge, plus ONE per merge.
    # Ports are taken from anywhere on the perimeter (west-first, then overflow).
    in_anchor: dict[tuple[str, str], tuple[int, int]] = {}
    in_dir: dict[tuple[str, str], int] = {}           # belt must flow INTO the node here
    merge_target: dict[tuple, tuple[int, int]] = {}
    merge_dir: dict[tuple, int] = {}
    for name, body in bodies.items():
        individual = [e for e in graph.edges if e.dst == name and (e.src, e.dst) not in merge_pairs]
        merges_in = [m for m in merges if m[1] == name]
        in_lanes = [("ded", e) for e in individual] + [("merge", m) for m in merges_in]
        ports = _alloc_ports(body, len(in_lanes), "in", used_perim[name])
        for lane, (perim, anc, out_d, in_d, _side) in zip(in_lanes, ports):
            # input inserter faces OUT (grabs the belt anchor) and drops into the body
            if lane[0] == "ded":
                e = lane[1]
                layout.add(PlacedEntity(INSERTER, *perim, direction=out_d,
                                        meta={"role": "in-inserter", "edge": (e.src, e.dst)}))
                in_anchor[(e.src, e.dst)] = anc
                in_dir[(e.src, e.dst)] = in_d
            else:
                layout.add(PlacedEntity(INSERTER, *perim, direction=out_d,
                                        meta={"role": "in-inserter", "merge": lane[1]}))
                merge_target[lane[1]] = anc
                merge_dir[lane[1]] = in_d
            blocked.add(perim)

    # Output inserters + lanes.
    jobs: list[tuple] = []
    merge_src_anchor: dict[tuple, dict[str, tuple[int, int]]] = {m: {} for m in merges}
    for name, body in bodies.items():
        dedicated = [e for e in graph.edges if e.src == name
                     and (name, e.dst) not in shared_pairs and (name, e.dst) not in merge_pairs]
        shared_here = [dsts for (s, dsts) in shared if s == name]
        merge_here = [m for m in merges if name in m[0]]
        out_lanes = ([("shared", d) for d in shared_here] + [("ded", e) for e in dedicated]
                     + [("merge_src", m) for m in merge_here])
        ports = _alloc_ports(body, len(out_lanes), "out", used_perim[name], top_east=bool(shared_here))
        for lane, (perim, anc, out_d, in_d, _side) in zip(out_lanes, ports):
            # output inserter faces IN (grabs the body) and drops onto the belt anchor
            layout.add(PlacedEntity(INSERTER, *perim, direction=in_d,
                                    meta={"role": "out-inserter", "src": name}))
            blocked.add(perim)
            if lane[0] == "ded":
                dst = lane[1].dst
                jobs.append((anc, in_anchor[(name, dst)],
                             {"role": "belt", "edge": (name, dst), "end_dir": in_dir[(name, dst)]}))
            elif lane[0] == "shared":
                _build_manifold(layout, name, anc, dsts := lane[1], in_anchor, in_dir, blocked, jobs)
            else:  # merge_src
                merge_src_anchor[lane[1]][name] = anc

    for m in merges:
        _build_merge(layout, m, merge_src_anchor[m], merge_target[m], merge_dir[m], blocked, jobs)

    _route_jobs(layout, jobs, blocked)
    return layout


def _build_manifold(layout, src, out_anchor, dsts, in_anchor, in_dir, blocked, jobs) -> None:
    """One belt off `src` fanning out to several consumers via a splitter chain.

    A compact south-facing splitter chain sits just east of the source (where its
    output inserter drops): splitter i peels one east output and continues south to
    splitter i+1; the last splitter's two outputs feed the final two consumers. Each
    peeled tail is then *routed* to its consumer's input anchor, so consumers may sit
    anywhere (above, below, far) -- the chain doesn't assume their row order.
    """
    bx, R = out_anchor[0], out_anchor[1]
    cons = list(dsts)
    n = len(cons)

    def tail_job(start, dst):
        jobs.append((start, in_anchor[(src, dst)],
                     {"role": "belt", "edge": (src, dst), "end_dir": in_dir[(src, dst)]}))

    if n == 1:                                    # degenerate (shared belts have >=2)
        tail_job(out_anchor, cons[0])
        return
    y = R
    for i in range(n - 1):
        sp = PlacedEntity(SPLITTER, bx, y, direction=SOUTH, meta={"role": "splitter", "src": src})
        layout.add(sp)
        blocked.update(sp.tiles())
        tail_job((bx + 1, y + 1), cons[i])        # east output -> consumer i
        if i < n - 2:                             # continue the chain south to the next splitter
            layout.add(PlacedEntity(BELT, bx, y + 1, direction=SOUTH, meta={"role": "trunk", "src": src}))
            blocked.add((bx, y + 1))
            y += 2
        else:                                     # last splitter's straight output -> last consumer
            tail_job((bx, y + 1), cons[n - 1])


def _build_merge(layout, merge, src_anchor, target, target_dir, blocked, jobs) -> None:
    """Several sources combined onto ONE belt via a south-flowing splitter bus.

    Mirror of the fan-out bus: the topmost source starts a belt flowing SOUTH in a
    fixed column; each lower source merges in through a south-facing splitter (its
    belt arrives flowing south into the splitter's free north input). The bus stays
    in this one column (it does NOT march east toward the consumer), then its end is
    routed east to the consumer's single input inserter. n sources => n-1 splitters.
    """
    sources, dst = merge
    order = sorted(sources, key=lambda s: src_anchor[s][1])        # top-to-bottom
    rows = [src_anchor[s][1] for s in order]
    bx = max(a[0] for a in src_anchor.values()) + 2               # bus column, east of sources
    # topmost source starts the bus (its lane ends flowing south into column bx)
    jobs.append((src_anchor[order[0]], (bx, rows[0]),
                 {"role": "belt", "edge": (order[0], dst), "end_dir": SOUTH}))
    for i in range(1, len(order)):
        for y in range(rows[i - 1] + 1, rows[i]):                 # bus belts down to the splitter
            layout.add(PlacedEntity(BELT, bx, y, direction=SOUTH, meta={"role": "merge", "dst": dst}))
            blocked.add((bx, y))
        sp = PlacedEntity(SPLITTER, bx, rows[i], direction=SOUTH, meta={"role": "splitter", "merge": dst})
        layout.add(sp)
        blocked.update(sp.tiles())
        jobs.append((src_anchor[order[i]], (bx + 1, rows[i] - 1),  # source -> splitter's free north input
                     {"role": "belt", "edge": (order[i], dst), "end_dir": SOUTH}))
    jobs.append(((bx, rows[-1] + 1), target,
                 {"role": "belt", "edge": ("merge", dst), "end_dir": target_dir}))


def _route_jobs(layout, jobs, blocked) -> None:
    """Route all lanes with rip-up/retry + congestion history.

    Greedy one-shot routing fails on dense crossing layouts (a lane claims tiles a
    later lane needs). Instead: route short lanes first; if a lane can't fit, rip up
    the already-routed lanes near it, charge their tiles a growing *history* cost so
    routes learn to spread out, and retry. Lanes are committed to the layout only
    once every lane has a path. Deterministic: fixed ordering + deterministic search.
    """
    if not jobs:
        return
    static = set(blocked)
    static.update(t for (s, g, _m) in jobs for t in (s, g))   # each lane reserves its endpoints
    bx = [t[0] for t in static] or [0]
    by = [t[1] for t in static] or [0]
    bounds = (min(bx) - 8, max(bx) + 8, min(by) - 8, max(by) + 8)

    def manh(j):
        s, g, _ = j
        return abs(s[0] - g[0]) + abs(s[1] - g[1])

    order = sorted(range(len(jobs)), key=lambda i: (manh(jobs[i]), i))   # short lanes first
    routed: dict[int, list] = {}
    tiles: dict[int, set] = {}
    occ = set(static)
    hist: dict[tuple[int, int], int] = {}
    pending = deque(order)
    budget = 15 * len(jobs) + 60
    while pending:
        idx = pending.popleft()
        s, g, meta = jobs[idx]
        ops = _route_lane(s, g, bounds, occ, hist)
        if ops is not None:
            t = _lane_tiles(ops)
            routed[idx], tiles[idx] = ops, t
            occ |= t
            continue
        budget -= 1
        # rip up the few routed lanes most in the way; charge their tiles a growing
        # history cost so the re-routes (and this lane) learn to spread out
        victims = sorted((j for j in routed if _near(jobs[idx], tiles[j])),
                         key=lambda j: -_overlap(jobs[idx], tiles[j]))[:2]
        if not victims or budget <= 0:
            raise LayoutError(f"could not route belt lane {meta.get('edge')} (gave up after rip-up)")
        for j in victims:
            for tt in tiles[j]:
                hist[tt] = hist.get(tt, 0) + 2  # contested tiles get pricier
            del routed[j], tiles[j]
            pending.append(j)
        occ = set(static)                       # rebuild exactly: static + surviving lanes
        for surviving in tiles.values():        # (avoids un-blocking a tile a tunnel shared)
            occ |= surviving
        pending.appendleft(idx)
    for idx in range(len(jobs)):                 # commit in stable order
        meta = jobs[idx][2]
        _lay_ops(layout, routed[idx], meta, meta.get("end_dir", EAST))
        blocked.update(tiles[idx])


def _lane_tiles(ops) -> set:
    """All tiles a lane occupies INCLUDING the buried tiles between an underground's
    entrance and exit -- so no other lane runs through (or mis-pairs with) the tunnel."""
    s = set()
    for i, (kind, tile, *rest) in enumerate(ops):
        s.add(tile)
        if kind == "ug_in":
            d = rest[0]
            dx, dy = DIR_DELTA[d]
            x = ops[i + 1][1]
            t = (tile[0] + dx, tile[1] + dy)
            while t != x:
                s.add(t)
                t = (t[0] + dx, t[1] + dy)
    return s


def _route_lane(start, goal, bounds, occ, hist):
    """Search a lane; if the path self-crosses (a tunnel running over its own belt),
    block those tiles and retry until the path is simple."""
    extra = set()
    for _ in range(6):
        ops = _search(start, goal, bounds, occ | extra if extra else occ, hist)
        if ops is None:
            return None
        full = []                            # every tile incl. buried ones, with repeats
        for i, (kind, tile, *rest) in enumerate(ops):
            full.append(tile)
            if kind == "ug_in":
                dx, dy = DIR_DELTA[rest[0]]
                x = ops[i + 1][1]
                t = (tile[0] + dx, tile[1] + dy)
                while t != x:
                    full.append(t)
                    t = (t[0] + dx, t[1] + dy)
        seen, dup = set(), set()
        for t in full:
            (dup if t in seen else seen).add(t)
        if not dup:
            return ops
        extra |= dup
    return None


def _bbox(job):
    s, g, _ = job
    return (min(s[0], g[0]) - 3, max(s[0], g[0]) + 3, min(s[1], g[1]) - 3, max(s[1], g[1]) + 3)


def _near(job, lane_tiles) -> bool:
    """True if a routed lane passes through the bounding box of `job`'s endpoints."""
    lo_x, hi_x, lo_y, hi_y = _bbox(job)
    return any(lo_x <= x <= hi_x and lo_y <= y <= hi_y for x, y in lane_tiles)


def _overlap(job, lane_tiles) -> int:
    """How many of a routed lane's tiles fall in `job`'s bounding box (rip priority)."""
    lo_x, hi_x, lo_y, hi_y = _bbox(job)
    return sum(lo_x <= x <= hi_x and lo_y <= y <= hi_y for x, y in lane_tiles)


def _lay_ops(layout, ops, meta, end_dir=EAST) -> None:
    """Turn a routed op-list into placed belts and underground-belt pairs."""
    n = len(ops)
    for i, (kind, tile, *rest) in enumerate(ops):
        if kind == "belt":
            if i + 1 < n:
                nxt = ops[i + 1][1]
                d = delta_to_dir(nxt[0] - tile[0], nxt[1] - tile[1])
            else:
                d = end_dir  # final belt direction (into an inserter, or a bus/splitter)
            layout.add(PlacedEntity(BELT, tile[0], tile[1], direction=d, meta=meta))
        else:  # "ug_in" / "ug_out": direction is the (straight) hop direction
            layout.add(PlacedEntity(UNDERGROUND, tile[0], tile[1], direction=rest[0],
                                    ug_type="input" if kind == "ug_in" else "output",
                                    meta=meta))


def _search(start, goal, bounds, occ, hist=None):
    """Least-cost route start->goal as an op-list of belts + underground hops.

    Belt steps cost 1 (+ congestion history); an underground hop (entrance + tunnel
    + exit) costs its length plus UG_PENALTY, so the router tunnels only to cross
    something it can't cheaply go around. Underground ends are real search nodes
    ("E" entrance / "X" exit) and each tile is locked once finalized, so a path
    never reuses a tile (no self-overlap). Reversals are forbidden (no U-turns).
    Deterministic tie-break via an insertion counter.
    """
    if start == goal:
        return [("belt", start)]
    lo_x, hi_x, lo_y, hi_y = bounds
    hist = hist or {}
    INF = float("inf")

    def free(t):
        return lo_x <= t[0] <= hi_x and lo_y <= t[1] <= hi_y and t not in occ

    def h(tile):                 # A* heuristic: Manhattan distance to the goal
        return abs(tile[0] - goal[0]) + abs(tile[1] - goal[1])

    start_node = ("N", start, None)
    best = {start_node: 0}
    prev = {start_node: None}
    pq = [(h(start), 0, start_node, 0)]
    seq = 1
    goal_node = None
    while pq:
        _, _, cur, g = heapq.heappop(pq)
        if cur[0] == "N" and cur[1] == goal:
            goal_node = cur
            break
        if g > best.get(cur, INF):
            continue
        for ncost, node, ops in _moves(cur, goal, free, hist):
            ng = g + ncost
            if ng < best.get(node, INF):
                best[node] = ng
                prev[node] = (cur, ops)
                heapq.heappush(pq, (ng + h(node[1]), seq, node, ng))
                seq += 1
    if goal_node is None:
        return None
    chain = []
    node = goal_node
    while prev[node] is not None:
        parent, ops = prev[node]
        chain.append(ops)
        node = parent
    chain.reverse()
    result = [("belt", start)]
    for ops in chain:
        result.extend(ops)
    return result


def _moves(cur, goal, free, hist):
    """Yield (cost, landing_node, ops). Nodes: ("N", tile, came_dir) or ("X", tile, dir)."""
    if cur[0] == "X":                         # exit node: resurface straight (no turn yet)
        _, x, d = cur
        dx, dy = DIR_DELTA[d]
        b = (x[0] + dx, x[1] + dy)
        if free(b) or b == goal:
            yield (1 + hist.get(b, 0), ("N", b, d), [("belt", b)])
        return
    _, t, came = cur
    no_back = OPPOSITE[came] if came is not None else None
    for d in CARDINALS:                       # ordinary belt step (never straight back)
        if d == no_back:
            continue
        dx, dy = DIR_DELTA[d]
        nb = (t[0] + dx, t[1] + dy)
        if free(nb) or nb == goal:
            yield (1 + hist.get(nb, 0), ("N", nb, d), [("belt", nb)])
    for d in CARDINALS:                       # underground hop (straight, fed by cur's belt)
        if d == no_back:
            continue
        dx, dy = DIR_DELTA[d]
        e = (t[0] + dx, t[1] + dy)            # entrance (a mid-hop tile, validated below)
        if not free(e):
            continue
        for m in range(2, UG_MAX_GAP + 1):     # entrance->exit distance (<= max_distance)
            x = (e[0] + dx * m, e[1] + dy * m)  # exit
            if not free(x):
                continue
            yield ((1 + m) + UG_PENALTY + hist.get(e, 0) + hist.get(x, 0),
                   ("X", x, d), [("ug_in", e, d), ("ug_out", x, d)])
