"""Stage A rate metadata (docs/RATES.md): known numbers from real game data.
Skips when FBSR game data is unavailable (rates are dump-driven, no tables)."""
import pytest

from fgr.dsl import parse
from fgr.generators import compile_graph
from fgr import rates


def _analyze(text):
    g = parse(text)
    lay = compile_graph(g)
    try:
        return rates.analyze(g, lay)
    except rates.RatesUnavailable:
        pytest.skip("FBSR game data unavailable")


def test_gears_rates_match_hand_math():
    rep = _analyze("\n".join([
        "input     iron  : iron-plate",
        "assembler gears : iron-gear-wheel",
        "output    out",
        "",
        "iron  -> gears",
        "gears -> out",
    ]))
    # am2 crafting_speed 0.75, gear recipe 0.5s -> 1.5 crafts/s
    assert rep["machines"]["gears"]["max_crafts_per_s"] == pytest.approx(1.5)
    assert rep["outputs"]["out"]["solo_max_per_s"] == pytest.approx(1.5)
    # the gear machine draws 3 iron/s; a single inserter arm (~0.84/s) is the
    # sustained ceiling: 1.5 * 0.84/3
    link = rep["links"]["iron->gears"]
    assert link["required_per_s"] == pytest.approx(3.0)
    assert 0.5 < link["capacity_per_s"] < 1.3          # one swing arm, from the dump
    sustained = rep["sustained_est_per_s"]["out"]
    assert sustained == pytest.approx(1.5 * link["capacity_per_s"] / 3.0, rel=0.05)


def test_red_science_solo_rate():
    rep = _analyze("\n".join([
        "input     copper : copper-plate",
        "input     iron   : iron-plate",
        "assembler gear   : iron-gear-wheel",
        "assembler red    : automation-science-pack",
        "output    out",
        "",
        "iron   -> gear",
        "copper -> red",
        "gear   -> red",
        "red    -> out",
    ]))
    # red science: 5s craft on a 0.75x assembler -> 0.15/s
    assert rep["outputs"]["out"]["solo_max_per_s"] == pytest.approx(0.15)
    assert rep["bottleneck"] == "red"
