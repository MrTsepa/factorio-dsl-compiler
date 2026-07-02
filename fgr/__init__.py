"""fgr — a tiny high-level DSL for Factorio factories that compiles to a layout.

You describe *what* the factory is (a production graph of input chests,
assemblers, and output chests, wired with ``->`` edges). A layout **generator**
decides *where* everything physically goes — assembler placement, inserter
geometry, and the actual belt tile paths ("belt lanes") — and emits a real
Factorio 2.0 blueprint string that renders via Factorio-FBSR. An independent
**verifier** grades the result, so the generator is swappable: ``fgr.compile_graph``
below is the default (v2, the deterministic lane fabric); ``fgr.generators``
exposes the full registry (including v1, the original search router) by name.
"""

from .ir import Edge, Graph, Node, NodeKind
from .dsl import DslError, parse
from .layout import Layout, compile_graph
from .blueprint import to_blueprint, to_blueprint_string
from . import generators

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
    "generators",
]
