#!/usr/bin/env python
"""Refresh the `# STATUS (engine <sha>): ...` header each corner_cases/*.fgr file self-documents,
against the CURRENT v2 engine. Each file's second line records the pass/fail verdict at the commit
it was authored against; this keeps that claim honest as the generator changes (a failure the
corpus documented can get fixed -- the header should say so, not silently go stale).

    .venv/bin/python scripts/refresh_corner_case_status.py            # rewrite in place
    .venv/bin/python scripts/refresh_corner_case_status.py --check    # exit 1 if any header is stale
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fgr.dsl import parse                          # noqa: E402
from fgr.layout import compile_graph                # noqa: E402
from fgr.verify import verify                       # noqa: E402

STATUS_RE = re.compile(r"^# STATUS \(engine [0-9a-f]+\): .*$")


def _engine_sha() -> str:
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _verdict(path: Path) -> str:
    g = parse(path.read_text())
    rep = verify(g, compile_graph(g))
    if rep.ok:
        return "PASS (verifies)"
    fails = sorted(c.name for c in rep.checks if not c.ok)
    return f"FAIL -> {'; '.join(fails)}"


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    sha = _engine_sha()
    changed = []
    for path in sorted(ROOT.glob("corner_cases/**/*.fgr")):
        lines = path.read_text().splitlines(keepends=True)
        idx = next((i for i, ln in enumerate(lines) if STATUS_RE.match(ln.rstrip("\n"))), None)
        if idx is None:
            continue                                  # no STATUS header in this file -- skip
        new_line = f"# STATUS (engine {sha}): {_verdict(path)}\n"
        if lines[idx] != new_line:
            changed.append(path.relative_to(ROOT).as_posix())
            if not check_only:
                lines[idx] = new_line
                path.write_text("".join(lines))
    if changed:
        verb = "stale" if check_only else "refreshed"
        print(f"{len(changed)} {verb} STATUS header(s):")
        for c in changed:
            print(f"  {c}")
    else:
        print("all STATUS headers already current")
    return 1 if (check_only and changed) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
