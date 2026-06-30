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
| **pytest** | **88 passed, 24 xfailed** (green) |
| **All 49 examples compile** (no crashes) | **49 / 49** |
| **Examples that fully verify** | **30 / 49** |
| &nbsp;&nbsp;• basic | 6 / 6 |
| &nbsp;&nbsp;• complex (hand-authored) | 4 / 6 |
| &nbsp;&nbsp;• stress (generated DAGs) | 20 / 37 |

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

## Where it still fails (the tracked tail)

All remaining failures are **routing through a dense field**, never the verifier or the model:

- **Complex multi-fluid chains** (oil/chem refineries: `fluids_*`, `science_*`, several
  `highfanin_*`/`scale_*`) — machine→machine fluid lanes whose boxes get walled in. Keeping
  each fluid box's **approach corridor clear of belts** (reserved during item routing, since
  we know the boxes up front; belts then route around or dive under it) recovered several
  (`fluids_3`, `reconverge_4`). The residue is boxes walled by *machines* (not belts), which
  the corridor can't open — that needs **fluid-aware placement** (cluster fluid-connected
  machines), a structural change.
- **Very high fan-in to a 1×1 chest** — a 1×1 output chest has only 4 inserter faces, so an
  in-degree > 4 can't be wired directly. Five cases hit this: `wide_reconverge` (out, deg 8),
  `reconverge_1` (devices, 6), `scale_2` (lab, 6), `scale_5` (machines_out 6, modules_out 7).
  The fix is a **collector-belt merge** (the overflow producers flow onto one belt feeding a
  single port) — a well-scoped next feature.
- **High fan-in to a 3×3 assembler** (`flying_robot_frame` frame deg 4, `scale_2` engine/frame,
  `scale_5` am2/beacon) — 12 perimeter ports exist, but the last riser can't *route* to a free
  one through the crowded approaches: a routing problem, not a port-count one.
- **Spurious fluid lanes from stacked machines** (`reconverge_3`, `fluids_6`, `scale_*`) — two
  fluid machines stacked so a differing-fluid box pair sits 4-adjacent and welds. Weld-aware
  box selection (pick the non-touching box) fixed the avoidable ones (`fluids_4`); the residue
  is machines whose *both* box columns weld, which needs a 1-row placement gap.

Why the tunnel-reach asymmetry settles the routing order: a **pipe-to-ground reaches 10
tiles, an underground belt only 5**, so pipes cross the belt field far more easily than belts
cross a pipe field — routing **items first, fluids last** (the current order) is therefore
optimal, and giving fluids priority makes items the ones that can't cross (measured worse).
Two further levers were explored and found not to pay off *here*: **rotating machines** off
the default north — fluid boxes must stay on the N/S faces because E/W are the item-inserter
faces, so the only safe flip (N↔S) just trades one congested side for the other; and a
**belt-dive crossing** (sink a straight belt run underground so a pipe crosses on top) — a
sound technique, but redundant with the approach-corridor, which already prevents the
belt-walls it would cross. Closing the rest needs the two structural changes above
(fluid-aware placement + collector merges).

## Tests

`tests/test_examples.py` runs **every** example: each must compile, and each must verify
unless it's in `KNOWN_FAILING` (xfail). A ratchet test guards the pass rate. The verifier
unit tests were updated to v2's design (fan-out/merge are inline taps, asserted to use **no
splitters**). Shrinking `KNOWN_FAILING` to empty is the goal.
