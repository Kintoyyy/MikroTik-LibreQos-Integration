# LibreQoS MikroTik PPP and Active Hotspot User Sync

## Overview

This script synchronizes MikroTik router PPP secrets (PPPoE users) and active hotspot users with a LibreQoS-compatible CSV file (`ShapedDevices.csv`). It continuously monitors the MikroTik router and ensures the CSV file remains up to date.

## Prerequisites

Before installation, ensure you have:

- A Linux system running a Debian-based distribution ( Ubuntu )
- Python 3.7 or higher
- A MikroTik router with:
  - Enabled API access
  - Configured PPP secrets and/or hotspot users
- LibreQoS installed and configured

## Installation Steps

### 1. Prepare Your System

```bash
# Update system packages
sudo apt update
sudo apt upgrade -y

# Install required system dependencies
sudo apt install -y \
    git \
    python3 \
    python3-pip \
    python3-venv \
    jq
```

### 2. Clone the Repository

```bash
# Clone the repository
git clone https://github.com/Kintoyyy/MikroTik-LibreQos-Integration

# Navigate to the project directory
cd MikroTik-LibreQos-Integration
```

### 3. Run the Installation Script

```bash
# Run the installation script with sudo
sudo ./install_updatecsv.sh
```

## Post-Installation Verification

### Check Installation Components

```bash
# Verify Python script location
ls /opt/libreqos/src/updatecsv.py

# Verify systemd service file
ls /etc/systemd/system/updatecsv.service

# Check service status
sudo systemctl status updatecsv.service
```

## Configuration

### Editing Configuration

The configuration file is located at `/opt/libreqos/src/config.json`. 

```bash
# Edit the configuration
sudo nano /opt/libreqos/src/config.json
```

   ```json
   {
      "flat_network": false,
      "no_parent": false,
      "preserve_network_config": false,
       "routers": [
           {
               "name": "MikroTik-XYZ",
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
   ```

### Configuration Parameters

#### Global Settings
- `flat_network`: Enable single network without hierarchical nodes
- `no_parent`: Disable parent node creation
- `preserve_network_config`: Allow dynamic node updates

#### Router Connection
- `name`: Router nickname
- `address`: Router IP address
- `port`: API port (default: 8728)
- `username`: API username
- `password`: API password

#### Service Configurations
- DHCP
- Hotspot
- PPPoE

Each service can be enabled/disabled with specific bandwidth limits.

### Restart Service After Configuration

```bash
# Restart the service after configuration changes
sudo systemctl restart updatecsv.service

# View recent logs
journalctl -u updatecsv.service --no-pager --since "1 hour ago"
```

## Troubleshooting

### Common Issues

1. **Service Not Running**
   ```bash
   # Check service logs
   journalctl -u updatecsv.service

   # Verify RouterOS API installation
   /opt/libreqos/venv/bin/pip show routeros_api
   ```

2. **CSV Not Updating**
   - Verify router details in `config.json`
   - Confirm PPP/Hotspot users exist on router
   - Validate JSON configuration:
     ```bash
     jq . /opt/libreqos/src/config.json
     ```

3. **Permission Problems**
   ```bash
   sudo chown -R root:root /opt/libreqos/src
   sudo chmod -R 755 /opt/libreqos/src
   sudo chmod 640 /opt/libreqos/src/config.json
   ```

## Uninstallation

```bash
# Run uninstallation script
sudo ./uninstall.sh
```
---
### **Donations**

If this script has helped you streamline your network management, synchronize MikroTik PPP and hotspot users with LibreQoS, or saved you time and effort, please consider supporting the development and maintenance of this project. Your donations help ensure that the script remains up-to-date, reliable, and free for everyone to use.

#### **How to Donate**
You can support this project by donating via the following methods:

- **PayPal**: [Donate via PayPal](https://paypal.me/Kintoyyyy?country.x=PH)  
- **Buy Me a Coffee**: [Buy Me a Coffee](https://www.buymeacoffee.com/kintoyyy)  


<img src="https://i.imgur.com/nfxbhOv.jpeg" alt="LibreQoS MikroTik Sync" width="500" />

Every contribution, no matter how small, is greatly appreciated and helps keep this project alive. Thank you for your support!

---

## Contact and Feedback

For issues, feature requests, or suggestions, please [open an issue](https://github.com/Kintoyyy/MikroTik-LibreQos-Integration/issues) on the GitHub repository.

Happy Networking! 🚀