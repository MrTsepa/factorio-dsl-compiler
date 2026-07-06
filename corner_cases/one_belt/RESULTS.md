# ONE-BELT suite — results (engine as of this commit)

A full yellow belt (15/s) of each of 61 base-game items; graded by the verifier
+ the SIDE-AWARE placed-layout flow oracle. `/goal`: all 61 passing; metric:
entity count (lower is better).

**39 / 61 carry the full belt** (50 compile as banks; total passing entities 419,893, median 3601), 8 verify but fall short, 3 fail
verification, 6 too big for the routed fallback, 3 verify-timeouts (the flow
oracle needs a straight-run collapse for 50k+ entity layouts), 0 errors.

| class | items | next fix |
|---|---|---|
| SHORT ~14.7 (one lane short at exit) | boiler, burner_inserter, transport_belt, storage_tank, electric_mining_drill | collector lane-group budgeting off-by-one |
| SHORT mid | fast_transport_belt 8.6, medium_electric_pole 13.1, steam_engine 13.9, lab | multi-port arm dealing shortfalls |
| VERIFY-FAIL | long_handed_inserter, pump, processing_unit | overlaps in dense multi-lh margins |
| TIMEOUT (150 s) | big_electric_pole, chemical_science, electric_engine_unit, electric_furnace, lab | verify/oracle cost on huge builds |
| TOO-BIG (routed fallback) | advanced_circuit, am2, bulk_inserter, flying_robot_frame, production_science, substation | >3-ingredient stages -> bank v3 |

Entity leaderboard (passing, lowest): copper_cable (176), iron_stick (176), pipe (235), plastic_bar (268), iron_gear_wheel (289), small_electric_pole (360), stone_brick (432), sulfur (470)
