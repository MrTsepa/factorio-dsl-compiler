#!/usr/bin/env bash
# fgr's thin wrapper around the Factorio-FBSR CLI (com.demod.fbsr.FBSRMain).
#
# fgr does NOT bundle a renderer (rendering is optional — correctness comes from the
# verifier). This wrapper depends on an EXTERNAL Factorio-FBSR build that you provide:
#
#   1. Build FBSR + its data wrapper, and bake sprites from your Factorio install,
#      per https://github.com/demodude4u/Factorio-FBSR  (needs JDK 21+ and Maven).
#   2. Point this wrapper at that build and tell fgr to use it:
#        export FBSR_HOME=/path/to/Factorio-FBSR/FactorioBlueprintStringRenderer
#        export FGR_FBSR_SH="$PWD/scripts/fbsr.sh"
#   3. Render:  python -m fgr compile examples/basic/gears.fgr -o out/gears.png
#
# (FBSR_HOME also accepts FGR_FBSR_HOME — the same var fgr's data checks use, since both
# point at the FactorioBlueprintStringRenderer directory.)
set -euo pipefail

FBSR_HOME="${FBSR_HOME:-${FGR_FBSR_HOME:-}}"
[ -n "$FBSR_HOME" ] || { echo "fbsr.sh: set FBSR_HOME (or FGR_FBSR_HOME) to your built Factorio-FBSR (…/FactorioBlueprintStringRenderer); see this script's header." >&2; exit 1; }
[ -d "$FBSR_HOME/target" ] || { echo "fbsr.sh: $FBSR_HOME has no target/ — build FBSR first (mvn package)." >&2; exit 1; }

# Java (need a JDK 21+): use $JAVA_HOME if set, else try macOS java_home, then a brew
# openjdk@21, then fall back to `java` on PATH.
if [ -z "${JAVA_HOME:-}" ]; then
  if [ -x /usr/libexec/java_home ]; then
    JAVA_HOME="$(/usr/libexec/java_home -v 21 2>/dev/null || true)"
  fi
  if [ -z "${JAVA_HOME:-}" ] && command -v brew >/dev/null 2>&1; then
    _b="$(brew --prefix openjdk@21 2>/dev/null || true)"
    [ -n "$_b" ] && [ -d "$_b/libexec/openjdk.jdk/Contents/Home" ] && JAVA_HOME="$_b/libexec/openjdk.jdk/Contents/Home"
  fi
fi
[ -n "${JAVA_HOME:-}" ] && export PATH="$JAVA_HOME/bin:$PATH"
command -v java >/dev/null || { echo "fbsr.sh: no JDK found — install JDK 21+ or set JAVA_HOME." >&2; exit 1; }
command -v mvn  >/dev/null || { echo "fbsr.sh: maven (mvn) not found — needed once to resolve the classpath." >&2; exit 1; }

cd "$FBSR_HOME"
CPF="$FBSR_HOME/.fbsr_cp.txt"               # cache the resolved classpath after the first run
if [ ! -f "$CPF" ]; then
  mvn -q dependency:build-classpath -Dmdep.outputFile=.deps_cp
  JAR="$(ls target/FactorioBlueprintStringRenderer-*.jar | head -1)"
  CP="$JAR:$(cat .deps_cp):$FBSR_HOME/lib/*"
  # macOS/Apple-Silicon only: FBSR ships an x86-only webp codec (sejda); swap in the
  # arm64 webp-imageio fork (+ kotlin-stdlib it needs). No-op on other platforms.
  if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
    M2="${M2_REPO:-$HOME/.m2/repository}"
    WV="${WEBP_VER:-0.11.0}"; KV="${KOTLIN_VER:-2.0.21}"
    WEBP="$M2/com/github/usefulness/webp-imageio/$WV/webp-imageio-$WV.jar"
    KOTLIN="$M2/org/jetbrains/kotlin/kotlin-stdlib/$KV/kotlin-stdlib-$KV.jar"
    SEJDA="$M2/org/sejda/imageio/webp-imageio/0.1.6/webp-imageio-0.1.6.jar"
    [ -f "$WEBP" ]   || mvn -q dependency:get -Dartifact="com.github.usefulness:webp-imageio:$WV"
    [ -f "$KOTLIN" ] || mvn -q dependency:get -Dartifact="org.jetbrains.kotlin:kotlin-stdlib:$KV"
    CP="$WEBP:$JAR:$(tr ':' '\n' < .deps_cp | grep -vF "$SEJDA" | paste -sd: -):$KOTLIN:$FBSR_HOME/lib/*"
  fi
  echo "$CP" > "$CPF"
fi
exec java -cp "$(cat "$CPF")" com.demod.fbsr.FBSRMain "$@"
