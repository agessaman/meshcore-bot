#!/usr/bin/env bash
# Verify critical package data exists in built wheel/sdist.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${PROJECT_ROOT}/dist"
TARGET_ASSET="modules/web_viewer/static/ico/site.webmanifest"

echo "==> Checking package data in built artifacts"
echo "    Target asset: ${TARGET_ASSET}"

python3 -m pip install --disable-pip-version-check --no-input --quiet build

rm -rf "${DIST_DIR}/_pkgcheck"
mkdir -p "${DIST_DIR}/_pkgcheck"
trap 'rm -rf "${DIST_DIR}/_pkgcheck"' EXIT

python3 -m build --sdist --wheel --outdir "${DIST_DIR}/_pkgcheck" "${PROJECT_ROOT}"

WHEEL_FILE="$(ls "${DIST_DIR}/_pkgcheck"/*.whl | head -n 1)"
SDIST_FILE="$(ls "${DIST_DIR}/_pkgcheck"/*.tar.gz | head -n 1)"

echo "==> Inspecting wheel: $(basename "${WHEEL_FILE}")"
export _PKGDATA_WHEEL="${WHEEL_FILE}"
export _PKGDATA_TARGET="${TARGET_ASSET}"
python3 <<'PY' || exit 1
import os
import zipfile

wheel_path = os.environ["_PKGDATA_WHEEL"]
target = os.environ["_PKGDATA_TARGET"]

with zipfile.ZipFile(wheel_path, "r") as zf:
    names = set(zf.namelist())
    if target not in names:
        print(f"ERROR: Missing {target} in wheel")
        raise SystemExit(1)
print("OK: wheel contains target asset")
PY
unset _PKGDATA_WHEEL _PKGDATA_TARGET

echo "==> Inspecting sdist: $(basename "${SDIST_FILE}")"
export _PKGDATA_SDIST="${SDIST_FILE}"
export _PKGDATA_TARGET="${TARGET_ASSET}"
python3 <<'PY' || exit 1
import os
import tarfile

sdist_path = os.environ["_PKGDATA_SDIST"]
target_suffix = "/" + os.environ["_PKGDATA_TARGET"]

with tarfile.open(sdist_path, "r:gz") as tf:
    names = tf.getnames()
    if not any(name.endswith(target_suffix) for name in names):
        print(f"ERROR: Missing {target_suffix[1:]} in sdist")
        raise SystemExit(1)
print("OK: sdist contains target asset")
PY
unset _PKGDATA_SDIST _PKGDATA_TARGET

echo "==> Package-data check passed"
