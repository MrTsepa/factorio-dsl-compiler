#!/usr/bin/env bash
# Install step for the simulation harness (docs/RATES.md, Stage D): download the
# FREE Factorio headless server build (no account needed) into out/_factorio_sim/.
#
#   scripts/get_factorio.sh              # latest stable
#   scripts/get_factorio.sh 2.0.76       # pin a version
#
# The binary is linux-x86_64: on Linux it runs directly; on macOS scripts/simulate.py
# runs it through docker (OrbStack/Docker Desktop, Rosetta handles amd64).
set -euo pipefail

VERSION="${1:-stable}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/out/_factorio_sim"
URL="https://factorio.com/get-download/$VERSION/headless/linux64"

mkdir -p "$DEST"
if [ -x "$DEST/factorio/bin/x64/factorio" ]; then
  echo "already installed: $DEST/factorio ($(cat "$DEST/factorio/data/base/info.json" 2>/dev/null | grep -o '"version": *"[^"]*"' | head -1))"
  exit 0
fi

echo "downloading factorio headless ($VERSION) ..."
curl -fL --progress-bar "$URL" -o "$DEST/factorio-headless.tar.xz"
echo "extracting ..."
tar -xJf "$DEST/factorio-headless.tar.xz" -C "$DEST"
rm "$DEST/factorio-headless.tar.xz"
echo "installed: $DEST/factorio"
"$DEST/factorio/bin/x64/factorio" --version 2>/dev/null || true   # works on Linux only
