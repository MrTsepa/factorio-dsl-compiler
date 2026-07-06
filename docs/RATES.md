# Throughput: rate metadata today, ratio-exact generation next

Two asks drive this design: **(A)** every blueprint should carry metadata declaring the
rates it can sustain, and **(B)** the compiler should eventually take a *target* rate
("1 red science / s") and generate a layout sized exactly for it — the right machine
counts, the right number of input belts, the right feeding hardware. A is implemented
(`fgr/rates.py`); B and C are designed here.

Everything is computed from **Factorio's own prototype data** via the FBSR dump pipeline
(no hard-coded tables), consistent with the project's oracle philosophy.

## The physics (all from dumps, cross-checked against the wiki/FFF)

| quantity | source | vanilla value |
|---|---|---|
| machine craft rate | `crafting_speed / energy_required × result_amount` | am2 0.75× · chem 1× · furnace 2× |
| recipe time | recipe `energy_required` (default **0.5 s** when absent) | e.g. gear 0.5 s, red science 5 s |
| belt throughput | `speed` (tiles/tick) × 60 × 8 items/tile | yellow 15/s full, **7.5/s per lane** |
| loader | belt-coupled: full belt | 15/s |
| inserter swing | `rotation_speed` (turns/tick): 180° there + 180° back = `1/rotation_speed` ticks/item | inserter ≈ **0.83 items/s** (stack 1) |
| fluids (2.0) | segment model: contents available anywhere within segment extents (≤ ~250 tiles; pumps beyond) | effectively **∞ at our scale** |

Two consequences worth internalizing:

1. **Inserters are the real bottleneck**, not belts. A single yellow inserter moves
   ~0.83 items/s; an am2 crafting transport-belts *consumes gears at 1.5/s*. Any
   machine-feeding edge served by one inserter caps there. (This is why chest I/O moved
   to loaders; machines can't use loaders as flexibly, so machine feeds need inserter
   *counts* — or higher tiers / stack research, which we don't assume in a fresh sandbox.)
2. **Fluids don't bottleneck.** After the 2.0 rework a pipe network is a segment whose
   content is available at every port; our networks are far below the extent limit. Rate
   analysis can treat fluid lanes as uncapacitated (flagging extents if we ever grow).

## Stage A — rate metadata (IMPLEMENTED)

`fgr/rates.py` computes, for a spec + its compiled layout:

- **Per machine**: max craft rate (crafts/s and product items/s) from dump data.
- **Backward demand pass** (DAG): for each output chest, the crafts/s each node must
  sustain per 1 item/s delivered — merges split demand across same-product suppliers
  proportionally to their caps (labeled an estimate; exact for the common tree case).
- **Operating point**: the max *uniform* rate at which every output can run
  simultaneously, and each output's *solo* max. Bottleneck node reported.
- **Per-edge flow vs link capacity**: the layout knows how each lane is realized —
  loader (15/s), belt lane (7.5/s tap-fed single lane, 15/s loader-fed), inserter
  (swing-rate from the dump, per inserter serving that edge). Saturated links are
  flagged with their utilization.

Surfaces: `python -m fgr rates <file>` (human report + `--json`); the blueprint's
**in-game description field** (paste it and read the expected rates in the tooltip);
the landing page card meta line.

What Stage A deliberately does not do: dynamics (warm-up, buffering — the "gears took a
while to arrive" effect), belt saturation interactions, or quality/modules.

## Stage B — the ratio solver (design)

DSL grows a target:

```
target out : 1/s          # or: 0.5/s, 30/min
```

Solve on the spec graph, before layout:

1. Backward pass gives required crafts/s per node for the target(s).
2. **Machine multiplicity**: `N(node) = ceil(required_crafts / machine_cap)` — a node
   becomes a *bank* of N identical machines. (Rational ratios like 6.67 am2 for
   1 red/s → 7 machines at 95% utilization; report the slack.)
3. **Lane sizing**: each edge's required items/s → number of belt lanes
   (`ceil(rate / 15)` full belts when loader/side-fed pairs carry both lanes, `/7.5`
   for tap-fed) and **inserter counts** per machine (`ceil(ingredient_rate /
   inserter_cap)` input arms, same for output).
4. **Input belt count**: raw inputs get `ceil(total_draw / 15)` infinity-chest+loader
   feeds — "correct number of input belts" falls out of the same arithmetic.

Output: a *sized graph* — same IR, nodes annotated with `count`, edges with `lanes`
and `arms`. The verifier gets a new **static capacity check**: realized carrier
capacity ≥ required flow on every lane (it already knows every carrier's identity;
capacities come from the dumps). That keeps the oracle in charge: a layout PASSES at
rate T only if every link physically sustains T.

## Stage C — banked layout (design)

The layout engine learns to place a node with `count = N` as a **bank**: a row/column
of N machines sharing input belts down the seam(s) and an output belt collecting via
per-machine arms — exactly the classic human pattern (and our `furnace_stack` corner
cases already stress the shape). Sketch:

- Placement: banks tile perpendicular to the spine; a bank is a super-node with the
  same face/pin interface, so **the v3 router is unchanged** — it sees wider bodies
  with more terminals.
- Multi-lane edges route as parallel nets with a shared merge discipline (the lane
  purity rules already handle pairing; N lanes of one product may merge freely).
- Inserter counts per machine come from Stage B annotations; faces are chosen by the
  existing negotiation.

Risks, called out now: bank-aware placement is real work (est. the largest single
change since v3); multi-lane fan-in multiplies riser congestion (negotiation already
handles same-product merges, which caps the blast radius); verifier capacity check
must model lane merges correctly (sum of tributary flows ≤ lane cap at every tile —
computable on the existing flow graph).

## Stage D — simulate the blueprint in the real game (IMPLEMENTED)

The blueprints became **self-contained runnable worlds** the moment power (EEI),
infinity chests and loaders landed — which makes true end-to-end simulation cheap:
**use Factorio itself, headless, as the dynamic oracle**. No reimplemented belt
physics to drift out of sync.

Mechanics (all standard tooling):

1. `fgr simulate <case>`: generate a **scenario folder** whose `control.lua` embeds
   the blueprint string; `on_init` stamps it onto the surface (entities revive
   instantly in the scenario), then samples `force.item_production_statistics`
   every k ticks and `game.write_file(...)`s a JSON time-series to `script-output`.
2. Run `factorio --benchmark-scenario` (headless; BYO binary via `FACTORIO_HOME`,
   same convention as FBSR) for N ticks — a 60-second factory-run simulates in
   ~a second of wall time.
3. Compare: measured steady-state output rate vs Stage A's predicted rate
   (assert within tolerance), plus **warm-up time** to steady state — exactly the
   "gears took a while to reach red science" effect, quantified.

Implementation: `scripts/get_factorio.sh` installs the **free headless server build**
(no account; pinned via `https://factorio.com/get-download/<ver>/headless/linux64`)
into `out/_factorio_sim/`; `scripts/simulate.py` runs it natively on Linux or through
docker (OrbStack/Rosetta) on macOS, against a **private write dir** (never a real
game install), expansions disabled, recipes enabled *without* researching techs (tech
grants inserter-capacity bonuses a fresh world doesn't have — the rates model targets
fresh-world hardware).

**Methodology** (hard-won; the first attempt got this wrong): sample every output
chest once per game-second; discard everything before `first_item + 30 s`; split the
remaining window in half and require the two slopes to agree within 8% — otherwise
the series is a **transient** and is refused, not reported (a fixed "last 60%"
window once averaged 126 s of zeros into red science's rate and reported a number
that was neither warm-up nor steady state). The engine is deterministic, so repeated
runs are meaningless as error bars — the honest sensitivity axis is window choice,
which the split-half test probes; quantization is flagged when a window holds <30
items. Chests must stay far from full (a saturating chest reads as a fake steady
state); production_statistics is the planned cross-check.

**Results at joint steady state** (science_3 needed 30 game-minutes; ~88 s wall):

| output | binding constraint (predicted) | predicted | measured | error |
|---|---|---|---|---|
| gears | inserter arm | 0.42/s | 0.454/s | +8% |
| circuits | inserter arm | 0.28/s | 0.286/s | +2% |
| science_3 red | its own machine (solo cap) | 0.150/s | **0.150/s** | 0% |
| science_3 green | its own machine | 0.125/s | **0.125/s** | 0% |
| science_3 military | inserter chain | 0.084/s | 0.0938/s | +12% |

Two lessons the game taught the model: (1) **machine-limited predictions are exact**;
inserter-limited ones run 2–12% above the pure-rotation swing estimate (real arms
overlap pickup with travel — the model is deliberately conservative). (2) The game's
equilibrium is **not the uniform fair-share point**: each output runs as fast as its
own constraints allow given shared supply (red hit its solo cap even with everything
else running). Warm-up is real and long on shared tapped belts — military's first
pack at 89 s, red's at 318 s, green's at 470 s.

The verifier chain is now *spec → physical topology (oracle) → game-accurate recipes
(dumps) → measured throughput (the game itself)* — no stronger ground truth exists.

(An in-repo discrete-event simulator was considered and parked: fast and CI-friendly,
but it reimplements belt/inserter physics that WILL drift from the game; headless runs
on the dev machine are the honest version. Revisit only if we need simulation in
environments without a game install.)

## Order of execution

1. ✅ Stage A (this commit): metadata + bottleneck honesty on every blueprint.
2. Stage B solver + verifier capacity check — pure math + oracle, no layout risk.
3. Stage C banks — behind a flag, graded case-by-case like every generator change.
4. ✅ Stage D headless simulation — closed the loop: machine-limited predictions
   exact; inserter-limited within 2–12% (conservative by design).

## Sources

- [FFF-416 — Fluids 2.0](https://factorio.com/blog/post/fff-416),
  [FFF-430 — Drowning in Fluids](https://factorio.com/blog/post/fff-430) (segment
  model, extents), [forum: 2.0 fluid throughput](https://forums.factorio.com/viewtopic.php?t=119151)
- [wiki: Belt transport system](https://wiki.factorio.com/Belt_transport_system)
  (15/s yellow, 8 items/tile), [wiki: Inserters](https://wiki.factorio.com/Inserter)
  (swing throughput)
- Prototype numbers: our own FBSR dumps (`fgr/fbsr_validation.py` loaders).
