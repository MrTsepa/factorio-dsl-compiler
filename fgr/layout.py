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
import math
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
FURNACE = "electric-furnace"      # smelting (no recipe field; auto-smelts its input)
CHEMICAL = "chemical-plant"       # recipes with fluid ingredients
FLUID_SOURCE = "infinity-pipe"    # an infinite fluid source (pipe analogue of the chest)
INSERTER = "inserter"
BELT = "transport-belt"
UNDERGROUND = "underground-belt"
SPLITTER = "splitter"
PIPE = "pipe"                     # fluids travel by pipe, not belt
PIPE_TO_GROUND = "pipe-to-ground"  # a pipe tunnel (to cross belts/other pipes)
TANK = "storage-tank"             # a fluid sink/buffer (output that receives fluid)

INPUT_FILL = 4800  # items the infinity chest maintains (~a full chest)

# Footprints in tiles. The splitter's 2x1 is its DEFAULT (north/south) orientation;
# PlacedEntity.size swaps it to 1x2 when the splitter faces east/west.
SIZE = {CHEST_INPUT: (1, 1), CHEST_OUTPUT: (1, 1), ASSEMBLER: (3, 3),
        FURNACE: (3, 3), CHEMICAL: (3, 3), FLUID_SOURCE: (1, 1), TANK: (3, 3),
        INSERTER: (1, 1), BELT: (1, 1), UNDERGROUND: (1, 1), SPLITTER: (2, 1),
        PIPE: (1, 1), PIPE_TO_GROUND: (1, 1)}

# Real fluid-box pipe connections (from Factorio data), as (position-relative-to-
# centre, pipe-out direction, flow). These ROTATE with the entity, so the actual
# connection tiles depend on the body's `direction` -- see _fluid_connections.
FLUID_BOX = {
    CHEMICAL:      [((-1, -1), NORTH, "input"), ((1, -1), NORTH, "input"),
                    ((-1, 1), SOUTH, "output"), ((1, 1), SOUTH, "output")],
    # assembling-machine-2/3 craft "crafting-with-fluid" recipes (e.g. electric-engine-unit,
    # processing-unit): one fluid input (north) + one output (south).
    ASSEMBLER:     [((0, -1), NORTH, "input"), ((0, 1), SOUTH, "output")],
    FLUID_SOURCE:  [((0, 0), NORTH, "both"), ((0, 0), EAST, "both"),
                    ((0, 0), SOUTH, "both"), ((0, 0), WEST, "both")],
    TANK:          [((-1, -1), NORTH, "both"), ((1, 1), EAST, "both"),
                    ((1, 1), SOUTH, "both"), ((-1, -1), WEST, "both")],
}


def _rot_cw(v, k):
    x, y = v
    for _ in range(k % 4):
        x, y = -y, x
    return (x, y)


def _fluid_connections(proto, x, y, direction):
    """External pipe tiles for an entity's fluid boxes, rotated by ``direction``:
    a list of (tile, flow) where flow is 'input' | 'output' | 'both'. ``tile`` is
    where a pipe must sit to attach to that fluid box."""
    if proto not in FLUID_BOX:
        return []
    w, h = SIZE[proto]
    cx, cy = x + w / 2, y + h / 2
    d0 = direction or 0
    out = []
    for (px, py), d, flow in FLUID_BOX[proto]:
        rpx, rpy = _rot_cw((px, py), d0 // 4)
        rd = (d + d0) % 16
        ctile = (math.floor(cx + rpx), math.floor(cy + rpy))
        dxy = DIR_DELTA[rd]
        out.append(((ctile[0] + dxy[0], ctile[1] + dxy[1]), flow))
    return out

UG_MAX_GAP = 5    # underground-belt max_distance (entrance/exit at most this far apart)
PIPE_UG_GAP = 10  # pipe-to-ground reaches farther underground than belts (vanilla 2.0)

# --- placement spacing knobs -------------------------------------------------
COL_STRIDE = 13  # tiles between successive column origins (assembler=3 + wide corridor)
ROW_STRIDE = 7   # baseline rows per node (assembler=3 + gap); see ROW_GAP for stacking
ROW_GAP = 4      # free rows left between a node (incl. its manifold) and the next below


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
            NodeKind.ASSEMBLER: ASSEMBLER, NodeKind.FURNACE: FURNACE,
            NodeKind.CHEMICAL: CHEMICAL, NodeKind.FLUID: FLUID_SOURCE}[kind]


def _node_proto(graph: Graph, name: str, fluid_sinks: set) -> str:
    node = graph.nodes[name]
    if node.kind is NodeKind.OUTPUT and name in fluid_sinks:
        return TANK              # an output that receives fluid is a storage tank
    return _proto_for(node.kind)


# only assemblers / chemical plants carry a recipe field in the blueprint
_RECIPE_KINDS = (NodeKind.ASSEMBLER, NodeKind.CHEMICAL)


def _place_nodes(graph: Graph, col: dict[str, int], shared) -> dict[str, PlacedEntity]:
    """Drop each node's body and return name -> placed body entity. ``shared`` is the
    FINAL shared-belt list (incl. auto-promotions) so spacing reserves manifold room."""
    columns: dict[int, list[str]] = {}
    for name in graph.nodes:  # insertion order -> stable row order within a column
        columns.setdefault(col[name], []).append(name)
    fluid_sinks = {e.dst for e in graph.edges if e.fluid}   # outputs that hold fluid -> tanks

    # A node's fan-out splitter chain extends ~2*(consumers-1) rows below it, so each
    # node must reserve that much vertical room or its manifold collides with the node
    # stacked beneath it. Reserve max(body, manifold) height per node.
    def manifold_rows(name):
        return max([2 * (len(dsts) - 1) for (s, dsts) in shared if s == name], default=0)

    bodies: dict[str, PlacedEntity] = {}
    for c, names in columns.items():
        y = 0
        for name in names:
            node = graph.nodes[name]
            proto = (TANK if node.kind is NodeKind.OUTPUT and name in fluid_sinks
                     else _proto_for(node.kind))
            # Chemical plants stay NORTH: fluid inputs on the north face, outputs on
            # the south, keeping the (crowded) east/west sides free for item inserters
            # and giving the two fluid networks separate north/south room. (The model
            # is rotation-aware -- _fluid_connections handles any direction -- this is
            # just the orientation that routes best.)
            direction = None
            bodies[name] = PlacedEntity(
                proto, x=c * COL_STRIDE, y=y, direction=direction,
                recipe=node.recipe if node.kind in _RECIPE_KINDS else None,
                item=node.item,                       # input chest item / fluid-source fluid
                meta={"node": name},
            )
            h = max(SIZE[proto][1], manifold_rows(name) + 1)
            y += h + ROW_GAP
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


def _consolidate_lanes(graph: Graph, fluid_sinks: set):
    """Decide the belt TOPOLOGY before placement. Start from the DSL's explicit fan-out
    (``shared``) and ``merge`` groups, then:

    1. **auto-promote** -- if a node needs more lanes than its perimeter has tiles,
       bundle its dedicated lanes onto one belt (many outputs -> a shared splitter belt,
       many inputs -> a merge), like a player using a bus instead of ringing a chest;
    2. **absorb** -- fold any leftover dedicated out-edge of a node that already has a
       shared belt into it (one bus beats a bus plus rival lanes fighting one corridor).

    Returns ``(shared, shared_pairs, merges, merge_pairs)``.
    """
    shared = [(s, list(dsts)) for (s, dsts) in graph.shared_belts]
    shared_pairs = {(s, d) for s, dsts in shared for d in dsts}
    merges = [(tuple(srcs), dst) for (srcs, dst) in graph.merges]
    merge_pairs = {(s, dst) for srcs, dst in merges for s in srcs}

    for name in graph.nodes:
        w, h = SIZE[_node_proto(graph, name, fluid_sinks)]
        cap = 2 * (w + h)                                 # perimeter tiles
        ded_in = [e for e in graph.edges if e.dst == name and not e.fluid
                  and (e.src, e.dst) not in merge_pairs]
        ded_out = [e for e in graph.edges if e.src == name and not e.fluid
                   and (name, e.dst) not in shared_pairs and (name, e.dst) not in merge_pairs]
        fluid_ports = sum(1 for e in graph.edges if (e.dst == name or e.src == name) and e.fluid)

        def demand():
            return (len(ded_in) + len(ded_out) + fluid_ports
                    + sum(1 for m in merges if m[1] == name)        # merges arriving
                    + sum(1 for s, _ in shared if s == name)        # fan-out belts leaving
                    + sum(1 for m in merges if name in m[0]))       # merge sources
        while demand() > cap:
            if len(ded_in) >= 2 and len(ded_in) >= len(ded_out):
                merges.append((tuple(e.src for e in ded_in), name))
                merge_pairs.update((e.src, name) for e in ded_in); ded_in = []
            elif len(ded_out) >= 2:
                dsts = tuple(e.dst for e in ded_out)
                shared.append((name, dsts)); shared_pairs.update((name, d) for d in dsts); ded_out = []
            else:
                break   # can't reduce further; the port check during placement will report it

    for i, (s, dsts) in enumerate(shared):
        extra = [e.dst for e in graph.edges if e.src == s and not e.fluid
                 and (s, e.dst) not in shared_pairs and (s, e.dst) not in merge_pairs]
        if extra:
            shared[i] = (s, list(dsts) + extra)
            shared_pairs.update((s, d) for d in extra)
    return shared, shared_pairs, merges, merge_pairs


def _reserve_fluid_networks(graph, bodies, used_perim, blocked, jobs) -> None:
    """Reserve fluid-box tiles and queue pipe-routing jobs (run BEFORE item ports so the
    boxes are claimed first). Each fluid SOURCE drives ONE network: a single OUTPUT box
    branching -- via a nearest-neighbour chain of `pipe` jobs -- to one INPUT box on
    every consumer. One box per source (not per edge) sidesteps the 2/4-connection limit;
    the legs route through the same rip-up router as belts."""
    fluid_used = {name: set() for name in bodies}
    fluid_by_src: dict[str, list[str]] = {}
    for e in graph.edges:
        if e.fluid:
            fluid_by_src.setdefault(e.src, []).append(e.dst)
    for src, consumers in fluid_by_src.items():
        sbox = _pick_fluid_box(bodies[src], "output", fluid_used[src], used_perim[src])
        if sbox is None:
            raise LayoutError(f"fluid source {src!r}: no free output fluid box")
        boxes = [sbox]
        for c in consumers:
            cb = _pick_fluid_box(bodies[c], "input", fluid_used[c], used_perim[c])
            if cb is None:
                raise LayoutError(f"fluid lane {src}~>{c}: no free input fluid box on {c!r}")
            boxes.append(cb)
        blocked.update(boxes)
        chain = _nearest_chain(boxes)
        for a, b in zip(chain, chain[1:]):
            jobs.append((a, b, {"role": "pipe", "pipe": True, "net": src, "edge": (src, b)}))


def _nearest_chain(points):
    """Order ``points`` as a nearest-neighbour chain starting from the first (so a
    branching network is laid as short successive legs that route reliably)."""
    chain, rem = [points[0]], list(points[1:])
    while rem:
        last = chain[-1]
        nxt = min(rem, key=lambda t: abs(t[0] - last[0]) + abs(t[1] - last[1]))
        chain.append(nxt); rem.remove(nxt)
    return chain


def compile_graph(graph: Graph) -> Layout:
    """Generate a candidate :class:`Layout` for ``graph`` -- the reference generator.

    Pipeline: layer nodes into columns -> decide belt topology (fan-out/merge buses) ->
    place bodies -> reserve fluid pipe networks -> place item inserters and queue belt
    lanes (dedicated, fan-out manifolds, merges) -> route every lane with the rip-up
    router. The result is graded independently by :func:`fgr.verify.verify`.
    """
    col = _layer(graph)
    fluid_sinks = {e.dst for e in graph.edges if e.fluid}
    shared, shared_pairs, merges, merge_pairs = _consolidate_lanes(graph, fluid_sinks)

    bodies = _place_nodes(graph, col, shared)
    layout = Layout(list(bodies.values()))
    blocked: set[tuple[int, int]] = set()
    for b in bodies.values():
        blocked.update(b.tiles())
    # Block EVERY fluid-box external tile of a fluid-active body: a pipe sitting on one
    # attaches that body's fluid to the network, so an unused box left open would let a
    # stray passing pipe silently weld in a second fluid. The boxes a network uses are
    # re-added as routing endpoints inside _reserve_fluid_networks. An assembler's box is
    # only active for a fluid recipe, so reserve it ONLY when the node has a fluid lane --
    # otherwise its north/south tiles stay free for item inserters.
    fluid_nodes = {e.src for e in graph.edges if e.fluid} | {e.dst for e in graph.edges if e.fluid}
    for name, b in bodies.items():
        if b.proto in (CHEMICAL, FLUID_SOURCE, TANK) or name in fluid_nodes:
            for tile, _flow in _fluid_connections(b.proto, b.x, b.y, b.direction):
                blocked.add(tile)

    used_perim: dict[str, set] = {name: set() for name in bodies}
    jobs: list[tuple] = []
    _reserve_fluid_networks(graph, bodies, used_perim, blocked, jobs)

    # Input inserters: one per dedicated/shared incoming edge, plus ONE per merge.
    # Ports are taken from anywhere on the perimeter (west-first, then overflow).
    in_anchor: dict[tuple[str, str], tuple[int, int]] = {}
    in_dir: dict[tuple[str, str], int] = {}           # belt must flow INTO the node here
    merge_target: dict[tuple, tuple[int, int]] = {}
    merge_dir: dict[tuple, int] = {}
    for name, body in bodies.items():
        individual = [e for e in graph.edges if e.dst == name and not e.fluid
                      and (e.src, e.dst) not in merge_pairs]
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
    merge_src_anchor: dict[tuple, dict[str, tuple[int, int]]] = {m: {} for m in merges}
    for name, body in bodies.items():
        dedicated = [e for e in graph.edges if e.src == name and not e.fluid
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
                _build_manifold(layout, name, anc, lane[1], in_anchor, in_dir, blocked, jobs)
            else:  # merge_src
                merge_src_anchor[lane[1]][name] = anc

    for m in merges:
        _build_merge(layout, m, merge_src_anchor[m], merge_target[m], merge_dir[m], blocked, jobs)

    _route_jobs(layout, jobs, blocked)
    return layout



def _pick_fluid_box(body, want, used, used_perim):
    """Reserve a free fluid-box external tile of the wanted flow ('input'/'output')."""
    for tile, flow in _fluid_connections(body.proto, body.x, body.y, body.direction):
        if tile in used or (flow != want and flow != "both"):
            continue
        used.add(tile)
        used_perim.add(tile)
        return tile
    return None


def _build_manifold(layout, src, out_anchor, dsts, in_anchor, in_dir, blocked, jobs) -> None:
    """One belt off `src` fanning out to several consumers via a splitter chain.

    A compact south-facing splitter chain sits just east of the source (where its
    output inserter drops): splitter i peels one east output and continues south to
    splitter i+1; the last splitter's two outputs feed the final two consumers. Each
    peeled tail is then *routed* to its consumer's input anchor. Consumers are peeled
    top-to-bottom (sorted by row) so the tails don't cross.
    """
    bx, R = out_anchor[0], out_anchor[1]
    cons = sorted(dsts, key=lambda d: in_anchor[(src, d)][1])
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

    The trunk runs SOUTH in a fixed column (``bx``) east of the sources; each splitter
    sits one column WEST of the trunk so its free input faces the sources. A source
    therefore curves straight into that west input from its own side -- it never has to
    tunnel UNDER the bus to reach a far (east) input, which is what forced the old
    underground U-turns. n sources => n-1 splitters; the trunk end routes to the consumer.
    """
    sources, dst = merge
    order = sorted(sources, key=lambda s: src_anchor[s][1])        # top-to-bottom
    rows = [src_anchor[s][1] for s in order]
    # trunk column: east of the sources, shifted further east until it AND the splitter's
    # west tile (bx-1) are clear of bodies, fan-out manifolds, and ANOTHER merge's bus.
    bx = max(a[0] for a in src_anchor.values()) + 2
    span = range(min(rows) - 1, max(rows) + 2)
    while any((bx, y) in blocked or (bx - 1, y) in blocked for y in span):
        bx += 1
    # topmost source heads the trunk (curves into column bx flowing south)
    jobs.append((src_anchor[order[0]], (bx, rows[0]),
                 {"role": "belt", "edge": (order[0], dst), "end_dir": SOUTH}))
    for i in range(1, len(order)):
        for y in range(rows[i - 1] + 1, rows[i]):                 # trunk belts down to the splitter
            layout.add(PlacedEntity(BELT, bx, y, direction=SOUTH, meta={"role": "merge", "dst": dst}))
            blocked.add((bx, y))
        # splitter spans (bx-1, bx): trunk feeds its EAST input, the source its WEST input
        sp = PlacedEntity(SPLITTER, bx - 1, rows[i], direction=SOUTH, meta={"role": "splitter", "merge": dst})
        layout.add(sp)
        blocked.update(sp.tiles())
        jobs.append((src_anchor[order[i]], (bx - 1, rows[i] - 1),  # source -> WEST input, from its own side
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
    gx = [t[0] for t in static] or [0]
    gy = [t[1] for t in static] or [0]
    glob = (min(gx) - 8, max(gx) + 8, min(gy) - 8, max(gy) + 8)

    def manh(j):
        s, g, _ = j
        return abs(s[0] - g[0]) + abs(s[1] - g[1])

    def lane_bounds(s, g, margin):
        # search only a local window around the lane (keeps A* fast on big layouts),
        # clamped to the global extent
        lo_x = max(glob[0], min(s[0], g[0]) - margin)
        hi_x = min(glob[1], max(s[0], g[0]) + margin)
        lo_y = max(glob[2], min(s[1], g[1]) - margin)
        hi_y = min(glob[3], max(s[1], g[1]) + margin)
        return (lo_x, hi_x, lo_y, hi_y)

    order = sorted(range(len(jobs)), key=lambda i: (manh(jobs[i]), i))   # short lanes first
    routed: dict[int, list] = {}
    tiles: dict[int, set] = {}                   # all tiles incl. buried (for occupancy)
    surf: dict[int, set] = {}                    # SURFACE (entity) tiles only (for fluid isolation)
    occ = set(static)
    hist: dict[tuple[int, int], int] = {}
    pending = deque(order)
    budget = 20 * len(jobs) + 80
    net_boxes: dict[str, set] = {}              # fluid net -> its box tiles (for isolation)
    for s, g, m in jobs:
        if m.get("net") is not None:
            net_boxes.setdefault(m["net"], set()).update((s, g))
    while pending:
        idx = pending.popleft()
        s, g, meta = jobs[idx]
        # a fluid lane may not run ADJACENT to a DIFFERENT fluid's SURFACE pipes/ends
        # (that would weld two fluids into one network); forbid those neighbour tiles.
        # It may freely cross another fluid by tunnelling -- the BURIED segment is
        # underground and doesn't connect, so buried tiles are excluded here (this is
        # what makes long-reach pipe tunnels a routing *advantage*, not a wall).
        ob = occ
        net = meta.get("net")
        if net is not None:
            avoid = set()                       # other fluid nets' surface pipes + reserved boxes
            for j in routed:
                jn = jobs[j][2].get("net")
                if jn is not None and jn != net:
                    avoid |= surf[j]
            for onet, bxs in net_boxes.items():
                if onet != net:
                    avoid |= bxs
            ob = occ | {(x + DIR_DELTA[d][0], y + DIR_DELTA[d][1])
                        for (x, y) in avoid for d in CARDINALS}
        # try a tight window first (fast), widen on failure before falling to rip-up.
        # The router builds short (<=UG_MAX_GAP) tunnels for both belts and pipes -- always
        # valid and fast; the verifier independently ACCEPTS the longer pipe tunnels real
        # Factorio allows, so it correctly grades any generator's layout, not just ours.
        ops = (_route_lane(s, g, lane_bounds(s, g, 12), ob, hist)
               or _route_lane(s, g, lane_bounds(s, g, 28), ob, hist))
        if ops is not None:
            t = _lane_tiles(ops)
            routed[idx], tiles[idx] = ops, t
            surf[idx] = {op[1] for op in ops}   # entity tiles only (pipes + tunnel ends)
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
            del routed[j], tiles[j], surf[j]
            pending.append(j)
        occ = set(static)                       # rebuild exactly: static + surviving lanes
        for surviving in tiles.values():        # (avoids un-blocking a tile a tunnel shared)
            occ |= surviving
        pending.appendleft(idx)
    pipe_done: set = set()                        # de-dup shared box tiles across a net's legs
    for idx in range(len(jobs)):                  # commit in stable order
        meta = jobs[idx][2]
        ops = routed[idx]
        if meta.get("pipe"):                      # same-network legs share their box tiles
            ops = [op for op in ops if op[1] not in pipe_done]
            pipe_done.update(op[1] for op in routed[idx])
        _lay_ops(layout, ops, meta, meta.get("end_dir", EAST))
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


def _route_lane(start, goal, bounds, occ, hist, ug_max=UG_MAX_GAP):
    """Search a lane; if the path self-crosses (a tunnel running over its own belt),
    block those tiles and retry until the path is simple."""
    extra = set()
    for _ in range(6):
        ops = _search(start, goal, bounds, occ | extra if extra else occ, hist, ug_max)
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
    """Turn a routed op-list into placed entities. A *pipe* lane lays undirected
    pipes (and pipe-to-ground for tunnels); an item lane lays directed belts +
    underground-belts."""
    pipe = meta.get("pipe", False)
    n = len(ops)
    for i, (kind, tile, *rest) in enumerate(ops):
        if kind == "belt":
            if pipe:                          # pipes auto-connect; no direction needed
                layout.add(PlacedEntity(PIPE, tile[0], tile[1], meta=meta))
                continue
            if i + 1 < n:
                nxt = ops[i + 1][1]
                d = delta_to_dir(nxt[0] - tile[0], nxt[1] - tile[1])
            else:
                d = end_dir  # final belt direction (into an inserter, or a bus/splitter)
            layout.add(PlacedEntity(BELT, tile[0], tile[1], direction=d, meta=meta))
        elif pipe:  # pipe tunnel. A pipe-to-ground's `direction` is its OPEN (above-ground)
            d = rest[0]  # mouth; the underground side is the opposite. Fluid flows the hop
            # direction d, so the entrance's underground faces d => its open mouth (direction)
            # faces back (OPPOSITE[d]); the exit's open mouth faces forward (d).
            layout.add(PlacedEntity(PIPE_TO_GROUND, tile[0], tile[1],
                                    direction=OPPOSITE[d] if kind == "ug_in" else d, meta=meta))
        else:       # belt tunnel: both ends face the (straight) hop direction
            layout.add(PlacedEntity(UNDERGROUND, tile[0], tile[1], direction=rest[0],
                                    ug_type="input" if kind == "ug_in" else "output",
                                    meta=meta))


def _search(start, goal, bounds, occ, hist=None, ug_max=UG_MAX_GAP):
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
        for ncost, node, ops in _moves(cur, goal, free, hist, ug_max):
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


def _moves(cur, goal, free, hist, ug_max=UG_MAX_GAP):
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
    if ug_max < 2:                            # ug disabled (surface only)
        return
    for d in CARDINALS:                       # underground hop (straight, fed by cur's belt)
        if d == no_back:
            continue
        dx, dy = DIR_DELTA[d]
        e = (t[0] + dx, t[1] + dy)            # entrance (a mid-hop tile, validated below)
        if not free(e):
            continue
        if free((e[0] + dx, e[1] + dy)):      # nothing to tunnel under right ahead -> a belt
            continue                          # is cheaper; defer tunnelling to the obstacle
        for m in range(2, ug_max + 1):         # entrance->exit distance (<= max_distance)
            x = (e[0] + dx * m, e[1] + dy * m)  # exit
            if not free(x):
                continue
            yield ((1 + m) + UG_PENALTY + hist.get(e, 0) + hist.get(x, 0),
                   ("X", x, d), [("ug_in", e, d), ("ug_out", x, d)])
