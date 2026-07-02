"""Regression guard over EVERY example: each must compile (never crash) and the verifier
must pass, on the DEFAULT generator (v3, the global negotiated router). KNOWN_FAILING is
the xfail'd tail -- EMPTY since v3: it routes all 49 (v2's tail was fluids_7 + scale_1 +
scale_5). A KNOWN_FAILING case that starts passing fails loudly so we delist it.
"""
import glob
import os

import pytest

from fgr.dsl import parse
from fgr.generators import compile_graph
from fgr.verify import verify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = sorted(glob.glob(os.path.join(ROOT, "examples", "*", "*.fgr")))


def _rel(p):
    return p.split("examples" + os.sep)[1].replace(os.sep, "/")


# The default generator's unfixed tail (xfail). Empty since v3; keep sorted if it regrows.
KNOWN_FAILING: set = set()


@pytest.mark.parametrize("path", EXAMPLES, ids=_rel)
def test_example_compiles_and_verifies(path):
    rel = _rel(path)
    g = parse(open(path).read())
    layout = compile_graph(g)               # must never crash, for ANY example
    rep = verify(g, layout)
    if rel in KNOWN_FAILING:
        if rep.ok:
            pytest.fail(f"{rel} now PASSES -- remove it from KNOWN_FAILING")
        pytest.xfail(f"tracked tail (not yet routed): {[c.name for c in rep.checks if not c.ok]}")
    assert rep.ok, f"{rel}:\n{rep.format()}"


def test_all_examples_compile():
    """Even unverified cases must produce a layout (no crashes)."""
    for path in EXAMPLES:
        g = parse(open(path).read())
        compile_graph(g)


def test_pass_rate_does_not_regress():
    """Ratchet: at least this many examples fully verify (raise as the tail shrinks)."""
    ok = 0
    for path in EXAMPLES:
        g = parse(open(path).read())
        if verify(g, compile_graph(g)).ok:
            ok += 1
    assert ok >= len(EXAMPLES) - len(KNOWN_FAILING), f"only {ok}/{len(EXAMPLES)} verify"
