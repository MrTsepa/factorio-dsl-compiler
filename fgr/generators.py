"""One interface over the interchangeable layout generators.

The POC's premise is that the *generator* is swappable and :mod:`fgr.verify` is the oracle that
grades whatever it produces. Two concrete generators live in the tree:

    v1  -- the original search router (fixed grid + A* wire routing with rip-up/retry).
    v2  -- the deterministic lane-fabric engine (four passes, no search; see fgr/layout.py).

Both expose the same ``compile_graph(graph) -> Layout`` signature, so anything downstream (verifier,
blueprint export, renderers, tests, the comparison script) can target either by name.
"""
from __future__ import annotations

from . import layout as _v2
from . import layout_v1 as _v1
from . import layout_v3 as _v3
from .ir import Graph
from .layout import Layout

GENERATORS = {
    "v1": _v1.compile_graph,   # search router (A* + rip-up); optimal-ish but can blow up on scale
    "v2": _v2.compile_graph,   # deterministic lane fabric; fast and robust, cleaner layouts
    "v3": _v3.compile_graph,   # global negotiated-congestion router (PathFinder-style)
}
DEFAULT = "v2"


def compile_graph(graph: Graph, generator: str = DEFAULT) -> Layout:
    """Compile ``graph`` with the named generator (``"v1"`` or ``"v2"``)."""
    try:
        fn = GENERATORS[generator]
    except KeyError:
        raise ValueError(f"unknown generator {generator!r}; choose from {sorted(GENERATORS)}")
    return fn(graph)
