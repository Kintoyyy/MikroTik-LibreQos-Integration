#!/bin/bash
#
# Remove LQ-Sync: its services and its directory, /opt/lq-sync (code, config,
# GUI login, device database, graphs). LibreQoS is not touched — its venv,
# ShapedDevices.csv and network.json stay, so shaping keeps the last state.

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

APP_DIR="/opt/lq-sync"
LEGACY_DIR="/opt/libreqos/src"        # where older versions installed themselves
UNIT_DIR="/etc/systemd/system"
SERVICES="updatecsv wan_service gui"
LEGACY_FILES="updatecsv.py wan_service.py gui.py settings.py rate_resolver.py device_database.py node_assigner.py router_scanner.py wan_manager.py config.json settings.json gui_auth.json devices.db devices.db-wal devices.db-shm .installed_version"
OURS='routeros|RouterScanner|DeviceDatabase|RateResolver|NodeAssigner|WANManager|_SETTINGS_PATH|LQ-Sync|LQ <span|"bras"|password_hash|wan_assignment'

DELETED_FILES=()

if [[ $EUID -ne 0 ]]; then
    printf "${RED}✘ This script must be run as root. Use sudo.${NC}\n"
    exit 1
fi

# ── Stop and disable services ─────────────────────────────────────────────

printf "${BLUE}➜ Stopping and disabling services...${NC}\n"
for svc in $SERVICES; do
    systemctl stop "$svc.service"    2>/dev/null
    systemctl disable "$svc.service" 2>/dev/null
    if [ -f "$UNIT_DIR/$svc.service" ]; then
        rm -f "$UNIT_DIR/$svc.service"
        DELETED_FILES+=("$UNIT_DIR/$svc.service")
    fi
done
systemctl daemon-reload

# ── Remove the app directory ──────────────────────────────────────────────

if [ -d "$APP_DIR" ]; then
    printf "${BLUE}➜ Removing $APP_DIR...${NC}\n"
    rm -rf --one-file-system "$APP_DIR"
    DELETED_FILES+=("$APP_DIR")
fi

# ── Leftovers of an old install inside LibreQoS's directory ───────────────
# Only files that are recognisably this app's are removed.

OUR_DB=0
[ -f "$LEGACY_DIR/devices.db" ] && grep -qa "circuit_id" "$LEGACY_DIR/devices.db" && OUR_DB=1
for f in $LEGACY_FILES; do
    p="$LEGACY_DIR/$f"
    [ -f "$p" ] || continue
    case "$f" in
        devices.db*)        [ "$OUR_DB" = 1 ] || continue ;;   # its schema has circuit_id
        .installed_version) ;;                                  # named only by this app
        *)                  grep -qE "$OURS" "$p" || continue ;;
    esac
    rm -f "$p"
    DELETED_FILES+=("$p")
done
if [ -f "$LEGACY_DIR/templates/index.html" ] && grep -qE "$OURS" "$LEGACY_DIR/templates/index.html"; then
    rm -f "$LEGACY_DIR/templates/index.html"
    rmdir "$LEGACY_DIR/templates" 2>/dev/null
    DELETED_FILES+=("$LEGACY_DIR/templates/index.html")
fi

# ── Summary ───────────────────────────────────────────────────────────────

if [ ${#DELETED_FILES[@]} -gt 0 ]; then
    printf "\n${GREEN}✔ Deleted:${NC}\n"
    for f in "${DELETED_FILES[@]}"; do
        printf "${YELLOW}  • $f${NC}\n"
    done
else
    printf "${YELLOW}➜ Nothing to remove — LQ-Sync is not installed.${NC}\n"
fi

printf "\n${BLUE}LibreQoS was left untouched: its venv, ShapedDevices.csv and network.json stay,\n"
printf "so shaping keeps the last state this app wrote.${NC}\n"
printf "${GREEN}✔ LQ-Sync uninstall complete.${NC}\n"
