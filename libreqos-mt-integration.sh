#!/bin/bash

# Define the paths
LIBREQOS_DIR="/opt/libreqos"
SRC_DIR="$LIBREQOS_DIR/src"
PYTHON_SCRIPT="$SRC_DIR/updatecsv.py"
SERVICE_FILE="/etc/systemd/system/updatecsv.service"

# Create the directories if they don't exist
mkdir -p "$SRC_DIR"

# Write the Python script to the specified location
cat << 'EOF' > "$PYTHON_SCRIPT"
import csv
import logging
import time
import uuid
import routeros_api
from collections import OrderedDict

# Configuration
ROUTER_IP = '172.2.0.237'
USERNAME = 'demo'
PASSWORD = 'demo123'
CSV_FILE = 'ShapedDevices.csv'
FIELDNAMES = [
    'Circuit ID', 'Circuit Name', 'Device ID', 'Device Name', 'Parent Node',
    'MAC', 'IPv4', 'IPv6', 'Download Min Mbps', 'Upload Min Mbps',
    'Download Max Mbps', 'Upload Max Mbps', 'Comment'
]

# Logging configuration
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def connect_to_router():
    try:
        connection = routeros_api.RouterOsApiPool(
            ROUTER_IP, username=USERNAME, password=PASSWORD, plaintext_login=True
        )
        return connection.get_api()
    except Exception as e:
        logging.error(f"Connection error: {e}")
        return None

def get_ppp_secrets(api):
    try:
        return api.get_resource('/ppp/secret').get()
    except Exception as e:
        logging.error(f"Failed to get PPP secrets: {e}")
        return []

def get_profile_rate_limits(api, profile_name):
    try:
        profiles = api.get_resource('/ppp/profile').get(name=profile_name)
        if profiles:
            profile = profiles[0]
            rate_limit = profile.get('rate-limit', '0/0')  # Default to '0/0' if not found
            rx, tx = rate_limit.split('/')  # Split into download and upload rates
            return rx.rstrip('M'), tx.rstrip('M')  # Remove 'M' suffix and return values
        return '0', '0'  # Default values if profile not found
    except Exception as e:
        logging.error(f"Failed to get profile rate limits: {e}")
        return '0', '0'

def read_csv_data():
    try:
        with open(CSV_FILE, 'r') as f:
            return {row['Circuit Name']: row for row in csv.DictReader(f)}
    except FileNotFoundError:
        return OrderedDict()

def write_csv_data(data):
    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in data.values():
            writer.writerow(row)

def process_secrets(api, existing_data):
    current_secrets = {s['name']: s for s in get_ppp_secrets(api) if 'name' in s}
    updated = False

    # Check for deletions
    for name in list(existing_data.keys()):
        if name not in current_secrets:
            del existing_data[name]
            logging.info(f"Removed secret: {name}")
            updated = True

    # Process additions/updates
    for name, secret in current_secrets.items():
        entry = existing_data.get(name, {})

        # Generate IDs only for new entries
        if not entry:
            entry.update({
                'Circuit ID': str(uuid.uuid4()),
                'Device ID': str(uuid.uuid4())
            })

        # Get the assigned profile for the secret
        profile_name = secret.get('profile', '')
        rx, tx = get_profile_rate_limits(api, profile_name)

        # Convert rates to integers for calculation
        rx_int = int(rx) if rx.isdigit() else 0
        tx_int = int(tx) if tx.isdigit() else 0

        # Calculate min rates as 50% of max rates
        rx_min = str(int(rx_int * 0.5))  # 50% of download rate
        tx_min = str(int(tx_int * 0.5))  # 50% of upload rate

        # Extract values from MikroTik secret
        new_values = {
            'Circuit Name': name,
            'Device Name': name,
            'IPv4': secret.get('remote-address', ''),
            'Comment': secret.get('comment', ''),
            'Download Max Mbps': rx,  # Download rate from profile
            'Upload Max Mbps': tx,    # Upload rate from profile
            'Download Min Mbps': rx_min,  # 50% of download rate
            'Upload Min Mbps': tx_min     # 50% of upload rate
        }

        # Check for changes
        if any(entry.get(k) != v for k, v in new_values.items()):
            entry.update(new_values)
            logging.info(f"Updated secret: {name}")
            updated = True

        # Add empty fields for new entries
        if name not in existing_data:
            entry.update({'Parent Node': '', 'MAC': '', 'IPv6': ''})
            existing_data[name] = entry
            logging.info(f"Added new secret: {name}")
            updated = True

    return updated

def main_loop(api):
    while True:
        try:
            existing_data = read_csv_data()
            if process_secrets(api, existing_data):
                write_csv_data(existing_data)
            time.sleep(10)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    api = connect_to_router()
    if api:
        main_loop(api)
    else:
        logging.error("Failed to establish initial connection")
EOF

# Set the appropriate permissions for the Python script
chmod +x "$PYTHON_SCRIPT"

# Write the systemd service file
cat << 'EOF' > "$SERVICE_FILE"
[Unit]
Description=SyncMikrotiktoLibreQos
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/opt/libreqos/src
ExecStart=/usr/bin/python3 /opt/libreqos/src/updatecsv.py
Restart=always
RestartSec=10

Environment="PYTHONUNBUFFERED=1"
PermissionsStartOnly=true
ExecStartPre=/bin/chown -R root:root /opt/libreqos/src
ExecStartPre=/bin/chmod -R 755 /opt/libreqos/src

[Install]
WantedBy=multi-user.target
EOF

# Reload the systemd daemon
systemctl daemon-reload

# Start the service
systemctl start updatecsv.service

# Enable the service to run on boot
systemctl enable updatecsv.service

# Check the status of the service
systemctl status updatecsv.service
