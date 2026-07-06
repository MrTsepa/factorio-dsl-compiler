#!/usr/bin/env python
"""Collect the data for the throughput deep-dive (docs/rate_analysis.html):

* MICRO-BENCHMARKS -- hand-built layouts isolating ONE mechanism each (pure inserter
  swing, long-handed swing, loader+belt path, positional contention on a tapped belt),
  measured in the real game. These calibrate the primitive capacities empirically.
* CORPUS RUNS -- full factories (gears, circuits, science_3) with complete per-second
  series for the measurement-methodology analysis.

Writes one JSON per experiment into out/rate_study/. Run scripts/get_factorio.sh first.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fgr.ir import EAST, NORTH, WEST  # noqa: E402
from fgr.layout import (BELT, CHEST_INPUT, CHEST_OUTPUT, EEI, INSERTER, LOADER,  # noqa: E402
                        LONG_INSERTER, SUBSTATION, Layout, PlacedEntity)
from fgr.blueprint import to_blueprint_string  # noqa: E402
from simulate import simulate, simulate_bp  # noqa: E402

OUT = ROOT / "out" / "rate_study"


def _powered(entities):
    """Wrap a hand-built entity list with power (sub + EEI) into a Layout."""
    lay = Layout()
    for e in entities:
        lay.add(e)
    lay.add(PlacedEntity(SUBSTATION, 0, -6, meta={"role": "power"}))
    lay.add(PlacedEntity(EEI, 3, -6, meta={"role": "power"}))
    return lay


def micro_pure_inserter():
    """infinity chest -> ONE inserter -> steel chest: the raw swing rate, nothing else."""
    return _powered([
        PlacedEntity(CHEST_INPUT, 0, 0, item="iron-plate", meta={"node": "src"}),
        PlacedEntity(INSERTER, 1, 0, direction=WEST, meta={}),   # picks west, drops east
        PlacedEntity(CHEST_OUTPUT, 2, 0, meta={"node": "dst"}),
    ])


def micro_long_inserter():
    """Same, with a long-handed inserter (reach 2)."""
    return _powered([
        PlacedEntity(CHEST_INPUT, 0, 0, item="iron-plate", meta={"node": "src"}),
        PlacedEntity(LONG_INSERTER, 2, 0, direction=WEST, meta={}),
        PlacedEntity(CHEST_OUTPUT, 4, 0, meta={"node": "dst"}),
    ])


def micro_loader_belt():
    """infinity chest -> loader -> 6 belt tiles -> loader -> steel chest: the full-belt path."""
    ents = [
        PlacedEntity(CHEST_INPUT, 0, 0, item="iron-plate", meta={"node": "src"}),
        PlacedEntity(LOADER, 1, 0, direction=EAST, loader_type="output", meta={}),
    ]
    for x in range(3, 9):
        ents.append(PlacedEntity(BELT, x, 0, direction=EAST, meta={}))
    ents += [
        PlacedEntity(LOADER, 9, 0, direction=EAST, loader_type="input", meta={}),
        PlacedEntity(CHEST_OUTPUT, 11, 0, meta={"node": "dst"}),
    ]
    return _powered(ents)


def micro_inserter_from_belt():
    """loader-fed compressed belt -> inserter -> steel chest: swing rate when the
    pickup is a moving belt instead of a chest (the common machine-feed shape)."""
    ents = [
        PlacedEntity(CHEST_INPUT, 0, 0, item="iron-plate", meta={"node": "src"}),
        PlacedEntity(LOADER, 1, 0, direction=EAST, loader_type="output", meta={}),
    ]
    for x in range(3, 7):
        ents.append(PlacedEntity(BELT, x, 0, direction=EAST, meta={}))
    ents += [
        PlacedEntity(INSERTER, 6, 1, direction=NORTH, meta={}),  # picks belt, drops south
        PlacedEntity(CHEST_OUTPUT, 6, 2, meta={"node": "dst"}),
    ]
    return _powered(ents)


CONTENTION_FGR = "\n".join([
    "# positional contention: one gear line tapped by TWO identical consumers.",
    "# Which one starves reveals the game's allocation rule (upstream tap wins?).",
    "input iron : iron-plate",
    "input iron2 : iron-plate",
    "assembler gear : iron-gear-wheel",
    "assembler belt1 : transport-belt",
    "assembler belt2 : transport-belt",
    "output out1",
    "output out2",
    "",
    "iron -> gear",
    "gear -> belt1, belt2",
    "iron2 -> belt1, belt2",
    "belt1 -> out1",
    "belt2 -> out2",
])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    micros = {
        "micro_pure_inserter": (micro_pure_inserter(), 7200),
        "micro_long_inserter": (micro_long_inserter(), 7200),
        "micro_loader_belt": (micro_loader_belt(), 3600),
        "micro_inserter_from_belt": (micro_inserter_from_belt(), 7200),
    }
    for name, (lay, ticks) in micros.items():
        print(f"== {name} ({ticks} ticks)")
        data = simulate_bp(to_blueprint_string(lay, label=name), ticks)
        (OUT / f"{name}.json").write_text(json.dumps(data))

    contention = ROOT / "out" / "rate_study" / "contention.fgr"
    contention.write_text(CONTENTION_FGR)
    corpus = {
        "gears": (ROOT / "examples/basic/gears.fgr", 14400),
        "circuits": (ROOT / "examples/basic/circuits.fgr", 14400),
        "contention": (contention, 21600),
        "science_3": (ROOT / "examples/stress/science_3.fgr", 108000),
    }
    for name, (path, ticks) in corpus.items():
        print(f"== {name} ({ticks} ticks)")
        data = simulate(path, ticks)
        (OUT / f"{name}.json").write_text(json.dumps(data))
    print(f"study data in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
