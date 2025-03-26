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

mkdir -p "$SRC_DIR"

printf "${BLUE}➜ Installing system dependencies...${NC}\n"
if command -v apt-get &> /dev/null; then
    apt-get update
    apt-get install -y \
        python3 \
        python3-pip \
        python3-venv \
        jq \
        git
else
    printf "${RED}✘ Unsupported package manager. Please install dependencies manually.${NC}\n"
    exit 1
fi

printf "${BLUE}➜ Creating Python virtual environment...${NC}\n"
python3 -m venv "$LIBREQOS_DIR/venv"

source "$LIBREQOS_DIR/venv/bin/activate"

printf "${BLUE}➜ Installing Python dependencies...${NC}\n"
pip3 install \
    routeros-api && printf "${GREEN}✔ Dependencies installed successfully${NC}\n" || printf "${RED}✘ Failed to install dependencies${NC}\n"
    
deactivate

if [ -f "updatecsv.py" ]; then
    printf "${YELLOW}➜ Copying updatecsv.py from repository...${NC}\n"
    cp "updatecsv.py" "$PYTHON_SCRIPT"
    chmod +x "$PYTHON_SCRIPT"
else
    printf "${YELLOW}➜ updatecsv.py not found in repository. Skipping...${NC}\n"
fi

if [ -f "config.json" ]; then
    if jq empty config.json >/dev/null 2>&1; then
        printf "${YELLOW}➜ Copying config.json from repository...${NC}\n"
        cp "config.json" "$CONFIG_JSON"
        chmod 640 "$CONFIG_JSON"
    else
        printf "${RED}✘ Error: Invalid JSON in config.json. Using default configuration.${NC}\n"
        cp "$CONFIG_JSON" "$CONFIG_JSON.bak"
    fi
else
    printf "${YELLOW}➜ Creating default config.json...${NC}\n"
fi

if [ ! -s "$CONFIG_JSON" ]; then
    cat << 'EOF' > "$CONFIG_JSON"
{
    "flat_network": false,
    "no_parent": false,
    "preserve_network_config": false,
    "routers": [
        {
            "name": "Mikrotik 1",
            "address": "192.168.88.1",
            "port": 8728,
            "username": "admin",
            "password": "password",
            "dhcp": {
                "enabled": true,
                "download_limit_mbps": 1000,
                "upload_limit_mbps": 1000,
                "dhcp_server": [
                    "dhcp1",
                    "dhcp2"
                ]
            },
            "hotspot": {
                "enabled": true,
                "include_mac": true,
                "download_limit_mbps": 10,
                "upload_limit_mbps": 10
            },
            "pppoe": {
                "enabled": true,
                "per_plan_node": true
            }
        }
    ]
}
EOF
fi

if [ -f "updatecsv.service" ]; then
    printf "${YELLOW}➜ Copying updatecsv.service from repository...${NC}\n"
    cp "updatecsv.service" "$SERVICE_FILE"
else
    printf "${YELLOW}➜ Creating default updatecsv.service...${NC}\n"
    cat << 'EOF' > "$SERVICE_FILE"
[Unit]
Description=SyncMikrotiktoLibreQos
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/opt/libreqos/src
Environment="VIRTUAL_ENV=/opt/libreqos/venv"
Environment="PATH=/opt/libreqos/venv/bin:$PATH"
ExecStart=/opt/libreqos/venv/bin/python3 /opt/libreqos/src/updatecsv.py
Restart=always
RestartSec=10
Environment="PYTHONUNBUFFERED=1"
PermissionsStartOnly=true
ExecStartPre=/bin/chown -R root:root /opt/libreqos/src
ExecStartPre=/bin/chmod -R 755 /opt/libreqos/src

[Install]
WantedBy=multi-user.target
EOF
fi

chmod 755 "$SRC_DIR"

if ! jq empty "$CONFIG_JSON" >/dev/null 2>&1; then
    printf "${RED}✘ Error: Invalid JSON in $CONFIG_JSON${NC}\n"
    exit 1
fi

printf "${BLUE}➜ Reloading systemd daemon...${NC}\n"
systemctl daemon-reload

printf "${BLUE}➜ Starting updatecsv service...${NC}\n"
systemctl start updatecsv.service

printf "${BLUE}➜ Enabling updatecsv service...${NC}\n"
systemctl enable updatecsv.service

printf "${GREEN}✔ Installation complete. Checking service status:${NC}\n"
systemctl status updatecsv.service