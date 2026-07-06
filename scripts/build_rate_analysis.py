#!/usr/bin/env python
"""Build docs/rate_analysis.html: the throughput deep-dive (data from rate_study.py).

Method notes: steady-state estimation follows standard simulation output analysis --
MSER-5 truncation for the initial transient (White 1997; the default rule in the DES
literature) and batch-means confidence intervals on the retained window. Figures use
the dataviz reference palette (dark mode slots, validated set).
"""
from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "out" / "rate_study"
OUT = ROOT / "docs" / "rate_analysis.html"

# dataviz reference palette, dark-mode column (pre-validated set)
SURFACE = "#1c1f24"
CARD = "#15171b"
INK = "#e8eaed"
MUT = "#9aa3af"
GRID = "#3a414b"
BLUE = "#3987e5"
AQUA = "#199e70"
YELLOW = "#c98500"
VIOLET = "#9085e9"
RED = "#e66767"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": MUT, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.6, "grid.alpha": 0.5,
    "xtick.color": MUT, "ytick.color": MUT, "text.color": INK,
    "font.size": 11, "axes.titlesize": 12.5, "axes.titlecolor": INK,
    "legend.frameon": False, "lines.linewidth": 2.0,
    "axes.spines.top": False, "axes.spines.right": False,
})


def load(name):
    return json.loads((STUDY / f"{name}.json").read_text())


def series(data, item, key="out"):
    return [(s["tick"] / 60.0, (s.get(key) or {}).get(item, 0))
            for s in data["samples"]]


def increments(pts):
    """Per-sample production increments (items per sample interval)."""
    out = []
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        out.append((t1, (v1 - v0) / (t1 - t0)))
    return out


def mser(incs, batch=5):
    """MSER-`batch` truncation: batch the increment series, pick the truncation d*
    minimizing the marginal standard error of the retained mean. Returns
    (d_seconds, retained_batches)."""
    zs = []
    for i in range(0, len(incs) - batch + 1, batch):
        chunk = incs[i:i + batch]
        zs.append((chunk[-1][0], sum(v for _, v in chunk) / batch))
    best = None
    n = len(zs)
    for d in range(0, n // 2):
        tail = [v for _, v in zs[d:]]
        m = sum(tail) / len(tail)
        var = sum((v - m) ** 2 for v in tail) / max(len(tail) - 1, 1)
        score = var / len(tail)
        if best is None or score < best[0]:
            best = (score, d)
    d = best[1] if best else 0
    return (zs[d][0] if d < n else 0), zs[d:]


def batch_means_ci(zbatches, k=12):
    """Group retained MSER batches into ~k macro-batches; t-CI on their means."""
    vals = [v for _, v in zbatches]
    if len(vals) < k * 2:
        k = max(2, len(vals) // 2)
    size = len(vals) // k
    means = [sum(vals[i * size:(i + 1) * size]) / size for i in range(k)]
    m = sum(means) / k
    s = math.sqrt(sum((x - m) ** 2 for x in means) / (k - 1))
    t = {2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31,
         9: 2.26, 10: 2.23, 11: 2.20, 12: 2.20}.get(k, 2.20)
    half = t * s / math.sqrt(k)
    return m, half


def ols(pts):
    n = len(pts)
    sx = sum(t for t, _ in pts)
    sy = sum(v for _, v in pts)
    sxx = sum(t * t for t, _ in pts)
    sxy = sum(t * v for t, v in pts)
    d = n * sxx - sx * sx
    return (n * sxy - sx * sy) / d if d else 0.0


def png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight",
                facecolor=SURFACE)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def fig_warmup():
    d = load("science_3")
    packs = [("automation-science-pack", "red science", RED),
             ("logistic-science-pack", "green science", AQUA),
             ("military-science-pack", "military science", YELLOW)]
    fig, ax = plt.subplots(figsize=(9.2, 4.2))
    for i, (item, label, color) in enumerate(packs):
        pts = series(d, item)
        ax.plot([t / 60 for t, _ in pts], [v for _, v in pts],
                color=color, label=label)
        first = next((t for t, v in pts if v > 0), None)
        if first:
            ax.axvline(first / 60, color=color, lw=1, ls=":", alpha=0.6)
            ax.annotate(f"{label.split()[0]} first at {first / 60:.1f} min",
                        (first / 60, 0), xytext=(first / 60 + 0.25, 36 - 13 * i),
                        color=color, fontsize=9)
    # the window the old fixed rule used for red (last 60% of an 8-min run)
    ax.axvspan(192 / 60, 480 / 60, color=RED, alpha=0.08)
    ax.annotate("window the fixed 'last 60%' rule fit for red\nin the 8-min run "
                "(starts inside 2 min of zeros)", (192 / 60, 150), color=MUT,
                fontsize=9)
    ax.set_xlabel("game time (minutes)")
    ax.set_ylabel("items delivered (cumulative)")
    ax.set_title("science_3 — outputs fill in belt-priority order; warm-up spans minutes")
    ax.legend(loc="upper left")
    return png(fig)


def fig_rolling():
    d = load("science_3")
    pts = series(d, "automation-science-pack")
    W = 120
    ts, rs = [], []
    for i in range(len(pts)):
        t1, v1 = pts[i]
        j = next((j for j in range(i, -1, -1) if pts[j][0] <= t1 - W), None)
        if j is None:
            continue
        t0, v0 = pts[j]
        ts.append(t1 / 60)
        rs.append((v1 - v0) / (t1 - t0))
    fig, ax = plt.subplots(figsize=(9.2, 3.9))
    ax.plot(ts, rs, color=RED, label="rolling rate (120 s window)")
    for y, lbl, c in ((0.150, "machine cap 0.150 (measured plateau)", INK),
                      (0.092, "what the fixed window reported: 0.092", VIOLET),
                      (0.084, "sustained estimate 0.084 (model)", MUT)):
        ax.axhline(y, color=c, lw=1.2, ls="--", alpha=0.75)
        ax.annotate(lbl, (ts[-1], y), ha="right", va="bottom", color=c, fontsize=9)
    ax.set_xlabel("game time (minutes)")
    ax.set_ylabel("red science / s")
    ax.set_ylim(0, 0.19)
    ax.set_title("red science — a transient, then a clean plateau the fixed window never saw")
    return png(fig)


def fig_estimators():
    d = load("science_3")
    pts = series(d, "automation-science-pack")
    # (a) the old rule on the 8-min prefix
    prefix = [(t, v) for t, v in pts if t <= 480]
    fixed = ols(prefix[int(len(prefix) * 0.4):])
    # (b) endpoint + (c) OLS on post-steady of the FULL run
    first = next(t for t, v in pts if v > 0)
    steady = [(t, v) for t, v in pts if t >= first + 30]
    endpoint = (steady[-1][1] - steady[0][1]) / (steady[-1][0] - steady[0][0])
    olss = ols(steady)
    # (d) MSER-5 + batch means on the full run
    incs = increments(pts)
    d_at, retained = mser(incs)
    m, half = batch_means_ci(retained)
    fig, ax = plt.subplots(figsize=(8.6, 3.4))
    names = ["fixed 'last 60%'\n(8-min run)", "OLS after first item\n(30-min run)",
             "endpoint slope\n(30-min run)", "MSER-5 + batch means\n(30-min run)"]
    vals = [fixed, olss, endpoint, m]
    colors = [VIOLET, BLUE, BLUE, AQUA]
    bars = ax.barh(names, vals, color=colors, height=0.55)
    ax.errorbar([m], [3], xerr=[half], fmt="none", ecolor=INK, capsize=4, lw=1.5)
    ax.axvline(0.150, color=INK, lw=1.2, ls="--", alpha=0.8)
    ax.annotate("plateau 0.150", (0.150, -0.45), color=INK, fontsize=9, ha="center")
    for b, v in zip(bars, vals):
        ax.annotate(f"{v:.3f}", (v, b.get_y() + b.get_height() / 2),
                    va="center", ha="left", color=INK, fontsize=9,
                    xytext=(4, 0), textcoords="offset points")
    ax.set_xlim(0, 0.185)
    ax.set_xlabel("estimated red science / s")
    ci = f"±{half:.4f}" if half >= 5e-5 else "±<0.0001 — deterministic clockwork"
    ax.set_title(f"estimator comparison — MSER-5 truncates at t={d_at:.0f}s, CI {ci}")
    ax.grid(axis="y", alpha=0)
    return png(fig)


def fig_primitives():
    rows = [
        ("inserter\nchest→chest", "micro_pure_inserter", 0.84, "rotation only (0.014×60)"),
        ("long-handed\nchest→chest", "micro_long_inserter", 1.20, "rotation only (0.02×60)"),
        ("inserter\nbelt→chest", "micro_inserter_from_belt", 0.84, "no belt-pickup model"),
        ("loader + belt", "micro_loader_belt", 15.0, "belt speed × 8/tile"),
    ]
    meas, analytic, names = [], [], []
    for label, exp, model, _note in rows:
        d = load(exp)
        pts = series(d, "iron-plate")
        first = next(t for t, v in pts if v > 0)
        win = [(t, v) for t, v in pts if t >= first + 20]
        meas.append((win[-1][1] - win[0][1]) / (win[-1][0] - win[0][0]))
        analytic.append(model)
        names.append(label)
    fig, axs = plt.subplots(1, 2, figsize=(9.2, 3.4),
                            gridspec_kw={"width_ratios": [3, 1]})
    x = range(3)
    ax = axs[0]
    ax.bar([i - 0.17 for i in x], analytic[:3], width=0.3, color=MUT,
           label="analytic (dump formula)")
    ax.bar([i + 0.17 for i in x], meas[:3], width=0.3, color=BLUE,
           label="measured (game)")
    for i in x:
        ax.annotate(f"{analytic[i]:.2f}", (i - 0.17, analytic[i]), ha="center",
                    va="bottom", fontsize=9, color=MUT)
        ax.annotate(f"{meas[i]:.3f}", (i + 0.17, meas[i]), ha="center",
                    va="bottom", fontsize=9, color=INK)
    ax.set_xticks(list(x))
    ax.set_xticklabels(names[:3], fontsize=9)
    ax.set_ylabel("items / s")
    ax.set_title("inserter primitives: quantized ticks beat the analytic formula")
    ax.legend(fontsize=9)
    ax.grid(axis="x", alpha=0)
    ax = axs[1]
    ax.bar([-0.17], [analytic[3]], width=0.3, color=MUT)
    ax.bar([0.17], [meas[3]], width=0.3, color=BLUE)
    ax.annotate(f"{analytic[3]:.1f}", (-0.17, analytic[3]), ha="center",
                va="bottom", fontsize=9, color=MUT)
    ax.annotate(f"{meas[3]:.2f}", (0.17, meas[3]), ha="center", va="bottom",
                fontsize=9, color=INK)
    ax.set_xticks([0])
    ax.set_xticklabels([names[3]], fontsize=9)
    ax.set_title("belt path: exact")
    ax.grid(axis="x", alpha=0)
    return png(fig), meas


def fig_contention():
    d = load("contention")
    keys = sorted({k for s in d["samples"] for k in (s.get("chests") or {})})
    fig, ax = plt.subplots(figsize=(9.2, 3.9))
    labels = {}
    rates = {}
    for k, color, lbl in zip(keys, (BLUE, YELLOW), ("upstream tap (belt1)",
                                                    "downstream tap (belt2)")):
        pts = series(d, k, key="chests")
        ax.plot([t / 60 for t, _ in pts], [v for _, v in pts], color=color, label=lbl)
        first = next((t for t, v in pts if v > 0), None)
        win = [(t, v) for t, v in pts if first and t >= first + 20]
        r = (win[-1][1] - win[0][1]) / (win[-1][0] - win[0][0]) if len(win) > 5 else 0
        rates[lbl] = r
        labels[k] = lbl
        ax.annotate(f"{r:.3f}/s", (pts[-1][0] / 60, pts[-1][1]), color=color,
                    ha="right", va="bottom", fontsize=10)
    ax.set_xlabel("game time (minutes)")
    ax.set_ylabel("transport belts delivered (cumulative)")
    ax.set_title("one gear line, two identical consumers — the upstream tap takes ~17×")
    ax.legend(loc="upper left")
    return png(fig), rates


def main() -> int:
    f_warm = fig_warmup()
    f_roll = fig_rolling()
    f_est = fig_estimators()
    f_prim, prim_meas = fig_primitives()
    f_cont, cont_rates = fig_contention()

    html = HTML_TEMPLATE
    for key, val in (("WARMUP", f_warm), ("ROLLING", f_roll), ("ESTIMATORS", f_est),
                     ("PRIMITIVES", f_prim), ("CONTENTION", f_cont)):
        html = html.replace(f"@{key}@", val)
    OUT.write_text(html)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
    return 0


HTML_TEMPLATE = r"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>fgr — throughput: measurement & modeling deep-dive</title>
<style>
:root{--bg:#1c1f24;--card:#262b33;--panel:#15171b;--ink:#e8eaed;--mut:#9aa3af;
--line:#3a414b;--acc:#ffb454;--blue:#3987e5;--aqua:#199e70;--red:#e66767;--violet:#9085e9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:36px 24px 90px}
h1{font-size:30px;margin:0 0 6px}h2{font-size:21px;margin:44px 0 12px;
border-bottom:1px solid var(--line);padding-bottom:8px}
h3{font-size:16px;margin:22px 0 8px}
p{margin:10px 0}.mut{color:var(--mut)}
a{color:var(--acc)}code{font-family:ui-monospace,Menlo,monospace;font-size:13px;
background:var(--panel);padding:1px 5px;border-radius:4px}
img{width:100%;border-radius:10px;border:1px solid var(--line);margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:12px 0}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600}
.decision{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--acc);
border-radius:10px;padding:14px 18px;margin:14px 0}
.finding{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:12px 16px;margin:12px 0;font-size:14px}
.finding b:first-child{color:var(--acc)}
.good{color:#4caf50}.bad{color:var(--red)}
</style></head><body><div class='wrap'>

<h1>Throughput: measurement &amp; modeling</h1>
<p class='mut'>A deep-dive to replace ad-hoc heuristics with a defensible methodology.
All data below is measured in the real game (headless 2.0.77, deterministic, fresh-world
hardware, expansions off) via <code>scripts/rate_study.py</code>; every figure is
reproducible from <code>out/rate_study/*.json</code>.</p>

<div class='decision'><b>Proposed decisions (for review)</b>
<ol style='margin:8px 0 2px'>
<li><b>Measurement</b>: adopt MSER-5 truncation + batch-means confidence intervals
(the standard simulation output-analysis method) with run-length control: extend the
run until the CI half-width is &lt; 3% of the mean. Retire the split-half/fixed-window
heuristics.</li>
<li><b>Primitive capacities</b>: calibrate from micro-benchmarks measured in the game
(cached per game version), not analytic formulas — the game quantizes inserter swings
to whole ticks and belt-pickup differs from chest-pickup; formulas can't keep up.</li>
<li><b>Allocation model</b>: predict multi-consumer sharing with <b>belt-position
priority</b> (upstream tap first), not proportional splits — measured contention is a
17:1 split, unambiguous.</li>
<li><b>Prediction architecture</b>: a three-layer stack — static bounds from dumps
(design-time), a calibrated priority-flow model (fast metadata), and game-in-the-loop
simulation (ground truth for anything that matters). The layers cross-check each
other; no layer pretends to be what it isn't.</li>
</ol></div>

<h2>1 · What we measure, and why it went wrong</h2>
<p>The harness samples every output chest once per game-second and fits a rate to the
cumulative series. The first estimator was a fixed window ("last 60% of samples") —
which silently <i>assumes</i> the series is steady inside that window. It isn't:
warm-up on shared tapped belts spans <b>minutes</b>, because belts fill consumers in
priority order (§3).</p>
<img src='data:image/png;base64,@WARMUP@' alt='science_3 warm-up curves'>
<p>On the 8-minute run, the fixed window for red science covered 2 minutes of zeros
plus a ramp — and reported 0.092/s, a number that was neither the warm-up nor the
steady state. The rolling rate makes the regimes obvious:</p>
<img src='data:image/png;base64,@ROLLING@' alt='red science rolling rate'>

<h2>2 · The right estimator: MSER-5 + batch means</h2>
<p>This is a textbook problem — <i>steady-state output analysis of a discrete-event
simulation</i>. The standard treatment: delete the initial transient with
<b>MSER-5</b> (truncate where the marginal standard error of the retained mean is
minimized — no thresholds to hand-tune), then compute a <b>batch-means confidence
interval</b> on what remains (batching defeats the autocorrelation that makes naive
OLS standard errors meaningless). Determinism makes repeated runs worthless as error
bars; the CI measures within-run stability, and <b>run-length control</b> (extend
until CI &lt; 3% of mean) replaces guessed tick budgets.</p>
<img src='data:image/png;base64,@ESTIMATORS@' alt='estimator comparison'>
<div class='finding'><b>Finding.</b> On a long-enough run all sane estimators agree
(0.150 ± tight CI) — the discipline isn't in the slope formula, it's in <i>refusing
to report before steady state is established</i> and in saying how sure you are.
MSER-5 gives both mechanically.</div>

<h2>3 · Allocation is positional, not proportional</h2>
<p>A purpose-built probe: one gear machine, its output belt tapped by two identical
transport-belt assemblers. If sharing were proportional they'd split ~50/50.</p>
<img src='data:image/png;base64,@CONTENTION@' alt='contention experiment'>
<div class='finding'><b>Finding.</b> The upstream tap runs at its own arm's full rate
(0.857/s — exactly the pure-inserter swing rate, §4); the downstream tap gets the
leftovers (0.050/s). <b>Sharing on a tapped belt is priority-by-position.</b> This
also explains science_3's equilibrium: red science hit its solo machine cap while
green idled for 8 minutes — red's taps sit upstream. Any prediction model using
proportional splits is structurally wrong on contended lanes; conversely, priority
allocation is deterministic and cheap to compute from the layout's tap order.</div>

<h2>4 · Primitive capacities: measure, don't derive</h2>
<p>Micro-benchmarks isolating one mechanism each, against the analytic formulas the
rate model used:</p>
<img src='data:image/png;base64,@PRIMITIVES@' alt='primitive calibration'>
<table>
<tr><th>primitive</th><th>analytic</th><th>measured</th><th>explanation</th></tr>
<tr><td>inserter, chest→chest</td><td>0.840/s</td><td><b>0.857/s</b></td>
<td>the game rounds each half-swing to whole ticks: 60/70 exactly</td></tr>
<tr><td>long-handed, chest→chest</td><td>1.200/s</td><td><b>1.204/s</b></td>
<td>60 ÷ 50 ticks ≈ 1.2 — quantization is kinder here</td></tr>
<tr><td>inserter, belt→chest</td><td>(no model)</td><td><b>0.938/s</b></td>
<td>belt pickup shortens the swing: 60/64 ticks — <i>faster</i> than chest pickup</td></tr>
<tr><td>loader + belt path</td><td>15.0/s</td><td><b>15.03/s</b></td>
<td>belt physics model is exact</td></tr>
</table>
<div class='finding'><b>Finding.</b> Belt-and-loader math is exact; inserter timing is
game-engine arcana (tick rounding, pickup geometry) that an analytic formula will
chronically mispredict by 2–12%. The general solution is a <b>calibration table
measured by these micro-benchmarks</b>, cached per game version — same philosophy as
the FBSR dumps: real data over reimplementation.</div>

<h2>5 · Proposed architecture</h2>
<table>
<tr><th>layer</th><th>source of truth</th><th>answers</th><th>cost</th></tr>
<tr><td><b>L1 static bounds</b></td><td>prototype dumps</td>
<td>machine caps, belt caps, Stage-B sizing (machine counts, lane counts, arms)</td>
<td>instant</td></tr>
<tr><td><b>L2 priority-flow model</b></td><td>L1 + calibrated primitives + layout tap
order</td><td>per-output steady rates &amp; bottlenecks for metadata (blueprint
tooltip, landing cards)</td><td>ms</td></tr>
<tr><td><b>L3 game-in-the-loop</b></td><td>headless Factorio + MSER-5/batch-means</td>
<td>ground truth: measured rates with CIs, warm-up times, regressions in CI</td>
<td>~2 s per game-minute</td></tr>
</table>
<p>Each layer validates the one above it: L3 measured the 17:1 contention split that
falsified L2's proportional assumption; the micro-benchmarks calibrate L2's constants;
L2's bottleneck math sizes L1's Stage-B solver. Metadata should carry its provenance
("L2 estimate" vs "L3 measured ± CI").</p>

<h2>6 · What changes concretely</h2>
<table>
<tr><th>component</th><th>today</th><th>proposed</th></tr>
<tr><td>simulate.py estimator</td><td>split-half agreement heuristic</td>
<td>MSER-5 + batch-means CI + run-length control</td></tr>
<tr><td>rates.py link caps</td><td>analytic swing formula</td>
<td>calibration table from micro-benchmarks (auto-refreshed per game version)</td></tr>
<tr><td>rates.py merge splits</td><td>proportional to caps</td>
<td>priority-by-position from the layout's tap order</td></tr>
<tr><td>rates.py operating point</td><td>uniform fair-share across outputs</td>
<td>greedy per-output equilibrium under priority allocation (matches the game)</td></tr>
<tr><td>CI / regression</td><td>none</td>
<td>micro-benchmark suite + 2–3 corpus sims asserting measured ≈ L2 ± CI</td></tr>
</table>

<p class='mut' style='margin-top:40px'>Reproduce: <code>scripts/get_factorio.sh</code>
· <code>python scripts/rate_study.py</code> · <code>python
scripts/build_rate_analysis.py</code>. Method references: MSER-5 truncation (White,
Simulation 1997); batch means (Law &amp; Kelton, <i>Simulation Modeling and
Analysis</i>); Factorio mechanics cross-checked against the wiki and FFF-416/430.</p>

</div></body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
