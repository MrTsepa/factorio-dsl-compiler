"""A tiny dependency-free ASCII view of a Layout — for debugging the generator and as a
zero-setup preview when FBSR isn't installed. One glyph per tile.

    from fgr.ascii_render import ascii_layout
    print(ascii_layout(compile_graph(parse(src))))
"""
from __future__ import annotations

from .ir import EAST, NORTH, SOUTH, WEST
from .layout import (BELT, INSERTER, PIPE, PIPE_TO_GROUND, SPLITTER, UNDERGROUND, Layout)

_BELT = {EAST: ">", WEST: "<", NORTH: "^", SOUTH: "v", None: ">"}
_UG_IN = {EAST: "]", WEST: "[", NORTH: "i", SOUTH: "!", None: "]"}
_UG_OUT = {EAST: ")", WEST: "(", NORTH: ":", SOUTH: ".", None: ")"}
_INS = {EAST: "→", WEST: "←", NORTH: "↑", SOUTH: "↓", None: "→"}

LEGEND = ("legend: belt > < ^ v | ug-in ] [ i ! | ug-out ) ( : . | "
          "inserter →←↑↓ (->pickup) | splitter S | pipe + | pipe-ug o | "
          "loader L | substation % | EEI @ | body=Name | overlap X")


def _glyph(e) -> str:
    p = e.proto
    if p == BELT:
        return _BELT[e.direction]
    if p == UNDERGROUND:
        return (_UG_IN if e.ug_type == "input" else _UG_OUT)[e.direction]
    if p == INSERTER:
        return _INS[e.direction]
    if p == SPLITTER:
        return "S"
    if p == PIPE:
        return "+"
    if p == PIPE_TO_GROUND:
        return "o"
    if p == "long-handed-inserter":
        return _INS[e.direction]
    if p == "loader":
        return "L"
    if p == "substation":
        return "%"
    if p == "electric-energy-interface":
        return "@"
    # a body: use the node name's first char (upper), else '#'
    n = e.meta.get("node")
    return (n[0].upper() if n else "#")


def ascii_layout(layout: Layout, legend: bool = True) -> str:
    ents = layout.entities
    if not ents:
        return "(empty)"
    tiles = [(t, e) for e in ents for t in e.tiles()]
    xs = [t[0] for t, _ in tiles]
    ys = [t[1] for t, _ in tiles]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    grid = [[" "] * w for _ in range(h)]
    for (x, y), e in tiles:
        gx, gy = x - x0, y - y0
        cell = grid[gy][gx]
        g = _glyph(e)
        # body fill: only the body's own tiles get its letter; show overlaps as X
        grid[gy][gx] = "X" if cell not in (" ",) else g
    body = "\n".join("".join(row) for row in grid)
    head = f"[{w}x{h}] origin=({x0},{y0})"
    return f"{head}\n{body}" + (f"\n{LEGEND}" if legend else "")
