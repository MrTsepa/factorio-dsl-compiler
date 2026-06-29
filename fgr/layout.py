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


def _fluid_connections(proto, x, y, direction):
    """External pipe tiles for an entity's fluid boxes, rotated by ``direction``: a list
    of (tile, flow) where flow is 'input'|'output'|'both' and tile is where a pipe must
    sit to attach to that box."""
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
    def half(n):                                   # half-height (clearance each side)
        h = SIZE[_node_proto(graph, n, fluid_sinks)][1]
        return h // 2

    cr: dict[str, int] = {}
    for c in sorted(cols):
        used: list[tuple[int, int]] = []           # occupied [lo,hi] center-spans this column
        def free(center, hh):
            lo, hi = center - hh - 1, center + hh + 1   # +1 row of clearance
            return all(hi < a or lo > b for a, b in used)
        for n in cols[c]:
            hh = half(n)
            pp = _primary_pred(graph, n, col)
            want = cr[pp] if pp in cr else 0
            r = want
            for off in range(0, 400):              # search outward from desired row
                if free(want + off, hh):
                    r = want + off; break
                if free(want - off, hh):
                    r = want - off; break
            cr[n] = r
            used.append((r - hh - 1, r + hh + 1))
    return cr, order


# ---------------------------------------------------------------------------
# Geometry: lay a belt/pipe polyline, diving (underground) over occupied tiles.
# ---------------------------------------------------------------------------
def _sign(v):
    return (v > 0) - (v < 0)


def _expand(pts):
    """Polyline waypoints -> ordered list of (tile, dir_to_next). Straight axis-aligned
    segments only. The last tile inherits the previous direction."""
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
    West, then south, north, east (east last; usually the output side)."""
    bx, by = body.x, body.y
    bw, bh = body.size
    slots = []
    for r in range(bh):
        slots.append(((bx - 2, by + r), (bx - 1, by + r), WEST))
    for c in range(bw):
        slots.append(((bx + c, by + bh + 1), (bx + c, by + bh), SOUTH))
    for c in range(bw):
        slots.append(((bx + c, by - 2), (bx + c, by - 1), NORTH))
    for r in range(bh):
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

    # adaptive gutter widths from the number of vertical lines crossing each gutter
    _prod_set = {e.src for e in item_edges}
    producers = [n for n in graph.nodes if n in _prod_set]   # deterministic node order
    nvert = {g: 0 for g in range(cmax + 1)}
    for p in producers:
        nvert[col[p]] = nvert.get(col[p], 0) + 1                  # feed east of producer
    for e in item_edges:
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

    band_bot = max((b.y + b.size[1] - 1 for b in bodies.values()), default=0)
    Rp = {p: band_bot + 2 + 3 * i      # spacing 3: 2 free rows between trunks -> clean dives
          for i, p in enumerate(sorted(producers, key=lambda n: (col[n], cr[n])))}

    # output inserters (east face) -> feed drop tiles (reserve drops so later lanes
    # don't clobber a producer's feed start)
    out_drop: dict[str, tuple[int, int]] = {}
    for p in producers:
        drop, ins = _output_drop(bodies[p])
        layout.add(PlacedEntity(INSERTER, ins[0], ins[1], direction=WEST,
                                meta={"role": "out", "src": p}))
        occ.add(ins)
        occ.add(drop)
        out_drop[p] = drop

    # input inserters (allocate a free port per item edge), record anchor tiles
    in_anchor: dict[tuple[str, str], tuple[int, int]] = {}
    used_ins: dict[str, set] = {n: set() for n in bodies}
    anchors: set[tuple[int, int]] = set()
    for e in item_edges:
        for anchor, ins, d in _input_slots(bodies[e.dst]):
            if ins in occ or ins in used_ins[e.dst] or anchor in occ or anchor in anchors:
                continue
            layout.add(PlacedEntity(INSERTER, ins[0], ins[1], direction=d,
                                    meta={"role": "in", "edge": (e.src, e.dst)}))
            occ.add(ins)
            used_ins[e.dst].add(ins)
            in_anchor[(e.src, e.dst)] = anchor
            anchors.add(anchor)
            break
        else:
            raise LayoutError(f"no free input port on {e.dst!r} for lane {e.src}->{e.dst}")

    # assign a DISTINCT vertical column (vx) in each gutter to every feed and riser, so
    # stacked producers / multi-input consumers never share a column. Avoid columns that
    # hold inserters or are reserved as west-face anchors.
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
    for a in anchors:                                  # reserve anchor columns + a free
        g = gutter_of(a[0])                            # buffer west of each so a dive
        if g is not None:                              # never has to exit onto the anchor
            blocked_cols[g].add(a[0])
            blocked_cols[g].add(a[0] - 1)

    # PLANAR vertical-lane assignment so feeds/risers never cross each other in the band
    # (only channel trunks get crossed -> clean dives). Feeds occupy the WEST of the
    # gutter, risers the EAST. Feeds: topmost producer -> eastmost (its long jog clears
    # lower, shorter verticals). Risers: topmost consumer -> westmost (its long approach
    # clears lower verticals that don't reach its row). See docs/V2_DESIGN.md.
    vx_feed: dict[str, int] = {}
    vx_riser: dict[tuple[str, str], int] = {}
    for g in range(cmax + 1):
        feeds = sorted((p for p in producers if col[p] == g), key=lambda p: out_drop[p][1])
        risers = sorted((e for e in item_edges if col[e.dst] - 1 == g),
                        key=lambda e: in_anchor[(e.src, e.dst)][1])
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
    for e in item_edges:
        consumers_of.setdefault(e.src, []).append(e)

    # --- emit, diving discipline by phase order: trunks, feeds, risers ---
    # 1) trunks (horizontal lanes, one per producer). End each with a 1-tile SOUTH tail
    # into the (free) row below so the terminal belt never faces a foreign lane east of it
    # (which would weld a spurious lane). Rows Rp+1/Rp+2 are free (trunk spacing 3).
    for p in producers:
        xs = [vx_riser[(p, e.dst)] for e in consumers_of[p]] + [vx_feed[p]]
        x0, x1 = min(xs), max(xs)
        pts = [(x0, Rp[p]), (x1, Rp[p]), (x1, Rp[p] + 1)]
        if not _lay_polyline(layout, occ, pts, {"role": "trunk", "src": p}):
            _lay_polyline(layout, occ, [(x0, Rp[p]), (x1, Rp[p])], {"role": "trunk", "src": p})
    # 2) feeds (producer body -> its trunk), jogging to the feed column then diving down
    for p in producers:
        drop = out_drop[p]
        occ.discard(drop)
        fx = vx_feed[p]
        pts = [drop, (fx, drop[1]), (fx, Rp[p] - 1)] if fx != drop[0] else [drop, (fx, Rp[p] - 1)]
        if not _lay_polyline(layout, occ, pts, {"role": "feed", "src": p}):
            raise LayoutError(f"could not lay feed for {p!r}")
    # 3) risers + tap inserters
    for e in item_edges:
        p, c = e.src, e.dst
        rx = vx_riser[(p, c)]
        layout.add(PlacedEntity(INSERTER, rx, Rp[p] - 1, direction=SOUTH,
                                meta={"role": "tap", "edge": (p, c)}))
        occ.add((rx, Rp[p] - 1))
        anchor = in_anchor[(p, c)]
        anchors.discard(anchor)
        occ.discard(anchor)
        start = (rx, Rp[p] - 2)
        pts = [start, anchor] if rx == anchor[0] else [start, (rx, anchor[1]), anchor]
        if not _lay_polyline(layout, occ, pts, {"role": "riser", "edge": (p, c)}):
            raise LayoutError(f"could not lay riser for lane {p}->{c}")

    return layout
