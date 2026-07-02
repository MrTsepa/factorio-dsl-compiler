"""The verifier is the centerpiece, so these tests pin down that it both PASSES
correct layouts and FAILS broken ones (missing belts, flipped inserters, etc.)."""

from pathlib import Path

from fgr.blueprint import to_blueprint_string
from fgr.dsl import parse
from fgr.ir import EAST
from fgr.generators import compile_graph
from fgr.layout import BELT, INSERTER
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
    # drop the belt tile nearest the midpoint between the two bodies -- mid-run on the
    # iron->gears lane whatever generator laid it (no reliance on generator meta)
    bodies = {e.meta["node"]: e for e in lay.entities if e.meta.get("node")}
    ax, ay = bodies["iron"].center
    bx, by = bodies["gears"].center
    mx, my = (ax + bx) / 2, (ay + by) / 2
    belts = [e for e in lay.entities if e.proto == BELT]
    victim = min(belts, key=lambda e: abs(e.x - mx) + abs(e.y - my))
    lay.entities = [e for e in lay.entities if e is not victim]
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


# --- item mixing on belt lanes (a belt: two sides, ONE product per side) ----------
_TWO_PRODUCTS = "\n".join([
    "input iron   : iron-plate",
    "input copper : copper-plate",
    "output out",
    "",
    "iron   -> out",
    "copper -> out",
])


def _two_product_layout(separated):
    """Hand-built: iron and copper meet at the pick tile (4,2) of out's inserter.
    ``separated``: each side-loads from an opposite side -- one product per lane
    (legal). Otherwise copper joins iron's column one tile early, landing on a lane
    iron already uses -- mixing (illegal)."""
    from fgr.ir import NORTH, SOUTH, WEST
    from fgr.layout import CHEST_INPUT, CHEST_OUTPUT, Layout, PlacedEntity

    g = parse(_TWO_PRODUCTS)
    lay = Layout()
    lay.add(PlacedEntity(CHEST_INPUT, 0, 0, item="iron-plate", meta={"node": "iron"}))
    lay.add(PlacedEntity(CHEST_INPUT, 0, 4, item="copper-plate", meta={"node": "copper"}))
    lay.add(PlacedEntity(CHEST_OUTPUT, 6, 2, meta={"node": "out"}))
    lay.add(PlacedEntity(BELT, 4, 2, direction=EAST, meta={}))     # shared pick tile
    lay.add(PlacedEntity(INSERTER, 5, 2, direction=WEST, meta={}))  # -> out chest
    # iron: chest -> eastward run -> south down column x=4 into the pick tile
    lay.add(PlacedEntity(INSERTER, 1, 0, direction=WEST, meta={}))
    for x in (2, 3):
        lay.add(PlacedEntity(BELT, x, 0, direction=EAST, meta={}))
    lay.add(PlacedEntity(BELT, 4, 0, direction=SOUTH, meta={}))
    lay.add(PlacedEntity(BELT, 4, 1, direction=SOUTH, meta={}))
    lay.add(PlacedEntity(INSERTER, 1, 4, direction=WEST, meta={}))
    lay.add(PlacedEntity(BELT, 2, 4, direction=EAST, meta={}))
    if separated:
        # copper arrives from the SOUTH side of the pick tile: its own lane
        lay.add(PlacedEntity(BELT, 3, 4, direction=EAST, meta={}))
        lay.add(PlacedEntity(BELT, 4, 4, direction=NORTH, meta={}))
        lay.add(PlacedEntity(BELT, 4, 3, direction=NORTH, meta={}))
    else:
        # copper side-loads into iron's column at (4,1): shares iron's lane
        lay.add(PlacedEntity(BELT, 3, 4, direction=NORTH, meta={}))
        lay.add(PlacedEntity(BELT, 3, 3, direction=NORTH, meta={}))
        lay.add(PlacedEntity(BELT, 3, 2, direction=NORTH, meta={}))
        lay.add(PlacedEntity(BELT, 3, 1, direction=EAST, meta={}))
    return g, lay


def test_lane_separated_products_pass():
    g, lay = _two_product_layout(separated=True)
    report = verify(g, lay)
    assert report.ok, report.format()


def test_same_lane_mixing_fails():
    g, lay = _two_product_layout(separated=False)
    report = verify(g, lay)
    bad = [c for c in report.checks if "mixing on a belt lane" in c.name][0]
    assert not bad.ok, "two products sharing one belt lane must be flagged"
    assert "copper-plate" in bad.detail and "iron-plate" in bad.detail
