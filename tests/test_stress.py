"""Stress cases: hard graphs (wide multi-in/out, deep chains, dense many-to-many,
large fan-out/merge, ratio builds) must all compile to a layout the verifier passes.
These guard the rip-up router + perimeter ports + position-agnostic manifolds."""
import pytest

from fgr.dsl import parse
from fgr.layout import compile_graph
from fgr.verify import verify

REC, ITEM = "iron-gear-wheel", "iron-plate"


def _multi_in_dedicated(k):
    return "\n".join([f"input in{i} : {ITEM}" for i in range(k)] + [f"assembler asm : {REC}", "output out"]
                     + [f"in{i} -> asm" for i in range(k)] + ["asm -> out"])


def _multi_in_merged(k):
    return "\n".join([f"input in{i} : {ITEM}" for i in range(k)] + [f"assembler asm : {REC}", "output out"]
                     + [", ".join(f"in{i}" for i in range(k)) + " -> asm", "asm -> out"])


def _fanout(k):
    return "\n".join(["input src : " + ITEM] + [f"output o{i}" for i in range(k)]
                     + ["src -> " + ", ".join(f"o{i}" for i in range(k))])


def _deep_chain(k):
    return "\n".join(["input src : " + ITEM] + [f"assembler a{i} : {REC}" for i in range(k)] + ["output out"]
                     + ["src -> a0"] + [f"a{i} -> a{i+1}" for i in range(k - 1)] + [f"a{k-1} -> out"])


def _wide_parallel(k):
    L = []
    for i in range(k):
        L += [f"input in{i} : {ITEM}", f"assembler a{i} : {REC}", f"output o{i}",
              f"in{i} -> a{i}", f"a{i} -> o{i}"]
    return "\n".join(L)


def _bipartite(m, n):
    L = ["input feed : " + ITEM] + [f"assembler s{i} : {REC}" for i in range(m)] \
        + [f"assembler c{j} : {REC}" for j in range(n)] + [f"output out{j}" for j in range(n)]
    L += ["feed -> " + ", ".join(f"s{i}" for i in range(m))]
    L += [f"s{i} -> c{j}" for i in range(m) for j in range(n)]
    L += [f"c{j} -> out{j}" for j in range(n)]
    return "\n".join(L)


def _ratio():
    L = ["input copper : copper-plate", "input iron : iron-plate"] \
        + [f"assembler cable{i} : copper-cable" for i in range(3)] \
        + [f"assembler circ{j} : electronic-circuit" for j in range(2)] \
        + ["output chips0", "output chips1", "copper -> cable0, cable1, cable2"]
    L += [f"cable{i} -> circ0, circ1" for i in range(3)]
    L += ["iron -> circ0, circ1", "circ0 -> chips0", "circ1 -> chips1"]
    return "\n".join(L)


def _full_ports():
    return "\n".join([f"input in{i} : {ITEM}" for i in range(3)] + ["assembler hub : " + REC]
                     + [f"output o{i}" for i in range(3)]
                     + [f"in{i} -> hub" for i in range(3)] + [f"hub -> o{i}" for i in range(3)])


CASES = {
    "multi_in_dedicated[5]": _multi_in_dedicated(5),   # 5 inputs -> perimeter ports (all sides)
    "multi_in_merged[5]": _multi_in_merged(5),
    "fanout[8]": _fanout(8),
    "deep_chain[15]": _deep_chain(15),
    "wide_parallel[12]": _wide_parallel(12),
    "bipartite[3x3]": _bipartite(3, 3),               # dense many-to-many -> rip-up router
    "bipartite[4x4]": _bipartite(4, 4),
    "ratio_3wire_2circuit": _ratio(),                 # source-below-consumer fan-out
    "full_ports_3x3": _full_ports(),
}


@pytest.mark.parametrize("name", list(CASES))
def test_stress_case_verifies(name):
    g = parse(CASES[name])
    report = verify(g, compile_graph(g))
    assert report.ok, f"{name} should verify:\n{report.format()}"
    assert report.lanes_found == {(e.src, e.dst) for e in g.edges}


def test_determinism_on_a_hard_case():
    from fgr.blueprint import to_blueprint_string
    a = to_blueprint_string(compile_graph(parse(_bipartite(3, 3))))
    b = to_blueprint_string(compile_graph(parse(_bipartite(3, 3))))
    assert a == b
