# Inspiration & prior art — compiling a DSL to a layout

Where to steal ideas from for the **DSL → placed layout** problem (`fgr/layout.py`), and
which slice of our pipeline each field illuminates. This is a *map for future work*, not a
plan — see `docs/V2_DESIGN.md` for what the generator actually does today.

## TL;DR

- **EDA / VLSI place-and-route ("electroschema") is the right primary lens** and the deepest
  one — but it's one of ~6 mature literatures that each cover a different slice of our
  pipeline. We already borrow from three (often without the names).
- Highest-value ideas we are **not** yet using: **register allocation** (for track rows +
  a real spill rule), **Brandes–Köpf** (for spine alignment), **Steiner trees / FLUTE** (for
  fan-out), full **negotiated-congestion routing on fluids** (PathFinder, done properly), and
  a **constraint-solver escape hatch** (CP-SAT/SMT) for the dense cases the deterministic
  passes can't place.
- There is **direct Factorio prior art** that frames the whole thing as a constraint model.

## Meta-point: our architecture is already an EDA pattern

Swappable *generator* + an independent *oracle* that grades the result = the EDA split
between a **P&R engine** and **DRC/LVS** (design-rule-check / layout-vs-schematic). Our
`fgr/verify.py` is essentially LVS+DRC. This validates the "verifier is the centerpiece"
principle ([[project-goal]]): it's the same problem shape the chip industry converged on.

## Our pipeline mapped to the literatures

| Pass | What we do now | Field it comes from | Upgrade we're missing |
|------|----------------|---------------------|-----------------------|
| **LAYER** (ASAP/ALAP columns) | longest-path depth | Sugiyama layer assignment **+ compiler list scheduling** (ASAP/ALAP are literally instruction-scheduling terms) | solid as is |
| **ORDER** (barycenter) | barycenter sweeps | Sugiyama crossing-minimization (Sugiyama–Tagawa–Toda) | median heuristic is sometimes better; fine |
| **PLACE** (`_primary_pred` spine align) | greedy "align to dominant predecessor" | Sugiyama **coordinate assignment** | **Brandes–Köpf**: the standard algorithm for exactly our goal — align nodes into vertical blocks so edges run straight with few bends. `_primary_pred` is a greedy approximation of it |
| **EMIT — track rows** (`_color_tracks`) | left-edge interval coloring | VLSI **channel routing** (already cited) **+ register allocation** | reframe as **linear-scan register allocation**: a belt lane is a *value* live from its producer column to its last-consumer column; rows are registers; **running out of rows = spilling** (detour to a far row). Gives a principled fallback for the high-fan-in tail instead of an ad-hoc rescue pass |
| **EMIT — fan-out** (one belt, many taps) | trunk + multi-tap inserters | **Steiner trees** | the tapped-belt is a degenerate rectilinear Steiner tree. **FLUTE / RSMT** give the optimal multi-terminal shape fast — relevant when one producer feeds consumers spread across many rows |
| **ROUTE** (`_pipe_path` BFS; v1 A*/rip-up) | Lee maze + "PathFinder-lite" | **EDA detailed routing**: Lee's maze = our BFS; **PathFinder negotiated-congestion** = v1's history cost | negotiation currently runs only on *belts (v1)*. The **fluid router is greedy** — and that's where the tail dies |
| **undergrounds / pipes-to-ground** | bounded-reach dives | **vias** — but with a pairing twist (see caveats) | — |
| **POWER overlay** | substation lattice | facility-coverage / set-cover | fine as a post-pass |

## Fields we're probably missing

### 1. Compilers — register allocation (the cleanest reframe)
The whole thing *is* a compiler ("compile a DSL to a layout"), and track-row assignment is
**register allocation by live-range coloring**, exactly. The payoff is not aesthetic: register
allocators have a **theory of spilling**. When a value can't be colored, spill it to memory and
reload. Our equivalent — "this producer's lane can't get a clean row near its consumers" —
currently has no principled fallback (hence the high-fan-in failures). Linear-scan + spill
heuristics give one.

### 2. Orthogonal graph drawing — Topology-Shape-Metrics (TSM)
A *different decomposition* of the whole problem: **planarize → orthogonalize → compact**. The
orthogonalize step is **Tamassia's bend-minimization**, which reduces "fewest turns" to a
**min-cost flow** and is provably bend-minimal for degree-≤4 plane graphs. We care about turns
(straight spines = clean) and currently minimize them greedily; this is the principled version.
- Tamassia min-cost-flow bend minimization: <https://link.springer.com/chapter/10.1007/978-3-642-25878-7_12>
- Orthogonal drawing survey (GD2019): <https://kam.mff.cuni.cz/conferences/gd2019/part1.pdf>

### 3. CGRA / spatial-architecture mapping — the closest *computing* analogy
Mapping a dataflow graph onto a 2D grid of processing elements with **fixed, limited
interconnect** is almost literally Factorio: PEs = machines, the scarce interconnect = our
underground-belt reach limit (5). FPGA P&R assumes flexible routing; **CGRA mapping is the
variant where routing is scarce and the router gives up** — precisely our failure mode. They
attack it with **ILP/SAT-based DFG mapping**.
- Graph-minor mapping: <http://courses.ece.ubc.ca/583/papers/99.pdf>
- ILP CGRA mapping: <https://arxiv.org/pdf/1901.11129>
- CGRA survey: <https://www.comp.nus.edu.sg/~tulika/CGRA-Survey.pdf>

### 4. Industrial engineering — Facility Layout Problem / QAP
We are literally laying out a factory. The **Facility Layout Problem** assigns machines to
locations minimizing **flow × distance** (from-to charts); the **Quadratic Assignment Problem**
is its core. Classic heuristics (CRAFT, CORELAP, ALDEP; Muther's Systematic Layout Planning) are
the OR ancestors of EDA placement. Main takeaway: the **objective function** — "minimize
material-flow-weighted distance" is a cleaner, throughput-aware placement target than our column
heuristic.

### 5. Microfluidic biochip physical design — our fluids problem has a dedicated literature
Continuous-flow biochips route **channels carrying actual fluid** between placed components: the
same P&R problem as our pipes, with the same physics we care about (minimize channel length,
intersections, bends). A recent three-stage method uses **force-directed quadratic placement +
negotiation-based routing** — the EDA toolkit applied to fluid. Strong hint that the fix for our
greedy fluid router is **full negotiated-congestion (PathFinder) on pipes**, not more
special-casing.
- Three-stage CFMB design: <https://www.mdpi.com/2079-9292/13/2/332>
- Any-angle fluid routing (AARF): <https://www.researchgate.net/publication/322346898_AARF_Any-Angle_Routing_for_Flow-Based_Microfluidic_Biochips>
- Path-driven fluid routing: <https://pmc.ncbi.nlm.nih.gov/articles/PMC12195421/>

### 6. Constraint solvers (CP-SAT / SMT / ASP) — plus direct Factorio prior art
Fits our stated principle perfectly ([[project-goal]]: the generator need not be deterministic,
only *verifiable*): encode placement + routing as constraints and let a solver find *a* legal
layout for the dense cases the deterministic passes can't. The oracle still grades the result, so
adopting this is "free" architecturally.
- **Towards Automatic Design of Factorio Blueprints** (ModRef 2023) — frames blueprint design as
  a constraint model "interleaving bin-packing, routing, and network design":
  <https://arxiv.org/abs/2310.01505> ·
  <https://modref.github.io/papers/ModRef2023_TowardsAutomaticDesignOfFactorioBlueprints.pdf>
- **Factorio-SAT** (SAT solvers for belts/balancers): <https://github.com/R-O-C-K-E-T/Factorio-SAT>
- **VeriFactory** (a blueprint verifier — parallels our oracle): <https://github.com/alegnani/verifactory>

## Where the EDA analogy breaks (don't over-borrow)

- **Belts are directed and carry throughput; wires are undirected and dimensionless.** Once we go
  past connectivity to *rate* (a tracked TODO), the right model is **network flow with
  conservation**, not EDA routing. QAP / microfluidics handle this better than VLSI.
- **Undergrounds have *pairing* semantics** (nearest same-axis entrance↔exit) that vias don't — a
  routing-legality constraint with no EDA analog. We've already hit this (the "mispairing trap").
- **Inserters are active placed entities** (a port that costs a tile and has reach 1–2), not free
  pins. Closer to CGRA "the interconnect is itself a placed resource."
- **Splitters / balancers are network *design*, not P&R** — the sub-problem the Factorio-SAT work
  focuses on; genuinely separate from layout.
- **No clock/timing** — but throughput *ratios* are a kind of retiming.

## If we pick what to actually pull in (ranked by leverage on the failing tail)

The tail is two things: *dense-field routing where greedy gives up* (fluids) and *high-fan-in
port routing* (see `STATUS.md`).

1. **Full negotiated-congestion on the fluid router** — we already have the pattern in v1 belts;
   the microfluidics literature confirms it's the right tool for fluid channels. Biggest bang for
   the tracked failures.
2. **A CP-SAT escape hatch** for the handful of cases the deterministic passes can't place — the
   verifiable-generator architecture makes this cheap to adopt. The ModRef paper is a template.
3. **Register-allocation framing** of track rows → a real spill rule for high fan-in.
4. **Brandes–Köpf + Steiner/FLUTE** for cleanliness (the stated #1 goal), once the correctness
   tail is closed.
