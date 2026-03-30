#!/bin/bash

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

LIBREQOS_DIR="/opt/libreqos"
SRC_DIR="$LIBREQOS_DIR/src"
SERVICE_FILE="/etc/systemd/system/updatecsv.service"
GUI_SERVICE_FILE="/etc/systemd/system/gui.service"

DELETED_FILES=()

log_delete() {
    local path="$1"
    if [ -f "$path" ] || [ -d "$path" ]; then
        rm -rf "$path"
        DELETED_FILES+=("$path")
    fi
}

if [[ $EUID -ne 0 ]]; then
    printf "${RED}✘ This script must be run as root. Use sudo.${NC}\n"
    exit 1
fi

# ── Stop and disable services ─────────────────────────────────────────────

printf "${BLUE}➜ Stopping and disabling services...${NC}\n"
for svc in updatecsv.service gui.service; do
    systemctl stop "$svc"    2>/dev/null
    systemctl disable "$svc" 2>/dev/null
done

# ── Remove installed files ────────────────────────────────────────────────

printf "${BLUE}➜ Removing installed files...${NC}\n"

log_delete "$SRC_DIR/updatecsv.py"
log_delete "$SRC_DIR/gui.py"
log_delete "$SRC_DIR/templates"
log_delete "$SRC_DIR/config.json"
log_delete "$SRC_DIR/config.json.bak"
log_delete "$SRC_DIR/devices.db"
log_delete "$SRC_DIR/gui_auth.json"
log_delete "$SRC_DIR/network.json"
log_delete "$SRC_DIR/ShapedDevices.csv"
log_delete "$SERVICE_FILE"
log_delete "$GUI_SERVICE_FILE"

# ── Remove Python venv ────────────────────────────────────────────────────

if [ -d "$LIBREQOS_DIR/venv" ]; then
    printf "${BLUE}➜ Removing Python virtual environment...${NC}\n"
    rm -rf "$LIBREQOS_DIR/venv"
    DELETED_FILES+=("$LIBREQOS_DIR/venv")
fi

# ── Reload systemd ────────────────────────────────────────────────────────

systemctl daemon-reload

# ── Summary ───────────────────────────────────────────────────────────────

if [ ${#DELETED_FILES[@]} -gt 0 ]; then
    printf "\n${GREEN}✔ Deleted:${NC}\n"
    for f in "${DELETED_FILES[@]}"; do
        printf "${YELLOW}  • $f${NC}\n"
    done
    printf "\n${GREEN}✔ Total removed: ${#DELETED_FILES[@]}${NC}\n"
else
    printf "${YELLOW}➜ Nothing to remove — files not found.${NC}\n"
fi

printf "${GREEN}✔ LQ-Sync uninstall complete.${NC}\n"
