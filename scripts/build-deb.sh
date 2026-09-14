#!/usr/bin/env bash
# build-deb.sh — Build a .deb package for meshcore-bot
# Usage: ./scripts/build-deb.sh [version]
#
# Produces: dist/meshcore-bot_<version>_all.deb
# Requirements: dpkg-deb, fakeroot (sudo apt install fakeroot)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Version ──────────────────────────────────────────────────────────────────
VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
    VERSION="$(python3 -c "import tomllib; d=tomllib.load(open('${PROJECT_ROOT}/pyproject.toml','rb')); print(d['project']['version'])" 2>/dev/null || \
               python3 -c "import tomli; d=tomli.load(open('${PROJECT_ROOT}/pyproject.toml','rb')); print(d['project']['version'])" 2>/dev/null || \
               grep -Po '(?<=^version = ")([^"]+)' "${PROJECT_ROOT}/pyproject.toml" || true)"
fi
# Never fall back to a hardcoded version: silently stamping a stale number onto
# the package produces an apparent downgrade that upgrades will refuse.
if [[ -z "${VERSION}" ]]; then
    echo "ERROR: Could not read the version from pyproject.toml" >&2
    echo "       Install tomli (Python 3.10) or pass the version explicitly:" >&2
    echo "       ./scripts/build-deb.sh 1.0.0" >&2
    exit 1
fi

# Validate VERSION: must be semver-like (digits and dots only) to prevent
# shell injection via the unquoted <<EOF heredoc below.
if [[ ! "${VERSION}" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?([~+][A-Za-z0-9._-]+)?$ ]]; then
    echo "ERROR: Invalid version string: '${VERSION}'" >&2
    echo "       Must match semver format, e.g. 1.2.3 or 1.2.3~rc1" >&2
    exit 1
fi

PACKAGE_NAME="meshcore-bot"
ARCH="all"
INSTALL_ROOT="/opt/meshcore-bot"
CONF_DIR="/etc/meshcore-bot"
LOG_DIR="/var/log/meshcore-bot"
DATA_DIR="/var/lib/meshcore-bot"
SYSTEMD_DIR="/lib/systemd/system"

BUILD_DIR="${PROJECT_ROOT}/dist/deb-build/${PACKAGE_NAME}_${VERSION}_${ARCH}"
OUT_DIR="${PROJECT_ROOT}/dist"

echo "==> Building ${PACKAGE_NAME} ${VERSION} (arch=${ARCH})"
echo "    Project root : ${PROJECT_ROOT}"
echo "    Build dir    : ${BUILD_DIR}"
echo "    Output dir   : ${OUT_DIR}"
echo ""

# ── Clean & create staging tree ──────────────────────────────────────────────
rm -rf "${BUILD_DIR}"
mkdir -p \
    "${BUILD_DIR}/DEBIAN" \
    "${BUILD_DIR}${INSTALL_ROOT}" \
    "${BUILD_DIR}${CONF_DIR}" \
    "${BUILD_DIR}${LOG_DIR}" \
    "${BUILD_DIR}${DATA_DIR}" \
    "${BUILD_DIR}${SYSTEMD_DIR}" \
    "${OUT_DIR}"

# ── Copy application files ────────────────────────────────────────────────────
echo "==> Copying application files…"
rsync -a \
    --exclude='.git' \
    --exclude='.github' \
    --exclude='.cursor' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.egg-info' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='dist' \
    --exclude='local' \
    --exclude='*.db' \
    --exclude='*.log' \
    --exclude='config.ini' \
    --exclude='.env' \
    --exclude='*.key' \
    --exclude='*.pem' \
    --exclude='*.p12' \
    --exclude='*.pfx' \
    --exclude='node_modules' \
    "${PROJECT_ROOT}/" "${BUILD_DIR}${INSTALL_ROOT}/"

# ── Default config ────────────────────────────────────────────────────────────
echo "==> Installing default config…"
cp "${PROJECT_ROOT}/config.ini.example" "${BUILD_DIR}${CONF_DIR}/config.ini"
# Service installs keep mutable state and local plugins out of the root-owned
# application tree.  Only replace the documented defaults; custom package
# builders can still provide absolute paths of their own.
sed -i \
    -e "s|^db_path = meshcore_bot.db$|db_path = ${DATA_DIR}/meshcore_bot.db|" \
    -e "s|^#local_dir_path = local$|local_dir_path = ${DATA_DIR}/local|" \
    -e "s|^log_file = meshcore_bot.log$|log_file = ${LOG_DIR}/meshcore_bot.log|" \
    "${BUILD_DIR}${CONF_DIR}/config.ini"
# Symlink so the app finds it in the install root
ln -sf "${CONF_DIR}/config.ini" "${BUILD_DIR}${INSTALL_ROOT}/config.ini"

# ── systemd unit ──────────────────────────────────────────────────────────────
echo "==> Installing systemd unit…"
cat > "${BUILD_DIR}${SYSTEMD_DIR}/${PACKAGE_NAME}.service" << 'UNIT'
[Unit]
Description=MeshCore Bot - Mesh Network Bot Service
Documentation=https://github.com/agessaman/meshcore-bot
After=network.target
Wants=network.target

# Restart policy. These belong in [Unit], not [Service] — systemd 230 moved them,
# and it silently ignores them anywhere else. Give up after 3 starts in 60 s so a
# bot that cannot reach its radio lands in `failed` instead of looping forever.
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=meshcore
Group=meshcore
WorkingDirectory=/opt/meshcore-bot
ExecStart=/opt/meshcore-bot/venv/bin/python /opt/meshcore-bot/meshcore_bot.py --config /etc/meshcore-bot/config.ini
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=meshcore-bot
Environment=PYTHONPATH=/opt/meshcore-bot
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
UMask=0077
ConfigurationDirectory=meshcore-bot
ConfigurationDirectoryMode=0700
StateDirectory=meshcore-bot
StateDirectoryMode=0700
LogsDirectory=meshcore-bot
LogsDirectoryMode=0750
ReadWritePaths=/etc/meshcore-bot
ReadWritePaths=/var/log/meshcore-bot
ReadWritePaths=/var/lib/meshcore-bot
LimitNOFILE=65536
MemoryMax=1G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
UNIT

# ── DEBIAN/control ─────────────────────────────────────────────────────────────
echo "==> Writing DEBIAN/control…"
# Compute rough installed size (kB)
INSTALLED_SIZE=$(du -s "${BUILD_DIR}${INSTALL_ROOT}" | awk '{print $1}')

cat > "${BUILD_DIR}/DEBIAN/control" << EOF
Package: ${PACKAGE_NAME}
Version: ${VERSION}
Section: net
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_SIZE}
Depends: python3 (>= 3.10), python3-pip, python3-venv, adduser
Recommends: systemd
Suggests: sqlite3
Maintainer: MeshCore Bot Team <noreply@example.com>
Description: MeshCore Bot - Mesh Network Automation Bot
 A feature-rich bot for MeshCore mesh radio networks.
 Supports command handling, scheduled messages, web viewer,
 Discord/Telegram bridges, inbound webhooks, and more.
Homepage: https://github.com/agessaman/meshcore-bot
EOF

# ── DEBIAN/conffiles ──────────────────────────────────────────────────────────
echo "${CONF_DIR}/config.ini" > "${BUILD_DIR}/DEBIAN/conffiles"

# Debian runs the old prerm before this new preinst.  The helper preserves
# markers written by hardened prerm versions and recognizes the first legacy
# transition through a root-owned persistent policy marker.
cp "${PROJECT_ROOT}/scripts/debian_service_state.sh" "${BUILD_DIR}/DEBIAN/preinst"
chmod 0755 "${BUILD_DIR}/DEBIAN/preinst"

# ── DEBIAN/postinst ───────────────────────────────────────────────────────────
cat > "${BUILD_DIR}/DEBIAN/postinst" << 'POSTINST'
#!/bin/bash
set -e

INSTALL_ROOT="/opt/meshcore-bot"
CONF_DIR="/etc/meshcore-bot"
LOG_DIR="/var/log/meshcore-bot"
DATA_DIR="/var/lib/meshcore-bot"
SERVICE_USER="meshcore"

# Create service user if it doesn't exist
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    adduser --system --group --no-create-home \
        --home "${INSTALL_ROOT}" \
        --shell /usr/sbin/nologin \
        --gecos "MeshCore Bot service account" \
        "${SERVICE_USER}"
fi

# Root owns executable code and the virtualenv; the service can write only its
# explicit configuration/state/log trees.  0700/0600 also protect API keys,
# database contents, local plugin config, and backups from other local users.
chown -R root:root "${INSTALL_ROOT}"
find "${INSTALL_ROOT}" -type d -exec chmod 0755 {} +
find "${INSTALL_ROOT}" -type f -exec chmod go-w {} +
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${LOG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 \
    "${DATA_DIR}" "${DATA_DIR}/local" "${DATA_DIR}/local/commands" \
    "${DATA_DIR}/local/service_plugins" "${DATA_DIR}/backups"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${CONF_DIR}"
# Previous package versions resolved relative settings from /etc/meshcore-bot.
# The service is stopped by prerm, so SQLite backup migration is coherent.
python3 "${INSTALL_ROOT}/scripts/migrate_service_layout.py" \
    --config "${CONF_DIR}/config.ini" \
    --legacy-base "${CONF_DIR}" \
    --state-dir "${DATA_DIR}" \
    --log-dir "${LOG_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${CONF_DIR}" "${DATA_DIR}" "${LOG_DIR}"
find "${CONF_DIR}" "${DATA_DIR}" -type d -exec chmod 0700 {} +
find "${CONF_DIR}" "${DATA_DIR}" -type f -exec chmod 0600 {} +
chmod 0750 "${LOG_DIR}"
find "${LOG_DIR}" -type f -exec chmod 0600 {} +

# Rebuild the venv from trusted requirements.  Older releases made it writable
# by the service user, so an in-place upgrade could preserve executable .pth or
# module persistence.
VENV_BUILD="${INSTALL_ROOT}/.venv-build-$$"
VENV_OLD="${INSTALL_ROOT}/.venv-old-$$"
rm -rf "${VENV_BUILD}" "${VENV_OLD}"
# 32-bit ARM: piwheels + constraints-armv7.txt (issue #269).  No-op elsewhere.
# shellcheck source=/opt/meshcore-bot/scripts/armv7_pip_args.sh
. "${INSTALL_ROOT}/scripts/armv7_pip_args.sh"
configure_armv7_pip_args "${INSTALL_ROOT}/requirements.txt"
echo "Building fresh Python virtualenv…"
python3 -m venv "${VENV_BUILD}"
"${VENV_BUILD}/bin/python" -m pip install --quiet --upgrade pip
"${VENV_BUILD}/bin/python" -m pip install --quiet "${ARMV7_PIP_ARGS[@]}" -r "${INSTALL_ROOT}/requirements.txt"
if [ -d "${INSTALL_ROOT}/venv" ]; then
    mv "${INSTALL_ROOT}/venv" "${VENV_OLD}"
fi
if ! mv "${VENV_BUILD}" "${INSTALL_ROOT}/venv"; then
    [ -d "${VENV_OLD}" ] && mv "${VENV_OLD}" "${INSTALL_ROOT}/venv"
    exit 1
fi
# Shebangs still point at .venv-build-$$; rewrite for the final path (#229).
# Keep VENV_OLD until rewrite succeeds so a failure can restore the prior tree.
if ! bash "${INSTALL_ROOT}/scripts/rewrite_venv_shebangs.sh" "${INSTALL_ROOT}/venv"; then
    if [ -d "${VENV_OLD}" ]; then
        rm -rf "${INSTALL_ROOT}/venv"
        mv "${VENV_OLD}" "${INSTALL_ROOT}/venv"
    fi
    exit 1
fi
rm -rf "${VENV_OLD}"

# A legacy prerm destroys the prior state before new package code can observe
# it.  The helper uses an explicit availability-first policy for that one
# transition, then preserves enablement/activity exactly on later upgrades.
bash "${INSTALL_ROOT}/scripts/debian_service_state.sh" restore "${2:-}"

echo ""
echo "meshcore-bot installed successfully."
echo "Edit /etc/meshcore-bot/config.ini before starting the service."
POSTINST
chmod 0755 "${BUILD_DIR}/DEBIAN/postinst"

# ── DEBIAN/prerm ──────────────────────────────────────────────────────────────
cat > "${BUILD_DIR}/DEBIAN/prerm" << 'PRERM'
#!/bin/bash
set -e

bash /opt/meshcore-bot/scripts/debian_service_state.sh preremove "${1:-}"
PRERM
chmod 0755 "${BUILD_DIR}/DEBIAN/prerm"

# ── DEBIAN/postrm ─────────────────────────────────────────────────────────────
cat > "${BUILD_DIR}/DEBIAN/postrm" << 'POSTRM'
#!/bin/bash
set -e

if [ "$1" = "purge" ]; then
    rm -rf /opt/meshcore-bot/venv
    rm -rf /var/lib/meshcore-bot
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload || true
    fi
fi
POSTRM
chmod 0755 "${BUILD_DIR}/DEBIAN/postrm"

# ── Set permissions ───────────────────────────────────────────────────────────
echo "==> Setting permissions…"
find "${BUILD_DIR}" -type d -exec chmod 0755 {} \;
find "${BUILD_DIR}" -type f -exec chmod 0644 {} \;
chmod 0755 "${BUILD_DIR}/DEBIAN/preinst" "${BUILD_DIR}/DEBIAN/postinst" \
    "${BUILD_DIR}/DEBIAN/prerm" "${BUILD_DIR}/DEBIAN/postrm"
chmod 0600 "${BUILD_DIR}${CONF_DIR}/config.ini"
# Make Python entry point executable
chmod 0755 "${BUILD_DIR}${INSTALL_ROOT}/meshcore_bot.py" 2>/dev/null || true

# ── Build the .deb ────────────────────────────────────────────────────────────
DEB_FILE="${OUT_DIR}/${PACKAGE_NAME}_${VERSION}_${ARCH}.deb"
echo "==> Building .deb: ${DEB_FILE}"
fakeroot dpkg-deb --build "${BUILD_DIR}" "${DEB_FILE}"

echo ""
echo "Done! Package: ${DEB_FILE}"
echo ""
echo "Install with:  sudo dpkg -i ${DEB_FILE}"
echo "               sudo apt-get install -f  # fix dependencies if needed"
