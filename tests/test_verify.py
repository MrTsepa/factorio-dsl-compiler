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
        if e.proto == INSERTER and e.meta.get("role") == "in" \
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


def test_fanout_uses_inline_taps_not_splitters():
    # v2: `iron -> gears, sticks, out_raw` is one producer belt tapped by inline inserters,
    # NO splitters anywhere (the user's "feed many machines without splitting").
    from fgr.layout import SPLITTER
    g, lay = _load("bus.fgr")
    report = verify(g, lay)
    assert report.ok, report.format()
    assert {("iron", "gears"), ("iron", "sticks"), ("iron", "out_raw")} <= report.lanes_found
    assert not any(e.proto == SPLITTER for e in lay.entities), \
        "v2 fans out with inline inserter taps, not splitters"


def test_circuits_two_input_lane_verifies():
    # iron->circuit must reach circuit past the cable lane; v2 routes it (direct or a dive).
    g, lay = _load("circuits.fgr")
    report = verify(g, lay)
    assert report.ok, report.format()
    assert {("iron", "circuit"), ("cable", "circuit")} <= report.lanes_found


def test_merge_is_multi_tap_no_splitter():
    # `iron_a, iron_b -> gears`: v2 gives each source its own lane, both tapping gears --
    # a multi-tap merge with NO splitter.
    from fgr.layout import SPLITTER
    g, lay = _load("merge.fgr")
    report = verify(g, lay)
    assert report.ok, report.format()
    assert {("iron_a", "gears"), ("iron_b", "gears")} <= report.lanes_found
    assert not any(e.proto == SPLITTER for e in lay.entities), \
        "v2 merges by multi-tap, not a splitter"


def test_breaking_an_output_inserter_breaks_its_lane():
    # rip out a producer's output inserter; its consumer loses the lane.
    g, lay = _load("merge.fgr")
    lay.entities = [e for e in lay.entities
                    if not (e.proto == INSERTER and e.meta.get("role") == "out"
                            and e.meta.get("src") == "iron_a")]
    report = verify(g, lay)
    assert not report.ok
    assert ("iron_a", "gears") not in report.lanes_found
