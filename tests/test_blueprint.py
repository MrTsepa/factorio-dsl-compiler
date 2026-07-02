"""The blueprint encoder must produce an importable string that round-trips back to the
same entities -- otherwise a "verified" layout wouldn't actually load in Factorio."""
from fgr.blueprint import to_blueprint, to_blueprint_string
from fgr.dsl import parse
from fgr.encode import decode_blueprint_string
from fgr.generators import compile_graph

SPEC = """
input iron : iron-plate
assembler gears : iron-gear-wheel
output out
iron -> gears
gears -> out
"""


def test_blueprint_string_round_trips():
    lay = compile_graph(parse(SPEC))
    bp = to_blueprint(lay, "gears")
    s = to_blueprint_string(lay, "gears")
    assert s[0] == "0"                                   # Factorio 2.0 version byte
    decoded = decode_blueprint_string(s)
    assert decoded == bp                                 # string decodes to the same dict
    assert len(decoded["blueprint"]["entities"]) == len(lay.entities)


def test_every_entity_has_a_center_position():
    """Each emitted entity carries a numeric center position (what Factorio imports)."""
    lay = compile_graph(parse(SPEC))
    for e in to_blueprint(lay, "gears")["blueprint"]["entities"]:
        assert isinstance(e["position"]["x"], (int, float))
        assert isinstance(e["position"]["y"], (int, float))
        assert e["name"]
