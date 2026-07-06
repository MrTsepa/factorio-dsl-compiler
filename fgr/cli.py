"""Command-line front-end: compile a .fgr file to a blueprint, verify, render.

    python -m fgr compile examples/basic/gears.fgr            # -> blueprint string + verify
    python -m fgr compile examples/basic/gears.fgr -o out.png # also render via FBSR
    python -m fgr compile examples/basic/gears.fgr -g v1      # use the v1 (search) generator
    python -m fgr verify  examples/basic/gears.fgr            # just print the verifier report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .blueprint import to_blueprint_string
from .dsl import DslError, parse
from .generators import DEFAULT as DEFAULT_GENERATOR
from .generators import GENERATORS
from .generators import compile_graph as _compile_graph
from .layout import LayoutError as LayoutErrorV2
from .layout_v1 import LayoutError as LayoutErrorV1
from .verify import verify

LayoutError = (LayoutErrorV1, LayoutErrorV2)


def _build(path: str, generator: str = DEFAULT_GENERATOR):
    text = Path(path).read_text()
    graph = parse(text)
    layout = _compile_graph(graph, generator)
    return graph, layout


def _recipe_lint(graph):
    """Print the recipe-vs-machine check (a SPEC check, from live Factorio data).
    Returns True if all ok, False on a mismatch, None if game data is unavailable."""
    from .fbsr_validation import FbsrUnavailable, check_recipes, format_checks
    try:
        checks = check_recipes(graph)
    except FbsrUnavailable:
        return None
    print("\n## recipe check (recipe category vs machine crafting_categories, from Factorio data)")
    print(format_checks(checks))
    return all(c.ok for c in checks)


def cmd_compile(args) -> int:
    graph, layout = _build(args.source, args.generator)
    report = verify(graph, layout)
    desc = None
    try:                                          # rates ride along in the in-game
        from .rates import analyze, summary_lines  # blueprint description (tooltip)
        desc = "\n".join(summary_lines(analyze(graph, layout)))
    except Exception:                             # noqa: BLE001 -- metadata only
        pass
    bp = to_blueprint_string(layout, label=Path(args.source).stem, description=desc)

    print(f"# {args.source}: {len(graph.nodes)} nodes, {len(graph.edges)} lanes, "
          f"{len(layout.entities)} entities")
    print("\n## verification")
    print(report.format())
    recipes_ok = _recipe_lint(graph)
    print("\n## blueprint string")
    print(bp)

    ok = report.ok and recipes_ok is not False    # recipes_ok None = data unavailable
    if args.bp_out:
        Path(args.bp_out).write_text(bp)
        print(f"\n(blueprint string written to {args.bp_out})")
    if args.out:
        from .render import RenderError, render_blueprint_string
        try:
            out = render_blueprint_string(bp, args.out)
            print(f"\n(rendered to {out})")
        except RenderError as exc:
            print(f"\n[render skipped] {exc}", file=sys.stderr)
    return 0 if ok else 2


def cmd_verify(args) -> int:
    graph, layout = _build(args.source, args.generator)
    report = verify(graph, layout)
    print(report.format())
    recipes_ok = _recipe_lint(graph)
    return 0 if report.ok and recipes_ok is not False else 2


def cmd_rates(args) -> int:
    graph, layout = _build(args.source, args.generator)
    from .rates import RatesUnavailable, analyze, summary_lines
    try:
        rep = analyze(graph, layout)
    except RatesUnavailable as e:
        print(f"rates unavailable (needs FBSR game data): {e}", file=sys.stderr)
        return 1
    if args.json:
        import json
        print(json.dumps(rep, indent=2))
    else:
        print(f"# {args.source} — steady-state rate estimate (docs/RATES.md)")
        for ln in summary_lines(rep):
            print(f"  {ln}")
        print("\n## links (required vs capacity at the all-outputs operating point)")
        for k, v in sorted((rep.get("links") or {}).items()):
            cap = v["capacity_per_s"]
            u = f" — {int(v['utilization'] * 100)}%" if v.get("utilization") else ""
            print(f"  {k}: {v['required_per_s']}/s over {v.get('via', 'pipe')} "
                  f"(cap {cap if cap is not None else 'unbounded'}/s){u}")
    return 0


def cmd_solve(args) -> int:
    import json as _json
    text = Path(args.source).read_text()
    graph = parse(text)
    from .solver import SolveError, solve
    from .rates import RatesUnavailable
    try:
        g2, plan = solve(graph)
    except (SolveError, RatesUnavailable) as e:
        print(f"solve failed: {e}", file=sys.stderr)
        return 1
    print(f"# {args.source} — sizing plan")
    print(_json.dumps(plan, indent=2))
    layout = _compile_graph(g2, args.generator)
    report = verify(g2, layout)
    print(f"\n## sized layout: {len(layout.entities)} entities — "
          f"{'VERIFIES' if report.ok else 'FAILS VERIFICATION'}")
    if not report.ok:
        print(report.format())
        return 1
    desc = "sized by fgr solve: " + ", ".join(
        f"{o} >= {t}/s (expect ~{plan['expected_actual_per_s'].get(o)}/s)"
        for o, t in plan["target_per_s"].items())
    print("\n## blueprint string")
    print(to_blueprint_string(layout, label=Path(args.source).stem, description=desc))
    return 0


def cmd_validate_model(args) -> int:
    """Validate the verifier's geometric assumptions against Factorio data (via FBSR)."""
    from .fbsr_validation import FbsrUnavailable, format_checks, validate
    try:
        checks = validate()
    except FbsrUnavailable as exc:
        print(f"[skipped] {exc}", file=sys.stderr)
        return 0
    print("## verifier model vs. Factorio data (FBSR dump-entity)")
    print(format_checks(checks))
    return 0 if all(c.ok for c in checks) else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fgr", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compile", help="compile a .fgr file to a blueprint")
    c.add_argument("source")
    c.add_argument("-o", "--out", help="render the layout to this PNG (needs FBSR service)")
    c.add_argument("--bp-out", help="write the raw blueprint string to this file")
    c.add_argument("-g", "--generator", choices=sorted(GENERATORS), default=DEFAULT_GENERATOR,
                   help=f"layout generator to use (default: {DEFAULT_GENERATOR})")
    c.set_defaults(func=cmd_compile)

    s = sub.add_parser("solve", help="size a factory to its @rate annotations -> sized blueprint")
    s.add_argument("source")
    s.add_argument("-g", "--generator", choices=sorted(GENERATORS), default=DEFAULT_GENERATOR)
    s.set_defaults(func=cmd_solve)

    r = sub.add_parser("rates", help="steady-state throughput estimate for a .fgr file")
    r.add_argument("source")
    r.add_argument("-g", "--generator", choices=sorted(GENERATORS), default=DEFAULT_GENERATOR)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_rates)

    v = sub.add_parser("verify", help="print the verifier report for a .fgr file")
    v.add_argument("source")
    v.add_argument("-g", "--generator", choices=sorted(GENERATORS), default=DEFAULT_GENERATOR,
                   help=f"layout generator to use (default: {DEFAULT_GENERATOR})")
    v.set_defaults(func=cmd_verify)

    m = sub.add_parser("validate-model",
                       help="check the verifier's geometry against Factorio data (via FBSR)")
    m.set_defaults(func=cmd_validate_model)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (DslError, LayoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
