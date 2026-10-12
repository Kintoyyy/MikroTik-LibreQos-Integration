#!/bin/bash
#
# Install or upgrade LQ-Sync into its own directory, /opt/lq-sync.
#
# LibreQoS keeps /opt/libreqos to itself: this app only writes ShapedDevices.csv
# and network.json into LibreQoS's directory (lqos_directory in /etc/lqos.conf).
# Re-running is safe: config.json, settings.json, the GUI login, the device
# database and the graphs are kept. Installs made by older versions inside
# /opt/libreqos/src are moved here automatically.

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

APP_DIR="/opt/lq-sync"
VENV="$APP_DIR/venv"
LEGACY_DIR="/opt/libreqos/src"        # where older versions installed themselves
CONFIG_JSON="$APP_DIR/config.json"
SETTINGS_JSON="$APP_DIR/settings.json"
UNIT_DIR="/etc/systemd/system"
SERVICES="updatecsv wan_service gui"
STATE_FILES="config.json settings.json gui_auth.json devices.db devices.db-wal devices.db-shm .installed_version"
LEGACY_CODE="updatecsv.py wan_service.py gui.py settings.py rate_resolver.py device_database.py node_assigner.py router_scanner.py wan_manager.py"
# Text that only this app's files contain — nothing is moved out of LibreQoS's
# directory unless it is recognisably ours.
OURS='routeros|RouterScanner|DeviceDatabase|RateResolver|NodeAssigner|WANManager|_SETTINGS_PATH|LQ-Sync|LQ <span'

if [ "$EUID" -ne 0 ]; then
    printf "${RED}✘ This installer must be run as root (use sudo).${NC}\n"
    exit 1
fi

cd "$(dirname "$(readlink -f "$0")")" || exit 1

for svc in $SERVICES; do
    if [ ! -f "$svc.service" ]; then
        printf "${RED}✘ $svc.service is missing — run the installer from the repository checkout.${NC}\n"
        exit 1
    fi
done

# ── System packages ───────────────────────────────────────────────────────

printf "${BLUE}➜ Installing system dependencies...${NC}\n"
if ! command -v apt-get &> /dev/null; then
    printf "${RED}✘ Unsupported package manager. Please install dependencies manually.${NC}\n"
    exit 1
fi
apt-get update || { printf "${RED}✘ Failed to update package lists.${NC}\n"; exit 1; }
apt-get install -y python3 python3-pip python3-venv jq git iputils-ping rrdtool fonts-dejavu-core || {
    printf "${RED}✘ Failed to install system dependencies.${NC}\n"
    exit 1
}

# ── Stop the services while files move ────────────────────────────────────

for svc in $SERVICES; do
    systemctl stop "$svc.service" 2>/dev/null
done

mkdir -p "$APP_DIR/templates"
FIRST_INSTALL=0
[ -f "$APP_DIR/gui_auth.json" ] || [ -f "$LEGACY_DIR/gui_auth.json" ] || FIRST_INSTALL=1

# ── Move an older install out of LibreQoS's directory ─────────────────────

if [ -f "$LEGACY_DIR/updatecsv.py" ] && grep -qE "$OURS" "$LEGACY_DIR/updatecsv.py"; then
    BACKUP="$APP_DIR/migrated-from-libreqos-src-$(date +%Y%m%d-%H%M%S)"
    printf "${BLUE}➜ Moving LQ-Sync out of $LEGACY_DIR into $APP_DIR...${NC}\n"
    mkdir -p "$BACKUP"
    chmod 700 "$BACKUP"

    # State: moved here unless this directory already has its own copy
    for f in $STATE_FILES; do
        [ -e "$LEGACY_DIR/$f" ] || continue
        if [ -e "$APP_DIR/$f" ]; then
            mv "$LEGACY_DIR/$f" "$BACKUP/$f"
        else
            mv "$LEGACY_DIR/$f" "$APP_DIR/$f"
            printf "  • $f\n"
        fi
    done
    if [ -d "$LEGACY_DIR/rrd" ] && [ ! -e "$APP_DIR/rrd" ]; then
        mv "$LEGACY_DIR/rrd" "$APP_DIR/rrd"
    fi

    # Old code: kept in the backup folder, only files that are ours
    for f in $LEGACY_CODE; do
        if [ -f "$LEGACY_DIR/$f" ] && grep -qE "$OURS" "$LEGACY_DIR/$f"; then
            mv "$LEGACY_DIR/$f" "$BACKUP/$f"
        fi
    done
    if [ -f "$LEGACY_DIR/templates/index.html" ] && grep -qE "$OURS" "$LEGACY_DIR/templates/index.html"; then
        mkdir -p "$BACKUP/templates"
        mv "$LEGACY_DIR/templates/index.html" "$BACKUP/templates/index.html"
        rmdir "$LEGACY_DIR/templates" 2>/dev/null    # only removed when now empty
    fi
    for d in "$LEGACY_DIR"/.update_backup_*; do
        [ -d "$d" ] && mv "$d" "$BACKUP/"
    done
    printf "${GREEN}✔ Moved. Old copies are in $BACKUP${NC}\n"
    printf "${YELLOW}  ShapedDevices.csv and network.json stay in $LEGACY_DIR — LibreQoS reads them there.${NC}\n"
fi

# ── Python environment (this app's own; LibreQoS keeps /opt/libreqos/venv) ─

printf "${BLUE}➜ Creating Python virtual environment in $VENV...${NC}\n"
python3 -m venv "$VENV" || { printf "${RED}✘ Failed to create the virtual environment.${NC}\n"; exit 1; }
"$VENV/bin/pip" install --upgrade routeros-api flask psutil || {
    printf "${RED}✘ Failed to install Python dependencies.${NC}\n"
    exit 1
}

# ── Code ──────────────────────────────────────────────────────────────────

printf "${YELLOW}➜ Copying application files...${NC}\n"
cp ./*.py "$APP_DIR/"
chmod +x "$APP_DIR/updatecsv.py" "$APP_DIR/wan_service.py" "$APP_DIR/gui.py"
cp -r templates/. "$APP_DIR/templates/"

# ── config.json / settings.json — never overwritten ───────────────────────

if [ -s "$CONFIG_JSON" ]; then
    printf "${YELLOW}➜ Keeping existing config.json.${NC}\n"
elif [ -f "config.json" ] && jq empty config.json >/dev/null 2>&1; then
    printf "${YELLOW}➜ Copying sample config.json — set your routers in the GUI.${NC}\n"
    cp "config.json" "$CONFIG_JSON"
else
    printf "${YELLOW}➜ Writing default config.json.${NC}\n"
    cat << 'EOF' > "$CONFIG_JSON"
{
    "strategy": "cpu",
    "promote_to_root": false,
    "queues": true,
    "bras": [
        {
            "name": "Mikrotik AC",
            "address": "192.168.88.2",
            "port": 8728,
            "username": "admin",
            "password": "password",
            "pppoe":        { "enabled": true,  "default_download_limit": 100, "default_upload_limit": 100 },
            "hotspot":      { "enabled": false, "default_download_limit": 10,  "default_upload_limit": 10 },
            "dhcp":         { "enabled": false, "default_download_limit": 50,  "default_upload_limit": 50 },
            "address_list": { "default_download_limit": 100, "default_upload_limit": 100 }
        }
    ],
    "cores": [],
    "wan_assignment": { "enabled": false, "include_hotspot": false, "include_dhcp": false, "interval": 300 }
}
EOF
fi

if [ -s "$SETTINGS_JSON" ]; then
    printf "${YELLOW}➜ Keeping existing settings.json.${NC}\n"
elif [ -f "settings.json" ] && jq empty settings.json >/dev/null 2>&1; then
    cp "settings.json" "$SETTINGS_JSON"
fi

if ! jq empty "$CONFIG_JSON" >/dev/null 2>&1; then
    printf "${RED}✘ Error: Invalid JSON in $CONFIG_JSON${NC}\n"
    exit 1
fi

# Router passwords, the GUI session key and subscriber data: root only.
chown -R root:root "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 600 "$CONFIG_JSON"
for f in gui_auth.json devices.db; do
    [ -f "$APP_DIR/$f" ] && chmod 600 "$APP_DIR/$f"
done

# Record the installed version (shown in the GUI) when installing from a git checkout.
if git rev-parse --short HEAD >/dev/null 2>&1; then
    git rev-parse --short HEAD > "$APP_DIR/.installed_version"
fi

# ── systemd ───────────────────────────────────────────────────────────────

printf "${BLUE}➜ Installing systemd units...${NC}\n"
for svc in $SERVICES; do
    cp "$svc.service" "$UNIT_DIR/$svc.service"
done
systemctl daemon-reload

printf "${BLUE}➜ Starting updatecsv and the GUI...${NC}\n"
systemctl enable updatecsv.service gui.service
systemctl restart updatecsv.service gui.service

# WAN assignment is optional: wan_service runs only when config.json enables it.
# The GUI enables/disables the service when WAN Assignment is toggled.
if jq -e '.wan_assignment.enabled == true' "$CONFIG_JSON" >/dev/null 2>&1; then
    printf "${BLUE}➜ WAN assignment enabled — starting and enabling wan_service...${NC}\n"
    systemctl enable wan_service.service
    systemctl restart wan_service.service
else
    printf "${BLUE}➜ WAN assignment disabled — wan_service installed but not enabled.${NC}\n"
    systemctl disable --now wan_service.service >/dev/null 2>&1
fi

printf "${GREEN}✔ Installation complete — LQ-Sync lives in $APP_DIR${NC}\n\n"
printf "${GREEN}  Web GUI:${NC}  http://$(hostname -I | awk '{print $1}'):5000\n"
if [ "$FIRST_INSTALL" = 1 ]; then
    printf "${GREEN}  Password:${NC} admin  (change it after the first login)\n"
fi
printf "\n${BLUE}Service status:${NC}\n"
systemctl status updatecsv.service --no-pager -l
printf "\n"
if systemctl is-enabled --quiet wan_service.service; then
    systemctl status wan_service.service --no-pager -l
    printf "\n"
fi
systemctl status gui.service --no-pager -l
