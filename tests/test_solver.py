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
    # 92% of the declared belt (a 100%-loaded belt permanently starves the tail
    # taps), multi-arm machines: 3 iron arms in + 2 output arms each
    assert plan["target_per_s"]["out"] == pytest.approx(6.9)
    assert plan["machines"]["gears"]["copies"] == 5
    assert plan["machines"]["gears"]["arms_in_per_copy"] == {"iron-plate": 3}
    assert plan["machines"]["gears"]["output_arms_per_copy"] == 2
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
    assert plan["machines"]["gear"]["copies"] == 1         # multi-arm: one suffices
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
            if (e.meta.get("net") or "").startswith("b:iron")
            and e.meta.get("role") == "tap"]
    assert not taps, "sized input net must reach every machine by direct trunk pickup"
    # multi-arm feeds are real: 5 machines x 3 iron arms = 15 input inserters
    iron_ins = [e for e in layout.entities
                if (e.meta.get("net") or "").startswith("b:iron")
                and e.meta.get("role") == "in"]
    assert len(iron_ins) == 15
