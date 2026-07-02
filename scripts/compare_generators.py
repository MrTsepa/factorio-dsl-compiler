#!/usr/bin/env python
"""Compare the layout generators (v1 search router / v2 lane fabric / v3 global
negotiated router) on the same test set: pass rate, quality metrics (area, fill%,
belt turns, tunnel crossings), and speed.

Runs each (file, generator) pair as an isolated subprocess with a timeout, because v1's A*
rip-up router can hang on large/congested graphs (that's the perf problem v2 was built to fix)
-- a hang or crash on one case must never take down the whole comparison.

    .venv/bin/python scripts/compare_generators.py                  # examples/ (49 cases)
    .venv/bin/python scripts/compare_generators.py --corner-cases    # + corner_cases/ (106 cases)
    .venv/bin/python scripts/compare_generators.py --generators=v2,v3   # subset head-to-head
    .venv/bin/python scripts/compare_generators.py --json > cmp.json
    .venv/bin/python scripts/compare_generators.py --markdown > cmp.md   # README-ready summary
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "_compare_worker.py"
GENERATORS = ("v1", "v2", "v3")
TIMEOUT_S = 20


def _run_one(path: Path, generator: str) -> dict:
    t0 = time.perf_counter()
    try:
        out = subprocess.run([sys.executable, str(WORKER), str(path), generator],
                             capture_output=True, text=True, timeout=TIMEOUT_S, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return {"path": str(path), "generator": generator, "ok": False,
               "timeout": True, "ms": round((time.perf_counter() - t0) * 1000)}
    if out.returncode != 0 or not out.stdout.strip():
        return {"path": str(path), "generator": generator, "ok": False,
               "error": (out.stderr or "worker crashed").strip()[-200:],
               "ms": round((time.perf_counter() - t0) * 1000)}
    return json.loads(out.stdout.strip().splitlines()[-1])


def _default_paths(corner_cases: bool) -> list[Path]:
    paths = sorted((ROOT / "examples").glob("*/*.fgr"))
    if corner_cases:
        paths += sorted((ROOT / "corner_cases").glob("*/*.fgr"))
    return paths


def run(paths: list[Path]) -> list[dict]:
    rows = []
    for i, p in enumerate(paths, 1):
        rel = p.relative_to(ROOT).as_posix()
        row = {"case": rel}
        for gen in GENERATORS:
            row[gen] = _run_one(p, gen)
        rows.append(row)
        marks = " ".join(
            f"{g}={'ok' if row[g].get('ok') else ('TO' if row[g].get('timeout') else 'FAIL'):4s}"
            for g in GENERATORS)
        print(f"[{i}/{len(paths)}] {rel:40s} {marks}", file=sys.stderr)
    return rows


def _summary(rows: list[dict]) -> dict:
    s = {}
    for gen in GENERATORS:
        oks = [r[gen] for r in rows if r[gen].get("ok")]
        timeouts = sum(1 for r in rows if r[gen].get("timeout"))
        s[gen] = {
            "pass": len(oks), "total": len(rows), "timeouts": timeouts,
            "avg_ms": round(sum(o["ms"] for o in oks) / len(oks)) if oks else None,
            "total_ents": sum(o["ents"] for o in oks),
            "avg_area": round(sum(o["area"] for o in oks) / len(oks)) if oks else None,
            "avg_fill": round(sum(o["fill"] for o in oks) / len(oks), 1) if oks else None,
            "total_turns": sum(o["turns"] for o in oks),
            "total_cross": sum(o["cross"] for o in oks),
        }
    return s


def _print_table(rows: list[dict]) -> None:
    hdr = (f"{'case':40s} " + " ".join(f"{g:>8s}" for g in GENERATORS)
           + "  " + " ".join(f"{g + ' ents':>9s}" for g in GENERATORS)
           + "  " + " ".join(f"{g + ' ms':>8s}" for g in GENERATORS))
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        cells = [r[g] for g in GENERATORS]
        marks = " ".join(
            f"{('ok' if c.get('ok') else ('TIMEOUT' if c.get('timeout') else 'FAIL')):>8s}"
            for c in cells)
        ents = " ".join(f"{c.get('ents', '-'):>9}" for c in cells)
        ms = " ".join(f"{c.get('ms', '-'):>8}" for c in cells)
        print(f"{r['case']:40s} {marks}  {ents}  {ms}")
    s = _summary(rows)
    print("-" * len(hdr))
    for gen in GENERATORS:
        d = s[gen]
        print(f"{gen}: {d['pass']}/{d['total']} pass, {d['timeouts']} timeouts, "
              f"avg {d['avg_ms']}ms, avg area {d['avg_area']}, avg fill {d['avg_fill']}%, "
              f"total turns {d['total_turns']}, total crossings {d['total_cross']}")


def _print_markdown(rows: list[dict]) -> None:
    s = _summary(rows)
    print(f"Compared on {len(rows)} cases (`examples/`"
          + (" + `corner_cases/`" if len(rows) > 49 else "") + f"), {TIMEOUT_S}s timeout per case.\n")
    print("| generator | pass rate | timeouts | avg compile | avg area | avg fill% | belt turns | tunnel crossings |")
    print("|---|---|---|---|---|---|---|---|")
    for gen in GENERATORS:
        d = s[gen]
        print(f"| {gen} | {d['pass']}/{d['total']} | {d['timeouts']} | {d['avg_ms']} ms | "
              f"{d['avg_area']} tiles | {d['avg_fill']}% | {d['total_turns']} | {d['total_cross']} |")


def main(argv: list[str]) -> int:
    global GENERATORS
    corner = "--corner-cases" in argv
    as_json = "--json" in argv
    as_md = "--markdown" in argv
    for a in argv:
        if a.startswith("--generators="):
            GENERATORS = tuple(a.split("=", 1)[1].split(","))
    args = [a for a in argv if not a.startswith("--")]
    paths = [ROOT / a for a in args] if args else _default_paths(corner)

    rows = run(paths)
    if as_json:
        print(json.dumps({"rows": rows, "summary": _summary(rows)}, indent=2))
    elif as_md:
        _print_markdown(rows)
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
