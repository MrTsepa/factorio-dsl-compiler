#!/usr/bin/env python
"""Where does each compile path break as scale grows?

Two sweeps, one spec family (electronic circuits -- two stages, the canonical bank):

1. TARGET sweep through the normal pipeline: at what rate does the bank template
   hand off to the routed path, and where does the routed path stop being viable?
2. ROUTED-ONLY sweep (--no-bank equivalent): machine count vs compile time for the
   generic solver + v3 router, to locate the negotiation knee.

Each case runs subprocess-isolated with a timeout (a lesson the one-belt suite
taught). Results: out/scale_boundary.json + a printed table.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASE_TIMEOUT = 240

SPEC = """input copper : copper-plate
input iron : iron-plate
assembler cable : copper-cable
assembler circuit : electronic-circuit
output chips @ {rate}/s

copper -> cable
cable -> circuit
iron -> circuit
circuit -> chips
"""

TARGETS = [1, 3.75, 7.5, 15, 22.5, 30, 45]
ROUTED_TARGETS = [1, 2, 3, 4.5, 6, 7.5, 10, 15]


def run_one(rate: float, force_routed: bool) -> dict:
    from fgr.dsl import parse
    from fgr.flow import estimate
    from fgr.generators import compile_graph
    from fgr.layout_bank import BankInapplicable, compile_bank
    from fgr.solver import solve
    from fgr.verify import verify

    g = parse(SPEC.format(rate=rate))
    t0 = time.time()
    row: dict = {"rate": rate, "path": "routed" if force_routed else "auto"}
    lay = None
    if not force_routed:
        try:
            g2, plan, lay = compile_bank(g)
            row["mode"] = "bank"
        except BankInapplicable as e:
            row["bank_skip"] = str(e)[:70]
    if lay is None:
        g2, plan = solve(g)
        row["mode"] = "routed"
        row["nodes"] = len(g2.nodes)
        lay = compile_graph(g2)
    row["machines"] = sum(m["copies"] for m in plan["machines"].values())
    row["entities"] = len(lay.entities)
    row["compile_s"] = round(time.time() - t0, 1)
    rep = verify(g2, lay)
    row["verify"] = rep.ok
    if rep.ok:
        est = estimate(g2, lay)
        row["flow_per_s"] = round(sum(est["outputs_per_s"].values()), 2)
    return row


def sweep():
    rows = []
    for force_routed, rates in ((False, TARGETS), (True, ROUTED_TARGETS)):
        for rate in rates:
            try:
                proc = subprocess.run(
                    [sys.executable, __file__, "--one", str(rate),
                     "routed" if force_routed else "auto"],
                    capture_output=True, text=True, timeout=CASE_TIMEOUT)
                row = json.loads(proc.stdout.strip().splitlines()[-1])
            except subprocess.TimeoutExpired:
                row = {"rate": rate, "path": "routed" if force_routed else "auto",
                       "mode": "TIMEOUT", "compile_s": CASE_TIMEOUT}
            except Exception as e:                        # noqa: BLE001
                row = {"rate": rate, "path": "routed" if force_routed else "auto",
                       "mode": "ERROR", "error": str(e)[:100]}
            rows.append(row)
            print(f"{row['path']:6s} @ {rate:>5}/s: {row.get('mode', '?'):8s} "
                  f"m={row.get('machines', '-'):>4} e={row.get('entities', '-'):>5} "
                  f"t={row.get('compile_s', '-'):>5}s verify={row.get('verify', '-')} "
                  f"flow={row.get('flow_per_s', '-')}", flush=True)
    out = ROOT / "out" / "scale_boundary.json"
    out.write_text(json.dumps(rows, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    if "--one" in sys.argv:
        i = sys.argv.index("--one")
        rate, path = float(sys.argv[i + 1]), sys.argv[i + 2]
        try:
            print(json.dumps(run_one(rate, force_routed=(path == "routed"))))
        except Exception as e:                            # noqa: BLE001
            print(json.dumps({"rate": rate, "path": path, "mode": "ERROR",
                              "error": f"{type(e).__name__}: {e}"[:120]}))
        raise SystemExit(0)
    sweep()
