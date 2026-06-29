"""fgr — a tiny high-level DSL for Factorio factories that compiles to a layout.

You describe *what* the factory is (a production graph of input chests,
assemblers, and output chests, wired with ``->`` edges). The compiler decides
*where* everything physically goes — assembler placement, inserter geometry, and
the actual belt tile paths ("belt lanes") — fully deterministically, and emits a
real Factorio 2.0 blueprint string that renders via Factorio-FBSR.
"""

from .ir import Edge, Graph, Node, NodeKind
from .dsl import DslError, parse
from .layout import Layout, compile_graph
from .blueprint import to_blueprint, to_blueprint_string

__all__ = [
    "Edge",
    "Graph",
    "Node",
    "NodeKind",
    "DslError",
    "parse",
    "Layout",
    "compile_graph",
    "to_blueprint",
    "to_blueprint_string",
]
