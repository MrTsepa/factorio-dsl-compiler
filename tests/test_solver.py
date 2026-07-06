"""The rate solver (fgr.solver): sizing math against known numbers, and the expanded
graph must still verify. Skips when FBSR game data is unavailable."""
import pytest

from fgr.dsl import parse
from fgr.generators import compile_graph
from fgr.verify import verify


def _solve(text):
    from fgr import solver
    from fgr.rates import RatesUnavailable
    try:
        return solver.solve(parse(text))
    except RatesUnavailable:
        pytest.skip("FBSR game data unavailable")


GEARS_1BELT = "\n".join([
    "input iron : iron-plate @ 1 belt",
    "assembler gears : iron-gear-wheel",
    "output out",
    "",
    "iron -> gears",
    "gears -> out",
])


def test_input_driven_gears_plan():
    g2, plan = _solve(GEARS_1BELT)
    # 15 iron/s -> 7.5 gears/s target; arm-limited copies (0.9375/2 * 0.95)
    assert plan["target_per_s"]["out"] == pytest.approx(7.5)
    assert plan["machines"]["gears"]["copies"] == 17
    assert plan["machines"]["gears"]["binding"] == "inserter arms"
    assert plan["input_lanes"]["iron"] == 1
    # every expanded machine keeps its supplier lanes unmerged (arms!)
    assert any(n.startswith("gears_") for n in g2.no_merge)


def test_output_driven_red_science_plan_and_verifies():
    g2, plan = _solve("\n".join([
        "input copper : copper-plate",
        "input iron : iron-plate",
        "assembler gear : iron-gear-wheel",
        "assembler red : automation-science-pack",
        "output out @ 0.45/s",
        "",
        "iron -> gear",
        "copper -> red",
        "gear -> red",
        "red -> out",
    ]))
    assert plan["machines"]["red"]["copies"] == 4          # 0.45 / (0.15*0.95)
    assert plan["machines"]["gear"]["copies"] == 2
    # ceil() overdelivers: machines run at cap, not at the plan -- the expected
    # actual (min stage capacity) is what the game measures (0.6 == 4 x 0.15)
    assert plan["expected_actual_per_s"]["out"] == pytest.approx(0.6)
    layout = compile_graph(g2)
    assert verify(g2, layout).ok


def test_sized_graph_compiles_and_verifies():
    g2, _plan = _solve(GEARS_1BELT)
    layout = compile_graph(g2)
    rep = verify(g2, layout)
    assert rep.ok, rep.format()
    # the input net must have NO internal tap arms (a tap throttles a subtree)
    taps = [e for e in layout.entities
            if e.meta.get("net") == "b:iron" and e.meta.get("role") == "tap"]
    assert not taps, "sized input net must reach every machine by direct trunk pickup"
