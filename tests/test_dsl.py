import pytest

from fgr.dsl import DslError, parse
from fgr.ir import NodeKind


def test_parses_nodes_and_edges():
    g = parse("""
        input iron : iron-plate
        assembler gears : iron-gear-wheel
        output out
        iron -> gears -> out
    """)
    assert set(g.nodes) == {"iron", "gears", "out"}
    assert g.nodes["iron"].kind is NodeKind.INPUT
    assert g.nodes["iron"].item == "iron-plate"
    assert g.nodes["gears"].recipe == "iron-gear-wheel"
    assert [(e.src, e.dst) for e in g.edges] == [("iron", "gears"), ("gears", "out")]


def test_comments_and_blank_lines_ignored():
    g = parse("# a comment\n\ninput a : x  # trailing\noutput b\na -> b\n")
    assert set(g.nodes) == {"a", "b"}


@pytest.mark.parametrize("src, msg", [
    ("input a : x\nb -> a", "undeclared"),       # edge to unknown node
    ("input a : x\noutput b\nb -> a", "incoming"),  # input cannot receive
    ("input a : x\noutput b\nb -> a", None),     # placeholder; see below
    ("input a", "needs an item"),                # input without item
    ("assembler a", "needs a recipe"),           # assembler without recipe
    ("output a : x", "takes no"),                # output with item
    ("input a : x\ninput a : y", "duplicate"),   # duplicate name
])
def test_validation_errors(src, msg):
    if msg is None:
        return
    with pytest.raises(DslError) as exc:
        parse(src)
    assert msg in str(exc.value)


def test_output_cannot_emit():
    with pytest.raises(DslError) as exc:
        parse("input a : x\noutput b\nb -> a")
    assert "outgoing" in str(exc.value) or "incoming" in str(exc.value)
