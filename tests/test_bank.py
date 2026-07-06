"""The bank generator (fgr.layout_bank): template layouts must verify, and the
placed-layout flow oracle must carry the plan target. Skips without FBSR data."""
import pytest

from fgr.dsl import parse
from fgr.verify import verify


def _bank(text):
    from fgr.layout_bank import compile_bank
    from fgr.rates import RatesUnavailable
    try:
        return compile_bank(parse(text))
    except RatesUnavailable:
        pytest.skip("FBSR game data unavailable")


GREENCHIPS = "\n".join([
    "input copper : copper-plate",
    "input iron : iron-plate",
    "assembler cable : copper-cable",
    "assembler circuit : electronic-circuit",
    "output chips @ 15/s",
    "",
    "copper -> cable",
    "cable -> circuit",
    "iron -> circuit",
    "circuit -> chips",
])


def test_full_belt_of_circuits_bank():
    g2, plan, lay = _bank(GREENCHIPS)
    rep = verify(g2, lay)
    assert rep.ok, rep.format()
    # the whole point: a full yellow belt from a compact bank (~600 entities,
    # was 12k+ with point-to-point routing)
    assert len(lay.entities) < 800
    from fgr.flow import estimate
    est = estimate(g2, lay)
    assert sum(est["outputs_per_s"].values()) >= 15.0 - 1e-6


def test_gears_bank_verifies_and_carries_target():
    g2, plan, lay = _bank("\n".join([
        "input iron : iron-plate @ 1 belt",
        "assembler gears : iron-gear-wheel",
        "output out",
        "",
        "iron -> gears",
        "gears -> out",
    ]))
    assert verify(g2, lay).ok
    from fgr.flow import estimate
    est = estimate(g2, lay)
    assert sum(est["outputs_per_s"].values()) >= plan["target_per_s"]["out"] - 1e-6


def test_inapplicable_specs_fall_back():
    # fluids bank now (1- and 2-fluid stages); MULTI-OUTPUT specs are the stable
    # fallback shape (the bank template has exactly one collector boundary)
    from fgr.layout_bank import BankInapplicable, compile_bank
    from fgr.rates import RatesUnavailable
    multi_out = parse("\n".join([
        "input iron : iron-plate",
        "assembler gears : iron-gear-wheel",
        "assembler sticks : iron-stick",
        "output a @ 1/s",
        "output b @ 1/s",
        "",
        "iron -> gears, sticks",
        "gears -> a",
        "sticks -> b",
    ]))
    try:
        with pytest.raises(BankInapplicable):
            compile_bank(multi_out)
    except RatesUnavailable:
        pytest.skip("FBSR game data unavailable")


def test_single_fluid_stage_banks():
    g2, plan, lay = _bank("\n".join([
        "input iron : iron-plate",
        "input copper : copper-plate",
        "fluid acid : sulfuric-acid",
        "chemical battery : battery",
        "output out @ 0.5/s",
        "",
        "iron -> battery",
        "copper -> battery",
        "acid ~> battery",
        "battery -> out",
    ]))
    assert verify(g2, lay).ok
