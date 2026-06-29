"""The verifier is the centerpiece, so these tests pin down that it both PASSES
correct layouts and FAILS broken ones (missing belts, flipped inserters, etc.)."""

from pathlib import Path

from fgr.blueprint import to_blueprint_string
from fgr.dsl import parse
from fgr.ir import EAST
from fgr.layout import BELT, INSERTER, compile_graph
from fgr.verify import verify

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "basic"


def _load(name):
    g = parse((EXAMPLES / name).read_text())
    return g, compile_graph(g)


EXAMPLE_FILES = ("gears.fgr", "circuits.fgr", "science.fgr", "fanout.fgr",
                 "bus.fgr", "merge.fgr")


def test_examples_verify_clean():
    for name in EXAMPLE_FILES:
        g, lay = _load(name)
        report = verify(g, lay)
        assert report.ok, f"{name} should verify:\n{report.format()}"
        assert report.lanes_found == {(e.src, e.dst) for e in g.edges}


def test_missing_belt_breaks_a_lane():
    g, lay = _load("gears.fgr")
    # drop one belt tile in the middle of the iron->gears lane
    lay.entities = [e for e in lay.entities
                    if not (e.proto == BELT and e.meta.get("edge") == ("iron", "gears")
                            and e.x == 4)]
    report = verify(g, lay)
    assert not report.ok
    assert ("iron", "gears") not in report.lanes_found


def test_flipped_inserter_breaks_a_lane():
    g, lay = _load("gears.fgr")
    # flip the input inserter on `gears` 180° so it no longer feeds the assembler
    # (it was correctly facing WEST = picking from the belt; EAST reverses it)
    for e in lay.entities:
        if e.proto == INSERTER and e.meta.get("role") == "in-inserter" \
                and e.meta.get("edge") == ("iron", "gears"):
            e.direction = EAST  # now picks from the assembler / drops onto the belt
    report = verify(g, lay)
    assert not report.ok


def test_deterministic_output():
    # same source -> byte-identical blueprint (the reference generator is deterministic)
    a = to_blueprint_string(_load("circuits.fgr")[1])
    b = to_blueprint_string(_load("circuits.fgr")[1])
    assert a == b


def test_no_overlaps_in_examples():
    for name in EXAMPLE_FILES:
        g, lay = _load(name)
        seen = set()
        for e in lay.entities:
            for t in e.tiles():
                assert t not in seen, f"{name}: overlap at {t}"
                seen.add(t)


def test_shared_belt_uses_splitters_not_three_inserters():
    # `iron -> gears, sticks, out_raw` is ONE belt: the iron chest gets a single
    # output inserter, and the fan-out is done with splitters.
    from fgr.layout import SPLITTER
    g, lay = _load("bus.fgr")
    out_ins = [e for e in lay.entities if e.meta.get("role") == "out-inserter"
               and e.meta.get("src") == "iron"]
    assert len(out_ins) == 1, "shared belt should need only one output inserter on iron"
    assert any(e.proto == SPLITTER for e in lay.entities), "fan-out should place splitters"


def test_underground_crosses_in_circuits():
    # the iron->circuit lane has to cross the cable lane, so it must tunnel
    from fgr.layout import UNDERGROUND
    _, lay = _load("circuits.fgr")
    assert any(e.proto == UNDERGROUND for e in lay.entities), "expected an underground crossing"


def test_merge_uses_one_input_inserter_and_a_splitter():
    # `iron_a, iron_b -> gears`: both sources reach gears, which has ONE input
    # inserter, and a splitter does the merging.
    from fgr.layout import SPLITTER, INSERTER
    g, lay = _load("merge.fgr")
    report = verify(g, lay)
    assert report.ok, report.format()
    assert {("iron_a", "gears"), ("iron_b", "gears")} <= report.lanes_found
    gears_inputs = [e for e in lay.entities if e.proto == INSERTER
                    and e.meta.get("role") == "in-inserter"
                    and (e.meta.get("edge", ("", ""))[1] == "gears"
                         or (e.meta.get("merge") and e.meta["merge"][1] == "gears"))]
    assert len(gears_inputs) == 1, "merged belt should feed gears via one inserter"
    assert any(e.proto == SPLITTER for e in lay.entities), "merge should use a splitter"


def test_breaking_a_splitter_breaks_the_fanout():
    from fgr.layout import SPLITTER
    g, lay = _load("bus.fgr")
    before = {(e.src, e.dst) for e in g.edges}
    lay.entities = [e for e in lay.entities if e.proto != SPLITTER]  # rip out the splitters
    report = verify(g, lay)
    assert not report.ok
    assert report.lanes_found != before  # some consumer is no longer fed
