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
               grep -Po '(?<=^version = ")([^"]+)' "${PROJECT_ROOT}/pyproject.toml" || echo "0.9.0")"
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
    --exclude='node_modules' \
    "${PROJECT_ROOT}/" "${BUILD_DIR}${INSTALL_ROOT}/"

# ── Default config ────────────────────────────────────────────────────────────
echo "==> Installing default config…"
cp "${PROJECT_ROOT}/config.ini.example" "${BUILD_DIR}${CONF_DIR}/config.ini"
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
ReadWritePaths=/opt/meshcore-bot
ReadWritePaths=/etc/meshcore-bot
ReadWritePaths=/var/log/meshcore-bot
ReadWritePaths=/var/lib/meshcore-bot
LimitNOFILE=65536
MemoryMax=512M
StartLimitInterval=60
StartLimitBurst=3

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
Depends: python3 (>= 3.9), python3-pip, python3-venv, adduser
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

# Create directories
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0755 "${LOG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0755 "${DATA_DIR}"
install -d -o root -g root -m 0755 "${CONF_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_ROOT}"

# Create virtualenv and install dependencies
if [ ! -d "${INSTALL_ROOT}/venv" ]; then
    echo "Creating Python virtualenv…"
    python3 -m venv "${INSTALL_ROOT}/venv"
fi
echo "Installing Python dependencies…"
"${INSTALL_ROOT}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_ROOT}/venv/bin/pip" install --quiet -r "${INSTALL_ROOT}/requirements.txt"

# Enable and start systemd service
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable meshcore-bot.service || true
    echo "Service enabled. Start with: sudo systemctl start meshcore-bot"
fi

echo ""
echo "meshcore-bot installed successfully."
echo "Edit /etc/meshcore-bot/config.ini before starting the service."
POSTINST
chmod 0755 "${BUILD_DIR}/DEBIAN/postinst"

# ── DEBIAN/prerm ──────────────────────────────────────────────────────────────
cat > "${BUILD_DIR}/DEBIAN/prerm" << 'PRERM'
#!/bin/bash
set -e

if command -v systemctl >/dev/null 2>&1; then
    systemctl stop meshcore-bot.service 2>/dev/null || true
    systemctl disable meshcore-bot.service 2>/dev/null || true
fi
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
chmod 0755 "${BUILD_DIR}/DEBIAN/postinst" "${BUILD_DIR}/DEBIAN/prerm" "${BUILD_DIR}/DEBIAN/postrm"
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
