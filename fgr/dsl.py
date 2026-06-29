"""The .fgr DSL front-end: text -> Graph IR.

Grammar (one statement per line, ``#`` starts a comment):

    input  NAME : item-name        # an input chest stocked with `item-name`
    assembler NAME : recipe-name   # an assembler crafting `recipe-name`
    output NAME                    # an output chest

    A -> B                         # a belt lane carrying items from A to B
    A -> B -> C                    # chains expand to A->B and B->C
    A -> B, C, D                   # ONE belt off A, split to feed B, C and D

Node names are identifiers (letters/digits/underscore). Item and recipe names are
Factorio internal names (lowercase, hyphenated), e.g. `iron-gear-wheel`.
"""

from __future__ import annotations

import re

from .ir import Graph, Node, NodeKind

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_PROTO = r"[A-Za-z0-9_-]+"  # Factorio item/recipe internal name
_DECL_RE = re.compile(rf"^(input|assembler|output)\s+({_NAME})\s*(?::\s*({_PROTO})\s*)?$")
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
    edges: list[tuple[str, str, int]] = []          # (src, dst, lineno), validated after decls
    shared: list[tuple[str, tuple[str, ...]]] = []  # (src, dsts) fan-out groups
    merges: list[tuple[tuple[str, ...], str]] = []  # (srcs, dst) merge groups

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue

        if "->" in line:
            _parse_edge_statement(line, lineno, edges, shared, merges)
            continue

        m = _DECL_RE.match(line)
        if not m:
            raise DslError(f"cannot parse statement: {line!r}", lineno)
        kind_s, name, proto = m.group(1), m.group(2), m.group(3)
        node = _build_node(kind_s, name, proto, lineno)
        try:
            graph.add_node(node)
        except ValueError as exc:
            raise DslError(str(exc), lineno) from exc

    _validate_edges(graph, edges)
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


def _build_node(kind_s: str, name: str, proto: str | None, lineno: int) -> Node:
    kind = NodeKind(kind_s)
    if kind is NodeKind.INPUT:
        if not proto:
            raise DslError(f"input {name!r} needs an item: `input {name} : item-name`", lineno)
        return Node(name, kind, item=proto)
    if kind is NodeKind.ASSEMBLER:
        if not proto:
            raise DslError(f"assembler {name!r} needs a recipe: `assembler {name} : recipe`", lineno)
        return Node(name, kind, recipe=proto)
    # OUTPUT
    if proto:
        raise DslError(f"output {name!r} takes no item/recipe", lineno)
    return Node(name, kind)


def _validate_edges(graph: Graph, edges: list[tuple[str, str, int]]) -> None:
    for src, dst, lineno in edges:
        for end in (src, dst):
            if end not in graph.nodes:
                raise DslError(f"edge references undeclared node: {end!r}", lineno)
        if src == dst:
            raise DslError(f"self-loop not allowed: {src!r}", lineno)
        sn, dn = graph.nodes[src], graph.nodes[dst]
        if dn.kind is NodeKind.INPUT:
            raise DslError(f"input chest {dst!r} cannot have an incoming lane", lineno)
        if sn.kind is NodeKind.OUTPUT:
            raise DslError(f"output chest {src!r} cannot have an outgoing lane", lineno)
        graph.add_edge(src, dst)
