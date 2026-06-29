# fgr v2 — clean layout engine design

A from-scratch rewrite of the layout *generator* (`fgr/layout.py`) that replaces v1's
fixed grid + A\* rip-up router with a **deterministic, closed-form lane-fabric**. The
verifier (`fgr/verify.py`), DSL, IR, and blueprint emitter stay stable — only the
generator changes, plus an **additive** power check (and an optional, gated loader rule)
in the verifier.

Goal priority: **clean / structured / expandable output** first, while still passing the
verifier on *every* case (incl. the 6 historically-hard ones) and being **fast**.

## Why v1 fails

(From the diagnosis — see `git log` / the design workflow.) v1 lays the DAG into columns
on a **fixed** `COL_STRIDE=13` grid with **no port alignment**, then routes every edge as
an independent **A\* lane with rip-up/retry**. Consequences:

- **Sprawl + jogs:** gears is width 27 / 20 belts for a 3-node chain; densities are 16–24%
  (mostly empty); belts:machines runs ~21:1 (processing_unit) to ~30:1 (flying_robot_frame).
- **Routing failures / thrash:** the rip-up router contends for shared corridors and
  exhausts its budget — reconverge_1/scale_5/scale_6 give up; reconverge_6 boxes-in a merge
  endpoint; scale_4 collides two splitters on the same tiles; scale_2 takes 48s.
- **Spaghetti:** 3,750 undergrounds across the stress battery (≈1,875 crossings).

## The v2 idea: a lane fabric

Four deterministic passes, O(V+E + E·log E), **no search, no rip-up, cannot give up**:

1. **LAYER** — `L(n)` = ALAP depth (longest path *to a sink*); **INPUT** pinned to column 0
   (west), **OUTPUT** to the last column (east). Flow always points east; deeply-consumed
   inputs hug their consumers instead of being stranded at column 0.
2. **ORDER** — per-layer **barycenter** row ordering (integer keys, stable sort, fixed
   sweep count, insertion-index tiebreak → deterministic & version-stable) to near-planarize
   before any routing exists.
3. **PLACE** — **running-sum X** (no fixed stride): `X[c+1] = X[c] + maxBodyWidth(c) +
   gutter(c)`, where `gutter(c)` is exactly the number of riser columns that gap needs. The
   **dominant path** (longest chain through a node) is **center-row aligned** across columns,
   so the main spine is a dead-straight east belt with **zero turns**.
4. **EMIT** — the universal lane primitive (below). A cheap internal **self-check** runs the
   verifier's own flow model before returning and asserts `lanes == spec`.

### The universal lane primitive — one product = one belt, tapped by many

Every **producer** `P` gets **one horizontal east belt** on a reserved **track row** `R_P`.
Every consumer of `P` **taps** that belt with **one input inserter** where the lane passes
its column (pickup = belt tile, drop = body). The belt keeps flowing east past each tap to
reach the next consumer; it terminates one tile past the last tap into empty space.

This one primitive realizes **everything**:

- **Belt-fed machine rows** (your "one belt feeds 10 furnaces") — consumers laid
  footprint-tight along `P`'s lane, each tapping with its own inserter. We have no power
  poles in the row, so it's perfectly straight with zero in-row undergrounds.
- **Fan-out** `P -> {C1..Cn}` — one lane, n taps, **zero splitters**.
- **Merge** `{u1,u2} -> v` — u1 and u2 each own a lane; v taps **both** with two input
  inserters on distinct faces. **No splitter, no merge gadget** → this *deletes the entire
  scale_4 splitter-overlap bug class*.
- **Reconvergence / skip-edges** (`cable->green,red`; `green->blue`) — the shared
  intermediate is a single lane on a track row; every downstream taps it where it already
  passes that column. (The v1 thrash case becomes the *easy* case here.)

Track rows are allocated by **left-edge interval coloring** of producer-lane lifespans
`[outCol .. lastConsumerCol]` (the classic VLSI channel-router coloring) — short spans
nearer the machine band. When a consumer's port isn't directly adjacent to `R_P`, a
vertical **riser** in a private (left-edge-colored) gutter column bridges the track to the
body face.

### Provable no-spurious-lanes (the verifier's hazard, eliminated by construction)

1. Each track row hosts **at most one** producer lane over any x → a lane tile's east
   neighbour is its own next tile or empty; never a foreign carrier. (Belts accept side
   feeds, so this matters.)
2. Taps are **inserters** (not carriers) dropping into **bodies** (BFS-terminal) → exactly
   the declared lane, no chaining.
3. **All undergrounds are vertical (N/S)** on **private riser columns**; horizontal lanes
   carry zero undergrounds. Every riser crossing a horizontal lane **dives** (one
   underground; the buried tile is the foreign lane's surface belt, untouched).
4. Each dive resurfaces 2 tiles ahead with a surface gap between consecutive pairs, and the
   horizontal/vertical **axis split** means the nearest same-direction underground ahead of
   any entrance is always its own exit → **no UG mispairing** (the verifier's "nearest
   same-dir within 5" trap can't fire).
5. A final **self-check** runs `_flow_edges`/`_direct_lanes` and asserts `lanes == spec`
   with no carrier facing a foreign accepting carrier — catching any emit bug before
   `verify.py`.

### Fluids

Verifier contract unchanged; structured placement. One pipe network per fluid **source**;
reuse `_fluid_connections`/`FLUID_BOX` verbatim (boxes rotate with body direction). Fluid
consumers get fluid priority on their N/S box columns; item taps use the west face + the
free north-center tile. Each network is a deterministic nearest-neighbour pipe tree of
straight runs on reserved fluid channels disjoint from belt tracks; cross belts/other
fluids with `pipe-to-ground` (reach 10, opposite-facing pair). No-mixing is structural: a
1-tile keep-out from every other fluid's surface pipes, and unused fluid-box tiles
pre-blocked. Tanks sit on the east edge.

### Power (new) — pure post-placement overlay

After the bbox is known, lay a **substation** lattice (2×2, supply 18×18, wire reach 18) at
**pitch 16**: centers 16 apart < 18 → one wired component *and* full coverage with a 2-tile
margin (nudges never open a gap). Each substation drops at its lattice point or spirals to
the nearest free 2×2 (≤8 < margin); gutters reserve 2×2 holes to guarantee a legal slot.
One **electric-energy-interface** (2×2, infinite) drops in a free gap inside a supply area
and the component.

New **additive** verifier check `_check_power`: (1) **coverage** — every powered entity
({assembler, furnace, chemical, inserter, EEI}) within Chebyshev ≤9 of a substation; (2)
**connectivity** — union-find over substations (edge if center-distance ≤18) is one
component; (3) the EEI lies in a supply area and that component. Items/fluids oracle
untouched.

### Clean I/O on the boundaries

INPUT nodes pin to column 0 (west line), OUTPUT nodes (steel-chest or tank) to the last
column (east line), on aligned rows → clean vertical I/O lists. Input bodies carry only
east-face entities, outputs only west-face → **nothing protrudes** past the boundary; all
tracks/risers stay strictly interior. Default I/O is verifier-clean inserter→belt.
**Optional full-belt I/O** (infinity-chest + `loader-1x1` → belt) is gated behind a flag
and needs a small additive loader rule in the verifier.

## Code shape (`layout.py` rewrite)

- **Keep byte-stable** (verify.py imports): `ASSEMBLER, BELT, CHEMICAL, CHEST_INPUT,
  CHEST_OUTPUT, FLUID_SOURCE, FURNACE, INSERTER, PIPE, PIPE_TO_GROUND, PIPE_UG_GAP,
  SPLITTER, TANK, UG_MAX_GAP, UNDERGROUND, Layout, PlacedEntity, _fluid_connections`,
  plus `SIZE/FLUID_BOX/_rot_cw/INPUT_FILL/LayoutError` and `compile_graph()`.
- **Add:** `SUBSTATION`, `EEI` (+SIZE 2×2); optional `LOADER='loader-1x1'`.
- **Delete:** `COL_STRIDE, ROW_STRIDE, ROW_GAP, UG_PENALTY` and the whole A\* router
  (`_route_jobs/_search/_moves/_route_lane/_build_manifold/_build_merge`).
- **New passes:** `_layer` (ALAP+pin) · `_order` (barycenter) · `_assign_coords`
  (running-sum X, band Y, dominant-spine alignment, adjacency-collapse) · `_producer_lanes`
  · `_color_tracks` / `_color_risers` (left-edge) · `_emit_lane`/`_emit_riser`/`_dive` ·
  `_emit_fluids` · `_place_power` · `_self_check`.
- `verify.py`: add `_check_power` + the POWERED set, called from `verify()`; optional gated
  loader carrier rule in `_flow_edges`.

## Implementation plan (each phase independently testable)

1. **Skeleton + chains** — layer/order/coords + dominant-spine alignment + adjacency-collapse
   (gears = 0 belts) + `_self_check`. *Gate:* gears + all linear chains pass; gears
   ~0 belts / area ~21 / fill ≥50% (vs v1 w27/area81/20 belts); 61 existing tests green.
2. **Producer-lane fan-out + track coloring + multi-tap merges** (no risers yet). *Gate:*
   fan-out/merge cases exact-lane-equal, zero spurious; belts:machines → low single digits;
   determinism (compile twice → identical).
3. **Risers + dives** (reconvergence / skip-edges / multi-input). *Gate:* the 6 hard cases
   + wide_reconverge pass, sub-second, ug count in the tens (vs 3,750).
4. **Fluids** — per-source pipe tree. *Gate:* chemical/fluid cases connect + no-mixing.
5. **Power overlay + `_check_power`.** *Gate:* power checks pass on every case; no overlaps.
6. **Clean I/O boundaries** (+ optional gated loader). *Gate:* nothing protrudes; render spot-check.
7. **Wire `examples/stress/*.fgr` into gating pytest** + metric gates. *Gate:* 37/37 stress +
   61 existing pass under a sub-second-per-case budget; metrics meet targets.
8. **Polish + expandability** — "add a machine = local diff" test; fill/area tuning.

## Targets (vs v1 baseline)

| metric | v1 | v2 target |
|--------|----|-----------|
| gears | w27 / area81 / 20 belts | ~0–4 belts, tight & aligned, ≥50% fill |
| belts:machines | 7–30:1 | ~1–4:1 |
| fill % | 16–24% | 40–65% |
| stress undergrounds | 3,750 | tens |
| stress pass | 32/37 (6 broken/slow) | 37/37, sub-second |

## Residual risks (tracked)

- **High fan-in face exhaustion** (a body has limited inserter faces) → spread taps across
  faces + risers; worst case a short dedicated access belt.
- **Riser crossing many consecutive lanes** could exceed UG gap 5 → span-ordered tracks keep
  bands thin; assert per-dive gap ≤5 before emit; chain two hops if needed.
- **Fluid-box vs item-tap contention** on N/S faces → reserve N/S for fluid, items to west.
- **Power coverage holes** in a dense core → pitch-16 margin + reserved gutter holes + nudge.
