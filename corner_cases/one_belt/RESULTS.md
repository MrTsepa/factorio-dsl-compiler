# ONE-BELT suite — results (engine as of this commit)

A full yellow belt (15/s) of each of 61 base-game items; specs generated
from real recipe data by `scripts/belt_suite.py --gen`, graded by the
verifier + the SIDE-AWARE placed-layout flow oracle (`--run`). The oracle
now agrees with in-game measurements: a one-sided routed merge is ONE lane
(7.5/s) no matter how many chests you'd be tempted to add — the boundary
contract is one output chest per full belt.

**7 / 61 honestly carry the full belt** (5 as compact banks), 10 verify but fall short (routed one-lane merges at 7.5/s,
plus near-misses), 41 too big for the routed fallback, 3
time out in v3 negotiation. 0 verifier failures, 0 errors.

| outcome | items |
|---|---|
| full belt (bank) | copper_cable, electronic_circuit, iron_stick, pipe, small_electric_pole |
| full belt (routed, both-lane merge) | plastic_bar, sulfur |
| short | battery (7.5/s), burner_inserter (7.5/s), concrete (7.5/s), explosives (7.5/s), firearm_magazine (7.5/s), iron_chest (7.5/s), iron_gear_wheel (7.5/s), stone_brick (7.5/s), stone_furnace (7.5/s), transport_belt (14.724/s) |
| v3 timeout (150 s) | inserter, rail, underground_belt |

## The roadmap this measures (priority order)

1. **Fluids in banks** — every chem item (battery, concrete, sulfur*,
   plastic*, explosives) needs the bank's lane weave to fill both output
   lanes; the routed path caps at one lane. (*pass today only because their
   routed merges happen to enter from both sides — geometry luck the oracle
   now grades per layout.)
2. **Mirrored blocks / multi-belt outputs** — unlocks >15/s and most of the
   41 too-big items, along with per-tier targets (items/min).
3. **fastbelt-class long-hauls** — a 5-per-craft ingredient overflows one
   drop-fed lane; needs two long-haul rows or adjacency-aware reordering.
