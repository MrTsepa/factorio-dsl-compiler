"""v2 layout generator: production graph -> placed entities, as a *lane fabric*.

This is one concrete generator; :mod:`fgr.verify` grades any layout against the spec,
so the generator is swappable. v2 replaces v1's fixed grid + A* rip-up router with four
deterministic passes (no search, no rip-up, cannot give up):

  1. LAYER   columns by ALAP depth; inputs pinned west (col 0), outputs east (last col).
  2. ORDER   barycenter row ordering per column (near-planarize before routing).
  3. PLACE   running-sum X (adaptive, no fixed stride); center-row alignment of the
             dominant chain so the main spine is a dead-straight belt.
  4. EMIT    the universal lane primitive: one belt per PRODUCER, tapped by one inserter
             per consumer (belt-fed rows; merges are multi-tap, no splitters). Crossings
             dive via vertical undergrounds so distinct lanes never weld (no spurious lanes,
             no underground mispairing).

Coordinates: tile space, x right, y down (Factorio). An entity is its top-left tile + size.
See docs/V2_DESIGN.md for the full rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ir import (EAST, WEST, NORTH, SOUTH, DIR_DELTA, OPPOSITE, Graph, NodeKind,
                 delta_to_dir)

# --- entity prototypes & footprints (names are the verifier's contract) -------
CHEST_INPUT = "infinity-chest"
CHEST_OUTPUT = "steel-chest"
ASSEMBLER = "assembling-machine-2"
FURNACE = "electric-furnace"
CHEMICAL = "chemical-plant"
FLUID_SOURCE = "infinity-pipe"
INSERTER = "inserter"
LONG_INSERTER = "long-handed-inserter"   # reach-2 (two-sided belt-fed rows)
BELT = "transport-belt"
UNDERGROUND = "underground-belt"
SPLITTER = "splitter"
PIPE = "pipe"
PIPE_TO_GROUND = "pipe-to-ground"
TANK = "storage-tank"
LOADER = "loader-1x1"                     # full-belt I/O (chest <-> belt)
SUBSTATION = "substation"
EEI = "electric-energy-interface"

INPUT_FILL = 4800  # items an infinity chest maintains

SIZE = {CHEST_INPUT: (1, 1), CHEST_OUTPUT: (1, 1), ASSEMBLER: (3, 3),
        FURNACE: (3, 3), CHEMICAL: (3, 3), FLUID_SOURCE: (1, 1), TANK: (3, 3),
        INSERTER: (1, 1), LONG_INSERTER: (1, 1), BELT: (1, 1), UNDERGROUND: (1, 1),
        SPLITTER: (2, 1), PIPE: (1, 1), PIPE_TO_GROUND: (1, 1), LOADER: (1, 1),
        SUBSTATION: (2, 2), EEI: (2, 2)}

# Real fluid-box pipe connections (Factorio data), as (offset-from-centre, out-dir, flow);
# they ROTATE with the body direction -- see _fluid_connections.
FLUID_BOX = {
    CHEMICAL:      [((-1, -1), NORTH, "input"), ((1, -1), NORTH, "input"),
                    ((-1, 1), SOUTH, "output"), ((1, 1), SOUTH, "output")],
    ASSEMBLER:     [((0, -1), NORTH, "input"), ((0, 1), SOUTH, "output")],
    FLUID_SOURCE:  [((0, 0), NORTH, "both"), ((0, 0), EAST, "both"),
                    ((0, 0), SOUTH, "both"), ((0, 0), WEST, "both")],
    TANK:          [((-1, -1), NORTH, "both"), ((1, 1), EAST, "both"),
                    ((1, 1), SOUTH, "both"), ((-1, -1), WEST, "both")],
}

UG_MAX_GAP = 5     # underground-belt max entrance->exit distance
PIPE_UG_GAP = 10   # pipe-to-ground reach (vanilla 2.0)

# substation geometry (FBSR dump, vanilla 2.0): 2x2, supply 18x18, wire reach 18
SUBSTATION_SUPPLY = 9    # Chebyshev half-extent of the supply area from the body
SUBSTATION_WIRE = 18     # max centre-distance for two substations to connect


def _rot_cw(v, k):
    x, y = v
    for _ in range(k % 4):
        x, y = -y, x
    return (x, y)


import math


def _fluid_connections(proto, x, y, direction, with_dir=False):
    """External pipe tiles for an entity's fluid boxes, rotated by ``direction``: a list of
    (tile, flow) where flow is 'input'|'output'|'both' and tile is where a pipe must sit to
    attach to that box. With ``with_dir=True`` each entry is (tile, flow, machine_dir) where
    machine_dir is the direction from the external tile back toward the machine (the way a
    pipe-to-ground's open mouth must face to feed the box)."""
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
        ext = (ctile[0] + dxy[0], ctile[1] + dxy[1])
        out.append((ext, flow, OPPOSITE[rd]) if with_dir else (ext, flow))
    return out


class LayoutError(RuntimeError):
    """Raised when this generator cannot produce a layout."""


@dataclass
class PlacedEntity:
    proto: str
    x: int
    y: int
    direction: int | None = None
    recipe: str | None = None
    item: str | None = None
    ug_type: str | None = None     # underground-belt: "input"|"output"
    loader_type: str | None = None  # loader: "input"|"output"
    meta: dict = field(default_factory=dict)

    @property
    def size(self) -> tuple[int, int]:
        if self.proto == SPLITTER:
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
    entities: list[PlacedEntity] = field(default_factory=list)

    def add(self, e: PlacedEntity) -> PlacedEntity:
        self.entities.append(e)
        return e


def _proto_for(kind: NodeKind) -> str:
    return {NodeKind.INPUT: CHEST_INPUT, NodeKind.OUTPUT: CHEST_OUTPUT,
            NodeKind.ASSEMBLER: ASSEMBLER, NodeKind.FURNACE: FURNACE,
            NodeKind.CHEMICAL: CHEMICAL, NodeKind.FLUID: FLUID_SOURCE}[kind]


def _node_proto(graph: Graph, name: str, fluid_sinks: set) -> str:
    node = graph.nodes[name]
    if node.kind is NodeKind.OUTPUT and name in fluid_sinks:
        return TANK
    return _proto_for(node.kind)


_RECIPE_KINDS = (NodeKind.ASSEMBLER, NodeKind.CHEMICAL)


# ---------------------------------------------------------------------------
# Pass 1: LAYER (columns). ASAP longest-path; inputs west, outputs east.
# ---------------------------------------------------------------------------
def _layers(graph: Graph) -> dict[str, int]:
    asap: dict[str, int] = {}

    def depth(n, stack=()):
        if n in stack:
            raise LayoutError(f"cycle through {n!r}; graph must be a DAG")
        if n in asap:
            return asap[n]
        preds = graph.predecessors(n)
        d = 0 if not preds else 1 + max(depth(p, stack + (n,)) for p in preds)
        asap[n] = d
        return d

    for n in graph.nodes:
        depth(n)
    non_out = [asap[n] for n in graph.nodes if graph.nodes[n].kind is not NodeKind.OUTPUT]
    out_col = (max(non_out) + 1) if non_out else 1
    col = {}
    for n, node in graph.nodes.items():
        if node.kind is NodeKind.INPUT:
            col[n] = 0
        elif node.kind is NodeKind.OUTPUT:
            col[n] = out_col
        else:
            col[n] = asap[n]
    return col


# ---------------------------------------------------------------------------
# Pass 2: ORDER (row slots within each column) via barycenter.
# ---------------------------------------------------------------------------
def _order(graph: Graph, col: dict[str, int]) -> dict[str, int]:
    cols: dict[int, list[str]] = {}
    for n in graph.nodes:                       # insertion order = stable initial order
        cols.setdefault(col[n], []).append(n)
    slot = {n: i for ns in cols.values() for i, n in enumerate(ns)}

    cmax = max(cols) if cols else 0
    for _ in range(4):
        for c in range(1, cmax + 1):            # down sweep: order by predecessors
            _bary_sweep(graph, cols, slot, c, graph.predecessors)
        for c in range(cmax - 1, -1, -1):       # up sweep: order by successors
            _bary_sweep(graph, cols, slot, c, graph.successors)
    return slot


def _bary_sweep(graph, cols, slot, c, neigh_fn):
    ns = cols.get(c)
    if not ns:
        return
    def key(n):
        ns_adj = [slot[m] for m in neigh_fn(n) if m in slot]
        return (sum(ns_adj) / len(ns_adj)) if ns_adj else slot[n]
    ns.sort(key=lambda n: (key(n), slot[n]))    # stable; original slot breaks ties
    for i, n in enumerate(ns):
        slot[n] = i


# ---------------------------------------------------------------------------
# Pass 3: PLACE. Center-row alignment of the dominant chain; running-sum X.
# ---------------------------------------------------------------------------
def _primary_pred(graph: Graph, n: str, col: dict[str, int]):
    """The predecessor a node should align its row to (its dominant input lane)."""
    preds = [p for p in graph.predecessors(n) if col[p] < col[n]]
    if not preds:
        return None
    # prefer the nearest column, then graph order
    return min(preds, key=lambda p: (col[n] - col[p], list(graph.nodes).index(p)))


def _assign_rows(graph, col):
    """Center row per node: align each node to its primary predecessor's row when free,
    else nearest free slot in its column. Returns {name: center_row}. Bodies of height 3
    occupy center-1..center+1; height-1 occupy [center]."""
    order = _order(graph, col)
    cols = {}
    for n in graph.nodes:
        cols.setdefault(col[n], []).append(n)
    for c in cols:
        cols[c].sort(key=lambda n: order[n])

    fluid_sinks = {e.dst for e in graph.edges if e.fluid}
    indeg: dict[str, int] = {}
    for e in graph.edges:
        if not e.fluid:
            indeg[e.dst] = indeg.get(e.dst, 0) + 1

    def half(n):                                   # half-height (clearance each side)
        return SIZE[_node_proto(graph, n, fluid_sinks)][1] // 2

    def pad(n):                                     # extra clearance for crowded consumers:
        return max(1, (indeg.get(n, 0) + 1) // 2)   # ~1 row of slack per 2 converging inputs

    cr: dict[str, int] = {}
    for c in sorted(cols):
        used: list[tuple[int, int]] = []           # occupied [lo,hi] center-spans this column
        def free(center, hh, pd):
            lo, hi = center - hh - pd, center + hh + pd
            return all(hi < a or lo > b for a, b in used)
        for n in cols[c]:
            hh, pd = half(n), pad(n)
            pp = _primary_pred(graph, n, col)
            want = cr[pp] if pp in cr else 0
            r = want
            for off in range(0, 400):              # search outward from desired row
                if free(want + off, hh, pd):
                    r = want + off; break
                if free(want - off, hh, pd):
                    r = want - off; break
            cr[n] = r
            used.append((r - hh - pd, r + hh + pd))
    return cr, order


# ---------------------------------------------------------------------------
# Geometry: lay a belt/pipe polyline, diving (underground) over occupied tiles.
# ---------------------------------------------------------------------------
def _sign(v):
    return (v > 0) - (v < 0)


def _expand(pts):
    """Polyline waypoints -> ordered list of (tile, dir_to_next). Straight axis-aligned
    segments only. The last tile inherits the previous direction."""
    pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]   # drop repeats
    if len(pts) == 1:
        return [(pts[0], EAST)]
    seq = []
    for a, b in zip(pts, pts[1:]):
        d = delta_to_dir(_sign(b[0] - a[0]), _sign(b[1] - a[1]))
        t = a
        while t != b:
            seq.append((t, d))
            t = (t[0] + DIR_DELTA[d][0], t[1] + DIR_DELTA[d][1])
    seq.append((pts[-1], seq[-1][1] if seq else EAST))
    return seq


def _lay_polyline(layout, occ, pts, meta, pipe=False, ug_max=None):
    """Lay belts (or pipes) from pts[0]..pts[-1] along the polyline, tunnelling under any
    tile already in ``occ``. Endpoints must be free. Returns the set of tiles used (incl.
    buried) or None if a required tunnel exceeds reach. Marks used tiles into ``occ``."""
    if ug_max is None:
        ug_max = PIPE_UG_GAP if pipe else UG_MAX_GAP
    seq = _expand(pts)
    n = len(seq)
    # Only CORNERS (direction changes) must stay surface belts: an underground end can't
    # turn. Endpoints may be undergrounds (an inserter can pick/drop on a ug end, and a
    # feed can enter a trunk from one). Endpoints + corners must be free to place.
    must_surface = set()
    for i in range(1, n):
        if seq[i][1] != seq[i - 1][1]:
            must_surface.add(i)
    if seq[0][0] in occ or seq[-1][0] in occ:
        return None
    if any(seq[i][0] in occ for i in must_surface):
        return None
    ops = []          # (kind, tile, dir)
    used = set()
    i = 0
    while i < n:
        t, d = seq[i]
        if t not in occ:
            ops.append(("belt", t, d)); used.add(t); i += 1
            continue
        # dive under occupied tiles as ONE underground (entrance..exit <= ug_max). Merge
        # nearby occupied runs into a single tunnel so we never emit two same-direction
        # entrances in a line (which the verifier would mispair). Entrance = previous
        # surface belt; exit = first free tile past the LAST occupied within reach.
        if i - 1 in must_surface or not ops or ops[-1][0] != "belt" or ops[-1][2] != d:
            return None
        ent_tile = ops[-1][1]
        last_occ, j = i, i
        while j < n and (j - (i - 1)) <= ug_max:
            if seq[j][0] in occ:
                last_occ = j
            j += 1
        exit_i = last_occ + 1
        if (exit_i >= n or seq[exit_i][0] in occ or (exit_i - (i - 1)) > ug_max
                or any(k in must_surface for k in range(i, exit_i + 1))):
            return None
        exit_tile = seq[exit_i][0]
        ops[-1] = ("ug_in", ent_tile, d)
        bx, by = ent_tile
        while (bx, by) != exit_tile:          # buried tiles (incl. any free gaps)
            bx += DIR_DELTA[d][0]; by += DIR_DELTA[d][1]
            used.add((bx, by))
        ops.append(("ug_out", exit_tile, d)); used.add(exit_tile)
        i = exit_i + 1
    # commit
    for kind, tile, d in ops:
        if pipe:
            if kind == "belt":
                layout.add(PlacedEntity(PIPE, tile[0], tile[1], meta=meta))
            else:
                layout.add(PlacedEntity(PIPE_TO_GROUND, tile[0], tile[1],
                                        direction=OPPOSITE[d] if kind == "ug_in" else d, meta=meta))
        elif kind == "belt":
            layout.add(PlacedEntity(BELT, tile[0], tile[1], direction=d, meta=meta))
        else:
            layout.add(PlacedEntity(UNDERGROUND, tile[0], tile[1], direction=d,
                                    ug_type="input" if kind == "ug_in" else "output", meta=meta))
    occ |= used
    return used


def _input_slots(body):
    """Candidate input ports around a body: (anchor_tile, inserter_tile, inserter_dir).
    Channel risers come from the south channel, so prefer WEST rows BOTTOM-first and the
    SOUTH face (reachable from below without crossing the body or a direct belt); NORTH and
    EAST are last resorts."""
    bx, by = body.x, body.y
    bw, bh = body.size
    slots = []
    for r in reversed(range(bh)):                              # west, bottom row first
        slots.append(((bx - 2, by + r), (bx - 1, by + r), WEST))
    for c in range(bw):                                        # south (anchor below body)
        slots.append(((bx + c, by + bh + 1), (bx + c, by + bh), SOUTH))
    for c in range(bw):                                        # north (last resort)
        slots.append(((bx + c, by - 2), (bx + c, by - 1), NORTH))
    for r in range(bh):                                        # east
        slots.append(((bx + bw + 1, by + r), (bx + bw, by + r), EAST))
    return slots


def _output_drop(body):
    """The output port on a body's east face: (drop_tile, inserter_tile)."""
    bx, by = body.x, body.y
    bw, bh = body.size
    r = bh // 2
    return ((bx + bw + 1, by + r), (bx + bw, by + r))


# ---------------------------------------------------------------------------
# Pass 4 + orchestration.
# ---------------------------------------------------------------------------
def compile_graph(graph: Graph) -> Layout:
    """Generate a candidate :class:`Layout` (the v2 reference generator)."""
    col = _layers(graph)
    cr, order = _assign_rows(graph, col)
    fluid_sinks = {e.dst for e in graph.edges if e.fluid}
    item_edges = [e for e in graph.edges if not e.fluid]
    cmax = max(col.values()) if col else 0

    cols: dict[int, list[str]] = {c: [] for c in range(cmax + 1)}
    for n in graph.nodes:
        cols[col[n]].append(n)
    for c in cols:
        cols[c].sort(key=lambda n: cr[n])

    # DIRECT edges: a consumer in the next column at the SAME center row is wired by a
    # straight horizontal belt (no channel detour) -- this keeps chains/spines tight.
    # (1x1 producers qualify only when single-consumer, so their lone east tile isn't
    # claimed by both a direct belt and a channel feed.)
    out_count: dict[str, int] = {}
    for e in item_edges:
        out_count[e.src] = out_count.get(e.src, 0) + 1

    def _is_direct(e):
        if col[e.dst] != col[e.src] + 1 or cr[e.dst] != cr[e.src]:
            return False
        return SIZE[_node_proto(graph, e.src, fluid_sinks)][0] == 3 or out_count[e.src] == 1
    direct_set = {(e.src, e.dst) for e in item_edges if _is_direct(e)}
    ch_edges = [e for e in item_edges if (e.src, e.dst) not in direct_set]

    # adaptive gutter widths from the number of CHANNEL vertical lines crossing each gutter
    _prod_set = {e.src for e in ch_edges}
    producers = [n for n in graph.nodes if n in _prod_set]   # deterministic node order
    nvert = {g: 0 for g in range(cmax + 1)}
    for p in producers:
        nvert[col[p]] = nvert.get(col[p], 0) + 1                  # feed east of producer
    for e in ch_edges:
        g = col[e.dst] - 1
        if g >= 0:
            nvert[g] = nvert.get(g, 0) + 1                        # riser west of consumer
    gutter = {g: max(6, nvert.get(g, 0) + 5) for g in range(cmax + 1)}
    Xcol = {0: 0}
    for c in range(1, cmax + 1):
        Xcol[c] = Xcol[c - 1] + 3 + gutter[c - 1]

    # place bodies LEFT-aligned in uniform 3-wide cells -> west face is at Xcol[c]-1/-2
    # for every body (1x1 or 3x3), so input geometry is uniform.
    bodies: dict[str, PlacedEntity] = {}
    layout = Layout()
    for c in cols:
        for n in cols[c]:
            proto = _node_proto(graph, n, fluid_sinks)
            bw, bh = SIZE[proto]
            bx = Xcol[c]
            by = cr[n] - 1 if bh == 3 else cr[n]
            node = graph.nodes[n]
            bodies[n] = layout.add(PlacedEntity(
                proto, bx, by,
                recipe=node.recipe if node.kind in _RECIPE_KINDS else None,
                item=node.item, meta={"node": n}))
    occ: set[tuple[int, int]] = set()
    for b in bodies.values():
        occ |= set(b.tiles())

    # reserve fluid-box external tiles of fluid-active bodies so item inserters avoid them
    # (a stray belt/inserter on a box would weld or block a fluid network).
    fluid_nodes = ({e.src for e in graph.edges if e.fluid}
                   | {e.dst for e in graph.edges if e.fluid})
    for name, b in bodies.items():
        if b.proto in (CHEMICAL, FLUID_SOURCE, TANK) or name in fluid_nodes:
            for tile, _flow in _fluid_connections(b.proto, b.x, b.y, b.direction):
                occ.add(tile)

    band_bot = max((b.y + b.size[1] - 1 for b in bodies.values()), default=0)

    used_ins: dict[str, set] = {n: set() for n in bodies}

    # DIRECT edges: straight horizontal belt at the shared center row.
    direct_belts = []
    for e in item_edges:
        if (e.src, e.dst) not in direct_set:
            continue
        P, C = bodies[e.src], bodies[e.dst]
        r = cr[e.src]
        oins, odrop = (P.x + P.size[0], r), (P.x + P.size[0] + 1, r)
        iins, ianch = (C.x - 1, r), (C.x - 2, r)
        layout.add(PlacedEntity(INSERTER, oins[0], oins[1], direction=WEST,
                                meta={"role": "out", "src": e.src}))
        layout.add(PlacedEntity(INSERTER, iins[0], iins[1], direction=WEST,
                                meta={"role": "in", "edge": (e.src, e.dst)}))
        occ.add(oins)
        occ.add(iins)
        used_ins[e.dst].add(iins)
        direct_belts.append((odrop, ianch, (e.src, e.dst)))
    for odrop, ianch, edge in direct_belts:
        occ.discard(odrop)
        occ.discard(ianch)
        if _lay_polyline(layout, occ, [odrop, ianch], {"role": "direct", "edge": edge}) is not None:
            continue
        lb = (min(odrop[0], ianch[0]) - 4, max(odrop[0], ianch[0]) + 4,    # reroute around any
              min(odrop[1], ianch[1]) - 6, max(odrop[1], ianch[1]) + 6)    # obstacle (e.g. a pipe)
        path = _pipe_path(occ, {odrop}, ianch, lb, max_gap=UG_MAX_GAP)
        if path and _lay_belt_path(layout, occ, path, {"role": "direct", "edge": edge}):
            continue
        oi, ii = (odrop[0] - 1, odrop[1]), (ianch[0] + 1, ianch[1])        # non-fatal: drop the
        layout.entities = [e for e in layout.entities                     # two inserters (lane
                           if not (e.proto == INSERTER and (e.x, e.y) in (oi, ii))]  # left unrouted)
        occ.discard(oi)
        occ.discard(ii)
        used_ins[edge[1]].discard(ii)

    # channel output inserters (only producers with channel edges), on a free east tile
    out_drop: dict[str, tuple[int, int]] = {}
    for p in producers:
        b = bodies[p]
        bw, bh = b.size
        rows = [bh // 2 + 1, bh // 2, bh // 2 - 1, bh - 1, 0] if bh == 3 else [0]
        for r in rows:
            ins, drop = (b.x + bw, b.y + r), (b.x + bw + 1, b.y + r)
            if ins not in occ and drop not in occ and 0 <= r < bh:
                layout.add(PlacedEntity(INSERTER, ins[0], ins[1], direction=WEST,
                                        meta={"role": "out", "src": p}))
                occ.add(ins)
                occ.add(drop)
                out_drop[p] = drop
                break
        else:
            raise LayoutError(f"no free output port on {p!r}")

    # assign a DISTINCT vertical column (vx) in each gutter to every feed and riser. Avoid
    # columns holding inserters and the west-face access columns of channel consumers
    # (inserter + anchor + buffer) -- the actual input port is chosen later, riser-aware.
    blocked_cols: dict[int, set] = {g: set() for g in range(cmax + 1)}

    def gutter_of(x):
        for g in range(cmax):
            if Xcol[g] + 3 <= x <= Xcol[g + 1] - 1:
                return g
        return None
    for e in layout.entities:
        if e.proto in (INSERTER, LONG_INSERTER):
            g = gutter_of(e.x)
            if g is not None:
                blocked_cols[g].add(e.x)
    for c in {e.dst for e in ch_edges}:                # west access cols of channel consumers
        b = bodies[c]
        for x in (b.x - 1, b.x - 2, b.x - 3):
            g = gutter_of(x)
            if g is not None:
                blocked_cols[g].add(x)

    # PLANAR vertical-lane assignment so feeds/risers never cross each other in the band
    # (only channel trunks get crossed -> clean dives). Feeds occupy the WEST of the
    # gutter, risers the EAST. Feeds: topmost producer -> eastmost (its long jog clears
    # lower, shorter verticals). Risers: topmost consumer -> westmost (its long approach
    # clears lower verticals that don't reach its row). See docs/V2_DESIGN.md.
    vx_feed: dict[str, int] = {}
    vx_riser: dict[tuple[str, str], int] = {}
    for g in range(cmax + 1):
        feeds = sorted((p for p in producers if col[p] == g), key=lambda p: out_drop[p][1])
        risers = sorted((e for e in ch_edges if col[e.dst] - 1 == g),
                        key=lambda e: cr[e.dst])
        hi = (Xcol[g + 1] if g < cmax else Xcol[g] + 3 + gutter[g])
        avail = [x for x in range(Xcol[g] + 3, hi - 1) if x not in blocked_cols[g]]
        if len(avail) < len(feeds) + len(risers):
            raise LayoutError(f"gutter {g} too narrow for {len(feeds) + len(risers)} lanes")
        west = avail[:len(feeds)]
        east = avail[len(feeds):len(feeds) + len(risers)]
        for i, p in enumerate(feeds):                  # top producer -> eastmost of west block
            vx_feed[p] = west[len(west) - 1 - i]
        for i, e in enumerate(risers):                 # top consumer -> westmost of east block
            vx_riser[(e.src, e.dst)] = east[i]

    consumers_of: dict[str, list] = {}
    for e in ch_edges:
        consumers_of.setdefault(e.src, []).append(e)

    # CHANNEL-ROW COLORING: a trunk spans [min..max] of its feed+riser columns. Trunks
    # whose x-spans don't overlap SHARE a channel row -> the channel collapses from 3 rows
    # per producer to 3 * (chromatic number), hugely shrinking wide/reconvergent layouts.
    span = {}
    for p in producers:
        xs = [vx_riser[(p, e.dst)] for e in consumers_of[p]] + [vx_feed[p]]
        span[p] = (min(xs), max(xs))
    Rp: dict[str, int] = {}
    row_end: list[int] = []                            # rightmost x occupied in each row
    for p in sorted(producers, key=lambda n: span[n][0]):
        lo, hi = span[p]
        for k in range(len(row_end)):
            if row_end[k] < lo - 1:                    # free row (1-tile gap) -> reuse
                Rp[p] = band_bot + 2 + 3 * k
                row_end[k] = hi
                break
        else:
            Rp[p] = band_bot + 2 + 3 * len(row_end)
            row_end.append(hi)

    # reserve every riser's tap + start tile up front so risers/feeds don't clobber each
    # other's connection corridor (two lanes into the same node would otherwise collide).
    for e in ch_edges:
        rx = vx_riser[(e.src, e.dst)]
        occ.add((rx, Rp[e.src] - 1))
        occ.add((rx, Rp[e.src] - 2))

    # --- emit, diving discipline by phase order: trunks, feeds, risers ---
    # 1) trunks (one horizontal lane per producer). The terminal TURNS UP into its last consumer's
    # riser -- a plain belt corner that feeds the machine directly, so that consumer needs NO tap
    # inserter (the cleanest shape). We also keep the tile BELOW the terminal reserved, so the
    # occupancy is byte-identical to the south-turn variant -> all downstream routing is unchanged
    # (zero regression). If the up-turn can't lay, fall back to a SOUTH turn (tapped) into an empty
    # reserved tile -- never a protruding dead-end stub.
    inline_last: dict[str, tuple] = {}
    for p in producers:
        xs = [vx_riser[(p, e.dst)] for e in consumers_of[p]] + [vx_feed[p]]
        x0, x1 = min(xs), max(xs)
        le = max(consumers_of[p], key=lambda e: vx_riser[(p, e.dst)])
        if vx_riser[(p, le.dst)] == x1:                # turn UP into the last consumer's riser
            occ.discard((x1, Rp[p] - 1))               # the up-turn belt takes the tap tile
            if _lay_polyline(layout, occ, [(x0, Rp[p]), (x1, Rp[p]), (x1, Rp[p] - 1)],
                             {"role": "trunk", "src": p}) is not None:
                occ.add((x1, Rp[p] + 1))               # reserve the south tile too (occupancy
                inline_last[p] = (le.src, le.dst)      # match) so downstream routing is identical
                continue
            occ.add((x1, Rp[p] - 1))                   # up-turn unplaceable -> restore, south-turn
        if _lay_polyline(layout, occ, [(x0, Rp[p]), (x1, Rp[p]), (x1, Rp[p] + 1)],
                         {"role": "trunk", "src": p}) is not None:
            # drop the tail BELT but KEEP its tile reserved -> downstream routing byte-identical
            # (zero regression); only the dead-end stub belt is gone.
            layout.entities = [e for e in layout.entities
                               if not (e.proto == BELT and (e.x, e.y) == (x1, Rp[p] + 1)
                                       and e.meta.get("src") == p)]
        else:
            _lay_polyline(layout, occ, [(x0, Rp[p]), (x1, Rp[p])], {"role": "trunk", "src": p})
    # BFS reroute bounds (feeds + risers): the whole plane around the build.
    bx_max = max(b.x + b.size[0] for b in bodies.values())
    by_min = min(b.y for b in bodies.values())
    bfs_bounds = (-2, bx_max + 4, by_min - 8, band_bot + 3 * len(row_end) + 6)
    # 2) feeds (producer body -> its trunk): straight jog to the feed column, else BFS reroute
    # (diving under any pipe/belt), else non-fatal -- drop the out-inserter (lanes left unrouted).
    for p in producers:
        drop = out_drop[p]
        occ.discard(drop)
        fx = vx_feed[p]
        target = (fx, Rp[p] - 1)
        occ.discard(target)
        pts = [drop, (fx, drop[1]), target] if fx != drop[0] else [drop, target]
        if _lay_polyline(layout, occ, pts, {"role": "feed", "src": p}) is not None:
            continue
        path = _pipe_path(occ, {drop}, target, bfs_bounds, max_gap=UG_MAX_GAP)
        if path and _lay_belt_path(layout, occ, path, {"role": "feed", "src": p}):
            continue
        oi = (drop[0] - 1, drop[1])
        layout.entities = [e for e in layout.entities if not (e.proto == INSERTER and (e.x, e.y) == oi)]
        occ.discard(oi)
    # 3) risers: tap the producer trunk, then route up to a consumer input port. Try the
    # candidate ports (west bottom-first, then south/north/east) and use the FIRST whose
    # riser lays cleanly -- so a riser never has to cross a direct belt right at its corner.
    for e in ch_edges:
        p, c = e.src, e.dst
        rx = vx_riser[(p, c)]
        tap, start = (rx, Rp[p] - 1), (rx, Rp[p] - 2)
        inline = inline_last.get(p) == (p, c)          # trunk already turned up -> no tap inserter
        occ.discard(start)                             # free our reserved start for the riser
        cand = [(a, i, d) for a, i, d in _input_slots(bodies[c])
                if i not in occ and i not in used_ins[c] and a not in occ]
        reserved = {i for _, i, _ in cand}             # keep risers off the inserter tiles
        occ |= reserved
        laid = None
        for anchor, ins, d in cand:                    # (1) fast deterministic L-route
            occ.discard(anchor)
            pts = [start, anchor] if rx == anchor[0] else [start, (rx, anchor[1]), anchor]
            if _lay_polyline(layout, occ, pts, {"role": "riser", "edge": (p, c)}) is not None:
                laid = (ins, d)
                break
            occ.add(anchor)
        if laid is None:                               # (2) bounded BFS via the open north
            for anchor, ins, d in cand:
                occ.discard(anchor)
                path = _pipe_path(occ, {start}, anchor, bfs_bounds, max_gap=UG_MAX_GAP)
                if path and _lay_belt_path(layout, occ, path, {"role": "riser", "edge": (p, c)}):
                    laid = (ins, d)
                    break
                occ.add(anchor)
        occ -= (reserved - ({laid[0]} if laid else set()))   # free unused inserter tiles
        if laid is not None:                           # connect: input inserter (+ tap unless inline)
            ins, d = laid
            if not inline:                             # inline = trunk flows straight up, no tap
                layout.add(PlacedEntity(INSERTER, tap[0], tap[1], direction=SOUTH,
                                        meta={"role": "tap", "edge": (p, c)}))
                occ.add(tap)
            layout.add(PlacedEntity(INSERTER, ins[0], ins[1], direction=d,
                                    meta={"role": "in", "edge": (p, c)}))
            occ.add(ins)
            used_ins[c].add(ins)
        else:
            occ.add(start)                             # leave lane unrouted (verifier reports it)

    _emit_fluids(graph, layout, bodies, occ)
    return layout


# ---------------------------------------------------------------------------
# Fluids: a small BFS pipe router (fluids are sparse -> no congestion/rip-up).
# ---------------------------------------------------------------------------
def _pipe_path(occ, starts, goal, bounds, max_gap=PIPE_UG_GAP, step_goal=False):
    """BFS a tile path from any tile in ``starts`` to ``goal``, stepping to free tiles or
    JUMPING over an occupied run (<= max_gap) to a free tile. The first move out of a start
    tile is always adjacent. With ``step_goal=True`` a tunnel may NOT land on the goal -- the
    goal must be reached by a plain step from a free neighbour (so a pipe link arriving at a
    fluid box connects 4-adjacently to the box's plain pipe rather than tunnelling onto it,
    which the interior-only layer can't realise). Used for pipes (max_gap=PIPE_UG_GAP) and as a
    last-resort belt riser router (max_gap=UG_MAX_GAP). Returns the tile path [start..goal]."""
    from collections import deque
    lo_x, hi_x, lo_y, hi_y = bounds
    starts = set(starts)

    def free(t):
        return lo_x <= t[0] <= hi_x and lo_y <= t[1] <= hi_y and (t not in occ or t == goal)

    q = deque(starts)
    prev = {s: (None, None, None) for s in starts}       # tile -> (parent, dir, via)
    while q:
        cur = q.popleft()
        if cur == goal:
            path = [cur]
            while prev[path[-1]][0] is not None:
                path.append(prev[path[-1]][0])
            path.reverse()
            return path
        _, came_dir, came_via = prev[cur]
        for d in (EAST, SOUTH, NORTH, WEST):
            # after a tunnel, the exit can't turn -> first move out must be straight
            if came_via == "jump" and d != came_dir:
                continue
            dx, dy = DIR_DELTA[d]
            nb = (cur[0] + dx, cur[1] + dy)
            if nb not in prev and free(nb):
                prev[nb] = (cur, d, "step")
                q.append(nb)
            # tunnel over an occupied run, but ONLY when arriving straight AND not right
            # after another tunnel (so each tile has a single role: entrance OR exit).
            elif nb not in prev and nb in occ and nb != goal and came_dir == d and came_via != "jump":
                m = 2
                while m <= max_gap:
                    ex = (cur[0] + dx * m, cur[1] + dy * m)
                    if free(ex):
                        if ex not in prev and not (step_goal and ex == goal):
                            prev[ex] = (cur, d, "jump")
                            q.append(ex)
                        break
                    m += 1
    return None


def _lay_belt_path(layout, occ, path, meta, pipe=False):
    """Lay a BFS tile path (from _pipe_path) DIRECTLY as belts (or pipes). Adjacent tiles ->
    belt/pipe; a gap (a tunnel jump) -> underground/pipe-to-ground entrance+exit. The BFS
    guarantees a tunnel is entered/left straight and never abuts another, so every tile has
    one role. Returns the set of tiles used, or None if an endpoint is occupied."""
    if path[0] in occ or path[-1] in occ:
        return None
    n = len(path)
    if n == 1:                                          # single tile: one belt/pipe
        t = path[0]
        layout.add(PlacedEntity(PIPE if pipe else BELT, t[0], t[1],
                                direction=None if pipe else EAST, meta=meta))
        occ.add(t)
        return {t}
    dirs = [delta_to_dir(_sign(path[i + 1][0] - path[i][0]),
                         _sign(path[i + 1][1] - path[i][1])) for i in range(n - 1)]
    placed, used = [], set()
    for i, t in enumerate(path):
        d_out = dirs[i] if i < n - 1 else dirs[-1]
        out_gap = i < n - 1 and (abs(path[i + 1][0] - t[0]) + abs(path[i + 1][1] - t[1])) > 1
        in_gap = i > 0 and (abs(t[0] - path[i - 1][0]) + abs(t[1] - path[i - 1][1])) > 1
        if out_gap:
            placed.append(("ug_in", t, d_out)); used.add(t)
            a = t
            while a != path[i + 1]:                    # buried tiles
                a = (a[0] + DIR_DELTA[d_out][0], a[1] + DIR_DELTA[d_out][1])
                if a != path[i + 1]:
                    used.add(a)
        elif in_gap:
            placed.append(("ug_out", t, dirs[i - 1])); used.add(t)
        else:
            placed.append(("belt", t, d_out)); used.add(t)
    for kind, t, d in placed:
        if pipe:
            if kind == "belt":
                layout.add(PlacedEntity(PIPE, t[0], t[1], meta=meta))
            else:                                       # p2g: direction = OPEN mouth
                layout.add(PlacedEntity(PIPE_TO_GROUND, t[0], t[1],
                                        direction=OPPOSITE[d] if kind == "ug_in" else d, meta=meta))
        elif kind == "belt":
            layout.add(PlacedEntity(BELT, t[0], t[1], direction=d, meta=meta))
        else:
            layout.add(PlacedEntity(UNDERGROUND, t[0], t[1], direction=d,
                                    ug_type="input" if kind == "ug_in" else "output", meta=meta))
    occ |= used
    return used


def _waypoints(path):
    """Collapse a tile path to corner waypoints (where direction changes)."""
    if len(path) <= 2:
        return list(path)
    wp = [path[0]]
    for i in range(1, len(path) - 1):
        if (path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]) != \
           (path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1]):
            wp.append(path[i])
    wp.append(path[-1])
    return wp


def _emit_fluids(graph, layout, bodies, occ):
    """Place pipe networks for fluid lanes. One network per fluid SOURCE: its output box is
    attached, then each consumer's input box is attached and linked into the network with a
    mostly-UNDERGROUND pipe run (tunnels straight under the belt field, saving space)."""
    fluid_edges = [e for e in graph.edges if e.fluid]
    if not fluid_edges:
        return
    by_src: dict[str, list] = {}
    for e in fluid_edges:
        by_src.setdefault(e.src, []).append(e.dst)
    box_used = {n: set() for n in bodies}

    def pick_box(name, want):
        b = bodies[name]
        for tile, flow, mdir in _fluid_connections(b.proto, b.x, b.y, b.direction, with_dir=True):
            if tile in box_used[name] or (flow != want and flow != "both"):
                continue
            box_used[name].add(tile)
            return tile, mdir
        return None

    xs = [t[0] for e in layout.entities for t in e.tiles()]
    ys = [t[1] for e in layout.entities for t in e.tiles()]
    bounds = (min(xs) - 12, max(xs) + 12, min(ys) - 12, max(ys) + 12)

    # Block every fluid-box tile so a pipe never routes ONTO an unused box (which would weld a
    # phantom fluid connection in-game). Phase 1 below unblocks the boxes it actually uses.
    fluid_ep = {e.src for e in fluid_edges} | {e.dst for e in fluid_edges}
    for name, b in bodies.items():
        if b.proto == ASSEMBLER and name not in fluid_ep:
            continue                                          # solid-recipe assembler: no boxes
        for tile, _flow, _md in _fluid_connections(b.proto, b.x, b.y, b.direction, with_dir=True):
            occ.add(tile)

    placed_surface: dict[str, set] = {}                       # src -> its surface pipe tiles
    seed: dict[str, tuple] = {}                               # src -> source box tile (net seed)
    cons_boxes: dict[str, list] = {}                          # src -> [(box, mdir) | None]
    order = [n for n in graph.nodes if n in by_src]            # deterministic

    # PHASE 1: place a plain pipe on EVERY fluid box (source + consumers) before routing any link,
    # so the isolation in phase 2 sees all box pipes and links never weld a foreign box (the MIX
    # bug). A plain pipe on a box connects to its machine 4-adjacently.
    for src in order:
        sb = pick_box(src, "output")
        if sb is None:
            continue
        occ.discard(sb[0])
        layout.add(PlacedEntity(PIPE, sb[0][0], sb[0][1], meta={"role": "pipe", "net": src}))
        occ.add(sb[0])
        seed[src] = sb[0]
        cbs, surf = [], {sb[0]}
        for d in by_src[src]:
            db = pick_box(d, "input")
            cbs.append(db)
            if db is not None:
                occ.discard(db[0])
                layout.add(PlacedEntity(PIPE, db[0][0], db[0][1], meta={"role": "pipe", "net": src}))
                occ.add(db[0])
                surf.add(db[0])
        cons_boxes[src] = cbs
        placed_surface[src] = surf

    def _lay_interior(path):
        """Lay path[1:-1] (endpoints already pipes); True if it connects to both ends."""
        if len(path) < 3:
            return True                                       # endpoints already 4-adjacent
        return _lay_belt_path(layout, occ, path[1:-1], {"role": "pipe", "net": cur_src}, pipe=True) is not None

    # PHASE 2: link each consumer box into its network, kept >=1 tile from every OTHER network's
    # pipes so fluids never weld. First try to STEP the run onto the box's plain pipe; if no step-
    # route exists (the box is walled in by belts), REPLACE the box pipe with a pipe-to-ground
    # facing the machine and route to its tunnel EXIT -- the link then tunnels under the belt
    # field straight into the box.
    for src in order:
        if src not in seed:
            continue
        cur_src = src
        avoid = {(t[0] + dx, t[1] + dy)
                 for o, tiles in placed_surface.items() if o != src
                 for t in tiles for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
        avoid -= occ
        occ |= avoid
        before = len(layout.entities)
        net = {seed[src]}
        for cb in cons_boxes[src]:
            if cb is None:
                continue
            db, mdir = cb
            path = _pipe_path(occ, net, db, bounds, step_goal=True)        # (a) step onto box
            if path is not None and _lay_interior(path):
                net |= set(path)
                continue
            # (b) tunnel INTO the box: swap its plain pipe for a p2g facing the machine + an exit
            box_pipe = next((e for e in layout.entities if e.proto == PIPE and (e.x, e.y) == db
                             and e.meta.get("net") == src), None)
            if box_pipe is not None:
                layout.entities.remove(box_pipe)
            occ.discard(db)
            dx, dy = DIR_DELTA[OPPOSITE[mdir]]
            laid = False
            for m in range(2, PIPE_UG_GAP + 1):
                ex = (db[0] + dx * m, db[1] + dy * m)
                if ex in occ:
                    continue
                path = _pipe_path(occ, net, ex, bounds, step_goal=True)
                if path is None:
                    continue
                bp = layout.add(PlacedEntity(PIPE_TO_GROUND, db[0], db[1], direction=mdir,
                                             meta={"role": "pipe", "net": src}))      # mouth->machine
                xp = layout.add(PlacedEntity(PIPE_TO_GROUND, ex[0], ex[1], direction=OPPOSITE[mdir],
                                             meta={"role": "pipe", "net": src}))       # mouth->net
                occ.add(db)
                occ.add(ex)
                for k in range(1, m):
                    occ.add((db[0] + dx * k, db[1] + dy * k))
                if _lay_interior(path):
                    net |= set(path) | {db, ex}
                    laid = True
                    break
                layout.entities.remove(bp)
                layout.entities.remove(xp)
            if not laid:
                layout.add(PlacedEntity(PIPE, db[0], db[1], meta={"role": "pipe", "net": src}))
                occ.add(db)                                    # restore plain pipe (machine still fed)
        placed_surface[src] |= {(e.x, e.y) for e in layout.entities[before:]
                                if e.proto in (PIPE, PIPE_TO_GROUND)}
        occ -= avoid
