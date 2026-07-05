"""Power overlay: substation lattice + one electric-energy-interface (EEI).

Makes a routed layout *runnable* when pasted into a sandbox/editor world: every powered
entity (machines, inserters, loaders) sits inside a substation supply area, the
substations form ONE wire-connected network, and an EEI on that network generates the
power (its vanilla defaults are a creative source -- no fuel, no steam). Shared by all
generators; the independent grade is fgr.verify._check_power.

Geometry (vanilla 2.0, cross-checked by fgr.fbsr_validation): substation 2x2 body,
18x18 supply area centred on the body, 18-tile wire reach between centres. An entity is
powered if ANY of its tiles overlaps a supply area; a generator (the EEI) feeds a
network under the same rule.

Deterministic and non-fatal by design. The reliable path (v3) PLANS the lattice before
routing -- plan_power() against machine-only occupancy, its tiles become router walls,
emit_power() materialises after routing. The legacy path (v1/v2) overlays post-routing
via add_power(), which can strand entities on dense layouts; either way nothing raises,
the verifier grades the result.
"""

from __future__ import annotations

from .layout import (ASSEMBLER, CHEMICAL, EEI, FURNACE, INSERTER, LOADER,
                     LONG_INSERTER, SUBSTATION, SUBSTATION_WIRE, Layout, PlacedEntity)

# Entities that need electric power. Loaders are included defensively: vanilla loaders
# draw no power, but covering them costs nothing (they sit next to covered chests).
POWERED = (ASSEMBLER, FURNACE, CHEMICAL, INSERTER, LONG_INSERTER, LOADER)

PITCH = 16          # lattice pitch: supply is 18 wide -> 2 tiles of overlap slack,
#                     and 16 <= wire reach 18 keeps orthogonal neighbours connected
SNAP = 3            # max Chebyshev nudge when snapping a lattice point to free ground


def _supply_covers(s, tiles):
    """Does the substation at top-left s cover ANY of the tiles? (18x18 area centred
    on the 2x2 body: x in [sx-8, sx+9], y in [sy-8, sy+9].)"""
    sx, sy = s
    return any(sx - 8 <= tx <= sx + 9 and sy - 8 <= ty <= sy + 9 for tx, ty in tiles)


def _wired(a, b):
    """Two substations (top-lefts) connect iff centre distance <= wire reach."""
    dx, dy = a[0] - b[0], a[1] - b[1]
    return dx * dx + dy * dy <= SUBSTATION_WIRE * SUBSTATION_WIRE


def plan_power(occ: set) -> list:
    """PRE-ROUTING power plan: substation top-lefts + one EEI spot, chosen against the
    machine-only occupancy `occ` (bodies + reserved fluid-box tiles) so the ground is
    still open. The caller must treat every returned tile as a WALL for the routers --
    belts/pipes go around or dive under (game entities never block underground runs).
    Planning before routing is what makes dense fields work: after routing, dense
    layouts have no free 2x2 left, and a post-hoc overlay strands entire regions.

    Returns [("sub", (x, y)), ..., ("eei", (x, y))]."""
    if not occ:
        return []
    occ = set(occ)

    def free(s):
        sx, sy = s
        return not ({(sx, sy), (sx + 1, sy), (sx, sy + 1), (sx + 1, sy + 1)} & occ)

    def claim(s):
        occ.update(((s[0], s[1]), (s[0] + 1, s[1]), (s[0], s[1] + 1), (s[0] + 1, s[1] + 1)))

    def snap(px, py):
        cand = [(px + dx, py + dy) for dx in range(-SNAP, SNAP + 1)
                for dy in range(-SNAP, SNAP + 1)]
        cand.sort(key=lambda s: (abs(s[0] - px) + abs(s[1] - py), s))
        for s in cand:
            if free(s):
                return s
        return None

    xs = [t[0] for t in occ]
    ys = [t[1] for t in occ]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)

    # full lattice, margined past the bbox: routing may wander ~12 tiles beyond the
    # machines (router bounds), and a tap inserter out there needs power too
    subs: list[tuple[int, int]] = []
    gy = y0 - 6
    while gy <= y1 + 8:
        gx = x0 - 6
        while gx <= x1 + 8:
            s = snap(gx, gy)
            if s is not None:
                subs.append(s)
                claim(s)
            gx += PITCH
        gy += PITCH
    if not subs:
        return []
    plan = _bridge(subs, free, claim)

    # the EEI: a free 2x2 in the first substation's supply area, nearest it
    s0 = plan[0]
    cand = [(s0[0] + dx, s0[1] + dy) for dx in range(-8, 9) for dy in range(-8, 9)]
    cand.sort(key=lambda t: (abs(t[0] - s0[0]) + abs(t[1] - s0[1]), t))
    out = [("sub", s) for s in plan]
    for t in cand:
        if free(t) and _supply_covers(s0, ((t[0], t[1]), (t[0] + 1, t[1] + 1))):
            out.append(("eei", t))
            break
    return out


def power_tiles(plan) -> set:
    """All tiles reserved by a plan_power() result (2x2 per entity)."""
    return {(x + dx, y + dy) for _k, (x, y) in plan for dx in (0, 1) for dy in (0, 1)}


def emit_power(layout: Layout, plan) -> None:
    """Materialise a plan_power() result into the layout."""
    for kind, (x, y) in plan:
        layout.add(PlacedEntity(SUBSTATION if kind == "sub" else EEI, x, y,
                                meta={"role": "power"}))


def _bridge(subs, free, claim):

    # bridge wire-disconnected islands with relay substations placed along the line
    # between the closest cross-component pair (snap can stretch a 16-pitch gap past
    # the 18 wire reach).
    def components():
        comp = {i: i for i in range(len(subs))}

        def find(i):
            while comp[i] != i:
                comp[i] = comp[comp[i]]
                i = comp[i]
            return i
        for i in range(len(subs)):
            for j in range(i + 1, len(subs)):
                if _wired(subs[i], subs[j]):
                    comp[find(i)] = find(j)
        groups: dict[int, list[int]] = {}
        for i in range(len(subs)):
            groups.setdefault(find(i), []).append(i)
        return list(groups.values())

    for _ in range(64):                     # bounded; each pass adds one relay
        groups = components()
        if len(groups) <= 1:
            break
        main = max(groups, key=len)
        rest = [i for g in groups if g is not main for i in g]
        a, b = min(((i, j) for i in main for j in rest),
                   key=lambda p: (subs[p[0]][0] - subs[p[1]][0]) ** 2
                                 + (subs[p[0]][1] - subs[p[1]][1]) ** 2)
        ax, ay = subs[a]
        bx, by = subs[b]
        dist = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5   # Euclidean: wire reach is
        step = min(PITCH, dist - 1) / dist if dist else 0  # radial, not Chebyshev
        ideal = (round(ax + (bx - ax) * step), round(ay + (by - ay) * step))
        cand = [(ideal[0] + dx, ideal[1] + dy) for dx in range(-SNAP, SNAP + 1)
                for dy in range(-SNAP, SNAP + 1)]
        cand.sort(key=lambda s: (abs(s[0] - ideal[0]) + abs(s[1] - ideal[1]), s))
        relay = next((s for s in cand if free(s) and _wired(subs[a], s)), None)
        if relay is None:
            break                            # can't bridge; verifier will report it
        subs.append(relay)
        claim(relay)
    return subs


def add_power(layout: Layout) -> None:
    """POST-ROUTING fallback overlay (used by the legacy v1/v2 generators): plan
    against whatever ground routing left and emit immediately. Dense layouts may not
    leave enough 2x2 ground -- v3 plans BEFORE routing instead (the reliable path)."""
    if not any(e.proto in POWERED for e in layout.entities):
        return
    plan = plan_power({t for e in layout.entities for t in e.tiles()})
    emit_power(layout, plan)
