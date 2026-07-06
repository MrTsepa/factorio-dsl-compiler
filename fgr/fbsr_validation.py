"""Validate the verifier's *model* against Factorio's real data, via FBSR.

The verifier (`fgr/verify.py`) decides whether items can flow A->B by reasoning
about geometry: how big each entity is, and — crucially — which tile an inserter
picks from vs. drops to. Those facts are assumptions, and a wrong assumption
silently makes the oracle agree with broken layouts (we hit exactly this: the
inserter `direction` quirk). So the assumptions themselves need an independent
check against ground truth.

FBSR embeds Factorio's actual `data.raw`. Its `dump-entity` command writes a
prototype's real fields — `pickup_position`, `insert_position`, `selection_box` —
to `build/<profile>/debug/`. This module reads those and asserts:

* the verifier's inserter rule (pick at ``T + DIR_DELTA[dir]``, drop at the
  opposite tile) reproduces Factorio's real rotated pickup/insert positions for
  *every* orientation; and
* each entity's tile footprint matches the generator/verifier ``SIZE`` table.

This is a *static* validation — FBSR renders, it does not simulate, so it cannot
prove throughput. But connectivity is a topological property derived from exactly
these geometric facts, so pinning them to real data closes the gap that caused the
inserter bug. (For a dynamic flow check you'd drive the real game over RCON; out
of scope here.)
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ir import DIR_DELTA, EAST, NORTH, OPPOSITE, SOUTH, WEST, Graph, Node, NodeKind
from .layout import (ASSEMBLER, CHEMICAL, CHEST_INPUT, CHEST_OUTPUT, FURNACE, INSERTER,
                     Layout, PlacedEntity, SIZE, _fluid_connections)
from .verify import verify

CARDINALS = [NORTH, EAST, SOUTH, WEST]
PROTOS = sorted(SIZE)  # the entities the compiler/verifier actually use

_DEFAULT_HOME = Path.home() / "Workspace" / "Factorio-FBSR" / "FactorioBlueprintStringRenderer"


class FbsrUnavailable(RuntimeError):
    """Raised when neither dumps nor the FBSR CLI are available to produce them."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def fbsr_home() -> Path:
    return Path(os.environ.get("FGR_FBSR_HOME", str(_DEFAULT_HOME)))


def _dump_dir(profile: str = "vanilla") -> Path:
    return fbsr_home() / "build" / profile / "debug"


def _round_vec(v) -> tuple[int, int]:
    return (round(v[0]), round(v[1]))


def _rotate_cw(v, quarter_turns: int) -> tuple[float, float]:
    """Rotate a (x, y) offset clockwise by N quarter turns (Factorio y points down)."""
    x, y = v
    for _ in range(quarter_turns % 4):
        x, y = -y, x
    return (x, y)


def _load_dump(name: str, kind: str, profile: str, dumper) -> dict:
    """Return a dumped prototype's JSON (kind = 'entity' | 'recipe' | ...), generating
    the dump on demand if needed."""
    ddir = _dump_dir(profile)
    pattern = str(ddir / f"{profile} {kind} {name} *.json")
    matches = sorted(glob.glob(pattern))
    if not matches and dumper is not None:
        dumper(name, kind)
        matches = sorted(glob.glob(pattern))
    if not matches:
        raise FbsrUnavailable(
            f"no {kind} dump for {name!r} in {ddir} and could not generate one")
    return json.load(open(matches[-1]))  # newest (e.g. highest version) wins


def _load_proto(proto: str, profile: str, dumper) -> dict:
    return _load_dump(proto, "entity", profile, dumper)


def _fbsr_dumper():
    """A callback that runs `fbsr.sh dump-<kind> <name>` to create missing dumps."""
    from .render import fbsr_script
    script = fbsr_script()
    if not script.exists():
        return None

    def dump(name: str, kind: str = "entity") -> None:
        subprocess.run(["bash", str(script), f"dump-{kind}", name],
                       capture_output=True, text=True, timeout=120)

    return dump


def validate(profile: str = "vanilla", dumper="auto") -> list[Check]:
    """Run all model checks against FBSR's data; return a list of :class:`Check`."""
    if dumper == "auto":
        dumper = _fbsr_dumper()
    checks: list[Check] = []

    # --- inserter pickup/drop semantics ---
    ins = _load_proto("inserter", profile, dumper)
    pickup = ins.get("pickup_position")
    insert = ins.get("insert_position")
    if pickup is None or insert is None:
        checks.append(Check("inserter exposes pickup/insert_position", False,
                            f"missing in dump: {sorted(ins)[:8]}..."))
        return checks

    bad = []
    for d in CARDINALS:
        exp_pick = DIR_DELTA[d]
        got_pick = _round_vec(_rotate_cw(pickup, d // 4))
        exp_drop = (-DIR_DELTA[d][0], -DIR_DELTA[d][1])
        got_drop = _round_vec(_rotate_cw(insert, d // 4))
        if got_pick != exp_pick:
            bad.append(f"dir {d}: real pickup {got_pick} != verifier {exp_pick}")
        if got_drop != exp_drop:
            bad.append(f"dir {d}: real drop {got_drop} != verifier {exp_drop}")
    checks.append(Check(
        "inserter picks at +direction, drops at -direction (all orientations)",
        not bad,
        f"Factorio base pickup={pickup}, insert={insert}" if not bad else "; ".join(bad)))

    # --- exercise the REAL verifier on a probe, not just the constants ---
    # Place chest -> inserter -> chest using Factorio's true pickup/insert tiles,
    # then assert verify() discovers the lane in the game-accurate direction. This
    # catches a sign regression inside verify.py itself, not only in DIR_DELTA.
    probe_bad = []
    for d in CARDINALS:
        pick_off = _round_vec(_rotate_cw(pickup, d // 4))
        drop_off = _round_vec(_rotate_cw(insert, d // 4))
        g = Graph()
        g.add_node(Node("A", NodeKind.INPUT, item="iron-plate"))
        g.add_node(Node("B", NodeKind.OUTPUT))
        g.add_edge("A", "B")
        lay = Layout([
            PlacedEntity(CHEST_INPUT, pick_off[0], pick_off[1], item="iron-plate", meta={"node": "A"}),
            PlacedEntity(CHEST_OUTPUT, drop_off[0], drop_off[1], meta={"node": "B"}),
            PlacedEntity(INSERTER, 0, 0, direction=d, meta={"role": "probe"}),
        ])
        found = verify(g, lay).lanes_found
        if found != {("A", "B")}:
            probe_bad.append(f"dir {d}: verifier saw {found or '{}'} , expected {{('A','B')}}")
    checks.append(Check("real verify() agrees with Factorio inserter flow (probe, all dirs)",
                        not probe_bad, "" if not probe_bad else "; ".join(probe_bad)))

    # --- entity footprints ---
    foot_bad = []
    for proto in PROTOS:
        d = _load_proto(proto, profile, dumper)
        box = d.get("selection_box") or d.get("collision_box")
        w = round(box[1][0] - box[0][0])
        h = round(box[1][1] - box[0][1])
        if (w, h) != SIZE[proto]:
            foot_bad.append(f"{proto}: real {w}x{h} != SIZE {SIZE[proto]}")
    checks.append(Check("entity footprints match the SIZE table", not foot_bad,
                        f"checked {', '.join(PROTOS)}" if not foot_bad else "; ".join(foot_bad)))

    # --- pipe-to-ground: its `direction` is the OPEN mouth; the underground side is the
    # opposite. (The compiler emits entrance=opposite-of-flow, exit=flow on this basis;
    # getting it backwards renders every tunnel reversed -- a real bug we hit.) ---
    ptg = _load_proto("pipe-to-ground", profile, dumper)
    conns = _fluid_box_connections(ptg)
    normal = [c for c in conns if c[3] != "underground"]
    under = [c for c in conns if c[3] == "underground"]
    ok = bool(normal and under) and under[0][2] == OPPOSITE[normal[0][2]]
    checks.append(Check(
        "pipe-to-ground tunnels opposite its open mouth", ok,
        f"open dir={normal[0][2] if normal else '?'}, underground dir={under[0][2] if under else '?'}"
        if ok else f"normal={normal}, underground={under}"))

    # --- machine fluid boxes match Factorio data (rotation-aware model). Both the
    # chemical plant and the assembler (crafting-with-fluid recipes) carry fluid. ---
    for proto_name, model in (("chemical-plant", CHEMICAL), ("assembling-machine-2", ASSEMBLER)):
        real = set()
        for pt, pos, dr, ct in _fluid_box_connections(_load_proto(proto_name, profile, dumper)):
            if ct == "underground":
                continue
            ext = (1 + pos[0] + DIR_DELTA[dr][0], 1 + pos[1] + DIR_DELTA[dr][1])  # center (1,1) for 3x3
            real.add((ext, "input" if pt == "input" else "output"))
        mine = set(_fluid_connections(model, 0, 0, NORTH))
        checks.append(Check(f"{proto_name} fluid boxes match Factorio data", real == mine,
                            f"{sorted(mine)}" if real == mine else f"real {sorted(real)} != model {sorted(mine)}"))
    return checks


# Which machine the DSL maps each recipe-bearing node kind to.
_MACHINE_PROTO = {NodeKind.ASSEMBLER: ASSEMBLER, NodeKind.CHEMICAL: CHEMICAL,
                  NodeKind.FURNACE: FURNACE}


def check_recipes(graph: Graph, profile: str = "vanilla", dumper="auto") -> list[Check]:
    """Spec-level check (NOT the layout): is each node's recipe actually craftable by the
    machine its DSL kind picks? Done NATIVELY from Factorio data -- a recipe's `category`
    must be in the machine's `crafting_categories` (no hard-coded recipe table) -- so e.g.
    a chemical-plant recipe placed on an `assembler` (or vice-versa) is flagged. Raises
    FbsrUnavailable if the game data can't be loaded at all (caller should treat as skip)."""
    if dumper == "auto":
        dumper = _fbsr_dumper()
    machine_cats: dict[str, set] = {}
    bad, unknown = [], []
    for name, node in graph.nodes.items():
        proto = _MACHINE_PROTO.get(node.kind)
        if proto is None or not node.recipe:
            continue
        if proto not in machine_cats:                       # may raise FbsrUnavailable (no data)
            machine_cats[proto] = set(_load_proto(proto, profile, dumper).get("crafting_categories", []))
        try:
            cat = _load_dump(node.recipe, "recipe", profile, dumper).get("category", "crafting")
        except FbsrUnavailable:
            unknown.append(f"{name}: recipe {node.recipe!r} not in {profile} data")
            continue
        if cat not in machine_cats[proto]:
            bad.append(f"{name}: {node.kind.value!r} can't craft {node.recipe!r} "
                       f"(category {cat!r} not in {sorted(machine_cats[proto])})")
    checks = [Check("every recipe is craftable by its machine (category vs crafting_categories)",
                    not bad, "" if not bad else "; ".join(bad))]
    if unknown:
        checks.append(Check("every recipe exists in Factorio data", False, "; ".join(unknown)))
    return checks


def check_ingredients(graph: Graph, profile: str = "vanilla", dumper="auto") -> list[Check]:
    """Spec-level check against REAL recipe data: every machine's incoming lanes must
    deliver exactly its recipe's ingredients, each on the right channel (items by belt
    `->`, fluids by pipe `~>`). This is the guardrail between "routes correctly" and
    "actually crafts in-game": the physical oracle proves items REACH the machine; this
    proves they're the items the recipe CONSUMES. Raises FbsrUnavailable if the game
    data can't be loaded (caller treats as skip)."""
    if dumper == "auto":
        dumper = _fbsr_dumper()

    def produces(name):
        """(product, type) a node delivers downstream, from real data where possible."""
        node = graph.nodes[name]
        if node.kind is NodeKind.INPUT:
            return node.item, "item"
        if node.kind is NodeKind.FLUID:
            return node.item, "fluid"
        if node.recipe:
            try:
                d = _load_dump(node.recipe, "recipe", profile, dumper)
            except FbsrUnavailable:
                return node.recipe, "item"          # unknown recipe: named after itself
            res = d.get("results") or d.get("products") or []
            if res:
                return res[0].get("name", node.recipe), res[0].get("type", "item")
            return node.recipe, "item"
        return None, None

    missing, extra, wrong_ch = [], [], []
    for name, node in graph.nodes.items():
        if _MACHINE_PROTO.get(node.kind) is None or not node.recipe:
            continue
        try:
            d = _load_dump(node.recipe, "recipe", profile, dumper)
        except FbsrUnavailable:
            continue                                 # existence flagged by check_recipes
        want = {(i["name"], i.get("type", "item")) for i in d.get("ingredients", [])}
        have = set()
        for e in graph.edges:
            if e.dst != name:
                continue
            prod, ptype = produces(e.src)
            if prod is None:
                continue
            ch = "fluid" if e.fluid else "item"
            have.add((prod, ch))
            if (prod, ptype) in want and ch != ptype:
                wrong_ch.append(f"{name}: {prod!r} must arrive by "
                                f"{'pipe ~>' if ptype == 'fluid' else 'belt ->'}")
        for ing, itype in sorted(want - have):
            missing.append(f"{name} ({node.recipe}): missing {itype} {ing!r}")
        for got, ch in sorted(have - want):
            extra.append(f"{name} ({node.recipe}): fed {got!r} which the recipe "
                         f"does not consume")
    checks = [Check("every machine is fed exactly its recipe's real ingredients",
                    not (missing or extra),
                    "" if not (missing or extra) else "; ".join(missing + extra))]
    if wrong_ch:
        checks.append(Check("ingredients arrive on the right channel (belt vs pipe)",
                            False, "; ".join(wrong_ch)))
    return checks


def _fluid_box_connections(dump):
    """Every fluid-box pipe connection in a prototype dump as
    (production_type, position, direction, connection_type)."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            if "pipe_connections" in o:
                pt = o.get("production_type")
                for c in o["pipe_connections"]:
                    out.append((pt, tuple(c.get("position", [0, 0])),
                                c.get("direction", 0), c.get("connection_type", "normal")))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(dump)
    return out


def format_checks(checks: list[Check]) -> str:
    lines = [f"  [{'ok  ' if c.ok else 'FAIL'}] {c.name}" + (f" — {c.detail}" if c.detail else "")
             for c in checks]
    lines.append(f"\n  => {'PASS' if all(c.ok for c in checks) else 'FAIL'}")
    return "\n".join(lines)
