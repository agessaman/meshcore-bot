#!/bin/bash
# MeshCore Bot Service Installation Script
# This script installs the MeshCore Bot as a system service
# Supports both Linux (systemd) and macOS (launchd)
#
# This script will:
#   1. Create a dedicated system user for the bot (Linux only)
#   2. Copy bot files to installation directory
#   3. Set up proper file permissions
#   4. Install and enable the service (systemd or launchd)
#   5. Create a Python virtual environment with dependencies
#
# Usage:
#   ./install-service.sh          # Normal installation (non-destructive if already installed)
#   ./install-service.sh --upgrade # Upgrade mode (copies new files, updates dependencies)
#   ./install-service.sh -u        # Short form of --upgrade
#   ./install-service.sh -u --install-extras # upgrade + install optional packages (profanity, geocoding)
#   ./install-service.sh --update-venv           # Only refresh the venv, in place
#   ./install-service.sh -u --update-venv        # Upgrade code, reuse the venv
#
# Prerequisites:
#   - Linux system with systemd OR macOS
#   - Python 3.10+ installed
#   - sudo access (script will prompt if needed)
#   - Run from the meshcore-bot directory

set -e
umask 077

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Detect operating system
OS="$(uname -s)"
IS_MACOS=false

if [[ "$OS" == "Darwin" ]]; then
    IS_MACOS=true
elif [[ "$OS" == "Linux" ]]; then
    : # Linux detected; service paths configured below
else
    echo "Error: Unsupported operating system: $OS"
    echo "This script supports Linux (systemd) and macOS (launchd)"
    exit 1
fi

# Configuration - OS-specific paths
SERVICE_NAME="meshcore-bot"
PLIST_NAME="com.meshcore.bot"

if [[ "$IS_MACOS" == true ]]; then
    SERVICE_USER="$(whoami)"  # macOS: use current user or _meshcore
    SERVICE_GROUP="staff"
    INSTALL_DIR="/usr/local/meshcore-bot"
    CONF_DIR="/usr/local/etc/meshcore-bot"
    STATE_DIR="/usr/local/var/lib/meshcore-bot"
    LOG_DIR="/usr/local/var/log/meshcore-bot"
    SERVICE_FILE="com.meshcore.bot.plist"
    LAUNCHD_DIR="/Library/LaunchDaemons"
else
    SERVICE_USER="meshcore"
    SERVICE_GROUP="meshcore"
    INSTALL_DIR="/opt/meshcore-bot"
    CONF_DIR="/etc/meshcore-bot"
    STATE_DIR="/var/lib/meshcore-bot"
    LOG_DIR="/var/log/meshcore-bot"
    SERVICE_FILE="meshcore-bot.service"
    SYSTEMD_DIR="/etc/systemd/system"
fi

CONFIG_FILE="$CONF_DIR/config.ini"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse command line arguments (before sudo check so help works)
UPGRADE_MODE=false
UPDATE_VENV=false
INSTALL_EXTRAS=false
for arg in "$@"; do
    case $arg in
        --upgrade|-u)
            UPGRADE_MODE=true
            ;;
        --update-venv)
            UPDATE_VENV=true
            ;;
        --install-extras|-ie)
            INSTALL_EXTRAS=true
            ;;
        --help|-h)
            echo "MeshCore Bot Service Installation Script"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --upgrade, -u    Upgrade mode: update files and dependencies"
            echo "  --update-venv    Update dependencies inside the existing virtualenv"
            echo "                   instead of rebuilding it from scratch. On its own"
            echo "                   nothing else is touched: no file sync, no service"
            echo "                   file changes. Combine with --upgrade to sync code"
            echo "                   and still skip the slow rebuild."
            echo "  --help, -h       Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                     # Normal installation (non-destructive if already installed)"
            echo "  $0 --upgrade           # Upgrade existing installation (rebuilds the venv)"
            echo "  $0 -u                  # Short form of --upgrade"
            echo "  $0 -u --install-extras # Upgrade and install optional packages without prompting"
            echo "  $0 --update-venv       # Refresh dependencies only, keeping the venv"
            echo "  $0 -u --update-venv    # Upgrade code and refresh the venv in place"
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $arg" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

# --update-venv on its own is a virtualenv-only run: skip the service user,
# directory, file-sync and service-file steps entirely.  Combined with --upgrade
# it stays a normal upgrade that reuses the virtualenv instead of rebuilding it.
VENV_ONLY_MODE=false
if [[ "$UPDATE_VENV" == true && "$UPGRADE_MODE" != true ]]; then
    VENV_ONLY_MODE=true
fi

# Function to print section headers
print_section() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# Function to print info messages
print_info() {
    echo -e "${CYAN}ℹ${NC}  $1"
}

# Function to print success messages
print_success() {
    echo -e "${GREEN}✓${NC}  $1"
}

# Function to print warning messages
print_warning() {
    echo -e "${YELLOW}⚠${NC}  $1"
}

# Function to print error messages
print_error() {
    echo -e "${RED}✗${NC}  $1"
}

# Function to ask yes/no question
ask_yes_no() {
    local prompt="$1"
    local default="${2:-n}"
    local response

    if [[ "$default" == "y" ]]; then
        prompt="${prompt} [Y/n]: "
    else
        prompt="${prompt} [y/N]: "
    fi

    while true; do
        read -p "$(echo -e "${YELLOW}${prompt}${NC}")" response
        response="${response:-$default}"
        case "$response" in
            [Yy]|[Yy][Ee][Ss]) return 0 ;;
            [Nn]|[Nn][Oo])     return 1 ;;
            *) echo "Please answer yes or no." ;;
        esac
    done
}

if [[ "$VENV_ONLY_MODE" == true ]]; then
    print_section "MeshCore Bot Virtual Environment Update"
    print_info "Running in VENV-ONLY mode - only Python dependencies will change"
elif [[ "$UPGRADE_MODE" == true ]]; then
    print_section "MeshCore Bot Service Upgrader"
    print_info "Running in UPGRADE mode - will update files and dependencies"
    if [[ "$UPDATE_VENV" == true ]]; then
        print_info "Reusing the existing virtualenv instead of rebuilding it"
    fi
else
    print_section "MeshCore Bot Service Installer"
fi
echo ""
if [[ "$IS_MACOS" == true ]]; then
    print_info "Detected macOS - will install as launchd service"
    print_info "The bot will start automatically on boot using launchd"
else
    print_info "Detected Linux - will install as systemd service"
    print_info "The bot will run as a dedicated user and start automatically on boot"
fi
echo ""

# Check if script has execute permissions
if [ ! -x "$0" ]; then
    print_warning "Script does not have execute permissions. Attempting to set them..."
    chmod +x "$0" 2>/dev/null || {
        print_error "Could not set execute permissions. Please run: chmod +x install-service.sh"
        exit 1
    }
    print_success "Execute permissions set"
fi

# Capture original user before sudo (for macOS)
ORIGINAL_USER="${SUDO_USER:-$USER}"

# Check if running as root, if not re-execute with sudo
if [[ $EUID -ne 0 ]]; then
    print_warning "This script requires root privileges to install system services"
    print_info "Re-executing with sudo..."
    echo ""
    exec sudo "$0" "$@"
fi

# Verify we're in the right directory
if [ ! -f "meshcore_bot.py" ]; then
    print_error "This script must be run from the meshcore-bot directory"
    print_error "Expected file not found: meshcore_bot.py"
    print_info "Please cd to the meshcore-bot directory and run this script again"
    exit 1
fi

# Check for service file
if [ ! -f "$SERVICE_FILE" ]; then
    print_error "Service file not found: $SERVICE_FILE"
    if [[ "$IS_MACOS" == true ]]; then
        print_info "Expected: com.meshcore.bot.plist"
    else
        print_info "Expected: meshcore-bot.service"
    fi
    exit 1
fi

# OS-specific service manager checks
if [[ "$IS_MACOS" == true ]]; then
    if ! command -v launchctl &> /dev/null; then
        print_error "launchctl is not available on this system"
        print_error "This script requires macOS with launchd"
        exit 1
    fi
else
    if ! command -v systemctl &> /dev/null; then
        print_error "systemd is not available on this system"
        print_error "This script requires a Linux system with systemd"
        exit 1
    fi
fi

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 is not installed or not in PATH"
    print_error "Please install Python 3.10 or higher before running this script"
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    print_error "Python 3.10 or higher is required"
    print_error "Found: $(python3 --version 2>&1)"
    exit 1
fi
if ! command -v rsync &> /dev/null; then
    print_error "rsync is required for a source-authoritative secure install"
    print_error "Install rsync before stopping or upgrading the service"
    exit 1
fi

# Stop a running legacy service before changing code or taking the SQLite
# backup used for layout migration.  This runs only after sudo re-execution and
# service-manager validation, so --help and unprivileged discovery stay inert.
SERVICE_RESTART_PENDING=false
SERVICE_RESTART_SAFE=true

restart_previously_active_service() {
    if [[ "$SERVICE_RESTART_PENDING" != true ]]; then
        return 0
    fi

    if [[ "$IS_MACOS" == true ]]; then
        if ! launchctl list "$PLIST_NAME" &>/dev/null; then
            if ! launchctl load "$LAUNCHD_DIR/$SERVICE_FILE" 2>/dev/null; then
                return 1
            fi
        fi
    elif ! systemctl start "$SERVICE_NAME"; then
        return 1
    fi

    SERVICE_RESTART_PENDING=false
    return 0
}

restore_active_service_on_failure() {
    local status=$?
    trap - EXIT
    if [[ "$status" -ne 0 && "$SERVICE_RESTART_PENDING" == true && "$SERVICE_RESTART_SAFE" == true ]]; then
        print_warning "Upgrade failed; attempting to restore the previously active service"
        if restart_previously_active_service; then
            print_warning "Previously active service was restarted after the failed upgrade"
        else
            print_error "Could not restart the previously active service; manual recovery is required"
        fi
    elif [[ "$status" -ne 0 && "$SERVICE_RESTART_PENDING" == true ]]; then
        print_error "Upgrade failed and the executable rollback was incomplete"
        print_error "Refusing to restart a potentially partial code tree; manual recovery is required"
    fi
    exit "$status"
}

trap restore_active_service_on_failure EXIT

# Normalize virtualenv permissions after any pip activity.  pip inherits this
# script's `umask 077`, so freshly written files (pyvenv.cfg, *.pth, compiled
# *.so, dist-info metadata) land root-only and the service account cannot import
# them -- Python aborts with "init_import_site: Failed to import the site
# module".  Add read for all plus execute/traverse only where an execute bit
# already exists.  No write bit is ever granted, so the service account still
# cannot modify dependency code.
harden_venv_permissions() {
    local venv="$1"
    chmod -R go-w "$venv" 2>/dev/null || true
    chmod -R a+rX "$venv" 2>/dev/null || true
}

# Echo a human-readable reason when an existing virtualenv must not be updated
# in place, or nothing when reuse is safe.  The rebuild path exists because a
# service-writable venv can hide a malicious .pth or module that hardening would
# then bless as root-owned executable code, so reuse demands a tree that is
# root-owned and free of group/other write bits.
venv_reuse_blocker() {
    local venv="$1"

    if [ ! -d "$venv" ]; then
        echo "no virtualenv exists at $venv"
        return
    fi
    if [ ! -x "$venv/bin/python" ]; then
        echo "$venv has no usable interpreter at bin/python"
        return
    fi

    local offender
    offender="$(find "$venv" ! -user root -print 2>/dev/null | head -1)"
    if [ -n "$offender" ]; then
        echo "not root-owned throughout (e.g. $offender)"
        return
    fi

    offender="$(find "$venv" \( -perm -g+w -o -perm -o+w \) -print 2>/dev/null | head -1)"
    if [ -n "$offender" ]; then
        echo "group/other-writable paths present (e.g. $offender)"
        return
    fi

    # A system Python upgrade leaves the venv pointing at a replaced interpreter
    # or a stale site-packages version directory; in-place pip cannot repair it.
    local venv_ver sys_ver
    venv_ver="$("$venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    sys_ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if [ -z "$venv_ver" ]; then
        echo "the interpreter at $venv/bin/python does not run"
        return
    fi
    if [ -n "$sys_ver" ] && [ "$venv_ver" != "$sys_ver" ]; then
        echo "virtualenv Python $venv_ver does not match system python3 $sys_ver"
        return
    fi
}

# Extra pip arguments for 32-bit ARM hosts (Raspberry Pi 2/3 on a 32-bit OS).
# Shared with the .deb postinst via scripts/armv7_pip_args.sh so the two install
# paths cannot drift (issue #269).  See that helper and constraints-armv7.txt.
_ARMV7_PIP_HELPER="$SCRIPT_DIR/scripts/armv7_pip_args.sh"
if [ ! -f "$_ARMV7_PIP_HELPER" ]; then
    print_error "Missing $_ARMV7_PIP_HELPER"
    exit 1
fi
# shellcheck source=scripts/armv7_pip_args.sh
source "$_ARMV7_PIP_HELPER"

# Bring an existing virtualenv up to date with requirements.txt.  Plain
# `pip install -r` (no --upgrade) is deliberate: it installs what is missing and
# upgrades only what no longer satisfies a specifier, which is the fast path.
# `--upgrade` would eagerly churn transitive dependencies on every run.  Extras
# installed previously (profanity filter, geocoding) survive, unlike a rebuild.
# Also rewrites console-script shebangs so an earlier .venv-build-$$ relocate
# does not leave venv/bin/pip broken after --update-venv (issue #229).
update_venv_in_place() {
    local venv="$1"
    local requirements="$2"
    local rewrite_helper

    print_info "Reusing the existing virtualenv at $venv"
    print_info "Synchronizing dependencies from $requirements"
    configure_armv7_pip_args "$requirements"
    if ! "$venv/bin/python" -m pip install --quiet "${ARMV7_PIP_ARGS[@]}" -r "$requirements"; then
        print_error "Failed to update Python dependencies"
        print_info "Check your internet connection, or rebuild with: $0 --upgrade"
        return 1
    fi

    # Prefer the helper shipped with this installer invocation so --update-venv
    # heals broken shebangs even before a full code sync lands under INSTALL_DIR.
    if [ -f "$SCRIPT_DIR/scripts/rewrite_venv_shebangs.sh" ]; then
        rewrite_helper="$SCRIPT_DIR/scripts/rewrite_venv_shebangs.sh"
    else
        rewrite_helper="$INSTALL_DIR/scripts/rewrite_venv_shebangs.sh"
    fi
    if ! bash "$rewrite_helper" "$venv"; then
        print_error "Failed to rewrite virtualenv console-script shebangs"
        print_info "Rebuild with: $0 --upgrade"
        return 1
    fi

    harden_venv_permissions "$venv"
    print_success "Dependencies are up to date in the existing virtualenv"
}

if [[ "$IS_MACOS" == true ]]; then
    if launchctl list "$PLIST_NAME" &>/dev/null; then
        if ! launchctl unload "$LAUNCHD_DIR/$SERVICE_FILE" 2>/dev/null; then
            launchctl stop "$PLIST_NAME" 2>/dev/null || true
        fi
        if launchctl list "$PLIST_NAME" &>/dev/null; then
            print_error "The launchd service is still running; refusing an unsafe live database migration"
            exit 1
        fi
        SERVICE_RESTART_PENDING=true
    fi
elif systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
    SERVICE_RESTART_PENDING=true
fi

# ---------------------------------------------------------------------------
# Virtualenv-only fast path.  Everything above still applies (privilege check,
# service-manager and Python validation, stopping a running service); nothing
# below does, so the run ends here rather than threading conditionals through
# every remaining step.
# ---------------------------------------------------------------------------
if [[ "$VENV_ONLY_MODE" == true ]]; then
    print_section "Updating Python Virtual Environment"

    if [ ! -d "$INSTALL_DIR" ]; then
        print_error "No installation found at $INSTALL_DIR"
        print_info "Run a full install first: sudo $0"
        exit 1
    fi

    REQUIREMENTS_SRC="$SCRIPT_DIR/requirements.txt"
    if [ ! -f "$REQUIREMENTS_SRC" ]; then
        print_error "requirements.txt not found at $REQUIREMENTS_SRC"
        exit 1
    fi

    VENV_BLOCKER="$(venv_reuse_blocker "$INSTALL_DIR/venv")"
    if [ -n "$VENV_BLOCKER" ]; then
        print_error "Cannot update the virtualenv in place: $VENV_BLOCKER"
        print_info "Rebuild it with: sudo $0 --upgrade"
        exit 1
    fi

    update_venv_in_place "$INSTALL_DIR/venv" "$REQUIREMENTS_SRC" || exit 1

    if [[ "$SERVICE_RESTART_PENDING" == true ]]; then
        print_info "Restarting the service because it was running before the update"
        if ! restart_previously_active_service; then
            print_error "Failed to restart the service"
            exit 1
        fi
        print_success "Service restarted"
    elif [[ "$IS_MACOS" == true ]]; then
        print_info "Service was not running; start it with: sudo launchctl load $LAUNCHD_DIR/$SERVICE_FILE"
    else
        print_info "Service was not running; start it with: sudo systemctl start $SERVICE_NAME"
    fi

    print_section "Virtual Environment Update Complete"
    print_info "Code in $INSTALL_DIR, configuration and the service file were left untouched"
    print_info "Dependencies came from $REQUIREMENTS_SRC"
    exit 0
fi

print_section "Step 1: Setting Up Service User"
if [[ "$IS_MACOS" == true ]]; then
    # Use original user if available, otherwise root
    if [[ -n "$ORIGINAL_USER" && "$ORIGINAL_USER" != "root" ]]; then
        SERVICE_USER="$ORIGINAL_USER"
        SERVICE_GROUP="$(id -gn "$ORIGINAL_USER" 2>/dev/null || echo "staff")"
    else
        SERVICE_USER="root"
        SERVICE_GROUP="wheel"
    fi
    print_info "macOS: Service will run as user '$SERVICE_USER'"
    print_info "On macOS, launchd services run as the specified user"
    print_success "Using user: $SERVICE_USER"
else
    print_info "Creating a dedicated system user '$SERVICE_USER' for security"
    print_info "This user will run the bot service with minimal privileges"
    # Create service user and group
    if ! id "$SERVICE_USER" &>/dev/null; then
        useradd --system --no-create-home --shell /bin/false "$SERVICE_USER"
        print_success "Created system user: $SERVICE_USER"
    else
        print_warning "User $SERVICE_USER already exists (skipping creation)"
    fi

    # Add user to dialout group for serial port access (Linux)
    print_info "Configuring serial port access permissions"
    if getent group dialout > /dev/null 2>&1; then
        if groups "$SERVICE_USER" | grep -q "\bdialout\b"; then
            print_warning "User $SERVICE_USER is already in dialout group"
        else
            usermod -a -G dialout "$SERVICE_USER"
            print_success "Added $SERVICE_USER to dialout group for serial port access"
        fi
    else
        print_warning "dialout group not found - serial port access may require manual configuration"
        print_info "If using serial connection, you may need to: sudo usermod -a -G dialout $SERVICE_USER"
    fi

    # Also check for other common serial port groups (tty, uucp, lock)
    for group in tty uucp lock; do
        if getent group "$group" > /dev/null 2>&1; then
            if ! groups "$SERVICE_USER" | grep -q "\b$group\b"; then
                usermod -a -G "$group" "$SERVICE_USER" 2>/dev/null && print_info "Added $SERVICE_USER to $group group" || true
            fi
        fi
    done
fi

print_section "Step 2: Creating Installation Directories"
print_info "Creating directory structure for bot installation"
# Create installation directory
if [ -d "$INSTALL_DIR" ]; then
    if [[ "$UPGRADE_MODE" == true ]]; then
        print_info "Installation directory $INSTALL_DIR already exists"
        print_info "Upgrade mode: will update files while preserving configuration"
    else
        print_warning "Installation directory $INSTALL_DIR already exists"
        print_info "Non-destructive mode: will update files without removing existing installation"
        print_info "Use --upgrade flag for explicit upgrade mode"
    fi
else
    mkdir -p "$INSTALL_DIR"
    print_success "Created installation directory: $INSTALL_DIR"
fi

# Create mutable runtime directories separately from executable code.
mkdir -p "$CONF_DIR" "$STATE_DIR" "$LOG_DIR"
print_success "Created configuration directory: $CONF_DIR"
print_success "Created state directory: $STATE_DIR"
print_success "Created log directory: $LOG_DIR"

print_section "Step 3: Copying Bot Files"
if [[ "$UPGRADE_MODE" == true ]]; then
    print_info "Upgrading files in $INSTALL_DIR"
    print_info "Replacing executable files from the trusted source while preserving explicit runtime state"
else
    print_info "Copying bot files to $INSTALL_DIR"
    print_info "Executable files will exactly match the trusted source"
fi

# Synchronize executable code authoritatively.  Using --update or a merge-only
# fallback can preserve a newer/stale Python file written by a previously
# compromised service account and then cement it as root-owned executable code.
# Only the explicitly excluded runtime paths survive an upgrade.
sync_executable_tree() {
    local source_dir="$1"
    local dest_dir="$2"

    rsync -a --delete --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.pyo' \
        --exclude='.DS_Store' \
        --exclude='venv' \
        --exclude='*.db' \
        --exclude='*.db-shm' \
        --exclude='*.db-wal' \
        --exclude='*.log' \
        --exclude='backups' \
        --exclude='local/' \
        --exclude='config.ini' \
        "$source_dir/" "$dest_dir/"
}

copy_files_smart() {
    local source_dir="$1"
    local dest_dir="$2"
    local alternatives_backup
    local rollback_backup

    alternatives_backup="$(mktemp -d "${TMPDIR:-/tmp}/meshcore-alternatives.XXXXXX")"
    rollback_backup="$(mktemp -d "${TMPDIR:-/tmp}/meshcore-executable-rollback.XXXXXX")"
    if ! python3 "$SCRIPT_DIR/scripts/preserve_service_alternatives.py" backup \
          --source "$source_dir/modules/commands/alternatives" \
          --installed "$dest_dir/modules/commands/alternatives" \
          --backup "$alternatives_backup"; then
        print_error "Failed to preserve installed-only alternative commands"
        print_error "Partial backup retained at $alternatives_backup"
        return 1
    fi

    print_info "Creating a rollback snapshot of the installed executable tree"
    if ! sync_executable_tree "$dest_dir" "$rollback_backup"; then
        print_error "Failed to create the executable rollback snapshot"
        print_error "Incomplete snapshot retained at $rollback_backup"
        return 1
    fi

    print_info "Using rsync for source-authoritative executable synchronization"
    SERVICE_RESTART_SAFE=false
    if ! sync_executable_tree "$source_dir" "$dest_dir"; then
        print_warning "rsync failed; restoring the previous executable tree"
        if sync_executable_tree "$rollback_backup" "$dest_dir"; then
            SERVICE_RESTART_SAFE=true
            rm -rf -- "$rollback_backup"
            if ! python3 "$SCRIPT_DIR/scripts/preserve_service_alternatives.py" restore \
                  --installed "$dest_dir/modules/commands/alternatives" \
                  --backup "$alternatives_backup"; then
                print_error "Alternative-command backup retained at $alternatives_backup"
            fi
            print_warning "Previous executable tree restored after synchronization failure"
        else
            print_error "Executable rollback failed; snapshot retained at $rollback_backup"
        fi
        print_error "rsync failed; refusing to continue the upgrade"
        return 1
    fi
    if ! python3 "$SCRIPT_DIR/scripts/preserve_service_alternatives.py" restore \
          --installed "$dest_dir/modules/commands/alternatives" \
          --backup "$alternatives_backup"; then
        print_error "Failed to restore installed-only alternative commands"
        print_error "Backup retained at $alternatives_backup"
        print_warning "Restoring the previous executable tree"
        if sync_executable_tree "$rollback_backup" "$dest_dir"; then
            SERVICE_RESTART_SAFE=true
            rm -rf -- "$rollback_backup"
            print_warning "Previous executable tree restored after alternative-command failure"
        else
            print_error "Executable rollback failed; snapshot retained at $rollback_backup"
        fi
        return 1
    fi

    SERVICE_RESTART_SAFE=true
    rm -rf -- "$rollback_backup"
    print_success "Executable files synchronized authoritatively using rsync"
}

# Copy files using smart copy function
copy_files_smart "$SCRIPT_DIR" "$INSTALL_DIR" || {
    print_error "Failed to copy files. Check permissions and disk space"
    exit 1
}

# Write .version_info at install dir so web viewer and packet_capture show version after install
if command -v git &>/dev/null && [ -d "$SCRIPT_DIR/.git" ]; then
    GIT_HASH="$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
    # --tags matches lightweight tags too; must stay in step with the runtime
    # lookup in modules/version_info.py or the two disagree about one commit.
    if VERSION="$(git -C "$SCRIPT_DIR" describe --tags --exact-match HEAD 2>/dev/null)"; then
        INSTALLER_VER="$VERSION"
    else
        INSTALLER_VER="dev-${GIT_HASH}"
    fi
    printf '%s\n' "{\"installer_version\": \"${INSTALLER_VER}\", \"git_hash\": \"${GIT_HASH}\"}" > "$INSTALL_DIR/.version_info"
    print_success "Wrote version info (${INSTALLER_VER}) to $INSTALL_DIR/.version_info"
fi

# Keep configuration out of the root-owned application tree.  On upgrade, copy
# the legacy config once so existing credentials and settings are preserved.
if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$INSTALL_DIR/config.ini" ] && [ ! -L "$INSTALL_DIR/config.ini" ]; then
        cp -p "$INSTALL_DIR/config.ini" "$CONFIG_FILE"
        print_success "Migrated existing configuration to $CONFIG_FILE"
    elif [ -f "$INSTALL_DIR/config.ini.example" ]; then
        cp "$INSTALL_DIR/config.ini.example" "$CONFIG_FILE"
        print_success "Created $CONFIG_FILE from config.ini.example"
    elif [ -f "$SCRIPT_DIR/config.ini.example" ]; then
        cp "$SCRIPT_DIR/config.ini.example" "$CONFIG_FILE"
        print_success "Created $CONFIG_FILE from config.ini.example"
    else
        print_warning "config.ini.example not found. Create $CONFIG_FILE manually before starting the bot."
    fi
fi

# Rewrite all relative runtime paths and coherently migrate an existing SQLite
# database.  Absolute custom paths are left untouched; operators can grant an
# additional systemd path explicitly when they intentionally store state there.
if [ -f "$CONFIG_FILE" ]; then
    python3 "$INSTALL_DIR/scripts/migrate_service_layout.py" \
        --config "$CONFIG_FILE" \
        --legacy-base "$INSTALL_DIR" \
        --state-dir "$STATE_DIR" \
        --log-dir "$LOG_DIR"
fi

# Remove a source-tree config copied into the application tree only when it is
# identical to the active service config; otherwise retain it root-only as a
# migration backup.
if [ -f "$INSTALL_DIR/config.ini" ] && [ ! -L "$INSTALL_DIR/config.ini" ]; then
    chmod 0600 "$INSTALL_DIR/config.ini"
fi

if [ ! -f "$CONFIG_FILE" ]; then
    # Retain the old diagnostic wording for automation which looks for it.
    if [ -f "$INSTALL_DIR/config.ini.example" ]; then
        print_warning "Failed to create $CONFIG_FILE; check directory permissions"
    fi
fi

print_section "Step 4: Setting Up Python Virtual Environment"

# --update-venv skips the rebuild when the existing environment is safe to reuse.
# If it is not, say why and fall back to a full rebuild rather than failing the
# whole upgrade.
VENV_UPDATED_IN_PLACE=false
if [[ "$UPDATE_VENV" == true ]]; then
    VENV_BLOCKER="$(venv_reuse_blocker "$INSTALL_DIR/venv")"
    if [ -n "$VENV_BLOCKER" ]; then
        print_warning "Cannot reuse the existing virtualenv: $VENV_BLOCKER"
        print_info "Falling back to a full rebuild"
    elif update_venv_in_place "$INSTALL_DIR/venv" "$INSTALL_DIR/requirements.txt"; then
        VENV_UPDATED_IN_PLACE=true
    else
        print_warning "In-place dependency update failed; falling back to a full rebuild"
    fi
fi

if [[ "$VENV_UPDATED_IN_PLACE" != true ]]; then
    # Build dependencies in a fresh environment.  The legacy venv was writable by
    # the service account; reusing it could preserve a malicious .pth/module and
    # turn that persistence into root-owned executable code during hardening.
    VENV_BUILD="$INSTALL_DIR/.venv-build-$$"
    VENV_OLD="$INSTALL_DIR/.venv-old-$$"
    rm -rf "$VENV_BUILD" "$VENV_OLD"
    print_info "Creating a fresh isolated Python environment"
    python3 -m venv "$VENV_BUILD"
    VENV_BUILD_PYTHON="$VENV_BUILD/bin/python"

    # Ensure pip is available and up to date inside the venv
    print_info "Ensuring pip is available and up to date in the virtual environment"
    $VENV_BUILD_PYTHON -m ensurepip --upgrade >/dev/null 2>&1 || true
    $VENV_BUILD_PYTHON -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true

    # Install dependencies in venv using python -m pip (more portable than calling pip directly)
    print_info "Installing Python dependencies from requirements.txt"
    print_info "This may take a few minutes depending on your internet connection..."
    if [ ! -f "$INSTALL_DIR/requirements.txt" ]; then
        print_error "requirements.txt not found in installation directory"
        exit 1
    fi
    configure_armv7_pip_args "$INSTALL_DIR/requirements.txt"
    $VENV_BUILD_PYTHON -m pip install --quiet "${ARMV7_PIP_ARGS[@]}" -r "$INSTALL_DIR/requirements.txt" || {
        print_error "Failed to install Python dependencies"
        print_info "You may need to check your internet connection or Python version"
        rm -rf "$VENV_BUILD"
        exit 1
    }
    if [ -d "$INSTALL_DIR/venv" ]; then
        mv "$INSTALL_DIR/venv" "$VENV_OLD"
    fi
    if ! mv "$VENV_BUILD" "$INSTALL_DIR/venv"; then
        [ -d "$VENV_OLD" ] && mv "$VENV_OLD" "$INSTALL_DIR/venv"
        print_error "Failed to activate the newly built virtual environment"
        exit 1
    fi
    # Console scripts still embed the .venv-build-$$ shebang; rewrite before any
    # later call to venv/bin/pip or other entry points (see issue #229).  Keep
    # VENV_OLD until rewrite succeeds so a failure can restore the prior tree.
    if ! bash "$INSTALL_DIR/scripts/rewrite_venv_shebangs.sh" "$INSTALL_DIR/venv"; then
        print_error "Failed to rewrite virtualenv console-script shebangs after relocate"
        if [ -d "$VENV_OLD" ]; then
            rm -rf "$INSTALL_DIR/venv"
            mv "$VENV_OLD" "$INSTALL_DIR/venv"
            print_warning "Restored the previous virtualenv after shebang rewrite failure"
        fi
        exit 1
    fi
    rm -rf "$VENV_OLD"
    print_success "Installed all Python dependencies into a fresh virtual environment"
fi

# Optional extras.  A rebuild starts empty so these have to be re-chosen; an
# in-place update keeps whatever was installed before, so don't re-prompt.
# --install-extras installs both sets non-interactively and takes precedence,
# so an unattended run can top up an in-place venv update too.
# Always invoke pip via `python -m pip` so a stale shebang cannot break installs.
VENV_PYTHON="$INSTALL_DIR/venv/bin/python"

install_profanity_packages() {
    print_info "Installing profanity filter packages..."
    if "$VENV_PYTHON" -m pip install --quiet "better-profanity>=0.7.0" "unidecode>=1.3.0"; then
        print_success "Installed profanity filter packages"
    else
        print_warning "Failed to install profanity filter packages (non-fatal)"
    fi
}

install_geocoding_packages() {
    print_info "Installing geocoding extras..."
    if "$VENV_PYTHON" -m pip install --quiet "pycountry>=23.12.0" "us>=2.0.0"; then
        print_success "Installed geocoding extras"
    else
        print_warning "Failed to install geocoding extras (non-fatal)"
    fi
}

if [[ "$INSTALL_EXTRAS" == true ]]; then
    print_info "Installing optional feature packages (--install-extras)"
    install_profanity_packages
    install_geocoding_packages
elif [[ "$VENV_UPDATED_IN_PLACE" == true ]]; then
    print_info "Kept any optional packages already installed in the virtualenv"
else
    echo ""
    print_info "Optional feature packages are available:"
    echo "  • Profanity filter (better-profanity, unidecode) — drop/censor offensive messages"
    echo "  • Geocoding extras (pycountry, us) — improved country/state name resolution"
    echo ""

    if ask_yes_no "Install profanity filter packages? (recommended if using the profanity filter feature)" "n"; then
        install_profanity_packages
    else
        print_info "Skipping profanity filter packages"
    fi

    if ask_yes_no "Install geocoding extras? (recommended if using location/path commands)" "n"; then
        install_geocoding_packages
    else
        print_info "Skipping geocoding extras"
    fi
fi

print_section "Step 5: Setting File Permissions"
print_info "Configuring file ownership and permissions for security"
print_info "Executable code is root-owned; the service owns only configuration and runtime state"
# Executable code and the virtual environment must not be writable by the
# network-facing service account.
# The service group receives read-only access to any explicitly installed key
# material while root remains the only account able to modify it.
CODE_GROUP="$SERVICE_GROUP"
chown -R "root:$CODE_GROUP" "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$LOG_DIR"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$CONF_DIR" "$STATE_DIR"
print_success "Separated root-owned code from service-owned runtime state"

# Code is readable/executable but never service-writable.  Preserve executable
# bits created by the virtualenv and source scripts while dropping group/other
# write access.
chmod 755 "$INSTALL_DIR"
chmod -R go-w "$INSTALL_DIR"
find "$INSTALL_DIR" -type f -name "*.py" -exec chmod 644 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type f -name "*.txt" -exec chmod 644 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type f -name "*.json" -exec chmod 644 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type d -exec chmod 755 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type f -name "*.ini" -exec chmod 600 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type f \( -name ".env" -o -name "*.key" -o -name "*.pem" -o -name "*.p12" -o -name "*.pfx" \) -exec chmod 640 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type f \( -name "*.db" -o -name "*.db-wal" -o -name "*.db-shm" -o -name "*.log" -o -name "*.log.*" \) -exec chmod 600 {} \; 2>/dev/null || true

# The pattern rules above only restore read on *.py/*.txt/*.json, and `chmod go-w`
# removes write without ever granting read, so the virtualenv is still full of
# root-only files created under `umask 077`.  Normalize it last so that *.ini
# files bundled inside site-packages stay readable to their own package.
harden_venv_permissions "$INSTALL_DIR/venv"

# Credentials, databases, backups, and local-plugin settings are private to the
# service user.  Directories must be writable for SQLite sidecars and atomic
# config updates; the 0700 boundary prevents local disclosure.
find "$CONF_DIR" "$STATE_DIR" -type d -exec chmod 700 {} \; 2>/dev/null || true
find "$CONF_DIR" "$STATE_DIR" -type f -exec chmod 600 {} \; 2>/dev/null || true
chmod 750 "$LOG_DIR"
find "$LOG_DIR" -type f -exec chmod 600 {} \; 2>/dev/null || true

# Make main script executable
chmod 755 "$INSTALL_DIR/meshcore_bot.py"
print_success "Configured file permissions"

print_section "Step 6: Installing Service"
if [[ "$IS_MACOS" == true ]]; then
    print_info "Installing launchd plist file to enable automatic startup"
    print_info "The service will be configured to start on boot and restart on failure"
    # Create LaunchDaemons directory if it doesn't exist
    mkdir -p "$LAUNCHD_DIR"

    # Update plist with actual installation paths and copy to LaunchDaemons
    if [ -f "$LAUNCHD_DIR/$SERVICE_FILE" ] && [[ "$UPGRADE_MODE" != true ]]; then
        print_info "Plist file already exists at $LAUNCHD_DIR/$SERVICE_FILE"
        print_info "Skipping update (use --upgrade to update service configuration)"
    else
        print_info "Updating plist file with installation paths"
        # Use a more portable approach for path substitution
        if command -v python3 &> /dev/null; then
            python3 -c "
import sys
import re
with open('$SERVICE_FILE', 'r') as f:
    content = f.read()
content = content.replace('/usr/local/meshcore-bot', '$INSTALL_DIR')
content = content.replace('/usr/local/etc/meshcore-bot', '$CONF_DIR')
content = content.replace('/usr/local/var/lib/meshcore-bot', '$STATE_DIR')
content = content.replace('/usr/local/var/log/meshcore-bot', '$LOG_DIR')
content = content.replace('__MESHCORE_SERVICE_USER__', '$SERVICE_USER')
with open('$LAUNCHD_DIR/$SERVICE_FILE', 'w') as f:
    f.write(content)
"
        else
            # Fallback to sed (works on both macOS and Linux)
            sed "s|/usr/local/meshcore-bot|$INSTALL_DIR|g; s|/usr/local/etc/meshcore-bot|$CONF_DIR|g; s|/usr/local/var/lib/meshcore-bot|$STATE_DIR|g; s|/usr/local/var/log/meshcore-bot|$LOG_DIR|g; s|__MESHCORE_SERVICE_USER__|$SERVICE_USER|g" "$SERVICE_FILE" > "$LAUNCHD_DIR/$SERVICE_FILE"
        fi
        print_success "Copied and configured plist file to $LAUNCHD_DIR/"
    fi

    # Set ownership
    chown root:wheel "$LAUNCHD_DIR/$SERVICE_FILE"
    chmod 644 "$LAUNCHD_DIR/$SERVICE_FILE"
    print_success "Set plist permissions"

    print_section "Step 7: Loading Service"
    # Check if service is already loaded
    if launchctl list "$PLIST_NAME" &>/dev/null; then
        if [[ "$UPGRADE_MODE" == true ]]; then
            print_info "Service already loaded - reloading in upgrade mode"
            launchctl unload "$LAUNCHD_DIR/$SERVICE_FILE" 2>/dev/null || true
            launchctl load "$LAUNCHD_DIR/$SERVICE_FILE" 2>/dev/null || {
                print_error "Failed to reload service. Check plist syntax and permissions."
                exit 1
            }
            print_success "Service '$PLIST_NAME' reloaded in launchd"
        else
            print_info "Service '$PLIST_NAME' is already loaded"
            print_info "Skipping reload (use --upgrade to reload service configuration)"
        fi
    else
        print_info "Loading service into launchd"
        if [[ "$SERVICE_RESTART_PENDING" == true ]]; then
            restart_previously_active_service || {
                print_error "Failed to restore the previously active launchd service"
                exit 1
            }
        else
            launchctl load "$LAUNCHD_DIR/$SERVICE_FILE" 2>/dev/null || {
                print_error "Failed to load service. Check plist syntax and permissions."
                exit 1
            }
        fi
        print_success "Service '$PLIST_NAME' loaded into launchd"
    fi
    print_info "Note: The service is loaded but not started yet. You'll start it after configuration."
else
    # Check if service file already exists
    if [ -f "$SYSTEMD_DIR/$SERVICE_NAME.service" ]; then
        if [[ "$UPGRADE_MODE" == true ]]; then
            print_info "Service file already exists - updating in upgrade mode"
            cp "$SERVICE_FILE" "$SYSTEMD_DIR/"
            print_success "Updated service file in $SYSTEMD_DIR/"
            systemctl daemon-reload
            print_success "Systemd configuration reloaded"
        else
            print_info "Service file already exists at $SYSTEMD_DIR/$SERVICE_NAME.service"
            print_info "Skipping update (use --upgrade to update service configuration)"
        fi
    else
        print_info "Installing systemd service file to enable automatic startup"
        print_info "The service will be configured to start on boot and restart on failure"
        cp "$SERVICE_FILE" "$SYSTEMD_DIR/"
        print_success "Copied service file to $SYSTEMD_DIR/"
        systemctl daemon-reload
        print_success "Systemd configuration reloaded"
    fi

    print_section "Step 7: Enabling Service"
    # Check if service is already enabled
    if systemctl is-enabled "$SERVICE_NAME" &>/dev/null; then
        print_info "Service '$SERVICE_NAME' is already enabled for automatic startup"
    else
        print_info "Enabling service to start automatically on system boot"
        systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
        print_success "Service '$SERVICE_NAME' enabled for automatic startup"
    fi
    print_info "Note: The service is enabled but not started yet. You'll start it after configuration."
fi

if [[ "$SERVICE_RESTART_PENDING" == true ]]; then
    print_info "Restarting the service because it was running before the upgrade"
    if ! restart_previously_active_service; then
        print_error "Failed to restart the previously active service"
        exit 1
    fi
fi
trap - EXIT

if [[ "$UPGRADE_MODE" == true ]]; then
    print_section "Upgrade Complete!"
    echo ""
    print_success "MeshCore Bot has been successfully upgraded!"
else
    print_section "Installation Complete!"
    echo ""
    print_success "MeshCore Bot has been successfully installed as a system service!"
fi
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📋 Next Steps${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${CYAN}1. Configure the bot:${NC}"
echo -e "   ${YELLOW}sudo nano $CONFIG_FILE${NC}"
echo "   Edit the configuration file with your bot settings, API keys, and device information"
echo ""

if [[ "$IS_MACOS" == true ]]; then
    echo -e "${CYAN}2. Start the service:${NC}"
    echo -e "   ${YELLOW}sudo launchctl load -w $LAUNCHD_DIR/$SERVICE_FILE${NC}"
    echo -e "   Or: ${YELLOW}sudo launchctl start $PLIST_NAME${NC}"
    echo ""
    echo -e "${CYAN}3. Verify it's running:${NC}"
    echo -e "   ${YELLOW}sudo launchctl list | grep $PLIST_NAME${NC}"
    echo -e "   Or check logs: ${YELLOW}tail -f $LOG_DIR/meshcore-bot.log${NC}"
    echo ""
    echo -e "${CYAN}4. View live logs (optional):${NC}"
    echo -e "   ${YELLOW}tail -f $LOG_DIR/meshcore-bot.log${NC}"
    echo "   Press Ctrl+C to exit log view"
else
    echo -e "${CYAN}2. Start the service:${NC}"
    echo -e "   ${YELLOW}sudo systemctl start $SERVICE_NAME${NC}"
    echo ""
    echo -e "${CYAN}3. Verify it's running:${NC}"
    echo -e "   ${YELLOW}sudo systemctl status $SERVICE_NAME${NC}"
    echo "   You should see 'active (running)' in green"
    echo ""
    echo -e "${CYAN}4. View live logs (optional):${NC}"
    echo -e "   ${YELLOW}sudo journalctl -u $SERVICE_NAME -f${NC}"
    echo "   Press Ctrl+C to exit log view"
fi
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🔧 Service Management Commands${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

if [[ "$IS_MACOS" == true ]]; then
    echo -e "  ${CYAN}Start service:${NC}     ${YELLOW}sudo launchctl start $PLIST_NAME${NC}"
    echo -e "  ${CYAN}Stop service:${NC}       ${YELLOW}sudo launchctl stop $PLIST_NAME${NC}"
    echo -e "  ${CYAN}Restart service:${NC}   ${YELLOW}sudo launchctl stop $PLIST_NAME && sudo launchctl start $PLIST_NAME${NC}"
    echo -e "  ${CYAN}Check status:${NC}      ${YELLOW}sudo launchctl list | grep $PLIST_NAME${NC}"
    echo -e "  ${CYAN}View logs:${NC}         ${YELLOW}tail -f $LOG_DIR/meshcore-bot.log${NC}"
    echo -e "  ${CYAN}View error logs:${NC}   ${YELLOW}tail -f $LOG_DIR/meshcore-bot.error.log${NC}"
    echo -e "  ${CYAN}Unload service:${NC}    ${YELLOW}sudo launchctl unload $LAUNCHD_DIR/$SERVICE_FILE${NC}"
    echo -e "  ${CYAN}Load service:${NC}       ${YELLOW}sudo launchctl load $LAUNCHD_DIR/$SERVICE_FILE${NC}"
else
    echo -e "  ${CYAN}Start service:${NC}     ${YELLOW}sudo systemctl start $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}Stop service:${NC}      ${YELLOW}sudo systemctl stop $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}Restart service:${NC}   ${YELLOW}sudo systemctl restart $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}Check status:${NC}      ${YELLOW}sudo systemctl status $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}View logs:${NC}         ${YELLOW}sudo journalctl -u $SERVICE_NAME -f${NC}"
    echo -e "  ${CYAN}View recent logs:${NC}  ${YELLOW}sudo journalctl -u $SERVICE_NAME -n 100${NC}"
    echo -e "  ${CYAN}Disable auto-start:${NC} ${YELLOW}sudo systemctl disable $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}Enable auto-start:${NC}  ${YELLOW}sudo systemctl enable $SERVICE_NAME${NC}"
fi
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📁 Important File Locations${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${CYAN}Configuration file:${NC}  ${YELLOW}$CONFIG_FILE${NC}"
echo -e "  ${CYAN}State directory:${NC}       ${YELLOW}$STATE_DIR${NC}"
echo -e "  ${CYAN}Log directory:${NC}        ${YELLOW}$LOG_DIR${NC}"
echo -e "  ${CYAN}Installation directory:${NC} ${YELLOW}$INSTALL_DIR${NC}"
if [[ "$IS_MACOS" == true ]]; then
    echo -e "  ${CYAN}Service plist:${NC}        ${YELLOW}$LAUNCHD_DIR/$SERVICE_FILE${NC}"
else
    echo -e "  ${CYAN}Service file:${NC}        ${YELLOW}$SYSTEMD_DIR/$SERVICE_NAME.service${NC}"
fi
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}ℹ️  Additional Information${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
print_info "The service is configured to:"
echo "  • Start automatically on system boot"
if [[ "$IS_MACOS" == true ]]; then
    echo "  • Restart automatically if it crashes (with 10 second throttle)"
    echo "  • Run as user '$SERVICE_USER'"
    echo "  • Log to: $LOG_DIR/meshcore-bot.log"
    echo ""
    print_info "After editing config.ini, restart the service for changes to take effect:"
    echo -e "  ${YELLOW}sudo launchctl stop $PLIST_NAME && sudo launchctl start $PLIST_NAME${NC}"
else
    echo "  • Restart automatically if it crashes (with 10 second delay)"
    echo "  • Run as user '$SERVICE_USER' for security"
    echo "  • Log to systemd journal (view with journalctl)"
    echo ""
    print_info "Serial port access:"
    echo "  • User '$SERVICE_USER' has been added to dialout group for serial port access"
    echo "  • If using serial connection, ensure the service is restarted after installation"
    echo "  • Group membership changes take effect after service restart"
    echo ""
    print_info "After editing config.ini, restart the service for changes to take effect:"
    echo -e "  ${YELLOW}sudo systemctl restart $SERVICE_NAME${NC}"
fi
echo ""
if [[ "$UPGRADE_MODE" == true ]]; then
    print_success "Upgrade complete! The bot files have been updated."
    print_info "You may want to restart the service to apply changes:"
    if [[ "$IS_MACOS" == true ]]; then
        echo -e "  ${YELLOW}sudo launchctl stop $PLIST_NAME && sudo launchctl start $PLIST_NAME${NC}"
    else
        echo -e "  ${YELLOW}sudo systemctl restart $SERVICE_NAME${NC}"
    fi
else
    print_success "Installation complete! The bot is ready to configure and start."
fi
echo ""
