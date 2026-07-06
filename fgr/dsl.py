"""The .fgr DSL front-end: text -> Graph IR.

Grammar (one statement per line, ``#`` starts a comment):

    input  NAME : item-name        # an input chest stocked with `item-name`
    assembler NAME : recipe-name   # an assembler crafting `recipe-name`
    furnace NAME : item-name       # a furnace smelting `item-name` (steel-plate, ...)
    chemical NAME : recipe-name    # a chemical plant (recipes that use fluids)
    fluid  NAME : fluid-name       # an infinite fluid source (water, sulfuric-acid, ...)
    output NAME                    # an output chest

    A -> B                         # a belt lane carrying items from A to B
    A -> B -> C                    # chains expand to A->B and B->C
    A -> B, C, D                   # ONE belt off A, split to feed B, C and D (splitters)
    A, B -> C                      # merge several sources onto one belt (splitters)
    A ~> B                         # a FLUID lane: A to B by pipe (not belt)
    A ~> B, C                      # one fluid source piped to several consumers

Node names are identifiers (letters/digits/underscore). Item and recipe names are
Factorio internal names (lowercase, hyphenated), e.g. `iron-gear-wheel`.
"""

from __future__ import annotations

import re

from .ir import Graph, Node, NodeKind

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_PROTO = r"[A-Za-z0-9_-]+"  # Factorio item/recipe internal name
_DECL_RE = re.compile(
    rf"^(input|assembler|furnace|chemical|fluid|output)\s+({_NAME})\s*"
    rf"(?::\s*({_PROTO})\s*)?"
    rf"(?:@\s*([0-9.]+)\s*(/s|/min|belt|belts?)?\s*)?$")
_NAME_RE = re.compile(rf"^{_NAME}$")


class DslError(ValueError):
    """A DSL syntax or validation error, carrying the offending line number."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].rstrip()


def parse(text: str) -> Graph:
    """Parse DSL source text into a validated :class:`Graph`."""
    graph = Graph()
    edges: list[tuple[str, str, int]] = []          # item (belt) lanes, validated after decls
    fedges: list[tuple[str, str, int]] = []         # fluid (pipe) lanes
    shared: list[tuple[str, tuple[str, ...]]] = []  # (src, dsts) fan-out groups
    merges: list[tuple[tuple[str, ...], str]] = []  # (srcs, dst) merge groups

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue

        if "~>" in line:
            _parse_fluid_statement(line, lineno, fedges)
            continue
        if "->" in line:
            _parse_edge_statement(line, lineno, edges, shared, merges)
            continue

        m = _DECL_RE.match(line)
        if not m:
            raise DslError(f"cannot parse statement: {line!r}", lineno)
        kind_s, name, proto = m.group(1), m.group(2), m.group(3)
        rate = None
        if m.group(4) is not None:
            val, unit = float(m.group(4)), (m.group(5) or "/s")
            rate = {"/s": val, "/min": val / 60.0,
                    "belt": val * 15.0, "belts": val * 15.0}[unit]
        node = _build_node(kind_s, name, proto, lineno, rate)
        try:
            graph.add_node(node)
        except ValueError as exc:
            raise DslError(str(exc), lineno) from exc

    _validate_edges(graph, edges, fedges)
    graph.shared_belts = shared
    graph.merges = merges
    return graph


def _names(text: str, lineno: int) -> list[str]:
    out = [n.strip() for n in text.split(",")]
    for n in out:
        if not _NAME_RE.match(n):
            raise DslError(f"invalid node name: {n!r}", lineno)
    return out


def _parse_edge_statement(line: str, lineno: int, out: list, shared: list, merges: list) -> None:
    """Parse `A -> B`, chain `A -> B -> C`, fan-out `A -> B, C`, or merge `A, B -> C`."""
    parts = [p.strip() for p in line.split("->")]
    if any("," in p for p in parts):
        if len(parts) != 2:
            raise DslError("a comma group can't be chained: use `A -> B, C` or `A, B -> C`", lineno)
        left, right = _names(parts[0], lineno), _names(parts[1], lineno)
        if len(left) > 1 and len(right) > 1:
            raise DslError("commas allowed on only ONE side (fan-out OR merge)", lineno)
        if len(right) > 1:                                  # fan-out: A -> B, C
            for d in right:
                out.append((left[0], d, lineno))
            shared.append((left[0], tuple(right)))
        else:                                               # merge: A, B -> C
            for s in left:
                out.append((s, right[0], lineno))
            merges.append((tuple(left), right[0]))
        return

    if any(not p for p in parts):
        raise DslError(f"malformed edge (empty endpoint): {line!r}", lineno)
    for p in parts:
        if not _NAME_RE.match(p):
            raise DslError(f"invalid node name in edge: {p!r}", lineno)
    for a, b in zip(parts, parts[1:]):
        out.append((a, b, lineno))


def _parse_fluid_statement(line: str, lineno: int, out: list) -> None:
    """Parse `A ~> B`, chain `A ~> B ~> C`, or branch `A ~> B, C` (pipes branch/merge
    freely, so commas on either side just expand to multiple fluid edges)."""
    parts = [p.strip() for p in line.split("~>")]
    if any(not p for p in parts):
        raise DslError(f"malformed fluid lane (empty endpoint): {line!r}", lineno)
    groups = [_names(p, lineno) for p in parts]
    for left, right in zip(groups, groups[1:]):
        for s in left:
            for d in right:
                out.append((s, d, lineno))


def _build_node(kind_s: str, name: str, proto: str | None, lineno: int,
                rate: float | None = None) -> Node:
    kind = NodeKind(kind_s)
    needs = {NodeKind.INPUT: "an item", NodeKind.ASSEMBLER: "a recipe", NodeKind.FURNACE: "an item",
             NodeKind.CHEMICAL: "a recipe", NodeKind.FLUID: "a fluid-name"}
    if rate is not None and kind not in (NodeKind.INPUT, NodeKind.OUTPUT):
        raise DslError("@rate is only valid on input/output nodes (the solver sizes "
                       "the machines in between)", lineno)
    if kind in needs:
        if not proto:
            raise DslError(f"{kind_s} {name!r} needs {needs[kind]}: `{kind_s} {name} : ...`", lineno)
        if kind in (NodeKind.INPUT, NodeKind.FLUID):
            return Node(name, kind, item=proto, rate=rate)
        return Node(name, kind, recipe=proto)
    # OUTPUT
    if proto:
        raise DslError(f"output {name!r} takes no item/recipe", lineno)
    return Node(name, kind, rate=rate)


_SOURCES = (NodeKind.INPUT, NodeKind.FLUID)


def _validate_edges(graph: Graph, edges, fedges) -> None:
    def check(src, dst, lineno, fluid):
        for end in (src, dst):
            if end not in graph.nodes:
                raise DslError(f"lane references undeclared node: {end!r}", lineno)
        if src == dst:
            raise DslError(f"self-loop not allowed: {src!r}", lineno)
        sn, dn = graph.nodes[src], graph.nodes[dst]
        if dn.kind in _SOURCES:
            raise DslError(f"{dn.kind.value} {dst!r} cannot have an incoming lane", lineno)
        if sn.kind is NodeKind.OUTPUT:
            raise DslError(f"output {src!r} cannot have an outgoing lane", lineno)
        if fluid:
            # chemical plants AND assemblers have fluid boxes (assembling-machine-2/3 craft
            # crafting-with-fluid recipes like electric-engine-unit / processing-unit);
            # furnaces and chests do not.
            if sn.kind not in (NodeKind.FLUID, NodeKind.CHEMICAL, NodeKind.ASSEMBLER):
                raise DslError(
                    f"fluid lane source {src!r} must be a `fluid`, `chemical`, or `assembler` node", lineno)
            if dn.kind not in (NodeKind.CHEMICAL, NodeKind.ASSEMBLER, NodeKind.OUTPUT):
                raise DslError(
                    f"fluid lane target {dst!r} must be a `chemical`, `assembler`, or `output` node", lineno)
        elif NodeKind.FLUID in (sn.kind, dn.kind):
            raise DslError(f"`{src} -> {dst}`: a fluid node needs a pipe lane (use `~>`)", lineno)
        graph.add_edge(src, dst, fluid)

    for src, dst, lineno in edges:
        check(src, dst, lineno, False)
    for src, dst, lineno in fedges:
        check(src, dst, lineno, True)

    # An output sink is a chest (items) or a tank (fluid) -- it can't be both.
    for name, node in graph.nodes.items():
        if node.kind is NodeKind.OUTPUT:
            kinds = {e.fluid for e in graph.edges if e.dst == name}
            if len(kinds) > 1:
                raise DslError(f"output {name!r} receives both item and fluid lanes "
                               "(use separate outputs)")
