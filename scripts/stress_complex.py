#!/usr/bin/env python
"""Compile + verify a directory of .fgr graphs; report where the compiler breaks.

    .venv/bin/python scripts/stress_complex.py examples/complex            # table
    .venv/bin/python scripts/stress_complex.py --json examples/complex     # JSON
    .venv/bin/python scripts/stress_complex.py a.fgr b.fgr                  # specific files

For each PASS it writes the importable blueprint string to <dir>/bp/<name>.txt so
the layout can be rendered / independently audited.
"""
from __future__ import annotations

import json
import signal
import sys
import time
import traceback
from pathlib import Path

PER_CASE_TIMEOUT = 15   # seconds; a case that exceeds this is reported as SLOW


class _Timeout(Exception):
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fgr.dsl import DslError, parse                  # noqa: E402
from fgr.layout import LayoutError                   # noqa: E402
from fgr.generators import compile_graph             # noqa: E402
from fgr.verify import verify                        # noqa: E402
from fgr.blueprint import to_blueprint_string        # noqa: E402


def _extent(lay):
    xs = [t[0] for e in lay.entities for t in e.tiles()]
    ys = [t[1] for e in lay.entities for t in e.tiles()]
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) if xs else (0, 0)


def run_one(path: Path, bp_dir: Path) -> dict:
    name = path.stem
    rec = {"name": name, "status": "?", "nodes": 0, "edges": 0, "entities": 0,
           "grid": "", "ms": 0.0, "detail": "", "bp_path": ""}
    t0 = time.time()
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(_Timeout()))
    signal.alarm(PER_CASE_TIMEOUT)
    try:
        g = parse(path.read_text())
        rec["nodes"], rec["edges"] = len(g.nodes), len(g.edges)
        lay = compile_graph(g)
        rep = verify(g, lay)
        rec["ms"] = round((time.time() - t0) * 1000, 1)
        rec["entities"] = len(lay.entities)
        w, h = _extent(lay)
        rec["grid"] = f"{w}x{h}"
        if rep.ok:
            rec["status"] = "PASS"
            bp_dir.mkdir(parents=True, exist_ok=True)
            bp = bp_dir / f"{name}.txt"
            bp.write_text(to_blueprint_string(lay, name))
            rec["bp_path"] = str(bp)
        else:
            rec["status"] = "VERIFY_FAIL"
            rec["detail"] = "; ".join(c.name for c in rep.checks
                                      if not c.ok and c.severity == "error")
    except _Timeout:
        rec["status"], rec["detail"] = "SLOW", f">{PER_CASE_TIMEOUT}s"
        rec["ms"] = PER_CASE_TIMEOUT * 1000
    except DslError as e:
        rec["status"], rec["detail"] = "PARSE_ERROR", str(e)
    except LayoutError as e:
        rec["status"], rec["detail"] = "LAYOUT_ERROR", str(e)
        rec["ms"] = round((time.time() - t0) * 1000, 1)
    except Exception as e:  # noqa: BLE001
        rec["status"], rec["detail"] = "CRASH", f"{type(e).__name__}: {e}"
        rec["traceback"] = traceback.format_exc()
    finally:
        signal.alarm(0)
    return rec


def gather(args) -> list[Path]:
    files: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            files += sorted(p.glob("*.fgr"))
        elif p.suffix == ".fgr":
            files.append(p)
    return files


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    files = gather(argv)
    if not files:
        print("no .fgr files found", file=sys.stderr)
        return 1
    bp_dir = ROOT / "out" / "bp"        # blueprint strings of PASSes (gitignored artifacts)
    results = [run_one(f, bp_dir) for f in files]
    n_pass = sum(r["status"] == "PASS" for r in results)
    if as_json:
        print(json.dumps({"results": results, "passed": n_pass, "total": len(results)}))
        return 0
    print(f"{'case':32} {'nodes':>5} {'edges':>5} {'ents':>5} {'grid':>9} {'ms':>7}  outcome")
    print("-" * 100)
    for r in sorted(results, key=lambda r: r["name"]):
        out = r["status"] + (f": {r['detail']}" if r["detail"] else "")
        print(f"{r['name']:32} {r['nodes']:5} {r['edges']:5} {r['entities']:5} "
              f"{r['grid']:>9} {r['ms']:7.0f}  {out[:120]}")
    print("-" * 100)
    print(f"{n_pass}/{len(results)} PASS")
    return 0 if n_pass == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
