# Status report

A snapshot of where the `fgr` POC stands: what works, the latest numbers, and exactly
where the compiler still fails. Regenerate the battery numbers with:

```bash
.venv/bin/python -m pytest -q                                      # unit/regression suite
.venv/bin/python scripts/stress_complex.py examples/complex examples/stress   # stress battery
```

## Results

| Suite | Result |
|-------|--------|
| **pytest** (unit + regression) | **61 / 61 green** |
| **Stress battery** (`examples/complex` + `examples/stress`) | **37 / 43 compile + verify** |
| &nbsp;&nbsp;• hand-authored complex (`examples/complex`) | 6 / 6 |
| &nbsp;&nbsp;• generated stress DAGs (`examples/stress`) | 31 / 37 |

The verifier is the source of truth: a "pass" means the compiled layout was *independently*
graded as physically realising the spec (every declared belt/pipe lane connects, nothing
spurious, no overlaps, fluids isolated). An independent second checker
(`scripts/independent_check.py`, a different code path) and FBSR renders corroborate it.

## Where it still fails (6 cases)

All six remaining failures are the **router/manifold** hitting its limit — *not* the
verifier or the model. Two flavours, same root cause (too many belt lanes competing for a
corridor):

**A. Fan-out / merge congestion** — `reconverge_6` (two merges into adjacent collector
chests). The fan-out manifold is a *compact* splitter chain whose peel tails run as long
diagonals to spread-out consumers; past ~6–7 the tails saturate the corridor and the rip-up
router gives up. (The merge side was improved this session — splitters now sit beside the
trunk so sources curve straight in, no underground U-turn — which recovered `science_3` and
`science_6`.)

**B. Large dense graphs (speed)** — `reconverge_1` (19 nodes / 37 edges), `scale_2`
(40 / 73), `scale_4` (37 / 72), `scale_5` (31 / 70), `scale_6` (46 / 89). These exceed the
15 s harness budget: the pure-Python A* + rip-up *thrashes* (lanes repeatedly rip and
re-route) before succeeding or giving up.

The highest-leverage remaining fix is the **bus fan-out manifold**: place each splitter at
its consumer's *row* so every peel is a short straight hop (instead of a long diagonal),
removing the congestion *and* shrinking layouts (faster routing). Prototyped earlier but had
geometry bugs and was reverted to keep the suite green; finishing it is the top open task. A
compiled / smarter router (or jump-point A*) would independently address flavour B.

## What landed this session

**Correctness (the oracle got stronger):**
- **Fluid-mixing detection** — the verifier now flags a pipe network carrying two fluids.
  This was a real blind spot: pure reachability (both the verifier *and* the independent
  checker) said PASS on a layout where petroleum-gas and water shared one network; only the
  FBSR render + visual audit caught it. Root cause was a stray pipe touching an *unused*
  fluid box — now every fluid-box tile is blocked unless a network claims it.
- **Underground pairing rules** — pipe-to-ground pairs with the *nearest* opposite mouth;
  a same-axis underground in the line *blocks* the scan, a perpendicular one is ignored
  (collinear interleaved tunnels cross-pair; perpendicular crossings are fine).
- **Pipe vs belt reach** — pipe-to-ground reaches farther underground (10) than an
  underground belt (5); the verifier now uses the correct, separate distances.
- **Fluid isolation** — different fluids are kept from running surface-adjacent (so they
  can't weld), while still crossing freely by tunnelling under one another.
- **Pipe-to-ground direction** — `direction` is the OPEN mouth (underground is the opposite
  side); the emitter had entrance/exit swapped, so FBSR drew every tunnel reversed. Fixed
  and pinned in `validate-model` against real data.
- **Recipe ↔ machine validity** — a *spec* check (not the layout) done NATIVELY from
  Factorio data: a recipe's `category` must be in its machine's `crafting_categories`, so a
  chemical-plant recipe on an `assembler` (or vice-versa) is flagged — no hard-coded recipe
  table. Surfaced by `fgr verify` / `fgr compile`. (This caught `electric-engine-unit` and
  `processing-unit`, which are crafting-with-fluid → assemblers, not chemical plants.)

**Capability:**
- Furnaces (`furnace`), chemical plants (`chemical`) + infinite fluid sources (`fluid`),
  and **fluid lanes** (`~>`) carried on **pipes** that attach at real, rotation-aware
  fluid-box tiles; outputs receiving fluid become storage tanks.
- Per-source pipe **networks** (one box branching to many consumers) — sidesteps the
  2-/4-connection limit.
- **Auto-promote**: a node with more lanes than its perimeter bundles them onto a shared
  (fan-out) belt or a merge, instead of failing; leftover dedicated out-edges are absorbed
  into an existing fan-out so one bus carries the source's item.
- **Merge splitter placed beside the trunk** (not across it): each merge splitter sits one
  column west of the south-flowing trunk, so a source curves straight into its west input
  from its own side instead of tunnelling under the bus and U-turning back. Removed the
  underground U-turns from merges and recovered `science_3` / `science_6` (35 → 37 / 43).

**Performance:** the A* only considers an underground hop when there's actually an obstacle
to tunnel under (it used to try a hop from every tile) — a ~4–5× routing speedup that
recovered several previously-timed-out cases.

**Cleanup / structure:** `compile_graph` is now a readable pipeline with the belt-topology
decisions (`_consolidate_lanes`) and fluid-network reservation (`_reserve_fluid_networks`)
extracted; dead code removed; unused imports cleared; the generated stress DAGs are
committed under `examples/stress/`, and the new `tests/test_complex.py` locks in the
complex-example, fluid-mixing, and underground-reach behaviour as regression tests.
