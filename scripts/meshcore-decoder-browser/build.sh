#!/usr/bin/env bash
# Rebuild the web viewer's browser bundle of @michaelhart/meshcore-decoder.
#
#   scripts/meshcore-decoder-browser/build.sh [version]
#
# Fetches the published package (source included), adds index.ts from this
# directory as src/browser/index.ts, installs its runtime dependencies with
# install scripts disabled, and bundles it with esbuild into
# modules/web_viewer/static/js/meshcore-browser.mjs. Everything is built in a
# temporary directory; only the bundle is written into the repo.
set -euo pipefail

VERSION="${1:-0.3.0}"
ESBUILD_VERSION="0.24.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$REPO/modules/web_viewer/static/js/meshcore-browser.mjs"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$WORK"
npm pack "@michaelhart/meshcore-decoder@$VERSION" --silent >/dev/null
tar xzf ./*.tgz
cd package
mkdir -p src/browser
cp "$HERE/index.ts" src/browser/index.ts
npm install --omit=dev --ignore-scripts --no-audit --no-fund --silent
npx --yes "esbuild@$ESBUILD_VERSION" src/browser/index.ts \
    --bundle --format=esm --platform=browser --target=es2020 \
    --outfile="$OUT" --log-level=warning

echo "Built @michaelhart/meshcore-decoder@$VERSION -> ${OUT#"$REPO"/} ($(wc -c < "$OUT" | tr -d ' ') bytes)"
