# LibreQoS MikroTik Integration

Automates synchronization of active MikroTik users (PPPoE, Hotspot, IPoE/DHCP, and Address List) with LibreQoS's `ShapedDevices.csv` and `network.json`. Supports RADIUS and User Manager authentication. Runs continuously as a systemd service, polling your routers on a fixed interval and triggering a LibreQoS update whenever anything changes.

Includes a full **web-based GUI** for configuration, monitoring, device management, and troubleshooting — accessible from any browser including mobile.

---

## Key Features

1. **Multi-source device tracking**
   - PPPoE active sessions (`/ppp/active`)
   - Hotspot active sessions (`/ip/hotspot/active`)
   - IPoE/DHCP leases (`/ip/dhcp-server/lease`)
   - Firewall address list entries (`/ip/firewall/address-list`)
   - RADIUS and User Manager authentication supported
   - Each source can be enabled/disabled per router

2. **Automatic rate resolution**
   - Rates are read from MikroTik **firewall address list names** in `X/X` format (e.g., `50M/50M`)
   - For PPPoE/Hotspot: session IP is matched against address list to find the rate
   - For IPoE/DHCP: rate is read from the static lease's `address-list` field
   - For RADIUS/User Manager: set `Mikrotik-Address-List` attribute — no `Mikrotik-Rate-Limit` needed
   - Falls back to configurable per-source defaults when no rate is found
   - Min rate = 50% of max; applied max = parsed rate × 1.15

3. **Flexible topology strategies**
   - `cpu` — greedy bin-pack across CPU queue nodes (best performance at scale)
   - `flat` — no hierarchy, empty `network.json`
   - `ap_only` — group devices under their router as parent
   - `ap_site` — group devices under site → router hierarchy
   - `full` — full path/backhaul shaping with optional `promote_to_root`

4. **Multi-router support**
   - Poll any number of MikroTik routers in a single scan cycle
   - Per-router service settings (PPPoE, Hotspot, IPoE/DHCP, Address List)
   - Duplicate IP conflict resolution by source priority

5. **Source priority deduplication**
   - When the same IP appears in multiple sources, the highest-priority source wins
   - Priority order: PPPoE > Hotspot > IPoE/DHCP > Address List

6. **Web GUI**
   - Password-protected dashboard with live CPU, memory, and interface throughput graphs
   - Form-based settings editor — no raw JSON required
   - Netplan / network interface configuration
   - Management port (DHCP or static IP) configuration
   - Service control (start / restart / stop) with live logs
   - File editor for `config.json`, `network.json`, `updatecsv.py`, and more
   - Sankey topology diagram showing bandwidth flow from parent nodes to devices
   - Device table with manual add / edit / delete
   - Troubleshooting tools: MikroTik ping, API connect, permission check, lqusers reset
   - One-click LibreQoS installer with live streaming output
   - Mobile-friendly responsive layout

7. **Logging and systemd integration**
   - Structured logging for all additions, updates, and removals
   - Runs as a `systemd` service with automatic restart on failure

---

## How It Works

1. **Reads `config.json`** — loads router credentials, enabled sources, default rates, and topology strategy.

2. **Connects to each router** — using the RouterOS API (`routeros_api` library).

3. **Fetches active sessions** — PPPoE, Hotspot, IPoE/DHCP leases, and firewall address list entries.

4. **Resolves rates** — looks up each device's IP in the firewall address list. The list name (e.g., `50M/50M`) becomes the rate. See [MIKROTIK_RATE_SETUP.md](MIKROTIK_RATE_SETUP.md) for how to configure this on the router.

5. **Upserts to SQLite** (`devices.db`) — adds new devices, updates changed ones, and removes devices not seen in the current scan.

6. **Builds `network.json`** — based on the configured `strategy`, distributes devices into queue nodes or a hierarchy.

7. **Exports `ShapedDevices.csv`** — writes all active devices in LibreQoS CSV format.

8. **Triggers LibreQoS** — runs `LibreQoS.py --updateonly` to apply the new configuration.

---

## Prerequisites

- Linux (Debian/Ubuntu) with Python 3.7+
- `routeros_api`, `flask`, `psutil` Python libraries (installed automatically)
- MikroTik router with API access enabled
- LibreQoS installed at `/opt/libreqos` (can be installed via the GUI)

---

## Installation

```bash
git clone https://github.com/Kintoyyy/MikroTik-LibreQos-Integration
cd MikroTik-LibreQos-Integration
chmod +x install_updatecsv.sh
sudo ./install_updatecsv.sh
```

The installer will:
- Install system dependencies (`python3`, `python3-venv`, `jq`, `git`)
- Create a Python virtual environment at `/opt/libreqos/venv`
- Install Python dependencies (`routeros-api`, `flask`, `psutil`)
- Copy `updatecsv.py`, `gui.py`, and `templates/` to `/opt/libreqos/src/`
- Create and enable `updatecsv.service` and `gui.service`
- Print the GUI URL and default credentials on completion

After installation, open the GUI in your browser:

```
http://<server-ip>:5000
```

Default password: **`admin`** — change it immediately via the key icon in the top bar.

See [Installation.md](Installation.md) for the full step-by-step guide.

---

## Web GUI

The GUI runs as a separate systemd service (`gui.service`) on port **5000**.

### Tabs

| Tab | Description |
|-----|-------------|
| **Dashboard** | Live CPU, memory, and interface throughput graphs; device count by source |
| **Settings** | Form-based `config.json` editor — routers, sources, topology strategy. Saving restarts `updatecsv` automatically |
| **Services** | Start / restart / stop all integration and LibreQoS services; view live logs |
| **Network** | Netplan configuration — Option A (Linux bridge) or Option B (Bifrost XDP); interface cards with speed and IP |
| **Management Port** | Configure the management interface as DHCP or static IP (`50-cloud-init.yaml`) |
| **Files** | Edit `config.json`, `network.json`, `updatecsv.py`, and other files directly in the browser |
| **Topology** | Sankey diagram showing bandwidth flow from parent nodes to devices; overview and detail modes |
| **Devices** | Live device table with search and filter; manually add, edit, or delete devices |
| **Troubleshooting** | MikroTik API diagnostics, lqusers reset, and one-click LibreQoS installer |

### Authentication

- Login required before any page or API endpoint is accessible
- Password is stored as a PBKDF2-SHA256 hash in `gui_auth.json`
- Change password anytime via the key icon in the top bar
- Sessions are invalidated on GUI service restart

### Service Management

```bash
# GUI service
sudo systemctl status gui.service
sudo systemctl restart gui.service
journalctl -u gui.service -f

# Sync service
sudo systemctl status updatecsv.service
sudo systemctl restart updatecsv.service
journalctl -u updatecsv.service -f
```

---

## Configuration

Edit `/opt/libreqos/src/config.json` after installation (or use the Settings tab in the GUI):

```json
{
    "strategy": "cpu",
    "promote_to_root": false,
    "queues": true,
    "routers": [
        {
            "name": "Core Router",
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
                "enabled": true,
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

For the full configuration reference, see [CONFIG.md](CONFIG.md).

---

## Setting Up Rates on MikroTik

Rates are driven by **firewall address list names** on the MikroTik side. See [MIKROTIK_RATE_SETUP.md](MIKROTIK_RATE_SETUP.md) for step-by-step instructions covering:

- PPPoE and Hotspot profile `address-list` setup
- IPoE/DHCP static lease rate configuration
- RADIUS / User Manager (`Mikrotik-Address-List` attribute)
- Rate format reference (`50M/50M`, `1G/1G`, `512k/256k`)

> **Using RADIUS or User Manager?**
> Return `Mikrotik-Address-List = 50M/50M` in your Access-Accept — do **not** use `Mikrotik-Rate-Limit`. The integration reads rates from the firewall address list name. See [MIKROTIK_RATE_SETUP.md](MIKROTIK_RATE_SETUP.md) for details.

---

## Output Files

| File | Description |
|------|-------------|
| `ShapedDevices.csv` | LibreQoS device shaping table |
| `network.json` | LibreQoS topology/queue node tree |
| `devices.db` | SQLite state database |
| `gui_auth.json` | GUI password hash (auto-created, do not share) |

**CSV columns:**
`Circuit ID`, `Circuit Name`, `Device ID`, `Device Name`, `Parent Node`, `MAC`, `IPv4`, `IPv6`, `Download Min Mbps`, `Upload Min Mbps`, `Download Max Mbps`, `Upload Max Mbps`, `Comment`

---

## MikroTik API User Setup

Create a minimal read-only API user on each router:

```shell
/user group add name=LibreQoS_API \
    policy="read,sensitive,api,!policy,!local,!telnet,!ssh,!ftp,!reboot,!write,!test,!winbox,!password,!web,!sniff,!romon"

/user add name="libreqos" group=LibreQoS_API \
    password="<strong-password>" \
    address="<LibreQoS-server-IP>" \
    disabled=no
```

---

## Donations

If this project has helped you, consider supporting it:

- **PayPal**: [Donate via PayPal](https://paypal.me/Kintoyyyy?country.x=PH)
- **Buy Me a Coffee**: [Buy Me a Coffee](https://www.buymeacoffee.com/kintoyyy)

<img src="https://i.imgur.com/nfxbhOv.jpeg" alt="LibreQoS MikroTik Integration" width="500" />

For issues or feature requests, [open an issue on GitHub](https://github.com/Kintoyyy/MikroTik-LibreQos-Integration/issues).

Happy networking!
