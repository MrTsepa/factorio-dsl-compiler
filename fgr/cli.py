"""Command-line front-end: compile a .fgr file to a blueprint, verify, render.

    python -m fgr compile examples/gears.fgr            # -> blueprint string + verify
    python -m fgr compile examples/gears.fgr -o out.png # also render via FBSR
    python -m fgr verify  examples/gears.fgr            # just print the verifier report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .blueprint import to_blueprint_string
from .dsl import DslError, parse
from .layout import LayoutError, compile_graph
from .verify import verify


def _build(path: str):
    text = Path(path).read_text()
    graph = parse(text)
    layout = compile_graph(graph)
    return graph, layout


def cmd_compile(args) -> int:
    graph, layout = _build(args.source)
    report = verify(graph, layout)
    bp = to_blueprint_string(layout, label=Path(args.source).stem)

    print(f"# {args.source}: {len(graph.nodes)} nodes, {len(graph.edges)} lanes, "
          f"{len(layout.entities)} entities")
    print("\n## verification")
    print(report.format())
    print("\n## blueprint string")
    print(bp)

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
            return 0 if report.ok else 2
    return 0 if report.ok else 2


def cmd_verify(args) -> int:
    graph, layout = _build(args.source)
    report = verify(graph, layout)
    print(report.format())
    return 0 if report.ok else 2


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
    c.set_defaults(func=cmd_compile)

    v = sub.add_parser("verify", help="print the verifier report for a .fgr file")
    v.add_argument("source")
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
