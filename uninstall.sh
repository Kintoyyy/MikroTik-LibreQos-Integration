#!/bin/bash

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

LIBREQOS_DIR="/opt/libreqos"
SRC_DIR="$LIBREQOS_DIR/src"
PYTHON_SCRIPT="$SRC_DIR/updatecsv.py"
CONFIG_JSON="$SRC_DIR/config.json"
SERVICE_FILE="/etc/systemd/system/updatecsv.service"

DELETED_FILES=()

log_deleted_file() {
    if [ -f "$1" ] || [ -d "$1" ]; then
        rm -f "$1"
        DELETED_FILES+=("$1")
    fi
}

if [[ $EUID -ne 0 ]]; then
   printf "${RED}✘ This script must be run as root. Use sudo.${NC}\n"
   exit 1
fi

printf "${BLUE}➜ Stopping LibreQoS update service...${NC}\n"
systemctl stop updatecsv.service 2>/dev/null
systemctl disable updatecsv.service 2>/dev/null

log_deleted_file "$PYTHON_SCRIPT"
log_deleted_file "$CONFIG_JSON"
log_deleted_file "$CONFIG_JSON.bak"
log_deleted_file "$SERVICE_FILE"

if [ -d "$LIBREQOS_DIR/venv" ]; then
    printf "${BLUE}➜ Removing Python dependencies...${NC}\n"
    "$LIBREQOS_DIR/venv/bin/pip" freeze | xargs "$LIBREQOS_DIR/venv/bin/pip" uninstall -y
fi

if [ ${#DELETED_FILES[@]} -gt 0 ]; then
    printf "\n${GREEN}✔ Deleted Files:${NC}\n"
    for file in "${DELETED_FILES[@]}"; do
        printf "${YELLOW}  • $file${NC}\n"
    done
    printf "\n${GREEN}✔ Total files deleted: ${#DELETED_FILES[@]}${NC}\n"
else
    printf "${YELLOW}➜ No files were deleted.${NC}\n"
fi

printf "${GREEN}✔ LibreQoS uninstallation process complete.${NC}\n"
printf "${BLUE}➜ Please adjust your ShapedDevice.csv and network.json to avoid sanity errors${NC}\n"