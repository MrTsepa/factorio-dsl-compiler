# ONE-BELT suite — results (engine as of this commit)

One belt (15/s) of every bulk item, realistic targets for the rest (science =
1/s = 60 SPM, buildings = 1/s). Graded by the verifier + the side-aware
placed-layout flow oracle. `/goal`: all 61 passing — **COMPLETE**.

**61 / 61 pass, all as banks.** 294,611 total entities, median 1478, zero verify failures, zero flow shortfalls, zero
timeouts, zero errors.

Leanest: stone_furnace (54), copper_cable (176), iron_stick (176), boiler (216), automation_science_pack (226), pipe (240), plastic_bar (268), iron_gear_wheel (289)

Largest: production_science_pack (15445), splitter (18185), medium_electric_pole (19084), bulk_inserter (21315), steel_chest (34906), big_electric_pole (64826)

Entity count is the standing optimization metric (lower is better); the
largest builds spend most of their entities on block replication and margin
plumbing — stage-order optimization and mirrored blocks are the tracked
reducers.
