# ONE-BELT suite — results (engine as of this commit)

A full yellow belt (15/s) of each of 61 base-game items; specs generated
from real recipe data by `scripts/belt_suite.py --gen`, graded by the
verifier + the placed-layout flow oracle (`--run`, subprocess-isolated,
150 s/case). Regenerate any time — this file records the current break map.

**15 / 61 carry the full belt** (4 as compact banks), 2 verify but fall short, 41 too big for the routed
fallback (>100 nodes), 3 time out in v3 negotiation. 0 verifier
failures, 0 errors.

| outcome | items |
|---|---|
| full belt (bank) | copper_cable, electronic_circuit, iron_stick, pipe |
| full belt (routed) | battery, burner_inserter, concrete, explosives, firearm_magazine, iron_chest, iron_gear_wheel, plastic_bar, stone_brick, stone_furnace, sulfur |
| short | small_electric_pole (14.975/s), transport_belt (13.722/s) |
| v3 timeout (150 s) | inserter, rail, underground_belt |

## Why the bank template falls back (the v2 priority list, measured)

1. **DAG shapes (~18 items)** — a stage consumes a NON-adjacent stage's
   product (inserter, lab, radar, splitter, …). Fix: multi-bus rows, both
   machine faces.
2. **Fluids in the chain (17)** — battery-tier and up. Fix: a pipe row in
   the sandwich.
3. **>2 collector lanes (12)** — blocks > 2 at 15/s of mid-tier items.
   Fix: mirrored blocks sharing collector rows (facing arm rows fill
   opposite lanes) + multi-belt outputs.
4. **Scale (41 too-big, 90..2,978 machines)** — a full belt of high-tier
   items is a megabase, not a blueprint; per-tier targets (items/min)
   would make these meaningful, the rest is bank-v2 coverage.
