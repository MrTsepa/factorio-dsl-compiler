# Rendering setup (optional)

`fgr`'s correctness comes from the **verifier**, not from pictures — so rendering is
entirely optional. The [gallery](../README.md#gallery) images and the
[report](https://mrtsepa.github.io/factorio-dsl-compiler/report.html) in this repo are
already rendered, so you can browse everything without installing anything.

You only need this if you want to render **your own** layouts to game-accurate PNGs (or
regenerate the gallery/report).

## Why you have to bring your own renderer

Game-accurate rendering uses [Factorio-FBSR](https://github.com/demodude4u/Factorio-FBSR),
which draws with Factorio's **real sprites and `data.raw`**. Those are Wube's proprietary
assets — they can't be redistributed, so FBSR *bakes* them from **your own licensed
Factorio install** at setup time. That's why nothing here can ship a working renderer; the
package just shells out to one via [`scripts/fbsr.sh`](../scripts/fbsr.sh).

## Prerequisites

- **JDK 21+** and **Maven** (`mvn`). On macOS: `brew install openjdk@21 maven`.
- A local **Factorio** install (any 2.0.x) — for the sprite/data bake.

## 1. Build FBSR

FBSR needs three of its author's repos built into your local Maven cache (`~/.m2`).
Follow the upstream README, but in short:

```bash
for r in Java-Factorio-Data-Wrapper Discord-Core-Bot-Apple Factorio-FBSR; do
  git clone https://github.com/demodude4u/$r
done
( cd Java-Factorio-Data-Wrapper && mvn -q install )
( cd Discord-Core-Bot-Apple    && mvn -q install )
( cd Factorio-FBSR/FactorioBlueprintStringRenderer && mvn -q package )
```

The build dir you'll point `fgr` at is `Factorio-FBSR/FactorioBlueprintStringRenderer`.

## 2. Bake Factorio data + sprites (one-time)

Point the wrapper at that build, then have FBSR bake from your Factorio install:

```bash
export FBSR_HOME="$PWD/Factorio-FBSR/FactorioBlueprintStringRenderer"
export FGR_FBSR_SH="/path/to/factorio-dsl-compiler/scripts/fbsr.sh"

# tell FBSR where Factorio is, then bake the vanilla data + sprite atlas
"$FGR_FBSR_SH" cfg-factorio -install=/path/to/Factorio.app/Contents -auto-find-exec
"$FGR_FBSR_SH" profile-default-vanilla -f
"$FGR_FBSR_SH" build -a
```

(Adjust the `-install` path for your OS; on Linux/Windows it points at the Factorio
program directory. See the FBSR README for exact flags — they can change between versions.)

## 3. Render

```bash
cd /path/to/factorio-dsl-compiler
export FBSR_HOME=/path/to/Factorio-FBSR/FactorioBlueprintStringRenderer
.venv/bin/python -m fgr compile examples/basic/circuits.fgr -o out/circuits.png
.venv/bin/python scripts/gen_gallery.py     # regenerate README gallery images
.venv/bin/python scripts/build_report.py    # regenerate out/report.html
```

## Environment variables

| var | purpose | default |
|-----|---------|---------|
| `FBSR_HOME` | your built FBSR (`…/FactorioBlueprintStringRenderer`) | — |
| `FGR_FBSR_HOME` | FBSR build for the model/recipe data checks (`validate-model`, `audit_specs`) | `~/Workspace/Factorio-FBSR/FactorioBlueprintStringRenderer` |
| `FGR_FBSR_SH` | override the render wrapper | this repo's `scripts/fbsr.sh` |

The wrapper auto-detects a JDK (`JAVA_HOME` → macOS `java_home` → Homebrew `openjdk@21`)
and, on Apple Silicon, swaps in the arm64 `webp-imageio` codec. If FBSR isn't set up,
rendering and the model/recipe checks **skip cleanly** — the rest of `fgr` (compile,
verify, tests) needs none of this.
