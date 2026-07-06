#!/usr/bin/env python
"""Audit every .fgr spec against real Factorio data (via FBSR): every referenced recipe is
craftable by the machine its DSL kind picks, and every recipe / input item / fluid actually
exists in the data. No hard-coded tables -- it queries dump-recipe / dump-item / dump-fluid.

    .venv/bin/python scripts/audit_specs.py [dir ...]      # default: examples/
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fgr.dsl import parse                                   # noqa: E402
from fgr.ir import NodeKind                                 # noqa: E402
from fgr import fbsr_validation as fv                       # noqa: E402


def audit(path: Path, dumper) -> list[str]:
    g = parse(path.read_text())
    issues = []
    for c in (fv.check_recipes(g, dumper=dumper)            # recipe ↔ machine + existence
              + fv.check_ingredients(g, dumper=dumper)):    # feeds == real ingredients
        if not c.ok and c.detail:
            issues += [s.strip() for s in c.detail.split(";")]
    for name, node in g.nodes.items():                      # input items / fluid sources exist?
        ref, kind = (node.item, "item") if node.kind is NodeKind.INPUT else \
                    (node.item, "fluid") if node.kind is NodeKind.FLUID else (None, None)
        if ref:
            try:
                fv._load_dump(ref, kind, "vanilla", dumper)
            except fv.FbsrUnavailable:
                issues.append(f"{name}: {kind} {ref!r} not in Factorio data")
    return issues


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    roots = [Path(a) for a in argv] or [ROOT / "examples"]
    files = sorted(f for r in roots for f in ([r] if r.suffix == ".fgr" else r.rglob("*.fgr")))
    dumper = fv._fbsr_dumper()
    if dumper is None:
        print("FBSR unavailable — cannot audit against Factorio data", file=sys.stderr)
        return 1
    clean, flagged = 0, 0
    for f in files:
        try:
            issues = audit(f, dumper)
        except fv.FbsrUnavailable as e:
            print(f"{f.relative_to(ROOT)}: data unavailable ({e})"); continue
        if issues:
            flagged += 1
            print(f"\n{f.relative_to(ROOT)}")
            for i in issues:
                print(f"  - {i}")
        else:
            clean += 1
    print(f"\n==== {clean} clean, {flagged} with issues, of {len(files)} specs ====")
    return 0 if flagged == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
