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

Deterministic and non-fatal by design: a lattice pitched at 16 (2 tiles of overlap
slack) is snapped to free ground, gaps are patched per-entity, disconnected islands get
relay substations, and anything that still can't be placed is simply left for the
verifier to report -- the overlay never raises.
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


def _spots_covering(tiles):
    """All substation top-lefts whose supply would cover at least one of `tiles`."""
    out = set()
    for tx, ty in tiles:
        for sx in range(tx - 9, tx + 9):
            for sy in range(ty - 9, ty + 9):
                out.add((sx, sy))
    return out


def _wired(a, b):
    """Two substations (top-lefts) connect iff centre distance <= wire reach."""
    dx, dy = a[0] - b[0], a[1] - b[1]
    return dx * dx + dy * dy <= SUBSTATION_WIRE * SUBSTATION_WIRE


def add_power(layout: Layout) -> None:
    """Place substations + one EEI so the layout is fully powered. Mutates `layout`."""
    consumers = [e for e in layout.entities if e.proto in POWERED]
    if not consumers:
        return
    occ = {t for e in layout.entities for t in e.tiles()}

    def free(s):
        sx, sy = s
        return not ({(sx, sy), (sx + 1, sy), (sx, sy + 1), (sx + 1, sy + 1)} & occ)

    def claim(s):
        occ.update(((s[0], s[1]), (s[0] + 1, s[1]), (s[0], s[1] + 1), (s[0] + 1, s[1] + 1)))

    subs: list[tuple[int, int]] = []

    # 1) lattice over the build's bbox, each point snapped to nearby free ground and
    #    kept only if it covers something (no substations over empty plains).
    xs = [t[0] for t in occ]
    ys = [t[1] for t in occ]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    ctiles = [tuple(e.tiles()) for e in consumers]

    def snap(px, py):
        cand = [(px + dx, py + dy) for dx in range(-SNAP, SNAP + 1)
                for dy in range(-SNAP, SNAP + 1)]
        cand.sort(key=lambda s: (abs(s[0] - px) + abs(s[1] - py), s))
        for s in cand:
            if free(s):
                return s
        return None

    gy = y0
    while gy <= y1 + 8:
        gx = x0
        while gx <= x1 + 8:
            s = snap(gx, gy)
            if s is not None and any(_supply_covers(s, ts) for ts in ctiles):
                subs.append(s)
                claim(s)
            gx += PITCH
        gy += PITCH

    # 2) patch pass: cover anything the snapped lattice missed. Candidates prefer the
    #    spot covering the most still-uncovered entities, then nearness to the network.
    uncovered = [ts for ts in ctiles if not any(_supply_covers(s, ts) for s in subs)]
    while uncovered:
        target = uncovered[0]
        best = None
        for s in sorted(_spots_covering(target)):
            if not free(s):
                continue
            ncov = sum(1 for ts in uncovered if _supply_covers(s, ts))
            dnet = min((abs(s[0] - o[0]) + abs(s[1] - o[1]) for o in subs), default=0)
            key = (-ncov, dnet, s)
            if best is None or key < best[0]:
                best = (key, s)
        if best is None:
            uncovered.pop(0)               # nothing can cover it; verifier will say so
            continue
        subs.append(best[1])
        claim(best[1])
        uncovered = [ts for ts in uncovered if not _supply_covers(best[1], ts)]

    if not subs:
        return

    # 3) connectivity: bridge wire-disconnected islands with relay substations placed
    #    along the line between the closest cross-component pair.
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

    # 4) the EEI: a free 2x2 inside some substation's supply area, nearest that
    #    substation (generators connect by supply-area overlap, same as consumers).
    eei = None
    for s in subs:
        cand = [(s[0] + dx, s[1] + dy) for dx in range(-8, 9) for dy in range(-8, 9)]
        cand.sort(key=lambda t: (abs(t[0] - s[0]) + abs(t[1] - s[1]), t))
        for t in cand:
            if free(t) and _supply_covers(s, ((t[0], t[1]), (t[0] + 1, t[1] + 1))):
                eei = t
                break
        if eei is not None:
            break

    for s in subs:
        layout.add(PlacedEntity(SUBSTATION, s[0], s[1], meta={"role": "power"}))
    if eei is not None:
        layout.add(PlacedEntity(EEI, eei[0], eei[1], meta={"role": "power"}))
