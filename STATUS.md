# Status report

Where the `fgr` POC stands on the **`v2-clean-layout`** branch: a from-scratch rewrite of
the layout *generator* into a deterministic **lane fabric** (the verifier/DSL/IR/emitter are
unchanged — the verifier remains the independent oracle). Regenerate the numbers with:

```bash
.venv/bin/python -m pytest -q                      # unit + regression + every example
```

## Results

| Suite | Result |
|-------|--------|
| **pytest** | **93 passed, 19 xfailed** (green) |
| **All 49 examples compile** (no crashes) | **49 / 49** |
| **Examples that fully verify** | **34 / 49** |
| &nbsp;&nbsp;• basic | 6 / 6 |
| &nbsp;&nbsp;• complex (hand-authored) | 5 / 6 |
| &nbsp;&nbsp;• stress (generated DAGs) | 23 / 37 |

A "pass" means the layout was *independently graded* by `fgr/verify.py` as physically
realising the spec: every declared belt/pipe lane connects, nothing spurious, no overlaps,
fluids isolated and attached at real fluid-box tiles. The not-yet-verified cases are tracked
in `tests/test_examples.py::KNOWN_FAILING` (xfail) and fail loudly if they start passing.

## What v2 is

Four deterministic passes — **no search, no rip-up, cannot give up** (see `docs/V2_DESIGN.md`):

1. **LAYER** — columns by ALAP depth; inputs pinned west (col 0), outputs east.
2. **ORDER** — barycenter row ordering per column (near-planarize before routing).
3. **PLACE** — running-sum X (adaptive, no fixed stride); the dominant chain is center-row
   aligned so the main spine is a dead-straight belt.
4. **EMIT** — the universal lane primitive: **one belt per producer, tapped by one inserter
   per consumer**. Fan-out and merge are inline taps with **zero splitters** (the requested
   "feed many machines without splitting"). Crossings dive via vertical undergrounds so
   distinct lanes never weld. Fluids route as mostly-underground pipe trees, one network per
   source, with a 1-tile keep-out between networks.

**Wins vs the v1 baseline:** deterministic & instant (no A*/rip-up thrash — v1's `scale_2`
took ~46 s; v2 compiles every case sub-second); far tighter (e.g. gears 81→57 area, 20→10
belts); no spaghetti; **zero** compile crashes, overlaps, or spurious lanes.

## Correctness fix this session (a real verifier false-pass)

A **pipe-to-ground sitting on a fluid box does not feed the machine** unless its open mouth
faces the machine — its underground side connects to its tunnel partner, not the box. The
verifier previously accepted any pipe/p2g on a box (false-pass, caught on an FBSR render).
Now a box connects only via a **plain pipe** or a **p2g whose mouth faces the machine**, in
both the verifier and the generator (which tunnels into boxes from under the belt field).
Plus fluid-network **isolation** (1-tile keep-out) so two fluids can't weld.

A parallel **adversarial audit** of the verifier then closed three more false-passes in the
same family (so a "pass" is trustworthy): a pipe-to-ground now surface-connects only toward
its open mouth (not all four sides); an underground-belt's pairing scan stops at the first
*same-axis* underground (an opposed same-tier one interferes in-game); and a new **"no
undeclared fluid lanes"** check (the fluid analogue of the item-side guarantee) catches a
network that joins one machine's produced fluid to another's input. A later audit pass also
hardened inserter **insertability** (an inserter touching a fluid-only body — tank / infinity-
pipe — is rejected, since it can't move items there). Assemblers are also modelled correctly
(fluid boxes only when the recipe uses fluid), and the generator blocks unused fluid-box
tiles so a passing pipe can't weld a phantom connection.

### What a "pass" does and doesn't cover

The oracle grades **physical material-flow topology**: placement (no overlaps), node↔machine
correspondence, every declared belt/pipe lane connected, no spurious lanes, fluids isolated
and attached at real boxes. Out of its scope (by design, tracked):

- **Power** — substations + an electric-energy-interface are specified in `docs/V2_DESIGN.md`
  but not yet placed; in-game the machines would need power. (Constants exist; the overlay +
  a `_check_power` are the next feature.)
- **Recipe↔machine category** (e.g. a chemistry recipe on an assembler) — checked by
  `fgr/fbsr_validation.py` against real Factorio data, deliberately kept out of the pure
  oracle (which holds no hard-coded recipe table).
- **Throughput** — connectivity only: that an item *can* reach a machine, not the rate.

## What landed (the fixes that raised the pass rate)

- **Approach corridor** — each fluid box's short approach is reserved off-limits to belts during
  item routing (we know the boxes up front), so belts route around/under and pipes reach the box.
- **Weld-aware box selection** — when two fluid machines stack with facing boxes 4-adjacent,
  pick each machine's *non-touching* box so the two networks don't weld into a spurious lane.
- **Collector-belt merge** — a 1×1 sink (4 faces) with > 4 item inputs routes all its producer
  risers onto one vertical collector belt (merged top-down, planar) feeding it through a single
  inserter. Fixed `wide_reconverge` (deg 8) and `reconverge_1` (deg 6).
- **Nearest-first fluid links** — a source that fans out to many consumers links its *closest*
  boxes first, so its net stays compact instead of sprawling and self-blocking the far links.
- **Belt-dive crossing (fallback)** — when a fluid link can't step or tunnel to its box, it may
  cross a belt by sinking that straight run underground (`ug-in [buried] ug-out`) and taking the
  freed surface — perpendicular-only, straight-run-fed-straight, never over a tapped belt, and it
  aborts cleanly (no overlap) if a crossed tile was already converted.

## Where it still fails (the tracked tail)

All remaining failures are **routing through a dense field**, never the verifier or the model:

- **Dense multi-fluid reach** (`deepchain_2/5`, `highfanin_2/6`, `science_6`, the unconn parts of
  `fluids_6`, `reconverge_3`, `scale_1/6`, `fluids_7`) — a few machine→machine fluid lanes whose
  boxes are *reachable* (free neighbours) but the greedy per-source pipe router can't find a path
  through the packed field. Needs a stronger (backtracking/rip-up) pipe router or fluid-aware
  placement; an exhaustive per-box retry and the belt-dive fallback are already in.
- **High fan-in to a 3×3 assembler** (`flying_robot_frame`, `scale_2`, `scale_5`) — 12 perimeter
  ports exist but the last riser can't *route* to a free one: the producer trunk ends far from the
  consumer and its end-of-trunk flow direction blocks a clean extension. Needs a riser-column /
  trunk-extension improvement (a rescue pass was tried; it needs the trunk to end flowing toward
  the free port).
- **Residual welds** (`reconverge_3`, `fluids_6`, `scale_6`) — stacked fluid machines whose *both*
  box columns are 4-adjacent. A 1-row placement gap fixes the weld but, applied globally, the
  extra spacing regressed 9 other cases (longer pipes) — it needs to be applied only to the
  welding pair, not every fluid machine.
- **Corridor side-effect** (`fluids_5` `explosives->explosives_out`, in-deg 1) — the K=5 approach
  corridor reserves a tile this lone item lane needed; a per-box adaptive corridor would recover it.

Why the tunnel-reach asymmetry settles the routing order: a **pipe-to-ground reaches 10 tiles, an
underground belt only 5**, so pipes cross the belt field far more easily than belts cross a pipe
field — routing **items first, fluids last** (the current order) is optimal; giving fluids priority
makes items the ones that can't cross (measured worse). **Rotating machines** off the default north
doesn't help either: fluid boxes must stay on N/S (E/W are the item-inserter faces), so the only
safe flip (N↔S) just trades one congested side for the other.

## Tests

`tests/test_examples.py` runs **every** example: each must compile, and each must verify
unless it's in `KNOWN_FAILING` (xfail). A ratchet test guards the pass rate. The verifier
unit tests were updated to v2's design (fan-out/merge are inline taps, asserted to use **no
splitters**). Shrinking `KNOWN_FAILING` to empty is the goal.
