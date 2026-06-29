"""Emit a Factorio 2.0 blueprint (dict + importable string) from a Layout."""

from __future__ import annotations

from .encode import encode_blueprint_string
from .layout import (ASSEMBLER, CHEMICAL, CHEST_INPUT, FLUID_SOURCE, INPUT_FILL,
                     UNDERGROUND, Layout)

# A Factorio 2.0 version stamp (any recent 2.0 value works; FBSR ignores the patch).
VERSION = 562949954207746


def to_blueprint(layout: Layout, label: str = "fgr") -> dict:
    """Convert a :class:`Layout` into a Factorio blueprint dict."""
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
        if e.proto == CHEST_INPUT and e.item:
            ent["infinity_settings"] = {
                "remove_unfiltered_items": True,
                "filters": [{"index": 1, "name": e.item, "count": INPUT_FILL, "mode": "exactly"}],
            }
        if e.proto == FLUID_SOURCE and e.item:   # infinity-pipe: an infinite fluid source
            ent["infinity_settings"] = {"name": e.item, "percentage": 1.0, "mode": "at-least"}
        entities.append(ent)
    return {"blueprint": {"item": "blueprint", "label": label,
                          "version": VERSION, "entities": entities}}


def to_blueprint_string(layout: Layout, label: str = "fgr") -> str:
    return encode_blueprint_string(to_blueprint(layout, label))
