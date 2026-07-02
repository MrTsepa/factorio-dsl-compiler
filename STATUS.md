# Status report

Two interchangeable layout **generators** live behind one interface (`fgr/generators.py`):
**v1** (the original fixed-grid + A\* rip-up router) and **v2** (a from-scratch deterministic
**lane fabric** — four passes, no search, no rip-up, cannot give up). The DSL, IR, and
**verifier** (`fgr/verify.py`) are shared and unchanged — the verifier is the independent
oracle that grades whatever either generator produces. **v2 is the default.**

```bash
.venv/bin/python -m pytest -q                                   # unit + regression + every example
.venv/bin/python scripts/compare_generators.py --corner-cases   # full v1-vs-v2 head-to-head
```

## Results (v2, the default)

| Suite | Result |
|-------|--------|
| **pytest** | **111 passed, 5 xfailed** (green) |
| **examples/** (49 curated cases) compile (no crashes) | **49 / 49** |
| **examples/** that fully verify | **46 / 49** |
| **corner_cases/** (106 generated stress cases) that fully verify | **92 / 106** |

A "pass" means the layout was *independently graded* by `fgr/verify.py` as physically
realising the spec: every declared belt/pipe lane connects, nothing spurious, no overlaps,
fluids isolated and attached at real fluid-box tiles. `examples/` failures are tracked in
`tests/test_examples.py::KNOWN_FAILING` (xfail, gates the suite); `corner_cases/` is a
standalone failure-hunting corpus (outside the gating suite) whose files each self-document
their current verdict in a `# STATUS (engine <sha>)` header — refresh with
`scripts/refresh_corner_case_status.py`.

## v1 vs v2: comparative analysis

Both generators were run over the same **155 cases** (`examples/` + `corner_cases/`) through
`scripts/compare_generators.py`, each `(file, generator)` pair isolated in its own subprocess
with a 20s timeout (v1's search router can hang; a hang on one case must never take down the
comparison). Full methodology and live numbers: run the script yourself, or see
`scripts/_compare_worker.py` for the exact metrics collected.

| generator | pass rate | timeouts (>20s) | avg compile (both-pass cases) |
|---|---|---|---|
| **v1** (A\* rip-up) | 132 / 155 | 10 | 521 ms |
| **v2** (lane fabric) | **138 / 155** | **0** | **13 ms** |

- **Reliability.** v2 passes strictly more cases and **never times out** — every one of the
  155 cases (up to N=64 fan-in/fan-out, N=32 deep chains) compiles in well under a second. v1
  hangs on 10/155 (roughly 6%), all on the larger generated stress cases (`scale_*`,
  `reconverge_1`, `furnace_stack_48/64`, `green_bank_24`, `red_bank_16`, `butterfly_6`) — the
  exact failure mode v2 was built to eliminate (search + rip-up degrades badly with scale).
- **Speed.** On the 119 cases **both** fully verify (an apples-to-apples set), v2 compiles
  **~40× faster on average** (13 ms vs 521 ms) — and that *understates* v1's cost, since it
  excludes the 10 outright timeouts (v2 handles every one of those in double-digit
  milliseconds).
- **Shape.** On that same fair set, v2 lays down roughly **2× the entities/belts/area** of
  v1 (v1: avg 600 ents/2210 area; v2: avg 1161 ents/3420 area) — v2 trades some compactness
  for *robustness*: the deliberate fluid-machine spacing and adaptive-gap escalation that let
  it route dense fluid fields without welding or fragmenting cost some tiles. But v2's belt
  routing is **~3× straighter** (avg 11.7 corners vs v1's 36.1) — fewer needless jogs, a more
  legible layout. On simple graphs v2 is tighter in absolute terms too (e.g. `gears`: v1 81
  tiles/20 belts vs v2 57 tiles/10 belts).
- **A shared bug, one fixed.** v1 hits the *identical* per-source fluid-network limitation v2
  had until this session's same-fluid merge fix: `no free input fluid box` on any case where
  same-fluid producers converge on a shared consumer with more sources than boxes
  (`refinery_*`, `fluid_fanin_6/8`, `lube_manifold_*`) — 9 cases. v2 fixed the underlying
  model (a fluid network is now a per-fluid connected component, not one per source); v1
  still has it, unpatched, since it's kept as the historical baseline.
- **A v1-only correctness bug.** v1 fails 3 cases (`reconverge_cross_8`, `furnace_stack_32`,
  `gear_bank_32`) with **"no undeclared belt lanes"** — a spurious weld the verifier catches
  that v2 doesn't produce on any tested case.
- **v2's remaining gap.** v2 fails/regresses on 13 cases (mostly `corner_cases/fanin`,
  `reconverge`, `bus`, `combo/butterfly`) that v1 *does* route — all one bucket: **congested
  belt risers**, where a high-fan-in consumer's producer trunks end deep in a packed channel
  and the deterministic router can't find a path through the fragmented free space (see
  "Where it still fails" below). v1's search-based router, despite being far slower and less
  reliable at scale, can still brute-force a path in some of these.

**Bottom line:** v2 is the better default — more reliable (zero hangs, higher pass rate,
∼40× faster), and its remaining failure mode (congested risers) is a known, scoped bucket
rather than v1's open-ended slow/hang failure mode. v1 is kept as the historical baseline and
reference implementation (`fgr/layout_v1.py`), selectable via `fgr compile -g v1` or
`compile_graph(graph, "v1")`.

## What v2 is

Four deterministic passes — **no search, no rip-up, cannot give up** (see `docs/V2_DESIGN.md`):

1. **LAYER** — columns by ALAP depth; inputs pinned west (col 0), outputs east.
2. **ORDER** — barycenter row ordering per column (near-planarize before routing).
3. **PLACE** — running-sum X (adaptive, no fixed stride); the dominant chain is center-row
   aligned so the main spine is a dead-straight belt.
4. **EMIT** — the universal lane primitive: **one belt per producer, tapped by one inserter
   per consumer**. Fan-out and merge are inline taps with **zero splitters** (the requested
   "feed many machines without splitting"). Crossings dive via vertical undergrounds so
   distinct lanes never weld. Fluids route as mostly-underground pipe networks — one network
   per **fluid** (same-fluid producers into a shared consumer merge into one network, not one
   each), with adaptive spacing between stacked fluid machines and a co-router that searches
   net routing orders when a lane is boxed out by contention.

### What a "pass" does and doesn't cover

The oracle grades **physical material-flow topology**: placement (no overlaps), node↔machine
correspondence, every declared belt/pipe lane connected, no spurious lanes, fluids isolated
and attached at real boxes. Out of its scope (by design, tracked):

- **Power** — substations + an electric-energy-interface are specified in `docs/V2_DESIGN.md`
  but not yet placed; in-game the machines would need power.
- **Recipe↔machine category** (e.g. a chemistry recipe on an assembler) — checked by
  `fgr/fbsr_validation.py` against real Factorio data, deliberately kept out of the pure
  oracle (which holds no hard-coded recipe table).
- **Throughput** — connectivity only: that an item *can* reach a machine, not the rate.

## Where it still fails (v2's tracked tail)

All remaining `examples/` failures (`fluids_7`, `scale_1`, `scale_5`) and most of
`corner_cases/`'s 14 remaining failures are the **same bucket**: **congested belt risers**.
A high-fan-in consumer (in-degree ≥ 3 on a 1×1/3×3 machine) sits far from one of its
producers, whose trunk ends deep in a packed channel; the riser tap gets wedged between
stacked trunk rows and other risers, and the free tiles that exist don't form a connected
path to any port (confirmed by relaxed-bounds BFS — it's not a reach limit, the space is
*fragmented*, not full). Tried and reverted (each hits a wall or regresses): a rescue pass
retapping at any free trunk column (the whole channel row is boxed), a BFS start-jump
(mis-feeds a belt at a boxed drop), wider channel-row spacing (breaks the shared "3" spacing
convention throughout), and barycenter row-alignment (breaks the straight-spine property).
It needs either a **global multi-net router** (route all risers together with shared
congestion awareness instead of one-at-a-time greedy) or a **bus/collector topology** that
cuts the number of converging risers in dense regions — a real project, not a tweak.

A couple of `corner_cases/fluids` cases (`refinery_4`, `refinery_6`) fail for a related but
distinct reason: large *merged* same-fluid networks (after the same-fluid fix) hitting
routing congestion in a dense field — the dense-fluid-reach problem the adaptive-gap search
already mitigates but hasn't fully solved at this scale.

Why the tunnel-reach asymmetry settles the routing order: a **pipe-to-ground reaches 10 tiles,
an underground belt only 5**, so pipes cross the belt field far more easily than belts cross a
pipe field — routing **items first, fluids last** (the current order) is optimal.

## Tests

`tests/test_examples.py` runs **every** example against v2 (the default): each must compile,
and each must verify unless it's in `KNOWN_FAILING` (xfail). A ratchet test guards the pass
rate. `tests/test_generators.py` checks both `v1` and `v2` are reachable by name through the
`fgr.generators` registry and verify a simple graph (not a full v1 battery — see
`scripts/compare_generators.py` for that). `corner_cases/` is a standalone corpus (outside
the gating `tests/` glob) for failure-hunting and the v1/v2 comparison; it isn't wired into
`pytest`.
