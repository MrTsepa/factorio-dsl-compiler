# ONE-BELT suite — results (engine as of this commit)

One belt (15/s) of every bulk item, REALISTIC targets for the rest (science =
1/s = 60 SPM, buildings = 1/s — user-set policy). Graded by the verifier + the
side-aware placed-layout flow oracle. `/goal`: all 61; metric: entities.

**59 / 61 pass** (58 as banks + flying_robot_frame routed; 263,015 total entities, median 1365). Remaining 3 = the 4-ingredient stages (assembling-machine-2, bulk-inserter, flying-robot-frame): the bank template
fits 3 item rows; the 4th needs paired lanes or per-machine feeder stubs —
the last tracked geometry.

Leanest: stone_furnace (54), copper_cable (176), iron_stick (176), boiler (216), automation_science_pack (226), pipe (240), plastic_bar (268), iron_gear_wheel (289)
Largest: splitter (18185), medium_electric_pole (19084), steel_chest (34906), big_electric_pole (64826)
