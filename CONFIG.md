# config.json Reference

`config.json` is the single configuration file that controls how the integration connects to your MikroTik routers, what data sources are enabled, and how devices are structured in LibreQoS.

---

## Top-Level Structure

```json
{
    "strategy": "cpu",
    "promote_to_root": false,
    "queues": 2,
    "routers": [ ... ]
}
```

---

## Global Settings

### `strategy`

Controls how devices are organized in `network.json` (the LibreQoS topology file).

| Value | Description |
|-------|-------------|
| `"cpu"` | **(Default)** Greedy bin-pack all devices across CPU nodes for maximum queue parallelism. Best for large flat networks. |
| `"flat"` | No parent hierarchy. Writes an empty `network.json`. Maximum performance, minimum visibility. |
| `"ap_only"` | Groups devices under their router name as a parent node. Good for multi-router deployments where per-router visibility matters. |
| `"ap_site"` | Groups devices under a site → router hierarchy. Requires a `"site"` key on each router. Better aggregation for multi-site networks. |
| `"full"` | Same as `ap_site` with full path shaping intent. Pair with `"promote_to_root": true` if single-core saturation occurs. |

**Choosing a strategy:**

```
Need hierarchy visibility?
  No  → flat
  Yes → Need site-level aggregation?
          No  → ap_only
          Yes → Need full backhaul/path shaping?
                  No  → ap_site
                  Yes → full
                          Single-core saturation?
                            Yes → set promote_to_root: true
```

**Example:**
```json
"strategy": "ap_only"
```

---

### `promote_to_root`

| Type | Default |
|------|---------|
| `boolean` | `false` |

Only relevant when `strategy` is `"full"`. When `true`, additionally distributes devices across CPU queue nodes on top of the site/router hierarchy. Use this to resolve single-core saturation without switching to a simpler strategy.

```json
"strategy": "full",
"promote_to_root": true
```

---

### `queues`

| Type | Default |
|------|---------|
| `integer`, `true`, or `false` | `true` |

Controls the number of CPU queue nodes when `strategy` is `"cpu"` (or when `promote_to_root` is active).

| Value | Behavior |
|-------|----------|
| `true` | Auto-detect from system CPU count |
| `false` | Skip `network.json` entirely (no queue nodes created) |
| `2`, `4`, `8`, ... | Use exactly this many CPU queue nodes |

```json
"queues": 4
```

> **Tip:** Set `queues` to match the number of physical CPU cores available to LibreQoS for optimal bin-packing.

---

## Router Settings

The `routers` array defines one or more MikroTik routers to poll. Each router is polled independently every scan cycle.

```json
"routers": [
    {
        "name": "Main Router",
        "address": "192.168.88.1",
        "port": 8728,
        "username": "admin",
        "password": "your-password",
        "site": "HQ",
        "pppoe": { ... },
        "hotspot": { ... },
        "dhcp": { ... },
        "address_list": { ... }
    }
]
```

### Connection Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Friendly display name. If two routers share the same name, the second is auto-renamed (e.g., `"Mikrotik 1 2"`). |
| `address` | string | Yes | IP address or hostname of the router. |
| `port` | integer | Yes | RouterOS API port. Default is `8728`; use `8729` for TLS. |
| `username` | string | Yes | API username. |
| `password` | string | Yes | API password. Can be empty string `""` for no password. |
| `site` | string | No | Site name for `ap_site` and `full` strategies. Devices from this router will be nested under this site in `network.json`. |

---

### `pppoe`

Tracks active PPPoE sessions from `/ppp/active`.

```json
"pppoe": {
    "enabled": true,
    "default_download_limit": 1000,
    "default_upload_limit": 1000
}
```

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Enable or disable PPPoE session tracking for this router. |
| `default_download_limit` | integer (Mbps) | Fallback download speed when no rate can be resolved from the firewall address list. |
| `default_upload_limit` | integer (Mbps) | Fallback upload speed when no rate can be resolved from the firewall address list. |

**How rates are resolved for PPPoE:**
The session's IP is looked up in the MikroTik firewall address list. The list name must follow the `X/X` rate format (e.g., `50M/50M`). If no match is found, the default limits above are used.

Device code format: `PPP-{session_name}`

---

### `hotspot`

Tracks active hotspot sessions from `/ip/hotspot/active`.

```json
"hotspot": {
    "enabled": true,
    "default_download_limit": 1000,
    "default_upload_limit": 1000
}
```

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Enable or disable hotspot session tracking for this router. |
| `default_download_limit` | integer (Mbps) | Fallback download speed when no rate is resolved. |
| `default_upload_limit` | integer (Mbps) | Fallback upload speed when no rate is resolved. |

**How rates are resolved for hotspot:**
Same as PPPoE — the session IP is matched against the firewall address list. The list name is expected to be a rate string like `10M/10M`.

Device code format: `HS-{MAC}` or `HS-{username}` if MAC is unavailable.

---

### `dhcp`

Tracks DHCP leases from `/ip/dhcp-server/lease`.

```json
"dhcp": {
    "enabled": true,
    "default_download_limit": 1000,
    "default_upload_limit": 1000
}
```

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Enable or disable DHCP lease tracking for this router. |
| `default_download_limit` | integer (Mbps) | Fallback download speed. |
| `default_upload_limit` | integer (Mbps) | Fallback upload speed. |

**How rates are resolved for DHCP:**
The lease must have a valid rate in its `address-list` field (e.g., `50M/50M`), **and** the lease IP must appear in the firewall address list. Leases that don't meet both conditions are skipped.

Device code format: `DHCP-{hostname}` or `DHCP-{MAC}` if hostname is unavailable.

---

### `address_list`

Processes standalone firewall address list entries from `/ip/firewall/address-list` where the list name itself is a rate string.

```json
"address_list": {
    "default_download_limit": 100,
    "default_upload_limit": 100
}
```

| Field | Type | Description |
|-------|------|-------------|
| `default_download_limit` | integer (Mbps) | Fallback download speed. |
| `default_upload_limit` | integer (Mbps) | Fallback upload speed. |

> Note: `address_list` has no `enabled` flag — it is always active. Entries are only included if their list name parses as a valid rate (e.g., `100M/100M`) and are not disabled.

Device code format: the entry's `comment` field if set, otherwise `ADDR-{IP}`.

---

## Rate String Format

Rates are expressed as `{download}/{upload}` with an optional unit suffix:

| Format | Meaning |
|--------|---------|
| `50M/50M` | 50 Mbps down / 50 Mbps up |
| `100M/50M` | 100 Mbps down / 50 Mbps up |
| `1G/1G` | 1000 Mbps down / 1000 Mbps up |
| `512k/256k` | 0.51 Mbps down / 0.26 Mbps up |

The script applies these multipliers automatically:
- **Max rate** = parsed value × 1.15
- **Min rate** = max rate × 0.50

---

## Source Priority

When the same IP is seen from multiple sources in one scan cycle, the highest-priority source wins:

| Priority | Source |
|----------|--------|
| 4 (highest) | `pppoe` |
| 3 | `hotspot` |
| 2 | `dhcp` |
| 1 (lowest) | `address_list` |

The lower-priority device record is deleted and replaced by the higher-priority one.

---

## Static Entries

Any device stored in the SQLite database with `is_static = 1` is never overwritten or deleted by the sync loop. To mark a device as static, set it directly in the database:

```sql
UPDATE devices SET is_static = 1 WHERE code = 'PPP-myuser';
```

Static devices retain their `parent_node`, rate limits, and all other fields across scans.

---

## Duplicate Router Names

If two routers in the `routers` array share the same `name`, the script automatically appends an incrementing suffix to prevent collisions:

```
"Mikrotik 1"   → "Mikrotik 1"   (first occurrence)
"Mikrotik 1"   → "Mikrotik 1 2" (second occurrence)
```

This suffix also affects parent node names in `ap_only`/`ap_site`/`full` strategies, so prefer unique router names.

---

## Complete Example

### Single router, CPU strategy (default)

```json
{
    "strategy": "cpu",
    "queues": 4,
    "routers": [
        {
            "name": "Core Router",
            "address": "192.168.88.1",
            "port": 8728,
            "username": "libreqos",
            "password": "strongpassword",
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

### Multi-router, ap_site strategy with site grouping

```json
{
    "strategy": "ap_site",
    "routers": [
        {
            "name": "Site A Router",
            "address": "10.0.1.1",
            "port": 8728,
            "username": "libreqos",
            "password": "pass1",
            "site": "Site A",
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
        },
        {
            "name": "Site B Router",
            "address": "10.0.2.1",
            "port": 8728,
            "username": "libreqos",
            "password": "pass2",
            "site": "Site B",
            "pppoe": {
                "enabled": true,
                "default_download_limit": 100,
                "default_upload_limit": 100
            },
            "hotspot": {
                "enabled": true,
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

This produces a `network.json` shaped like:
```
Site A
└── Site A Router
        └── (devices)
Site B
└── Site B Router
        └── (devices)
```

### Full strategy with promote_to_root

```json
{
    "strategy": "full",
    "promote_to_root": true,
    "queues": 8,
    "routers": [ ... ]
}
```

Use this when `full` strategy causes single-core CPU saturation in LibreQoS. The `queues` value should match available CPU cores.

---

## MikroTik API User Setup

Create a read-only API user on each router:

```shell
/user group add name=LibreQoS_API \
    policy="read,sensitive,api,!policy,!local,!telnet,!ssh,!ftp,!reboot,!write,!test,!winbox,!password,!web,!sniff,!romon"

/user add name="libreqos" group=LibreQoS_API \
    password="<strong-password>" \
    address="<LibreQoS-server-IP>" \
    disabled=no
```
