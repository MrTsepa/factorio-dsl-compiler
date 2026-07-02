"""The verifier: does a candidate layout realize the DSL spec, exactly?

This is the centerpiece of the POC. The layout *generator* is interchangeable —
deterministic placer, search, or a model — so we need an independent oracle that
grades any candidate against the requirements. The grade must be *physical*: it
is not enough that the generator claims it wired A to B; the placed inserters and
belts must actually carry items from A to B in the game.

How it works
------------
We build a directed "material-flow graph" over *carriers* (chest tiles, an
assembler's 3x3 body, and individual belt tiles) using real Factorio adjacency:

* an inserter at tile T takes an item from the tile it *faces* (T + d -- a
  blueprint inserter's `direction` points at its PICKUP, the well-known Factorio
  "inserters are stored reversed" quirk) and drops it on the opposite tile (T - d);
* a belt tile at T facing d hands its items to the transport carrier at (T + d)
  if it accepts a flow from direction d — a belt (not head-on), a splitter (only
  an aligned back feed), or an underground-belt entrance; belts can't load
  chests/assemblers, that needs an inserter (its own edge);
* a splitter is ONE carrier fed from its back tiles and pushing out both front
  tiles; an underground-belt entrance jumps to its paired exit (the nearest
  matching exit in front, within max_distance), which then pushes forward.

Then for every named node A we search this graph *without expanding through other
named nodes*, yielding the set of **direct lanes** physically present between
named nodes. The layout is correct iff that set equals the spec's edge set —
every declared lane exists, and no undeclared lane sneaks in.

Item/recipe/overlap correctness is checked separately. The flow check ignores all
generator-provided labels except the node<->body correspondence (you can't tell
two identical assemblers apart without it).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .ir import DIR_DELTA, OPPOSITE, Graph, NodeKind
from .layout import (ASSEMBLER, BELT, CHEMICAL, CHEST_INPUT, CHEST_OUTPUT, FLUID_SOURCE,
                     FURNACE, INSERTER, PIPE, PIPE_TO_GROUND, PIPE_UG_GAP, SPLITTER, TANK,
                     UG_MAX_GAP, UNDERGROUND, Layout, PlacedEntity, _fluid_connections)

# An output node is a chest for items but a storage-tank when it receives fluid.
_PROTO_FOR_KIND = {NodeKind.INPUT: CHEST_INPUT, NodeKind.OUTPUT: {CHEST_OUTPUT, TANK},
                   NodeKind.ASSEMBLER: ASSEMBLER, NodeKind.FURNACE: FURNACE,
                   NodeKind.CHEMICAL: CHEMICAL, NodeKind.FLUID: FLUID_SOURCE}
_RECIPE_KINDS = (NodeKind.ASSEMBLER, NodeKind.CHEMICAL)
_ITEM_KINDS = (NodeKind.INPUT, NodeKind.FLUID)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "error"  # "error" fails verification; "warn" is advisory


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    lanes_found: set[tuple[str, str]] = field(default_factory=set)

    def add(self, name, ok, detail="", severity="error") -> None:
        self.checks.append(Check(name, ok, detail, severity))

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.severity == "error")

    def format(self) -> str:
        lines = []
        for c in self.checks:
            mark = "ok  " if c.ok else ("FAIL" if c.severity == "error" else "warn")
            lines.append(f"  [{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        lines.append(f"\n  => {'PASS' if self.ok else 'FAIL'}")
        return "\n".join(lines)


def verify(graph: Graph, layout: Layout) -> Report:
    """Grade ``layout`` against ``graph``; return a structured :class:`Report`."""
    report = Report()
    occ, overlaps = _occupancy(layout)
    report.add("no overlapping entities", not overlaps,
               "" if not overlaps else f"{len(overlaps)} tile(s) double-booked: "
               f"{sorted(overlaps)[:5]}")

    bodies = _correspondence(graph, layout, report)

    # Map each occupied tile to a carrier id (what can hold/move items there):
    # bodies, belts, splitters (ONE carrier for the 2-tile entity), and the two
    # ends of an underground-belt.
    carrier_at: dict[tuple[int, int], object] = {}
    body_tiles: dict[str, list[tuple[int, int]]] = {}
    for name, ent in bodies.items():
        for t in ent.tiles():
            carrier_at[t] = ("body", name)
        body_tiles[name] = ent.tiles()
    trans_at: dict[tuple[int, int], PlacedEntity] = {}  # belt/splitter/underground tiles
    for e in layout.entities:
        if e.proto == BELT:
            carrier_at[(e.x, e.y)] = ("belt", (e.x, e.y))
            trans_at[(e.x, e.y)] = e
        elif e.proto == UNDERGROUND:
            carrier_at[(e.x, e.y)] = ("ug", (e.x, e.y))
            trans_at[(e.x, e.y)] = e
        elif e.proto == SPLITTER:
            cid = ("splitter", (e.x, e.y))
            for t in e.tiles():
                carrier_at[t] = cid
                trans_at[t] = e

    edges_out = _flow_edges(layout, carrier_at, trans_at, bodies, report)
    report.lanes_found = _direct_lanes(graph, bodies, body_tiles, edges_out)
    _compare_to_spec(graph, report)
    _check_lane_mixing(graph, layout, trans_at, bodies, report)
    _check_fluids(graph, layout, bodies, report)
    return report


def _occupancy(layout: Layout):
    occ: dict[tuple[int, int], PlacedEntity] = {}
    overlaps: set[tuple[int, int]] = set()
    for e in layout.entities:
        for t in e.tiles():
            if t in occ:
                overlaps.add(t)
            occ[t] = e
    return occ, overlaps


def _correspondence(graph: Graph, layout: Layout, report: Report) -> dict[str, PlacedEntity]:
    """Match each spec node to exactly one body entity; check proto/recipe/item."""
    by_node: dict[str, list[PlacedEntity]] = {}
    for e in layout.entities:
        n = e.meta.get("node")
        if n is not None:
            by_node.setdefault(n, []).append(e)

    bodies: dict[str, PlacedEntity] = {}
    missing, wrong = [], []
    for name, node in graph.nodes.items():
        ents = by_node.get(name, [])
        if len(ents) != 1:
            missing.append(f"{name} (found {len(ents)})")
            continue
        ent = ents[0]
        bodies[name] = ent
        allowed = _PROTO_FOR_KIND[node.kind]
        allowed = allowed if isinstance(allowed, set) else {allowed}
        if ent.proto not in allowed:
            wrong.append(f"{name}: {ent.proto} not in {sorted(allowed)}")
        if node.kind in _RECIPE_KINDS and ent.recipe != node.recipe:
            wrong.append(f"{name}: recipe {ent.recipe!r} != {node.recipe!r}")
        if node.kind in _ITEM_KINDS and ent.item != node.item:
            wrong.append(f"{name}: item {ent.item!r} != {node.item!r}")

    report.add("every node placed exactly once", not missing,
               "" if not missing else ", ".join(missing))
    report.add("node protos/recipes/items match spec", not wrong,
               "" if not wrong else "; ".join(wrong))
    return bodies


def _flow_edges(layout: Layout, carrier_at: dict, trans_at: dict, bodies: dict, report: Report) -> dict:
    """Directed carrier->carrier edges from inserters, belts, splitters, undergrounds."""
    edges: dict[object, set] = {}
    dangling = []

    def accepts(target_tile, d) -> bool:
        """Can the transport carrier at target_tile take an item flowing direction d?"""
        e = trans_at.get(target_tile)
        if e is None:
            return False  # bodies are loaded by inserters, not by belts
        td = e.direction or 0
        if e.proto == BELT:
            return td != OPPOSITE[d]          # belts accept straight/side feeds, not head-on
        if e.proto == SPLITTER:
            return d == td                     # splitters take only an aligned back feed
        if e.proto == UNDERGROUND:
            # underground ends are SIDE-LOADABLE like belts (the false-pass a render
            # audit caught: a belt dead-ending against an entrance's side does feed it
            # in-game). An entrance also takes its normal back feed; an exit's back is
            # the tunnel and its front is outgoing flow, so an exit takes sides only.
            if e.ug_type == "input":
                return td != OPPOSITE[d]
            return d not in (td, OPPOSITE[td])
        return False

    def push(src_id, target_tile, d) -> None:
        if accepts(target_tile, d):
            edges.setdefault(src_id, set()).add(carrier_at[target_tile])

    def item_carrier(cid) -> bool:
        # an inserter moves ITEMS; a fluid-only body (storage-tank, infinity-pipe) can neither
        # yield nor accept items, so an inserter touching one is non-functional in-game.
        return not (cid is not None and cid[0] == "body" and bodies[cid[1]].proto in (TANK, FLUID_SOURCE))

    for e in layout.entities:
        if e.proto == INSERTER:
            dx, dy = DIR_DELTA[e.direction]
            # an inserter's `direction` points at its PICKUP; it drops on the far side
            pick = carrier_at.get((e.x + dx, e.y + dy))
            drop = carrier_at.get((e.x - dx, e.y - dy))
            if pick is None or drop is None or not item_carrier(pick) or not item_carrier(drop):
                dangling.append((e.x, e.y))
                continue
            edges.setdefault(pick, set()).add(drop)
        elif e.proto == BELT:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            push(("belt", (e.x, e.y)), (e.x + dx, e.y + dy), d)
        elif e.proto == SPLITTER:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            cid = ("splitter", (e.x, e.y))
            for t in e.tiles():                # each cell pushes to the tile in front of it
                push(cid, (t[0] + dx, t[1] + dy), d)
        elif e.proto == UNDERGROUND:
            d = e.direction or 0
            dx, dy = DIR_DELTA[d]
            if e.ug_type == "output":
                push(("ug", (e.x, e.y)), (e.x + dx, e.y + dy), d)
            else:                              # entrance: items resurface at the paired exit
                exit_tile = _ug_exit((e.x, e.y), d, trans_at)
                if exit_tile is not None:
                    edges.setdefault(("ug", (e.x, e.y)), set()).add(("ug", exit_tile))
                else:
                    dangling.append((e.x, e.y))  # unpaired underground entrance
    report.add("no dangling inserters / unpaired undergrounds", not dangling,
               "" if not dangling else f"empty pickup/drop or unpaired underground at {dangling[:5]}")
    return edges


def _ug_exit(entrance, d, trans_at):
    """Nearest matching underground exit in front of an entrance (Factorio pairing)."""
    dx, dy = DIR_DELTA[d]
    for k in range(1, UG_MAX_GAP + 1):
        t = (entrance[0] + dx * k, entrance[1] + dy * k)
        e = trans_at.get(t)
        # The scan stops at the first SAME-AXIS underground (same tier): a same-direction
        # exit pairs; a same-direction entrance steals the pairing; an OPPOSITE-direction
        # one on the line also blocks it (in-game, same-tier opposed undergrounds interfere
        # -- the workaround is a different belt tier). Perpendicular tunnels are transparent.
        if e is not None and e.proto == UNDERGROUND and (e.direction or 0) in (d, OPPOSITE[d]):
            return t if (e.direction or 0) == d and e.ug_type == "output" else None
    return None


def _direct_lanes(graph: Graph, bodies, body_tiles, edges_out) -> set[tuple[str, str]]:
    """Physical lanes between named nodes (search stops at any other named node)."""
    body_carrier = {("body", n) for n in bodies}
    lanes: set[tuple[str, str]] = set()
    for src in bodies:
        seen = {("body", src)}
        q = deque(edges_out.get(("body", src), ()))
        for c in q:
            seen.add(c)
        while q:
            cur = q.popleft()
            if cur in body_carrier:          # reached another named node: record, don't pass through
                lanes.add((src, cur[1]))
                continue
            for nb in edges_out.get(cur, ()):
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
    return lanes


def _compare_to_spec(graph: Graph, report: Report) -> None:
    spec = {(e.src, e.dst) for e in graph.edges if not e.fluid}   # item lanes only
    found = report.lanes_found
    missing = sorted(spec - found)
    spurious = sorted(found - spec)
    report.add("every declared belt lane physically connects", not missing,
               "" if not missing else "missing lanes: " +
               ", ".join(f"{a}->{b}" for a, b in missing))
    report.add("no undeclared belt lanes", not spurious,
               "" if not spurious else "spurious lanes: " +
               ", ".join(f"{a}->{b}" for a, b in spurious))


def _check_lane_mixing(graph: Graph, layout: Layout, trans_at, bodies, report: Report) -> None:
    """A transport belt has TWO lanes (sides). Side-loading keeps two products separable
    -- one per side -- so a belt can carry at most two products; but two DIFFERENT
    products on the SAME lane mix in-game (jams, wrong ratios, starved machines).

    Track, per (tile, lane), the set of products that can reach it, using real lane
    physics: a straight feed and a CURVE (a belt whose only feed is perpendicular)
    preserve lanes; a side-load (perpendicular feed into a non-curved belt) drops BOTH
    of the feeder's lanes onto the receiver's near-side lane; an underground preserves
    lanes across the tunnel; an inserter drops on the FAR lane (both lanes,
    conservatively, when its arm is parallel to the belt). Products are tagged by the
    source node's item/recipe, so many producers of the SAME product may share a lane.
    """
    belts = {(e.x, e.y): e for e in layout.entities if e.proto == BELT}
    if not belts:
        return

    def tag_of(name):
        nd = graph.nodes.get(name)
        return (nd.item or nd.recipe or name) if nd is not None else name

    def left(d):
        dx, dy = DIR_DELTA[d]
        return (dy, -dx)                          # left of travel, y-down coordinates

    def lane_id(ent, tile):
        # splitter: one logical node (its 2 cells balance freely within a lane)
        return (ent.x, ent.y) if ent.proto == SPLITTER else tile

    # belt-connection pushes (they also define each belt's curve state)
    feeders: dict = {}
    pushes = []                                    # (src_ent, src_tile, dst_tile, flow_dir)
    for e in layout.entities:
        cells = []
        if e.proto == BELT or (e.proto == UNDERGROUND and e.ug_type == "output"):
            cells = [(e.x, e.y)]
        elif e.proto == SPLITTER:
            cells = e.tiles()
        d = e.direction or 0
        for c in cells:
            ft = (c[0] + DIR_DELTA[d][0], c[1] + DIR_DELTA[d][1])
            te = trans_at.get(ft)
            if te is None:
                continue
            td = te.direction or 0
            ok = ((te.proto == BELT and td != OPPOSITE[d])
                  or (te.proto == SPLITTER and d == td)
                  or (te.proto == UNDERGROUND
                      and (td != OPPOSITE[d] if te.ug_type == "input"
                           else d not in (td, OPPOSITE[td]))))
            if ok:
                pushes.append((e, c, ft, d))
                if te.proto == BELT:
                    feeders.setdefault(ft, []).append(d)

    def curved(tile):
        b = belts[tile]
        fs = feeders.get(tile, [])
        return len(fs) == 1 and fs[0] not in (b.direction or 0, OPPOSITE[b.direction or 0])

    edges: dict = {}                               # (lane_node, lane) -> set of (lane_node, lane)

    def link(src, sl, dst, dl):
        edges.setdefault((src, sl), set()).add((dst, dl))

    for e, c, ft, d in pushes:
        te = trans_at[ft]
        td = te.direction or 0
        src, dst = lane_id(e, c), lane_id(te, ft)
        o = (c[0] - ft[0], c[1] - ft[1])
        if te.proto == UNDERGROUND and td != d:
            # side-load onto an underground END: the tile is half belt, half hood
            # ("the half with a belt can accept input from the side; the other half
            # blocks incoming items" -- wiki). The feeder's two lanes arrive at two
            # positions ALONG the receiver's axis, so only the lane over the belt
            # half enters: the rear-side lane for an entrance (hood in front), the
            # front-side lane for an exit (hood behind). It lands on the near lane.
            dl = "L" if o == left(td) else "R"
            pass_dir = OPPOSITE[td] if te.ug_type == "input" else td
            sl = "L" if left(d) == DIR_DELTA[pass_dir] else "R"
            link(src, sl, dst, dl)
        elif te.proto == BELT and td != d and not curved(ft):
            # side-load: both feeder lanes land on the receiver's near-side lane
            dl = "L" if o == left(td) else "R"
            link(src, "L", dst, dl)
            link(src, "R", dst, dl)
        else:                                      # straight, curve, splitter, ug back-feed
            link(src, "L", dst, "L")
            link(src, "R", dst, "R")
    for e in layout.entities:                      # tunnel: entrance lanes -> paired exit
        if e.proto == UNDERGROUND and e.ug_type == "input":
            ex = _ug_exit((e.x, e.y), e.direction or 0, trans_at)
            if ex is not None:
                link((e.x, e.y), "L", ex, "L")
                link((e.x, e.y), "R", ex, "R")

    # inserters: seed from bodies, transfer belt->belt (taps), always onto the FAR lane
    name_at = {t: n for n, b in bodies.items() for t in b.tiles()}
    tags: dict = {}                                # (lane_node, lane) -> set of product tags
    work: list = []

    def add(node, lane, ts):
        cur = tags.setdefault((node, lane), set())
        if not ts <= cur:
            cur |= ts
            work.append((node, lane))

    for e in layout.entities:
        if e.proto != INSERTER:
            continue
        dx, dy = DIR_DELTA[e.direction]
        pick, drop = (e.x + dx, e.y + dy), (e.x - dx, e.y - dy)
        de = trans_at.get(drop)
        if de is None:
            continue                               # drops into a body/chest: no belt lane
        dd = de.direction or 0
        o = ((e.x) - drop[0], (e.y) - drop[1])     # from drop tile toward the inserter
        if o == left(dd):
            dls = ("R",)                           # inserter on the left -> far = right lane
        elif o == (-left(dd)[0], -left(dd)[1]):
            dls = ("L",)
        else:
            dls = ("L", "R")                       # parallel arm: could land either lane
        dst = lane_id(de, drop)
        if pick in name_at:                        # body -> belt: seed the product tag
            for dl in dls:
                add(dst, dl, {tag_of(name_at[pick])})
        elif pick in trans_at:                     # belt -> belt: a tap moves EITHER lane
            src = lane_id(trans_at[pick], pick)
            for dl in dls:
                link(src, "L", dst, dl)
                link(src, "R", dst, dl)

    for node_lane in list(tags):                   # fixpoint over the lane graph
        work.append(node_lane)
    while work:
        node, lane = work.pop()
        ts = tags.get((node, lane), set())
        for dst, dl in edges.get((node, lane), ()):
            add(dst, dl, ts)

    mixed = sorted(f"{node}:{lane}={'+'.join(sorted(ts))}"
                   for (node, lane), ts in tags.items() if len(ts) > 1)
    report.add("no item mixing on a belt lane (max two products per belt, one per side)",
               not mixed, "" if not mixed else "mixed lanes: " + "; ".join(mixed[:4]))


def _check_fluids(graph: Graph, layout: Layout, bodies, report: Report) -> None:
    """Each `A ~> B` fluid lane must join A's OUTPUT fluid-box to B's INPUT fluid-box
    through a connected pipe network.

    A pipe must sit on the exact fluid-box external tile to attach (those tiles
    depend on the body's rotation). Pipes connect by 4-adjacency; a pipe-to-ground
    joins to its paired opposite-facing pipe-to-ground. A lane passes iff some
    output-box network of A equals some input-box network of B."""
    fluid_edges = [e for e in graph.edges if e.fluid]
    if not fluid_edges:
        return
    # An ASSEMBLER only has fluid boxes when its recipe uses fluid (i.e. it's an endpoint of a
    # fluid lane); a solid-recipe assembler has NONE, so a pipe passing its nominal box tile
    # does not attach. Chemical plants / tanks / fluid sources always have their boxes.
    fluid_endpoints = {e.src for e in fluid_edges} | {e.dst for e in fluid_edges}

    def has_fluid_boxes(name, proto):
        return proto != ASSEMBLER or name in fluid_endpoints

    pipes = {(e.x, e.y): e for e in layout.entities if e.proto in (PIPE, PIPE_TO_GROUND)}
    parent = {t: t for t in pipes}

    def find(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    for t, e in pipes.items():
        for d in (0, 4, 8, 12):                       # surface connections
            nb = (t[0] + DIR_DELTA[d][0], t[1] + DIR_DELTA[d][1])
            ne = pipes.get(nb)
            if ne is None:
                continue
            # A plain pipe connects on all 4 sides; a pipe-to-ground surface-connects ONLY
            # toward its open mouth (its other sides / the underground back reach a partner,
            # not an adjacent surface pipe). Both endpoints must accept the link.
            t_out = e.proto == PIPE or (e.direction or 0) == d
            nb_back = ne.proto == PIPE or (ne.direction or 0) == OPPOSITE[d]
            if t_out and nb_back:
                parent[find(t)] = find(nb)
        if e.proto == PIPE_TO_GROUND:                 # tunnel: join to the nearest paired exit
            d = e.direction or 0                      # `direction` is the OPEN mouth; the
            dx, dy = DIR_DELTA[OPPOSITE[d]]           # tunnel runs the OPPOSITE way underground
            for k in range(1, PIPE_UG_GAP + 1):       # pipes reach farther than belts
                far = (t[0] + dx * k, t[1] + dy * k)
                fe = pipes.get(far)
                if fe is None or fe.proto != PIPE_TO_GROUND:
                    continue                          # surface pipe above the tunnel: ignore
                fd = fe.direction or 0
                if fd == OPPOSITE[d]:                  # partner faces back at us -> tunnel pair
                    parent[find(t)] = find(far)
                    break
                if fd == d:                           # same-axis underground: blocks the line
                    break                             # (the nearer one claims it; can't jump it)
                # perpendicular pipe-to-ground: a crossing tunnel, doesn't interfere -> keep scanning

    out_nets, in_nets = {}, {}                         # name -> set of pipe networks at its boxes
    for name, b in bodies.items():
        if not has_fluid_boxes(name, b.proto):
            continue
        on, inn = set(), set()
        for tile, flow, mdir in _fluid_connections(b.proto, b.x, b.y, b.direction, with_dir=True):
            # a fluid box attaches to a PLAIN pipe on its external tile, OR a pipe-to-ground
            # whose open mouth faces the machine (mdir). A p2g facing any other way does NOT
            # feed the box (its underground side connects to its tunnel partner, not here).
            pe = pipes.get(tile)
            if pe is None or not (pe.proto == PIPE or
                                  (pe.proto == PIPE_TO_GROUND and (pe.direction or 0) == mdir)):
                continue
            net = find(tile)
            if flow in ("output", "both"):
                on.add(net)
            if flow in ("input", "both"):
                inn.add(net)
        out_nets[name], in_nets[name] = on, inn

    bad = []
    for e in fluid_edges:
        if not (out_nets.get(e.src) and in_nets.get(e.dst) and out_nets[e.src] & in_nets[e.dst]):
            bad.append(f"{e.src}~>{e.dst}")
    report.add("every fluid lane connects at a real fluid-box (pipe network)", not bad,
               "" if not bad else "unconnected fluid lanes: " + ", ".join(bad))

    # No UNDECLARED fluid lanes (the fluid analogue of "no undeclared belt lanes"): a network
    # joining one body's PRODUCED fluid to another's CONSUMED fluid is a real in-game path.
    # Intent is by node kind (source produces, output/tank sink consumes, a machine uses its
    # box flow) so co-consumers of one source net and a sink tank on it don't read as spurious.
    src_net, snk_net = {}, {}
    for name, b in bodies.items():
        if not has_fluid_boxes(name, b.proto):
            continue
        kind = graph.nodes[name].kind if name in graph.nodes else None
        for tile, flow, mdir in _fluid_connections(b.proto, b.x, b.y, b.direction, with_dir=True):
            pe = pipes.get(tile)
            if pe is None or not (pe.proto == PIPE or
                                  (pe.proto == PIPE_TO_GROUND and (pe.direction or 0) == mdir)):
                continue
            net = find(tile)
            if kind is NodeKind.FLUID or flow == "output":
                src_net.setdefault(name, set()).add(net)
            if kind is NodeKind.OUTPUT or flow == "input":
                snk_net.setdefault(name, set()).add(net)
    declared = {(e.src, e.dst) for e in fluid_edges}
    spurious = sorted(f"{a}~>{b}" for a in src_net for b in snk_net
                      if a != b and (a, b) not in declared and src_net[a] & snk_net[b])
    report.add("no undeclared fluid lanes", not spurious,
               "" if not spurious else "spurious fluid lanes: " + ", ".join(spurious))

    # Fluid EXCLUSIVITY: in Factorio a pipe network carries ONE fluid. Tag each
    # network with the fluid of every source attached to it; if a network gets two
    # different fluids it's contaminated (the bug the render audit caught that pure
    # reachability misses).
    net_fluids: dict = {}
    for name, b in bodies.items():
        if not has_fluid_boxes(name, b.proto):
            continue
        if b.proto == FLUID_SOURCE and b.item:
            for tile, _flow, mdir in _fluid_connections(b.proto, b.x, b.y, b.direction, with_dir=True):
                pe = pipes.get(tile)
                if pe is not None and (pe.proto == PIPE or
                                       (pe.proto == PIPE_TO_GROUND and (pe.direction or 0) == mdir)):
                    net_fluids.setdefault(find(tile), set()).add(b.item)
    mixed = sorted("+".join(sorted(fl)) for fl in net_fluids.values() if len(fl) > 1)
    report.add("no fluid mixing (one fluid per pipe network)", not mixed,
               "" if not mixed else "contaminated networks: " + ", ".join(mixed))
