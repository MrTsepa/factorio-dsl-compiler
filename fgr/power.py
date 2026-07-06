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


def plan_power(occ: set, cover: set | None = None,
               eei_hint: tuple | None = None) -> list:
    """PRE-ROUTING power plan: substation top-lefts + one EEI spot, chosen against the
    machine-only occupancy `occ` (bodies + reserved fluid-box tiles) so the ground is
    still open. The caller must treat every returned tile as a WALL for the routers --
    belts/pipes go around or dive under (game entities never block underground runs).
    Planning before routing is what makes dense fields work: after routing, dense
    layouts have no free 2x2 left, and a post-hoc overlay strands entire regions.

    ``cover`` is the set of tiles whose coverage the plan must guarantee (defaults to
    ``occ``); ``eei_hint`` is the preferred EEI spot (top-left). Convention: just WEST
    of the build at the input row -- power feeds in alongside the raw materials,
    outside the fabric but not off in a corner. The EEI lands as close to the hint as
    a free, supply-covered 2x2 allows.

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

    # GREEDY SET COVER, not a blanket lattice (which ringed small builds with twice
    # the substations they need): the coverage target is `cover`, candidates sit on a
    # half-pitch grid snapped to free ground, and each round keeps the spot covering
    # the most still-uncovered target tiles. A rare consumer landing outside `cover`
    # during routing is handled post-routing by patch_power().
    if cover is None:
        cover = set(occ)
    todo = set(cover)
    # candidates: a half-pitch grid over the field PLUS lines through the cover's
    # centre axes -- a strip-shaped build is covered by ONE centred row of
    # substations, which the coarse grid alone can't express
    cxs = [t[0] for t in cover]
    cys = [t[1] for t in cover]
    mid_x = (min(cxs) + max(cxs)) // 2 - 1
    mid_y = (min(cys) + max(cys)) // 2 - 1
    cand: list = []
    seen: set = set()

    def consider(px, py):
        s = snap(px, py)
        if s is not None and s not in seen:
            seen.add(s)
            cand.append(s)

    # centre-line candidates put substations ON the main corridors: lovely for small
    # builds (one straight row covers a strip factory), but on big fields they choke
    # the router's busiest ground and negotiation rounds get seconds slower -- gate
    # them by build size
    mid_lines = (x1 - x0) * (y1 - y0) <= 2500
    gy = y0 - 8
    while gy <= y1 + 8:
        gx = x0 - 8
        while gx <= x1 + 8:
            consider(gx, gy)
            if mid_lines:
                consider(gx, mid_y)
                consider(mid_x, gy)
            gx += PITCH // 2
        gy += PITCH // 2

    # bucket candidates on a 9-tile grid: each todo tile touches O(1) buckets, so a
    # pick is one linear pass over the remaining tiles (the naive candidates x tiles
    # product took ~25s on the scale_* fields)
    buckets: dict = {}
    for i, s in enumerate(cand):
        buckets.setdefault((s[0] // 9, s[1] // 9), []).append(i)
    alive = [True] * len(cand)

    def covering(t):
        tx, ty = t
        for bx in range((tx - 9) // 9, (tx + 8) // 9 + 1):
            for by in range((ty - 9) // 9, (ty + 8) // 9 + 1):
                for i in buckets.get((bx, by), ()):
                    sx, sy = cand[i]
                    if alive[i] and sx - 8 <= tx <= sx + 9 and sy - 8 <= ty <= sy + 9:
                        yield i

    subs: list[tuple[int, int]] = []
    while todo:
        gain: dict = {}
        for t in todo:
            for i in covering(t):
                gain[i] = gain.get(i, 0) + 1
        if not gain:
            break                              # leftovers have no reachable candidate
        i = max(gain, key=lambda i: (gain[i], -cand[i][1], -cand[i][0]))
        alive[i] = False
        best = cand[i]
        if not free(best):                     # an earlier claim took this ground
            continue
        subs.append(best)
        claim(best)
        todo = {t for t in todo
                if not (best[0] - 8 <= t[0] <= best[0] + 9
                        and best[1] - 8 <= t[1] <= best[1] + 9)}
    if not subs:
        return []
    plan = _bridge(subs, free, claim)

    # the EEI: as close to the hint as possible (default: west of the build, mid-
    # height), on free ground inside SOME substation's supply area.
    hx, hy = eei_hint if eei_hint is not None else (x0 - 5, (y0 + y1) // 2)
    out = [("sub", s) for s in plan]
    cand = [(hx + dx, hy + dy) for dx in range(-8, 9) for dy in range(-8, 9)]
    cand.sort(key=lambda t: (abs(t[0] - hx) + abs(t[1] - hy), t))
    for t in cand:
        if free(t) and any(_supply_covers(s, ((t[0], t[1]), (t[0] + 1, t[1] + 1)))
                           for s in plan):
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


def patch_power(layout: Layout, plan) -> list:
    """POST-ROUTING safety net: any POWERED entity outside the planned supply gets a
    nearby substation on whatever free ground routing left (rare -- a tap on a fringe
    detour outside the dilated cover). Returns the extended plan; emit AFTER this."""
    subs = [s for k, s in plan if k == "sub"]
    occ = {t for e in layout.entities for t in e.tiles()} | power_tiles(plan)

    def free(s):
        sx, sy = s
        return not ({(sx, sy), (sx + 1, sy), (sx, sy + 1), (sx + 1, sy + 1)} & occ)

    def claim(s):
        occ.update(((s[0], s[1]), (s[0] + 1, s[1]), (s[0], s[1] + 1), (s[0] + 1, s[1] + 1)))

    dark = [e for e in layout.entities if e.proto in POWERED
            and not any(_supply_covers(s, e.tiles()) for s in subs)]
    if not dark:
        return plan
    for e in sorted(dark, key=lambda e: (e.y, e.x)):
        if any(_supply_covers(s, e.tiles()) for s in subs):
            continue                           # an earlier patch already covers it
        tx, ty = e.tiles()[0]
        cand = [(tx + dx, ty + dy) for dx in range(-9, 9) for dy in range(-9, 9)]
        cand.sort(key=lambda s: (min((abs(s[0] - o[0]) + abs(s[1] - o[1]) for o in subs),
                                     default=0), s))
        spot = next((s for s in cand if free(s) and _supply_covers(s, e.tiles())), None)
        if spot is not None:
            subs.append(spot)
            claim(spot)
    subs = _bridge(subs, free, claim)
    return [("sub", s) for s in subs] + [(k, s) for k, s in plan if k != "sub"]


def add_power(layout: Layout) -> None:
    """POST-ROUTING fallback overlay (used by the legacy v1/v2 generators): plan
    against whatever ground routing left and emit immediately. Dense layouts may not
    leave enough 2x2 ground -- v3 plans BEFORE routing instead (the reliable path)."""
    consumers = [e for e in layout.entities if e.proto in POWERED]
    if not consumers:
        return
    inputs = [e for e in layout.entities if e.proto == "infinity-chest"]
    anchor = inputs if inputs else consumers
    hint = (min(e.x for e in anchor) - 6,
            round(sum(e.y for e in anchor) / len(anchor)))
    plan = plan_power({t for e in layout.entities for t in e.tiles()},
                      cover={t for e in consumers for t in e.tiles()},
                      eei_hint=hint)
    emit_power(layout, plan)
