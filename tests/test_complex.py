"""Regression coverage for the complex multi-step / furnace / fluid features and the
fluid-correctness rules (mixing, underground reach) added while hardening the compiler.
These lock in behaviour the stress battery exercised so it can't silently regress."""
from pathlib import Path

import pytest

from fgr.dsl import parse
from fgr.layout import (FLUID_SOURCE, PIPE, PIPE_TO_GROUND, PIPE_UG_GAP, UG_MAX_GAP,
                        PlacedEntity, compile_graph)
from fgr.verify import verify

COMPLEX = Path(__file__).resolve().parents[1] / "examples" / "complex"


CURATED = sorted((COMPLEX.parent / "basic").glob("*.fgr")) + sorted(COMPLEX.glob("*.fgr"))


_V2_TAIL = set()   # all hand-authored complex factories now fully route


@pytest.mark.parametrize("path", sorted(COMPLEX.glob("*.fgr")), ids=lambda p: p.stem)
def test_complex_example_verifies(path):
    """Every hand-authored complex factory (deep chains, reconvergence, high fan-in,
    furnaces, oil/chem fluids) compiles to a layout the verifier accepts."""
    g = parse(path.read_text())
    rep = verify(g, compile_graph(g))
    if path.stem in _V2_TAIL:
        if rep.ok:
            pytest.fail(f"{path.stem} now PASSES -- remove from _V2_TAIL")
        pytest.xfail("v2 tail (not yet routed)")
    assert rep.ok, f"{path.stem} should verify:\n{rep.format()}"


@pytest.mark.parametrize("path", CURATED, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_curated_example_recipes_are_valid(path):
    """Every curated example (basic + complex) uses real recipes on the right machine,
    checked against live Factorio data. Skips if FBSR data isn't available."""
    from fgr import fbsr_validation as fv
    try:
        checks = fv.check_recipes(parse(path.read_text()))
    except fv.FbsrUnavailable:
        pytest.skip("FBSR data unavailable")
    assert all(c.ok for c in checks), [c.detail for c in checks if not c.ok]


MIX = """
fluid water : water
fluid steam : steam
chemical c : x
output tank
water ~> c
steam ~> c
c ~> tank
"""


def test_compiler_keeps_two_fluids_isolated():
    """Two different fluids into one plant get separate, non-touching pipe networks. (Fixed by the
    connection-aware fluid BFS: a link only steps off a net tile where its entity truly connects
    and won't tunnel over a foreign p2g, so steam's input net no longer welds into c's output)."""
    g = parse(MIX)
    assert verify(g, compile_graph(g)).ok


def test_verifier_detects_fluid_mixing():
    """Welding two different fluid networks into one makes that network carry two fluids
    -- physically broken, and the verifier must catch it (the blind spot the render audit
    found, which pure reachability misses). Bridge water's and steam's networks with a
    pipe run just west of the two stacked sources."""
    g = parse(MIX)
    lay = compile_graph(g)
    srcs = {e.item: e for e in lay.entities if e.proto == FLUID_SOURCE}
    w, s = srcs["water"], srcs["steam"]
    x = w.x - 1                                   # empty column west of both sources
    for y in range(min(w.y, s.y), max(w.y, s.y) + 1):
        lay.entities.append(PlacedEntity(PIPE, x, y, meta={"role": "pipe"}))
    rep = verify(g, lay)
    assert not rep.ok
    assert any("mixing" in c.name and not c.ok for c in rep.checks), rep.format()


def test_pipe_tunnel_reaches_farther_than_belt():
    """A pipe-to-ground pair connects underground up to PIPE_UG_GAP tiles -- farther than
    an underground belt (UG_MAX_GAP). Build water -> [9-tile tunnel] -> tank by hand
    (a span only pipes can bridge) and assert the verifier sees the fluid lane connect."""
    assert PIPE_UG_GAP > UG_MAX_GAP
    from fgr.ir import EAST, WEST
    from fgr.layout import TANK, Layout
    g = parse("fluid w : water\noutput t\nw ~> t")
    src = PlacedEntity(FLUID_SOURCE, 0, 0, item="water", meta={"node": "w"})
    tank = PlacedEntity(TANK, 15, 0, meta={"node": "t"})       # west box at (14, 0)
    pipes = [PlacedEntity(PIPE, 1, 0, meta={"role": "pipe"})]  # water box (east of source)
    # fluid flows EAST; a pipe-to-ground's `direction` is its OPEN mouth, so the entrance's
    # mouth faces back (WEST) and the exit's faces forward (EAST); they tunnel toward each other
    pipes.append(PlacedEntity(PIPE_TO_GROUND, 2, 0, direction=WEST, meta={"role": "pipe"}))
    pipes.append(PlacedEntity(PIPE_TO_GROUND, 11, 0, direction=EAST, meta={"role": "pipe"}))
    pipes += [PlacedEntity(PIPE, x, 0, meta={"role": "pipe"}) for x in (12, 13, 14)]
    rep = verify(g, Layout([src, tank, *pipes]))
    assert rep.ok, rep.format()                                # the 9-tile tunnel pairs


def test_recipe_machine_validity_against_factorio_data():
    """A spec check (from live Factorio data, no hard-coded recipes): a recipe's category
    must be in its machine's crafting_categories. Catches a chemistry recipe placed on an
    `assembler`, or a crafting recipe on a `chemical` plant. Skips if FBSR is unavailable."""
    from fgr import fbsr_validation as fv
    try:
        good = parse("input i : iron-plate\nassembler eu : electric-engine-unit\noutput o\n"
                     "i -> eu\neu -> o")                          # crafting-with-fluid -> assembler: ok
        ok = fv.check_recipes(good)
    except fv.FbsrUnavailable:
        pytest.skip("FBSR data unavailable")
    assert all(c.ok for c in ok), [c.detail for c in ok if not c.ok]
    bad = parse("input i : iron-plate\nassembler x : sulfuric-acid\noutput o\ni -> x\nx -> o")
    assert any(not c.ok for c in fv.check_recipes(bad))          # chemistry on an assembler -> flagged
