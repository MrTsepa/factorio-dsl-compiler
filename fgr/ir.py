"""The intermediate representation: a production graph, plus small geometry types.

This sits between the DSL front-end (``dsl.py``) and the layout compiler
(``layout.py``). The graph is intentionally tiny — nodes are the user's
primitives (input chest / assembler / output chest) and edges are "belt lanes":
a logical request to move items from one node to another. The compiler turns
each lane into concrete inserters + belt tiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# --- Factorio direction constants (2.0 uses the 16-way enum; we only need the
# four cardinals). Value is the in-game ``direction`` field; semantics verified
# by rendering: 0 points North, 4 East, 8 South, 12 West. For belts this is the
# flow direction. CAVEAT: an *inserter*'s ``direction`` points at the tile it
# picks FROM; it drops on the opposite tile (Factorio stores inserters reversed).
# -----------------------------------------------------------------------------
NORTH = 0
EAST = 4
SOUTH = 8
WEST = 12

# Unit step (dx, dy) for each cardinal direction. Factorio's y axis points down.
DIR_DELTA: dict[int, tuple[int, int]] = {
    NORTH: (0, -1),
    EAST: (1, 0),
    SOUTH: (0, 1),
    WEST: (-1, 0),
}
DELTA_DIR: dict[tuple[int, int], int] = {v: k for k, v in DIR_DELTA.items()}
OPPOSITE: dict[int, int] = {NORTH: SOUTH, SOUTH: NORTH, EAST: WEST, WEST: EAST}


def opposite(direction: int) -> int:
    return OPPOSITE[direction]


def delta_to_dir(dx: int, dy: int) -> int:
    """Direction constant for a unit step. Raises on non-cardinal/zero steps."""
    try:
        return DELTA_DIR[(dx, dy)]
    except KeyError as exc:  # pragma: no cover - guards a programming error
        raise ValueError(f"not a unit cardinal step: {(dx, dy)}") from exc


class NodeKind(Enum):
    """The DSL primitives that become entities."""

    INPUT = "input"        # an input chest, stocked with one item
    ASSEMBLER = "assembler"  # crafts a recipe
    OUTPUT = "output"      # an output chest, collects items


@dataclass(frozen=True)
class Node:
    """A vertex in the production graph.

    ``item`` is the stocked item for INPUT nodes; ``recipe`` is the crafted
    recipe for ASSEMBLER nodes. OUTPUT nodes carry neither.
    """

    name: str
    kind: NodeKind
    item: str | None = None
    recipe: str | None = None


@dataclass(frozen=True)
class Edge:
    """A "belt lane": a request to carry items from ``src`` to ``dst``."""

    src: str
    dst: str


@dataclass
class Graph:
    """A whole factory description: an ordered set of nodes and edges.

    Insertion order is preserved so that compilation is deterministic and stable
    with respect to how the DSL was written.
    """

    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    # `A -> B, C` fan-outs: one belt off the source feeds every consumer (via
    # splitters). Each is also expanded into plain `edges` so the verifier still
    # checks (src, dst) connectivity per consumer.
    shared_belts: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    # `A, B -> C` merges: several sources combine onto one belt (via splitters)
    # into a single consumer. Also expanded into plain `edges` (one per source).
    merges: list[tuple[tuple[str, ...], str]] = field(default_factory=list)

    def add_node(self, node: Node) -> None:
        if node.name in self.nodes:
            raise ValueError(f"duplicate node name: {node.name!r}")
        self.nodes[node.name] = node

    def add_edge(self, src: str, dst: str) -> None:
        self.edges.append(Edge(src, dst))

    def successors(self, name: str) -> list[str]:
        return [e.dst for e in self.edges if e.src == name]

    def predecessors(self, name: str) -> list[str]:
        return [e.src for e in self.edges if e.dst == name]
