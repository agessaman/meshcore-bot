#!/usr/bin/env bash
# Rebuild the web viewer's browser bundle of @michaelhart/meshcore-decoder.
#
#   scripts/meshcore-decoder-browser/build.sh [commit]
#
# Fetches the decoder's source from the agessaman/meshcore-decoder fork at a
# pinned commit (0.4.0 isn't published to npm), adds index.ts from this
# directory as src/browser/index.ts, installs its runtime dependencies from the
# lockfile with install scripts disabled, and bundles it with esbuild into
# modules/web_viewer/static/js/meshcore-browser.mjs. Everything is built in a
# temporary directory; only the bundle is written into the repo.
set -euo pipefail

REPO_URL="https://github.com/agessaman/meshcore-decoder.git"
COMMIT="${1:-60733c09947c5e5a10b01c0dcea7898c8acd9606}"  # Version 0.4.0
ESBUILD_VERSION="0.24.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$REPO/modules/web_viewer/static/js/meshcore-browser.mjs"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$WORK"
git init --quiet package
cd package
git fetch --quiet --depth 1 "$REPO_URL" "$COMMIT"
git -c advice.detachedHead=false checkout --quiet FETCH_HEAD
VERSION="$(node -p "require('./package.json').version")"
mkdir -p src/browser
cp "$HERE/index.ts" src/browser/index.ts
npm ci --omit=dev --ignore-scripts --no-audit --no-fund --silent
npx --yes "esbuild@$ESBUILD_VERSION" src/browser/index.ts \
    --bundle --format=esm --platform=browser --target=es2020 \
    --outfile="$OUT" --log-level=warning

echo "Built @michaelhart/meshcore-decoder@$VERSION (${COMMIT:0:8}) -> ${OUT#"$REPO"/} ($(wc -c < "$OUT" | tr -d ' ') bytes)"
