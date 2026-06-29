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
| **pytest** | **77 passed, 34 xfailed, 1 skipped** (green) |
| **All 49 examples compile** (no crashes) | **49 / 49** |
| **Examples that fully verify** | **22 / 49** |
| &nbsp;&nbsp;• basic | 6 / 6 |
| &nbsp;&nbsp;• complex (hand-authored) | 3 / 6 |
| &nbsp;&nbsp;• stress (generated DAGs) | 13 / 37 |

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
  `highfanin_*`/`scale_*`) — many machine→machine fluid lanes whose boxes end up boxed-in
  (one free neighbour) in the dense interior; the pipe tree can't reach them. The fix is
  **fluid-aware placement** (cluster fluid-connected machines / a dedicated fluid corridor),
  a structural rework rather than a routing tweak.
- **Very high fan-in to small sinks** (`wide_reconverge`: 7 inputs into one 1×1 chest) —
  exceeds the perimeter's port count; needs a **collector-belt merge** (several lanes onto
  one belt feeding a single port).
- **Congested reconvergence** (`reconverge_1`, `scale_*`) — a consumer's perimeter is
  crowded by its own converging inputs, so the last riser finds no free, reachable port.

Contained levers (gutter width, degree-aware clearance, isolation tuning, stronger BFS
fallback) were explored and are at their useful limit here; closing the tail needs the two
structural changes above (fluid-aware placement + collector merges).

## Tests

`tests/test_examples.py` runs **every** example: each must compile, and each must verify
unless it's in `KNOWN_FAILING` (xfail). A ratchet test guards the pass rate. The verifier
unit tests were updated to v2's design (fan-out/merge are inline taps, asserted to use **no
splitters**). Shrinking `KNOWN_FAILING` to empty is the goal.
