#!/bin/bash
# MeshCore Bot Service Uninstallation Script
# This script removes the MeshCore Bot service (systemd or launchd)
# Supports both Linux (systemd) and macOS (launchd)
#
# Safety features:
#   - Backs up config.ini before removal
#   - Asks for confirmation before destructive actions
#   - Optionally preserves installation files

set -e

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
IS_LINUX=false

if [[ "$OS" == "Darwin" ]]; then
    IS_MACOS=true
    _SERVICE_TYPE="launchd" # launchd (macOS); reserved for future diagnostics
elif [[ "$OS" == "Linux" ]]; then
    IS_LINUX=true
else
    echo "Error: Unsupported operating system: $OS"
    echo "This script supports Linux (systemd) and macOS (launchd)"
    exit 1
fi

# Configuration - OS-specific paths
SERVICE_NAME="meshcore-bot"
PLIST_NAME="com.meshcore.bot"

if [[ "$IS_MACOS" == true ]]; then
    SERVICE_USER="$(whoami)"
    _SERVICE_GROUP="staff" # default macOS group; reserved for future ownership hints
    INSTALL_DIR="/usr/local/meshcore-bot"
    LOG_DIR="/usr/local/var/log/meshcore-bot"
    SERVICE_FILE="com.meshcore.bot.plist"
    LAUNCHD_DIR="/Library/LaunchDaemons"
else
    SERVICE_USER="meshcore"
    INSTALL_DIR="/opt/meshcore-bot"
    LOG_DIR="/var/log/meshcore-bot"
    SERVICE_FILE="meshcore-bot.service"
    SYSTEMD_DIR="/etc/systemd/system"
fi

# Capture original user before sudo (for backup location)
ORIGINAL_USER="${SUDO_USER:-$USER}"
if [[ -n "$ORIGINAL_USER" ]] && [[ "$ORIGINAL_USER" != "root" ]]; then
    # Try to get home directory of original user
    HOME_DIR=$(eval echo ~"$ORIGINAL_USER" 2>/dev/null || echo "/home/$ORIGINAL_USER")
    # Verify it exists and is writable
    if [[ ! -d "$HOME_DIR" ]] || [[ ! -w "$HOME_DIR" ]]; then
        HOME_DIR="/tmp"
    fi
else
    # Fallback to /tmp if we can't determine user home
    HOME_DIR="/tmp"
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
    local default="${2:-n}"  # Default to 'no' for safety
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
            [Yy]|[Yy][Ee][Ss])
                return 0
                ;;
            [Nn]|[Nn][Oo])
                return 1
                ;;
            *)
                echo "Please answer yes or no."
                ;;
        esac
    done
}

print_section "MeshCore Bot Service Uninstaller"
echo ""
if [[ "$IS_MACOS" == true ]]; then
    print_info "Detected macOS - will remove launchd service"
else
    print_info "Detected Linux - will remove systemd service"
fi
print_warning "This script will remove the MeshCore Bot service"
echo ""

# Check if script has execute permissions
if [ ! -x "$0" ]; then
    print_warning "Script does not have execute permissions. Attempting to set them..."
    chmod +x "$0" 2>/dev/null || {
        print_error "Could not set execute permissions. Please run: chmod +x uninstall-service.sh"
        exit 1
    }
    print_success "Execute permissions set"
fi

# Check if running as root, if not re-execute with sudo
if [[ $EUID -ne 0 ]]; then
    print_warning "This script requires root privileges to remove system services"
    print_info "Re-executing with sudo..."
    echo ""
    exec sudo "$0" "$@"
fi

# Check if service exists
SERVICE_EXISTS=false
if [[ "$IS_MACOS" == true ]]; then
    if [ -f "$LAUNCHD_DIR/$SERVICE_FILE" ]; then
        SERVICE_EXISTS=true
    fi
else
    if [ -f "$SYSTEMD_DIR/$SERVICE_NAME.service" ]; then
        SERVICE_EXISTS=true
    fi
fi

if [[ "$SERVICE_EXISTS" == false ]]; then
    print_warning "Service not found - it may have already been removed"
    if [[ "$IS_MACOS" == true ]]; then
        print_info "Expected service file: $LAUNCHD_DIR/$SERVICE_FILE"
    else
        print_info "Expected service file: $SYSTEMD_DIR/$SERVICE_NAME.service"
    fi
    echo ""
    if ! ask_yes_no "Continue anyway? (will only remove files if they exist)"; then
        print_info "Uninstallation cancelled by user"
        exit 0
    fi
fi

# Check if installation directory exists
INSTALL_EXISTS=false
if [ -d "$INSTALL_DIR" ]; then
    INSTALL_EXISTS=true
    CONFIG_FILE="$INSTALL_DIR/config.ini"
else
    CONFIG_FILE=""
fi

# Step 1: Backup config.ini if it exists
if [[ "$INSTALL_EXISTS" == true ]] && [ -f "$CONFIG_FILE" ]; then
    print_section "Step 1: Backup Configuration File"
    print_info "Found configuration file: $CONFIG_FILE"
    
    if ask_yes_no "Would you like to backup config.ini?" "y"; then
        TIMESTAMP=$(date +%Y%m%d_%H%M%S)
        BACKUP_FILE="$HOME_DIR/meshcore-bot-config-backup-${TIMESTAMP}.ini"
        
        # Create backup
        cp "$CONFIG_FILE" "$BACKUP_FILE"
        
        # Try to set ownership to original user if possible
        if [[ -n "$ORIGINAL_USER" ]] && [[ "$ORIGINAL_USER" != "root" ]]; then
            chown "$ORIGINAL_USER:$(id -gn "$ORIGINAL_USER" 2>/dev/null || echo 'staff')" "$BACKUP_FILE" 2>/dev/null || true
        fi
        
        print_success "Configuration backed up to: $BACKUP_FILE"
        if [[ "$HOME_DIR" == "/tmp" ]]; then
            print_info "Backup saved to /tmp (home directory not accessible)"
        fi
    else
        print_info "Skipping config.ini backup"
    fi
    
    # Step 1b: Backup database file
    print_section "Step 1b: Database Backup"
    
    # Try to find the main database file (meshcore_bot.db by default, or from config)
    MAIN_DB_FILE=""
    
    # First, try to read db_path from config.ini if it exists
    if [ -f "$CONFIG_FILE" ]; then
        DB_PATH_FROM_CONFIG=$(grep -E "^db_path\s*=" "$CONFIG_FILE" 2>/dev/null | head -1 | cut -d'=' -f2 | tr -d ' ' || echo "")
        if [ -n "$DB_PATH_FROM_CONFIG" ]; then
            # If it's a relative path, make it relative to install dir
            if [[ "$DB_PATH_FROM_CONFIG" != /* ]]; then
                MAIN_DB_FILE="$INSTALL_DIR/$DB_PATH_FROM_CONFIG"
            else
                MAIN_DB_FILE="$DB_PATH_FROM_CONFIG"
            fi
        fi
    fi
    
    # If not found in config, try default location
    if [ -z "$MAIN_DB_FILE" ] || [ ! -f "$MAIN_DB_FILE" ]; then
        MAIN_DB_FILE="$INSTALL_DIR/meshcore_bot.db"
    fi
    
    # Check if main database file exists
    if [ -f "$MAIN_DB_FILE" ]; then
        db_name=$(basename "$MAIN_DB_FILE")
        db_size=$(du -h "$MAIN_DB_FILE" 2>/dev/null | cut -f1)
        print_info "Found database file: $db_name (${db_size})"
        echo ""
        
        if ask_yes_no "Would you like to backup the database file?" "y"; then
            TIMESTAMP=$(date +%Y%m%d_%H%M%S)
            BACKUP_FILE="$HOME_DIR/meshcore-bot-db-backup-${TIMESTAMP}.db"
            
            cp "$MAIN_DB_FILE" "$BACKUP_FILE"
            
            # Try to set ownership to original user if possible
            if [[ -n "$ORIGINAL_USER" ]] && [[ "$ORIGINAL_USER" != "root" ]]; then
                chown "$ORIGINAL_USER:$(id -gn "$ORIGINAL_USER" 2>/dev/null || echo 'staff')" "$BACKUP_FILE" 2>/dev/null || true
            fi
            
            print_success "Database backed up to: $BACKUP_FILE"
            if [[ "$HOME_DIR" == "/tmp" ]]; then
                print_info "Backup saved to /tmp (home directory not accessible)"
            fi
        else
            print_info "Skipping database backup"
        fi
    else
        print_warning "Database file not found: $MAIN_DB_FILE"
        print_info "Skipping database backup"
    fi
else
    print_section "Step 1: Configuration and Database Files"
    if [[ "$INSTALL_EXISTS" == false ]]; then
        print_warning "Installation directory not found: $INSTALL_DIR"
    else
        print_warning "Configuration file not found: $CONFIG_FILE"
    fi
    print_info "Skipping backup step"
fi

# Step 2: Confirm service removal
print_section "Step 2: Service Removal Confirmation"
print_warning "This will stop and remove the MeshCore Bot service"
if [[ "$IS_MACOS" == true ]]; then
    print_info "Service: $PLIST_NAME (launchd)"
else
    print_info "Service: $SERVICE_NAME (systemd)"
fi
echo ""

if ! ask_yes_no "Do you want to remove the service?"; then
    print_info "Service removal cancelled by user"
    echo ""
    if [[ "$INSTALL_EXISTS" == true ]]; then
        print_info "Installation files remain at: $INSTALL_DIR"
        print_info "You can remove them manually if needed"
    fi
    exit 0
fi

# Step 3: Stop and remove service
print_section "Step 3: Stopping and Removing Service"

if [[ "$IS_MACOS" == true ]]; then
    # macOS: Unload launchd service
    if launchctl list "$PLIST_NAME" &>/dev/null 2>&1; then
        print_info "Stopping service..."
        launchctl stop "$PLIST_NAME" 2>/dev/null || true
        launchctl unload "$LAUNCHD_DIR/$SERVICE_FILE" 2>/dev/null || true
        print_success "Service stopped and unloaded"
    else
        print_warning "Service is not currently loaded"
    fi
    
    # Remove plist file
    if [ -f "$LAUNCHD_DIR/$SERVICE_FILE" ]; then
        rm "$LAUNCHD_DIR/$SERVICE_FILE"
        print_success "Removed service plist file"
    else
        print_warning "Service plist file not found"
    fi
else
    # Linux: Stop and disable systemd service
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        print_info "Stopping service..."
        systemctl stop "$SERVICE_NAME"
        print_success "Service stopped"
    else
        print_warning "Service is not currently running"
    fi
    
    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        print_info "Disabling service..."
        systemctl disable "$SERVICE_NAME" >/dev/null 2>&1
        print_success "Service disabled"
    else
        print_warning "Service is not enabled"
    fi
    
    # Remove service file
    if [ -f "$SYSTEMD_DIR/$SERVICE_NAME.service" ]; then
        rm "$SYSTEMD_DIR/$SERVICE_NAME.service"
        print_success "Removed service file"
        
        # Reload systemd
        print_info "Reloading systemd configuration"
        systemctl daemon-reload
        print_success "Systemd configuration reloaded"
    else
        print_warning "Service file not found"
    fi
fi

# Step 4: Ask about removing installation files
print_section "Step 4: Installation Files"
if [[ "$INSTALL_EXISTS" == true ]]; then
    print_warning "Installation directory found: $INSTALL_DIR"
    print_info "This contains all bot files, including:"
    echo "  • Python scripts and modules"
    echo "  • Configuration files"
    echo "  • Virtual environment"
    echo "  • Database files"
    echo "  • Logs"
    echo ""
    
    if ask_yes_no "Do you want to DELETE the installation directory and all its contents?"; then
        print_warning "This action cannot be undone!"
        if ask_yes_no "Are you SURE you want to delete $INSTALL_DIR?"; then
            print_info "Removing installation directory..."
            rm -rf "$INSTALL_DIR"
            print_success "Removed installation directory: $INSTALL_DIR"
        else
            print_info "Installation directory preserved: $INSTALL_DIR"
        fi
    else
        print_info "Installation directory preserved: $INSTALL_DIR"
        print_info "You can remove it manually later if needed"
    fi
else
    print_warning "Installation directory not found: $INSTALL_DIR"
    print_info "Nothing to remove"
fi

# Step 5: Remove log directory (optional)
print_section "Step 5: Log Directory"
if [ -d "$LOG_DIR" ]; then
    print_info "Log directory found: $LOG_DIR"
    if ask_yes_no "Do you want to remove the log directory?"; then
        rm -rf "$LOG_DIR"
        print_success "Removed log directory: $LOG_DIR"
    else
        print_info "Log directory preserved: $LOG_DIR"
    fi
else
    print_warning "Log directory not found: $LOG_DIR"
fi

# Step 6: Remove service user (Linux only)
if [[ "$IS_LINUX" == true ]]; then
    print_section "Step 6: Service User"
    if id "$SERVICE_USER" &>/dev/null; then
        print_info "Service user found: $SERVICE_USER"
        if ask_yes_no "Do you want to remove the service user '$SERVICE_USER'?"; then
            userdel "$SERVICE_USER" 2>/dev/null || {
                print_warning "Could not remove user (may be in use or have dependencies)"
            }
            print_success "Removed service user: $SERVICE_USER"
        else
            print_info "Service user preserved: $SERVICE_USER"
        fi
    else
        print_warning "Service user not found: $SERVICE_USER"
    fi
fi

# Final summary
print_section "Uninstallation Complete"
echo ""
print_success "Service uninstallation completed!"
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📋 Summary${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

if [[ "$IS_MACOS" == true ]]; then
    echo -e "  ${CYAN}Service:${NC}        Removed from launchd"
    echo -e "  ${CYAN}Service file:${NC}   $LAUNCHD_DIR/$SERVICE_FILE (removed)"
else
    echo -e "  ${CYAN}Service:${NC}        Removed from systemd"
    echo -e "  ${CYAN}Service file:${NC}   $SYSTEMD_DIR/$SERVICE_NAME.service (removed)"
fi

if [[ "$INSTALL_EXISTS" == true ]]; then
    if [ -d "$INSTALL_DIR" ]; then
        echo -e "  ${CYAN}Installation:${NC}  ${YELLOW}Preserved at $INSTALL_DIR${NC}"
    else
        echo -e "  ${CYAN}Installation:${NC}  ${GREEN}Removed${NC}"
    fi
else
    echo -e "  ${CYAN}Installation:${NC}  Not found (may have been removed already)"
fi

if [ -d "$LOG_DIR" ]; then
    echo -e "  ${CYAN}Logs:${NC}           ${YELLOW}Preserved at $LOG_DIR${NC}"
else
    echo -e "  ${CYAN}Logs:${NC}           ${GREEN}Removed${NC}"
fi

if [[ "$IS_LINUX" == true ]]; then
    if id "$SERVICE_USER" &>/dev/null; then
        echo -e "  ${CYAN}Service user:${NC}  ${YELLOW}Preserved: $SERVICE_USER${NC}"
    else
        echo -e "  ${CYAN}Service user:${NC}  ${GREEN}Removed${NC}"
    fi
fi

echo ""

# Check for backup files in common locations
CONFIG_BACKUP_FOUND=$(find "$HOME_DIR" /tmp /home -name "meshcore-bot-config-backup-*.ini" -type f 2>/dev/null | head -1)
DB_BACKUP_FOUND=$(find "$HOME_DIR" /tmp /home -name "meshcore-bot-db-backup-*.db" -type f 2>/dev/null | head -1)

if [ -n "$CONFIG_BACKUP_FOUND" ] || [ -n "$DB_BACKUP_FOUND" ]; then
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}💾 Backup Files${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    
    if [ -n "$CONFIG_BACKUP_FOUND" ]; then
        print_success "Configuration backup saved to:"
        echo "  ${YELLOW}$CONFIG_BACKUP_FOUND${NC}"
        echo ""
    fi
    
    if [ -n "$DB_BACKUP_FOUND" ]; then
        print_success "Database backup saved to:"
        echo "  ${YELLOW}$DB_BACKUP_FOUND${NC}"
        echo ""
    fi
    
    print_info "You can restore these backups when reinstalling"
    echo ""
fi

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}ℹ️  Additional Notes${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
if [[ "$INSTALL_EXISTS" == true ]] && [ ! -d "$INSTALL_DIR" ]; then
    print_info "The virtual environment was removed with the installation directory"
    print_info "All Python packages installed for this bot have been removed"
else
    print_info "Python packages are installed inside the virtual environment at:"
    echo "  ${YELLOW}$INSTALL_DIR/venv${NC}"
    print_info "Removing the installation directory above also removes all packages"
fi
echo ""

if [[ "$INSTALL_EXISTS" == true ]] && [ -d "$INSTALL_DIR" ]; then
    print_warning "Installation files are still present at: $INSTALL_DIR"
    print_info "You can remove them manually with:"
    echo "  ${YELLOW}sudo rm -rf $INSTALL_DIR${NC}"
    echo ""
fi

print_success "Uninstallation process completed!"
echo ""
