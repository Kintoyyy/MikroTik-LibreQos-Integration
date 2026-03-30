# Installation Guide

## Prerequisites

Before installing, ensure you have:

- A Linux system running a Debian-based distribution (Ubuntu recommended)
- Python 3.7 or higher
- A MikroTik router with:
  - API access enabled
  - Firewall address list entries configured with rate names (see [MIKROTIK_RATE_SETUP.md](MIKROTIK_RATE_SETUP.md))
- LibreQoS installed and configured at `/opt/libreqos`

---

## Installation Steps

### 1. Prepare Your System

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3 python3-pip python3-venv jq
```

### 2. Clone the Repository

```bash
git clone https://github.com/Kintoyyy/MikroTik-LibreQos-Integration
cd MikroTik-LibreQos-Integration
```

### 3. Run the Installation Script

```bash
chmod +x install_updatecsv.sh
sudo ./install_updatecsv.sh
```

The script will:
- Install Python dependencies into a virtual environment at `/opt/libreqos/venv`
- Copy `updatecsv.py` to `/opt/libreqos/src/`
- Create a default `config.json` at `/opt/libreqos/src/config.json` if none exists
- Install and start the `updatecsv` systemd service

---

## Post-Installation Verification

```bash
# Verify files were installed
ls /opt/libreqos/src/updatecsv.py
ls /opt/libreqos/src/config.json

# Check service is running
sudo systemctl status updatecsv.service
```

---

## Configuration

Edit the config file to match your router details:

```bash
sudo nano /opt/libreqos/src/config.json
```

**Minimal single-router example:**

```json
{
    "strategy": "cpu",
    "promote_to_root": false,
    "queues": true,
    "routers": [
        {
            "name": "Main Router",
            "address": "192.168.88.1",
            "port": 8728,
            "username": "libreqos",
            "password": "your-password",
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
```

**Key config fields:**

| Field | Description |
|-------|-------------|
| `strategy` | Topology mode: `cpu`, `flat`, `ap_only`, `ap_site`, `full` |
| `promote_to_root` | Distribute across CPU nodes within `full` strategy (reduces single-core saturation) |
| `queues` | CPU queue count: `true` = auto-detect, `false` = skip, or an integer |
| `name` | Friendly router name (used as parent node label in `ap_only`/`ap_site`/`full`) |
| `address` | Router IP address |
| `port` | RouterOS API port (default `8728`) |
| `username` / `password` | API credentials |
| `pppoe.enabled` | Track active PPPoE sessions |
| `hotspot.enabled` | Track active Hotspot sessions |
| `dhcp.enabled` | Track DHCP leases |
| `default_download_limit` | Fallback rate (Mbps) when no address list match found |
| `default_upload_limit` | Fallback rate (Mbps) when no address list match found |

For the full configuration reference, see [CONFIG.md](CONFIG.md).

For setting up rates on your MikroTik router, see [MIKROTIK_RATE_SETUP.md](MIKROTIK_RATE_SETUP.md).

---

## Restart After Configuration Changes

```bash
sudo systemctl restart updatecsv.service

# Watch live logs
journalctl -u updatecsv.service -f
```

---

## Troubleshooting

### Service not running

```bash
journalctl -u updatecsv.service --no-pager --since "1 hour ago"
/opt/libreqos/venv/bin/pip show routeros_api
```

### CSV not updating

- Verify router credentials in `config.json`
- Confirm there are active PPPoE/Hotspot/DHCP sessions on the router
- Validate JSON syntax:
  ```bash
  jq . /opt/libreqos/src/config.json
  ```
- Confirm address list entries exist and use a valid rate format (e.g., `50M/50M`)

### Permission problems

```bash
sudo chown -R root:root /opt/libreqos/src
sudo chmod -R 755 /opt/libreqos/src
sudo chmod 640 /opt/libreqos/src/config.json
```

---

## Uninstallation

```bash
chmod +x uninstall.sh
sudo ./uninstall.sh
```

---

## Donations

If this project has helped you, consider supporting it:

- **PayPal**: [Donate via PayPal](https://paypal.me/Kintoyyyy?country.x=PH)
- **Buy Me a Coffee**: [Buy Me a Coffee](https://www.buymeacoffee.com/kintoyyy)

<img src="https://i.imgur.com/nfxbhOv.jpeg" alt="LibreQoS MikroTik Integration" width="500" />

For issues or feature requests, [open an issue on GitHub](https://github.com/Kintoyyy/MikroTik-LibreQos-Integration/issues).

Happy Networking!
