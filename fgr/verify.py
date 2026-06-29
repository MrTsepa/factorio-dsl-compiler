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
from .layout import (ASSEMBLER, BELT, CHEST_INPUT, CHEST_OUTPUT, INSERTER,
                     SPLITTER, UG_MAX_GAP, UNDERGROUND, Layout, PlacedEntity)

_PROTO_FOR_KIND = {NodeKind.INPUT: CHEST_INPUT, NodeKind.OUTPUT: CHEST_OUTPUT,
                   NodeKind.ASSEMBLER: ASSEMBLER}


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

    edges_out = _flow_edges(layout, carrier_at, trans_at, report)
    report.lanes_found = _direct_lanes(graph, bodies, body_tiles, edges_out)
    _compare_to_spec(graph, report)
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
        if ent.proto != _PROTO_FOR_KIND[node.kind]:
            wrong.append(f"{name}: {ent.proto} != {_PROTO_FOR_KIND[node.kind]}")
        if node.kind is NodeKind.ASSEMBLER and ent.recipe != node.recipe:
            wrong.append(f"{name}: recipe {ent.recipe!r} != {node.recipe!r}")
        if node.kind is NodeKind.INPUT and ent.item != node.item:
            wrong.append(f"{name}: item {ent.item!r} != {node.item!r}")

    report.add("every node placed exactly once", not missing,
               "" if not missing else ", ".join(missing))
    report.add("node protos/recipes/items match spec", not wrong,
               "" if not wrong else "; ".join(wrong))
    return bodies


def _flow_edges(layout: Layout, carrier_at: dict, trans_at: dict, report: Report) -> dict:
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
            return e.ug_type == "input" and d == td  # only an entrance, fed from behind
        return False

    def push(src_id, target_tile, d) -> None:
        if accepts(target_tile, d):
            edges.setdefault(src_id, set()).add(carrier_at[target_tile])

    for e in layout.entities:
        if e.proto == INSERTER:
            dx, dy = DIR_DELTA[e.direction]
            # an inserter's `direction` points at its PICKUP; it drops on the far side
            pick = carrier_at.get((e.x + dx, e.y + dy))
            drop = carrier_at.get((e.x - dx, e.y - dy))
            if pick is None or drop is None:
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
        if e is not None and e.proto == UNDERGROUND and (e.direction or 0) == d:
            # first same-direction underground on the line: an exit pairs, an
            # entrance blocks (steals the pairing) -> our entrance is unpaired
            return t if e.ug_type == "output" else None
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
    spec = {(e.src, e.dst) for e in graph.edges}
    found = report.lanes_found
    missing = sorted(spec - found)
    spurious = sorted(found - spec)
    report.add("every declared lane physically connects", not missing,
               "" if not missing else "missing lanes: " +
               ", ".join(f"{a}->{b}" for a, b in missing))
    report.add("no undeclared lanes", not spurious,
               "" if not spurious else "spurious lanes: " +
               ", ".join(f"{a}->{b}" for a, b in spurious))
