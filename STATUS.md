# Status report

Three interchangeable layout **generators** live behind one interface (`fgr/generators.py`):
**v1** (the original fixed-grid + A\* rip-up router), **v2** (a deterministic **lane
fabric** — four fixed passes, no search), and **v3** (v2's placement + a **global
negotiated-congestion router**, PathFinder-style — see `docs/INSPIRATION.md` for the
lineage). The DSL, IR, and **verifier** (`fgr/verify.py`) are shared and unchanged — the
verifier is the independent oracle that grades whatever any generator produces. **v3 is the
default.**

```bash
.venv/bin/python -m pytest -q                                   # unit + regression + every example
.venv/bin/python scripts/compare_generators.py --corner-cases   # full 3-way head-to-head
```

## Results (v3, the default)

| Suite | Result |
|-------|--------|
| **pytest** | **120 passed, 0 xfailed** (green — the xfail tails are empty) |
| **examples/** (49 curated cases) that fully verify | **49 / 49** |
| **corner_cases/** (106 generated stress cases) that fully verify | **106 / 106** |

A "pass" means the layout was *independently graded* by `fgr/verify.py` as physically
realising the spec: every declared belt/pipe lane connects, nothing spurious, no overlaps,
fluids isolated and attached at real fluid-box tiles, **full power** (see below), and
**no item mixing on a belt lane**
— a belt has two sides, so it may carry at most two products, one per side (side-loading
keeps them separable; two products on ONE side jam in-game). Underground belt ends count
as side-loadable — their belt half takes side input in-game, with the hood blocking one
of the feeder's lanes (the lane-filter mechanic) — so a belt dead-ending against a
foreign tunnel is a real feed, not a no-op. `tests/test_examples.py::KNOWN_FAILING`
(the xfail'd tail) is **empty**; `corner_cases/` files each self-document their verdict in a
`# STATUS (engine <sha>)` header — refresh with `scripts/refresh_corner_case_status.py`.

## v1 vs v2 vs v3: comparative analysis

All three generators over the same **155 cases** (`examples/` + `corner_cases/`) through
`scripts/compare_generators.py`, each `(file, generator)` pair isolated in its own subprocess
with a 10s timeout (v1's search router can hang; a hang on one case must never take down the
comparison). Live numbers: run the script yourself.

| generator | pass rate | timeouts | avg compile\* | total entities\* | belt turns\* | tunnel crossings\* |
|---|---|---|---|---|---|---|
| v1 (A\* rip-up) | 118 / 155 | 11 | 472 ms | 66,687 | 4,113 | 2,523 |
| v2 (lane fabric) | 132 / 155 | 0 | **28 ms** | 265,618 | 1,867 | 9,880 |
| **v3 (global router)** | **155 / 155** | **0** | 133 ms | **60,104** | **211** | **2,297** |

<sub>\*each on that generator's own passing set.</sub>

- **Completeness.** v3 passes the entire battery — including every case in v2's tracked
  tail (congested belt risers: `fanin_asm_*`, `reconverge_*`, `butterfly_*`, `bus_4`;
  merged-fluid congestion: `refinery_4/6`; and the `examples/` stragglers `fluids_7`,
  `scale_1`, `scale_5`) and every case v1 hangs or mis-welds on. Nothing regressed. The
  lane-mixing check costs v2 six passes (its collectors merge different products onto
  one lane — the in-game jam the check exists to catch); v1 loses those six plus seven
  more to side-loadable underground ends (belts it dead-ends against foreign tunnels
  are real feeds in-game). v3 routes the same cases with same-product lanes,
  lane-separated pairs, and tunnel-aware weld checks instead.
- **Shape.** v3 is the *leanest* of the three: ~5.4× fewer entities than v2 on the corpus,
  and fewer even than v1 (which bought compactness with search). Belt turns collapse to 211
  total vs v2's 1,867 — merges and flexible pins remove almost every needless jog. Tunnel
  crossings are the lowest of the three, and belts never tunnel across open ground (a dive
  only wins when the surface is actually blocked).
- **Speed.** v3 averages 133 ms — ~5× v2, ~3.5× faster than v1, worst case under 5 s,
  never times out. The negotiation loop is bounded (20 rounds, early stall
  cutoff) and every search is A\* over a finite field, so there is no hang mode.
- **Determinism.** Same input → byte-identical layout (no RNG, no wall clock; verified on
  the hard cases).

## What v3 is

v2 routed lanes one at a time with pins fixed up-front; its tail was *global contention
attacked with local rules*. v3 keeps v2's placement (LAYER / ORDER / PLACE) and replaces the
whole EMIT stage with a global router (`fgr/layout_v3.py`):

1. **Nets, not lanes.** A producer's whole fan-out is ONE multi-terminal net — a directed
   belt tree: trunk + tap-inserter branches. Fluids are one net per same-fluid biclique
   group (merging never manufactures an undeclared producer→consumer pair).
2. **Flexible pins.** The search itself chooses the output-inserter face, the consumer face,
   a tap on the net's own committed tree, a single-inserter bridge between adjacent bodies —
   or a **merge into another net's branch** that flows to the same consumer **and carries
   the same product** (different products never share a belt: the verifier would allow one
   per side, but half a belt starves throughput). Merge hosts are validated by *true flow
   reach* (real inserter attachments, propagated transitively through grounded merges,
   cycle-guarded), so collector belts **emerge** where fan-in is dense instead of being a
   special case. The one forced exception — a sink whose distinct-product fan-in exceeds
   its inserter faces (say 8 products into a 1×1 chest) — pairs products onto shared pick
   tiles with **lane-separated side-loads from opposite sides**, one product per belt side,
   which the audit re-validates every round.
3. **Negotiated congestion (PathFinder).** All nets route with SOFT costs: foreign claims
   and weld-creating moves are passable at a price that grows each round, plus a history
   cost on chronically contended tiles. Each round rips up only the conflicted nets (merge
   webs rip transitively — rings would thrash forever otherwise) and reroutes them against
   the rest. Bounded rounds + stall cutoff + best-snapshot fallback: it cannot hang and
   cannot give up.
4. **A\* over (tile, direction-lock) states.** A tunnel exit cannot turn, so the state space
   carries the lock; searches dive under anything (machines included) with axis-line
   resources keeping same-axis tunnels from stealing each other's pairing.
5. **Emission is pure.** Routing produces per-net plans; entities are materialised in one
   final pass. The occ-vs-entity divergence bug class of v2 cannot exist here.

Legality inside the router mirrors `fgr/verify.py` exactly: inserter `direction` points at
its pickup; belts weld via accepting side-feeds (never head-on); underground belts pair with
the nearest same-axis entity within reach; pipes weld by 4-adjacency (a pipe-to-ground only
on its mouth side); a pipe attaches to a machine only ON a fluid-box external tile.

One adaptive axis remains from v2 — vertical clearance between stacked fluid machines
(`FLUID_VGAP` → 6 → 10), engaged only when a fluid graph doesn't verify at the base gap.
Everything v2's co-router searched over (fluid net order, boxed-out lanes) is handled by
negotiation instead.

### What a "pass" does and doesn't cover

The oracle grades **physical material-flow topology plus power**: placement (no overlaps),
node↔machine correspondence, every declared belt/pipe lane connected, no spurious lanes,
fluids isolated and attached at real boxes, per-lane item purity, and **a live power grid**
— every powered entity must sit in a substation supply area wire-connected to an
electric-energy-interface (the vanilla creative generator), so a pasted blueprint *runs*
in a sandbox world with zero manual fixes. Chest I/O is **full-belt**: infinity chests
feed belts through vanilla (hidden) 1×2 loaders and output chests swallow full belts the
same way — both lanes, no inserter bottleneck at the endpoints.

Still out of scope (by design, tracked):

- **Recipe↔machine category** (e.g. a chemistry recipe on an assembler) — checked by
  `fgr/fbsr_validation.py` against real Factorio data, deliberately kept out of the pure
  oracle (which holds no hard-coded recipe table).
- **Throughput** — connectivity only: that an item *can* reach a machine, not the rate
  (though endpoints and product-pure whole belts remove the two biggest chokepoints).
- **Rendering of electric poles** — this FBSR build silently draws no poles (all four
  vanilla types); the blueprint carries them and the verifier grades them, so the gallery
  images simply don't show the grid.

## Where it can still fail (v3's honest edges)

The tracked corpus is clean, so the edges are structural rather than case-by-case:

- **Negotiation is bounded.** A graph whose contention never converges within the round
  budget emits the best-scoring state and lets the verifier report it (and the vgap
  escalation retries fluid graphs with more room). No such case exists in the corpus today;
  the failure-hunting playbook is to grow `corner_cases/` until one does.
- **Placement is inherited from v2.** The router can only negotiate over the space placement
  leaves. Densely-packed columns with belt-only graphs have no escalation axis yet (a gutter
  widening retry is the obvious next lever if a case ever demands it).
- **Rates and power** as above — the oracle's scope, not the router's.

## Tests

`tests/test_examples.py` runs **every** example against the default generator (v3): each
must compile and verify — `KNOWN_FAILING` is empty, and a ratchet test guards the pass rate.
`tests/test_stress.py`'s tail set is likewise empty. `tests/test_generators.py` checks v1,
v2 and v3 are all reachable by name through the `fgr.generators` registry and verify a
simple graph (see `scripts/compare_generators.py` for the full battery). `corner_cases/` is
a standalone corpus (outside the gating `tests/` glob) for failure-hunting and the
generator comparison; it isn't wired into `pytest`.
