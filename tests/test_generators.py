"""The generator registry (fgr.generators): both v1 and v2 are reachable by name and
produce a valid, verifying layout on simple graphs. This is NOT a full v1 regression
battery -- v1's search router can be slow/hang on large or congested graphs (that's the
whole reason v2 exists; see scripts/compare_generators.py for the full head-to-head)."""
import pytest

from fgr.dsl import parse
from fgr.generators import DEFAULT, GENERATORS, compile_graph
from fgr.verify import verify

SIMPLE = "\n".join([
    "input     iron  : iron-plate",
    "assembler gears : iron-gear-wheel",
    "output    out",
    "",
    "iron  -> gears",
    "gears -> out",
])


def test_default_generator_is_v2():
    assert DEFAULT == "v2"


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_each_generator_verifies_a_simple_graph(name):
    g = parse(SIMPLE)
    layout = compile_graph(g, name)
    rep = verify(g, layout)
    assert rep.ok, f"{name}:\n{rep.format()}"


def test_unknown_generator_name_raises():
    with pytest.raises(ValueError):
        compile_graph(parse(SIMPLE), "v3")
