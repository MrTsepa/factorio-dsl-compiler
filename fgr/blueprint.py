"""Emit a Factorio 2.0 blueprint (dict + importable string) from a Layout."""

from __future__ import annotations

from .encode import encode_blueprint_string
from .layout import (ASSEMBLER, CHEMICAL, CHEST_INPUT, FLUID_SOURCE, INPUT_FILL,
                     LOADER, SUBSTATION, SUBSTATION_WIRE, UNDERGROUND, Layout)

# A Factorio 2.0 version stamp (any recent 2.0 value works; FBSR ignores the patch).
VERSION = 562949954207746


def to_blueprint(layout: Layout, label: str = "fgr", description: str | None = None) -> dict:
    """Convert a :class:`Layout` into a Factorio blueprint dict. ``description`` shows
    in the in-game blueprint tooltip -- we use it for the expected-rates metadata."""
    entities = []
    for i, e in enumerate(layout.entities, start=1):
        cx, cy = e.center
        ent: dict = {"entity_number": i, "name": e.proto,
                     "position": {"x": cx, "y": cy}}
        if e.direction:  # 0 (North) is the default and omitted by convention
            ent["direction"] = e.direction
        if e.proto in (ASSEMBLER, CHEMICAL) and e.recipe:
            ent["recipe"] = e.recipe
        if e.proto == UNDERGROUND and e.ug_type:
            ent["type"] = e.ug_type  # "input" = entrance, "output" = exit
        if e.proto == LOADER and e.loader_type:
            ent["type"] = e.loader_type  # "output" = container->belt, "input" = belt->container
        if e.proto == CHEST_INPUT and e.item:
            ent["infinity_settings"] = {
                "remove_unfiltered_items": True,
                "filters": [{"index": 1, "name": e.item, "count": INPUT_FILL, "mode": "exactly"}],
            }
        if e.proto == FLUID_SOURCE and e.item:   # infinity-pipe: an infinite fluid source
            ent["infinity_settings"] = {"name": e.item, "percentage": 1.0, "mode": "at-least"}
        entities.append(ent)
    bp = {"blueprint": {"item": "blueprint", "label": label,
                        "version": VERSION, "entities": entities}}
    if description:
        bp["blueprint"]["description"] = description
    wires = _pole_wires(layout)
    if wires:
        bp["blueprint"]["wires"] = wires
    return bp


def _pole_wires(layout: Layout) -> list:
    """Explicit copper-wire connections between substations (2.0 blueprint format:
    top-level `wires` = [[ent_a, connector_a, ent_b, connector_b], ...], connector 5 =
    pole copper). Factorio 2.0 revives blueprint poles with EXACTLY these wires -- and
    FBSR draws a pole's sprite only when it has at least one -- so without this array
    the pasted grid is dead metal and the render shows nothing. A minimum spanning
    tree over the wire-reach graph keeps the network exactly as connected as the
    verifier's distance model says, without a visual cobweb."""
    poles = [(i, e) for i, e in enumerate(layout.entities, start=1)
             if e.proto == SUBSTATION]
    if len(poles) < 2:
        return []
    edges = []
    for a in range(len(poles)):
        for b in range(a + 1, len(poles)):
            (ia, ea), (ib, eb) = poles[a], poles[b]
            d2 = (ea.x - eb.x) ** 2 + (ea.y - eb.y) ** 2
            if d2 <= SUBSTATION_WIRE * SUBSTATION_WIRE:
                edges.append((d2, ia, ib))
    edges.sort()
    parent = {i: i for i, _e in poles}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    wires = []
    for _d2, ia, ib in edges:                    # Kruskal: one MST per component
        ra, rb = find(ia), find(ib)
        if ra != rb:
            parent[ra] = rb
            wires.append([ia, 5, ib, 5])
    return wires


def to_blueprint_string(layout: Layout, label: str = "fgr",
                        description: str | None = None) -> str:
    return encode_blueprint_string(to_blueprint(layout, label, description))
