#!/bin/bash

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

LIBREQOS_DIR="/opt/libreqos"
SRC_DIR="$LIBREQOS_DIR/src"
PYTHON_SCRIPT="$SRC_DIR/updatecsv.py"
GUI_SCRIPT="$SRC_DIR/gui.py"
TEMPLATES_DIR="$SRC_DIR/templates"
CONFIG_JSON="$SRC_DIR/config.json"
SERVICE_FILE="/etc/systemd/system/updatecsv.service"
GUI_SERVICE_FILE="/etc/systemd/system/gui.service"

if [ "$EUID" -ne 0 ]; then
    printf "${RED}✘ This installer must be run as root (use sudo).${NC}\n"
    exit 1
fi

mkdir -p "$SRC_DIR"
mkdir -p "$TEMPLATES_DIR"

printf "${BLUE}➜ Installing system dependencies...${NC}\n"
if command -v apt-get &> /dev/null; then
    apt-get update || {
        printf "${RED}✘ Failed to update package lists.${NC}\n"
        exit 1
    }
    apt-get install -y \
        python3 \
        python3-pip \
        python3-venv \
        jq \
        git \
        iputils-ping || {
        printf "${RED}✘ Failed to install system dependencies.${NC}\n"
        exit 1
    }
else
    printf "${RED}✘ Unsupported package manager. Please install dependencies manually.${NC}\n"
    exit 1
fi

printf "${BLUE}➜ Creating Python virtual environment...${NC}\n"
python3 -m venv "$LIBREQOS_DIR/venv" || {
    printf "${RED}✘ Failed to create Python virtual environment.${NC}\n"
    exit 1
}

source "$LIBREQOS_DIR/venv/bin/activate" || {
    printf "${RED}✘ Failed to activate Python virtual environment.${NC}\n"
    exit 1
}

printf "${BLUE}➜ Installing Python dependencies...${NC}\n"
if pip3 install \
    routeros-api \
    flask \
    psutil; then
    printf "${GREEN}✔ Dependencies installed successfully${NC}\n"
else
    printf "${RED}✘ Failed to install Python dependencies${NC}\n"
    deactivate
    exit 1
fi

deactivate

# ── Copy scripts ──────────────────────────────────────────────────────────

if [ -f "updatecsv.py" ]; then
    printf "${YELLOW}➜ Copying updatecsv.py...${NC}\n"
    cp "updatecsv.py" "$PYTHON_SCRIPT"
    chmod +x "$PYTHON_SCRIPT"
else
    printf "${YELLOW}➜ updatecsv.py not found in repository. Skipping...${NC}\n"
fi

if [ -f "gui.py" ]; then
    printf "${YELLOW}➜ Copying gui.py...${NC}\n"
    cp "gui.py" "$GUI_SCRIPT"
    chmod +x "$GUI_SCRIPT"
else
    printf "${YELLOW}➜ gui.py not found in repository. Skipping...${NC}\n"
fi

if [ -d "templates" ]; then
    printf "${YELLOW}➜ Copying templates/...${NC}\n"
    cp -r templates/. "$TEMPLATES_DIR/"
else
    printf "${YELLOW}➜ templates/ not found in repository. Skipping...${NC}\n"
fi

# ── config.json ───────────────────────────────────────────────────────────

if [ -f "config.json" ]; then
    if jq empty config.json >/dev/null 2>&1; then
        printf "${YELLOW}➜ Copying config.json...${NC}\n"
        cp "config.json" "$CONFIG_JSON"
        chmod 640 "$CONFIG_JSON"
    else
        printf "${RED}✘ Error: Invalid JSON in local config.json.${NC}\n"
        if [ -f "$CONFIG_JSON" ] && jq empty "$CONFIG_JSON" >/dev/null 2>&1; then
            printf "${YELLOW}➜ Keeping existing installed config at $CONFIG_JSON.${NC}\n"
        else
            printf "${YELLOW}➜ Falling back to default configuration.${NC}\n"
            rm -f "$CONFIG_JSON"
        fi
    fi
else
    printf "${YELLOW}➜ Creating default config.json...${NC}\n"
fi

if [ ! -s "$CONFIG_JSON" ]; then
    cat << 'EOF' > "$CONFIG_JSON"
{
    "strategy": "cpu",
    "promote_to_root": false,
    "queues": true,
    "routers": [
        {
            "name": "Mikrotik 1",
            "address": "192.168.88.1",
            "port": 8728,
            "username": "admin",
            "password": "password",
            "pppoe": {
                "enabled": true,
                "default_download_limit": 100,
                "default_upload_limit": 100
            },
            "hotspot": {
                "enabled": false,
                "default_download_limit": 10,
                "default_upload_limit": 10
            },
            "dhcp": {
                "enabled": false,
                "default_download_limit": 50,
                "default_upload_limit": 50
            },
            "address_list": {
                "default_download_limit": 100,
                "default_upload_limit": 100
            }
        }
    ]
}
EOF
    chmod 640 "$CONFIG_JSON"
fi

# ── updatecsv.service ─────────────────────────────────────────────────────

if [ -f "updatecsv.service" ]; then
    printf "${YELLOW}➜ Copying updatecsv.service...${NC}\n"
    cp "updatecsv.service" "$SERVICE_FILE"
else
    printf "${YELLOW}➜ Creating default updatecsv.service...${NC}\n"
    cat << 'EOF' > "$SERVICE_FILE"
[Unit]
Description=LibreQoS MikroTik Sync
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/opt/libreqos/src
Environment="VIRTUAL_ENV=/opt/libreqos/venv"
Environment="PATH=/opt/libreqos/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/opt/libreqos/venv/bin/python3 /opt/libreqos/src/updatecsv.py
Restart=always
RestartSec=10
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
EOF
fi

# ── gui.service ───────────────────────────────────────────────────────────

if [ -f "gui.service" ]; then
    printf "${YELLOW}➜ Copying gui.service...${NC}\n"
    cp "gui.service" "$GUI_SERVICE_FILE"
else
    printf "${YELLOW}➜ Creating default gui.service...${NC}\n"
    cat << 'EOF' > "$GUI_SERVICE_FILE"
[Unit]
Description=LibreQoS MikroTik GUI
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/opt/libreqos/src
Environment="VIRTUAL_ENV=/opt/libreqos/venv"
Environment="PATH=/opt/libreqos/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/opt/libreqos/venv/bin/python3 /opt/libreqos/src/gui.py
Restart=always
RestartSec=5
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
EOF
fi

# ── Permissions ───────────────────────────────────────────────────────────

chmod 755 "$SRC_DIR"

if ! jq empty "$CONFIG_JSON" >/dev/null 2>&1; then
    printf "${RED}✘ Error: Invalid JSON in $CONFIG_JSON${NC}\n"
    exit 1
fi

# ── Systemd ───────────────────────────────────────────────────────────────

printf "${BLUE}➜ Reloading systemd daemon...${NC}\n"
systemctl daemon-reload

printf "${BLUE}➜ Starting and enabling updatecsv service...${NC}\n"
systemctl enable --now updatecsv.service

printf "${BLUE}➜ Starting and enabling GUI service...${NC}\n"
systemctl enable --now gui.service

printf "${GREEN}✔ Installation complete!${NC}\n"
printf "\n"
printf "${GREEN}  Web GUI:${NC}  http://$(hostname -I | awk '{print $1}'):5000\n"
printf "${GREEN}  Password:${NC} admin  (change after first login)\n"
printf "\n"
printf "${BLUE}Service status:${NC}\n"
systemctl status updatecsv.service --no-pager -l
printf "\n"
systemctl status gui.service --no-pager -l