# LibreQoS MikroTik Integration

This script automates synchronization of active MikroTik users (PPPoE, Hotspot, IPoE/DHCP, and Address List) with LibreQoS's `ShapedDevices.csv` and `network.json`. It supports RADIUS and User Manager authentication. It runs continuously as a systemd service, polling your routers on a fixed interval and triggering a LibreQoS update whenever anything changes.

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

6. **Logging and systemd integration**
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
- `routeros_api` Python library
- MikroTik router with API access enabled
- LibreQoS installed at `/opt/libreqos`

---

## Installation

```bash
git clone https://github.com/Kintoyyy/MikroTik-LibreQos-Integration
cd MikroTik-LibreQos-Integration
chmod +x install_updatecsv.sh
sudo ./install_updatecsv.sh
```

See [Installation.md](Installation.md) for the full step-by-step guide.

---

## Configuration

Edit `/opt/libreqos/src/config.json` after installation:

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

- How to create address list entries with rate names
- PPPoE and Hotspot rate lookup flow
- DHCP lease rate configuration
- Rate format reference (`50M/50M`, `1G/1G`, `512k/256k`)

> **Using User Manager?**
> Assign rates via user groups with the `Mikrotik-Address-List` attribute (e.g., `attributes=Mikrotik-Address-List:50M/50M`) instead of `Mikrotik-Rate-Limit`. The integration reads rates from the firewall address list name — see [MIKROTIK_RATE_SETUP.md](MIKROTIK_RATE_SETUP.md) for setup details.

---

## Output Files

| File | Description |
|------|-------------|
| `ShapedDevices.csv` | LibreQoS device shaping table |
| `network.json` | LibreQoS topology/queue node tree |
| `devices.db` | SQLite state database |

**CSV columns:**
`Circuit ID`, `Circuit Name`, `Device ID`, `Device Name`, `Parent Node`, `MAC`, `IPv4`, `IPv6`, `Download Min Mbps`, `Upload Min Mbps`, `Download Max Mbps`, `Upload Max Mbps`, `Comment`

**Example rows:**
```
Circuit ID,Circuit Name,Device ID,Device Name,Parent Node,MAC,IPv4,IPv6,Download Min Mbps,Upload Min Mbps,Download Max Mbps,Upload Max Mbps,Comment
A1B2C3D4,PPP-john,E5F6G7H8,PPP-john,CPU0,AA:BB:CC:DD:EE:FF,10.0.0.5,,46,46,92,92,pppoe | 80M/80M | 2026-03-25 10:00:00
X9Y8Z7W6,HS-AABBCCDD,Q1R2S3T4,HS-AABBCCDD,CPU1,AA:BB:CC:11:22:33,10.0.0.6,,6,6,12,12,hotspot | 10M/10M | 2026-03-25 10:00:00
```

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

## Service Management

```bash
# Check status
sudo systemctl status updatecsv.service

# View logs
journalctl -u updatecsv.service -f

# Restart after config change
sudo systemctl restart updatecsv.service
```

---

## Donations

If this project has helped you, consider supporting it:

- **PayPal**: [Donate via PayPal](https://paypal.me/Kintoyyyy?country.x=PH)
- **Buy Me a Coffee**: [Buy Me a Coffee](https://www.buymeacoffee.com/kintoyyy)

<img src="https://i.imgur.com/nfxbhOv.jpeg" alt="LibreQoS MikroTik Integration" width="500" />

For issues or feature requests, [open an issue on GitHub](https://github.com/Kintoyyy/MikroTik-LibreQos-Integration/issues).

Happy networking!
